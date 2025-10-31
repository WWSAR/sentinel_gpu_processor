#include <iostream>
#include <fstream>
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

void save_float(float *img,
                const std::size_t n,
                const std::string& imgfile){
    save_float(img,false,n,imgfile);
    return; 
}

void save_float(float *img,
                bool append,
                const std::size_t n,
                const std::string& imgfile){
    std::ofstream fout;
    if(append){
        fout.open(imgfile, std::ios::app | std::ios::binary);
    }else{
        fout.open(imgfile, std::ios::out | std::ios::binary);
    }
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) img,sizeof(float)*n);
    fout.close();
    return; 
}

void save_double(double *img,
                const std::size_t n,
                const std::string& imgfile){
    save_double(img,false,n,imgfile);
    return; 
}

void save_double(double *img,
                bool append,
                const std::size_t n,
                const std::string& imgfile){
    //std::cout << "outputfile: " << imgfile <<std::endl;
    std::ofstream fout;
    if(append){
        fout.open(imgfile,std::ios::app|std::ios::binary);
    }else{
        fout.open(imgfile, std::ios::out | std::ios::binary);
    }
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) img,sizeof(double)*n);
    fout.close();
    return; 
}

void save_int(int *img,
                const std::size_t n,
                const std::string& imgfile){
    save_int(img,false,n,imgfile);
}

void save_int(int *img,
              bool append,
              const std::size_t n,
              const std::string& imgfile){
    //std::cout << "outputfile: " << imgfile <<std::endl;
    std::ofstream fout;
    if(append){
        fout.open(imgfile,std::ios::app|std::ios::binary);
    }else{
        fout.open(imgfile, std::ios::out | std::ios::binary);
    }
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) img,sizeof(int)*n);
    fout.close();
    return; 
}

void save_int(int *img,
              const std::size_t toskip,
              const std::size_t n,
              const std::string& imgfile){
    std::fstream fout;
    fout.open(imgfile, std::ios_base::binary|std::ios_base::out|std::ios_base::in);
    fout.seekp(toskip*sizeof(int), std::ios_base::beg);
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) img, sizeof(int)*n);
    fout.close();
    return; 
}

void readdem(const std::string& imgfile, 
             const std::size_t n,
             short int *dem){
    readdem(imgfile,0,n,dem);
    return;
}

void readdem(const std::string& imgfile, 
             const std::size_t toskip,
             const std::size_t n,
             short int *dem){
    std::ifstream fin(imgfile, std::ios::binary);
    std::cout << "dem to skip: " << toskip << std::endl;
    fin.seekg(toskip*sizeof(short int),std::ios::beg);
    if (!fin){
        printf("File %s does not exist.\n",imgfile.c_str());
        return;
    }
    fin.read((char *)dem, sizeof(short int)*n);
    fin.close();
    return;
}

void read_int(const std::string& imgfile, 
                const std::size_t n,
                int *img){
    read_int(imgfile,0,n,img);
    return;
}

void read_int(const std::string& imgfile, 
              const std::size_t toskip,
              const std::size_t n,
              int *img){
    std::ifstream fin(imgfile, std::ios::binary);
    fin.seekg(toskip*sizeof(int),std::ios::beg);
    if (!fin){
        printf("File %s does not exist.\n",imgfile.c_str());
        return;
    }
    fin.read((char *)img, sizeof(int)*n);
    fin.close();
    return;
}


void read_float(const std::string& imgfile, 
                const std::size_t n,
                float *img){
    read_float(imgfile,0,n,img);
    return;
}

void read_float(const std::string& imgfile, 
                const std::size_t toskip,
                const std::size_t n,
                float *img){
    std::ifstream fin(imgfile, std::ios::binary);
    fin.seekg(toskip*sizeof(float),std::ios::beg);
    if (!fin){
        printf("File %s does not exist.\n",imgfile.c_str());
        return;
    }
    fin.read((char *)img, sizeof(float)*n);
    fin.close();
    return;
}

void read_double(const std::string& imgfile, 
                const std::size_t n,
                double *img){
    read_double(imgfile,0,n,img);
    return;
}

void read_double(const std::string& imgfile, 
                const std::size_t toskip,
                const std::size_t n,
                double *img){
    std::ifstream fin(imgfile, std::ios::binary);
    fin.seekg(toskip*sizeof(double),std::ios::beg);
    if (!fin){
        printf("File %s does not exist.\n",imgfile.c_str());
        return;
    }
    fin.read((char *)img, sizeof(double)*n);
    fin.close();
    return;
}

void read_cpx(const std::string& imgfile,
              const std::size_t toskip,
              const std::size_t n,
              Complex *img){
    float *imgbuffer = (float*)malloc(sizeof(float)*n*2);
    std::ifstream fin(imgfile, std::ios::binary);
    if (!fin){
        printf("File %s does not exist\n",imgfile.c_str());
        return;
    }
    // skip the first several elements
    fin.seekg(toskip*sizeof(float)*2,std::ios::beg);
    if (!fin){
        printf("Cannot skip the first %zu elements\n",toskip);
        return;
    }
    fin.read((char*) imgbuffer, sizeof(float)*n*2);
    for (std::size_t i = 0; i < n; i++){
        img[i].x = imgbuffer[i*2];
        img[i].y = imgbuffer[i*2+1];
    }
    free(imgbuffer);
    return;
}

void read_cpx(const std::string& imgfile,
              const std::size_t n,
              Complex *img){
    read_cpx(imgfile,0,n,img);
    return;
}


void save_cpx(Complex *img,
              bool append,
              const std::size_t n,
              const std::string& imgfile){
    float *imgbuffer = (float*)malloc(sizeof(float)*n*2);
    for (std::size_t i = 0; i < n; i++){
        imgbuffer[i*2] = img[i].x;
        imgbuffer[i*2+1] = img[i].y;
    }
    std::ofstream fout;
    if (append){
        fout.open(imgfile, std::ios::app | std::ios::binary);
    }else{
        fout.open(imgfile, std::ios::out | std::ios::binary);
    }
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) imgbuffer, sizeof(float)*n*2);
    fout.close();
    free(imgbuffer);
    return; 
}

void save_cpx(Complex *img,
              const std::size_t n,
              const std::string& imgfile){
    save_cpx(img,false,n,imgfile);
    return; 
}

void save_cpx(Complex *img,
              const std::size_t toskip,
              const std::size_t n,
              const std::string& imgfile){
    float *imgbuffer = (float*)malloc(sizeof(float)*n*2);
    for (std::size_t i = 0; i < n; i++){
        imgbuffer[i*2] = img[i].x;
        imgbuffer[i*2+1] = img[i].y;
    }
    std::fstream fout;
    fout.open(imgfile, std::ios_base::binary|std::ios_base::out|std::ios_base::in);
    fout.seekp(toskip*sizeof(Complex), std::ios_base::beg);
    if (!fout.is_open()){
        printf("Unable to open file %s\n",imgfile.c_str());
        return;
    }
    fout.write((char*) imgbuffer, sizeof(float)*n*2);
    fout.close();
    free(imgbuffer);
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