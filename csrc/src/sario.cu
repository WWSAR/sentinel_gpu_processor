#include <algorithm>
#include <complex>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <sstream>

#include "sario.hpp"

rsc readrsc(const std::string& rscfile){
    std::string line;
    int nlat, nlon;
    double dlat, dlon, lonmin, lonmax, latmin, latmax;
    std::ifstream fin(rscfile);
    rsc dem_rsc;
    if (!fin.is_open()){
        printf("Unable to open file %s\n",rscfile.c_str());
        exit(-1);
    }
    while (std::getline(fin, line)){
        // split line into words
        std::istringstream iss(line);
        std::string word;
        iss >> word;
        if (word == "WIDTH"){
            iss >> word;
            nlon = std::stoi(word);
        }else if (word == "FILE_LENGTH"){
            iss >> word;
            nlat = std::stoi(word);
        }else if (word == "X_FIRST"){
            iss >> word;
            lonmin = std::stod(word);
        }else if (word == "Y_FIRST"){
            iss >> word;
            latmax = std::stod(word);
        }else if (word == "X_STEP"){
            iss >> word;
            dlon = std::stod(word);
        }else if (word == "Y_STEP"){
            iss >> word;
            dlat = std::stod(word);
        }else{
            continue;
        }
    }
    lonmax = lonmin + (nlon - 1) * dlon;
    latmin = latmax + (nlat - 1) * dlat;
    dem_rsc.nlat = nlat;
    dem_rsc.nlon = nlon;
    dem_rsc.dlat = dlat;
    dem_rsc.dlon = dlon;
    dem_rsc.lonmin = lonmin;
    dem_rsc.lonmax = lonmax;
    dem_rsc.latmin = latmin;
    dem_rsc.latmax = latmax;
    return dem_rsc;
}

void readdem(const std::string& imgfile, 
             const std::size_t toskip,
             const std::size_t n,
             short int *dem){
    std::ifstream fin(imgfile, std::ios::binary);
    fin.seekg(toskip*sizeof(short int),std::ios::beg);
    if (!fin){
        printf("File %s does not exist.\n",imgfile.c_str());
        return;
    }
    fin.read((char *)dem, sizeof(short int)*n);
    fin.close();
    return;
}

void readdem(const std::string& imgfile, 
             const std::size_t n,
             short int *dem){
    readdem(imgfile,0,n,dem);
    return;
}

void read_polynomials(const std::string& fname,
                      int& n,
                      double **t,
                      double **t0,
                      double **p0,
                      double **p1,
                      double **p2){
    double *t_loc, *t0_loc, *p0_loc, *p1_loc, *p2_loc;
    std::ifstream fin(fname);
    if (!fin.is_open()){
        std::cerr << "Error: Could not open file!" << std::endl;
    }
    fin >> n;
    t_loc = (double*)malloc(sizeof(double)*n);
    t0_loc = (double*)malloc(sizeof(double)*n);
    p0_loc = (double*)malloc(sizeof(double)*n);
    p1_loc = (double*)malloc(sizeof(double)*n);
    p2_loc = (double*)malloc(sizeof(double)*n);
    for (int i = 0; i < n; ++i){
        fin >> t_loc[i] >> t0_loc[i] >> p0_loc[i] >> p1_loc[i] >> p2_loc[i];
    }
    fin.close();
    *t = t_loc;
    *t0 = t0_loc;
    *p0 = p0_loc;
    *p1 = p1_loc;
    *p2 = p2_loc;
    return;
}

/**
 * @param header Array of 64 int32 elements
 * @param img pointer to the image
 * @param nrow number of rows
 * @param ncol number of columns
 * @param top index of the first row in the original large image
 * @param outfile output file
 */
