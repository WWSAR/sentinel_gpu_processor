#ifndef SARIO
#define SARIO

#include <complex>
#include <fstream>
#include <iostream>
#include <string>

typedef float2 Complex;

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

void readslc(const std::string& imgfile, 
             const std::size_t naz,
             const std::size_t nrg,
             std::complex<float> *slc);

void readslc(const std::string& imgfile, 
             const std::size_t nrg,
             const std::size_t az_start,
             const std::size_t az_end,
             std::complex<float> *slc);

void saveslc(std::complex<float> *slc,
             const std::size_t nrow,
             const std::size_t ncol,
             const std::string& imgfile);

void save_int(int *img,
              const std::size_t n,
              const std::string& imgfile);

void save_int(int*img,
              bool append,
              const std::size_t n,
              const std::string& imgfile);

void save_int(int *img,
              const std::size_t toskip,
              const std::size_t n,
              const std::string& imgfile);

void save_float(float *slc,
                const std::size_t n,
                const std::string& imgfile);

void save_float(float *slc,
                bool append,
                const std::size_t n,
                const std::string& imgfile);

void save_double(double *slc,
                 const std::size_t n,
                 const std::string& imgfile);

void save_double(double *img,
                bool append,
                const std::size_t n,
                const std::string& imgfile);

void readdem(const std::string& imgfile, 
             const std::size_t n,
             short int *dem);

void readdem(const std::string& imgfile, 
             const std::size_t toskip,
             const std::size_t n,
             short int *dem);

//void read_int(const std::string& imgfile, 
//              const std::size_t n,
//              int *img);
//
//void read_int(const std::string& imgfile, 
//              const std::size_t toskip,
//              const std::size_t n,
//              int *img);
//
//void read_float(const std::string& imgfile, 
//                const std::size_t n,
//                float *img);
//
//void read_float(const std::string& imgfile, 
//                const std::size_t toskip,
//                const std::size_t n,
//                float *img);
//
//void read_double(const std::string& imgfile, 
//                const std::size_t n,
//                double *img);
//                
//void read_double(const std::string& imgfile, 
//                const std::size_t toskip,
//                const std::size_t n,
//                double *img);
//
//void read_cpx(const std::string& imgfile,
//              const std::size_t n,
//              Complex *img);
//
//void read_cpx(const std::string& imgfile,
//              const std::size_t toskip,
//              const std::size_t n,
//              Complex *img);
//
//void save_cpx(Complex *img,
//              const std::size_t n,
//              const std::string& imgfile);
//
//void save_cpx(Complex *img,
//              bool append,
//              const std::size_t n,
//              const std::string& imgfile);
//
//void save_cpx(Complex *img,
//              const std::size_t toskip,
//              const std::size_t n,
//              const std::string& imgfile);

void read_polynomials(const std::string& fname,
                      int& n,
                      double **t,
                      double **t0,
                      double **p0,
                      double **p1,
                      double **p2);

void read_param_file(const std::string& fname,
                     std::string& dem_fname,
                     std::string& rsc_fname);

void read_and_resample(
        const std::string& filename,
        Complex* dst,
        const int left_dst,
        const int top_dst,
        const int right_dst,
        const int bottom_dst,
        const int row_begin,
        const int row_end);

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

#endif
