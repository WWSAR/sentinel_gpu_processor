#include <cstdlib>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>
#include <iostream>
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include "sario.hpp"

#ifndef M_PI
    #define M_PI 3.14159265358979323846
#endif

#ifndef SOL
    #define SOL 299792458.0
#endif

// ----------------- Utility -----------------
#define CHECK_CUDA(x) do { cudaError_t err = (x); \
if (err != cudaSuccess) { \
    std::cerr << "CUDA error " << cudaGetErrorString(err) \
              << " at " << __FILE__ << ":" << __LINE__ << std::endl; \
    exit(1); \
} } while(0)

int multilook(const std::string &slcfile,
              const std::string &outfile,
              const int rowlook,
              const int collook,
              int batch_lines = 3000){
    // delcaration
    std::int32_t in_header[NHEADER], out_header[NHEADER];
    Complex *slc, *d_slc;
    float *d_amp, *d_amp_collook, *d_amp_look, *amp_look;
    int blockSize = 256, batch_sm;
    int nbatch, numBlocks, ncol_sm;
    // parameters of raw image
    int left, top, right, bottom, nrow, ncol;
    // end of declaration

    batch_sm = batch_lines/rowlook;
    batch_lines = batch_sm*rowlook;
    // read the header of the raw slc image
    read_binary<std::int32_t>(slcfile, NHEADER, in_header);
    // read the parameters of the raw slc image
    left = (in_header[2] + collook - 1) / collook * collook;
    top = (in_header[3] + rowlook - 1) / rowlook * rowlook;
    right = in_header[4];
    bottom = in_header[5];
    nrow = bottom - top;
    ncol = right - left;
    // fill out_header
    out_header[0] = int(in_header[0] / rowlook);
    out_header[1] = int(in_header[1] / collook);
    out_header[2] = left / collook;
    out_header[3] = top / rowlook;
    out_header[4] = right / collook;
    out_header[5] = bottom / rowlook;

    nbatch = (nrow + batch_lines-1) / batch_lines;
    ncol_sm = ncol / collook;
    slc = (Complex *)malloc(sizeof(Complex)*ncol*batch_lines);
    amp_look = (float *)malloc(sizeof(Complex)*batch_sm*ncol_sm);
    cudaMalloc((void**)&d_slc,sizeof(Complex)*ncol*batch_lines);
    cudaMalloc((void**)&d_amp,sizeof(float)*ncol*batch_lines);
    cudaMalloc((void**)&d_amp_collook,sizeof(float)*batch_lines*ncol_sm);
    cudaMalloc((void**)&d_amp_look,sizeof(float)*batch_sm*ncol_sm);

    for (int ibatch = 0; ibatch < nbatch; ++ibatch){
        std::size_t line_start = ibatch * batch_lines;
        std::size_t line_end = line_start + batch_lines;
        line_end = line_end < nrow ? line_end : nrow;
        std::size_t nlines = line_end - line_start;
        read_and_resample(slcfile, slc, left, top, right, bottom,
                line_start, line_end);
        CHECK_CUDA(cudaMemcpy(d_slc,slc,sizeof(Complex)*nlines*ncol,
                   cudaMemcpyHostToDevice));
        numBlocks = (nlines*ncol+blockSize-1)/blockSize;
        point_power<<<numBlocks, blockSize>>>(d_slc, d_amp, nlines*ncol);
        CHECK_CUDA(cudaDeviceSynchronize());
        numBlocks = (nlines*ncol_sm+blockSize-1)/blockSize;
        // column look
        col_look<<<numBlocks,blockSize>>>(
                d_amp, d_amp_collook, collook, ncol, nlines*ncol_sm);
        CHECK_CUDA(cudaDeviceSynchronize());
        // row look
        numBlocks = (nlines/rowlook*ncol_sm+blockSize-1)/blockSize;
        row_look<<<numBlocks,blockSize>>>(
                d_amp_collook, d_amp_look, rowlook, ncol_sm,
                nlines / rowlook * ncol_sm);
        point_sqrt<<<numBlocks, blockSize>>>(
                d_amp_look, d_amp_look, nlines / rowlook * ncol_sm);
        CHECK_CUDA(cudaDeviceSynchronize());
        CHECK_CUDA(cudaMemcpy(
                    amp_look,d_amp_look,sizeof(float)*nlines/rowlook*ncol_sm,
                    cudaMemcpyDeviceToHost));
        if(ibatch==0){
            save_binary<float>(amp_look,nlines/rowlook*ncol_sm,out_header,
                    NHEADER,outfile);
        }else{
            save_binary<float>(amp_look,true,nlines/rowlook*ncol_sm,
                    outfile);
        }
    }

    free(slc);
    free(amp_look);
    cudaFree(d_slc);
    cudaFree(d_amp);
    cudaFree(d_amp_collook);
    cudaFree(d_amp_look);
    return 0;
}

int main(int argc, char *argv[]){
    if (argc<5){
        std::cout << "Usage: multilook slcfile outfile rowlook collook " <<
            "[batch_lines]" << std::endl;
        return 0;
    }
    const std::string slcfile = std::string(argv[1]);
    const std::string outfile = std::string(argv[2]);
    const int rowlook = std::stoi(argv[3]);
    const int collook = std::stoi(argv[4]);
    int batch_lines = 3000;
    if (argc > 5){
        batch_lines = std::stoi(argv[5]);
    }
    return multilook(slcfile,outfile,rowlook,collook,batch_lines);
}
