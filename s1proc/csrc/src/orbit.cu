#ifndef _USE_MATH_DEFINES
#define _USE_MATH_DEFINES

#include <iostream>
#include <fstream>
#include <vector>
#include <cmath>
#include <string>

#include "orbit.hpp"
#ifndef M_PI
    #define M_PI 3.14159265358979323846
#endif

__host__ __device__
void llh2xyz(
        double llh[3],
        double xyz[3],
        const double r_a,
        const double r_e2){
    double r_lat, r_lon, h;
    double sinlat, sinlon, coslat, coslon;
    double re, x, y, z;
    r_lat = llh[0]*M_PI/180;
    r_lon = llh[1]*M_PI/180;
    h = llh[2]; 
    coslat = cos(r_lat);
    coslon = cos(r_lon);
    sinlat = sin(r_lat);
    sinlon = sin(r_lon);
    re = r_a/sqrt(1.-r_e2*sinlat*sinlat);
    x = (re+h)*coslat*coslon;
    y = (re+h)*coslat*sinlon;
    z = (re*(1-r_e2)+h)*sinlat;
    xyz[0] = x;
    xyz[1] = y;
    xyz[2] = z;
    return;
}

__host__ __device__
void xyz2llh(
        double *xyz,
        double *llh,
        const double r_a,
        const double r_e2){
    double x,y,z,r_q2,r_q,r_q3,r_b,r_p,r_re,r_tant,r_theta,lon,lat,h;
    x = xyz[0];
    y = xyz[1];
    z = xyz[2];

    r_q2 = 1./(1.-r_e2);
    r_q = sqrt(r_q2);
    r_q3 = r_q2 - 1.;
    r_b = r_a * sqrt(1 - r_e2);

    lon = atan2(y,x);
    if (y < 0 && lon > 0){
        lon = lon-M_PI;
    }
    if (y > 0 && lon < 0){
        lon = lon+M_PI;
    }
    r_p = sqrt(x*x+y*y);
    r_tant = (z/r_p)*r_q;
    r_theta = atan(r_tant);
    r_tant = (z + r_q3*r_b*pow(sin(r_theta),3))/(r_p-r_e2*r_a*pow(cos(r_theta),3));
    lat = atan(r_tant);
    r_re = r_a/sqrt(1.-r_e2*sin(lat)*sin(lat));
    h = r_p/cos(lat) - r_re;
    lat = lat/M_PI*180;
    lon = lon/M_PI*180;
    llh[0] = lat;
    llh[1] = lon;
    llh[2] = h;
    return;
}

__host__ __device__
void xyz2llh(
        float *xyz,
        double *llh,
        const double r_a,
        const double r_e2){
    double x,y,z,r_q2,r_q,r_q3,r_b,r_p,r_re,r_tant,r_theta,lon,lat,h;
    x = xyz[0];
    y = xyz[1];
    z = xyz[2];

    r_q2 = 1./(1.-r_e2);
    r_q = sqrt(r_q2);
    r_q3 = r_q2 - 1.;
    r_b = r_a * sqrt(1 - r_e2);

    lon = atan2(y,x);
    if (y < 0 && lon > 0){
        lon = lon-M_PI;
    }
    if (y > 0 && lon < 0){
        lon = lon+M_PI;
    }
    r_p = sqrt(x*x+y*y);
    r_tant = (z/r_p)*r_q;
    r_theta = atan(r_tant);
    r_tant = (z + r_q3*r_b*pow(sin(r_theta),3))/(r_p-r_e2*r_a*pow(cos(r_theta),3));
    lat = atan(r_tant);
    r_re = r_a/sqrt(1.-r_e2*sin(lat)*sin(lat));
    h = r_p/cos(lat) - r_re;
    lat = lat/M_PI*180;
    lon = lon/M_PI*180;
    llh[0] = lat;
    llh[1] = lon;
    llh[2] = h;
    return;
}

void read_orbit(
        const std::string orbitfile,
        size_t &nstatvec,
        double **t,
        double **x,
        double **v){
    double *orbitdata;
    double *t_loc, *x_loc, *v_loc;
    std::size_t filesize;
    std::ifstream fin(orbitfile, std::ios::binary);
    if (!fin){
        printf("File %s does not exist.\n",orbitfile.c_str());
    }
    fin.seekg(0, std::ios::end);
    filesize = fin.tellg();
    printf("Filesize: %d\n",int(filesize));
    nstatvec = filesize/7/sizeof(double);
    printf("nstatvec: %d\n",int(nstatvec));

    orbitdata = (double*)malloc(sizeof(double)*nstatvec*7);
    t_loc = (double*)malloc(sizeof(double)*nstatvec);
    x_loc = (double*)malloc(sizeof(double)*nstatvec*3);
    v_loc = (double*)malloc(sizeof(double)*nstatvec*3);

    fin.seekg(0, std::ios::beg);
    fin.read((char*) orbitdata, filesize);
    fin.close();

    for (std::size_t i = 0; i < nstatvec; i++){
        t_loc[i] = orbitdata[i*7];
        x_loc[i*3] = orbitdata[i*7+1];
        x_loc[i*3+1] = orbitdata[i*7+2];
        x_loc[i*3+2] = orbitdata[i*7+3];
        v_loc[i*3] = orbitdata[i*7+4];
        v_loc[i*3+1] = orbitdata[i*7+5];
        v_loc[i*3+2] = orbitdata[i*7+6];
    }
    *t = t_loc;
    *x = x_loc;
    *v = v_loc;
    free(orbitdata);
    return;
}

