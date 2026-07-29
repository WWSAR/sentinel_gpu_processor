#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>

#include "gpu_device.hpp"
#include "sario.hpp"
#include <cuda_runtime.h>

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
 * GPU kernel for PS-based phase interpolation.
 *
 * For each pixel, searches for up to N nearest PS neighbors within a spiral
 * search region defined by the precomputed indices array. Computes a weighted
 * average of normalized phases using a Gaussian weighting scheme, and scales
 * the result by the original pixel amplitude.
 *
 * @tparam NMAX Maximum number of PS neighbors (compile-time constant for
 *              stack allocation of local arrays)
 * @param ifg Input interferogram (complex float2 array, nrow x ncol)
 * @param ps Persistent scatterer mask (int32 array, 1=PS, 0=non-PS,
 *           nrow x ncol)
 * @param indices Precomputed spiral search offsets (int2 array)
 * @param nindices Number of search offsets
 * @param N Number of nearest PS neighbors to use
 * @param alpha Exponent controlling distance weighting decay
 * @param nrow Number of rows in the interferogram
 * @param ncol Number of columns in the interferogram
 * @param ifg_interp Output interpolated interferogram
 */
template <int NMAX>
__global__ void
phase_interp_kernel(const Complex *__restrict__ ifg,
                    const int32_t *__restrict__ ps,
                    const int2 *__restrict__ indices, const size_t nindices,
                    const unsigned int N, const float alpha, const int nrow,
                    const int ncol, Complex *__restrict__ ifg_interp) {

  int r0 = blockIdx.y * blockDim.y + threadIdx.y;
  int c0 = blockIdx.x * blockDim.x + threadIdx.x;
  if (r0 >= nrow || c0 >= ncol)
    return;

  float local_r2[NMAX];
  Complex local_cphase[NMAX];
  unsigned int counter = 0;

  // Search for up to N nearest PS neighbors in spiral order
  for (size_t i = 0; i < nindices; ++i) {
    int2 idx = indices[i];
    int r = r0 + idx.x;
    int c = c0 + idx.y;
    if (r >= 0 && r < nrow && c >= 0 && c < ncol && ps[r * ncol + c]) {
      float dx = (float)idx.x;
      float dy = (float)idx.y;
      local_r2[counter] = dx * dx + dy * dy;
      Complex val = ifg[r * ncol + c];
      float mag = sqrtf(val.x * val.x + val.y * val.y);
      if (mag > 1e-12f) {
        local_cphase[counter].x = val.x / mag;
        local_cphase[counter].y = val.y / mag;
      } else {
        local_cphase[counter].x = 0.0f;
        local_cphase[counter].y = 0.0f;
      }
      ++counter;
      if (counter >= N)
        break;
    }
  }

  // Compute weighted sum of normalized phases
  Complex csum = {0.0f, 0.0f};
  if (counter > 0) {
    float r2_last = local_r2[counter - 1];
    float denom = 2.0f * powf(r2_last, alpha);
    for (unsigned int i = 0; i < counter; ++i) {
      float weight = expf(-local_r2[i] / denom);
      csum.x += weight * local_cphase[i].x;
      csum.y += weight * local_cphase[i].y;
    }
  }

  // Scale by original pixel amplitude, preserving phase of weighted sum
  Complex center = ifg[r0 * ncol + c0];
  float amp = sqrtf(center.x * center.x + center.y * center.y);
  float csum_mag = sqrtf(csum.x * csum.x + csum.y * csum.y) + 1e-12f;
  ifg_interp[r0 * ncol + c0].x = amp * csum.x / csum_mag;
  ifg_interp[r0 * ncol + c0].y = amp * csum.y / csum_mag;
}

/**
 * PS-based phase interpolation for a single interferogram.
 *
 * Reads an interferogram and a PS mask from binary files, performs
 * GPU-accelerated interpolation using neighboring PS pixels within a
 * spiral search region, and saves the interpolated interferogram.
 *
 * @param ifgfile Path to the input interferogram (binary float2 file)
 * @param psfile Path to the PS mask (binary int32 file, 1=PS, 0=non-PS)
 * @param outputfile Path to the output interpolated interferogram
 * @param nrow Number of rows in the interferogram
 * @param ncol Number of columns in the interferogram
 * @param N Maximum number of nearest PS neighbors to use
 * @param rdmax Maximum search radius in pixels
 * @param alpha Exponent controlling distance weighting decay
 */
