#!/usr/bin/env python3
#
#  process one sentinel scene files to coregistered geocoded slc, single/dual pol

import glob
import os
import numpy as np
import shutil
import sqlite3
import subprocess
import zipfile
from pathlib import Path
from typing import Sequence

from s1proc import sql_mod
from s1proc._log import setup_logger, set_logging_level
from s1proc.geocoordinates import GeoCoordinates
from s1proc.orbit import rah2ll
from s1proc.precise_orbit import parse_orbit
from s1proc.sario import sentinel_parser, compress
from s1proc.sentinel_roidb import create_db
logger = setup_logger(name = __name__, level = 'INFO')
SOL = 299792458.

def clip_polygon_with_rect(poly, lon_min, lat_min, lon_max, lat_max):

    def inside_left(p):   return p[0] >= lon_min
    def inside_right(p):  return p[0] <= lon_max
    def inside_bottom(p): return p[1] >= lat_min
    def inside_top(p):    return p[1] <= lat_max

    def intersect(p1, p2, edge):
        x1, y1 = p1
        x2, y2 = p2

        if edge == "left":
            x = lon_min
            y = y1 + (y2-y1)*(lon_min-x1)/(x2-x1)
        elif edge == "right":
            x = lon_max
            y = y1 + (y2-y1)*(lon_max-x1)/(x2-x1)
        elif edge == "bottom":
            y = lat_min
            x = x1 + (x2-x1)*(lat_min-y1)/(y2-y1)
        elif edge == "top":
            y = lat_max
            x = x1 + (x2-x1)*(lat_max-y1)/(y2-y1)

        return (x,y)

    def clip(poly, inside, edge):
        result = []
        for i in range(len(poly)):
            curr = poly[i]
            prev = poly[i-1]

            if inside(curr):
                if not inside(prev):
                    result.append(intersect(prev,curr,edge))
                result.append(curr)
            elif inside(prev):
                result.append(intersect(prev,curr,edge))

        return result

    poly = clip(poly, inside_left, "left")
    poly = clip(poly, inside_right, "right")
    poly = clip(poly, inside_bottom, "bottom")
    poly = clip(poly, inside_top, "top")

    return poly

def dem_bounds(footprint, rsc):
    """
    Get the bounds of the overlapped area between Sentinel-1 footprint and
    the study area defined by the RSC
    """
    overlap_poly = clip_polygon_with_rect(
        footprint,
        rsc.lonmin, rsc.latmin, rsc.lonmax, rsc.latmax
    )

    if overlap_poly:
        lons = [p[0] for p in overlap_poly]
        lats = [p[1] for p in overlap_poly]

        overlap_bbox = (
            min(lons), min(lats),
            max(lons), max(lats)
        )
    else:
        overlap_bbox = None
        return None
    rowmin, colmin = rsc.ll2xy(overlap_bbox[3],overlap_bbox[0])
    rowmax, colmax = rsc.ll2xy(overlap_bbox[1],overlap_bbox[2])
    rowmin = int(np.maximum(rowmin, 0))
    colmin = int(np.maximum(colmin, 0))
    rowmax = int(np.minimum(rowmax, rsc.nlat))
    colmax = int(np.minimum(colmax, rsc.nlon))
    return colmin, rowmin, colmax, rowmax

def footprint_bounds(orbfname:str, dbfname:str):
    """
    Get the lat/lon bounds of current subswath
    """
    # read orbit
    with open(orbfname, 'r') as f:
        firstline = f.readline()
        nline = int(firstline.strip())
        tt = np.zeros(nline, dtype=np.float64)
        xx = np.zeros((nline,3), dtype=np.float64)
        vv = np.zeros((nline,3), dtype=np.float64)
        for i in range(nline):
            line = f.readline()
            words = line.split()
            val = np.array([float(w) for w in words])
            tt[i] = val[0]
            xx[i,:] = val[1:4]
            vv[i,:] = val[4:7]
    con = sqlite3.connect(dbfname)
    # create a cursor
    c = con.cursor()
    tblname = 'file'
    prf = sql_mod.valuef(c,tblname,'prf')
    azimuth_bursts = sql_mod.valuei(c,tblname,'azimuthBursts')
    lines_per_burst = sql_mod.valuei(c,tblname,'linesPerBurst')
    slant_range_time = sql_mod.valuef(c,tblname,'slantRangeTime') 
    range_sampling_rate = sql_mod.valuef(c,tblname,'rangeSamplingRate')
    nrange = sql_mod.valuei(c,tblname,'samplesPerBurst')
    starttime = sql_mod.valuef(c, tblname, f'azimuthTimeSeconds1')
    starttime_last = sql_mod.valuef(c, tblname,
            f'azimuthTimeSeconds{azimuth_bursts}')
    c.close()
    con.close()
    stoptime = starttime_last + lines_per_burst/prf
    rngstart = slant_range_time*SOL/2.
    dmrg = SOL/2./range_sampling_rate
    rngend = rngstart + (nrange-1)*dmrg

    rah = []
    for t in [starttime, stoptime]:
        for i, r in enumerate([rngstart, rngend]):
            if i == 0:
                rah.append([r,t,0])
            else:
                rah.append([r,t,10000])
    lats,lons = rah2ll(tt,xx,vv,starttime,stoptime,np.array(rah))
    return np.array(list(zip(lons, lats)))