void read_orbit_ascii(
        const std::string orbitfile,
        size_t &nstatvec,
        double **t,
        double **x,
        double **v){
    double *t_loc, *x_loc, *v_loc, temp;
    std::ifstream fin(orbitfile);
    if (!fin){
        printf("Orbit file %s does not exist\n",orbitfile.c_str());
    }
    fin >> nstatvec;
    t_loc = (double*)malloc(sizeof(double)*nstatvec);
    x_loc = (double*)malloc(sizeof(double)*nstatvec*3);
    v_loc = (double*)malloc(sizeof(double)*nstatvec*3);

    for (std::size_t i = 0; i < nstatvec; i++){
        fin >> t_loc[i] >> x_loc[i*3] >> x_loc[i*3+1] >> x_loc[i*3+2];
        fin >> v_loc[i*3] >> v_loc[i*3+1] >> v_loc[i*3+2];
        fin >> temp >> temp >> temp;
    }
    *t = t_loc;
    *x = x_loc;
    *v = v_loc;
    return;
}

__host__ __device__
void orbithermite(
        double *tt,
        double *xx,
        double *vv,
        const double t,
        double *satx,
        double *satv){
    double dl; // derivative of Lagrange basis at tt[i]
    double hdot; // derivative of Lagrange basis at t
    double p,l2;
    double li[4],a[4],b[4],a2[4],b2[4];
    //double *li,*a,*b,*a2,*b2;
    //li = (double*)malloc(sizeof(double)*n);
    //a = (double*)malloc(sizeof(double)*n);
    //b = (double*)malloc(sizeof(double)*n);
    //a2 = (double*)malloc(sizeof(double)*n);
    //b2 = (double*)malloc(sizeof(double)*n);
    for (std::size_t i = 0; i < 4; ++i){
        li[i] = 1.0;
    }
    for (std::size_t i = 0; i < 4; ++i){
        dl = 0.;
        hdot = 0.;
        for (std::size_t j = 0; j < 4; ++j){
            if (i == j) continue;
            dl = dl + 1./(tt[i] - tt[j]);
            li[i] = li[i]*(t-tt[j])/(tt[i]-tt[j]);
            p = 1./(tt[i]-tt[j]);
            for (std::size_t k = 0; k < 4; ++k){
                if (k == i || k == j) continue;
                p = p*(t-tt[k])/(tt[i]-tt[k]);
            }
            hdot = hdot+p;
        }
        l2 = li[i]*li[i];
        a[i] = (1-2*(t-tt[i])*dl)*l2;
        b[i] = (t-tt[i])*l2;
        a2[i] = -2*dl*l2 + (1-2*(t-tt[i])*dl)*li[i]*2*hdot;
        b2[i] = l2 + (t-tt[i])*li[i]*2*hdot;
    }
    for (std::size_t j = 0; j < 3; ++j){
        satx[j] = 0;
        satv[j] = 0;
        for (std::size_t i = 0; i < 4; ++i){
            satx[j] += a[i]*xx[i*3+j] + b[i]*vv[i*3+j];
            satv[j] += a2[i]*xx[i*3+j] + b2[i]*vv[i*3+j];
        }
    }
    //free(li);
    //free(a);
    //free(b);
    //free(a2);
    //free(b2);
    return;
}

__host__ __device__
void intp_orbit(
        const std::size_t nstatvec,
        double *timeorbit,
        double *xx,
        double *vv,
        const double t,
        double *satx,
        double *satv){
    std::size_t ilocation = 0;
    double delta_t, min_delta_t = 1.e10;
    double tt[4], x[12], v[12];
    // find the location of the sampling time that is closest to t
    for (std::size_t i = 0; i<nstatvec; ++i){
        delta_t = fabs(t-timeorbit[i]);
        if (delta_t < min_delta_t){
            min_delta_t = delta_t;
            ilocation = i; 
        }
    }
    // Four points are needed for the Hermite interpolation
    ilocation = ilocation>1 ? ilocation : 1;
    ilocation = ilocation<(nstatvec-3) ? ilocation : nstatvec-3;
    
    for (std::size_t i = 0; i < 4; ++i){
        tt[i] = timeorbit[ilocation-1+i];
        x[i*3] = xx[(ilocation-1+i)*3];
        x[i*3+1] = xx[(ilocation-1+i)*3+1];
        x[i*3+2] = xx[(ilocation-1+i)*3+2];
        v[i*3] = vv[(ilocation-1+i)*3];
        v[i*3+1] = vv[(ilocation-1+i)*3+1];
        v[i*3+2] = vv[(ilocation-1+i)*3+2];
    }
    orbithermite(tt,x,v,t,satx,satv);
    return;
}

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
        double &tline,
        double *dr){
    double tprev,fn,fnprime;
    double satx[3], satv[3];
    for (int i = 0; i < 3; i++){
        satx[i] = satx0[i];
        satv[i] = satv0[i];
    }
     
    tline = tline0;
    for (int k = 0; k < 51; ++k){
        tprev = tline;
        fn = 0.;
        fnprime = 0.;
        for (int i = 0; i<3; ++i){
            dr[i] = xyz[i] - satx[i];
            fn += dr[i]*satv[i];
            fnprime -= satv[i]*satv[i];
        }
        tline -= fn/fnprime;
        intp_orbit(nstatvec,timeorbit,xx,vv,tline,satx,satv);
        //std::cout << tline-tprev << std::endl;
        if (fabs(tline-tprev) < 5.e-9){
            break;
        }
    }
    for (int i = 0; i<3; ++i){
        dr[i] = xyz[i] - satx[i];
    }
    return;
}

#endif

