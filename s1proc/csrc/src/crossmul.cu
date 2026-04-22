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
             const std::string &intfile,
             const int rowlook, const int collook,
             const int out_float){
    // delcaration
    std::int32_t header1[NHEADER], header2[NHEADER], ifg_header[NHEADER];
    Complex *slc1, *slc2, *ifglook;
    Complex *d_slc1, *d_slc2, *d_ifg, *d_ifg_collook, *d_ifglook;
    float *phase, *d_phase;
    int batch_lines = 3000, blockSize=256, batch_sm;
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
    // read the header of the first image
    read_binary<std::int32_t>(slcfile1, NHEADER, header1);
    // read the header of the second image
    read_binary<std::int32_t>(slcfile2, NHEADER, header2);
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
    top = (top + rowlook -1) / rowlook * rowlook;
    bottom = bottom1 > bottom2 ? bottom1 : bottom2;
    bottom = bottom / rowlook * rowlook;
    nrow = bottom - top;
    ncol = right - left;
    // fill ifg_header
    ifg_header[0] = header1[0] / rowlook;
    ifg_header[1] = header1[1] / collook;
    ifg_header[2] = left / collook;
    ifg_header[3] = top / rowlook;
    ifg_header[4] = right / collook;
    ifg_header[5] = bottom / rowlook;
    
    nbatch = (nrow + batch_lines-1)/batch_lines;
    ncol_sm = ncol/collook;
    slc1 = (Complex *)malloc(sizeof(Complex)*ncol*batch_lines);
    slc2 = (Complex *)malloc(sizeof(Complex)*ncol*batch_lines);
    if (out_float){
        phase = (float *)malloc(sizeof(float)*batch_sm*ncol_sm);
        CHECK_CUDA(cudaMalloc((void**)&d_phase,sizeof(float)*batch_sm*ncol_sm));
    }else{
        ifglook = (Complex *)malloc(sizeof(Complex)*batch_sm*ncol_sm);
    } 
    CHECK_CUDA(cudaMalloc((void**)&d_slc1,sizeof(Complex)*ncol*batch_lines));
    CHECK_CUDA(cudaMalloc((void**)&d_slc2,sizeof(Complex)*ncol*batch_lines));
    CHECK_CUDA(cudaMalloc((void**)&d_ifg,sizeof(Complex)*ncol*batch_lines));
    if (collook > 1){
        CHECK_CUDA(cudaMalloc((void**)&d_ifg_collook,
            sizeof(Complex)*batch_lines*ncol_sm));
    }
    if (rowlook > 1){
        CHECK_CUDA(cudaMalloc((void**)&d_ifglook,
            sizeof(Complex)*batch_sm*ncol_sm));
    }
    
    for (int ibatch = 0; ibatch < nbatch; ++ibatch){
        std::cout << "batch " << (ibatch+1) << "/" << nbatch <<std::endl;
        std::size_t line_start = ibatch * batch_lines;
        std::size_t line_end = line_start + batch_lines;
        line_end = line_end < nrow ? line_end : nrow;
        std::size_t nlines = line_end - line_start;
        read_and_resample<Complex>(slcfile1, slc1, left, top, right, bottom,
                line_start, line_end);
        read_and_resample<Complex>(slcfile2, slc2, left, top, right, bottom,
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
        if (collook > 1){
            cpx_col_look<<<numBlocks,blockSize>>>(d_ifg,d_ifg_collook,
                                              collook,ncol,nlines*ncol_sm);
        }else{
            d_ifg_collook = d_ifg;
        }
        CHECK_CUDA(cudaDeviceSynchronize());
        numBlocks = (nlines/rowlook*ncol_sm+blockSize-1)/blockSize;
        if (rowlook > 1){
            cpx_row_look<<<numBlocks,blockSize>>>(d_ifg_collook,d_ifglook,
                                        rowlook,ncol_sm,nlines/rowlook*ncol_sm);
        }else{
            d_ifglook = d_ifg_collook;
        }
        CHECK_CUDA(cudaDeviceSynchronize());
        if (out_float){
            point_angle<<<numBlocks,blockSize>>>(d_ifglook,d_phase,
                                        nlines/rowlook*ncol_sm);
            CHECK_CUDA(cudaMemcpy(
                phase,d_phase,sizeof(float)*nlines/rowlook*ncol_sm,
                cudaMemcpyDeviceToHost));
        }else{
            CHECK_CUDA(cudaMemcpy(
                ifglook,d_ifglook,sizeof(Complex)*nlines/rowlook*ncol_sm,
                cudaMemcpyDeviceToHost));
        }
        if(ibatch==0){
            if (out_float){
                save_binary<float>(phase,nlines/rowlook*ncol_sm,ifg_header,
                    NHEADER,intfile);
            }else{
                save_binary<Complex>(ifglook,nlines/rowlook*ncol_sm,ifg_header,
                    NHEADER,intfile);
            }
        }else{
            if (out_float){
                save_binary<float>(phase,true,nlines/rowlook*ncol_sm,
                    intfile);
            }else{
                save_binary<Complex>(ifglook,true,nlines/rowlook*ncol_sm,
                    intfile);
            }
        }
    }

    free(slc1);
    free(slc2);
    if (out_float){
        free(phase);
        cudaFree(d_phase);
    }else{
        free(ifglook);
    }
    cudaFree(d_slc1);
    cudaFree(d_slc2);
    cudaFree(d_ifg);
    if (collook > 1){
        cudaFree(d_ifg_collook);
    }
    if (rowlook > 1){
        cudaFree(d_ifglook);
    }
    return 0;
}

int main(int argc, char *argv[]){
    if (argc<7){
        std::cout << "Usage: crossmul slcfile1 slcfile2 intfile rowlook " <<
            "collook out_float" << std::endl;
        return 0;
    }
    const std::string slcfile1 = std::string(argv[1]);
    const std::string slcfile2 = std::string(argv[2]);
    const std::string intfile = std::string(argv[3]);
    const int rowlook = std::stoi(argv[4]);
    const int collook = std::stoi(argv[5]);
    const int out_float = std::stoi(argv[6]);
    crossmul(slcfile1,slcfile2,intfile,rowlook,collook,out_float);
    return 0;
}
