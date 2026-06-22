#include "gpu_device.hpp"
#include "sario.hpp"
#include <array>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#ifndef SOL
#define SOL 299792458.0
#endif

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

class ScopedTimer {
public:
  explicit ScopedTimer(const std::string &name)
      : name_(name), start_(std::chrono::steady_clock::now()) {}

  ~ScopedTimer() {
    auto end = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration<double>(end - start_).count();
    std::cout << "[TIMER] " << name_ << " Elapsed Time: " << duration << " s"
              << std::endl;
  }

  // 禁止拷贝，防止误用
  ScopedTimer(const ScopedTimer &) = delete;
  ScopedTimer &operator=(const ScopedTimer &) = delete;

private:
  std::string name_;
  std::chrono::steady_clock::time_point start_;
};

/**
 * Read the input file into a vector of arrays, each array contains three
 * elements: filename of the reference burst, filename of the secondary
 * burst, and filename of the output interferogram
 */
std::vector<std::array<std::string, 3>>
parse_input_file(const std::string &filename) {
  // 存储结果的容器：每一行是一个 array<string,3>
  std::vector<std::array<std::string, 3>> data;
  std::ifstream infile(filename);
  if (!infile.is_open()) {
    std::cerr << "Cannot open file: " << filename << std::endl;
    return data;
  }

  std::string line;
  while (std::getline(infile, line)) {
    if (line.empty())
      continue;
    std::istringstream iss(line);
    std::array<std::string, 3> row;
    if (iss >> row[0] >> row[1] >> row[2]) {
      data.push_back(row);
    } else {
      std::cerr << "Warning: skipping a row with incorrect format" << line
                << std::endl;
    }
  }

  infile.close();
  return data;
}

