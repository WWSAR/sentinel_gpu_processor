#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>
#include <thread>

#include "gpu_device.hpp"
#include <cuda_runtime.h>
#include <sario.hpp>

// ----------------- Utility -----------------
#define CHECK_CUDA(x)                                                          \
  do {                                                                         \
    cudaError_t err = (x);                                                     \
    if (err != cudaSuccess) {                                                  \
      std::cerr << "CUDA error " << cudaGetErrorString(err) << " at "          \
                << __FILE__ << ":" << __LINE__ << std::endl;                   \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

typedef float2 Complex;

/**
 * read an interferogram and store it in an interfergram stack
 * @param filename Filename of the interferogram
 * @param stack 3D image stack
 * @param ifg_idx Index of the current interferogram
 * @param nlines Number of rows to read
 * @param ncol Number of columns of each interferogram
 * @param rowstart The first row to read
 */
void read_one_ifg(const std::string &filename, Complex *stack, int ifg_idx,
                  int nlines, int ncol, int rowstart) {
  std::ifstream fb(filename, std::ios::binary);
  if (!fb.is_open()) {
    std::cerr << "Cannot open: " << filename << std::endl;
  }

  // skip rows before rowstart
  std::size_t offset = (std::size_t)rowstart * ncol * sizeof(Complex);
  fb.seekg(offset, std::ios::beg);

  // output location
  std::size_t base_idx = (std::size_t)ifg_idx * nlines * ncol;
  std::size_t nelement = (std::size_t)nlines * ncol;

  // direct binary read
  fb.read(reinterpret_cast<char *>(stack + base_idx),
          nelement * sizeof(Complex));

  if (!fb) {
    std::cerr << "Read failed: " << filename << std::endl;
  }

  fb.close();
}

/**
 * read multiple interferograms into a 3D image stack
 * @param infile Input txt file
 * @param nifg Number of interferograms
 * @param nrow Number of rows of each interferogram
 * @param ncol Number of columns of each interferogram
 * @param rowstart First row to read (included)
 * @param rowend Last row to read (excluded)
 */
std::string *read_ifg_list(const std::string &infile, int &nifg, int &nrow,
                           int &ncol) {
  std::ifstream fin(infile);

  if (!fin.is_open()) {
    std::cerr << "Cannot open input file " << infile << std::endl;

    return nullptr;
  }

  fin >> nifg >> nrow >> ncol;

  std::string *filenames = new std::string[nifg];

  for (int i = 0; i < nifg; ++i) {
    fin >> filenames[i];
  }

  fin.close();
  return filenames;
}

/**
 * read multiple interferograms into a 3D image stack
 * @param infile Input txt file
 * @param nifg Number of interferograms
 * @param nrow Number of rows of each interferogram
 * @param ncol Number of columns of each interferogram
 * @param rowstart First row to read (included)
 * @param rowend Last row to read (excluded)
 */
void read_ifg_stack(Complex *stack, const std::string *ifg_list, int &nifg,
                    int &nrow, int &ncol, int &rowstart, int &rowend) {
  // sequential reading
  for (int i = 0; i < nifg; ++i) {
    read_one_ifg(ifg_list[i], stack, i, rowend - rowstart, ncol, rowstart);
  }

  return;
}

/*
 * Normalize a complex image stack
 * @param stack A 3D complex image stack
 * @param nifg Number of interferograms
 * @param nrow Number of rows of each interferogram
 * @param ncol Number of columns of each interferogram
 */
__global__ void normalize(Complex *__restrict__ stack, std::size_t n) {
  std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= n)
    return;

  Complex val = stack[idx];
  float mag = sqrtf(val.x * val.x + val.y * val.y);
  if (mag > 1e-8f) {
    float inv_mag = 1.0f / mag;
    stack[idx].x *= inv_mag;
    stack[idx].y *= inv_mag;
    if (idx == 382 * 3015 + 460) {
      printf("x:%f,y:%f,inv_mag:%f\n", stack[idx].x, stack[idx].y, inv_mag);
    }
  } else {
    stack[idx].x = 0.0f;
    stack[idx].y = 0.0f;
  }
}

/*
 * Update PS candidates based on phase similarity measurements
 * @param ps 2D PS array with 1 representing PS pixels
 * @param ph_sim Phase similarity (can be either median or maximum phase
 * similarity)
 * @param threshold Phase similarity threshold
 * @param overwrite Overwrite exisiting PS pixels
 * @param n Number of radar pixels
 */
__global__ void update_ps(int32_t *__restrict__ ps,
                          const float *__restrict__ ph_sim,
                          const float threshold, const bool overwrite,
                          const int n) {
  std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (int(idx) >= n)
    return;
  if (ph_sim[idx] >= threshold) {
    ps[idx] = 1;
  } else if (overwrite) {
    ps[idx] = 0;
  }
}

/*
 * Calculate median phase similarity
 * @param stack 3D normalized interferogram stack
 * @param nifg Number of interferograms
 * @param nrow Number of rows of each interferogram
 * @param ncol Number of columns of each interferogram
 * @param rowstart_comp First row to compute phase similarity
 * @param rowend_comp Last row to comput phase similarity
 * @param rowstart First row of the loaded data
 * @param rowend Last row of the loaded data
 * @param ps Persistent Scatterer Mask
 * @param indices Relative indices of neighboring radar pixels
 * @param nindices Number of indices to explore
 * @param sim_median Output median phase similarity
 */
template <int NNEIGH>
__global__ void
median_similarity(const Complex *__restrict__ stack, const std::size_t nifg,
                  const std::size_t nrow, const std::size_t ncol,
                  const int rowstart_comp, const int rowend_comp,
                  const int rowstart, const int rowend,
                  const int32_t *__restrict__ ps, const unsigned int N,
                  const int2 *__restrict__ indices, const size_t nindices,
                  float *__restrict__ sim_median) {
  int r0 = blockIdx.y * blockDim.y + threadIdx.y + rowstart_comp;
  int c0 = blockIdx.x * blockDim.x + threadIdx.x;
  if (r0 >= rowend_comp || c0 >= (int)ncol)
    return;
  if (!ps[r0 * ncol + c0])
    return;

  float local_sims[NNEIGH];
  unsigned int counter = 0;
  int2 scan_idx;

  for (std::size_t i = 0; i < nindices; ++i) {
    scan_idx = indices[i];
    int r = r0 + scan_idx.x;
    int c = c0 + scan_idx.y;
    Complex c1, c2;
    if (r >= 0 && r < rowend && c >= 0 && c < (int)ncol && ps[r * ncol + c]) {
      float sim = 0.0f;
      for (size_t j = 0; j < nifg; ++j) {
        c1 = stack[(j * nrow + r0 - rowstart) * ncol + c0];
        c2 = stack[(j * nrow + r - rowstart) * ncol + c];
        sim += c1.x * c2.x + c1.y * c2.y; // cosine similarity
                                          // if (r0 == 382 && c0 == 460){
        //     printf("c1.x:%f,c1.y:%f,c2.x:%f,c2.y:%f\n",c1.x,c1.y,c2.x,c2.y);
        //     printf("sim:%f\n",sim);
        // }
      }
      sim /= nifg;
      local_sims[counter] = sim;
      counter++;
      if (counter >= N)
        break;
    }
  }

  // compute the median
  if (counter > 0) {
    for (unsigned int i = 1; i < counter; ++i) {
      float key = local_sims[i];
      int j = i - 1;
      while (j >= 0 && local_sims[j] > key) {
        local_sims[j + 1] = local_sims[j];
        --j;
      }
      local_sims[j + 1] = key;
    }
    sim_median[r0 * ncol + c0] = local_sims[counter / 2];
  } else {
    sim_median[r0 * ncol + c0] = 0.0;
  }
}

__global__ void
maximum_similarity(const Complex *__restrict__ stack, const std::size_t nifg,
                   const std::size_t nrow, const std::size_t ncol,
                   const int rowstart_comp, const int rowend_comp,
                   const int rowstart, const int rowend,
                   const int32_t *__restrict__ ps, const unsigned int N,
                   const int2 *__restrict__ indices, const size_t nindices,
                   float *__restrict__ sim_max) {
  int r0 = blockIdx.y * blockDim.y + threadIdx.y + rowstart_comp;
  int c0 = blockIdx.x * blockDim.x + threadIdx.x;
  if (r0 >= rowend_comp || c0 >= (int)ncol)
    return;
  if (ps[r0 * ncol + c0]) {
    sim_max[r0 * ncol + c0] = 1.0f;
    return;
  }

  float max_sim = -1.0f;
  unsigned int counter = 0;
  int2 scan_idx;

  for (std::size_t i = 0; i < nindices; ++i) {
    scan_idx = indices[i];
    int r = r0 + scan_idx.x;
    int c = c0 + scan_idx.y;
    Complex c1, c2;
    if (r >= 0 && r < rowend && c >= 0 && c < (int)ncol && ps[r * ncol + c]) {
      float sim = 0.0f;
      for (size_t j = 0; j < nifg; ++j) {
        c1 = stack[(j * nrow + r0 - rowstart) * ncol + c0];
        c2 = stack[(j * nrow + r - rowstart) * ncol + c];
        sim += c1.x * c2.x + c1.y * c2.y; // cosine similarity
      }
      sim /= nifg;
      if (max_sim < sim) {
        max_sim = sim;
      }
      counter++;
      if (counter >= N)
        break;
    }
  }

  if (counter > 0) {
    sim_max[r0 * ncol + c0] = max_sim;
  } else {
    sim_max[r0 * ncol + c0] = 0.0;
  }
}

struct IndexArray {
  int2 *data;
  size_t size;
};

/**
 * Generate an index array to specify the order of pixel exploration
 * @param rdmin Inner radius of the spiral
 * @param rdmax Outer radius of the spiral
 */
IndexArray scan_array(const unsigned int rdmin, const unsigned int rdmax) {
  // estimate the maximum number of points to allocate memory
  size_t max_points = 0;
  for (unsigned int r = rdmin + 1; r < rdmax; ++r) {
    max_points += 8 * r; // each radius contributes at most 8*r points
  }
  int2 *temp = (int2 *)malloc(max_points * sizeof(int2));
  size_t count = 0;

  // visited array: rdmax x rdmax, using char type (1 byte)
  char *visited = (char *)calloc(rdmax * rdmax, sizeof(char));
  visited[0 * rdmax + 0] = 1; // visited[0][0] = true

  for (int r = 1; r < (int)rdmax; ++r) {
    int x = r, y = 0;
    int p = 1 - r;
    if (r > (int)rdmin) {
      // four axis points
      temp[count++] = {r, 0};
      temp[count++] = {-r, 0};
      temp[count++] = {0, r};
      temp[count++] = {0, -r};
    }
    visited[r * rdmax + 0] = 1;
    visited[0 * rdmax + r] = 1;
    int flag = 0;
    while (x > y) {
      if (flag == 0) {
        y++;
        if (p <= 0) {
          p += 2 * y + 1;
        } else {
          x--;
          p += 2 * y - 2 * x + 1;
        }
      } else {
        flag--;
      }
      if (x < y)
        break;
      // move left until visited[x-1][y] is true
      while (x - 1 >= 0 && y < (int)rdmax && !visited[(x - 1) * rdmax + y]) {
        x--;
        flag++;
      }
      visited[x * rdmax + y] = 1;
      visited[y * rdmax + x] = 1;
      if (r > (int)rdmin) {
        temp[count++] = {x, y};
        temp[count++] = {-x, -y};
        temp[count++] = {x, -y};
        temp[count++] = {-x, y};
        if (x != y) {
          temp[count++] = {y, x};
          temp[count++] = {-y, -x};
          temp[count++] = {y, -x};
          temp[count++] = {-y, x};
        }
      }
      if (flag > 0) {
        x++;
      }
    }
  }

  free(visited);
  // reallocate to the actual size and return
  int2 *result = (int2 *)malloc(count * sizeof(int2));
  memcpy(result, temp, count * sizeof(int2));
  free(temp);
  return {result, count};
}

void free_index_array(IndexArray *arr) {
  if (arr->data)
    free(arr->data);
  arr->data = nullptr;
  arr->size = 0;
}

void similarity(const std::string &infile, const std::string &psfile,
                const std::string &med_sim_outfile,
                const std::string &max_sim_outfile, const int nneigh,
                const int rdmin, const int rdmax, const float med_sim_th) {
  // Declaration ============
  // Image stack
  Complex *stack, *d_stack;

  // ps
  int32_t *ps, *d_ps;

  // Phase similarity
  float *med_sim, *max_sim;
  float *d_med_sim, *d_max_sim;

  // Interferogram list
  std::string *ifg_list;

  // dimensions
  int nifg, nrow, ncol, rowstart, rowend, rowstart_comp, rowend_comp;

  // batch size
  int line_byte, nbatch, batch_lines; // byte of a line
  std::size_t total_size;

  // scan array
  int2 *d_indices;

  // cuda block dimensions
  int block_size = 256, num_blocks;
  dim3 block(16, 16), grid;
  // end of declaration =====

  // read interferogram list
  ifg_list = read_ifg_list(infile, nifg, nrow, ncol);
  // decide how to divide a large image into multiple batches
  line_byte = std::size_t(nifg) * ncol * sizeof(Complex);
  batch_lines = std::size_t(2e9 / line_byte);
  batch_lines = std::min(batch_lines, nrow);
  std::cout << "number of lines per batch " << batch_lines << std::endl;
  nbatch = (nrow + batch_lines - 1) / batch_lines;
  std::cout << "interferograms are divided into " << nbatch << " batch(es)"
            << std::endl;
  std::cout << "Each batch consists of " << batch_lines << "rows" << std::endl;
  total_size =
      (std::size_t)nifg * std::min(nrow, batch_lines + 2 * rdmax) * ncol;
  num_blocks = (total_size + block_size - 1) / block_size;
  grid.x = (ncol + block.x - 1) / block.x;
  grid.y = (batch_lines + block.y - 1) / block.y;

  // index array
  IndexArray indices = scan_array(rdmin, rdmax);

  // Memory allocation
  ps = new int32_t[nrow * ncol];
  stack = new Complex[total_size];
  med_sim = new float[nrow * ncol];
  max_sim = new float[nrow * ncol];
  CHECK_CUDA(cudaMalloc((void **)&d_ps, sizeof(int32_t) * nrow * ncol));
  CHECK_CUDA(cudaMalloc((void **)&d_stack, sizeof(Complex) * total_size));
  CHECK_CUDA(cudaMalloc((void **)&d_med_sim, sizeof(float) * nrow * ncol));
  CHECK_CUDA(cudaMalloc((void **)&d_max_sim, sizeof(float) * nrow * ncol));
  CHECK_CUDA(cudaMalloc((void **)&d_indices, sizeof(int2) * indices.size));

  // read ps candidates
  read_binary<int32_t>(psfile, nrow * ncol, ps);
  CHECK_CUDA(cudaMemcpy(d_ps, ps, sizeof(int32_t) * nrow * ncol,
                        cudaMemcpyHostToDevice));
  delete[] ps;

  // Copy indices to device
  CHECK_CUDA(cudaMemcpy(d_indices, indices.data, sizeof(int2) * indices.size,
                        cudaMemcpyHostToDevice));

  for (int i = 0; i < nbatch; i++) {
    // compute the starting and ending row of current batch
    rowstart_comp = batch_lines * i;
    rowend_comp = std::min(rowstart_comp + batch_lines, nrow);
    rowstart = std::max(0, rowstart_comp - rdmax);
    rowend = std::min(nrow, rowend_comp + rdmax + 1);
    // number of rows of current batch
    std::cout << "Similarity computation, first row: " << rowstart_comp
              << ", last row: " << rowend_comp << std::endl;
    std::cout << "Data loading, first row: " << rowstart
              << ", last row: " << rowend << std::endl;
    // read interferogram stack
    read_ifg_stack(stack, ifg_list, nifg, nrow, ncol, rowstart, rowend);
    // copy interferogram stack to device
    CHECK_CUDA(cudaMemcpy(d_stack, stack, sizeof(Complex) * total_size,
                          cudaMemcpyHostToDevice));
    // data normlization
    normalize<<<num_blocks, block_size>>>(
        d_stack, (std::size_t)nifg * (rowend - rowstart) * ncol);
    CHECK_CUDA(cudaDeviceSynchronize());
    // compute median similarity
    if (nneigh < 32) {
      median_similarity<32><<<grid, block>>>(
          d_stack, nifg, nrow, ncol, rowstart_comp, rowend_comp, rowstart,
          rowend, d_ps, nneigh, d_indices, indices.size, d_med_sim);
    } else if (nneigh < 64) {
      median_similarity<64><<<grid, block>>>(
          d_stack, nifg, nrow, ncol, rowstart_comp, rowend_comp, rowstart,
          rowend, d_ps, nneigh, d_indices, indices.size, d_med_sim);
    } else if (nifg < 128) {
      median_similarity<128><<<grid, block>>>(
          d_stack, nifg, nrow, ncol, rowstart_comp, rowend_comp, rowstart,
          rowend, d_ps, nneigh, d_indices, indices.size, d_med_sim);
    } else {
      std::cerr << "Does not support similarity computation with more "
                << "than 128 nearest neighbor pixels" << std::endl;
      exit(1);
    }
    CHECK_CUDA(cudaDeviceSynchronize());
  }
  // copy median phase similarity to host
  CHECK_CUDA(cudaMemcpy(med_sim, d_med_sim, sizeof(float) * nrow * ncol,
                        cudaMemcpyDeviceToHost));
  // save median phase similarity
  save_binary<float>(med_sim, nrow * ncol, med_sim_outfile);

  // update PS candidates based on median phase similarity
  num_blocks = (nrow * ncol + block_size - 1) / block_size;
  update_ps<<<num_blocks, block_size>>>(d_ps, d_med_sim, med_sim_th, true,
                                        nrow * ncol);
  CHECK_CUDA(cudaDeviceSynchronize());

  // Compute maximum phase similarity
  for (int i = nbatch - 1; i >= 0; i--) {
    // compute the starting and ending row of current batch
    rowstart_comp = batch_lines * i;
    rowend_comp = std::min(rowstart_comp + batch_lines, nrow);
    rowstart = std::max(0, rowstart_comp - rdmax);
    rowend = std::min(nrow, rowend_comp + rdmax + 1);
    // number of rows of current batch
    std::cout << "Similarity computation, first row: " << rowstart_comp
              << ", last row: " << rowend_comp << std::endl;
    std::cout << "Data loading, first row: " << rowstart
              << ", last row: " << rowend << std::endl;
    if (i < nbatch - 1) {
      // read interferogram stack
      read_ifg_stack(stack, ifg_list, nifg, nrow, ncol, rowstart, rowend);
      // copy interferogram stack to device
      CHECK_CUDA(cudaMemcpy(d_stack, stack, sizeof(Complex) * total_size,
                            cudaMemcpyHostToDevice));
      // data normlization
      normalize<<<num_blocks, block_size>>>(
          d_stack, (std::size_t)nifg * (rowend - rowstart) * ncol);
    }
    CHECK_CUDA(cudaDeviceSynchronize());
    // compute median similarity
    maximum_similarity<<<grid, block>>>(
        d_stack, nifg, nrow, ncol, rowstart_comp, rowend_comp, rowstart, rowend,
        d_ps, nneigh, d_indices, indices.size, d_max_sim);
    CHECK_CUDA(cudaDeviceSynchronize());
  }

  // copy median phase similarity to host
  CHECK_CUDA(cudaMemcpy(max_sim, d_max_sim, sizeof(float) * nrow * ncol,
                        cudaMemcpyDeviceToHost));
  // save median phase similarity
  save_binary<float>(max_sim, nrow * ncol, max_sim_outfile);

  // Deallocation
  delete[] ifg_list;
  delete[] med_sim;
  delete[] max_sim;
  free_index_array(&indices);
  cudaFree(d_ps);
  cudaFree(d_stack);
  cudaFree(d_med_sim);
  cudaFree(d_max_sim);
  cudaFree(d_indices);
  return;
}

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));
  if (argc < 9) {
    std::cout << "Usage: phase_similarity infile psfile med_sim_outfile "
              << "max_sim_outfile N rdmin rdmax med_sim_th"
              << " [--gpu DEVICE_ID]" << std::endl;
    return 0;
  }
  const std::string infile = std::string(argv[1]);
  const std::string psfile = std::string(argv[2]);
  const std::string med_sim_outfile = std::string(argv[3]);
  const std::string max_sim_outfile = std::string(argv[4]);
  const int nneigh = std::stoi(argv[5]);
  const int rdmin = std::stoi(argv[6]);
  const int rdmax = std::stoi(argv[7]);
  const float med_sim_th = std::stod(argv[8]);
  similarity(infile, psfile, med_sim_outfile, max_sim_outfile, nneigh, rdmin,
             rdmax, med_sim_th);
  return 0;
}
