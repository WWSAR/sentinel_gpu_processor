#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <iostream>
#include <sqlite3.h>
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include "sql_mod.hpp"
#include "sario.hpp"
#include "orbit.hpp"

#ifndef M_PI
    #define M_PI 3.14159265358979323846
#endif

int closest_sample(const double *t,
                   const double t0,
                   const int n){
    int idx = 0;
    double mindist = 1e10, dist;
    for (int i=0; i<n; ++i){
       dist = fabs(t[i]-t0);
       if (mindist > dist){
            mindist = dist;
            idx = i;
       }
    }
    return idx;
}

__global__ void deramp_burst(
    double *kt, double *eta, double *etaref,
    Complex *burst, double *deramp_phase,
    const std::size_t nrange,
    const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    unsigned int row, col;
    double phase,etadiff;
    Complex a,b;
    for (std::size_t i=index; i<n; i+=stride){
        a.x = burst[i].x;
        a.y = burst[i].y;
        row = i/nrange;
        col = i - row*nrange;
        etadiff = eta[row]-etaref[col];
        phase = -M_PI*kt[col]*etadiff*etadiff;
        deramp_phase[i] = phase;
        b.x = cos(phase);
        b.y = sin(phase);
        burst[i].x = a.x*b.x-a.y*b.y;
        burst[i].y = a.x*b.y+a.y*b.x;
    }
}

