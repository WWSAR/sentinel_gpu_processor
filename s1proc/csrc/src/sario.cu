#include <algorithm>
#include <complex>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>

#include "sario.hpp"

Strip::Strip(const int nrow0_, const int ncol0_,
      const int left_, const int top_,
      const int right_, const int bottom_,
      const int start_line_,
      std::string &fname_,
      Complex* data_
     ):nrow0(nrow0_),
       ncol0(ncol0_),
       left(left_),
       top(top_),
       right(right_),
       start_line(start_line_),
       bottom(bottom_),
       fname(fname_),
       data(std::move(data_))
{
    nrow = bottom - top;
    ncol = right - left;
}

Strip::Strip(
      const std::string &fname_,
      bool load_data_):fname(fname_)
{
    std::ifstream fin(fname_, std::ios::binary);
    if (!fin)
        throw std::runtime_error("Cannot open file");
    int32_t header[64];
    fin.read(reinterpret_cast<char*>(header), sizeof(header));
    fin.close();
    nrow0 = header[0];
    ncol0 = header[1];
    left = header[2];
    top = header[3];
    right = header[4];
    bottom = header[5];
    start_line = 0;
    nrow = bottom - top;
    ncol = right - left;
    data = nullptr;
    if (load_data_){
        load_data(left, top, right, bottom);
    }else{
        data = nullptr;
    }
}

void Strip::load_data(const int left, const int top,
        const int right, const int bottom){
    if (this->data){
        free(this->data);
        this->data = nullptr;
    }
    this->data = (Complex *)malloc(sizeof(Complex)*(bottom-top)*(right-left));
    read_and_resample(this->fname, this->data,
            this->left, this->top, this->right, this->bottom,
            this->start_line, left, top, right, bottom, 0, bottom-top);
    return;
}

void Strip::save_data(){
    if (!this->data){
        printf("Cannot save emtpy data.\n");
        return;
    }
    std::int32_t header[64];
    header[0] = this->nrow0;
    header[1] = this->ncol0;
    header[2] = this->left;
    header[3] = this->top;
    header[4] = this->right;
    header[5] = this->bottom;
    save_binary<Complex>(this->data,this->nrow*this->ncol,header,
            NHEADER,"temp.int");
    return;
}

