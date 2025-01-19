#ifndef ORBIT
#define ORBIT

#include<string>

#ifndef RA
        #define RA 6378137.0
#endif
#ifndef RE2
        #define RE2 0.00669437999015
#endif

__host__ __device__
void llh2xyz(
        double llh[3],
        double xyz[3],
        const double r_a = RA,
        const double r_e2 = RE2);

__host__ __device__
void xyz2llh(
        double *xyz,
        double *llh,
        const double r_a = RA,
        const double r_e2 = RE2);

__host__ __device__
void xyz2llh(
        float *xyz,
        double *llh,
        const double r_a = RA,
        const double r_e2 = RE2);

void read_orbit(
        const std::string orbitfile,
        size_t &nstatvec,
        double **t,
        double **x,
        double **v);

void read_orbit_ascii(
        const std::string orbitfile,
        size_t &nstatvec,
        double **t,
        double **x,
        double **v);

__host__ __device__
void intp_orbit(
        const std::size_t nstatvec,
        double *timeorbit,
        double *xx,
        double *vv,
        const double t,
        double *satx,
        double *satv);

__host__ __device__
void orbitrangetime(
        const std::size_t nstatvec,
        double *timeorbit,
        double *xx,
        double *vv,
        double *xyz,
        const double tline0,
        double *satx0,
        double *satv0,
        double& tline,
        double *dr);

#endif
