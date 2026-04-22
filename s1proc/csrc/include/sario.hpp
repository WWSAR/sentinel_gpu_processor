#ifndef SARIO
#define SARIO

#include <complex>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

typedef float2 Complex;
const int NHEADER = 64;

// io functions
void read_polynomials(const std::string& fname,
                      int& n,
                      double **t,
                      double **t0,
                      double **p0,
                      double **p1,
                      double **p2);


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


/**
 read data from a file and resample the data to a destination grid
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
template<typename T>
void read_and_resample(
    const std::string& filename,
    T* dst,
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
    T* buffer = new T[(size_t)src_w * copy_h];

    size_t offset =
        header_bytes +
        (size_t)src_row0 * src_w * sizeof(T);

    fin.seekg(offset, std::ios::beg);

    fin.read(reinterpret_cast<char*>(buffer),
        (size_t)src_w * copy_h * sizeof(T));
    
    if (!fin) {
        delete[] buffer;
        throw std::runtime_error("Failed to read data from file");
    }
    
    fin.close();

    /* crop in memory */
    for (int r = 0; r < copy_h; ++r)
    {
        T* src_ptr = buffer + (size_t)r * src_w + src_col0;
        T* dst_ptr = dst + (size_t)(dst_row0 + r - row_begin) * dst_w + dst_col0;
        std::memcpy(dst_ptr, src_ptr, copy_w * sizeof(T));
    }

    delete[] buffer;
}

template<typename T>
void read_and_resample(
    const std::string& filename,
    T* dst,
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
    
    read_and_resample<T>(filename, dst, 
                         left_src, top_src, right_src, bottom_src,
                         0, left_dst, top_dst, right_dst, bottom_dst, 
                         row_begin, row_end);
}

void write_compressed_strips(
        int32_t* header,
        const float2* img,
        const int nrow,
        const int ncol,
        const int top,
        const std::string& outfile);

// classes
template<typename T>
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
    T *data;

    Strip(const int nrow0_, const int ncol0_,
          const int left_, const int top_,
          const int right_, const int bottom_,
          const int start_line_,
          const std::string &fname_,
          T* data_);

    Strip(const std::string &fname_, bool load_data_);

    Strip& operator=(const Strip& other) {
        if (this != &other) {
            nrow0 = other.nrow0;
            ncol0 = other.ncol0;
            left = other.left;
            top = other.top;
            right = other.right;
            bottom = other.bottom;
            start_line = other.start_line;
            nrow = other.nrow;
            ncol = other.ncol;
            fname = other.fname;
            data = other.data;  // shallow copy (probably NOT what you want)
        }
        return *this;
    }

    Strip(const Strip& other)
    : nrow0(other.nrow0), ncol0(other.ncol0),
      left(other.left), top(other.top),
      right(other.right), bottom(other.bottom),
      start_line(other.start_line),
      nrow(other.nrow), ncol(other.ncol),
      fname(other.fname),
      data(other.data)
    {}

    // Move constructor
    Strip(Strip&& other) noexcept
        : nrow0(other.nrow0), ncol0(other.ncol0),
          left(other.left), top(other.top),
          right(other.right), bottom(other.bottom),
          start_line(other.start_line),
          nrow(other.nrow), ncol(other.ncol),
          fname(std::move(other.fname)),
          data(other.data)
    {
        other.data = nullptr;
    }

    void load_data(const int left, const int top, const int right,
            const int bottom);

    void save_data();

};


template<typename T>
Strip<T>::Strip(const int nrow0_, const int ncol0_,
                const int left_, const int top_,
                const int right_, const int bottom_,
                const int start_line_,
                const std::string &fname_,
                T* data_
               ):nrow0(nrow0_),
                 ncol0(ncol0_),
                 left(left_),
                 top(top_),
                 right(right_),
                 start_line(start_line_),
                 bottom(bottom_),
                 fname(fname_),
                 data(data_)
{
    nrow = bottom - top;
    ncol = right - left;
}

template<typename T>
Strip<T>::Strip(const std::string &fname_, bool load_data_)
    : fname(fname_), data(nullptr)
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
    
    if (load_data_) {
        load_data(left, top, right, bottom);
    }
}

template<typename T>
void Strip<T>::load_data(const int left, const int top,
        const int right, const int bottom){
    if (this->data){
        free(this->data);
        this->data = nullptr;
    }
    this->data = (T *)calloc((bottom-top)*(right-left), sizeof(T));
    read_and_resample<T>(this->fname, this->data,
            this->left, this->top, this->right, this->bottom,
            this->start_line, left, top, right, bottom, 0, bottom-top);
    return;
}

template<typename T>
void Strip<T>::save_data(){
    if (!this->data){
        printf("Cannot save empty data.\n");
        return;
    }
    std::int32_t header[64];
    header[0] = this->nrow0;
    header[1] = this->ncol0;
    header[2] = this->left;
    header[3] = this->top;
    header[4] = this->right;
    header[5] = this->bottom;
    
    save_binary<T>(this->data, this->nrow * this->ncol, header,
                   NHEADER, this->fname);
}

template<typename T>
class Subswath
{
    public:
    int nrow0;
    int ncol0;
    int left;
    int right;
    int nstrip;
    std::string fname;
    std::vector<Strip<T>> data;
    Subswath(const std::string &fname_);
};

template<typename T>
Subswath<T>::Subswath(const std::string &fname_): fname(fname_)
{
    int start_line = 0;
    std::ifstream fin(fname_, std::ios::binary);
    if (!fin)
        throw std::runtime_error("Cannot open file");
    
    int32_t header[64];
    fin.read(reinterpret_cast<char*>(header), sizeof(header));
    fin.close();
    
    nrow0 = header[0];
    ncol0 = header[1];
    left = header[2];
    right = header[3];
    nstrip = header[4];
    
    for (int i = 0; i < nstrip; i++) {
        int top = header[5 + 2*i];
        int bottom = header[6 + 2*i];
        
        Strip<T> s(nrow0, ncol0, left, top, right, bottom, start_line,
                   fname, nullptr);
        data.push_back(s);
        start_line += (bottom - top);
    }
}

// rsc structure
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

// CUDA functions

__global__
void conj_mul(Complex *a, Complex *b, Complex *c, const std::size_t n);

__global__
void point_power(Complex *a, float *b, const std::size_t n);

__global__
void point_sqrt(float *a, float *b, const std::size_t n);

__global__
void point_angle(Complex *z, float *phase, const std::size_t n);

__global__
void cpx_col_look(Complex *a, Complex *b, const int collook,
                  const std::size_t ncol, const std::size_t n);

__global__
void cpx_row_look(Complex *a, Complex *b, const int rowlook,
                  const std::size_t ncol, const std::size_t n);

__global__
void col_look(float *a, float *b, const int collook,
              const std::size_t ncol, const std::size_t n);

__global__
void row_look(float *a, float *b, const int rowlook,
              const std::size_t ncol, const std::size_t n);

#endif