void phase_interp(const std::string &ifgfile, const std::string &psfile,
                  const std::string &outputfile, const int nrow, const int ncol,
                  const unsigned int N, const unsigned int rdmax,
                  const float alpha) {
  // Host arrays
  Complex *ifg, *d_ifg, *d_ifg_interp;
  int32_t *ps, *d_ps;
  int2 *d_indices;

  // CUDA block dimensions
  dim3 block(16, 16), grid;
  grid.x = (ncol + block.x - 1) / block.x;
  grid.y = (nrow + block.y - 1) / block.y;

  // Generate spiral search indices (rdmin=0 to include all pixels within rdmax)
  IndexArray indices = scan_array(0, rdmax);
  // std::cout << "Generated " << indices.size << " spiral search indices"
  //          << std::endl;

  // Allocate host memory
  size_t npixels = (size_t)nrow * ncol;
  ifg = new Complex[npixels];
  ps = new int32_t[npixels];

  // Read input data
  read_binary<Complex>(ifgfile, npixels, ifg);
  read_binary<int32_t>(psfile, npixels, ps);

  // Allocate device memory
  CHECK_CUDA(cudaMalloc((void **)&d_ifg, sizeof(Complex) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_ps, sizeof(int32_t) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_ifg_interp, sizeof(Complex) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_indices, sizeof(int2) * indices.size));

  // Copy data to device
  CHECK_CUDA(cudaMemcpy(d_ifg, ifg, sizeof(Complex) * npixels,
                        cudaMemcpyHostToDevice));
  CHECK_CUDA(
      cudaMemcpy(d_ps, ps, sizeof(int32_t) * npixels, cudaMemcpyHostToDevice));
  CHECK_CUDA(cudaMemcpy(d_indices, indices.data, sizeof(int2) * indices.size,
                        cudaMemcpyHostToDevice));

  // Launch interpolation kernel with appropriate template instantiation
  if (N <= 32) {
    phase_interp_kernel<32><<<grid, block>>>(d_ifg, d_ps, d_indices,
                                             indices.size, N, alpha, nrow, ncol,
                                             d_ifg_interp);
  } else if (N <= 64) {
    phase_interp_kernel<64><<<grid, block>>>(d_ifg, d_ps, d_indices,
                                             indices.size, N, alpha, nrow, ncol,
                                             d_ifg_interp);
  } else if (N <= 128) {
    phase_interp_kernel<128><<<grid, block>>>(d_ifg, d_ps, d_indices,
                                              indices.size, N, alpha, nrow,
                                              ncol, d_ifg_interp);
  } else if (N <= 256) {
    phase_interp_kernel<256><<<grid, block>>>(d_ifg, d_ps, d_indices,
                                              indices.size, N, alpha, nrow,
                                              ncol, d_ifg_interp);
  } else {
    std::cerr << "Does not support interpolation with more than 256 nearest "
              << "neighbor PS pixels" << std::endl;
    exit(1);
  }
  CHECK_CUDA(cudaDeviceSynchronize());

  // Copy result back to host
  CHECK_CUDA(cudaMemcpy(ifg, d_ifg_interp, sizeof(Complex) * npixels,
                        cudaMemcpyDeviceToHost));

  // Save output
  save_binary<Complex>(ifg, npixels, outputfile);

  // Cleanup
  delete[] ifg;
  delete[] ps;
  free_index_array(&indices);
  cudaFree(d_ifg);
  cudaFree(d_ps);
  cudaFree(d_ifg_interp);
  cudaFree(d_indices);
}

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));
  if (argc < 9) {
    std::cout << "Usage: phase_interp ifgfile psfile outputfile "
              << "nrow ncol N rdmax alpha [--gpu DEVICE_ID]" << std::endl;
    return 0;
  }
  const std::string ifgfile = std::string(argv[1]);
  const std::string psfile = std::string(argv[2]);
  const std::string outfile = std::string(argv[3]);
  const int nrow = std::stoi(argv[4]);
  const int ncol = std::stoi(argv[5]);
  const unsigned int N = std::stoi(argv[6]);
  const unsigned int rdmax = std::stoi(argv[7]);
  const float alpha = std::stof(argv[8]);

  phase_interp(ifgfile, psfile, outfile, nrow, ncol, N, rdmax, alpha);
  return 0;
}
