#include "bounds.hpp"
#include "orbit.hpp"
#include <algorithm>
#include <cmath>
#include <iostream>

double min_el(const double x[8]) {
  double xmin = x[0];
  for (int i = 1; i < 8; ++i) {
    if (xmin > x[i]) {
      xmin = x[i];
    }
  }
  return xmin;
}

double max_el(const double x[8]) {
  double xmax = x[0];
  for (int i = 1; i < 8; ++i) {
    if (xmax < x[i]) {
      xmax = x[i];
    }
  }
  return xmax;
}

double radians(double degrees) { return degrees * M_PI / 180.0; }

double degrees(double radians) { return radians * 180.0 / M_PI; }

double sign(double value) { return (value > 0) - (value < 0); }

double reast(double r_lat, double r_a = RA, double r_e2 = RE2) {
  return r_a / sqrt(1.0 - r_e2 * pow(sin(r_lat), 2));
}

double rnorth(double r_lat, double r_a = RA, double r_e2 = RE2) {
  return r_a * (1.0 - r_e2) / pow(1.0 - r_e2 * pow(sin(r_lat), 2), 1.5);
}

double rdir(double r_lat, double hdg, double r_a = RA, double r_e2 = RE2) {
  double re = reast(r_lat, r_a, r_e2);
  double rn = rnorth(r_lat, r_a, r_e2);
  return (re * rn) / (re * pow(cos(hdg), 2) + rn * pow(sin(hdg), 2));
}

double norm(double *x) { return sqrt(x[0] * x[0] + x[1] * x[1] + x[2] * x[2]); }

void normalize(double vec[3]) {
  double n = sqrt(vec[0] * vec[0] + vec[1] * vec[1] + vec[2] * vec[2]);
  for (int i = 0; i < 3; ++i)
    vec[i] /= n;
}

double dot_product(const double a[3], const double b[3]) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

void cross_product(const double a[3], const double b[3], double c[3]) {
  c[0] = a[1] * b[2] - a[2] * b[1];
  c[1] = a[2] * b[0] - a[0] * b[2];
  c[2] = a[0] * b[1] - a[1] * b[0];
  return;
}

void tcnbasis(double pos[3], double vel[3], double r_t[3], double r_c[3],
              double r_n[3], const std::string &look_dir = "RIGHT",
              double r_a = RA, double r_e2 = RE2) {
  double llh[3];
  xyz2llh(pos, llh, r_a, r_e2);
  double r_lat = llh[0] * M_PI / 180.0;
  double r_lon = llh[1] * M_PI / 180.0;

  r_n[0] = -std::cos(r_lat) * std::cos(r_lon);
  r_n[1] = -std::cos(r_lat) * std::sin(r_lon);
  r_n[2] = -std::sin(r_lat);

  double r_temp[3];
  if (look_dir == "RIGHT") {
    cross_product(r_n, vel, r_temp);
  } else if (look_dir == "LEFT") {
    cross_product(vel, r_n, r_temp);
  } else {
    std::cerr << "unrecognized look direction: " << look_dir << std::endl;
  }

  std::copy(r_temp, r_temp + 3, r_c);
  normalize(r_c);
  cross_product(r_c, r_n, r_temp);
  std::copy(r_temp, r_temp + 3, r_t);
  normalize(r_t);
}

double spherical_hdg(double *ll1, double *ll2) {
  double r_sinlati = sin(radians(ll1[0]));
  double r_coslati = cos(radians(ll1[0]));
  double r_tanlatf = tan(radians(ll2[0]));

  double r_t1 = radians(ll2[1]) - radians(ll1[1]);
  if (fabs(r_t1) > M_PI) {
    r_t1 = (2.0 * M_PI - fabs(r_t1)) * copysign(1.0, -r_t1);
  }
  double r_sinlon = sin(r_t1);
  double r_coslon = cos(r_t1);

  double r_t2 = r_coslati * r_tanlatf - r_sinlati * r_coslon;
  double r_geohdg = atan2(r_sinlon, r_t2);

  return r_geohdg;
}

