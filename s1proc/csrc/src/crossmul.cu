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
    std::string date = filename.substr(0,8);
    return date;
}

int crossmul(const std::string &slcfile1,
             const std::string &slcfile2,
             const int rowlook, const int collook,
             std::string intfile=""){
    // delcaration
    const int nheader = 64;
    std::int32_t header_data1[nheader], header_data2[nheader];
    Complex *slc1, *slc2, *ifglook;
    Complex *d_slc1, *d_slc2, *d_ifg, *d_ifg_collook, *d_ifglook;
    int batch_lines = 2000, blockSize=256, batch_sm;
    int nbatch, numBlocks, ncol_sm;
    // image parameters
    // image1
    int left1, top1, right1, bottom1;
    // image2
    int left2, top2, right2, bottom2;
    // raw interferogram
    int left, top, right, bottom, nrow, ncol;
    std::string date1, date2;
    // end of declaration

    batch_sm = batch_lines/rowlook;
    batch_lines = batch_sm*rowlook;
    date1 = extract_date(slcfile1);
    date2 = extract_date(slcfile2);
    if (intfile==""){
        intfile = date1+"_"+date2+".int";
    }
    std::cout << "output filename: " << intfile << std::endl;
    // read the header of the first image
    read_binary<std::int32_t>(slcfile1, nheader, header_data1);
    // read the header of the second image
    read_binary<std::int32_t>(slcfile2, nheader, header_data2);
    // read the parameters of the first image
    left1 = header_data1[2];
    top1 = header_data1[3];
    right1 = header_data1[4];
    bottom1 = header_data1[5];
    // read the parameters of the second image
    left2 = header_data2[2];
    top2 = header_data2[3];
    right2 = header_data2[4];
    bottom2 = header_data2[5];
    // determine the size of the interferogram 
    left = left1 < left2 ? left1 : left2;
    left = int(left / collook) * collook;
    right = right1 > right2 ? right1 : right2;
    right = int(right / collook) * collook;
    top = top1 < top2 ? top1 : top2;
    top = int(top / rowlook) * rowlook;
    bottom = bottom1 > bottom2 ? bottom1 : bottom2;
    bottom = int(bottom/rowlook)*rowlook;
    nrow = bottom - top;
    ncol = right - left;
    
    nbatch = (nrow + batch_lines-1)/batch_lines;
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
        read_and_resample(slcfile1, slc1, left, top, right, bottom,
                line_start, line_end);
        read_and_resample(slcfile2, slc2, left, top, right, bottom,
                line_start, line_end);
        CHECK_CUDA(cudaMemcpy(d_slc1,slc1,sizeof(Complex)*nlines*ncol,
                   cudaMemcpyHostToDevice));
        CHECK_CUDA(cudaMemcpy(d_slc2,slc2,sizeof(Complex)*nlines*ncol,
                   cudaMemcpyHostToDevice));
        numBlocks = (nlines*ncol+blockSize-1)/blockSize;
        conj_mul<<<numBlocks,blockSize>>>(d_slc1,d_slc2,d_ifg,nlines*ncol);
        CHECK_CUDA(cudaDeviceSynchronize());
        numBlocks = (nlines*ncol_sm+blockSize-1)/blockSize;
        //std::cout << "column look" << std::endl;
        cpx_col_look<<<numBlocks,blockSize>>>(d_ifg,d_ifg_collook,
                                              collook,ncol,nlines*ncol_sm);
        CHECK_CUDA(cudaDeviceSynchronize());
        //std::cout << "row look" << std::endl;
        numBlocks = (nlines/rowlook*ncol_sm+blockSize-1)/blockSize;
        cpx_row_look<<<numBlocks,blockSize>>>(d_ifg_collook,d_ifglook,
                                              rowlook,ncol_sm,nlines/rowlook*ncol_sm);
        CHECK_CUDA(cudaDeviceSynchronize());
        CHECK_CUDA(cudaMemcpy(ifglook,d_ifglook,sizeof(Complex)*nlines/rowlook*ncol_sm,
                   cudaMemcpyDeviceToHost));
        if(ibatch==0){
            save_binary<Complex>(ifglook,false,nlines/rowlook*ncol_sm,intfile);
        }else{
            save_binary<Complex>(ifglook,true,nlines/rowlook*ncol_sm,intfile);
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
    if (argc<5){
        std::cout << "Usage: crossmul slcfile1 slcfile2 rowlook collook " <<
            "[intfile]" << std::endl;
        return 0;
    }
    const std::string slcfile1 = std::string(argv[1]);
    const std::string slcfile2 = std::string(argv[2]);
    const int rowlook = std::stoi(argv[3]);
    const int collook = std::stoi(argv[4]);
    std::string intfile = "";
    if (argc > 5){
        intfile = std::string(argv[5]);
    }
    crossmul(slcfile1,slcfile2,rowlook,collook,intfile);
    return 0;
}
