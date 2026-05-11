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


template<unsigned int block_size>
__device__ void warpReduce(volatile float *sdata, unsigned int tid){
    if(block_size>=64){
        sdata[tid] += sdata[tid+32];
    }
    if(block_size>=32){
        sdata[tid] += sdata[tid+16];
    }
    if(block_size>=16){
        sdata[tid] += sdata[tid+8];
    }
    if(block_size>=8){
        sdata[tid] += sdata[tid+4];
    }
    if(block_size>=4){
        sdata[tid] += sdata[tid+2];
    }
    if(block_size>=2){
        sdata[tid] += sdata[tid+1];
    }
    return;
}

template<unsigned int block_size>
__global__ void reduce(float *idata,unsigned int n){
    extern __shared__ float sdata[];
    unsigned int tid = threadIdx.x;
    unsigned int i = blockIdx.x*(block_size*2)+tid;
    unsigned int gridSize = block_size*2*gridDim.x;
    sdata[tid] = 0;
    while (i<n) {
        sdata[tid] += idata[i] + idata[i+block_size];
        i+=gridSize;
    }
    __syncthreads();
    if (block_size >= 512){
        if (tid<256){
            sdata[tid] += sdata[tid+256];
        }
        __syncthreads();
    }
    if (block_size >= 256){
        if (tid<128){
            sdata[tid] += sdata[tid+128];
        }
        __syncthreads();
    }
    if (block_size >= 128){
        if (tid<64){
            sdata[tid] += sdata[tid+64];
        }
        __syncthreads();
    }
    if (tid < 32) warpReduce<block_size>(sdata, tid);
    if (tid == 0){
        idata[blockIdx.x] = sdata[0];
    }
    return;
}

float reduce_sum(float *d_c, unsigned int n, const unsigned int block_size){
    float sum;
    unsigned int smem_size = block_size*sizeof(float);
    unsigned int num_blocks = (n + block_size - 1)/block_size;
    while (1){
        if (block_size==512){
            reduce<512><<<num_blocks,block_size,smem_size>>>(d_c,n);
        }else if (block_size==256){
            reduce<256><<<num_blocks,block_size,smem_size>>>(d_c,n);
        }
        CHECK_CUDA(cudaDeviceSynchronize());
        n = num_blocks;
        if (num_blocks<=1){
            break;
        }
        num_blocks = (n+block_size-1)/block_size;
    }
    CHECK_CUDA(cudaMemcpy(&sum,d_c,sizeof(float),cudaMemcpyDeviceToHost));
    return sum;
}

__global__ void find_first_last_nonzero_row_warp(
    const int* data,
    int nrow,
    int ncol,
    int* first_row,
    int* last_row)

{
    int warp_id = (blockIdx.x * blockDim.x + threadIdx.x) / warpSize;
    int lane    = threadIdx.x % warpSize;
    if (warp_id >= nrow)
        return;
    const int* row_ptr = data + warp_id * ncol;
    bool found = false;

    // Scan columns in stride of warpSize
    for (int c = lane; c < ncol; c += warpSize)
    {
        if (row_ptr[c] != 0.0f)
        {
            found = true;
            break;
        }
    }
    unsigned mask = __ballot_sync(0xffffffff, found);
    if (mask && lane == 0)
    {
        // Update first and last row atomically
        atomicMin(first_row, warp_id);
        atomicMax(last_row, warp_id);
    }
}

int get_first_last_nonzero_row(int* d_data, int nrow, int ncol,
                               int &first, int &last)

{
    int h_first = nrow;  // initialize to nrow for atomicMin
    int h_last  = -1;    // initialize to -1 for atomicMax
    int *d_first, *d_last;

    CHECK_CUDA(cudaMalloc(&d_first, sizeof(int)));
    CHECK_CUDA(cudaMalloc(&d_last,  sizeof(int)));
    CHECK_CUDA(cudaMemcpy(d_first, &h_first, sizeof(int),
                cudaMemcpyHostToDevice));
    CHECK_CUDA(cudaMemcpy(d_last,  &h_last,  sizeof(int),
                cudaMemcpyHostToDevice));
    int threads = 256;
    int warps_per_block = threads / 32;
    int blocks = (nrow + warps_per_block - 1) / warps_per_block;
    find_first_last_nonzero_row_warp<<<blocks, threads>>>(
        d_data, nrow, ncol, d_first, d_last);

    CHECK_CUDA(cudaDeviceSynchronize());
    CHECK_CUDA(cudaMemcpy(&h_first, d_first, sizeof(int),
                cudaMemcpyDeviceToHost));
    CHECK_CUDA(cudaMemcpy(&h_last,  d_last,  sizeof(int),
                cudaMemcpyDeviceToHost));
    cudaFree(d_first);
    cudaFree(d_last);
    // If no non-zero row exists
    first = (h_first == nrow) ? -1 : h_first;
    last  = h_last;
    return 0;
}