def sentinel_scene(
        zip_file: str,
        demfile: str,
        rscfile: str,
        orbfile: str|None = None,
        polarization: str = 'vv',
        subswath_list: Sequence[int] = [1,2,3],
        proc_dir: str = 'stack',
        slc_dir: str = 'slc',
        rm_zipfile: bool = False,
        rm_folder: bool = False):
    """
    Process one sentinel scene files to coregistered geocoded slc

    Parameters
    ----------
    zip_file: str
        Input zip file
    demfile: str
        DEM file
    rscfile: str
        rsc file
    orbfile: str|None
        Precise orbit file. If none, coarse orbit will be used
    polarization: str
        Polarization(s) to process
    subswath_list: Sequence[int]
        Subswaths to process
    proc_dir: str
        Directory to store temporary files
    slc_dir: str        
        Directory to store geocoded SLC files
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped data folder after image processing is done
    """
    logger.info(f'Processing: {zip_file} to a geocoded SLC')
    logger.debug(f'input orbit file: {orbfile}')

    sent = sentinel_parser(zip_file)
    mission_id = sent['mission_id']
    unique_id = sent['unique_id']
    acq_date = sent['start_time'][0:8]
    basename = os.path.basename(zip_file)
    data_dir = os.path.join(proc_dir, basename.replace('.zip','.SAFE'))
    os.makedirs(slc_dir, exist_ok = True)

    if not os.path.exists(data_dir):
        with zipfile.ZipFile(zip_file,'r') as zip_ref:
            zip_ref.extractall(proc_dir)
    logger.debug(f"Contents extracted to current folder")

    # read image size
    rsc = GeoCoordinates(rscfile)

    # first, preprocess the Sentinel products:
    #   1.  Get the number of subswaths
    #   2.  Create a db file for each subswath and each orbit
    #   3.  Create orbtiming file with orbit state vectors
    #   4.  Rename and save the ancillary files needed for deramping the slc
    #   5.  Unpack the geotiff product into floating point slc

    if orbfile is not None:
        logger.debug('*** Using precise orbit ***')
        parse_orbit(orbfile.strip(),zip_file,
                os.path.join(proc_dir,f'{acq_date}.orbtiming'))

    swathfiles = []
    xmlfiles = []
    for subswath in sorted(subswath_list):
        swathfiles.append(
            glob.glob(
                os.path.join(
                data_dir,
                'measurement',
                f'*iw{subswath}*{polarization}*.tiff'))[0])
        xmlfiles.append(
            glob.glob(
                os.path.join(
                data_dir,
                'annotation',
                f'*iw{subswath}*{polarization}*.xml'))[0])

    #  loop over subswaths
    bounds_list = []
    for ifile,fn in enumerate(swathfiles):
        #    for itemp in range(1):  # remove to go back to usual
        #        file=swathfiles[ifile-1] # remove to go back to usual
        # create the orbtiming file, roi.db.X file with metadata, file table for each subswath
        subswath = subswath_list[ifile]
        dbfname = os.path.join(proc_dir, f'{acq_date}_iw{subswath}.db')
        orbfname = os.path.join(proc_dir, f'{acq_date}.orbtiming')
        dcfname = os.path.join(proc_dir,f'{acq_date}_iw{subswath}.dcinfo')
        fmratefname = os.path.join(proc_dir,
                f'{acq_date}_iw{subswath}.fmrateinfo')
        create_db(data_dir, subswath, xmlfiles[ifile],
                dbfname, orbfname, dcfname, fmratefname)
        con = sqlite3.connect(dbfname)

        # create a cursor
        c = con.cursor()
        swathfile='file'
        # get slc product times (may not need these two)
        firsttime=sql_mod.valuef(c,swathfile,'raw_slc_first_line_time')
        lasttime=sql_mod.valuef(c,swathfile,'raw_slc_last_line_time')
        #  Reversing lines or pixels?
        lineTimeOrdering=sql_mod.valuec(c,swathfile,'lineTimeOrdering')
        pixelTimeOrdering=sql_mod.valuec(c,swathfile,'pixelTimeOrdering')
        lineTimeOrdering='Increasing'
        pixelTimeOrdering='Increasing'

        # add ancillary data file names to database
        sql_mod.add_param(c,swathfile,'orbinfo')
        sql_mod.edit_param(c,swathfile,'orbinfo',orbfname,'-','char','')
        sql_mod.add_param(c,swathfile,'dcinfo')
        sql_mod.edit_param(c,swathfile,'dcinfo',dcfname,'-','char','')
        sql_mod.add_param(c,swathfile,'fmrateinfo')
        sql_mod.edit_param(c,swathfile,'fmrateinfo',fmratefname,'-','char','')
        con.commit()
        c.close()
        con.close()

        footprint = footprint_bounds(orbfname, dbfname)
        bounds = dem_bounds(footprint, rsc)
        bounds_list.append(bounds)
        if bounds is None:
            continue

        # extract geotiff file, reversing lines and pixels if necessary
        linereverse='n'
        pixelreverse='n'
        if lineTimeOrdering == 'Decreasing':
            linereverse='y'

        if pixelTimeOrdering == 'Decreasing':
            pixelreverse='y'

        basename = os.path.basename(fn)
        output_slc_file = os.path.join(data_dir,basename.replace('tiff','rawslc'))
        command= 'readgeotiff '+fn.rstrip()+' '+output_slc_file+' '+\
                linereverse+' '+pixelreverse
        logger.info(command)
        ret = subprocess.check_call(command, shell=True)

    # Now, process each subswath to a geocoded slc
    for ifile, fn in enumerate(swathfiles):
        bounds = bounds_list[ifile]
        if bounds is None:
            continue
        subswath = subswath_list[ifile]
        slavedb = os.path.join(proc_dir, f'{acq_date}_iw{subswath}.db')
        deramp_phase_file = os.path.join(proc_dir, f'{acq_date}_iw{subswath}.phase')

        # remove ramp, resample to lat lon, reinsert ramp
        #  save parameters in database file
        con = sqlite3.connect(slavedb.strip())
        # create a cursor
        c = con.cursor()  # update slc entry in database
        sql_mod.add_param(c,'file','demfile')
        sql_mod.add_param(c,'file','rscfile')
        sql_mod.edit_param(c,'file','demfile',demfile,'-','char','DEM file')
        sql_mod.edit_param(c,'file','rscfile',rscfile,'-','char','DEM file')
        sql_mod.add_param(c,'file','raw_slc_file')
        origslcfile = sql_mod.valuec(c,'file','slc_file')
        sql_mod.edit_param(c,'file','raw_slc_file',origslcfile,'-','char',
                'raw, nonderamped slc')
        derampedslcfile = origslcfile.replace('rawslc','rawslc.deramp')
        sql_mod.edit_param(c,'file','slc_file',derampedslcfile,'-','char',
                'deramped slc')
        rawslcfile = sql_mod.valuec(c,'file','raw_slc_file')
        con.commit()
        c.close()
        con.close()

        # deramp the slave file
        command='deramp_burst '+slavedb.strip()+' '+rawslcfile+' '+deramp_phase_file
        logger.info(command)
        os.system(command)

        # and geocode/reramp the slave
        main_slc_file = os.path.join(slc_dir,
                f'{acq_date}_{mission_id}_{unique_id}_iw{subswath}_main.geo')
        sec_slc_file = os.path.join(slc_dir,
                f'{acq_date}_{mission_id}_{unique_id}_iw{subswath}_sec.geo')
        compress_slc_file = os.path.join(slc_dir,
                f'{acq_date}_{mission_id}_{unique_id}_iw{subswath}.geo')
        command = f'geo2rdr_reramp {slavedb} {deramp_phase_file} ' + \
                   f'{main_slc_file} {sec_slc_file} {bounds[0]} ' + \
                   f'{bounds[1]} {bounds[2]} {bounds[3]}'
        logger.info(command)
        os.system(command)

        #compress(main_slc_file, sec_slc_file, compress_slc_file,
        #         rsc.nlat, rsc.nlon)
        # remove the original slc files
        #os.remove(main_slc_file)
        #os.remove(sec_slc_file)
        os.remove(origslcfile)
        os.remove(derampedslcfile)
        os.remove(deramp_phase_file)
        logger.info('Swath processed to common coordinates and coregistered.')
    # Clean up zip files to lessen disk space requirements
    if rm_zipfile:
        os.remove(zip_file)
    # Clean up unzipped SAFE folders to lessen disk space requirements
    if rm_folder:
        shutil.rmtree(dir)

    logger.info('Loop over swaths complete.')

