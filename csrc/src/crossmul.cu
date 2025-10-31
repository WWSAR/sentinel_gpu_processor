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

__global__
void conj_mul(Complex *a, Complex *b, Complex *c, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex ai, bi, s;
    for (std::size_t i = index; i < n; i += stride){ 
       ai = a[i];
       bi = b[i];
       s.x = ai.x*bi.x+ai.y*bi.y;
       s.y = -ai.y*bi.x+ai.x*bi.y;
       c[i] = s;
    }
}

__global__
void cpx_col_look(Complex *a, Complex *b, const int collook,
                  const std::size_t ncol, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    std::size_t ncol_sm = ncol/collook, row, col, idx0;
    Complex temp, sum;
    for (std::size_t i = index; i < n; i += stride){ 
       sum.x = 0;
       sum.y = 0;
       row = i/ncol_sm;
       col = i - row*ncol_sm;
       idx0 = row*ncol+col*collook;
       for (std::size_t j = 0; j < collook; j++) {
            temp = a[idx0+j];
            //if(i==1999*ncol_sm+ncol_sm/2){
            //    printf("ncol_sm:%llu,j:%llu,temp.x=%f,temp.y=%f\n",ncol_sm,j,temp.x,temp.y);
            //}
            sum.x = sum.x + temp.x;
            sum.y = sum.y + temp.y;
       }
       //if(i==200000){
       // printf("collook:%d\n",collook);
       // printf("idx = %u, sum.x = %f, sum.y =%f\n",idx0,sum.x,sum.y);
       //}
       sum.x = sum.x/collook;
       sum.y = sum.y/collook;
       b[i] = sum;
    }
}

__global__
void cpx_row_look(Complex *a, Complex *b, const std::size_t rowlook,
                  const std::size_t ncol, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    std::size_t row,col,idx0;
    Complex temp, sum;
    
    for (std::size_t i = index; i < n; i += stride){ 
       sum.x = 0;
       sum.y = 0;
       row = i/ncol;
       col = i%ncol;
       idx0 = row*rowlook*ncol+col;
       //if(row==1 && col == 1){
       // printf("row:%llu,col:%llu\n",row,col);
       // printf("idx0:%llu\n",idx0);
       // printf("rowlook:%llu\n",rowlook);
       // printf("ncol:%llu\n",ncol);
       // temp = a[idx0+ncol];
       // printf("temp.x:%f,temp.y:%f\n",temp.x,temp.y);
       // ////printf("idx = %u, sum.x = %f, sum.y =%f\n",idx0,sum.x,sum.y);
       //}
       for (std::size_t j = 0; j < rowlook; ++j) {
            temp = a[idx0+j*ncol];
            sum.x = sum.x + temp.x;
            sum.y = sum.y + temp.y;
       }
       sum.x = sum.x/rowlook;
       sum.y = sum.y/rowlook;
       b[i] = sum;
    }
}

std::string extract_date(const std::string &s){
    std::size_t last_slash_pos;
    #ifdef _WIN32
        last_slash_pos = s.find_last_of('\\');
    #else
        last_slash_pos = s.find_last_of('/');
    #endif
    std::string filename = (last_slash_pos!=std::string::npos) ? 
                           s.substr(last_slash_pos+1) : s;
    std::string date = filename.substr(17,8);
    return date;
}

