#include "bounds.hpp"
#include "gpu_device.hpp"
#include "orbit.hpp"
#include "sario.hpp"
#include "sql_mod.hpp"
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include <fstream>
#include <iostream>
#include <sqlite3.h>
#include <string>

#ifdef _WIN32
#include <fcntl.h>
#include <io.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

#ifndef SOL
#define SOL 299792458.0
#endif

// ----------------- Utility macros -----------------
#define CHECK_CUDA(x)                                                          \
  do {                                                                         \
    cudaError_t err = (x);                                                     \
    if (err != cudaSuccess) {                                                  \
      std::cerr << "CUDA error " << cudaGetErrorString(err) << " at "          \
                << __FILE__ << ":" << __LINE__ << std::endl;                   \
      exit(1);                                                                 \
    }                                                                          \
  } while (0)

// ----------------- Helper functions -----------------

bool file_exists(const std::string &filename) {
  std::ifstream f(filename);
  return f.good();
}

int closest_sample(const double *t, const double t0, const int n) {
  int idx = 0;
  double mindist = 1e10, dist;
  for (int i = 0; i < n; ++i) {
    dist = fabs(t[i] - t0);
    if (mindist > dist) {
      mindist = dist;
      idx = i;
    }
  }
  return idx;
}

void set_stdin_binary() {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
#endif
}

template <typename T> void read_binary_stdin(const std::size_t n, T *img) {
  size_t bytes_to_read = sizeof(T) * n;
  size_t bytes_read = fread(img, 1, bytes_to_read, stdin);
  if (bytes_read != bytes_to_read) {
    std::cerr << "Error: expected " << bytes_to_read
              << " bytes from stdin, got " << bytes_read << std::endl;
    exit(1);
  }
}

// ------------------ CUDA kernels ------------------

/**
 * Finds the first and last non-all-zero rows in a row-major float2 array.
 *
 * One thread block per row, 256 threads per block.  Threads within a block
 * stride over columns; a block-wide reduction determines whether the row has a
 * non-zero element.  Thread 0 atomically updates global min/max row indices.
 */
__global__ void find_first_last_nonzero_rows_kernel(const float2 *d_data,
                                                    int rows, int cols,
                                                    int *d_first, int *d_last) {
  int row = blockIdx.x;
  int tid = threadIdx.x;
  int stride = blockDim.x; // 256

  const float2 *row_ptr = d_data + (size_t)row * cols;

  bool has_nonzero = false;
  for (int c = tid; c < cols; c += stride) {
    float2 val = row_ptr[c];
    if (val.x != 0.0f || val.y != 0.0f) {
      has_nonzero = true;
      break;
    }
  }

  __shared__ bool sdata[256];
  sdata[tid] = has_nonzero;
  __syncthreads();

  if (tid < 128)
    sdata[tid] = sdata[tid] || sdata[tid + 128];
  __syncthreads();
  if (tid < 64)
    sdata[tid] = sdata[tid] || sdata[tid + 64];
  __syncthreads();

  if (tid < 32) {
    sdata[tid] = sdata[tid] || sdata[tid + 32];
    sdata[tid] = sdata[tid] || sdata[tid + 16];
    sdata[tid] = sdata[tid] || sdata[tid + 8];
    sdata[tid] = sdata[tid] || sdata[tid + 4];
    sdata[tid] = sdata[tid] || sdata[tid + 2];
    sdata[tid] = sdata[tid] || sdata[tid + 1];
  }

  if (tid == 0 && sdata[0]) {
    atomicMin(d_first, row);
    atomicMax(d_last, row + 1);
  }
}

/**
 * Deramp burst SLC data in-place on the device.
 *
 * For each pixel at (row, col):
 *   etadiff = eta[row] - etaref[col]
 *   phase   = -pi * kt[col] * etadiff^2
 *   burst   = burst * exp(i * phase)
 */
