#ifndef SARIO
#define SARIO

#include <complex>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

typedef float2 Complex;
const int NHEADER = 64;

struct rsc{
    int nlat;
    int nlon;
    double dlat;
    double dlon;
    double lonmin;
    double lonmax;
    double latmin;
    double latmax;
};

rsc readrsc(const std::string& rscfile);

void read_polynomials(const std::string& fname,
                      int& n,
                      double **t,
                      double **t0,
                      double **p0,
                      double **p1,
                      double **p2);

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
    const int row_end);

void read_and_resample(
        const std::string& filename,
        Complex* dst,
        const int left_dst,
        const int top_dst,
        const int right_dst,
        const int bottom_dst,
        const int row_begin,
        const int row_end);

void write_compressed_strips(
        int32_t* header,
        const float2* img,
        const int nrow,
        const int ncol,
        const int top,
        const std::string& outfile);

template <typename T>
void read_binary(const std::string& imgfile,
                 const size_t width,
                 const size_t row_start,
                 const size_t row_end,
                 const size_t col_start,
                 const size_t col_end,
                 T* img)
{
    std::ifstream fin(imgfile, std::ios::binary);

    if (!fin) {
        printf("File %s does not exist.\n", imgfile.c_str());
        return;
    }

    size_t sub_width  = col_end - col_start;
    size_t sub_height = row_end - row_start;

    for (size_t r = 0; r < sub_height; r++) {

        size_t file_offset =
        ((row_start + r) * width + col_start) * sizeof(T);

        fin.seekg(file_offset, std::ios::beg);

        fin.read(reinterpret_cast<char*>(img + r * sub_width),
        sub_width * sizeof(T));
    }

    fin.close();
}

template <typename T>
void read_binary(const std::string& imgfile,
                 const std::size_t toskip,
                 const std::size_t n,
                 T* img)
{
    std::ifstream fin(imgfile, std::ios::binary);

    if (!fin) {
        printf("File %s does not exist.\n", imgfile.c_str());
        return;
    }
    fin.seekg(toskip * sizeof(T), std::ios::beg);
    fin.read(reinterpret_cast<char*>(img), sizeof(T) * n);
    fin.close();
}

template <typename T>
void read_binary(
        const std::string& imgfile,
        const std::size_t n,
        T* img)
{
    read_binary<T>(imgfile, 0, n, img);
}

template <typename T>
void save_binary(
        T* img,
        bool append,
        const std::size_t n,
        const std::string& imgfile)
{
    std::ofstream fout;

    if (append) {
        fout.open(imgfile, std::ios::app | std::ios::binary);
    } else {
        fout.open(imgfile, std::ios::out | std::ios::binary);
    }

    if (!fout.is_open()) {
        printf("Unable to open file %s\n", imgfile.c_str());
        return;
    }

    fout.write(reinterpret_cast<char*>(img), sizeof(T) * n);
    fout.close();
}

template <typename T>
void save_binary(
        const T* img,
        const std::size_t toskip,
        const std::size_t n,
        const std::string& imgfile)
{
    std::fstream fout;

    fout.open(imgfile, std::ios::binary | std::ios::out | std::ios::in);

    if (!fout.is_open()) {
        printf("Unable to open file %s\n", imgfile.c_str());
        return;
    }

    fout.seekp(toskip * sizeof(T), std::ios::beg);
    fout.write(reinterpret_cast<const char*>(img), sizeof(T) * n);
    fout.close();
}

template <typename T>
void save_binary(
        T* img,
        const std::size_t n,
        const std::string& imgfile)
{
    save_binary<T>(img, false, n, imgfile);
}

template <typename T>
void save_binary(
        const T* img,
        const std::size_t n,
        const std::int32_t* header,
        const std::size_t nheader,
        const std::string& imgfile)
{
    std::ofstream fout;

    fout.open(imgfile, std::ios::out | std::ios::binary);

    if (!fout.is_open()) {
        printf("Unable to open file %s\n", imgfile.c_str());
        return;
    }
    fout.write(reinterpret_cast<const char*>(header),
            sizeof(std::int32_t)*nheader);
    fout.write(reinterpret_cast<const char*>(img), sizeof(T) * n);
    fout.close();
}

class Strip
{
    public:
    int nrow0;
    int ncol0;
    int left;
    int top;
    int right;
    int bottom;
    int start_line;

    int nrow;
    int ncol;
    std::string fname;
    Complex *data;

    Strip(const int nrow0_, const int ncol0_,
          const int left_, const int top_,
          const int right_, const int bottom_,
          const int start_line_,
          std::string &fname_,
          Complex* data_);

    Strip(const std::string &fname_, bool load_data_);

    void load_data(const int left, const int top, const int right,
            const int bottom);

    void save_data();
};

class Subswath
{
    public:
    int nrow0;
    int ncol0;
    int left;
    int right;
    int nstrip;
    std::string fname;
    std::vector<Strip> data;
    Subswath(const std::string &fname_);
};

#endif