template <typename T>
__global__ void apply_mask(T *a, T *b, Complex *mask,
        const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    for (std::size_t i = index; i < n; i += stride){
        if (mask[i].x == 0){
            b[i] = 0;
        }else{
            b[i] = a[i];
        }
    }
}

template<>
__global__ void apply_mask<Complex>(Complex *a, Complex *b, Complex *mask,
        const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex zero;
    zero.x = 0.;
    zero.y = 0.;
    for (std::size_t i = index; i < n; i += stride){
        if (mask[i].x == 0){
            b[i] = zero;
        }else{
            b[i] = a[i];
        }
    }
}

template <typename T>
__global__ void replace(T *a, Complex *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex bi;
    for (std::size_t i = index; i < n; i += stride){
        bi = b[i];
        if (bi.x != 0){
            a[i] = atan2f(bi.y,bi.x);
        }
    }
}

template<>
__global__ void replace<Complex>(Complex *a, Complex *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex ai, bi;
    float ai_amp, bi_amp, ratio;
    for (std::size_t i = index; i < n; i += stride){
        bi = b[i];
        if (bi.x != 0){
            ai = a[i];
            ai_amp = sqrtf(ai.x*ai.x + ai.y*ai.y);
            bi_amp = sqrtf(bi.x*bi.x + bi.y*bi.y);
            ratio = ai_amp/(bi_amp + 1e-10);
            ai.x = bi.x * ratio;
            ai.y = bi.y * ratio; 
            a[i] = ai;
        }
    }
}

__global__
void non_overlap_mask(
        Complex *a,
        Complex *b,
        float *mask1,
        float *mask2,
        const int ncol,
        const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    float ax, bx;
    Complex zero;
    zero.x = 0;
    zero.y = 0;
    for (std::size_t i = index; i < n; i += stride){
        ax = a[i].x;
        bx = b[i].x;
        if (ax != 0 && bx == 0){
            mask1[i] = int(i/ncol);
            mask2[i] = 0;
            b[i] = zero;
        }else if(ax == 0 && bx != 0){
            mask1[i] = 0;
            mask2[i] = int(i/ncol);
            a[i] = zero;
        }else{
            mask1[i] = 0;
            mask2[i] = 0;
            a[i] = zero;
            b[i] = zero;
        }
    }
}

__global__
void point_mask(float *a, float *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    for (std::size_t i = index; i < n; i += stride){
        if (a[i] == 0){
            b[i] = 0.;
        }else{
            b[i] = 1.;
        }
    }
}

void multilook(
    Complex *d_ref,
    Complex *d_sec,
    Complex **d_ifglook,
    const int nrow,
    const int ncol,
    const int rowlook,
    const int collook) {

    const int block_size = 256;
    int num_blocks;
    int nrow_sm = nrow / rowlook;
    int ncol_sm = ncol / collook;

    // full-resolution inteferogram
    Complex *d_ifg;
    CHECK_CUDA(cudaMalloc((void**)&d_ifg, sizeof(Complex) * nrow * ncol));
    num_blocks = (nrow * ncol + block_size - 1) / block_size;
    conj_mul<<<num_blocks, block_size>>>(d_ref, d_sec, d_ifg, nrow * ncol);

    // no multilook
    if (rowlook == 1 && collook == 1) {
        *d_ifglook = d_ifg;
        return;
    }

    // column look
    Complex *d_ifg_collook = nullptr;
    if (collook > 1) {
        CHECK_CUDA(cudaMalloc((void**)&d_ifg_collook,
                              sizeof(Complex) * nrow * ncol_sm));
        num_blocks = (nrow * ncol_sm + block_size - 1) / block_size;
        cpx_col_look<<<num_blocks, block_size>>>(
                d_ifg, d_ifg_collook, collook, ncol, nrow * ncol_sm);
        cudaDeviceSynchronize();
        cudaFree(d_ifg);
    } else {
        d_ifg_collook = d_ifg;
    }

    // row look
    if (rowlook > 1) {
        Complex *d_ifg_final;
        CHECK_CUDA(cudaMalloc((void**)&d_ifg_final,
                    sizeof(Complex) * nrow_sm * ncol_sm));
        num_blocks = (nrow_sm * ncol_sm + block_size - 1) / block_size;
        cpx_row_look<<<num_blocks, block_size>>>(
                d_ifg_collook, d_ifg_final, rowlook, ncol_sm, nrow_sm * ncol_sm);
        cudaDeviceSynchronize();
        cudaFree(d_ifg_collook);
        *d_ifglook = d_ifg_final;
        return;
    } else {
        *d_ifglook = d_ifg_collook; 
        return;
    }
}