void write_compressed_strips(
        int32_t* header,
        const float2* img,
        const int nrow,
        const int ncol,
        const int top,
        const std::string& outfile) {
    int& nstrip = header[4];
    std::fstream fs;

    if (nstrip == 0) {
        // --- Case 1: Create a new file ---
        fs.open(outfile, std::ios::binary | std::ios::out | std::ios::trunc);
        if (!fs) return;
        fs.write(reinterpret_cast<const char*>(header), 64 * sizeof(int32_t));
    } else {
        // --- Case 2: Write to an existing file ---
        fs.open(outfile, std::ios::binary | std::ios::out | std::ios::in);
        if (!fs) return;
        // Read the latest header
        //fs.seekg(0, std::ios::beg);
        //fs.read(reinterpret_cast<char*>(header), 64 * sizeof(int32_t));
        // Move to the end to append new data
        fs.seekp(0, std::ios::end);
    }

    bool in_strip = false;
    int start_row = 0;

    for (int i = 0; i < nrow; ++i) {
        bool row_has_data = false;
        for (int j = 0; j < ncol; ++j) {
            float2 val = img[i * ncol + j];
            if (val.x != 0.0f || val.y != 0.0f) {
                row_has_data = true;
                break;
            }
        }

        if (!in_strip && row_has_data) {
            in_strip = true;
            start_row = i + top;
        } 
        else if (in_strip && !row_has_data) {
            int end_row = i + top;
            
            // --- stitch  ---
            // If there exist previous strips and the first row of current
            // strip equals the last row of previous strip, stitch them 
            if (nstrip > 0 && start_row == header[6 + 2 * (nstrip - 1)]) {
                // update the last row of previous strip
                header[6 + 2 * (nstrip - 1)] = end_row;
            } else {
                // write new indices for the created strip
                header[5 + 2 * nstrip] = start_row;
                header[6 + 2 * nstrip] = end_row;
                nstrip++;
            }

            // write the data block
            size_t strip_size = (size_t)(end_row - start_row) * ncol * sizeof(float2);
            fs.write(reinterpret_cast<const char*>(
                        &img[(start_row-top) * ncol]), strip_size);
            
            in_strip = false;
        }
    }

    // In case the last row of the input image has data
    if (in_strip) {
        int end_row = nrow + top;
        if (nstrip > 0 && start_row == header[6 + 2 * (nstrip - 1)]) {
            header[6 + 2 * (nstrip - 1)] = end_row;
        } else {
            header[5 + 2 * nstrip] = start_row;
            header[6 + 2 * nstrip] = end_row;
            nstrip++;
        }
        size_t strip_size = (size_t)(end_row - start_row) * ncol * sizeof(float2);
        fs.write(reinterpret_cast<const char*>(&img[(start_row-top)* ncol]), strip_size);
    }

    // --- update the header ---
    fs.seekp(0, std::ios::beg);
    fs.write(reinterpret_cast<const char*>(header), 64 * sizeof(int32_t));
    fs.close();
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
void point_power(Complex *a, float *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex ai;
    for (std::size_t i = index; i < n; i += stride){
        ai = a[i];
        b[i] = ai.x*ai.x+ai.y*ai.y;
    }
}

__global__
void point_sqrt(float *a, float *b, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    for (std::size_t i = index; i < n; i += stride){
        b[i] = sqrtf(a[i]);
    }
}

__global__
void point_angle(Complex* z, float* phase, const std::size_t n) {
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    Complex zi;
    for (std::size_t i = index; i < n; i += stride){
        zi = z[i];
        phase[i] = atan2f(zi.y, zi.x);
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
        row = i / ncol_sm;
        col = i - row*ncol_sm;
        idx0 = row*ncol+col*collook;
        for (std::size_t j = 0; j < collook; j++) {
            temp = a[idx0+j];
            sum.x = sum.x + temp.x;
            sum.y = sum.y + temp.y;
        }
        sum.x = sum.x/collook;
        sum.y = sum.y/collook;
        b[i] = sum;
    }
}

__global__
void cpx_row_look(Complex *a, Complex *b, const int rowlook,
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
void col_look(float *a, float *b, const int collook,
              const std::size_t ncol, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    std::size_t ncol_sm = ncol/collook, row, col, idx0;
    float sum;
    for (std::size_t i = index; i < n; i += stride){
        row = i/ncol_sm;
        col = i - row*ncol_sm;
        idx0 = row*ncol+col*collook;
        sum = 0.;
        for (std::size_t j = 0; j < collook; j++) {
            sum += a[idx0+j];
        }
        b[i] = sum/collook;
    }
}

__global__
void row_look(float *a, float *b, const int rowlook,
              const std::size_t ncol, const std::size_t n){
    std::size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    std::size_t stride = blockDim.x * gridDim.x;
    std::size_t row,col,idx0;
    float sum;

    for (std::size_t i = index; i < n; i += stride){
        row = i/ncol;
        col = i%ncol;
        idx0 = row*rowlook*ncol+col;
        sum = 0.;
        for (std::size_t j = 0; j < rowlook; ++j) {
            sum += a[idx0+j*ncol];
        }
        b[i] = sum/rowlook;
    }
}
