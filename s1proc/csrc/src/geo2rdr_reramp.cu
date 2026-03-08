#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <iostream>
#include <fstream>
#include <sqlite3.h>
#include <string>
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include "sql_mod.hpp"
#include "sario.hpp"
#include "orbit.hpp"
#include "bounds.hpp"

#ifndef M_PI
    #define M_PI 3.14159265358979323846
#endif

#ifndef SOL
    #define SOL 299792458.0
#endif

bool file_exists(const std::string& filename) {
    std::ifstream f(filename);
    return f.good();
}

__global__
void reproject(short int *dem, Complex *burstdata, double *deramp_phase,
               Complex *outdata, Complex *overlapdata,
               double *tt, double *xx, double *vv, const std::size_t nstatvec,
               const double tmid, double *xmid, double *vmid,
               const double latmax, const double lonmin, const double dlat, 
               const double dlon, const std::size_t nlon, 
               const double burst_latmin, const double burst_latmax,
               const double burst_lonmin, const double burst_lonmax,
               const double rngstart, const double tstart,
               const double dmrg, const double dtaz, const int nrange,
               const int lines_per_burst, const int first_valid_line,
               const int last_valid_line, const int first_valid_sample,
               const int last_valid_sample, const double wvl,
               const bool written, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    int row, col, intr, inta;
    double lat, lon, h;
    std::size_t idx1, idx2, idx3, idx4;
    double tline, rngpix, rgoff, azoff, fracr, fraca, llh[3], xyz[3], dr[3];
    double reramp1, reramp2, reramp, phase1, phase2, phase3, phase4, phase;
    double resx, resy, cosphase, sinphase;
    Complex cpx1, cpx2, burst1, burst2, burst3, burst4, res, zero;
    bool overlapped;
    zero.x = 0;
    zero.y = 0;
    for (std::size_t i = index; i < n; i += stride){
        row = i/nlon; 
        col = i - nlon*row;
        lat = latmax + row*dlat;
        lon = lonmin + col*dlon;
        h = dem[i];
        if (written){
            res = outdata[i];
        }else{
            res.x = 0;
            res.y = 0;
            outdata[i] = zero;
        }
        if (lat > burst_latmax || lat < burst_latmin ||
            lon > burst_lonmax || lon < burst_lonmin){
            if (!written){
                outdata[i] = zero;
                overlapdata[i] = zero; 
            }
            continue;
        }
        if (res.x != 0 || res.y !=0){
            // if res is not zero, it means this pixel has been processed
            // in a previous burst, so we need to save it to overlapdata
            overlapped = true;
        }else{
            overlapped = false;
        }
        llh[0] = lat;
        llh[1] = lon;
        llh[2] = h;
        llh2xyz(llh,xyz);
        orbitrangetime(nstatvec,tt,xx,vv,xyz,tmid,xmid,vmid,tline,dr);
        rngpix = sqrt(dr[0]*dr[0]+dr[1]*dr[1]+dr[2]*dr[2]);
        rgoff = (rngpix - rngstart)/dmrg;
        azoff = (tline - tstart)/dtaz;
        //if ((row == 426 || row == 427) && col == 1841){
        //  printf("lat:%f,lon:%f,h%f\n",lat,lon,h);
        //    printf("x:%f,y:%f,z:%f\n",xyz[0],xyz[1],xyz[2]);
        //    printf("rngpix:%f, rngstart:%f, dmrg:%f\n",rngpix,rngstart,dmrg);
        //    printf("tline:%f, tstart:%f, dtaz:%f\n",tline,tstart,dtaz);
        //    printf("rgoff: %f, azoff: %f\n",rgoff,azoff);
        //    printf("\n\n\n");
        //}
        if (rgoff >= first_valid_sample && rgoff < last_valid_sample &&
            azoff >= first_valid_line+5 && azoff < last_valid_line-5){
            intr = int(rgoff);
            fracr = rgoff - intr;
            inta = int(azoff);
            fraca = azoff - inta;
            idx1 = std::size_t(inta)*nrange+intr;
            idx2 = idx1 + 1;
            idx3 = idx1 + nrange;
            idx4 = idx3 + 1;
            burst1 = burstdata[idx1];
            burst2 = burstdata[idx2];
            burst3 = burstdata[idx3];
            burst4 = burstdata[idx4];
            cpx1.x = burst1.x*(1-fracr) + burst2.x*fracr;
            cpx1.y = burst1.y*(1-fracr) + burst2.y*fracr;
            cpx2.x = burst3.x*(1-fracr) + burst4.x*fracr;
            cpx2.y = burst3.y*(1-fracr) + burst4.y*fracr;
            phase1 = deramp_phase[idx1];
            phase2 = deramp_phase[idx2];
            phase3 = deramp_phase[idx3];
            phase4 = deramp_phase[idx4];
            reramp1 = phase1*(1-fracr) + phase2*fracr;
            reramp2 = phase3*(1-fracr) + phase4*fracr;
            resx = cpx1.x*(1-fraca) + cpx2.x*fraca;
            resy = cpx1.y*(1-fraca) + cpx2.y*fraca;
            reramp = reramp1*(1-fraca) + reramp2*fraca;
            phase = 4.0*M_PI/wvl*rngpix - reramp;
            cosphase = cos(phase);
            sinphase = sin(phase);
            res.x = resx*cosphase - resy*sinphase;
            res.y = resx*sinphase + resy*cosphase;
        }else{
            continue;
        }
        if (overlapped){
            overlapdata[i] = res;
        }
        else{
            outdata[i] = res;
        }
    }
}