int crossmul(const std::string &slcfile1,
             const std::string &slcfile2,
             const std::string &rscfile,
             const int rowlook, const int collook,
             std::string intfile=""){
    rsc demrsc;
    demrsc = readrsc(rscfile);
    Complex *slc1, *slc2, *ifglook;
    Complex *d_slc1, *d_slc2, *d_ifg, *d_ifg_collook, *d_ifglook;
    //double *d_amp1, *d_amp2;
    int batch_lines = 2000, blockSize=256, batch_sm;
    int nbatch, numBlocks, nrow, ncol, ncol_sm;
    std::string date1, date2;
    batch_sm = batch_lines/rowlook;
    batch_lines = batch_sm*rowlook;
    date1 = extract_date(slcfile1);
    date2 = extract_date(slcfile2);
    if (intfile==""){
        intfile = date1+"_"+date2+".int";
    }
    std::cout << "output filename: " << intfile << std::endl;
    nbatch = (demrsc.nlat + batch_lines-1)/batch_lines;
    nrow = demrsc.nlat;
    ncol = demrsc.nlon;
    //nrow_sm = nrow/rowlook;
    ncol_sm = ncol/collook;
    slc1 = (Complex *)malloc(sizeof(Complex)*ncol*batch_lines);
    slc2 = (Complex *)malloc(sizeof(Complex)*ncol*batch_lines);
    ifglook = (Complex *)malloc(sizeof(Complex)*batch_sm*ncol_sm);
    cudaMalloc((void**)&d_slc1,sizeof(Complex)*ncol*batch_lines);
    cudaMalloc((void**)&d_slc2,sizeof(Complex)*ncol*batch_lines);
    cudaMalloc((void**)&d_ifg,sizeof(Complex)*ncol*batch_lines);
    cudaMalloc((void**)&d_ifg_collook,sizeof(Complex)*batch_lines*ncol_sm);
    cudaMalloc((void**)&d_ifglook,sizeof(Complex)*batch_sm*ncol_sm);
    
    for (int ibatch = 0; ibatch < nbatch; ++ibatch){
        std::cout << "batch " << (ibatch+1) << "/" << nbatch <<std::endl;
        std::size_t line_start = ibatch * batch_lines;
        std::size_t line_end = line_start + batch_lines;
        line_end = line_end < nrow ? line_end : nrow;
        std::size_t nlines = line_end - line_start;
        read_cpx(slcfile1,line_start*ncol,nlines*ncol,slc1);
        read_cpx(slcfile2,line_start*ncol,nlines*ncol,slc2);
        cudaMemcpy(d_slc1,slc1,sizeof(Complex)*nlines*ncol,
                   cudaMemcpyHostToDevice);
        cudaMemcpy(d_slc2,slc2,sizeof(Complex)*nlines*ncol,
                   cudaMemcpyHostToDevice);
        numBlocks = (nlines*ncol+blockSize-1)/blockSize;
        //std::cout << "cross multiplication" << std::endl;
        conj_mul<<<numBlocks,blockSize>>>(d_slc1,d_slc2,d_ifg,nlines*ncol);
        cudaDeviceSynchronize();
        numBlocks = (nlines*ncol_sm+blockSize-1)/blockSize;
        //std::cout << "column look" << std::endl;
        cpx_col_look<<<numBlocks,blockSize>>>(d_ifg,d_ifg_collook,
                                              collook,ncol,nlines*ncol_sm);
        cudaDeviceSynchronize();
        //std::cout << "row look" << std::endl;
        numBlocks = (nlines/rowlook*ncol_sm+blockSize-1)/blockSize;
        cpx_row_look<<<numBlocks,blockSize>>>(d_ifg_collook,d_ifglook,
                                              rowlook,ncol_sm,nlines/rowlook*ncol_sm);
        cudaError_t err = cudaGetLastError();
        if (err != cudaSuccess) {
            printf("CUDA error: %s\n", cudaGetErrorString(err));
        }
        cudaDeviceSynchronize();
        cudaMemcpy(ifglook,d_ifglook,sizeof(Complex)*nlines/rowlook*ncol_sm,
                   cudaMemcpyDeviceToHost);
        if(ibatch==0){
            save_cpx(ifglook,false,nlines/rowlook*ncol_sm,intfile);
        }else{
            save_cpx(ifglook,true,nlines/rowlook*ncol_sm,intfile);
        }
    }

    free(slc1);
    free(slc2);
    free(ifglook);
    cudaFree(d_slc1);
    cudaFree(d_slc2);
    cudaFree(d_ifg);
    cudaFree(d_ifg_collook);
    cudaFree(d_ifglook);
    return 0;
}

int main(int argc, char *argv[]){
    if (argc<3){
        std::cout << "Usage: crossmul slcfile1 slcfile2 rscfile rowlook collook" << std::endl;
    }
    const std::string slcfile1 = std::string(argv[1]);
    const std::string slcfile2 = std::string(argv[2]);
    const std::string rscfile = std::string(argv[3]);
    const int rowlook = std::stoi(argv[4]);
    const int collook = std::stoi(argv[5]);
    std::string intfile = "";
    if (argc > 6){
        intfile = std::string(argv[6]);
    }
    crossmul(slcfile1,slcfile2,rscfile,rowlook,collook,intfile);
    return 0;
}