__global__ void deramp_burst(double *kt, double *eta, double *etaref,
                             Complex *burst, const std::size_t nrange,
                             const std::size_t n) {
  std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  std::size_t stride = blockDim.x * gridDim.x;
  unsigned int row, col;
  double phase, etadiff;
  Complex a, b;
  for (std::size_t i = index; i < n; i += stride) {
    a.x = burst[i].x;
    a.y = burst[i].y;
    row = i / nrange;
    col = i - row * nrange;
    etadiff = eta[row] - etaref[col];
    phase = -M_PI * kt[col] * etadiff * etadiff;
    b.x = cos(phase);
    b.y = sin(phase);
    burst[i].x = a.x * b.x - a.y * b.y;
    burst[i].y = a.x * b.y + a.y * b.x;
  }
}

/**
 * Reproject deramped burst data to geographic grid.
 *
 * For each output pixel:
 *   1. Compute lat/lon from row/col, llh→xyz
 *   2. Compute range/azimuth time via orbit iteration
 *   3. Bilinearly interpolate deramped burst data
 *   4. Apply phase correction: exp(i * (4*pi/wvl*rngpix - reramp))
 *
 */
__global__ void
reproject(const short int *dem, const Complex *burstdata,
          Complex *__restrict__ outdata, double *tt, double *xx, double *vv,
          const std::size_t nstatvec, const double tmid, double *xmid,
          double *vmid, const double latmax, const double lonmin,
          const double dlat, const double dlon, const std::size_t nlon,
          const double rngstart, const double tstart, const double dmrg,
          const double dtaz, const int nrange, const int lines_per_burst,
          const double fmt0intp, const double fmc0intp, const double fmc1intp,
          const double fmc2intp, const double dct0intp, const double dcc0intp,
          const double dcc1intp, const double dcc2intp, const double etac0,
          const double ks, const int first_valid_line,
          const int last_valid_line, const int first_valid_sample,
          const int last_valid_sample, const double wvl, const std::size_t n) {
  std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  std::size_t stride = blockDim.x * gridDim.x;
  int row, col, intr, inta;
  double lat, lon, h;
  std::size_t idx1, idx2, idx3, idx4;
  double tline, rngpix, rgoff, azoff, fracr, fraca, llh[3], xyz[3], dr[3];
  double resx, resy, cosphase, sinphase, phase, reramp;
  Complex cpx1, cpx2, burst1, burst2, burst3, burst4, res, zero;
  zero.x = 0;
  zero.y = 0;
  for (std::size_t i = index; i < n; i += stride) {
    row = i / nlon;
    col = i - nlon * row;
    lat = latmax + row * dlat;
    lon = lonmin + col * dlon;
    h = dem[i];
    llh[0] = lat;
    llh[1] = lon;
    llh[2] = h;
    llh2xyz(llh, xyz);
    orbitrangetime(nstatvec, tt, xx, vv, xyz, tmid, xmid, vmid, tline, dr);
    rngpix = sqrt(dr[0] * dr[0] + dr[1] * dr[1] + dr[2] * dr[2]);
    rgoff = (rngpix - rngstart) / dmrg;
    azoff = (tline - tstart) / dtaz;
    if (rgoff > first_valid_sample && rgoff < last_valid_sample - 1 &&
        azoff >= first_valid_line + 5 && azoff < last_valid_line - 5) {
      double dt = rngpix * 2 / SOL - fmt0intp;
      double ka = fmc0intp + dt * (fmc1intp + fmc2intp * dt);
      dt = rngpix * 2 / SOL - dct0intp;
      double fnc = dcc0intp + dt * (dcc1intp + dcc2intp * dt);
      double kt = ka * ks / (ka - ks);
      double etac = -fnc / ka;
      double etaref = etac - etac0;
      double eta = -lines_per_burst * dtaz / 2. + tline - tstart;
      double etadiff = eta - etaref;
      reramp = -M_PI * kt * etadiff * etadiff;
      intr = int(rgoff);
      fracr = rgoff - intr;
      inta = int(azoff);
      fraca = azoff - inta;
      idx1 = std::size_t(inta) * nrange + intr;
      idx2 = idx1 + 1;
      idx3 = idx1 + nrange;
      idx4 = idx3 + 1;
      burst1 = burstdata[idx1];
      burst2 = burstdata[idx2];
      burst3 = burstdata[idx3];
      burst4 = burstdata[idx4];
      cpx1.x = burst1.x * (1 - fracr) + burst2.x * fracr;
      cpx1.y = burst1.y * (1 - fracr) + burst2.y * fracr;
      cpx2.x = burst3.x * (1 - fracr) + burst4.x * fracr;
      cpx2.y = burst3.y * (1 - fracr) + burst4.y * fracr;
      resx = cpx1.x * (1 - fraca) + cpx2.x * fraca;
      resy = cpx1.y * (1 - fraca) + cpx2.y * fraca;
      phase = 4.0 * M_PI / wvl * rngpix - reramp;
      cosphase = cos(phase);
      sinphase = sin(phase);
      res.x = resx * cosphase - resy * sinphase;
      res.y = resx * sinphase + resy * cosphase;
      outdata[i] = res;
    } else {
      outdata[i] = zero;
    }
  }
}