Subswath::Subswath(const std::string &fname_): fname(fname_)
{
    int start_line = 0;
    std::vector<Strip> data_ = {}; 
    std::ifstream fin(fname_, std::ios::binary);
    if (!fin)
        throw std::runtime_error("Cannot open file");
    int32_t header[64];
    fin.read(reinterpret_cast<char*>(header), sizeof(header));
    nrow0 = header[0];
    ncol0 = header[1];
    left = header[2];
    right = header[3];
    nstrip = header[4];
    for (int i = 0; i < nstrip; i++){
        int top = header[5 + 2*i], bottom = header[6 + 2*i];
        Strip s(nrow0, ncol0, left, top, right, bottom, start_line,
                fname, NULL); 
        data.push_back(s);
        start_line = start_line + bottom - top; 
    }
}

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
    for(int i=0; i<6; ++i){
        std::getline(fin, line);
        auto pos = line.find(" ");
        if (pos == std::string::npos){
            pos = line.find("\t");
        }
        switch (i){
            case 0 :
                nlon = std::stoi(line.substr(pos+1));
                break;
            case 1 :
                nlat = std::stoi(line.substr(pos+1));
                break;
            case 2 :
                lonmin = std::stod(line.substr(pos+1));
                break;
            case 3 :
                latmax = std::stod(line.substr(pos+1));
                break;
            case 4 :
                dlon = std::stod(line.substr(pos+1));
                break;
            case 5 :
                dlat = std::stod(line.substr(pos+1));
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
 read Complex data from a file and resample the data to a destination grid
 * @param filename data file to read
 * @param dst pointer to the stored data
 * @param left_src left boundary of the source image
 * @param top_src top boundary of the source image
 * @param right_src left boundary of the source image
 * @param bottom_src bottom boundary of the source image
 * @param offset_rows number of rows to skip when reading from the data file
 * @param left_dst left boundary of the destination image
 * @param top_dst top boundary of the destination image
 * @param right_dst left boundary of the destination image
 * @param bottom_dst bottom boundary of the destination image
 * @param row_begin first line to write to the destination image
 * @param row_end last line to write to the destination image
*/
void read_and_resample(
    const std::string& filename,
    Complex* dst,
    const int left_src,
    const int top_src,
    const int right_src,
    const int bottom_src,
    const int offset_rows,
    const int left_dst,
    const int top_dst,
    const int right_dst,
    const int bottom_dst,
    const int row_begin,
    const int row_end)
{
    std::ifstream fin(filename, std::ios::binary);
    if (!fin)
        throw std::runtime_error("Cannot open file");

    const size_t header_bytes = 64 * sizeof(int32_t);
    int src_w = right_src - left_src;
    int dst_w = right_dst - left_dst;
    int overlap_left  = std::max(left_dst, left_src);
    int overlap_right = std::min(right_dst, right_src);

    if (overlap_left >= overlap_right)
        return;

    int copy_w = overlap_right - overlap_left;

    int src_col0 = overlap_left - left_src;
    int dst_col0 = overlap_left - left_dst;

    int overlap_top    = std::max(top_dst + row_begin, top_src);
    int overlap_bottom = std::min(top_dst + row_end, bottom_src);

    if (overlap_top >= overlap_bottom)
        return;

    int copy_h = overlap_bottom - overlap_top;

    int src_row0 = overlap_top - top_src + offset_rows;
    int dst_row0 = overlap_top - top_dst;

    /* read full rows */
    Complex* buffer = new Complex[(size_t)src_w * copy_h];

    size_t offset =
        header_bytes +
        (size_t)src_row0 * src_w * sizeof(Complex);

    fin.seekg(offset, std::ios::beg);

    fin.read(reinterpret_cast<char*>(buffer),
        (size_t)src_w * copy_h * sizeof(Complex));
    fin.close();

    /* crop in memory */
    for (int r = 0; r < copy_h; ++r)
    {
        Complex* src_ptr = buffer + (size_t)r * src_w + src_col0;

        Complex* dst_ptr =
            dst + (size_t)(dst_row0 + r - row_begin) * dst_w + dst_col0;

        std::memcpy(dst_ptr, src_ptr, copy_w * sizeof(Complex));
    }

    delete[] buffer;
}

void read_and_resample(
    const std::string& filename,
    Complex* dst,
    const int left_dst,
    const int top_dst,
    const int right_dst,
    const int bottom_dst,
    const int row_begin,
    const int row_end)
{
    std::ifstream fin(filename, std::ios::binary);
    if (!fin)
        throw std::runtime_error("Cannot open file");

    int32_t head[64];
    fin.read(reinterpret_cast<char*>(head), sizeof(head));
    fin.close();

    int left_src   = head[2];
    int top_src    = head[3];
    int right_src  = head[4];
    int bottom_src = head[5];
    read_and_resample(filename, dst, left_src, top_src, right_src, bottom_src,
            0, left_dst, top_dst, right_dst, bottom_dst, row_begin, row_end);
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
        fs.seekg(0, std::ios::beg);
        fs.read(reinterpret_cast<char*>(header), 64 * sizeof(int32_t));
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
        int end_row = nrow;
        if (nstrip > 0 && start_row == header[6 + 2 * (nstrip - 1)]) {
            header[6 + 2 * (nstrip - 1)] = end_row;
        } else {
            header[5 + 2 * nstrip] = start_row;
            header[6 + 2 * nstrip] = end_row;
            nstrip++;
        }
        size_t strip_size = (size_t)(end_row - start_row) * ncol * sizeof(float2);
        fs.write(reinterpret_cast<const char*>(&img[start_row * ncol]), strip_size);
    }

    // --- update the header ---
    fs.seekp(0, std::ios::beg);
    fs.write(reinterpret_cast<const char*>(header), 64 * sizeof(int32_t));
    fs.close();
}
