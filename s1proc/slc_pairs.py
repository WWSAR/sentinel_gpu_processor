#!/usr/bin/env python3
# create a list of sbas pairs

import glob
import re
import numpy as np
import os
import sys
import tyro
from pathlib import Path
from datetime import datetime

import geocoordinates
import geometry
import orbit
from _log import setup_logger, set_logging_level
logger = setup_logger(name=__name__,level='INFO')


def sentinel_parser(filename:Path|str)->dict:
    filename = os.path.split(filename)[-1]
    words = re.split(r'[_]+|\.',filename)
    sent = {}
    sent['filename'] = filename
    sent['mission'] = words[0]
    sent['mode'] = words[1]
    sent['product_type'] = words[2]
    sent['level'] = words[3][0]
    sent['product_class'] = words[3][1]
    sent['polarization'] = words[3][2:4]
    sent['start_time'] = words[4]
    sent['stop_time'] = words[5]
    sent['orbit_number'] = words[6]
    sent['mission_id'] = words[7]
    sent['unique_id'] = words[8]
    sent['date'] = sent['start_time'][0:8]
    return sent

def sentinel_acq_time(filename):
    sent = sentinel_parser(filename)
    start_time = datetime.strptime(sent["start_time"],"%Y%m%dT%H%M%S")
    stop_time = datetime.strptime(sent["stop_time"],"%Y%m%dT%H%M%S")
    t = start_time + (stop_time-start_time)/2
    return t

def read_sentinel_orbit(orbfile):
    tt = None
    xx = None
    vv = None
    with open(orbfile,"r") as f:
        line = f.readline()
        nstatvec = int(line.strip())
        tt = np.zeros(nstatvec)
        xx = np.zeros((nstatvec,3))
        vv = np.zeros((nstatvec,3))
        for i in range(nstatvec):
            line = f.readline()
            words = line.split()
            num = np.array([float(s) for s in words])
            tt[i]  = num[0]
            xx[i,:] = num[1:4]
            vv[i,:] = num[4:7]
    return tt,xx,vv

def estimatebaseline(orbfile1,orbfile2,demfile,demrscfile):
    rsc = geocoordinates.GeoCoordinates(demrscfile)
    nrow,ncol = rsc.nlat, rsc.nlon
    with open(demfile,"r") as f:
        f.seek(nrow*ncol//2*2)
        hmid = np.fromfile(f,dtype=np.int16,count=1)[0]
    latmid = rsc.latmax + nrow//2*rsc.dlat
    lonmid = rsc.lonmin + ncol//2*rsc.dlon
    llh = np.array([latmid,lonmid,hmid])

    tt1,xx1,vv1 = read_sentinel_orbit(orbfile1)
    mid_idx = len(tt1)//2
    tmid1= tt1[mid_idx]
    xmid1= xx1[mid_idx,:]
    vmid1= vv1[mid_idx,:]
    tt2,xx2,vv2 = read_sentinel_orbit(orbfile2)
    mid_idx = len(tt2)//2
    tmid2 = tt2[mid_idx]
    xmid2 = xx2[mid_idx,:]
    vmid2 = vv2[mid_idx,:]
    xyz = geometry.llh2xyz(llh)
    dr1,_ = orbit.orbitrangetime(tt1,xx1,vv1,xyz,tmid1,xmid1,vmid1)
    dr2,_ = orbit.orbitrangetime(tt2,xx2,vv2,xyz,tmid2,xmid2,vmid2)
    u1 = -dr1/np.linalg.norm(dr1)
    u2 = -dr2/np.linalg.norm(dr2)
    uperp = np.cross(u1,u2)
    theta = np.arcsin(np.linalg.norm(uperp))
    vdotuperp = np.dot(vmid1,uperp)
    if vdotuperp>0:
        bperp = np.linalg.norm(dr1)*theta
    else:
        bperp = -np.linalg.norm(dr1)*theta
    return bperp

def create_slc_pair_list(
        min_tbl:int = 0,
        max_tbl:int = 30000,
        min_sbl:int = 0,
        max_sbl:int = 10000):
    """
    Create a list of SLC pairs for interferogram generation

    Parameters
    ----------
    min_tbl: int
        minimum temporal baseline threshold
    max_tbl: int
        maximum temporal baseline threshold
    min_sbl: int
        minimum temporal baseline threshold
    max_sbl: int
        maximum temporal baseline threshold
    """
    
    # set dem and rsc files
    demfile = os.path.join('..','elevation.dem')
    rscfile = os.path.join('..','elevation.dem.rsc')
    
    # find all slc images in the parent directory
    slc_list = glob.glob(os.path.join('..','*.geo'))
    slc_list = np.sort(slc_list)

    # create a list of all acquisition dates
    date_list = []
    for slc_file in slc_list:
        sent = sentinel_parser(slc_file)
        date_str = sent['date']
        date_list.append(date_str)
    
    # create a dictionary mapping date to slcfiles
    slc_dict = {}
    for i,date_str in enumerate(date_list):
        if date_str in slc_dict:
            slc_dict[date_str].append(slc_list[i])
        else:
            slc_dict[date_str] = [slc_list[i]]

    f = open('sbas_list','w')
    unique_date_list = np.sort(np.unique(date_list))
    ndates = len(unqiue_date_list)
    for i in range(ndates-1):
        date_str_ref = unique_date_list[i]
        date_ref = datetime.strptime(date_str_ref,'%Y%m%d')
        slcs_ref = slc_dict[date_str_ref]
        orbfile1 = slcs_ref[0].strip().replace('geo','orbtiming')
        for j in range(i+1,ndates):
            date_str_sec = unique_date_list[j]
            date_sec = datetime.strptime(date_str_sec,'%Y%m%d')
            slcs_sec = slc_dict[date_str_sec]
            orbfile2 = slcs_sec[0].strip().replace('geo','orbtiming')
            tempbl = (date_sec-date_ref).days 
            if tempbl > max_tbl or tempbl < min_tbl:
                continue
            bperp = estimatebaseline(orbfile1,orbfile2,demfile,rscfile)
            if bperp > max_sbl or bperp < min_sbl:
                continue
            if len(slcs_ref) != len(slcs_sec):
                logger.warning('Numbers of SLC images do not match for '
                        f'{date_str_ref} and {date_str_sec}, skipping')
                continue
            else:
                for k in range(len(slcs_ref)):
                    f.write(f'{date_str_ref} {date_str_sec} {tempbl} {bperp}\n')
    f.close()

if __name__ == '__main__':
    tyro.cli(create_slc_pair_list)
