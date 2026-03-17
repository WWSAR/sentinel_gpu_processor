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
__device__ void warpReduce(volatile Complex *sdata, unsigned int tid){
    if(block_size>=64){
        sdata[tid].x += sdata[tid+32].x;
        sdata[tid].y += sdata[tid+32].y;
    }
    if(block_size>=32){
        sdata[tid].x += sdata[tid+16].x;
        sdata[tid].y += sdata[tid+16].y;
    }
    if(block_size>=16){
        sdata[tid].x += sdata[tid+8].x;
        sdata[tid].y += sdata[tid+8].y;
    }
    if(block_size>=8){
        sdata[tid].x += sdata[tid+4].x;
        sdata[tid].y += sdata[tid+4].y;
    }
    if(block_size>=4){
        sdata[tid].x += sdata[tid+2].x;
        sdata[tid].y += sdata[tid+2].y;
    }
    if(block_size>=2){
        sdata[tid].x += sdata[tid+1].x;
        sdata[tid].y += sdata[tid+1].y;
    }
    return;
}

template<unsigned int block_size>
__global__ void reduce(Complex *idata,unsigned int n){
    extern __shared__ Complex sdata[];
    unsigned int tid = threadIdx.x;
    unsigned int i = blockIdx.x*(block_size*2)+tid;
    unsigned int gridSize = block_size*2*gridDim.x;
    Complex a, b, zero;
    float amp;
    zero.x = 0.;
    zero.y = 0.;
    sdata[tid] = zero;
    while (i<n) {
        a = idata[i];
        b = idata[i+block_size];
        amp = sqrt(a.x*a.x+a.y*a.y+1e-12);
        a.x = a.x/amp;
        a.y = a.y/amp;
        amp = sqrt(b.x*b.x+b.y*b.y+1e-12);
        b.x = b.x/amp;
        b.y = b.y/amp;
        sdata[tid].x += a.x+b.x;
        sdata[tid].y += a.y+b.y;
        i+=gridSize;
    }
    __syncthreads();
    if (block_size >= 512){
        if (tid<256){
            sdata[tid].x += sdata[tid+256].x;
            sdata[tid].y += sdata[tid+256].y;
        }
        __syncthreads();
    }
    if (block_size >= 256){
        if (tid<128){
            sdata[tid].x += sdata[tid+128].x;
            sdata[tid].y += sdata[tid+128].y;
        }
        __syncthreads();
    }
    if (block_size >= 128){
        if (tid<64){
            sdata[tid].x += sdata[tid+64].x;
            sdata[tid].y += sdata[tid+64].y;
        }
        __syncthreads();
    }
    if (tid < 32) warpReduce<block_size>(sdata, tid);
    if (tid == 0){
        idata[blockIdx.x] = sdata[0];
    }
    return;
}

float reduce_sum(Complex *d_c, unsigned int n, const unsigned int block_size){
    Complex sum;
    unsigned int smem_size=block_size*sizeof(Complex);
    unsigned int num_blocks = (n + block_size - 1)/block_size;
    while (1){
        if (block_size==512){
            reduce<512><<<num_blocks,block_size,smem_size>>>(d_c,n);
        }else if (block_size==256){
            reduce<256><<<num_blocks,block_size,smem_size>>>(d_c,n);
        }
        CHECK_CUDA(cudaDeviceSynchronize());
        n = num_blocks;
        if (num_blocks==1){
            break;
        }
        num_blocks = (n+block_size-1)/block_size;
    }
    CHECK_CUDA(cudaMemcpy(&sum,d_c,sizeof(Complex),cudaMemcpyDeviceToHost));
    return sum.x*sum.x + sum.y*sum.y;
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

__global__ void apply_mask(Complex *a, Complex *b, Complex *mask,
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

__global__ void replace(Complex *a, Complex *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex bi;
    for (std::size_t i = index; i < n; i += stride){ 
        bi = b[i];
        if (bi.x != 0){
            a[i] = bi;
        }
    }
}

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

__global__
void non_overlap_mask(
        Complex *a,
        Complex *b,
        int *mask1,
        int* mask2,
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
            mask1[i] = 1;
            mask2[i] = 0;
            b[i] = zero;
        }else if(ax == 0 && bx == 1){
            mask1[i] = 0;
            mask2[i] = 1;
            a[i] = zero;
        }else{
            mask1[i] = 0;
            mask2[i] = 0;
            a[i] = zero;
            b[i] = zero;
        }
    }
}

