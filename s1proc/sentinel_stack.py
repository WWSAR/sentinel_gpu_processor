#!/usr/bin/env python4
import glob
import numpy as np
import os
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
        data_dir: str = 'data',
        eof_dir: str = 'eof',
        proc_dir: str = 'proc',
        slc_dir: str = 'slc',
        demfile: str = 'elevation.dem',
        rscfile: str = 'elevation.dem.rsc',
        polarization: Literal['hh','hv','vh','vv'] = 'vv',
        subswath_list: Sequence[int] = [1,2,3],
        rm_rawslc: bool = True,
        rm_zipfile: bool = False,
        rm_folder: bool = False,
        reprocess: bool = False,
        zip_list: Sequence[str]|None = None):
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
    rm_rawslc: bool
        Remove raw SLC files after image processing is done
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped folder after image processing is done
    reprocess: bool
        Reprocess the geo file if it already exists
    zip_list: Sequence[str]
        List of zip files to process
    """

    # get list of geotiff products
    zips = sorted(glob.glob(os.path.join(data_dir,'*.zip')))
    if zip_list is not None:
        filtered_zips = []
        for fn in zips:
            basename = os.path.basename(fn)
            if basename[0:-4] in zip_list:
                filtered_zips.append(fn)
        zips = filtered_zips
    # get the precise orbit files
    preciseorbitlist = sorted(glob.glob(os.path.join(eof_dir,'*.EOF')))
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
        mission_id = sent['mission_id']
        unique_id = sent['unique_id']
        acq_date = sent['start_time'][0:8]
        slcfiles = []
        for subswath in subswath_list:
            main_slc_file = os.path.join(slc_dir,
                    f'{acq_date}_{mission_id}_{unique_id}_iw{subswath}_main.geo')
            sec_slc_file = os.path.join(slc_dir,
                    f'{acq_date}_{mission_id}_{unique_id}_iw{subswath}_sec.geo')
            slcfiles.append(main_slc_file)
            slcfiles.append(sec_slc_file)

        if all([os.path.exists(s) for s in slcfiles]) and not reprocess:
            logger.warning(f'{main_slc_file} and {sec_slc_file} exist, skipping')
            continue

        # Finding the date of acqusition following the naming rule
        current_date = sentinel_acq_time(zip_file)
        orbitfilename = None
        for j in range(norbit):
            if start_date[j] <= current_date and end_date[j] >= current_date:
                orbitfilename = preciseorbitlist[j]
                logger.info(f'Precise orbit file found: {orbitfilename}')
                break
        dem = np.fromfile(demfile, dtype=np.int16)
        hmin = dem.min() - 100
        hmax = dem.max() + 100
        logger.info(f'Minimum elevation: {hmin} m, Maximum elevation: {hmax} m')
        del dem
        sentinel_scene(zip_file, demfile, rscfile, orbitfilename, polarization,
                subswath_list, proc_dir, slc_dir, rm_rawslc, rm_zipfile,
                rm_folder, hmin, hmax)
    logger.info('Loop over scenes complete.')

def run_stack(
        polarization: Literal['hh','hv','vh','vv'] = 'vv',
        subswath_list: Sequence[int] = [1,2,3],
        rm_rawslc: bool = True,
        rm_zipfile: bool = False,
        rm_folder: bool = False,
        reprocess: bool = False,
        zip_list: Sequence[str]|None = None,
        config: str = 'config.yaml'):
    """
    Process a stack of sentinel products to coregistered geocoded SLCS
    
    Parameters
    ----------
    polarization: Literal
        Polarization to process
    subswath_list: Sequence[int]
        Subswaths to process 
    rm_rawslc: bool
        Remove raw SLC files after image processing is done
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped folder after image processing is done
    reprocess: bool
        Reprocess the geo file if it already exists
    zip_list: Sequence[str]
        List of zip files to process
    config: Path|str
        Configuration file
    """
    from s1proc._config import load_config
    icfg,pcfg = load_config(config)
    stack(data_dir=icfg.data_path,
            eof_dir=icfg.eof_path,
            proc_dir=icfg.proc_path,
            slc_dir=icfg.slc_path,
            demfile=icfg.dem_file,
            rscfile=icfg.rsc_file,
            polarization=polarization,
            subswath_list=subswath_list,
            rm_rawslc=rm_rawslc,
            rm_zipfile=rm_zipfile,
            rm_folder=rm_folder,
            reprocess=reprocess,
            zip_list=zip_list)
    return
