#include <cstdint>
#include <fstream>
#include <iostream>
#include <set>
#include <sstream>
#include <string>
#include <vector>

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

// ---------------------------------------------------------------------------
// GPU kernels
// ---------------------------------------------------------------------------

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
 * GPU kernel to zero out invalid pixels according to a validity mask.
 *
 * @param data Complex array to mask in-place (nrow x ncol)
 * @param mask Validity mask (int32 array, 1=valid, 0=invalid, nrow x ncol)
 * @param nrow Number of rows
 * @param ncol Number of columns
 */
__global__ void apply_mask_kernel(Complex *__restrict__ data,
                                  const int32_t *__restrict__ mask,
                                  const int nrow, const int ncol) {
  int r = blockIdx.y * blockDim.y + threadIdx.y;
  int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (r >= nrow || c >= ncol)
    return;
  int idx = r * ncol + c;
  if (mask[idx] == 0) {
    data[idx].x = 0.0f;
    data[idx].y = 0.0f;
  }
}

/**
 * GPU kernel to compute the maximum absolute phase difference between each
 * pixel and its four nearest (up/down/left/right) neighbors.
 *
 * For a pixel with near-zero amplitude the result is set to zero.
 *
 * @param ifg Input complex interferogram (nrow x ncol)
 * @param ph Output phase-difference map (float array, nrow x ncol)
 * @param nrow Number of rows
 * @param ncol Number of columns
 */
__global__ void phase_diff_kernel(const Complex *__restrict__ ifg,
                                  float *__restrict__ ph, const int nrow,
                                  const int ncol) {
  int r = blockIdx.y * blockDim.y + threadIdx.y;
  int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (r >= nrow || c >= ncol)
    return;

  int idx = r * ncol + c;
  Complex center = ifg[idx];
  float center_amp = sqrtf(center.x * center.x + center.y * center.y);
  if (center_amp < 1e-12f) {
    ph[idx] = 0.0f;
    return;
  }

  float max_diff = 0.0f;

  // Check each of the 4 neighbors
  // Right
  if (c + 1 < ncol) {
    Complex nb = ifg[r * ncol + c + 1];
    float nb_amp = sqrtf(nb.x * nb.x + nb.y * nb.y);
    if (nb_amp > 1e-12f) {
      // conj(center) * nb
      float re = center.x * nb.x + center.y * nb.y;
      float im = center.x * nb.y - center.y * nb.x;
      max_diff = fmaxf(max_diff, fabsf(atan2f(im, re)));
    }
  }
  // Left
  if (c - 1 >= 0) {
    Complex nb = ifg[r * ncol + c - 1];
    float nb_amp = sqrtf(nb.x * nb.x + nb.y * nb.y);
    if (nb_amp > 1e-12f) {
      float re = center.x * nb.x + center.y * nb.y;
      float im = center.x * nb.y - center.y * nb.x;
      max_diff = fmaxf(max_diff, fabsf(atan2f(im, re)));
    }
  }
  // Bottom
  if (r + 1 < nrow) {
    Complex nb = ifg[(r + 1) * ncol + c];
    float nb_amp = sqrtf(nb.x * nb.x + nb.y * nb.y);
    if (nb_amp > 1e-12f) {
      float re = center.x * nb.x + center.y * nb.y;
      float im = center.x * nb.y - center.y * nb.x;
      max_diff = fmaxf(max_diff, fabsf(atan2f(im, re)));
    }
  }
  // Top
  if (r - 1 >= 0) {
    Complex nb = ifg[(r - 1) * ncol + c];
    float nb_amp = sqrtf(nb.x * nb.x + nb.y * nb.y);
    if (nb_amp > 1e-12f) {
      float re = center.x * nb.x + center.y * nb.y;
      float im = center.x * nb.y - center.y * nb.x;
      max_diff = fmaxf(max_diff, fabsf(atan2f(im, re)));
    }
  }

  ph[idx] = max_diff;
}

