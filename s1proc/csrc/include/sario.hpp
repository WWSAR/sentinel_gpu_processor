#ifndef SARIO
#define SARIO

#include<complex>
#include<string>

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

void read_int(const std::string& imgfile, 
              const std::size_t n,
              int *img);

void read_int(const std::string& imgfile, 
              const std::size_t toskip,
              const std::size_t n,
              int *img);

void read_float(const std::string& imgfile, 
                const std::size_t n,
                float *img);

void read_float(const std::string& imgfile, 
                const std::size_t toskip,
                const std::size_t n,
                float *img);

void read_double(const std::string& imgfile, 
                const std::size_t n,
                double *img);
                
void read_double(const std::string& imgfile, 
                const std::size_t toskip,
                const std::size_t n,
                double *img);

void read_cpx(const std::string& imgfile,
              const std::size_t n,
              Complex *img);

void read_cpx(const std::string& imgfile,
              const std::size_t toskip,
              const std::size_t n,
              Complex *img);

void save_cpx(Complex *img,
              const std::size_t n,
              const std::string& imgfile);

void save_cpx(Complex *img,
              bool append,
              const std::size_t n,
              const std::string& imgfile);

void save_cpx(Complex *img,
              const std::size_t toskip,
              const std::size_t n,
              const std::string& imgfile);

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
#endif
