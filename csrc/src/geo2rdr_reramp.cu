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

bool file_exists(const std::string &filename) {
  std::ifstream f(filename);
  return f.good();
}

__global__ void
reproject(const short int *dem, const Complex *burstdata,
          const double *deramp_phase, Complex *__restrict__ outdata, double *tt,
          double *xx, double *vv, const std::size_t nstatvec, const double tmid,
          double *xmid, double *vmid, const double latmax, const double lonmin,
          const double dlat, const double dlon, const std::size_t nlon,
          const double rngstart, const double tstart, const double dmrg,
          const double dtaz, const int nrange, const int lines_per_burst,
          const int first_valid_line, const int last_valid_line,
          const int first_valid_sample, const int last_valid_sample,
          const double wvl, const std::size_t n) {
  std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
  std::size_t stride = blockDim.x * gridDim.x;
  int row, col, intr, inta;
  double lat, lon, h;
  std::size_t idx1, idx2, idx3, idx4;
  double tline, rngpix, rgoff, azoff, fracr, fraca, llh[3], xyz[3], dr[3];
  double reramp1, reramp2, reramp, phase1, phase2, phase3, phase4, phase;
  double resx, resy, cosphase, sinphase;
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
    // if ((row == 426 || row == 427) && col == 1841){
    //   printf("lat:%f,lon:%f,h%f\n",lat,lon,h);
    //     printf("x:%f,y:%f,z:%f\n",xyz[0],xyz[1],xyz[2]);
    //     printf("rngpix:%f, rngstart:%f, dmrg:%f\n",rngpix,rngstart,dmrg);
    //     printf("tline:%f, tstart:%f, dtaz:%f\n",tline,tstart,dtaz);
    //     printf("rgoff: %f, azoff: %f\n",rgoff,azoff);
    //     printf("\n\n\n");
    // }
    if (rgoff >= first_valid_sample && rgoff < last_valid_sample &&
        azoff >= first_valid_line + 5 && azoff < last_valid_line - 5) {
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
      phase1 = deramp_phase[idx1];
      phase2 = deramp_phase[idx2];
      phase3 = deramp_phase[idx3];
      phase4 = deramp_phase[idx4];
      reramp1 = phase1 * (1 - fracr) + phase2 * fracr;
      reramp2 = phase3 * (1 - fracr) + phase4 * fracr;
      resx = cpx1.x * (1 - fraca) + cpx2.x * fraca;
      resy = cpx1.y * (1 - fraca) + cpx2.y * fraca;
      reramp = reramp1 * (1 - fraca) + reramp2 * fraca;
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

int geo2rdr_reramp(const std::string &dbname,
                   const std::string &deramp_phase_file,
                   const std::string &slcoutfile, std::string &slcinfile) {
  sqlite3 *db; // database that stores relevant paramters

  // open the database
  if (sqlite3_open(dbname.c_str(), &db)) {
    std::cerr << "Cannot open database: " << sqlite3_errmsg(db) << std::endl;
    return 1;
  }
  // database table name
  const std::string tblname = "file";
  // filenames
  std::string orbfile, demfile, rscfile;
  std::size_t nstatvec, burstsize, buffer_size;
  int azimuth_bursts, lines_per_burst, nrange, blockSize = 256, numBlocks;
  int nrow_buffer, ncol_buffer;
  double prf, wvl, slant_range_time, range_sampling_rate, hmin, hmax;
  rsc demrsc;

  std::int32_t header[NHEADER] = {0};
  short int *dem, *d_dem;
  double *latlons;
  double *startime, *tt, *xx, *vv, *xmid, *vmid;
  double *d_tt, *d_xx, *d_vv, *d_xmid, *d_vmid;
  int *first_valid_line, *last_valid_line;
  int *first_valid_sample, *last_valid_sample;
  Complex *burstdata, *d_burstdata, *outdata, *d_outdata;
  double *deramp_phase, *d_deramp_phase;

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
  orbfile = get_params(db, tblname, "orbinfo");
  burstsize = nrange * lines_per_burst;
  if (slcinfile.empty()) {
    slcinfile = get_params(db, tblname, "slc_file");
  }
  startime = (double *)malloc(sizeof(double) * azimuth_bursts);
  first_valid_line = (int *)malloc(sizeof(int) * azimuth_bursts);
  last_valid_line = (int *)malloc(sizeof(int) * azimuth_bursts);
  first_valid_sample = (int *)malloc(sizeof(int) * azimuth_bursts);
  last_valid_sample = (int *)malloc(sizeof(int) * azimuth_bursts);
  xmid = (double *)malloc(sizeof(double) * 3);
  vmid = (double *)malloc(sizeof(double) * 3);
  burstdata = (Complex *)malloc(sizeof(Complex) * nrange * lines_per_burst);
  deramp_phase = (double *)malloc(sizeof(double) * nrange * lines_per_burst);
  latlons = (double *)malloc(sizeof(double) * azimuth_bursts * 4);
  read_orbit_ascii(orbfile, nstatvec, &tt, &xx, &vv);

  std::cout << "number of azimuth bursts: " << azimuth_bursts << std::endl;
  for (int iburst = 0; iburst < azimuth_bursts; iburst++) {
    std::string azimuth_time_second_key =
        "azimuthTimeSeconds" + std::to_string(iburst + 1);
    std::string first_valid_line_key =
        "firstValidLine" + std::to_string(iburst + 1);
    std::string last_valid_line_key =
        "lastValidLine" + std::to_string(iburst + 1);
    std::string first_valid_sample_key =
        "firstValidSample" + std::to_string(iburst + 1);
    std::string last_valid_sample_key =
        "lastValidSample" + std::to_string(iburst + 1);
    startime[iburst] = get_paramd(db, tblname, azimuth_time_second_key);
    first_valid_line[iburst] = get_parami(db, tblname, first_valid_line_key);
    last_valid_line[iburst] = get_parami(db, tblname, last_valid_line_key);
    first_valid_sample[iburst] =
        get_parami(db, tblname, first_valid_sample_key);
    last_valid_sample[iburst] = get_parami(db, tblname, last_valid_sample_key);

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
    // calculate lat/lon boundaries of current burst
    bounds(tstart, tend, rngstart, rngend, hmin, hmax, tt, xx, vv, nstatvec,
           latlons + iburst * 4, "RIGHT");
  }

  if (sqlite3_close(db) != SQLITE_OK) {
    std::cerr << "Can't close database: " << sqlite3_errmsg(db) << std::endl;
    return -1;
  }

  demrsc = readrsc(rscfile);

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
  // std::cout << "number of buffer rows: " << nrow_buffer << std::endl;
  // std::cout << "number of buffer columns: " << ncol_buffer << std::endl;
  buffer_size = sizeof(Complex) * nrow_buffer * ncol_buffer;

  outdata = (Complex *)malloc(buffer_size);
  dem = (short int *)malloc(sizeof(short int) * nrow_buffer * ncol_buffer);
  cudaMalloc((void **)&d_tt, sizeof(double) * nstatvec);
  cudaMalloc((void **)&d_xx, sizeof(double) * nstatvec * 3);
  cudaMalloc((void **)&d_vv, sizeof(double) * nstatvec * 3);
  cudaMalloc((void **)&d_xmid, sizeof(double) * 3);
  cudaMalloc((void **)&d_vmid, sizeof(double) * 3);
  cudaMalloc((void **)&d_dem, sizeof(short int) * nrow_buffer * ncol_buffer);
  cudaMalloc((void **)&d_outdata, buffer_size);
  cudaMalloc((void **)&d_burstdata, sizeof(Complex) * nrange * lines_per_burst);
  cudaMalloc((void **)&d_deramp_phase,
             sizeof(double) * nrange * lines_per_burst);

  cudaMemcpy(d_tt, tt, sizeof(double) * nstatvec, cudaMemcpyHostToDevice);
  cudaMemcpy(d_xx, xx, sizeof(double) * nstatvec * 3, cudaMemcpyHostToDevice);
  cudaMemcpy(d_vv, vv, sizeof(double) * nstatvec * 3, cudaMemcpyHostToDevice);
  for (int iburst = 0; iburst < azimuth_bursts; ++iburst) {
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
    if (left >= right || top >= bottom) {
      continue;
    }
    nrow = bottom - top;
    ncol = right - left;
    // populate header
    header[0] = demrsc.nlat;
    header[1] = demrsc.nlon;
    header[2] = left;
    header[3] = top;
    header[4] = right;
    header[5] = bottom;

    read_binary<short int>(demfile, demrsc.nlon, top, bottom, left, right, dem);
    cudaMemcpy(d_dem, dem, sizeof(short int) * nrow * ncol,
               cudaMemcpyHostToDevice);
    numBlocks = (nrow * ncol + blockSize - 1) / blockSize;
    std::cout << "Burst " << iburst << ", nrow: " << nrow << std::endl;

    double tstart, dtaz, tend, tmid;
    double rngstart, dmrg;
    tstart = startime[iburst];
    dtaz = 1. / prf;
    tend = tstart + (lines_per_burst - 1) * dtaz;
    tmid = (tstart + tend) * 0.5;
    rngstart = slant_range_time * SOL / 2.0;
    dmrg = SOL / 2.0 / range_sampling_rate;
    intp_orbit(nstatvec, tt, xx, vv, tmid, xmid, vmid);
    read_binary<Complex>(slcinfile, iburst * burstsize, burstsize, burstdata);
    read_binary<double>(deramp_phase_file, iburst * burstsize, burstsize,
                        deramp_phase);
    cudaMemcpy(d_burstdata, burstdata, sizeof(Complex) * burstsize,
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_deramp_phase, deramp_phase, sizeof(double) * burstsize,
               cudaMemcpyHostToDevice);
    cudaMemcpy(d_xmid, xmid, sizeof(double) * 3, cudaMemcpyHostToDevice);
    cudaMemcpy(d_vmid, vmid, sizeof(double) * 3, cudaMemcpyHostToDevice);

    // reprojection
    reproject<<<numBlocks, blockSize>>>(
        d_dem, d_burstdata, d_deramp_phase, d_outdata, d_tt, d_xx, d_vv,
        nstatvec, tmid, d_xmid, d_vmid, demrsc.latmax + demrsc.dlat * top,
        demrsc.lonmin + demrsc.dlon * left, demrsc.dlat, demrsc.dlon, ncol,
        rngstart, tstart, dmrg, dtaz, nrange, lines_per_burst,
        first_valid_line[iburst], last_valid_line[iburst],
        first_valid_sample[iburst], last_valid_sample[iburst], wvl,
        nrow * ncol);

    CHECK_CUDA(cudaDeviceSynchronize());

    cudaMemcpy(outdata, d_outdata, sizeof(Complex) * nrow * ncol,
               cudaMemcpyDeviceToHost);
    save_binary<Complex>(outdata, nrow * ncol, header, NHEADER,
                         slcoutfile + "_burst_" + std::to_string(iburst) +
                             ".gslc");
  }

  free(startime);
  free(first_valid_line);
  free(last_valid_line);
  free(first_valid_sample);
  free(last_valid_sample);
  free(latlons);
  free(burstdata);
  free(deramp_phase);
  free(dem);
  free(tt);
  free(xx);
  free(vv);
  free(xmid);
  free(vmid);
  cudaFree(d_burstdata);
  cudaFree(d_deramp_phase);
  cudaFree(d_dem);
  cudaFree(d_tt);
  cudaFree(d_xx);
  cudaFree(d_vv);
  cudaFree(d_xmid);
  cudaFree(d_vmid);
  return 0;
}

int main(int argc, char *argv[]) {
  set_gpu(parse_gpu_arg(argc, argv));
  if (argc < 4) {
    std::cout << "Usage: geo2rdr_reramp dbname deramp_phase_file "
              << "slcoutfile [slcinfile] [--gpu DEVICE_ID]" << std::endl;
    return 0;
  }
  const std::string dbname = std::string(argv[1]);
  const std::string deramp_phase_file = std::string(argv[2]);
  const std::string slcoutfile = std::string(argv[3]);
  std::string slcinfile = "";
  if (argc > 4) {
    slcinfile = std::string(argv[4]);
  }
  geo2rdr_reramp(dbname, deramp_phase_file, slcoutfile, slcinfile);
  return 0;
}
