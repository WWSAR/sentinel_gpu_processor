#include "gpu_device.hpp"
#include "sario.hpp"
#include <cuda_runtime.h>
#include <cufft.h>
#include <device_launch_parameters.h>
#include <fstream>
#include <iostream>
#include <math.h>
#include <string>
#include <vector>

#define CHECK_CUDA(x)                                                          \
  do {                                                                         \
    cudaError_t err = (x);                                                     \
    if (err != cudaSuccess) {                                                  \
      std::cerr << "CUDA error " << cudaGetErrorString(err) << " at "          \
                << __FILE__ << ":" << __LINE__ << std::endl;                   \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

#define CHECK_CUFFT(x)                                                         \
  do {                                                                         \
    cufftResult err = (x);                                                     \
    if (err != CUFFT_SUCCESS) {                                                \
      std::cerr << "CUFFT error " << err << " at " << __FILE__ << ":"          \
                << __LINE__ << std::endl;                                      \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

#define CUDA_CHECK_LAST_ERROR()                                                \
  do {                                                                         \
    cudaError_t err = cudaGetLastError();                                      \
    if (err != cudaSuccess) {                                                  \
      std::cerr << "CUDA kernel launch failed: " << cudaGetErrorString(err)    \
                << " at " << __FILE__ << ":" << __LINE__ << std::endl;         \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

// 7x7 Gaussian kernel with sigma=1.0, normalized to sum to 1
__constant__ float d_GaussianKernel[49] = {
    0.000004f, 0.000088f, 0.000543f, 0.000948f, 0.000543f, 0.000088f,
    0.000004f, 0.000088f, 0.001915f, 0.011776f, 0.020584f, 0.011776f,
    0.001915f, 0.000088f, 0.000543f, 0.011776f, 0.072410f, 0.126584f,
    0.072410f, 0.011776f, 0.000543f, 0.000948f, 0.020584f, 0.126584f,
    0.221141f, 0.126584f, 0.020584f, 0.000948f, 0.000543f, 0.011776f,
    0.072410f, 0.126584f, 0.072410f, 0.011776f, 0.000543f, 0.000088f,
    0.001915f, 0.011776f, 0.020584f, 0.011776f, 0.001915f, 0.000088f,
    0.000004f, 0.000088f, 0.000543f, 0.000948f, 0.000543f, 0.000088f,
    0.000004f};

/**
 * parse the input string
 * if it end with '.int', return a vector containing this string
 * otherwise, read the file named with the input string, read its lines into
 * a vector
 */
std::vector<std::string> process_input(const std::string &input, bool &is_ifg) {
  is_ifg = false;
  const std::string suffix = ".int";
  if (input.size() >= suffix.size() &&
      input.compare(input.size() - suffix.size(), suffix.size(), suffix) == 0) {
    is_ifg = true;
    return std::vector<std::string>{input};
  }

  std::ifstream file(input);
  if (!file.is_open()) {
    return std::vector<std::string>();
  }

  std::vector<std::string> lines;
  std::string line;
  while (std::getline(file, line)) {
    lines.push_back(line);
  }

  return lines;
}

/**
 * Core kernel: compute magnitude + perform 2D convolution smoothing directly
 * in frequency domain (with built-in fftshift logic)
 */
__global__ void gaussian_filter_kernel(cufftComplex *__restrict__ d_fft_data,
                                       float *__restrict__ d_filtered_mag,
                                       int n_win, int total_patches) {
  // Each block processes one patch, threads within block handle the patch
  // elements
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  int patch_idx = idx / (n_win * n_win);
  if (patch_idx >= total_patches)
    return;

  int pixel_idx = idx - patch_idx * n_win * n_win;
  int y = pixel_idx / n_win;
  int x = pixel_idx - y * n_win;

  std::size_t offset = (std::size_t)patch_idx * n_win * n_win + y * n_win + x;

  float smoothed_intensity = 0.0f;

  for (int ky = -3; ky <= 3; ++ky) {
    for (int kx = -3; kx <= 3; ++kx) {
      // fftshift mapping: center (0,0) at (n_win/2, n_win/2)
      int src_x = (x + kx + n_win) % n_win;
      int src_y = (y + ky + n_win) % n_win;
      std::size_t src_offset =
          (std::size_t)patch_idx * n_win * n_win + src_y * n_win + src_x;

      cufftComplex val = d_fft_data[src_offset];
      float mag = cuCabsf(val);

      int kernel_idx = (ky + 3) * 7 + (kx + 3);
      smoothed_intensity += mag * d_GaussianKernel[kernel_idx];
    }
  }

  // 2. save the smoothed_intensity
  d_filtered_mag[offset] = smoothed_intensity;
}

__global__ void spectrum_enhancement(cufftComplex *__restrict__ d_fft_data,
                                     float *__restrict__ d_filtered_mag,
                                     const float alpha,
                                     const std::size_t patch_elements) {
  std::size_t idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= patch_elements)
    return;

  float w = powf(d_filtered_mag[idx], alpha);
  cufftComplex v = d_fft_data[idx];
  if (w > 0.0f) {
    v.x *= w;
    v.y *= w;
    d_fft_data[idx] = v;
  }
}

__global__ void patchwise_normalize(float *__restrict__ data, int n_win,
                                    int total_patches) {
  int patch_size = n_win * n_win;
  int patch_idx = blockIdx.x; // each block handles one patch
  int tid = threadIdx.x;
  if (patch_idx >= total_patches)
    return;
  int base = patch_idx * patch_size; // global index of current patch
  // shared memory, size  = patch_size * sizeof(float)
  extern __shared__ float s_patch[];
  // load elements associated with current thread to the shared memory
  float val = data[base + tid];
  s_patch[tid] = val;
  __syncthreads();

  // reduce sum
  for (int stride = patch_size / 2; stride > 0; stride >>= 1) {
    if (tid < stride) {
      s_patch[tid] += s_patch[tid + stride];
    }
    __syncthreads();
  }

  // patch sum is now stored in s_patch[0]
  float sum = s_patch[0];
  float mean = sum / patch_size;

  // divide each element by the mean value
  if (mean > 1e-8f) {
    float inv_mean = 1.0f / mean;
    data[base + tid] = val * inv_mean;
  } else {
    data[base + tid] = 0.0f;
  }
}

__global__ void patchwise_normalize_large(float *__restrict__ data, int n_win,
                                          int total_patches) {
  int patch_size = n_win * n_win;
  int patch_idx = blockIdx.x;
  int tid = threadIdx.x;
  int block_threads = blockDim.x;
  if (patch_idx >= total_patches)
    return;
  int base = patch_idx * patch_size;

  // each thread takes charge of the summation of it its own part
  float local_sum = 0.0f;
  for (int i = tid; i < patch_size; i += block_threads) {
    local_sum += data[base + i];
  }

  extern __shared__ float s_sum[];
  s_sum[tid] = local_sum;
  __syncthreads();

  for (int stride = block_threads / 2; stride > 0; stride >>= 1) {
    if (tid < stride) {
      s_sum[tid] += s_sum[tid + stride];
    }
    __syncthreads();
  }

  float block_sum = s_sum[0];
  float mean = block_sum / patch_size;

  // broadcast the mean to all threads
  if (tid == 0) {
    s_sum[0] = mean;
  }
  __syncthreads();
  float mean_val = s_sum[0];

  if (mean_val > 1e-8f) {
    float inv_mean = 1.0f / mean_val;
    for (int i = tid; i < patch_size; i += block_threads) {
      data[base + i] *= inv_mean;
    }
  } else {
    for (int i = tid; i < patch_size; i += block_threads) {
      data[base + i] = 0.0f;
    }
  }
}

__global__ void
batched_overlap_add_kernel(const cufftComplex *__restrict__ d_filtered_patches,
                           cufftComplex *__restrict__ d_out_ph,
                           float *d_out_weight, int n_win, int n_inc,
                           int n_win_i, int n_win_j, int nrow, int ncol) {
  int patch_x = blockIdx.x;
  int patch_y = blockIdx.y;

  // Each thread processes one pixel in the patch

  if (threadIdx.x >= n_win || threadIdx.y >= n_win)
    return;

  int patch_idx = patch_y * n_win_j + patch_x;

  // Calculate the top-left corner of the current patch in the original image
  // (with boundary checks)
  int i1 = patch_y * n_inc;
  int j1 = patch_x * n_inc;

  if (i1 + n_win > nrow)
    i1 = nrow - n_win;
  if (j1 + n_win > ncol)
    j1 = ncol - n_win;

  for (int tx = threadIdx.x; tx < n_win; tx += 16) {
    for (int ty = threadIdx.y; ty < n_win; ty += 16) {
      int target_y = i1 + ty;
      int target_x = j1 + tx;

      float wx = (tx < n_win / 2) ? tx : (n_win - 1 - tx);
      float wy = (ty < n_win / 2) ? ty : (n_win - 1 - ty);
      float weight = wx + wy;

      int src_offset = patch_idx * n_win * n_win + ty * n_win + tx;
      cufftComplex val = d_filtered_patches[src_offset];

      // Add the weight to the corresponding position in the output image
      int dest_offset = target_y * ncol + target_x;

      float norm = 1.0f / (n_win * n_win);

      atomicAdd(&d_out_ph[dest_offset].x, val.x * weight * norm);
      atomicAdd(&d_out_ph[dest_offset].y, val.y * weight * norm);
      atomicAdd(&d_out_weight[dest_offset], weight);
    }
  }
}

/**
 * Normalize the output by dividing by the accumulated weights to get the final
 * pixel values
 */
__global__ void normalize_output_kernel(cufftComplex *__restrict__ d_out_ph,
                                        const float *d_out_weight,
                                        const int total_pixels) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= total_pixels)
    return;

  float w = d_out_weight[idx];
  if (w > 0.0f) {
    d_out_ph[idx].x /= w;
    d_out_ph[idx].y /= w;
  }
}

/**
 * Kernel to extract overlapping patches from the input image into a batch
 * buffer for filtering.
 */
__global__ void extract_patches_kernel(const cufftComplex *__restrict__ d_in,
                                       cufftComplex *__restrict__ d_patches,
                                       int n_win, int n_inc, int n_win_j,
                                       int nrow, int ncol) {
  int patch_x = blockIdx.x;
  int patch_y = blockIdx.y;

  if (threadIdx.x >= n_win || threadIdx.y >= n_win)
    return;

  int i1 = patch_y * n_inc;
  int j1 = patch_x * n_inc;
  if (i1 + n_win > nrow)
    i1 = nrow - n_win;
  if (j1 + n_win > ncol)
    j1 = ncol - n_win;

  int patch_idx = patch_y * n_win_j + patch_x;
  for (int tx = threadIdx.x; tx < n_win; tx += 16) {
    for (int ty = threadIdx.y; ty < n_win; ty += 16) {
      int src_idx = (i1 + ty) * ncol + (j1 + tx);
      int dst_idx = patch_idx * n_win * n_win + ty * n_win + tx;
      d_patches[dst_idx] = d_in[src_idx];
    }
  }
}

/**
 * Host function to perform Goldstein filtering on the input image.
 */
void goldstein_filter_cuda_host(cufftComplex *d_in, cufftComplex *d_out,
                                cufftComplex *out, cufftComplex *d_patch_buffer,
                                float *d_spec_magnitude, float *d_weight_buffer,
                                cufftHandle &plan, const int nrow,
                                const int ncol, const int n_win,
                                const float alpha) {
  int n_inc = n_win / 4;
  int n_win_i = (int)ceil((float)nrow / n_inc) - 3;
  int n_win_j = (int)ceil((float)ncol / n_inc) - 3;
  if (n_win_i <= 0)
    n_win_i = 1;
  if (n_win_j <= 0)
    n_win_j = 1;
  int total_patches = n_win_i * n_win_j;
  std::size_t patch_elements = std::size_t(total_patches) * n_win * n_win;
  std::size_t img_bytes = nrow * ncol * sizeof(cufftComplex);

  // 1. extract overlapping patches into batch buffer for filtering
  dim3 block_extract(16, 16);
  dim3 grid_extract(n_win_j, n_win_i);
  extract_patches_kernel<<<grid_extract, block_extract>>>(
      d_in, d_patch_buffer, n_win, n_inc, n_win_j, nrow, ncol);
  CUDA_CHECK_LAST_ERROR();

  // 2. Apply Batched FFT to all patches
  CHECK_CUFFT(
      cufftExecC2C(plan, d_patch_buffer, d_patch_buffer, CUFFT_FORWARD));

  // 3. Apply the smoothing kernel in the frequency domain
  int block_filter = 256;
  int grid_filter = (patch_elements + std::size_t(block_filter) - 1) /
                    std::size_t(block_filter);
  gaussian_filter_kernel<<<grid_filter, block_filter>>>(
      d_patch_buffer, d_spec_magnitude, n_win, total_patches);
  CUDA_CHECK_LAST_ERROR();
  CHECK_CUDA(cudaDeviceSynchronize());

  // 4. Compute the patchwise mean of spectrum magnitude
  int patch_size = n_win * n_win;
  int threads_per_block = patch_size;
  if (threads_per_block > 1024) {
    int threads = 256;
    std::size_t shared_bytes = threads * sizeof(float);
    patchwise_normalize_large<<<total_patches, threads, shared_bytes>>>(
        d_spec_magnitude, n_win, total_patches);
  } else {
    std::size_t shared_mem_bytes = patch_size * sizeof(float);
    patchwise_normalize<<<total_patches, threads_per_block, shared_mem_bytes>>>(
        d_spec_magnitude, n_win, total_patches);
  }
  CUDA_CHECK_LAST_ERROR();
  CHECK_CUDA(cudaDeviceSynchronize());

  // 5. Multiply the spectrum by the filtering factor
  spectrum_enhancement<<<grid_filter, block_filter>>>(
      d_patch_buffer, d_spec_magnitude, alpha, patch_elements);
  CUDA_CHECK_LAST_ERROR();
  CHECK_CUDA(cudaDeviceSynchronize());

  // 6. Apply Inverse FFT to get the filtered patches back in spatial domain
  CHECK_CUFFT(
      cufftExecC2C(plan, d_patch_buffer, d_patch_buffer, CUFFT_INVERSE));

  // 7. Batched overlap-add to reconstruct the output image from the
  // filtered patches
  batched_overlap_add_kernel<<<grid_extract, block_extract>>>(
      d_patch_buffer, d_out, d_weight_buffer, n_win, n_inc, n_win_i, n_win_j,
      nrow, ncol);
  CUDA_CHECK_LAST_ERROR();
  CHECK_CUDA(cudaDeviceSynchronize());

  // 8. Normalize the output by dividing by the accumulated weights
  int total_pixels = nrow * ncol;
  int block_norm = 256;
  int grid_norm = (total_pixels + 255) / 256;
  normalize_output_kernel<<<grid_norm, block_norm>>>(d_out, d_weight_buffer,
                                                     total_pixels);
  CUDA_CHECK_LAST_ERROR();
  CHECK_CUDA(cudaDeviceSynchronize());
  CHECK_CUDA(cudaMemcpy(out, d_out, img_bytes, cudaMemcpyDeviceToHost));
}

void goldstein_filter(const std::string &in_str, const std::string &out_str,
                      const int nrow, const int ncol, const int n_win,
                      const float alpha) {
  // declaration
  bool is_ifg;
  std::string in_ifg_file, out_ifg_file;
  int img_size = nrow * ncol;
  int n_inc = n_win / 4;
  int n_win_i = (int)ceil((float)nrow / n_inc) - 3;
  int n_win_j = (int)ceil((float)ncol / n_inc) - 3;
  if (n_win_i <= 0)
    n_win_i = 1;
  if (n_win_j <= 0)
    n_win_j = 1;
  int total_patches = n_win_i * n_win_j;
  std::size_t patch_elements = std::size_t(total_patches) * n_win * n_win;
  std::size_t patch_bytes = patch_elements * sizeof(cufftComplex);
  std::size_t img_bytes = nrow * ncol * sizeof(cufftComplex);

  // arrays
  cufftComplex *in_ifg, *out_ifg, *d_in_ifg, *d_out_ifg;
  cufftComplex *d_patch_buffer;
  float *d_spec_magnitude, *d_weight_buffer;
  // end of declaration

  std::vector<std::string> input_files = process_input(in_str, is_ifg);
  if (input_files.size() == 0) {
    std::cout << "Input list is empty, nothing to filter." << std::endl;
    return;
  }

  // Alocate host memory
  in_ifg = new float2[img_size];
  out_ifg = new float2[img_size];

  // Allocate device memory
  CHECK_CUDA(cudaMalloc((void **)&d_in_ifg, img_bytes));
  CHECK_CUDA(cudaMalloc((void **)&d_out_ifg, img_bytes));
  CHECK_CUDA(cudaMalloc((void **)&d_patch_buffer, patch_bytes));
  CHECK_CUDA(
      cudaMalloc((void **)&d_spec_magnitude, patch_elements * sizeof(float)));
  CHECK_CUDA(
      cudaMalloc((void **)&d_weight_buffer, nrow * ncol * sizeof(float)));

  // Configure cuFFT plan for Batched 2D FFT
  cufftHandle plan;
  int rank = 2;              // 2D FFT
  int n[2] = {n_win, n_win}; // Dimensions of each patch
  int idist = n_win * n_win; // Interval between batches in the input buffer
  int odist = n_win * n_win; // Interval between batches in the output buffer
  int inembed[2] = {n_win, n_win}; // Physical dimensions of the input data
  int onembed[2] = {n_win, n_win}; // Physical dimensions of the output data
  int istride = 1;                 // Stride within each matrix
  int ostride = 1;

  CHECK_CUFFT(cufftPlanMany(&plan, rank, n, inembed, istride, idist, onembed,
                            ostride, odist, CUFFT_C2C, total_patches));

  // read image
  for (int i = 0; i < input_files.size(); i++) {
    in_ifg_file = input_files[i];
    if (is_ifg) {
      if (out_str.empty()) {
        out_ifg_file = std::string("filtered.int");
      }
    } else {
      if (out_str.empty()) {
        out_ifg_file = in_ifg_file + std::string(".filt");
      } else {
        out_ifg_file = in_ifg_file + out_str;
      }
    }
    std::cout << "Run Goldstein filter, input: " << in_ifg_file
              << ", output: " << out_ifg_file << std::endl;
    CHECK_CUDA(cudaMemset(d_weight_buffer, 0, img_size * sizeof(float)));
    read_binary<float2>(in_ifg_file, std::size_t(img_size), in_ifg);
    CHECK_CUDA(cudaMemcpy(d_in_ifg, in_ifg, img_bytes, cudaMemcpyHostToDevice));

    goldstein_filter_cuda_host(d_in_ifg, d_out_ifg, out_ifg, d_patch_buffer,
                               d_spec_magnitude, d_weight_buffer, plan, nrow,
                               ncol, n_win, alpha);

    save_binary<float2>(out_ifg, std::size_t(img_size), out_ifg_file);
  }
  delete[] in_ifg;
  delete[] out_ifg;
  CHECK_CUDA(cudaFree(d_in_ifg));
  CHECK_CUDA(cudaFree(d_out_ifg));
  CHECK_CUDA(cudaFree(d_patch_buffer));
  CHECK_CUDA(cudaFree(d_weight_buffer));
  CHECK_CUDA(cudaFree(d_spec_magnitude));
  CHECK_CUFFT(cufftDestroy(plan));
}

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));
  if (argc < 6) {
    std::cout << "Usage: goldstein in_str nrow ncol n_win alpha [out_str]"
              << " [--gpu DEVICE_ID]" << std::endl;
    return 0;
  }
  const std::string in_str = std::string(argv[1]);
  const int nrow = std::stoi(argv[2]);
  const int ncol = std::stoi(argv[3]);
  const int n_win = std::stoi(argv[4]);
  const float alpha = std::stod(argv[5]);
  std::string out_str;
  if (argc > 6) {
    out_str = std::string(argv[6]);
  } else {
    out_str = "";
  }
  goldstein_filter(in_str, out_str, nrow, ncol, n_win, alpha);
  return 0;
}