int crossmul(const std::string &input_file, const int rowlook,
             const int collook, const int out_float) {
  // delcaration
  std::int32_t header1[NHEADER], header2[NHEADER], ifg_header[NHEADER];
  Complex *slc1, *slc2, *ifglook;
  Complex *d_slc1, *d_slc2, *d_ifg, *d_ifg_collook, *d_ifglook;
  float *phase, *d_phase;
  int nrow_sm, ncol_sm;
  int blockSize = 256, numBlocks;
  // image parameters
  // image1
  int left1, top1, right1, bottom1;
  // image2
  int left2, top2, right2, bottom2;
  // raw interferogram
  int left, top, right, bottom, nrow, ncol;
  int max_nrow = 0, max_ncol = 0;
  std::size_t max_elements;
  // zero
  // Complex zero;
  // zero.x = 0;
  // zero.y = 0;
  std::vector<std::array<std::string, 3>> slc_pairs =
      parse_input_file(input_file);
  // end of declaration

  // decide the maximum row and column for memory allocation
  for (const auto &slc_pair : slc_pairs) {
    // read the header of the first image
    read_binary<std::int32_t>(slc_pair[0], NHEADER, header1);
    // read the header of the second image
    read_binary<std::int32_t>(slc_pair[1], NHEADER, header2);
    // read the parameters of the first image
    left1 = header1[2];
    top1 = header1[3];
    right1 = header1[4];
    bottom1 = header1[5];
    max_nrow = std::max(bottom1 - top1, max_nrow);
    max_ncol = std::max(right1 - left1, max_ncol);
    // read the parameters of the second image
    left2 = header2[2];
    top2 = header2[3];
    right2 = header2[4];
    bottom2 = header2[5];

    left = left1 < left2 ? left1 : left2;
    // left = (left + collook - 1) / collook * collook;
    right = right1 > right2 ? right1 : right2;
    // right = right / collook * collook;
    top = top1 < top2 ? top1 : top2;
    // top = (top + rowlook - 1) / rowlook * rowlook;
    bottom = bottom1 > bottom2 ? bottom1 : bottom2;
    // bottom = bottom / rowlook * rowlook;

    max_nrow = std::max(bottom - top, max_nrow);
    max_ncol = std::max(right - left, max_ncol);
  }

  std::cout << "Maximum number of rows: " << max_nrow << std::endl;
  std::cout << "Maximum number of columns: " << max_ncol << std::endl;
  nrow_sm = max_nrow / rowlook;
  ncol_sm = max_ncol / collook;
  max_elements = std::size_t(max_nrow) * max_ncol;

  slc1 = (Complex *)malloc(sizeof(Complex) * max_elements);
  slc2 = (Complex *)malloc(sizeof(Complex) * max_elements);
  if (out_float) {
    phase = (float *)malloc(sizeof(float) * nrow_sm * ncol_sm);
    CHECK_CUDA(
        cudaMalloc((void **)&d_phase, sizeof(float) * nrow_sm * ncol_sm));
  } else {
    ifglook = (Complex *)malloc(sizeof(Complex) * nrow_sm * ncol_sm);
  }
  CHECK_CUDA(cudaMalloc((void **)&d_slc1, sizeof(Complex) * max_elements));
  CHECK_CUDA(cudaMalloc((void **)&d_slc2, sizeof(Complex) * max_elements));
  CHECK_CUDA(cudaMalloc((void **)&d_ifg, sizeof(Complex) * max_elements));
  if (collook > 1) {
    CHECK_CUDA(cudaMalloc((void **)&d_ifg_collook,
                          sizeof(Complex) * max_nrow * ncol_sm));
  }
  if (rowlook > 1) {
    CHECK_CUDA(
        cudaMalloc((void **)&d_ifglook, sizeof(Complex) * nrow_sm * ncol_sm));
  }

  for (const auto &slc_pair : slc_pairs) {
    // read the header of the first image
    read_binary<std::int32_t>(slc_pair[0], NHEADER, header1);
    // read the header of the second image
    read_binary<std::int32_t>(slc_pair[1], NHEADER, header2);
    // read the parameters of the first image
    left1 = header1[2];
    top1 = header1[3];
    right1 = header1[4];
    bottom1 = header1[5];
    // read the parameters of the second image
    left2 = header2[2];
    top2 = header2[3];
    right2 = header2[4];
    bottom2 = header2[5];
    // determine the size of the interferogram
    left = left1 < left2 ? left1 : left2;
    left = (left + collook - 1) / collook * collook;
    right = right1 > right2 ? right1 : right2;
    right = right / collook * collook;
    top = top1 < top2 ? top1 : top2;
    top = (top + rowlook - 1) / rowlook * rowlook;
    bottom = bottom1 > bottom2 ? bottom1 : bottom2;
    bottom = bottom / rowlook * rowlook;
    nrow = bottom - top;
    ncol = right - left;
    nrow_sm = nrow / rowlook;
    ncol_sm = ncol / collook;
    if (nrow_sm == 0 || ncol_sm == 0) {
      std::cout << "found empty image" << std::endl;
      continue;
    }
    // fill ifg_header
    ifg_header[0] = header1[0] / rowlook;
    ifg_header[1] = header1[1] / collook;
    ifg_header[2] = left / collook;
    ifg_header[3] = top / rowlook;
    ifg_header[4] = right / collook;
    ifg_header[5] = bottom / rowlook;

    // std::fill_n(slc1, max_elements, zero);
    // std::fill_n(slc2, max_elements, zero);
    // std::fill_n(ifglook, max_elements_sm, zero);
    read_and_resample<Complex>(slc_pair[0], slc1, left, top, right, bottom, 0,
                               bottom1 - top1);
    read_and_resample<Complex>(slc_pair[1], slc2, left, top, right, bottom, 0,
                               bottom2 - top2);
    CHECK_CUDA(cudaMemcpy(d_slc1, slc1, sizeof(Complex) * nrow * ncol,
                          cudaMemcpyHostToDevice));
    CHECK_CUDA(cudaMemcpy(d_slc2, slc2, sizeof(Complex) * nrow * ncol,
                          cudaMemcpyHostToDevice));
    numBlocks = (nrow * ncol + blockSize - 1) / blockSize;
    conj_mul<<<numBlocks, blockSize>>>(d_slc1, d_slc2, d_ifg, nrow * ncol);
    CHECK_CUDA(cudaDeviceSynchronize());
    numBlocks = (nrow * ncol_sm + blockSize - 1) / blockSize;
    std::cout << slc_pair[2] << std::endl;
    if (collook > 1) {
      cpx_col_look<<<numBlocks, blockSize>>>(d_ifg, d_ifg_collook, collook,
                                             ncol, nrow * ncol_sm);
    } else {
      d_ifg_collook = d_ifg;
    }
    CHECK_CUDA(cudaDeviceSynchronize());
    numBlocks = (nrow_sm * ncol_sm + blockSize - 1) / blockSize;
    if (rowlook > 1) {
      cpx_row_look<<<numBlocks, blockSize>>>(d_ifg_collook, d_ifglook, rowlook,
                                             ncol_sm, nrow_sm * ncol_sm);
    } else {
      d_ifglook = d_ifg_collook;
    }
    CHECK_CUDA(cudaDeviceSynchronize());
    if (out_float) {
      point_angle<<<numBlocks, blockSize>>>(d_ifglook, d_phase,
                                            nrow_sm * ncol_sm);
      CHECK_CUDA(cudaMemcpy(phase, d_phase, sizeof(float) * nrow_sm * ncol_sm,
                            cudaMemcpyDeviceToHost));
    } else {
      CHECK_CUDA(cudaMemcpy(ifglook, d_ifglook,
                            sizeof(Complex) * nrow_sm * ncol_sm,
                            cudaMemcpyDeviceToHost));
    }
    if (out_float) {
      save_binary<float>(phase, nrow_sm * ncol_sm, ifg_header, NHEADER,
                         slc_pair[2]);
    } else {
      save_binary<Complex>(ifglook, nrow_sm * ncol_sm, ifg_header, NHEADER,
                           slc_pair[2]);
    }
  }

  free(slc1);
  free(slc2);
  if (out_float) {
    free(phase);
    cudaFree(d_phase);
  } else {
    free(ifglook);
  }
  cudaFree(d_slc1);
  cudaFree(d_slc2);
  cudaFree(d_ifg);
  if (collook > 1) {
    cudaFree(d_ifg_collook);
  }
  if (rowlook > 1) {
    cudaFree(d_ifglook);
  }
  return 0;
}

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));
  if (argc < 5) {
    std::cout << "Usage: crossmul input_file rowlook "
              << "collook out_float [--gpu DEVICE_ID]" << std::endl;
    return 0;
  }
  const std::string input_file = std::string(argv[1]);
  const int rowlook = std::stoi(argv[2]);
  const int collook = std::stoi(argv[3]);
  const int out_float = std::stoi(argv[4]);
  crossmul(input_file, rowlook, collook, out_float);
  return 0;
}