__global__
void coherence_kernel(Complex *d_ifg, float *d_amp, float *d_coh, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex ai;
    for (std::size_t i = index; i < n; i += stride){
        ai = d_ifg[i];
        if (d_amp[i] > 0){
            d_coh[i] = (ai.x*ai.x+ai.y*ai.y)/d_amp[i];
        }else{
            d_coh[i] = 0;
        }
    }
}

float cal_coherence(Complex *d_ifg, const int nrow, const int ncol){
    const int block_size = 256;
    const int rowlook = 5, collook = 5;
    int num_blocks = (nrow*ncol+block_size-1)/block_size;
    int nrow_sm = nrow/rowlook;
    int ncol_sm = ncol/collook;
    if (nrow_sm <= 0 || ncol_sm <= 0){
        return 0;
    }
    float coh;
    Complex *d_ifg_collook, *d_ifg_look;
    float *d_amp, *d_amp_collook, *d_amp_look;
    // allocate memory
    CHECK_CUDA(cudaMalloc((void**)&d_ifg_collook,sizeof(Complex)*nrow*ncol_sm));
    CHECK_CUDA(cudaMalloc((void**)&d_ifg_look,sizeof(Complex)*nrow_sm*ncol_sm));
    CHECK_CUDA(cudaMalloc((void**)&d_amp,sizeof(float)*nrow*ncol));
    CHECK_CUDA(cudaMalloc((void**)&d_amp_collook,sizeof(float)*nrow*ncol_sm));
    CHECK_CUDA(cudaMalloc((void**)&d_amp_look,sizeof(float)*nrow_sm*ncol_sm));

    // calculate amplitude
    point_power<<<num_blocks,block_size>>>(d_ifg,d_amp,nrow*ncol);
    CHECK_CUDA(cudaDeviceSynchronize());

    // column look
    cpx_col_look<<<num_blocks,block_size>>>(d_ifg,d_ifg_collook,
                                          collook,ncol,nrow*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    col_look<<<num_blocks,block_size>>>(d_amp,d_amp_collook,
                                          collook,ncol,nrow*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());

    // row look
    num_blocks = (nrow_sm*ncol_sm+block_size-1)/block_size;
    cpx_row_look<<<num_blocks,block_size>>>(d_ifg_collook,d_ifg_look,
                                            rowlook,ncol_sm,nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    row_look<<<num_blocks,block_size>>>(d_amp_collook,d_amp_look,
                                        rowlook,ncol_sm,nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());

    // coherence calculation
    coherence_kernel<<<num_blocks,block_size>>>(
        d_ifg_look,d_amp_look,d_amp_look,nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());

    coh = reduce_sum(d_amp_look, nrow_sm*ncol_sm, block_size);
    coh = coh/(nrow_sm*ncol_sm);
    cudaFree(d_amp);
    cudaFree(d_amp_collook);
    cudaFree(d_amp_look);
    cudaFree(d_ifg_collook);
    cudaFree(d_ifg_look);
    return coh;
}

template<typename T>
void crossmul_strip(
        Strip<Complex> *strip1,
        Strip<Complex> *strip2,
        Strip<Complex> *main1,
        Strip<Complex> *main2,
        Strip<T> *ifg,
        const int rowlook,
        const int collook,
        bool asc,
        int &next_flag,
        bool &updated){

    int left, top, right, bottom, nrow, ncol, n;
    int nrow_ifg, nrow_sm, ncol_sm;
    //int first_nonzero_row, first_nonzero_row1, first_nonzero_row2;
    //int last_nonzero_row, last_nonzero_row1, last_nonzero_row2;
    int block_size = 256, num_blocks;
    Complex *d_slc1, *d_slc2, *d_main, *d_ifgsec;
    T *d_ifgmain, *d_ifgmain_masked;
    float *d_mask1, *d_mask2;
    float row_sum1, row_sum2, count1, count2, row_mean1, row_mean2;
    //float coh_main, coh_sec;

    if (strip1->top > strip2->bottom){
        next_flag = 0;
        return;
    }
    if (strip1->bottom <= strip2->top){
        next_flag = 1;
        return;
    }

    // define the common grid to resample the two strips
    left = ifg->left * collook;
    right = ifg->right * collook;
    top = std::min(strip1->top, strip2->top);
    top = (top + rowlook - 1) / rowlook * rowlook;
    bottom = std::max(strip1->bottom, strip2->bottom);
    bottom = bottom / rowlook * rowlook;
    nrow = bottom - top;
    ncol = right - left;
    n = nrow*ncol;
    num_blocks = (n + block_size) / block_size;
    // load two strips to the common grid
    strip1->load_data(left, top, right, bottom);
    strip2->load_data(left, top, right, bottom);

    // calculate nonoverlapped masks
    CHECK_CUDA(cudaMalloc((void**)&d_slc1,sizeof(Complex)*n));
    CHECK_CUDA(cudaMalloc((void**)&d_slc2,sizeof(Complex)*n));
    CHECK_CUDA(cudaMalloc((void**)&d_mask1,sizeof(float)*n));
    CHECK_CUDA(cudaMalloc((void**)&d_mask2,sizeof(float)*n));
    CHECK_CUDA(cudaMemcpy(d_slc1, strip1->data, sizeof(Complex)*n,
                cudaMemcpyHostToDevice));
    CHECK_CUDA(cudaMemcpy(d_slc2, strip2->data, sizeof(Complex)*n,
                cudaMemcpyHostToDevice));
    non_overlap_mask<<<num_blocks,block_size>>>(
            d_slc1,d_slc2,d_mask1,d_mask2,ncol,n);
    CHECK_CUDA(cudaDeviceSynchronize());
    //float *mask1, *mask2;
    //mask1 = (float*)malloc(sizeof(float)*n);
    //mask2 = (float*)malloc(sizeof(float)*n);
    //CHECK_CUDA(cudaMemcpy(mask1,d_mask1,sizeof(float)*n,
    //           cudaMemcpyDeviceToHost));
    //CHECK_CUDA(cudaMemcpy(mask2,d_mask2,sizeof(float)*n,
    //           cudaMemcpyDeviceToHost));
    //save_binary<float>(mask1, 0, n,std::to_string(top/rowlook - ifg->top)+".mask1");
    //save_binary<float>(mask2, 0, n,std::to_string(top/rowlook - ifg->top)+".mask2");
    //free(mask1);
    //free(mask2);

    row_sum1 = reduce_sum(d_mask1, n, 256);
    point_mask<<<num_blocks,block_size>>>(d_mask1, d_mask1, n);
    CHECK_CUDA(cudaDeviceSynchronize());
    count1 = reduce_sum(d_mask1, n, 256);
    cudaFree(d_mask1);
    if (count1 == 0){
        row_mean1 = 0;
    }else{
        row_mean1 = row_sum1/count1;
    }

    row_sum2 = reduce_sum(d_mask2, n, 256);
    point_mask<<<num_blocks,block_size>>>(d_mask2, d_mask2, n);
    CHECK_CUDA(cudaDeviceSynchronize());
    count2 = reduce_sum(d_mask2, n, 256);
    cudaFree(d_mask2);
    if (count2 == 0){
        row_mean2 = 0;
    }else{
        row_mean2 = row_sum2/count2;
    }
    
    //get_first_last_nonzero_row(d_mask1, nrow, ncol,
    //        first_nonzero_row1, last_nonzero_row1);
    //cudaFree(d_mask1);
    //get_first_last_nonzero_row(d_mask2, nrow, ncol,
    //        first_nonzero_row2, last_nonzero_row2);
    //cudaFree(d_mask2);

    // decide which mask will be used for interferogram generation
    if (row_mean1 == 0 && row_mean2 == 0){
        // two strips match perfectly
        next_flag = 2;
        cudaFree(d_slc1);
        cudaFree(d_slc2);
        return;
    }
    if (row_mean2 == 0 || row_mean1 < row_mean2){
        if (asc){
            // non-overlapped area is from strip1
            next_flag = 3;
            cudaFree(d_slc2);
        }else{
            next_flag = 4;
            cudaFree(d_slc1);
        }
    }else if(
        row_mean1 == 0 || row_mean1 > row_mean2){
        if (asc){
            // non-overlapped area is from strip2
            next_flag = 4;
            cudaFree(d_slc1);
        }else{
            // non-overlapped area is from strip1
            next_flag = 3;
            cudaFree(d_slc2);
        }
    }else{
        // should not reach here
        next_flag = 2;
        cudaFree(d_slc1);
        cudaFree(d_slc2);
        return;
    }
    //std::cout << "top line: " << top/rowlook - ifg->top << std::endl;
    //std::cout << "row_mean1: " << row_mean1 << std::endl;
    //std::cout << "row_mean2: " << row_mean2 << std::endl;

    // allocate for cross-multiplication
    //first_nonzero_row = (first_nonzero_row + rowlook - 1) / rowlook * rowlook;
    //last_nonzero_row = last_nonzero_row / rowlook * rowlook;
    //if (last_nonzero_row - first_nonzero_row <= rowlook){
    //    next_flag = 2;
    //    cudaFree(d_slc1);
    //    cudaFree(d_slc2);
    //    return;
    //}
    nrow_ifg = nrow;
    nrow_sm = nrow_ifg / rowlook;
    ncol_sm = ncol / collook;
    CHECK_CUDA(cudaMalloc((void**)&d_main,sizeof(Complex)*ncol*nrow_ifg));
    
    if (next_flag == 3){
        main2->load_data(left, top, right, bottom);
        CHECK_CUDA(cudaMemcpy(d_main,main2->data,sizeof(Complex)*ncol*nrow,
                cudaMemcpyHostToDevice));
        multilook(d_slc1,d_main,&d_ifgsec, nrow,ncol,rowlook,collook);
        cudaFree(d_slc1);
    }else{
        main1->load_data(left, top, right, bottom);
        CHECK_CUDA(cudaMemcpy(d_main,main1->data,sizeof(Complex)*ncol*nrow,
                cudaMemcpyHostToDevice));
        multilook(d_main,d_slc2,&d_ifgsec,nrow,ncol,rowlook,collook);
        cudaFree(d_slc2);
    }
    cudaFree(d_main);

    //Complex *ifgsec;
    //ifgsec = (Complex*)malloc(sizeof(Complex)*nrow_sm*ncol_sm);
    //CHECK_CUDA(cudaMemcpy(ifgsec,d_ifgsec,sizeof(Complex)*ncol_sm*nrow_sm,
    //           cudaMemcpyDeviceToHost));
    //save_binary<Complex>(ifgsec, 0, ncol_sm*nrow_sm,std::to_string(top/rowlook - ifg->top)+"_sec.int");
    //free(ifgsec);

    // load main interferogram strip
    CHECK_CUDA(cudaMalloc((void**)&d_ifgmain,sizeof(T)*nrow_sm*ncol_sm));
    CHECK_CUDA(cudaMalloc((void**)&d_ifgmain_masked,sizeof(T)*nrow_sm*ncol_sm));
    CHECK_CUDA(cudaMemcpy(d_ifgmain,ifg->data+((top/rowlook-ifg->top)*ifg->ncol),
                sizeof(T)*ncol_sm*nrow_sm,cudaMemcpyHostToDevice));

    // apply zero mask
    num_blocks = (nrow_sm*ncol_sm + block_size - 1) / block_size;
    apply_mask<T><<<num_blocks, block_size>>>(
        d_ifgmain, d_ifgmain_masked, d_ifgsec, nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    
    //coh_main = cal_coherence(d_ifgmain_masked, nrow_sm, ncol_sm);
    //coh_sec = cal_coherence(d_ifgsec, nrow_sm, ncol_sm);
    //if (coh_main > coh_sec){
    //    std::cout << "coh main " << coh_main << ", coh sec " << coh_sec <<
    //        ", no need to update" << std::endl;
    //}
    //T *ifgmain;
    //ifgmain = (T*)malloc(sizeof(T)*nrow_sm*ncol_sm);
    //CHECK_CUDA(cudaMemcpy(ifgmain,d_ifgmain_masked,sizeof(T)*nrow_sm*ncol_sm,
    //           cudaMemcpyDeviceToHost));
    //save_binary<T>(ifgmain, 0, ncol_sm*nrow_sm,std::to_string(top/rowlook - ifg->top)+"_main.int");
    //free(ifgmain);
    //if (coh_sec > coh_main){
    // replace main interferogram with secondary interferogram
    replace<T><<<num_blocks, block_size>>>(d_ifgmain, d_ifgsec, nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    CHECK_CUDA(cudaMemcpy(ifg->data+((top/rowlook-ifg->top)*ifg->ncol),
            d_ifgmain,sizeof(T)*ncol_sm*nrow_sm,
            cudaMemcpyDeviceToHost));
    updated = true;
    //}
    cudaFree(d_ifgsec);
    cudaFree(d_ifgmain);
    return;
}

template <typename T>
int crossmul_sec(
             const std::string &main_slcfile1,
             const std::string &sec_slcfile1,
             const std::string &main_slcfile2,
             const std::string &sec_slcfile2,
             const std::string &intfile,
             const int rowlook, const int collook,
             const bool asc){
    // delcaration
    bool updated = false;
    int strip_idx1 = 0, strip_idx2 = 0, next_flag;
    Subswath<Complex> sec1(sec_slcfile1);
    Subswath<Complex> sec2(sec_slcfile2);
    Strip<Complex> main1(main_slcfile1, false);
    Strip<Complex> main2(main_slcfile2, false);
    Strip<T> ifg(intfile, true);
    // end of declaration
    while (strip_idx1 < sec1.nstrip && strip_idx2 < sec2.nstrip){
        Strip<Complex> strip1 = sec1.data[strip_idx1];
        Strip<Complex> strip2 = sec2.data[strip_idx2];
        crossmul_strip<T>(&strip1, &strip2, &main1, &main2, &ifg, rowlook,
            collook, asc, next_flag, updated);
        std::cout << "strip1 " << strip_idx1 << ", strip2 " << strip_idx2 <<
                  ", next_flag " << next_flag << std::endl;
        if (next_flag == 0){
            strip_idx2++;
            continue;
        }
        if (next_flag == 1){
            strip_idx1++;
            continue;
        }
        if (next_flag >= 2){
            strip_idx1++;
            strip_idx2++;
            continue;
        }
    }
    if (updated){
        //std::cout << "updated" <<std::endl;
        ifg.save_data();
    }

    return 0;
}

int main(int argc, char *argv[]){
    if (argc<10){
        std::cout << "Usage: crossmul_sec main_slcfile1 sec_slcfile1 " <<
            "main_slcfile2 sec_slcfile2 intfile rowlook collook asc " <<
            "out_float" << std::endl;
        return 0;
    }
    const std::string main_slcfile1 = std::string(argv[1]);
    const std::string sec_slcfile1 = std::string(argv[2]);
    const std::string main_slcfile2 = std::string(argv[3]);
    const std::string sec_slcfile2 = std::string(argv[4]);
    const std::string intfile = std::string(argv[5]);
    const int rowlook = std::stoi(argv[6]);
    const int collook = std::stoi(argv[7]);
    const std::string direction = std::string(argv[8]);
    const int out_float = std::stoi(argv[9]);
    bool asc;
    if (direction == "asc"){
        asc = true;
    }else{
        asc = false;
    }
    if (out_float){
        crossmul_sec<float>(main_slcfile1,sec_slcfile1,main_slcfile2,
            sec_slcfile2,intfile,rowlook,collook,asc);
    }else{
        crossmul_sec<Complex>(main_slcfile1,sec_slcfile1,main_slcfile2,
            sec_slcfile2,intfile,rowlook,collook,asc);
    }
    return 0;
}
