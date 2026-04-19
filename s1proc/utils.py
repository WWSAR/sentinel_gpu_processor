#!/usr/bin/env python3
# create a list of sbas pairs

import glob
import re
import numpy as np
import os
import shutil
import sys
from functools import cmp_to_key
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

from s1proc import geocoordinates
from s1proc import geometry
from s1proc import orbit
from s1proc import sario
from s1proc._log import setup_logger, set_logging_level
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

def zip_subswath_lists(subswath_lists,subswath_numbers):
    idx = 0
    nsubswath = len(subswath_numbers)
    d = {}
    for i, subswath_list in enumerate(subswath_lists):
        for fn in subswath_list:
            basename = os.path.basename(fn)
            scene_id = basename[0:20]
            if scene_id in d:
                d.append(fn)
            else:
                d[scene_id] = [fn]
    slc_list = []
    for scene_id in sorted(d.keys()):
        fns = d[scene_id]
        l = [None]*nsubswath
        for fn in fns:
            basename = os.path.basename(fn)
            iw_number = int(basename[23])
            l[subswath_numbers.index(iw_nubmer)] = fn
        slc_list.append(l)
    return slc_list

def compare_arrays(a, b):
    i = 0
    while i < min(len(a), len(b)):
        if a[i] < 0 or b[i] < 0:
            i += 1
            continue
        if a[i] != b[i]:
            return -1 if a[i] < b[i] else 1
        else:
            i += 1
    if a[i-1] < 0:
        return -1
    else:
        return 1

def argsort(arrays):
    indices = list(range(len(arrays)))
    indices.sort(key=cmp_to_key(lambda i, j: compare_arrays(arrays[i], arrays[j])))
    return indices

def bbox_sort(slcs):
    n = len(slcs)
    nsubswath = len(slcs[0])
    visited = np.zeros(n, dtype=bool)
    top_idx = np.zeros((n,nsubswath), dtype=np.int32)
    for i in range(n):
        for j in range(nsubswath):
            fn = slcs[i][j]
            if fn is None:
                top_idx[i,j] = -1
                continue
            top = np.fromfile(fn, dtype=np.int32, count = 4)[-1]
            top_idx[i,j] = top
    sorted_idx = argsort(top_idx)
    return [slcs[i] for i in sorted_idx]