// ------------------ Host wrapper for non-zero row search ------------------

/**
 * Launch find_nonzero_rows kernel and retrieve results.
 *
 * Unlike the original in geo2rdr_reramp.cu, this function does NOT allocate or
 * free `d_first`/`d_last`.  The caller pre-allocates them once and resets their
 * contents (e.g. with cudaMemcpy of initial sentinel values) before each call.
 *
 * Parameters:
 *   d_data        - Device pointer to the float2 output array.
 *   rows          - Number of rows.
 *   cols          - Number of columns.
 *   d_first       - Pre-allocated device int, must be reset to `rows` before
 *                   call.
 *   d_last        - Pre-allocated device int, must be reset to `-1` before
 *                   call.
 *   first_idx     - (output) Index of the first non-zero row (inclusive).
 *   last_idx_excl - (output) Index of the row after the last non-zero row
 *                   (exclusive).
 *
 * Return: true if at least one non-zero row exists.
 */
bool find_nonzero_rows(const float2 *d_data, int rows, int cols, int *d_first,
                       int *d_last, int &first_idx, int &last_idx_excl) {
  if (d_data == nullptr || rows <= 0 || cols <= 0) {
    first_idx = 0;
    last_idx_excl = 0;
    return false;
  }

  int threads_per_block = 256;
  int blocks = rows;
  find_first_last_nonzero_rows_kernel<<<blocks, threads_per_block>>>(
      d_data, rows, cols, d_first, d_last);

  cudaDeviceSynchronize();

  CHECK_CUDA(
      cudaMemcpy(&first_idx, d_first, sizeof(int), cudaMemcpyDeviceToHost));
  CHECK_CUDA(
      cudaMemcpy(&last_idx_excl, d_last, sizeof(int), cudaMemcpyDeviceToHost));

  if (first_idx == rows) {
    first_idx = 0;
    last_idx_excl = 0;
    return false;
  }
  return true;
}

// ------------------ Main pipeline ------------------

/**
 * Unified geo2rdr pipeline:
 *
 * For each burst:
 *   1. Read original (non-deramped) SLC data from stdin or file.
 *   2. Compute deramp coefficients (kt, eta, etaref) on host, copy to device.
 *   3. Launch deramp_burst kernel → deramps burst data in-place.
 *   4. Launch reproject kernel → bilinearly interpolates deramped data,
 *      recomputes the reramp phase analytically at each pixel (no
 *      pre-computed deramp_phase file needed), and applies the range
 *      propagation phase.
 *   5. Find valid output rows; copy result to host and save.
 */