double ellipsoidal_hdg(double *ll1, double *ll2, double r_a = RA,
                       double r_e2 = RE2) {
  double r_e = sqrt(r_e2);
  double r_sqrtome2 = sqrt(1.0 - r_e2);
  double r_b0 = r_a * r_sqrtome2;
  double r_f = 1.0 - r_sqrtome2;
  double r_ep = r_e * r_f / (r_e2 - r_f);
  double r_n = r_f / r_e2;
  double r_k1 = (16.0 * r_e2 * r_n * r_n + r_ep * r_ep) / (r_ep * r_ep);
  double r_k2 =
      (16.0 * r_e2 * r_n * r_n) / (16.0 * r_e2 * r_n * r_n + r_ep * r_ep);
  double r_k3 = (16.0 * r_e2 * r_n * r_n) / (r_ep * r_ep);
  double r_k4 =
      (16.0 * r_n - r_ep * r_ep) / (16.0 * r_e2 * r_n * r_n + r_ep * r_ep);
  double r_k5 = 16.0 / (r_e2 * (16.0 * r_e2 * r_n * r_n + r_ep * r_ep));

  double r_tanlati = tan(radians(ll1[0]));
  double r_tanlatf = tan(radians(ll2[0]));
  double r_l = fabs(radians(ll2[1]) - radians(ll1[1]));
  double r_lsign = radians(ll2[1]) - radians(ll1[1]);
  double r_sinlon = sin(r_l);
  double r_coslon = cos(r_l);

  double r_tanbetai = r_sqrtome2 * r_tanlati;
  double r_tanbetaf = r_sqrtome2 * r_tanlatf;

  double r_cosbetai = 1.0 / sqrt(1.0 + r_tanbetai * r_tanbetai);
  double r_cosbetaf = 1.0 / sqrt(1.0 + r_tanbetaf * r_tanbetaf);
  double r_sinbetai = r_tanbetai * r_cosbetai;
  double r_sinbetaf = r_tanbetaf * r_cosbetaf;

  double r_ac = r_sinbetai * r_sinbetaf;
  double r_bc = r_cosbetai * r_cosbetaf;

  double r_cosphi = r_ac + r_bc * r_coslon;
  double r_sinphi =
      sign(r_sinlon) * sqrt(1.0 - std::min(r_cosphi * r_cosphi, 1.0));

  double r_phi = fabs(atan2(r_sinphi, r_cosphi));
  // std::cout << "ll1[0] = " << ll1[0] << std::endl;
  // std::cout << "ll1[1] = " << ll1[1] << std::endl;
  // std::cout << "ll2[0] = " << ll2[0] << std::endl;
  // std::cout << "ll2[1] = " << ll2[1] << std::endl;

  if (r_a * fabs(r_phi) > 1e-6) {
    double r_ca = (r_bc * r_sinlon) / r_sinphi;
    double r_cb = r_ca * r_ca;
    double r_cc = (r_cosphi * (1.0 - r_cb)) / r_k1;
    double r_cd = (-2.0 * r_ac) / r_k1;
    double r_ce = -r_ac * r_k2;
    double r_cf = r_k3 * r_cc;
    double r_cg = r_phi * r_phi / r_sinphi;

    double r_x = ((r_phi * (r_k4 + r_cb) + r_sinphi * (r_cc + r_cd) +
                   r_cg * (r_cf + r_ce)) *
                  r_ca) /
                 r_k5;
    double r_lambda = r_l + r_x;

    double r_sinlam = sin(r_lambda);
    double r_coslam = cos(r_lambda);

    double r_cosph0 = r_ac + r_bc * r_coslam;
    double r_sinph0 = sign(r_sinlam) * sqrt(1.0 - r_cosph0 * r_cosph0);
    double r_phi0 = fabs(atan2(r_sinph0, r_cosph0));

    double r_sin2phi = 2.0 * r_sinph0 * r_cosph0;
    double r_cosbeta0 = (r_bc * r_sinlam) / r_sinph0;
    double r_q = 1.0 - r_cosbeta0 * r_cosbeta0;
    double r_cos2sig = (2.0 * r_ac - r_q * r_cosph0) / r_q;
    double r_cos4sig = 2.0 * (r_cos2sig * r_cos2sig - 0.5);

    double r_ch = r_b0 * (1.0 + (r_q * r_ep * r_ep) / 4.0 -
                          (3.0 * r_q * r_q * r_ep * r_ep * r_ep * r_ep) / 64.0);
    double r_ci = r_b0 * ((r_q * r_ep * r_ep) / 4.0 -
                          ((r_q * r_q) * r_ep * r_ep * r_ep * r_ep) / 16.0);
    double r_cj = (r_q * r_q * r_b0 * r_ep * r_ep * r_ep * r_ep) / 128.0;

    double r_t2 = (r_tanbetaf * r_cosbetai - r_coslam * r_sinbetai);

    r_sinlon = r_sinlam * sign(r_lsign);
    double r_geohdg = atan2(r_sinlon, r_t2);
    return r_geohdg;
  } else {
    std::cerr << "Cannot calculate the heading angle" << std::endl;
    return 0;
  }
}