/**
 * GPU kernel to selectively replace pixels in the interpolated result with
 * original values, based on PS quality and local phase smoothness.
 *
 * Decision per pixel:
 * - If keep_threshold <= 0: always use the interpolated value.
 * - PS pixel (ps != 0): keep original value.
 * - Non-PS with phase_diff > keep_threshold: use interpolated value.
 * - Non-PS with phase_diff <= keep_threshold: keep original value.
 *
 * Additionally, if ``validity_mask`` is not NULL, pixels with
 * ``validity_mask[idx] == 0`` are zeroed.
 *
 * @param ifg_orig Original (pre-interpolation) interferogram
 * @param ifg_interp Interpolated interferogram
 * @param ps PS mask (int32, 1=PS, 0=non-PS, nrow x ncol)
 * @param phase_diff Precomputed per-pixel max adjacent phase difference
 * @param validity_mask Optional validity mask (int32, 1=valid, 0=invalid).
 *                      Pass ``NULL`` to skip.
 * @param nrow Number of rows
 * @param ncol Number of columns
 * @param keep_threshold Phase-difference threshold in radians.  Values
 *                       <= 0 disable selective replacement (all pixels
 *                       use the interpolated value).
 * @param output Output array (may alias ``ifg_orig`` for in-place operation)
 */
__global__ void selective_replace_kernel(
    const Complex *__restrict__ ifg_orig,
    const Complex *__restrict__ ifg_interp, const int32_t *__restrict__ ps,
    const float *__restrict__ phase_diff,
    const int32_t *__restrict__ validity_mask, const int nrow, const int ncol,
    const float keep_threshold, Complex *__restrict__ output) {
  int r = blockIdx.y * blockDim.y + threadIdx.y;
  int c = blockIdx.x * blockDim.x + threadIdx.x;
  if (r >= nrow || c >= ncol)
    return;

  int idx = r * ncol + c;
  Complex val;

  if (keep_threshold > 0.0f) {
    if (ps[idx] != 0) {
      // PS pixel: keep original
      val = ifg_orig[idx];
    } else if (phase_diff[idx] > keep_threshold) {
      // Non-PS with sharp phase discontinuity: use interpolated
      val = ifg_interp[idx];
    } else {
      // Non-PS with smooth phase: keep original
      val = ifg_orig[idx];
    }
  } else {
    // No selective replacement: use interpolated everywhere
    val = ifg_interp[idx];
  }

  // Apply validity mask if provided
  if (validity_mask != NULL && validity_mask[idx] == 0) {
    val.x = 0.0f;
    val.y = 0.0f;
  }

  output[idx] = val;
}

/**
 * GPU kernel to reconstruct a complex interferogram from two real-valued
 * phase arrays.
 *
 * Computes ``ifg = exp(1j * (phase1 - phase2))`` per pixel.
 *
 * @param phase1 First phase array (float, n pixels)
 * @param phase2 Second phase array (float, n pixels)
 * @param ifg_out Output complex interferogram (float2, n pixels)
 * @param n Total number of pixels (nrow * ncol)
 */
__global__ void reconstruct_ifg_kernel(const float *__restrict__ phase1,
                                       const float *__restrict__ phase2,
                                       Complex *__restrict__ ifg_out,
                                       const size_t n) {
  size_t i = blockIdx.x * blockDim.x + threadIdx.x;
  if (i >= n)
    return;
  float diff = phase1[i] - phase2[i];
  sincosf(diff, &ifg_out[i].y, &ifg_out[i].x);
}

// ---------------------------------------------------------------------------
// Helper: launch the templated phase_interp_kernel with the correct NMAX
// ---------------------------------------------------------------------------

static void launch_interp_kernel(const Complex *d_ifg, const int32_t *d_ps,
                                 const int2 *d_indices, size_t nindices,
                                 unsigned int N, float alpha, int nrow,
                                 int ncol, Complex *d_ifg_interp, dim3 grid,
                                 dim3 block) {
  if (N <= 32) {
    phase_interp_kernel<32><<<grid, block>>>(
        d_ifg, d_ps, d_indices, nindices, N, alpha, nrow, ncol, d_ifg_interp);
  } else if (N <= 64) {
    phase_interp_kernel<64><<<grid, block>>>(
        d_ifg, d_ps, d_indices, nindices, N, alpha, nrow, ncol, d_ifg_interp);
  } else if (N <= 128) {
    phase_interp_kernel<128><<<grid, block>>>(
        d_ifg, d_ps, d_indices, nindices, N, alpha, nrow, ncol, d_ifg_interp);
  } else if (N <= 256) {
    phase_interp_kernel<256><<<grid, block>>>(
        d_ifg, d_ps, d_indices, nindices, N, alpha, nrow, ncol, d_ifg_interp);
  } else {
    std::cerr << "Does not support interpolation with more than 256 nearest "
              << "neighbor PS pixels" << std::endl;
    exit(1);
  }
}