int geo2rdr(const std::string &dbname, const std::string &slcoutfile,
            const std::string &slcinfile, const bool use_stdin) {
  sqlite3 *db;
  if (sqlite3_open(dbname.c_str(), &db)) {
    std::cerr << "Cannot open database: " << sqlite3_errmsg(db) << std::endl;
    return 1;
  }
  const std::string tblname = "file";

  // ---- Phase 1: Read DB parameters (combined from both original files) ----
  std::string orbfile, demfile, rscfile, fmratefile, dcfile;
  std::size_t nstatvec, burstsize, buffer_size;
  int azimuth_bursts, lines_per_burst, nrange, blockSize = 256, numBlocks;
  int nrow_buffer, ncol_buffer, npolyfm, npolydc;
  double prf, wvl, slant_range_time, range_sampling_rate, hmin, hmax;
  double azimuth_steering_rate, azimuth_time_interval, radar_frequency;

  demfile = get_params(db, tblname, "demfile");
  rscfile = get_params(db, tblname, "rscfile");
  nrange = get_parami(db, tblname, "samplesPerBurst");
  lines_per_burst = get_parami(db, tblname, "linesPerBurst");
  azimuth_bursts = get_parami(db, tblname, "azimuthBursts");
  prf = get_paramd(db, tblname, "prf");
  wvl = get_paramd(db, tblname, "wvl");
  hmin = get_paramd(db, tblname, "hmin");
  hmax = get_paramd(db, tblname, "hmax");
  range_sampling_rate = get_paramd(db, tblname, "rangeSamplingRate");
  slant_range_time = get_paramd(db, tblname, "slantRangeTime");
  azimuth_steering_rate = get_paramd(db, tblname, "azimuthSteeringRate");
  azimuth_time_interval = get_paramd(db, tblname, "azimuthTimeInterval");
  radar_frequency = get_paramd(db, tblname, "radarFrequency");
  orbfile = get_params(db, tblname, "orbinfo");
  fmratefile = get_params(db, tblname, "fmrateinfo");
  dcfile = get_params(db, tblname, "dcinfo");
  // Read slc_file fallback while DB is still open (used for non-stdin mode)
  std::string slc_file_db = get_params(db, tblname, "slc_file");

  burstsize = nrange * lines_per_burst;

  // ---- Phase 2: Host allocations for per-burst metadata ----
  double *startime = (double *)malloc(sizeof(double) * azimuth_bursts);
  int *first_valid_line = (int *)malloc(sizeof(int) * azimuth_bursts);
  int *last_valid_line = (int *)malloc(sizeof(int) * azimuth_bursts);
  int *first_valid_sample = (int *)malloc(sizeof(int) * azimuth_bursts);
  int *last_valid_sample = (int *)malloc(sizeof(int) * azimuth_bursts);
  double *xmid = (double *)malloc(sizeof(double) * 3);
  double *vmid = (double *)malloc(sizeof(double) * 3);
  double *latlons = (double *)malloc(sizeof(double) * azimuth_bursts * 4);

  for (int iburst = 0; iburst < azimuth_bursts; iburst++) {
    std::string ts_key = "azimuthTimeSeconds" + std::to_string(iburst + 1);
    std::string flv_key = "firstValidLine" + std::to_string(iburst + 1);
    std::string llv_key = "lastValidLine" + std::to_string(iburst + 1);
    std::string fsv_key = "firstValidSample" + std::to_string(iburst + 1);
    std::string lsv_key = "lastValidSample" + std::to_string(iburst + 1);
    startime[iburst] = get_paramd(db, tblname, ts_key);
    first_valid_line[iburst] = get_parami(db, tblname, flv_key);
    last_valid_line[iburst] = get_parami(db, tblname, llv_key);
    first_valid_sample[iburst] = get_parami(db, tblname, fsv_key);
    last_valid_sample[iburst] = get_parami(db, tblname, lsv_key);
  }

  // ---- Phase 3: Read orbit, fmrate, dc polynomial data ----
  double *tt, *xx, *vv;
  double *fmtime, *fmt0, *fmc0, *fmc1, *fmc2;
  double *dctime, *dct0, *dcc0, *dcc1, *dcc2;
  read_orbit_ascii(orbfile, nstatvec, &tt, &xx, &vv);
  read_polynomials(fmratefile, npolyfm, &fmtime, &fmt0, &fmc0, &fmc1, &fmc2);
  read_polynomials(dcfile, npolydc, &dctime, &dct0, &dcc0, &dcc1, &dcc2);

  // ---- Phase 4: Compute per-burst bounding boxes ----
  for (int iburst = 0; iburst < azimuth_bursts; iburst++) {
    double tstart, dtaz, tend, tmid;
    double rngstart, dmrg, rngend;
    tstart = startime[iburst];
    dtaz = 1. / prf;
    tend = tstart + (lines_per_burst - 1) * dtaz;
    tmid = (tstart + tend) * 0.5;
    intp_orbit(nstatvec, tt, xx, vv, tmid, xmid, vmid);
    rngstart = slant_range_time * SOL / 2.0;
    dmrg = SOL / 2.0 / range_sampling_rate;
    rngend = rngstart + (nrange - 1) * dmrg;
    bounds(tstart, tend, rngstart, rngend, hmin, hmax, tt, xx, vv, nstatvec,
           latlons + iburst * 4, "RIGHT");
  }

  if (sqlite3_close(db) != SQLITE_OK) {
    std::cerr << "Can't close database: " << sqlite3_errmsg(db) << std::endl;
    return -1;
  }

  // ---- Phase 5: Read DEM RSC, compute buffer size ----
  rsc demrsc = readrsc(rscfile);
  nrow_buffer = 0;
  ncol_buffer = 0;
  for (int i = 0; i < azimuth_bursts; i++) {
    double latmin, latmax, lonmin, lonmax;
    int nrowi, ncoli;
    latmin = latlons[4 * i];
    latmax = latlons[4 * i + 1];
    lonmin = latlons[4 * i + 2];
    lonmax = latlons[4 * i + 3];
    nrowi = int((latmin - latmax) / demrsc.dlat + 1);
    nrow_buffer = std::max(nrowi, nrow_buffer);
    ncoli = int((lonmax - lonmin) / demrsc.dlon + 1);
    ncol_buffer = std::max(ncoli, ncol_buffer);
  }
  buffer_size = sizeof(Complex) * nrow_buffer * ncol_buffer;

  // ---- Phase 6: Pre-allocate host memory ----
  Complex *burst = (Complex *)malloc(sizeof(Complex) * burstsize);
  Complex *outdata = (Complex *)malloc(buffer_size);
  short int *dem =
      (short int *)malloc(sizeof(short int) * nrow_buffer * ncol_buffer);
  double *kt = (double *)malloc(sizeof(double) * nrange);
  double *eta = (double *)malloc(sizeof(double) * lines_per_burst);
  double *etaref = (double *)malloc(sizeof(double) * nrange);
  double *ka = (double *)malloc(sizeof(double) * nrange);
  double *etac = (double *)malloc(sizeof(double) * nrange);
  double *fnc = (double *)malloc(sizeof(double) * nrange);

  // ---- Phase 7: Pre-allocate device memory (once, reused across bursts) ----
  double *d_tt, *d_xx, *d_vv, *d_xmid, *d_vmid;
  double *d_kt, *d_eta, *d_etaref;
  Complex *d_burstdata, *d_outdata;
  short int *d_dem;
  int *d_first, *d_last; // pre-allocated for find_nonzero_rows

  cudaMalloc((void **)&d_tt, sizeof(double) * nstatvec);
  cudaMalloc((void **)&d_xx, sizeof(double) * nstatvec * 3);
  cudaMalloc((void **)&d_vv, sizeof(double) * nstatvec * 3);
  cudaMalloc((void **)&d_xmid, sizeof(double) * 3);
  cudaMalloc((void **)&d_vmid, sizeof(double) * 3);
  cudaMalloc((void **)&d_kt, sizeof(double) * nrange);
  cudaMalloc((void **)&d_eta, sizeof(double) * lines_per_burst);
  cudaMalloc((void **)&d_etaref, sizeof(double) * nrange);
  cudaMalloc((void **)&d_burstdata, sizeof(Complex) * burstsize);
  cudaMalloc((void **)&d_outdata, buffer_size);
  cudaMalloc((void **)&d_dem, sizeof(short int) * nrow_buffer * ncol_buffer);
  cudaMalloc((void **)&d_first, sizeof(int));
  cudaMalloc((void **)&d_last, sizeof(int));

  // Copy orbit data to device once (same for all bursts)
  cudaMemcpy(d_tt, tt, sizeof(double) * nstatvec, cudaMemcpyHostToDevice);
  cudaMemcpy(d_xx, xx, sizeof(double) * nstatvec * 3, cudaMemcpyHostToDevice);
  cudaMemcpy(d_vv, vv, sizeof(double) * nstatvec * 3, cudaMemcpyHostToDevice);

  numBlocks = (burstsize + blockSize - 1) / blockSize;

  // ---- Phase 8: Burst loop ----
  for (int iburst = 0; iburst < azimuth_bursts; ++iburst) {
    // for (int iburst = 1; iburst < 2; ++iburst) {
    //  --- 8a. Compute kt/eta/etaref on host ---
    double timecenterseconds;
    double xpoint[3], vpoint[3], vs, ks;
    double dcc0intp, dcc1intp, dcc2intp, dct0intp;
    double fmc0intp, fmc1intp, fmc2intp, fmt0intp;
    double trange, dt;
    double frac;
    int idx;

    timecenterseconds =
        startime[iburst] + lines_per_burst * azimuth_time_interval / 2;
    intp_orbit(nstatvec, tt, xx, vv, timecenterseconds, xpoint, vpoint);
    vs = sqrt(vpoint[0] * vpoint[0] + vpoint[1] * vpoint[1] +
              vpoint[2] * vpoint[2]);
    ks =
        2.0 * vs * radar_frequency / SOL * azimuth_steering_rate * M_PI / 180.0;

    idx = closest_sample(fmtime, timecenterseconds, npolyfm);
    if (fmtime[idx] < timecenterseconds && idx < npolyfm - 1) {
      frac =
          (timecenterseconds - fmtime[idx]) / (fmtime[idx + 1] - fmtime[idx]);
      fmc0intp = fmc0[idx] + frac * (fmc0[idx + 1] - fmc0[idx]);
      fmc1intp = fmc1[idx] + frac * (fmc1[idx + 1] - fmc1[idx]);
      fmc2intp = fmc2[idx] + frac * (fmc2[idx + 1] - fmc2[idx]);
      fmt0intp = fmt0[idx] + frac * (fmt0[idx + 1] - fmt0[idx]);
    } else if (fmtime[idx] > timecenterseconds && idx > 0) {
      frac = (timecenterseconds - fmtime[idx - 1]) /
             (fmtime[idx] - fmtime[idx - 1]);
      fmc0intp = fmc0[idx - 1] + frac * (fmc0[idx] - fmc0[idx - 1]);
      fmc1intp = fmc1[idx - 1] + frac * (fmc1[idx] - fmc1[idx - 1]);
      fmc2intp = fmc2[idx - 1] + frac * (fmc2[idx] - fmc2[idx - 1]);
      fmt0intp = fmt0[idx - 1] + frac * (fmt0[idx] - fmt0[idx - 1]);
    } else {
      fmc0intp = fmc0[idx];
      fmc1intp = fmc1[idx];
      fmc2intp = fmc2[idx];
      fmt0intp = fmt0[idx];
    }

    idx = closest_sample(dctime, timecenterseconds, npolydc);
    if (dctime[idx] < timecenterseconds && idx < npolydc - 1) {
      frac =
          (timecenterseconds - dctime[idx]) / (dctime[idx + 1] - dctime[idx]);
      dcc0intp = dcc0[idx] + frac * (dcc0[idx + 1] - dcc0[idx]);
      dcc1intp = dcc1[idx] + frac * (dcc1[idx + 1] - dcc1[idx]);
      dcc2intp = dcc2[idx] + frac * (dcc2[idx + 1] - dcc2[idx]);
      dct0intp = dct0[idx] + frac * (dct0[idx + 1] - dct0[idx]);
    } else if (dctime[idx] > timecenterseconds && idx > 0) {
      frac = (timecenterseconds - dctime[idx - 1]) /
             (dctime[idx] - dctime[idx - 1]);
      dcc0intp = dcc0[idx - 1] + frac * (dcc0[idx] - dcc0[idx - 1]);
      dcc1intp = dcc1[idx - 1] + frac * (dcc1[idx] - dcc1[idx - 1]);
      dcc2intp = dcc2[idx - 1] + frac * (dcc2[idx] - dcc2[idx - 1]);
      dct0intp = dct0[idx - 1] + frac * (dct0[idx] - dct0[idx - 1]);
    } else {
      dcc0intp = dcc0[idx];
      dcc1intp = dcc1[idx];
      dcc2intp = dcc2[idx];
      dct0intp = dct0[idx];
    }

    for (int i = 0; i < lines_per_burst; ++i) {
      eta[i] = -lines_per_burst * azimuth_time_interval / 2. +
               i * azimuth_time_interval;
    }
    for (int i = 0; i < nrange; ++i) {
      trange = slant_range_time + i / range_sampling_rate;
      dt = trange - fmt0intp;
      ka[i] = fmc0intp + dt * (fmc1intp + fmc2intp * dt);
      dt = trange - dct0intp;
      fnc[i] = dcc0intp + dt * (dcc1intp + dcc2intp * dt);
      kt[i] = ka[i] * ks / (ka[i] - ks);
      etac[i] = -fnc[i] / ka[i];
      etaref[i] = etac[i] - etac[0];
    }

    // --- 8b. Copy deramp coefficients to device ---
    cudaMemcpy(d_kt, kt, sizeof(double) * nrange, cudaMemcpyHostToDevice);
    cudaMemcpy(d_etaref, etaref, sizeof(double) * nrange,
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_eta, eta, sizeof(double) * lines_per_burst,
               cudaMemcpyHostToDevice);

    // --- 8c. Read original SLC burst data ---
    if (use_stdin) {
      read_binary_stdin<Complex>(burstsize, burst);
    } else {
      std::string srcfile = slcinfile.empty() ? slc_file_db : slcinfile;
      read_binary<Complex>(srcfile, iburst * burstsize, burstsize, burst);
    }

    // --- 8d. Copy burst data to device ---
    cudaMemcpy(d_burstdata, burst, sizeof(Complex) * burstsize,
               cudaMemcpyHostToDevice);

    // --- 8e. Deramp on GPU (burst data deramped in-place) ---
    deramp_burst<<<numBlocks, blockSize>>>(d_kt, d_eta, d_etaref, d_burstdata,
                                           nrange, burstsize);
    CHECK_CUDA(cudaDeviceSynchronize());

    // --- 8f. Compute burst geometry ---
    double latmin, latmax, lonmin, lonmax;
    int left, top, right, bottom, nrow, ncol;
    latmin = latlons[4 * iburst];
    latmax = latlons[4 * iburst + 1];
    lonmin = latlons[4 * iburst + 2];
    lonmax = latlons[4 * iburst + 3];
    left = int((lonmin - demrsc.lonmin) / demrsc.dlon);
    top = int((latmax - demrsc.latmax) / demrsc.dlat);
    right = int((lonmax - demrsc.lonmin) / demrsc.dlon + 1);
    bottom = int((latmin - demrsc.latmax) / demrsc.dlat + 1);
    left = std::max(0, left);
    top = std::max(0, top);
    right = std::min(demrsc.nlon, right);
    bottom = std::min(demrsc.nlat, bottom);
    if (left >= right || top >= bottom)
      continue;
    nrow = bottom - top;
    ncol = right - left;

    double tstart, dtaz, tend, tmid;
    double rngstart, dmrg;
    tstart = startime[iburst];
    dtaz = 1. / prf;
    tend = tstart + (lines_per_burst - 1) * dtaz;
    tmid = (tstart + tend) * 0.5;
    rngstart = slant_range_time * SOL / 2.0;
    dmrg = SOL / 2.0 / range_sampling_rate;
    intp_orbit(nstatvec, tt, xx, vv, tmid, xmid, vmid);

    // --- 8g. Copy xmid/vmid to device ---
    cudaMemcpy(d_xmid, xmid, sizeof(double) * 3, cudaMemcpyHostToDevice);
    cudaMemcpy(d_vmid, vmid, sizeof(double) * 3, cudaMemcpyHostToDevice);

    // --- 8h. Read DEM subset, copy to device ---
    read_binary<short int>(demfile, demrsc.nlon, top, bottom, left, right, dem);
    cudaMemcpy(d_dem, dem, sizeof(short int) * nrow * ncol,
               cudaMemcpyHostToDevice);

    // --- 8i. Reproject ---
    int demBlocks = (nrow * ncol + blockSize - 1) / blockSize;
    reproject<<<demBlocks, blockSize>>>(
        d_dem, d_burstdata, d_outdata, d_tt, d_xx, d_vv, nstatvec, tmid, d_xmid,
        d_vmid, demrsc.latmax + demrsc.dlat * top,
        demrsc.lonmin + demrsc.dlon * left, demrsc.dlat, demrsc.dlon, ncol,
        rngstart, tstart, dmrg, dtaz, nrange, lines_per_burst, fmt0intp,
        fmc0intp, fmc1intp, fmc2intp, dct0intp, dcc0intp, dcc1intp, dcc2intp,
        etac[0], ks, first_valid_line[iburst], last_valid_line[iburst],
        first_valid_sample[iburst], last_valid_sample[iburst], wvl,
        nrow * ncol);
    CHECK_CUDA(cudaDeviceSynchronize());

    // --- 8j. Find non-zero rows (reuse pre-allocated d_first/d_last) ---
    int h_first = nrow; // sentinel: "no non-zero row found yet"
    int h_last = -1;
    CHECK_CUDA(
        cudaMemcpy(d_first, &h_first, sizeof(int), cudaMemcpyHostToDevice));
    CHECK_CUDA(
        cudaMemcpy(d_last, &h_last, sizeof(int), cudaMemcpyHostToDevice));

    int first_non_zero_row, last_non_zero_row;
    bool valid = find_nonzero_rows(d_outdata, nrow, ncol, d_first, d_last,
                                   first_non_zero_row, last_non_zero_row);

    if (!valid || first_non_zero_row == last_non_zero_row)
      continue;

    // --- 8k. Copy valid output rows to host and save ---
    cudaMemcpy(outdata, d_outdata + first_non_zero_row * ncol,
               sizeof(Complex) * (last_non_zero_row - first_non_zero_row) *
                   ncol,
               cudaMemcpyDeviceToHost);

    std::int32_t header[NHEADER] = {0};
    header[0] = demrsc.nlat;
    header[1] = demrsc.nlon;
    header[2] = left;
    header[3] = top + first_non_zero_row;
    header[4] = right;
    header[5] = top + last_non_zero_row;
    save_binary<Complex>(
        outdata, (last_non_zero_row - first_non_zero_row) * ncol, header,
        NHEADER, slcoutfile + "_burst_" + std::to_string(iburst) + ".gslc");
  }

  // ---- Phase 9: Free all memory ----
  free(startime);
  free(first_valid_line);
  free(last_valid_line);
  free(first_valid_sample);
  free(last_valid_sample);
  free(latlons);
  free(xmid);
  free(vmid);
  free(burst);
  free(outdata);
  free(dem);
  free(kt);
  free(eta);
  free(etaref);
  free(ka);
  free(etac);
  free(fnc);
  free(fmtime);
  free(fmt0);
  free(fmc0);
  free(fmc1);
  free(fmc2);
  free(dctime);
  free(dct0);
  free(dcc0);
  free(dcc1);
  free(dcc2);
  free(tt);
  free(xx);
  free(vv);

  cudaFree(d_tt);
  cudaFree(d_xx);
  cudaFree(d_vv);
  cudaFree(d_xmid);
  cudaFree(d_vmid);
  cudaFree(d_kt);
  cudaFree(d_eta);
  cudaFree(d_etaref);
  cudaFree(d_burstdata);
  cudaFree(d_outdata);
  cudaFree(d_dem);
  cudaFree(d_first);
  cudaFree(d_last);

  return 0;
}

// ------------------ CLI entry point ------------------

int main(int argc, char *argv[]) {
  set_stdin_binary();
  set_gpu(parse_gpu_arg(argc, argv));

  bool use_stdin = false;
  for (int i = 1; i < argc; ++i) {
    if (std::string(argv[i]) == "--stdin") {
      use_stdin = true;
    }
  }

  if (argc < 3) {
    std::cout << "Usage: geo2rdr dbname slcoutfile [slcinfile]"
              << " [--stdin] [--gpu DEVICE_ID]" << std::endl;
    std::cout << "  --stdin  Read original SLC burst data from stdin"
              << std::endl;
    return 0;
  }

  const std::string dbname = std::string(argv[1]);
  const std::string slcoutfile = std::string(argv[2]);
  std::string slcinfile = "";
  if (argc > 3) {
    std::string arg3(argv[3]);
    if (arg3 != "--stdin" && arg3 != "--gpu" && arg3 != "-g") {
      slcinfile = arg3;
    }
  }
  geo2rdr(dbname, slcoutfile, slcinfile, use_stdin);
  return 0;
}