double geo_hdg(double *ll1, double *ll2, double r_a = RA, double r_e2 = RE2) {
  /**
  Computes the heading along a geodesic for either an ellipitical or spherical
  earth given the initial latitude and longtitude and the final latitude and
  longitude.

  Notes: These results are based on the memo
  "Summary of Mocomp Referene Line Determination Study", IOM 3346-93-163
  and the paper
  "A Rigourous Non-iterative Procedure for Rapid Inverse Solution of Very Long
  Geodesics" by E. M. Sadano, Bulletine Geodesique 1958

  Adpated from Scott Hensley's Fortran code and isce2 python code

  Args:
      r_a: semi-major axis of Earth
      r_e2: eccentricity of Earth ellipsoid
      ll1: lat lon of the first point
      ll2: lat lon of the second point

  Returns:
      hdg: the azimuth at the first point

      (The azimuth is the heading measured clockwise from north)
      The definition of the inverse geodesic problem can be found at:
      https://geographiclib.sourceforge.io/2009-03/geodesic.html
      A figure that shows what the azimuth is:
      https://en.wikipedia.org/wiki/Geodesics_on_an_ellipsoid#/media/File:Geodesic_problem_on_an_ellipsoid.svg
  **/
  if (r_e2 == 0.) {
    return spherical_hdg(ll1, ll2);
  } else {
    return ellipsoidal_hdg(ll1, ll2, r_a, r_e2);
  }
}

void rah2ll(double *rah, double *tt, double *xx, double *vv,
            const std::size_t nstatvec, const double start_time,
            const double end_time, double bnd_lat[8], double bnd_lon[8],
            const std::string &look_dir = "RIGHT") {
  // look_dir: "RIGHT" or "LEFT"
  double xyzsatstart[3], velsatstart[3], xyzsatend[3], velsatend[3];
  double xyzsatmid[3], velsatmid[3], llhstart[3], llhend[3], llhmid[3];
  double r_geohdg, rcurv, aa, bb, costheta, sintheta;
  double t, r, h, xyz[3], vel[3], llh[3], vhat[3], that[3], chat[3], nhat[3];
  double dopfact = 0., alpha, beta, gamm, delta, xyztar[3];
  intp_orbit(nstatvec, tt, xx, vv, start_time, xyzsatstart, velsatstart);
  intp_orbit(nstatvec, tt, xx, vv, end_time, xyzsatend, velsatend);
  intp_orbit(nstatvec, tt, xx, vv, (start_time + end_time) / 2., xyzsatmid,
             velsatmid);
  xyz2llh(xyzsatstart, llhstart);
  xyz2llh(xyzsatend, llhend);
  // std::cout << "xyzstart :" << xyzsatstart[0] << std::endl;
  // std::cout << "llhstart :" << llhstart[0] << std::endl;
  r_geohdg = geo_hdg(llhstart, llhend, RA, RE2);
  // std::cout << "r_geohdg :" << r_geohdg << std::endl;
  xyz2llh(xyzsatmid, llhmid);
  rcurv = rdir(radians(llhmid[0]), r_geohdg, RA, RE2);
  // std::cout << "rcurv: " << rcurv << std::endl;
  for (int i = 0; i < 8; ++i) {
    r = rah[i * 3];
    t = rah[i * 3 + 1];
    h = rah[i * 3 + 2];
    intp_orbit(nstatvec, tt, xx, vv, t, xyz, vel);
    xyz2llh(xyz, llh, RA, RE2);
    std::copy(vel, vel + 3, vhat);
    normalize(vhat);
    tcnbasis(xyz, vel, that, chat, nhat, look_dir, RA, RE2);
    aa = rcurv + llh[2];
    bb = rcurv + h;
    costheta = 0.5 * ((aa / r + r / aa - bb / aa * bb / r));
    sintheta = sqrt(1. - costheta * costheta);
    gamm = costheta * r;
    alpha = (dopfact * r - gamm * dot_product(nhat, vhat)) /
            dot_product(vhat, that);
    beta = sqrt((r * sintheta) * (r * sintheta) - alpha * alpha);
    for (int j = 0; j < 3; ++j) {
      delta = gamm * nhat[j] + alpha * that[j] + beta * chat[j];
      xyztar[j] = xyz[j] + delta;
    }
    xyz2llh(xyztar, llh, RA, RE2);
    bnd_lat[i] = llh[0];
    bnd_lon[i] = llh[1];
  }
  return;
}

void bounds(const double start_time, const double end_time,
            const double start_rng, const double end_rng, const double hmin,
            const double hmax, double *tt, double *xx, double *vv,
            const std::size_t nstatvec, double *latlons,
            const std::string &look_dir) {
  double rah[24], bnd_lat[8], bnd_lon[8];
  for (int i = 0; i < 8; ++i) {
    if (i < 4) {
      rah[3 * i] = start_rng;
    } else {
      rah[3 * i] = end_rng;
    }
    if (i % 4 < 2) {
      rah[3 * i + 1] = start_time;
    } else {
      rah[3 * i + 1] = end_time;
    }
    if (i % 2 == 0) {
      rah[3 * i + 2] = hmin;
    } else {
      rah[3 * i + 2] = hmax;
    }
  }
  rah2ll(rah, tt, xx, vv, nstatvec, start_time, end_time, bnd_lat, bnd_lon,
         look_dir);
  latlons[0] = min_el(bnd_lat);
  latlons[1] = max_el(bnd_lat);
  latlons[2] = min_el(bnd_lon);
  latlons[3] = max_el(bnd_lon);
}