// ---------------------------------------------------------------------------
// Core GPU pipeline — shared by single and batch modes
// ---------------------------------------------------------------------------

/**
 * Run the interpolation + optional post-processing pipeline on the GPU.
 *
 * On entry ``d_ifg`` holds the original interferogram.  On exit ``d_ifg``
 * holds the final result (either the raw interpolated output or the
 * selectively-replaced / masked output).
 *
 * ``d_ifg_interp`` is used as workspace and its contents are undefined on
 * return.
 *
 * @param d_ifg        [in/out] Original ifg on device; overwritten with result.
 * @param d_ifg_interp [temp]   Workspace buffer, same size as d_ifg.
 * @param d_ps         PS mask on device (1=PS, 0=non-PS).
 * @param d_mask       Validity mask on device, or ``NULL``.
 * @param d_indices    Spiral search offsets on device.
 * @param nindices     Number of search offsets.
 * @param nrow         Rows per image.
 * @param ncol         Columns per image.
 * @param N            Max PS neighbors.
 * @param alpha        Distance-weighting exponent.
 * @param keep_threshold  Phase-diff threshold for selective replacement
 *                        (<= 0 disables).
 * @param grid         CUDA grid dimensions.
 * @param block        CUDA block dimensions.
 */
static void phase_interp_core(Complex *d_ifg, Complex *d_ifg_interp,
                              const int32_t *d_ps, const int32_t *d_mask,
                              const int2 *d_indices, size_t nindices, int nrow,
                              int ncol, unsigned int N, float alpha,
                              float keep_threshold, dim3 grid, dim3 block) {
  // Step 1: interpolation
  launch_interp_kernel(d_ifg, d_ps, d_indices, nindices, N, alpha, nrow, ncol,
                       d_ifg_interp, grid, block);
  CHECK_CUDA(cudaDeviceSynchronize());

  // Step 2: post-processing (selective replacement + masking)
  bool do_post = (keep_threshold > 0.0f) || (d_mask != NULL);

  if (do_post) {
    // Allocate temporary phase-diff buffer if selective replacement is active
    float *d_phase_diff = NULL;
    if (keep_threshold > 0.0f) {
      size_t npixels = (size_t)nrow * ncol;
      CHECK_CUDA(cudaMalloc((void **)&d_phase_diff, sizeof(float) * npixels));
      phase_diff_kernel<<<grid, block>>>(d_ifg, d_phase_diff, nrow, ncol);
      CHECK_CUDA(cudaDeviceSynchronize());
    }

    // Selective replacement + mask application (single kernel)
    selective_replace_kernel<<<grid, block>>>(d_ifg, d_ifg_interp, d_ps,
                                              d_phase_diff, d_mask, nrow, ncol,
                                              keep_threshold, d_ifg);
    CHECK_CUDA(cudaDeviceSynchronize());

    if (d_phase_diff != NULL) {
      cudaFree(d_phase_diff);
    }
  } else {
    // No post-processing: just copy interpolated result over the input
    size_t npixels = (size_t)nrow * ncol;
    CHECK_CUDA(cudaMemcpy(d_ifg, d_ifg_interp, sizeof(Complex) * npixels,
                          cudaMemcpyDeviceToDevice));
  }
}

// ---------------------------------------------------------------------------
// Single-interferogram mode
// ---------------------------------------------------------------------------