void multilook(
        Complex *d_ref,
        Complex *d_sec,
        Complex *d_ifglook,
        const int nrow,
        const int ncol,
        const int rowlook,
        const int collook){
    const int block_size = 256;
    int num_blocks, nrow_sm, ncol_sm;
    Complex *d_ifg, *d_ifg_collook;

    num_blocks = (nrow*ncol+block_size-1)/block_size;
    nrow_sm = nrow/rowlook;
    ncol_sm = ncol/collook;
    CHECK_CUDA(cudaMalloc((void**)&d_ifg,sizeof(Complex)*nrow*ncol));
    CHECK_CUDA(cudaMalloc((void**)&d_ifg_collook,
                sizeof(Complex)*nrow*ncol_sm));
    conj_mul<<<num_blocks,block_size>>>(d_ref,d_sec,d_ifg,nrow*ncol);
    CHECK_CUDA(cudaDeviceSynchronize());
    num_blocks = (nrow*ncol_sm+block_size-1)/block_size;
    cpx_col_look<<<num_blocks,block_size>>>(d_ifg,d_ifg_collook,
                                          collook,ncol,nrow*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    cudaFree(d_ifg);
    num_blocks = (nrow_sm*ncol_sm+block_size-1)/block_size;
    cpx_row_look<<<num_blocks,block_size>>>(d_ifg_collook,d_ifglook,
                                          rowlook,ncol_sm,nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize());
    cudaFree(d_ifg_collook);
}

void crossmul_strip(
        Strip *strip1,
        Strip *strip2,
        Strip *main1,
        Strip *main2,
        Strip *ifg,
        const int rowlook,
        const int collook,
        int &next_flag,
        bool &updated){
    
    int left, top, right, bottom, nrow, ncol, n;
    int nrow_ifg, nrow_sm, ncol_sm;
    int first_nonzero_row, first_nonzero_row1, first_nonzero_row2;
    int last_nonzero_row, last_nonzero_row1, last_nonzero_row2;
    int block_size = 256, num_blocks;
    float coh_main, coh_sec;
    Complex *d_slc1, *d_slc2, *d_main, *d_ifgsec;
    Complex *d_ifgmain, *d_ifgmain_masked;
    int *d_mask1, *d_mask2;
    
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
    CHECK_CUDA(cudaMalloc((void**)&d_mask1,sizeof(int)*n));
    CHECK_CUDA(cudaMalloc((void**)&d_mask2,sizeof(int)*n));
    CHECK_CUDA(cudaMemcpy(d_slc1, strip1->data, sizeof(Complex)*nrow*ncol,
                cudaMemcpyHostToDevice));
    CHECK_CUDA(cudaMemcpy(d_slc2, strip2->data, sizeof(Complex)*nrow*ncol,
                cudaMemcpyHostToDevice));
    std::cout << "Calculate nonoverlapped masks" << std::endl;
    non_overlap_mask<<<num_blocks,block_size>>>(
            d_slc1,d_slc2,d_mask1,d_mask2,n);
    CHECK_CUDA(cudaDeviceSynchronize());
    get_first_last_nonzero_row(d_mask1, nrow, ncol,
            first_nonzero_row1, last_nonzero_row1);
    cudaFree(d_mask1);
    get_first_last_nonzero_row(d_mask2, nrow, ncol,
            first_nonzero_row2, last_nonzero_row2);
    cudaFree(d_mask2);

    // decide which mask will be used for interferogram generation
    if (first_nonzero_row1 == -1 && first_nonzero_row2 == -1){
        // two strips match perfectly
        next_flag = 2;
        cudaFree(d_slc1);
        cudaFree(d_slc2);
        return;
    }
    if (first_nonzero_row2 == -1 || first_nonzero_row1 < first_nonzero_row2){
        // non-overlapped area is from strip1
        next_flag = 3;
        first_nonzero_row = first_nonzero_row1;
        last_nonzero_row = last_nonzero_row1;
        cudaFree(d_slc2);
    }else if(
        first_nonzero_row1 == -1 || first_nonzero_row1 > first_nonzero_row2){
        // non-overlapped area is from strip2
        next_flag = 4;
        first_nonzero_row = first_nonzero_row2;
        last_nonzero_row = last_nonzero_row2;
        cudaFree(d_slc1);
    }else{
        // should not reach here
        next_flag = 2;
        cudaFree(d_slc1);
        cudaFree(d_slc2);
        return;
    }
    if (last_nonzero_row - first_nonzero_row <= rowlook){
        next_flag = 2;
        cudaFree(d_slc1);
        cudaFree(d_slc2);
        return;
    }
     
    // allocate for cross-multiplication
    first_nonzero_row = (first_nonzero_row + rowlook - 1) / rowlook * rowlook;
    last_nonzero_row = last_nonzero_row / rowlook * rowlook;
    nrow_ifg = last_nonzero_row - first_nonzero_row;
    nrow_sm = nrow_ifg / rowlook;
    ncol_sm = ncol / collook;
    CHECK_CUDA(cudaMalloc((void**)&d_main,sizeof(Complex)*ncol*nrow_ifg));
    CHECK_CUDA(cudaMalloc((void**)&d_ifgsec,sizeof(Complex)*ncol_sm*nrow_sm));
    if (next_flag == 3){
        main2->load_data(
               left, first_nonzero_row + top, right, last_nonzero_row + bottom);
        CHECK_CUDA(cudaMemcpy(d_main,main2->data,sizeof(Complex)*ncol*nrow_ifg,
                cudaMemcpyHostToDevice));
        multilook(d_slc1,d_main,d_ifgsec,nrow_ifg,ncol,rowlook,collook);
        cudaFree(d_slc1);
    }else{
        main1->load_data(
               left, first_nonzero_row + top, right, last_nonzero_row + bottom);
        CHECK_CUDA(cudaMemcpy(d_main,main1->data,sizeof(Complex)*ncol*nrow_ifg,
                cudaMemcpyHostToDevice));
        multilook(d_main,d_slc2,d_ifgsec,nrow_ifg,ncol,rowlook,collook);
        cudaFree(d_slc2);
    }
    cudaFree(d_main); 
    
    // load main interferogram strip
    CHECK_CUDA(cudaMalloc((void**)&d_ifgmain,sizeof(Complex)*nrow_sm*ncol_sm));
    CHECK_CUDA(cudaMalloc((void**)&d_ifgmain_masked,
                sizeof(Complex)*nrow_sm*ncol_sm));
    CHECK_CUDA(cudaMemcpy(d_ifgmain,ifg->data+((top/rowlook-ifg->top)*ifg->ncol),
                sizeof(Complex)*ncol_sm*nrow_sm,cudaMemcpyHostToDevice));

    // apply zero mask
    num_blocks = (nrow_sm*ncol_sm + block_size - 1) / block_size;
    apply_mask<<<num_blocks, block_size>>>(d_ifgmain, d_ifgmain_masked,
            d_ifgsec, nrow_sm*ncol_sm);
    CHECK_CUDA(cudaDeviceSynchronize()); 

    // calculate the average coherence of main and secondary interferogram
    // strips
    coh_main = reduce_sum(d_ifgmain_masked, nrow_sm*ncol_sm, block_size);
    cudaFree(d_ifgmain_masked);
    coh_sec = reduce_sum(d_ifgsec, nrow_sm*ncol_sm, block_size);
    std::cout << "average coherence of the main interferogram strip: " << coh_main << std::endl;
    std::cout << "average ocherence of the secondary interferogram strip: " << coh_sec << std::endl;
    if (coh_sec > coh_main){
        replace<<<num_blocks, block_size>>>(d_ifgmain, d_ifgsec, nrow_sm*ncol_sm);
        CHECK_CUDA(cudaDeviceSynchronize()); 
        CHECK_CUDA(cudaMemcpy(ifg->data+((top/rowlook-ifg->top)*ifg->ncol),
                d_ifgmain,sizeof(Complex)*ncol_sm*nrow_sm,
                cudaMemcpyDeviceToHost));
        updated = true;
    }
    cudaFree(d_ifgsec);
    cudaFree(d_ifgmain);
    return;
}

int crossmul_sec(
             const std::string &main_slcfile1,
             const std::string &sec_slcfile1,
             const std::string &main_slcfile2,
             const std::string &sec_slcfile2,
             const std::string &intfile,
             const int rowlook, const int collook){
    // delcaration
    bool updated = false;
    int strip_idx1 = 0, strip_idx2 = 0, next_flag;
    Subswath sec1(sec_slcfile1);
    Subswath sec2(sec_slcfile2);
    Strip main1(main_slcfile1, false);
    Strip main2(main_slcfile2, false);
    Strip ifg(intfile, true);
    // end of declaration

    while (strip_idx1 < sec1.nstrip && strip_idx2 < sec2.nstrip){
        Strip strip1 = sec1.data[strip_idx1];
        Strip strip2 = sec2.data[strip_idx2];
        crossmul_strip(&strip1, &strip2, &main1, &main2, &ifg, rowlook, collook,
                next_flag, updated);
        std::cout << "strip idx1 " << strip_idx1 << ", strip_idx2 " << strip_idx2 << ", next flag " << next_flag << std::endl;
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
        std::cout << "updated" <<std::endl;
        ifg.save_data();
    }

    return 0;
}

int main(int argc, char *argv[]){
    if (argc<8){
        std::cout << "Usage: crossmul_sec main_slcfile1 sec_slcfile1 " <<
            "main_slcfile2 sec_slcfile2 intfile rowlook collook " << std::endl;
        return 0;
    }
    const std::string main_slcfile1 = std::string(argv[1]);
    const std::string sec_slcfile1 = std::string(argv[2]);
    const std::string main_slcfile2 = std::string(argv[3]);
    const std::string sec_slcfile2 = std::string(argv[4]);
    const std::string intfile = std::string(argv[5]);
    const int rowlook = std::stoi(argv[6]);
    const int collook = std::stoi(argv[7]);
    crossmul_sec(main_slcfile1,sec_slcfile1,main_slcfile2,sec_slcfile2,intfile,
            rowlook,collook);
    return 0;
}
