#ifndef BOUND
#define BOUND
#include <string>

#ifndef RA
#define RA 6378137.0
#endif
#ifndef RE2
#define RE2 0.00669437999015
#endif
#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

void bounds(const double start_time, const double end_time,
            const double start_rng, const double end_rng, const double hmin,
            const double hmax, double *tt, double *xx, double *vv,
            const std::size_t nstatvec, double *latlons,
            const std::string &look_dir = "RIGHT");
#endif
