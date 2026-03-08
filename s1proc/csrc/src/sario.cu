#include <algorithm>
#include <complex>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <string>

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
    //std::cout << "dem to skip: " << toskip << std::endl;
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

void read_param_file(const std::string& fname,
                     std::string& dem_fname,
                     std::string& rsc_fname){
    std::ifstream fin(fname);
    if (!fin.is_open()){
        std::cerr << "Error: Could not open file " << fname << std::endl;
    }
    fin >> dem_fname;
    fin >> rsc_fname;
    fin.close();
    return;
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

    /* read header */
    int32_t head[64];
    fin.read(reinterpret_cast<char*>(head), sizeof(head));

    //int nrow0 = head[0];
    //int ncol0 = head[1];

    int left_src   = head[2];
    int top_src    = head[3];
    int right_src  = head[4];
    int bottom_src = head[5];

    int src_w = right_src - left_src;
    //int src_h = bottom_src - top_src;

    int dst_w = right_dst - left_dst;

    const size_t header_bytes = 64 * sizeof(int32_t);

    /* overlapping columns */
    int overlap_left  = std::max(left_dst, left_src);
    int overlap_right = std::min(right_dst, right_src);

    if (overlap_left >= overlap_right)
        return;

    int copy_w = overlap_right - overlap_left;
    int src_col0 = overlap_left - left_src;
    int dst_col0 = overlap_left - left_dst;

    for (int r = row_begin; r < row_end; ++r)
    {
        int global_row = top_dst + r;

        if (global_row < top_src || global_row >= bottom_src)
            continue;

        int src_row = global_row - top_src;

        size_t offset =
            header_bytes +
            ((size_t)src_row * src_w + src_col0) * sizeof(Complex);

        fin.seekg(offset, std::ios::beg);

        fin.read(reinterpret_cast<char*>(
                 dst + (size_t)(r - row_begin)*dst_w + dst_col0),
                 copy_w * sizeof(Complex));
    }

    fin.close();
}
