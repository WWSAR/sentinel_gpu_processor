#!/usr/bin/env python3
import sys
import os
import glob
import subprocess
import shutil
from datetime import datetime
from typing import Literal, Sequence

from s1proc._log import setup_logger, set_logging_level
from s1proc.sario import sentinel_acq_time, sentinel_parser
from s1proc.sentinel_scene import sentinel_scene 
logger = setup_logger(name = __name__, level = 'INFO')

def parse_orbitfilename(orbitfilelist):
    start_date = []
    end_date = []
    for orbitfile in orbitfilelist:
        words = orbitfile.split('_')
        s1 = words[-2]
        start_date_str = s1[1:9]
        s2 = words[-1]
        end_date_str = s2[0:8]
        start_date.append(datetime.strptime(start_date_str,"%Y%m%d"))
        end_date.append(datetime.strptime(end_date_str,"%Y%m%d"))
    return start_date,end_date

def stack(
        data_dir: str = '.',
        eof_dir: str = '.',
        proc_dir: str = 'proc',
        slc_dir: str = 'slc',
        demfile: str = 'elevation.dem',
        rscfile: str = 'elevation.dem.rsc',
        polarization: Literal['hh','hv','vh','vv'] = 'vv',
        subswath_list: Sequence[int] = [1,2,3],
        rm_zipfile: bool = False,
        rm_folder: bool = False,
        reprocess: bool = False):
    """
    Process a stack of sentinel products to coregistered geocoded SLCS
    
    Parameters
    ----------
    data_dir: str
        Data folder of Sentinel-1 zipfiles
    eof_dir: str
        Data folder of precise orbit EOF files
    proc_dir: str
        Data folder to store temporary files
    slc_dir : str
        Data folder to store geocoded SLCs
    demfile: str
        DEM file
    rscfile: str
        rsc file
    polarization: Literal
        Polarization to process
    subswath_list: Sequence[int]
        Subswaths to process 
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped folder after image processing is done
    reprocess: bool
        Reprocess the geo file if it already exists
    """

    # get list of geotiff products
    zips = sorted(glob.glob(os.path.join(data_dir,'*.zip')))
    # get the precise orbit files
    preciseorbitlist = glob.glob(os.path.join(eof_dir,'*.EOF'))
    #with open('preciseorbitfiles','w') as f:
    #    f.write('\n'.join(preciseorbitlist))
    start_date,end_date = parse_orbitfilename(preciseorbitlist)
    norbit = len(preciseorbitlist)

    # loop over directories and process each with sentinel_scene
    # sentinel_scene needs zip_file and precise orbit if available
    for ifile,zip_file in enumerate(zips):
        #  which precise orbit file for this scene?
        logger.info(f'Processing {zip_file}')
        sent = sentinel_parser(zip_file)
        #geofile = sent['start_time'][0:8] + '.geo'
        #if os.path.exists(geofile):
        #    if reprocess:
        #        os.remove(geofile)
        #    else:
        #        logger.warning(f'{geofile} exists')
        #        continue

        # Finding the date of acqusition following the naming rule
        current_date = sentinel_acq_time(zip_file)
        for j in range(norbit):
            if start_date[j] <= current_date and end_date[j] >= current_date:
                orbitfilename = preciseorbitlist[j]
                logger.info(f'Precise orbit file found: {orbitfilename}')
                break
        sentinel_scene(zip_file, demfile, rscfile, orbitfilename, polarization,
                subswath_list, proc_dir, slc_dir, rm_zipfile, rm_folder)
    logger.info('Loop over scenes complete.')