/**
 * PS-based phase interpolation for a single interferogram.
 *
 * Reads an interferogram and a PS mask from binary files, performs
 * GPU-accelerated interpolation using neighboring PS pixels within a
 * spiral search region, optionally applies a validity mask and
 * selective "keep original" replacement, and saves the result.
 *
 * @param ifgfile     Path to the input interferogram (binary float2 file)
 * @param psfile      Path to the PS mask (binary int32 file, 1=PS, 0=non-PS)
 * @param outputfile  Path to the output interpolated interferogram
 * @param nrow        Number of rows in the interferogram
 * @param ncol        Number of columns in the interferogram
 * @param N           Maximum number of nearest PS neighbors to use
 * @param rdmax       Maximum search radius in pixels
 * @param alpha       Exponent controlling distance weighting decay
 * @param maskfile    Optional validity mask (binary int32, 1=valid, 0=invalid).
 *                    Pass an empty string to skip.
 * @param keep_threshold  Phase-diff threshold in radians for selective
 *                        replacement.  Values <= 0 disable the feature.
 */
void phase_interp(const std::string &ifgfile, const std::string &psfile,
                  const std::string &outputfile, const int nrow, const int ncol,
                  const unsigned int N, const unsigned int rdmax,
                  const float alpha, const std::string &maskfile = "",
                  const float keep_threshold = 0.0f) {
  // Host arrays
  Complex *ifg, *d_ifg, *d_ifg_interp;
  int32_t *ps, *d_ps;
  int2 *d_indices;

  // CUDA block dimensions
  dim3 block(16, 16), grid;
  grid.x = (ncol + block.x - 1) / block.x;
  grid.y = (nrow + block.y - 1) / block.y;

  // Generate spiral search indices (rdmin=0 to include all pixels within
  // rdmax)
  IndexArray indices = scan_array(0, rdmax);

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

  // --- Validity mask (optional) ---
  int32_t *mask = NULL;
  int32_t *d_mask = NULL;
  if (!maskfile.empty()) {
    mask = new int32_t[npixels];
    read_binary<int32_t>(maskfile, npixels, mask);
    CHECK_CUDA(cudaMalloc((void **)&d_mask, sizeof(int32_t) * npixels));
    CHECK_CUDA(cudaMemcpy(d_mask, mask, sizeof(int32_t) * npixels,
                          cudaMemcpyHostToDevice));
  }

  // --- Core GPU pipeline ---
  phase_interp_core(d_ifg, d_ifg_interp, d_ps, d_mask, d_indices, indices.size,
                    nrow, ncol, N, alpha, keep_threshold, grid, block);

  // Copy result back to host
  CHECK_CUDA(cudaMemcpy(ifg, d_ifg, sizeof(Complex) * npixels,
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
  if (d_mask != NULL) {
    cudaFree(d_mask);
    delete[] mask;
  }
}

// ---------------------------------------------------------------------------
// Batch mode (optimized phase → interferogram reconstruction + interpolation)
// ---------------------------------------------------------------------------

/** An interferogram identified by its two acquisition dates. */
struct IfgPair {
  std::string date1;
  std::string date2;
};

/**
 * Join path components with ``/``, which works on both Windows and Linux
 * with ``std::ifstream``.
 */
static std::string path_join(const std::string &a, const std::string &b) {
  if (a.empty())
    return b;
  if (b.empty())
    return a;
  if (a.back() == '/')
    return a + b;
  return a + "/" + b;
}

/**
 * Parse a list of interferogram pairs from a text file.
 *
 * Each line should contain ``YYYYMMDD_YYYYMMDD``.  Empty lines and lines
 * beginning with ``#`` are skipped.
 */
static std::vector<IfgPair> read_ifg_pairs(const std::string &listfile) {
  std::vector<IfgPair> pairs;
  std::ifstream fin(listfile);
  if (!fin) {
    std::cerr << "Error: Cannot open interferogram list file: " << listfile
              << std::endl;
    return pairs;
  }
  std::string line;
  while (std::getline(fin, line)) {
    if (line.empty() || line[0] == '#')
      continue;
    // Split on '_'
    size_t sep = line.find('_');
    if (sep == std::string::npos || sep == 0 || sep + 1 >= line.size()) {
      std::cerr << "Warning: Skipping malformed line in ifg list: " << line
                << std::endl;
      continue;
    }
    IfgPair pair;
    pair.date1 = line.substr(0, sep);
    pair.date2 = line.substr(sep + 1);
    pairs.push_back(pair);
  }
  fin.close();
  return pairs;
}

/**
 * Extract unique, sorted acquisition dates from a list of interferogram
 * pairs.
 */
static std::vector<std::string>
extract_dates(const std::vector<IfgPair> &pairs) {
  std::set<std::string> date_set;
  for (const auto &p : pairs) {
    date_set.insert(p.date1);
    date_set.insert(p.date2);
  }
  return std::vector<std::string>(date_set.begin(), date_set.end());
}

/**
 * PS-based phase interpolation for multiple interferograms reconstructed
 * from pre-computed optimized phase files.
 *
 * Iterates over unique acquisition dates.  For each date *i*, its phase
 * file is read once.  Then for every later date *j* for which a pair
 * ``(date_i, date_j)`` is listed in *ifg_list_file*, the phase file for
 * date *j* is read, the complex interferogram is reconstructed on the GPU
 * as ``exp(1j*(phase_i - phase_j))``, interpolated, post-processed, and
 * saved.  Compared to a flat pair loop this halves the number of phase-file
 * reads.
 *
 * GPU memory is allocated once and reused across all iterations.
 *
 * @param phase_dir       Directory containing ``YYYYMMDD.phase`` files
 *                        (float32 binary, nrow x ncol).
 * @param ifg_list_file   Text file listing pairs, one ``YYYYMMDD_YYYYMMDD``
 *                        per line.
 * @param output_dir      Directory for output ``.int`` files (must already
 *                        exist).
 * @param nrow            Number of rows per image.
 * @param ncol            Number of columns per image.
 * @param N               Max PS neighbors.
 * @param rdmax           Max search radius in pixels.
 * @param alpha           Distance-weighting exponent.
 * @param psfile          Path to PS mask (binary int32, 1=PS, 0=non-PS).
 * @param maskfile        Optional validity mask. Pass an empty string to skip.
 * @param keep_threshold  Phase-diff threshold for selective replacement
 *                        (<= 0 disables).
 */
void phase_interp_batch(const std::string &phase_dir,
                        const std::string &ifg_list_file,
                        const std::string &output_dir, const int nrow,
                        const int ncol, const unsigned int N,
                        const unsigned int rdmax, const float alpha,
                        const std::string &psfile,
                        const std::string &maskfile = "",
                        const float keep_threshold = 0.0f) {
  size_t npixels = (size_t)nrow * ncol;

  // --- Read pair list ---
  std::vector<IfgPair> pairs = read_ifg_pairs(ifg_list_file);
  if (pairs.empty()) {
    std::cerr << "Error: No interferogram pairs found in " << ifg_list_file
              << std::endl;
    exit(1);
  }

  // --- Extract unique sorted dates and build pair lookup ---
  std::vector<std::string> dates = extract_dates(pairs);
  int ndate = (int)dates.size();
  std::cout << "Processing " << pairs.size() << " interferogram pairs over "
            << ndate << " dates" << std::endl;

  // Build a set of pair keys for O(1) lookup: "date1_date2"
  std::set<std::string> pair_set;
  for (const auto &p : pairs) {
    pair_set.insert(p.date1 + "_" + p.date2);
  }

  // --- CUDA block / grid ---
  dim3 block(16, 16), grid;
  grid.x = (ncol + block.x - 1) / block.x;
  grid.y = (nrow + block.y - 1) / block.y;

  // --- Spiral indices ---
  IndexArray indices = scan_array(0, rdmax);

  // --- Read PS mask ---
  int32_t *h_ps = new int32_t[npixels];
  read_binary<int32_t>(psfile, npixels, h_ps);

  // --- Read validity mask (optional) ---
  int32_t *h_mask = NULL;
  if (!maskfile.empty()) {
    h_mask = new int32_t[npixels];
    read_binary<int32_t>(maskfile, npixels, h_mask);
  }

  // --- Allocate GPU memory (once, reused) ---
  float *d_phase1, *d_phase2;
  Complex *d_ifg, *d_ifg_interp;
  int32_t *d_ps, *d_mask = NULL;
  int2 *d_indices;

  CHECK_CUDA(cudaMalloc((void **)&d_phase1, sizeof(float) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_phase2, sizeof(float) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_ifg, sizeof(Complex) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_ifg_interp, sizeof(Complex) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_ps, sizeof(int32_t) * npixels));
  CHECK_CUDA(cudaMalloc((void **)&d_indices, sizeof(int2) * indices.size));

  CHECK_CUDA(cudaMemcpy(d_ps, h_ps, sizeof(int32_t) * npixels,
                        cudaMemcpyHostToDevice));
  CHECK_CUDA(cudaMemcpy(d_indices, indices.data, sizeof(int2) * indices.size,
                        cudaMemcpyHostToDevice));

  if (h_mask != NULL) {
    CHECK_CUDA(cudaMalloc((void **)&d_mask, sizeof(int32_t) * npixels));
    CHECK_CUDA(cudaMemcpy(d_mask, h_mask, sizeof(int32_t) * npixels,
                          cudaMemcpyHostToDevice));
  }

  // --- Host buffers (reused) ---
  float *h_phase1 = new float[npixels];
  float *h_phase2 = new float[npixels];
  Complex *h_output = new Complex[npixels];

  // --- Reconstruct-ifg kernel launch config (1-D) ---
  dim3 recon_grid((npixels + 255) / 256);
  dim3 recon_block(256);

  // --- Nested loop over dates (outer reads phase1 once, inner reads ---
  // --- phase2 per-pair — halves phase-file I/O)                    ---
  size_t counter = 0;

  for (int i = 0; i < ndate - 1; ++i) {
    const std::string &date1 = dates[i];

    // Read phase1 once for this outer iteration
    {
      std::string path = path_join(phase_dir, date1 + ".phase");
      std::ifstream fin(path, std::ios::binary);
      if (!fin) {
        std::cerr << "Warning: Cannot open " << path
                  << ", skipping all pairs with " << date1 << std::endl;
        continue;
      }
      fin.read(reinterpret_cast<char *>(h_phase1), sizeof(float) * npixels);
      fin.close();
    }
    CHECK_CUDA(cudaMemcpy(d_phase1, h_phase1, sizeof(float) * npixels,
                          cudaMemcpyHostToDevice));

    for (int j = i + 1; j < ndate; ++j) {
      const std::string &date2 = dates[j];

      // Skip if this pair is not in the requested list
      if (pair_set.find(date1 + "_" + date2) == pair_set.end())
        continue;

      // Read phase2
      {
        std::string path = path_join(phase_dir, date2 + ".phase");
        std::ifstream fin(path, std::ios::binary);
        if (!fin) {
          std::cerr << "Warning: Cannot open " << path << ", skipping pair "
                    << date1 << "_" << date2 << std::endl;
          continue;
        }
        fin.read(reinterpret_cast<char *>(h_phase2), sizeof(float) * npixels);
        fin.close();
      }
      CHECK_CUDA(cudaMemcpy(d_phase2, h_phase2, sizeof(float) * npixels,
                            cudaMemcpyHostToDevice));

      // Reconstruct interferogram on GPU
      reconstruct_ifg_kernel<<<recon_grid, recon_block>>>(d_phase1, d_phase2,
                                                          d_ifg, npixels);
      CHECK_CUDA(cudaDeviceSynchronize());

      // Interpolation + post-processing
      phase_interp_core(d_ifg, d_ifg_interp, d_ps, d_mask, d_indices,
                        indices.size, nrow, ncol, N, alpha, keep_threshold,
                        grid, block);

      // Download result
      CHECK_CUDA(cudaMemcpy(h_output, d_ifg, sizeof(Complex) * npixels,
                            cudaMemcpyDeviceToHost));

      // Save
      {
        std::string out_path =
            path_join(output_dir, date1 + "_" + date2 + ".int");
        save_binary<Complex>(h_output, npixels, out_path);
      }

      ++counter;
      if (counter % 10 == 0 || counter == pairs.size()) {
        std::cout << "  Processed " << counter << " / " << pairs.size()
                  << " pairs" << std::endl;
      }
    }
  }

  // --- Cleanup ---
  delete[] h_ps;
  delete[] h_phase1;
  delete[] h_phase2;
  delete[] h_output;
  if (h_mask != NULL)
    delete[] h_mask;

  free_index_array(&indices);
  cudaFree(d_phase1);
  cudaFree(d_phase2);
  cudaFree(d_ifg);
  cudaFree(d_ifg_interp);
  cudaFree(d_ps);
  cudaFree(d_indices);
  if (d_mask != NULL)
    cudaFree(d_mask);

  std::cout << "Batch processing complete." << std::endl;
}

// ---------------------------------------------------------------------------
// Argument parsing helpers
// ---------------------------------------------------------------------------

/**
 * Search command-line arguments for a named string-valued flag.
 *
 * @returns the value following *flag*, or *default_val* if the flag is absent.
 */
static std::string parse_string_arg(int argc, char *argv[],
                                    const std::string &flag,
                                    const std::string &default_val = "") {
  for (int i = 1; i < argc - 1; ++i) {
    if (std::string(argv[i]) == flag) {
      return std::string(argv[i + 1]);
    }
  }
  return default_val;
}

/**
 * Search command-line arguments for a named float-valued flag.
 *
 * @returns the value following *flag*, or *default_val* if the flag is absent.
 */
static float parse_float_arg(int argc, char *argv[], const std::string &flag,
                             float default_val = 0.0f) {
  for (int i = 1; i < argc - 1; ++i) {
    if (std::string(argv[i]) == flag) {
      return std::stof(argv[i + 1]);
    }
  }
  return default_val;
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));

  // Detect batch mode by the --batch flag
  bool batch_mode = false;
  if (argc >= 2 && std::string(argv[1]) == "--batch") {
    batch_mode = true;
  }

  if (batch_mode) {
    // Usage: phase_interp --batch phase_dir ifg_list_file output_dir psfile
    //                     nrow ncol N rdmax alpha
    //                     [--mask maskfile] [--keep-orig threshold] [--gpu
    //                     DEVICE_ID]
    if (argc < 8) {
      std::cout << "Usage: phase_interp --batch phase_dir ifg_list_file "
                   "output_dir psfile "
                << "nrow ncol N rdmax alpha [--mask maskfile] "
                   "[--keep-orig threshold] [--gpu DEVICE_ID]"
                << std::endl;
      return 0;
    }

    // Positional args after --batch start at index 2
    const std::string phase_dir = std::string(argv[2]);
    const std::string ifg_list_file = std::string(argv[3]);
    const std::string output_dir = std::string(argv[4]);
    const std::string psfile = std::string(argv[5]);
    const int nrow = std::stoi(argv[6]);
    const int ncol = std::stoi(argv[7]);

    if (argc < 11) {
      std::cout << "Usage: phase_interp --batch phase_dir ifg_list_file "
                   "output_dir psfile "
                << "nrow ncol N rdmax alpha [--mask maskfile] "
                   "[--keep-orig threshold] [--gpu DEVICE_ID]"
                << std::endl;
      return 0;
    }
    const unsigned int N = std::stoi(argv[8]);
    const unsigned int rdmax = std::stoi(argv[9]);
    const float alpha = std::stof(argv[10]);

    const std::string mask_file = parse_string_arg(argc, argv, "--mask", "");
    const float keep_threshold =
        parse_float_arg(argc, argv, "--keep-orig", 0.0f);

    phase_interp_batch(phase_dir, ifg_list_file, output_dir, nrow, ncol, N,
                       rdmax, alpha, psfile, mask_file, keep_threshold);

  } else {
    // Single-interferogram mode
    // Usage: phase_interp ifgfile psfile outputfile nrow ncol N rdmax alpha
    //                     [--mask maskfile] [--keep-orig threshold] [--gpu
    //                     DEVICE_ID]
    if (argc < 9) {
      std::cout << "Usage: phase_interp ifgfile psfile outputfile "
                << "nrow ncol N rdmax alpha [--mask maskfile] "
                   "[--keep-orig threshold] [--gpu DEVICE_ID]"
                << std::endl;
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

    const std::string mask_file = parse_string_arg(argc, argv, "--mask", "");
    const float keep_threshold =
        parse_float_arg(argc, argv, "--keep-orig", 0.0f);

    phase_interp(ifgfile, psfile, outfile, nrow, ncol, N, rdmax, alpha,
                 mask_file, keep_threshold);
  }

  return 0;
}