int geo2rdr_reramp(const std::string &dbname,
                   const std::string &slcoutfile,
                   const std::string &overlapfile,
                   std::string &slcinfile){
    sqlite3 *db; // database that stores relevant paramters

    // open the database
    if (sqlite3_open(dbname.c_str(),&db)){
        std::cerr << "Cannot open database: " << sqlite3_errmsg(db) << std::endl;
        return 1;
    }
    // database table name
    const std::string tblname = "file";
    std::string orbfile, demfile, demrscfile;
    std::size_t nstatvec, burstsize;
    rsc demrsc;
    int azimuth_bursts, lines_per_burst, nrange, blockSize=256, numBlocks;
    int batch_lines = 3000, nbatch;
    short int *dem, *d_dem;
    double prf, wvl,slant_range_time,range_sampling_rate;
    double *startime, *tt, *xx, *vv, *xmid, *vmid;
    double *d_tt, *d_xx, *d_vv, *d_xmid, *d_vmid;
    int *first_valid_line, *last_valid_line;
    int *first_valid_sample, *last_valid_sample;
    Complex *burstdata, *d_burstdata, *outdata, *d_outdata;
    Complex *overlapdata, *d_overlapdata;
    double *deramp_phase, *d_deramp_phase; 
    bool written;

    nrange = get_parami(db,tblname,"samplesPerBurst");
    lines_per_burst = get_parami(db,tblname,"linesPerBurst");
    azimuth_bursts = get_parami(db,tblname,"azimuthBursts");
    prf = get_paramd(db,tblname,"prf");
    wvl = get_paramd(db,tblname,"wvl");
    range_sampling_rate = get_paramd(db,tblname,"rangeSamplingRate");
    slant_range_time = get_paramd(db,tblname,"slantRangeTime");
    orbfile = get_params(db,tblname,"orbinfo");
    burstsize = nrange*lines_per_burst;
    if (slcinfile.empty()){
        slcinfile = get_params(db,tblname,"slc_file");
    }
    startime = (double*)malloc(sizeof(double)*azimuth_bursts);
    first_valid_line = (int*)malloc(sizeof(int)*azimuth_bursts);
    last_valid_line = (int*)malloc(sizeof(int)*azimuth_bursts);
    first_valid_sample = (int*)malloc(sizeof(int)*azimuth_bursts);
    last_valid_sample = (int*)malloc(sizeof(int)*azimuth_bursts);
    xmid = (double*)malloc(sizeof(double)*3);
    vmid = (double*)malloc(sizeof(double)*3);
    burstdata = (Complex*)malloc(sizeof(Complex)*nrange*lines_per_burst);
    deramp_phase = (double*)malloc(sizeof(double)*nrange*lines_per_burst);

    std::cout << "number of azimuth bursts: " << azimuth_bursts << std::endl;
    for(int iburst=0; iburst<azimuth_bursts; iburst++){
        std::string azimuth_time_second_key = "azimuthTimeSeconds" + 
                                              std::to_string(iburst+1);
        std::string first_valid_line_key = "firstValidLine"+
                                           std::to_string(iburst+1);
        std::string last_valid_line_key = "lastValidLine"+
                                          std::to_string(iburst+1);
        std::string first_valid_sample_key = "firstValidSample"+
                                          std::to_string(iburst+1);
        std::string last_valid_sample_key = "lastValidSample"+
                                          std::to_string(iburst+1);
        startime[iburst] = get_paramd(db,tblname,azimuth_time_second_key);
        first_valid_line[iburst] = get_parami(db,tblname,first_valid_line_key);
        last_valid_line[iburst] = get_parami(db,tblname,last_valid_line_key);
        first_valid_sample[iburst] = get_parami(db,tblname,first_valid_sample_key);
        last_valid_sample[iburst]  = get_parami(db,tblname,last_valid_sample_key);
    }
    if (sqlite3_close(db) != SQLITE_OK) {
        std::cerr << "Can't close database: " << sqlite3_errmsg(db) << std::endl;
        return -1;
    }
    read_param_file("params",demfile,demrscfile);
    //std::cout << "dem file: " << demfile << std::endl;
    //std::cout << "rsc file: " << demrscfile << std::endl;
    demrsc = readrsc(demrscfile);
    //std::cout << "dem parameters" << std::endl;
    //std::cout << "latmax: " << demrsc.latmax << std::endl;
    //std::cout << "lonmin: " << demrsc.lonmin << std::endl;
    //std::cout << "nlat: " << demrsc.nlat << std::endl;
    //std::cout << "nlon: " << demrsc.nlon << std::endl;
    //std::cout << "dlat: " << demrsc.dlat << std::endl;
    //std::cout << "dlon: " << demrsc.dlon << std::endl;
    read_orbit_ascii(orbfile,nstatvec,&tt,&xx,&vv);
    //std::cout << "orbfile: " << orbfile << std::endl;
    outdata = (Complex*)malloc(sizeof(Complex)*demrsc.nlon*batch_lines);
    overlapdata = (Complex*)malloc(sizeof(Complex)*demrsc.nlon*batch_lines);
    dem = (short int*)malloc(sizeof(short int)*demrsc.nlon*batch_lines);
    cudaMalloc((void**)&d_tt,sizeof(double)*nstatvec);
    cudaMalloc((void**)&d_xx,sizeof(double)*nstatvec*3);
    cudaMalloc((void**)&d_vv,sizeof(double)*nstatvec*3);
    cudaMalloc((void**)&d_xmid,sizeof(double)*3);
    cudaMalloc((void**)&d_vmid,sizeof(double)*3);
    cudaMalloc((void**)&d_dem,sizeof(short int)*demrsc.nlon*batch_lines);
    cudaMalloc((void**)&d_outdata,sizeof(Complex)*demrsc.nlon*batch_lines);
    cudaMalloc((void**)&d_overlapdata,sizeof(Complex)*demrsc.nlon*batch_lines);
    cudaMalloc((void**)&d_burstdata,sizeof(Complex)*nrange*lines_per_burst);
    cudaMalloc((void**)&d_deramp_phase,sizeof(double)*nrange*lines_per_burst);

    cudaMemcpy(d_tt,tt,sizeof(double)*nstatvec,cudaMemcpyHostToDevice);
    cudaMemcpy(d_xx,xx,sizeof(double)*nstatvec*3,cudaMemcpyHostToDevice);
    cudaMemcpy(d_vv,vv,sizeof(double)*nstatvec*3,cudaMemcpyHostToDevice);
    nbatch = (demrsc.nlat + batch_lines-1)/batch_lines;
    for (int ibatch = 0; ibatch < nbatch; ++ibatch){
        // calculate the first and the last lines of current batch of output file
        std::size_t line_start = ibatch * batch_lines;
        std::size_t line_end = line_start + batch_lines;
        line_end = line_end < demrsc.nlat ? line_end : demrsc.nlat;
        std::size_t nlines = line_end - line_start;
        read_binary<short int>(demfile,line_start*demrsc.nlon,
                nlines*demrsc.nlon,dem);
        cudaMemcpy(d_dem,dem,sizeof(short int)*nlines*demrsc.nlon,
                cudaMemcpyHostToDevice);
        numBlocks = (nlines*demrsc.nlon+blockSize-1)/blockSize;
        std::cout << "Batch " << ibatch << ", nlines: " << nlines << std::endl;
        
        for (int iburst=0; iburst<azimuth_bursts; ++iburst){
            double tstart, dtaz, tend, tmid;
            double rngstart, dmrg, rngend;
            double latlons[4];
            if (iburst == 0){
                written = false;
            }else{
                written = true;
            }
            tstart = startime[iburst];
            dtaz = 1./prf;
            tend = tstart + (lines_per_burst-1)*dtaz;
            tmid = (tstart + tend)*0.5;
            intp_orbit(nstatvec, tt,xx,vv,tmid,xmid,vmid);
            rngstart = slant_range_time*SOL/2.0;
            dmrg = SOL/2.0/range_sampling_rate;
            rngend = rngstart + (nrange-1)*dmrg;

            read_binary<Complex>(slcinfile,iburst*burstsize,burstsize,
                    burstdata);
            read_binary<double>("deramp_phase",iburst*burstsize,burstsize,
                    deramp_phase);

            // calculate lat/lon bnoudaries of current burst
            bounds(tstart,tend,rngstart,rngend,tt,xx,vv,nstatvec,latlons,"RIGHT");
            cudaMemcpy(d_burstdata,burstdata,sizeof(Complex)*burstsize,
                    cudaMemcpyHostToDevice);
            cudaMemcpy(d_deramp_phase,deramp_phase,sizeof(double)*burstsize,
                    cudaMemcpyHostToDevice);
            cudaMemcpy(d_xmid,xmid,sizeof(double)*3,cudaMemcpyHostToDevice);
            cudaMemcpy(d_vmid,vmid,sizeof(double)*3,cudaMemcpyHostToDevice);
            
            // reprojection
            reproject<<<numBlocks,blockSize>>>(d_dem,d_burstdata,d_deramp_phase,
            d_outdata,d_overlapdata,d_tt,d_xx,d_vv,nstatvec,tmid,d_xmid,d_vmid, 
            demrsc.latmax+demrsc.dlat*line_start,demrsc.lonmin,
            demrsc.dlat,demrsc.dlon,demrsc.nlon,
            latlons[0],latlons[1],latlons[2],latlons[3],rngstart,tstart,dmrg,dtaz,
            nrange, lines_per_burst, first_valid_line[iburst],
            last_valid_line[iburst], first_valid_sample[iburst],
            last_valid_sample[iburst], wvl, written, nlines*demrsc.nlon);
            cudaDeviceSynchronize();
        }
        cudaMemcpy(outdata,d_outdata,sizeof(Complex)*demrsc.nlon*nlines,
                cudaMemcpyDeviceToHost);
        cudaMemcpy(overlapdata,d_overlapdata,sizeof(Complex)*demrsc.nlon*nlines,
                cudaMemcpyDeviceToHost);
        if (ibatch == 0){
            save_binary<Complex>(outdata,false,demrsc.nlon*nlines,slcoutfile);
            save_binary<Complex>(overlapdata,false,demrsc.nlon*nlines,
                    overlapfile);
        }else{
            save_binary<Complex>(outdata,true,demrsc.nlon*nlines,slcoutfile);
            save_binary<Complex>(overlapdata,true,demrsc.nlon*nlines,
                    overlapfile);
        }
    }

    free(startime);
    free(first_valid_line);
    free(last_valid_line);
    free(first_valid_sample);
    free(last_valid_sample);
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

int main(int argc, char *argv[]){
    if (argc<4){
        std::cout << "Usage: geo2rdr_reramp dbname slcoutfile overlapfile " <<
            "[slcinfile]" << std::endl;
        return 0;
    }
    const std::string dbname = std::string(argv[1]);
    const std::string slcoutfile = std::string(argv[2]);
    const std::string overlapfile = std::string(argv[3]);
    std::string slcinfile = "";
    if (argc>4){
        slcinfile = std::string(argv[4]);
    }
    geo2rdr_reramp(dbname,slcoutfile,overlapfile,slcinfile);
    return 0;
}