def create_slc_pair_list(
        min_tbl: int = 0,
        max_tbl: int = 30000,
        min_sbl: int = 0,
        max_sbl: int = 10000,
        slc_dir: str = 'slc',
        proc_dir: str = 'proc',
        ifg_dir: str = 'igrams',
        demfile: str = 'elevation.dem',
        rscfile: str = 'elevation.dem.rsc'):
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
    slc_dir: str
        SLC directory
    proc_dir: str
        Directory storing auxilary parameters
    ifg_dir: str
        Directory storing interferograms
    demfile: str
        DEM file
    rscfile: str
        rsc file
    """
    os.makedirs(ifg_dir, exist_ok = True)
    # find all slc images in the parent directory
    subswath_lists = []
    subswath_numbers = []
    for subswath in range(1,4):
        subswath_list = glob.glob(
                os.path.join(slc_dir,f'*iw{subswath}_main.geo'))
        if len(subswath_list) > 0:
            subswath_numbers.append(subswath)
            subswath_list = np.sort(subswath_list)
            subswath_lists.append(subswath_list)

    nsubswath = len(subswath_lists) 
    if nsubswath == 0:
        logger.warning('No SLC images were found.')
        return
    elif nsubswath == 1:
        slc_list = [[s] for s in subswath_lists[0]]
    else:
        slc_list = zip_subswath_lists(subswath_lists, subswath_numbers)
    
    # create a list of all acquisition dates
    date_list = []
    for subswath_files in slc_list:
        basename = os.path.basename(subswath_files[0])
        date_str = basename[0:8]
        date_list.append(date_str)
    
    # create a dictionary mapping date to slcfiles
    slc_dict = {}
    for i,date_str in enumerate(date_list):
        if date_str in slc_dict:
            slc_dict[date_str].append(slc_list[i])
        else:
            slc_dict[date_str] = [slc_list[i]]

    unique_date_list = np.sort(np.unique(date_list))
    for date_str in unique_date_list:
        slcs = slc_dict[date_str]
        slcs = bbox_sort(slcs)
        slc_dict[date_str] = slcs

    f = open(os.path.join(ifg_dir,'subswath_list'),'w')
    ndates = len(unique_date_list)
    for i in range(ndates-1):
        date_str_ref = unique_date_list[i]
        date_ref = datetime.strptime(date_str_ref,'%Y%m%d')
        slcs_ref = slc_dict[date_str_ref]
        basename1 = os.path.basename(slcs_ref[0][0])
        orbfile1 = os.path.join(proc_dir, basename1[0:20]+'.orbtiming')
        for j in range(i+1,ndates):
            date_str_sec = unique_date_list[j]
            date_sec = datetime.strptime(date_str_sec,'%Y%m%d')
            slcs_sec = slc_dict[date_str_sec]
            basename2 = os.path.basename(slcs_sec[0][0])
            orbfile2 = os.path.join(proc_dir, basename2[0:20]+'.orbtiming')
            tempbl = (date_sec-date_ref).days 
            if tempbl > max_tbl or tempbl < min_tbl:
                continue
            bperp = estimatebaseline(orbfile1,orbfile2,demfile,rscfile)
            if np.abs(bperp) > max_sbl or np.abs(bperp) < min_sbl:
                continue
            if len(slcs_ref) == 1:
                for k in range(len(slcs_sec)):
                    for j in range(nsubswath):
                        if slcs_ref[0][j] is None or slcs_sec[k][j] is None:
                            continue
                        f.write(f'{slcs_ref[0][j]} {slcs_sec[k][j]} ' + \
                                f'{tempbl} {bperp}\n')
            elif len(slcs_sec) == 1:
                for k in range(len(slcs_ref)):
                    for j in range(nsubswath):
                        if slcs_ref[k][j] is None or slcs_sec[0][j] is None:
                            continue
                        f.write(f'{slcs_ref[k][j]} {slcs_sec[0][j]} ' + \
                                f'{tempbl} {bperp}\n')
            elif len(slcs_ref) == len(slcs_sec):
                for k in range(len(slcs_ref)):
                    for j in range(nsubswath):
                        if slcs_ref[k][j] is None or slcs_sec[k][j] is None:
                            continue
                        f.write(f'{slcs_ref[k][j]} {slcs_sec[k][j]} ' + \
                                f'{tempbl} {bperp}\n')
            else:
                logger.warning('Numbers of SLC images do not match for '
                        f'{date_str_ref} and {date_str_sec}, skipping')
    f.close()

def mid_orbit(
        orb_list: List[Path|str]|None = None,
        dem_file: Path|str = 'elevation.dem',
        rsc_file: Path|str = 'elevation.dem.rsc') -> str:
    """
    Find the middle orbit
    
    Parameters
    ----------
    orb_list: List[Path|str]|None
        List of orbit files. If None, use all precise orbit files
    dem_file: Path|str
        DEM file
    rsc_file: Path|str
        rsc file

    Returns
    -------
    mid_orb_file
        File name of the middle orbit
    """
    if orb_list is None:
        orb_list = glob.glob('*.precise_orbtiming')

    norb = len(orb_list)
    bperps = np.zeros(norb,dtype=np.float32)
    for i in range(1,norb):
        bperps[i] = estimatebaseline(orb_list[0],orb_list[i],dem_file,rsc_file)
    idx = np.argsort(bperps)
    mid_orb_file = orb_list[idx[norb//2]]
    logger.info(f'Middle orbit file: {mid_orb_file}')
    return mid_orb_file

def _los(
        orb: np.ndarray,
        dem: np.ndarray,
        rsc: geocoordinates.GeoCoordinates
        ) -> Tuple[np.ndarray, np.ndarray]:
    """
    Calculate Line-Of-Sight (LOS) vectors for all grid points

    Parameters
    ----------
    orb: np.ndarray
        orbit array containing seven columns:
        time, x, y, z, vx, vy, vz
    dem: np.ndarray
        DEM (potentially multilooked)
    rsc: geocoordinates.GeoCoordinates
        rsc object describing the geographic information of DEM 

    Returns
    -------
    los: np.ndarray
        LOS vectors
    theta: np.ndarray
        look angle

    Notes
    -------
    This function first read orbit information from an orbtiming file in the
    parent directory. For each ground point, it calculates the corresponding
    Zero-Doppler time and the associated LOS vector and look angle.
    """
    nrow,ncol = rsc.nlat,rsc.nlon
    lat,lon = rsc.grid()
    # calculate the LOS vectors in the ECEF coordinate
    logger.info('Converting DEM grid into ECEF coordinates')
    llh = np.column_stack((lat.flatten(),lon.flatten(),dem.flatten()))
    logger.info('Computing LOS vectors')
    losvec = orbit.orbitrangetime_vec(llh,orb[:,0],orb[:,1:4],orb[:,4:7])
    losvec = losvec.astype(np.float32)
    logger.info('Computing LOS vectors, done.')

    # calculate look angle
    logger.info('Computing look angles')
    r_lat = np.radians(lat.flatten())
    r_lon = np.radians(lon.flatten())
    r_n = -np.column_stack([np.cos(r_lat)*np.cos(r_lon),
                            np.cos(r_lat)*np.sin(r_lon),
                            np.sin(r_lat)])
    costheta = np.sum(losvec*r_n,axis=1).reshape(nrow,ncol)
    costheta = np.clip(costheta,-1,1)
    theta = np.rad2deg(np.arccos(costheta)).astype(np.float32)
    logger.info('Computing look angles, done.')
    return losvec, theta

def los(dem_file: str,
        rsc_file: str,
        /,
        proc_dir: str = 'proc',
        rowlook: int = 1,
        collook: int = 1):
    """
    dem_file: Path|str
        dem file (int16)
    rsc_file: Path|str
        rsc file that defines the grid
    proc_dir: str
        Directory to save output files
    rowlook: int
        Number of looks in row direction
    collook: int
        Number of looks in column direction
    """
    small_dem_file = os.path.join(proc_dir, 'dem')
    rsc = geocoordinates.GeoCoordinates(rsc_file)
    rsclook = rsc.take_look(rowlook,collook)
    nrow, ncol = rsc.nlat, rsc.nlon
    nrow_sm = nrow // rowlook
    ncol_sm = ncol // collook
    if rowlook > 1 or collook > 1:
        logger.info('Multilooking dem file')
        sario.multilooks(dem_file, small_dem_file, np.int16, nrow, ncol,
                rowlook, collook)
        logger.info(f'Multilooked DEM is saved to {small_dem_file}')
    else:
        small_dem_file = dem_file
    dem = np.fromfile(small_dem_file, dtype = np.int16)
    orb_list = glob.glob(os.path.join(proc_dir, '*.orbtiming'))
    orb_file = mid_orbit(orb_list, dem_file, rsc_file)
    orb = sario.read_orbit(orb_file)
    losvec, theta = _los(orb, dem, rsclook)
    losvec_file = os.path.join(proc_dir, 'losvec')
    losvec.tofile(losvec_file)
    logger.info(f'LOS vectors are saved to {losvec_file}')
    theta_file = os.path.join(proc_dir, 'look_angle')
    theta.tofile(theta_file)
    logger.info(f'Look angles are saved to {theta_file}')