int deramp(const std::string &dbname,
           const std::string &burstfile,
           const std::string &deramp_phase_file){
    sqlite3 *db; // database that stores relevant paramters

    // open the database
    if (sqlite3_open(dbname.c_str(),&db)){
        std::cerr << "Cannot open database: " << sqlite3_errmsg(db) << std::endl;
        return 1;
    }
    
    unsigned int blockSize = 256,numBlocks;
    int nrange, lines_per_burst,azimuth_bursts, npolyfm, npolydc;
    std::size_t npolyorb;
    double azimuth_steering_rate, azimuth_time_interval, range_sampling_rate;
    double radar_frequency,slant_range_time;
    const std::string tblname = "file";
    std::string fmratefile, dcfile, orbfile;
    std::string burstoutfile;
    double *startime; 
    int *first_valid_line, *last_valid_line;
    double *eta, *ka, *kt, *etac, *fnc, *etaref;
    double *fmtime, *fmt0, *fmc0, *fmc1, *fmc2;
    double *dctime, *dct0, *dcc0, *dcc1, *dcc2;
    double *tt, *xx, *vv;
    double *d_kt, *d_eta, *d_etaref;
    double *deramp_phase, *d_deramp_phase;
    Complex *burst, *d_burst;

    burstoutfile = burstfile+".deramp";
    nrange = get_parami(db,tblname,"samplesPerBurst");
    lines_per_burst = get_parami(db,tblname,"linesPerBurst");
    azimuth_bursts = get_parami(db,tblname,"azimuthBursts");
    azimuth_steering_rate = get_paramd(db,tblname,"azimuthSteeringRate");
    azimuth_time_interval = get_paramd(db,tblname,"azimuthTimeInterval");
    range_sampling_rate = get_paramd(db,tblname,"rangeSamplingRate");
    radar_frequency = get_paramd(db,tblname,"radarFrequency");
    slant_range_time = get_paramd(db,tblname,"slantRangeTime");
    fmratefile = get_params(db,tblname,"fmrateinfo");
    dcfile = get_params(db,tblname,"dcinfo");
    orbfile = get_params(db,tblname,"orbinfo");

    startime = (double*)malloc(sizeof(double)*azimuth_bursts);
    first_valid_line = (int*)malloc(sizeof(int)*azimuth_bursts);
    last_valid_line = (int*)malloc(sizeof(int)*azimuth_bursts);
    eta = (double*)malloc(sizeof(double)*lines_per_burst);
    ka = (double*)malloc(sizeof(double)*nrange);
    kt = (double*)malloc(sizeof(double)*nrange);
    etac = (double*)malloc(sizeof(double)*nrange);
    fnc = (double*)malloc(sizeof(double)*nrange);
    etaref = (double*)malloc(sizeof(double)*nrange);

    numBlocks = (nrange*lines_per_burst+blockSize-1)/blockSize;
    burst = (Complex*)malloc(sizeof(Complex)*nrange*lines_per_burst);
    deramp_phase = (double*)malloc(sizeof(double)*nrange*lines_per_burst);
    cudaMalloc((void**)&d_burst,sizeof(Complex)*nrange*lines_per_burst);
    cudaMalloc((void**)&d_deramp_phase,sizeof(double)*nrange*lines_per_burst);
    cudaMalloc((void**)&d_kt,sizeof(double)*nrange);
    cudaMalloc((void**)&d_eta,sizeof(double)*lines_per_burst);
    cudaMalloc((void**)&d_etaref,sizeof(double)*nrange);

    for(int iburst=0; iburst<azimuth_bursts; iburst++){
        std::string azimuth_time_second_key = "azimuthTimeSeconds" + 
                                              std::to_string(iburst+1);
        std::string first_valid_line_key = "firstValidLine"+
                                           std::to_string(iburst+1);
        std::string last_valid_line_key = "lastValidLine"+
                                          std::to_string(iburst+1);
        startime[iburst] = get_paramd(db,tblname,azimuth_time_second_key);
        first_valid_line[iburst] = get_parami(db,tblname,first_valid_line_key);
        last_valid_line[iburst] = get_parami(db,tblname,last_valid_line_key);
    }
    if (sqlite3_close(db) != SQLITE_OK) {
        std::cerr << "Can't close database: " << sqlite3_errmsg(db) << std::endl;
        return -1;
    }
    std::cout << "reading fmrate from " << fmratefile << std::endl;
    read_polynomials(fmratefile,npolyfm,&fmtime,&fmt0,&fmc0,&fmc1,&fmc2);
    std::cout << "reading fdc from " << dcfile << std::endl;
    read_polynomials(dcfile,npolydc,&dctime,&dct0,&dcc0,&dcc1,&dcc2);
    std::cout << "reading orbit from " << orbfile << std::endl;
    read_orbit_ascii(orbfile,npolyorb,&tt,&xx,&vv);
    for (int iburst=0; iburst<azimuth_bursts; iburst++){
        std::cout << "processing burst " << (iburst+1) << "/" << azimuth_bursts << std::endl;
        double timecenterseconds;
        double xpoint[3], vpoint[3], vs, ks;
        double dcc0intp, dcc1intp, dcc2intp, dct0intp;
        double fmc0intp, fmc1intp, fmc2intp, fmt0intp;
        double trange, dt;
        double frac;
        int idx;
        timecenterseconds = startime[iburst] + lines_per_burst*azimuth_time_interval/2;
        intp_orbit(npolyorb,tt,xx,vv,timecenterseconds,xpoint,vpoint);
        vs = sqrt(vpoint[0]*vpoint[0]+vpoint[1]*vpoint[1]+vpoint[2]*vpoint[2]);
        ks = 2.0*vs*radar_frequency/299792458.0*azimuth_steering_rate*M_PI/180.0;
        idx = closest_sample(fmtime, timecenterseconds, npolyfm);
        if (fmtime[idx] < timecenterseconds){
            frac = (timecenterseconds - fmtime[idx]) / (fmtime[idx+1] - fmtime[idx]);
            fmc0intp = fmc0[idx] + frac * (fmc0[idx+1] - fmc0[idx]);
            fmc1intp = fmc1[idx] + frac * (fmc1[idx+1] - fmc1[idx]);
            fmc2intp = fmc2[idx] + frac * (fmc2[idx+1] - fmc2[idx]);
            fmt0intp = fmt0[idx] + frac * (fmt0[idx+1] - fmt0[idx]);
        }else{
            frac = (timecenterseconds - fmtime[idx-1]) / (fmtime[idx] - fmtime[idx-1]);
            fmc0intp = fmc0[idx-1] + frac * (fmc0[idx] - fmc0[idx-1]);
            fmc1intp = fmc1[idx-1] + frac * (fmc1[idx] - fmc1[idx-1]);
            fmc2intp = fmc2[idx-1] + frac * (fmc2[idx] - fmc2[idx-1]);
            fmt0intp = fmt0[idx-1] + frac * (fmt0[idx] - fmt0[idx-1]);
        }
        //fmc0intp = fmc0[idx];
        //fmc1intp = fmc1[idx];
        //fmc2intp = fmc2[idx];
        //fmt0intp = fmt0[idx];
        idx = closest_sample(dctime, timecenterseconds, npolydc);
        if (dctime[idx] < timecenterseconds){
            frac = (timecenterseconds - dctime[idx]) / (dctime[idx+1] - dctime[idx]);
            dcc0intp = dcc0[idx] + frac * (dcc0[idx+1] - dcc0[idx]);
            dcc1intp = dcc1[idx] + frac * (dcc1[idx+1] - dcc1[idx]);
            dcc2intp = dcc2[idx] + frac * (dcc2[idx+1] - dcc2[idx]);
            dct0intp = dct0[idx] + frac * (dct0[idx+1] - dct0[idx]);
        }else{
            frac = (timecenterseconds - dctime[idx-1]) / (dctime[idx] - dctime[idx-1]);
            dcc0intp = dcc0[idx-1] + frac * (dcc0[idx] - dcc0[idx-1]);
            dcc1intp = dcc1[idx-1] + frac * (dcc1[idx] - dcc1[idx-1]);
            dcc2intp = dcc2[idx-1] + frac * (dcc2[idx] - dcc2[idx-1]);
            dct0intp = dct0[idx-1] + frac * (dct0[idx] - dct0[idx-1]);
        }
        //dcc0intp = dcc0[idx];
        //dcc1intp = dcc1[idx];
        //dcc2intp = dcc2[idx];
        //dct0intp = dct0[idx];
        for(int i = 0; i<lines_per_burst; ++i){
            eta[i] = -lines_per_burst*azimuth_time_interval/2.+i*azimuth_time_interval;
        }
        for(int i = 0; i<nrange; ++i){
            trange = slant_range_time + i/range_sampling_rate;
            dt = trange - fmt0intp;
            ka[i] = fmc0intp + fmc1intp*dt + fmc2intp*dt*dt;
            dt = trange - dct0intp;
            fnc[i] = dcc0intp + dcc1intp*dt + dcc2intp*dt*dt;
            kt[i]  = ka[i]*ks/(ka[i]-ks);
            etac[i] = -fnc[i]/ka[i];
            etaref[i] = etac[i] - etac[0];
        }
        read_binary<Complex>(burstfile,iburst*nrange*lines_per_burst,
                nrange*lines_per_burst,burst);
        cudaMemcpy(d_burst,burst,sizeof(Complex)*nrange*lines_per_burst,
                   cudaMemcpyHostToDevice);
        cudaMemcpy(d_kt,kt,sizeof(double)*nrange,cudaMemcpyHostToDevice);
        cudaMemcpy(d_etaref,etaref,sizeof(double)*nrange,
                   cudaMemcpyHostToDevice);
        cudaMemcpy(d_eta,eta,sizeof(double)*lines_per_burst,
                   cudaMemcpyHostToDevice);
        deramp_burst<<<numBlocks,blockSize>>>(d_kt,d_eta,d_etaref,d_burst,
                                             d_deramp_phase,nrange,
                                             nrange*lines_per_burst);
        cudaDeviceSynchronize();
        cudaMemcpy(burst,d_burst,sizeof(Complex)*nrange*lines_per_burst,
                   cudaMemcpyDeviceToHost);
        cudaMemcpy(deramp_phase,d_deramp_phase,
                   sizeof(double)*nrange*lines_per_burst,
                   cudaMemcpyDeviceToHost);
        if (iburst == 0){
            save_binary<Complex>(burst,false,nrange*lines_per_burst,
                    burstoutfile);
            save_binary<double>(deramp_phase,false,nrange*lines_per_burst,
                    deramp_phase_file);
        }else{
            save_binary<Complex>(burst,true,nrange*lines_per_burst,
                    burstoutfile);
            save_binary<double>(deramp_phase,true,nrange*lines_per_burst,
                    deramp_phase_file);
        }
    }

    free(startime);
    free(first_valid_line);
    free(last_valid_line);
    free(eta);
    free(ka);
    free(kt);
    free(etac);
    free(fnc);
    free(etaref);
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
    free(burst);
    free(deramp_phase);
    cudaFree(d_burst);
    cudaFree(d_deramp_phase);
    cudaFree(d_eta);
    cudaFree(d_etaref);
    cudaFree(d_kt);
    return 0;
}

int main(int argc, char *argv[]){
    if (argc<4){
        std::cout << "Usage: deramp_burst dbname burstfile deramp_phase_file"
            << std::endl;
        return 0;
    }
    const std::string dbname = std::string(argv[1]);
    const std::string burstfile = std::string(argv[2]);
    const std::string deramp_phase_file = std::string(argv[3]);
    deramp(dbname,burstfile,deramp_phase_file);
    return 0;
}
