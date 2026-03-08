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

from s1proc._log import setup_logger, set_logging_level
from s1proc.sario import sentinel_parser 
from s1proc import sql_mod
from s1proc.precise_orbit import parse_orbit
from s1proc.sentinel_roidb import create_db
logger = setup_logger(name = __name__, level = 'INFO')

def sentinel_scene(
        zip_file: str,
        orbfile: str|None = None,
        polarization: str = 'vv',
        subswath_list: Sequence[int] = [1,2,3],
        rm_zipfile: bool = False,
        rm_folder: bool = False):
    """
    Process one sentinel scene files to coregistered geocoded slc

    Parameters
    ----------
    zip_file: str
        Input zip file
    orbfile: str|None
        Precise orbit file. If none, coarse orbit will be used
    polarization: str
        Polarization(s) to process
    subswath_list: Sequence[int]
        Subswaths to process
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped data folder after image processing is done
    """

    logger.info(f'Processing: {zip_file} to a geocoded SLC')
    logger.debug(f'input orbit file: {orbfile}')

    sent = sentinel_parser(zip_file)
    acq_date = sent['start_time'][0:8]
    data_dir = zip_file.replace('.zip','.SAFE')

    if not os.path.exists(data_dir):
        with zipfile.ZipFile(zip_file,'r') as zip_ref:
            zip_ref.extractall('.')
    logger.debug(f"Contents extracted to current folder")

    # first, preprocess the Sentinel products:
    #   1.  Get the number of subswaths
    #   2.  Create a db file for each subswath and each orbit
    #   3.  Create orbtiming file with orbit state vectors
    #   4.  Rename and save the ancillary files needed for deramping the slc
    #   5.  Unpack the geotiff product into floating point slc

    #  how many subswaths?
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
    #with open('swathfiles','w') as f:
    #    f.write('\n'.join(swathfiles))
    #with open('xmlfiles','w') as f:
    #    f.write('\n'.join(xmlfiles))

    #  retrieve scene start and stop times from scene name

    #  loop over subswaths
    for ifile,fn in enumerate(swathfiles):
        #    for itemp in range(1):  # remove to go back to usual
        #        file=swathfiles[ifile-1] # remove to go back to usual
        # create the orbtiming file, roi.db.X file with metadata, file table for each subswath
        subswath = subswath_list[ifile]
        dbfname = f'{data_dir}.db.{subswath}'
        orbfname = f'{data_dir}.orbtiming'
        dcfname = f'{data_dir}.dcinfo.{subswath}'
        fmratefname = f'{data_dir}.fmrateinfo.{subswath}'
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
        con.commit()
        c.close()
        con.close()

    if orbfile is not None:
        logger.debug('*** Using precise orbit ***')
        parse_orbit(orbfile.strip(),zip_file)

    # Now, process each subswath to a geocoded slc
    for ifile, fn in enumerate(swathfiles):
        subswath = subswath_list[ifile]
        slavedb = data_dir.strip()+'.db.'+str(subswath)

        # remove ramp, resample to lat lon, reinsert ramp
        #  save parameters in database file
        con = sqlite3.connect(slavedb.strip())
        # create a cursor
        c = con.cursor()  # update slc entry in database
        sql_mod.add_param(c,'file','raw_slc_file')
        origslcfile = sql_mod.valuec(c,'file','slc_file')
        sql_mod.edit_param(c,'file','raw_slc_file',origslcfile,'-','char',
                'raw, nonderamped slc')
        derampedslcfile = origslcfile.replace('rawslc','rawslc.deramp')
        sql_mod.edit_param(c,'file','slc_file',derampedslcfile,'-','char',
                'deramped slc')
        rawslcfile = sql_mod.valuec(c,'file','raw_slc_file')
        nrange = sql_mod.valuef(c,'file','samplesPerBurst')
        nazimuth = sql_mod.valuef(c,'file','linesPerBurst')
        con.commit()
        c.close()
        con.close()
        logger.info(f'nrange:{nrange}, nazimuth:{nazimuth}')
        # deramp the slave file
        command='deramp_burst '+slavedb.strip()+' '+rawslcfile
        logger.info(command)
        os.system(command)

        # and geocode/reramp the slave
        outfile = f'{acq_date}_iw{subswath}.geo'
        overlapfile = f'{acq_date}_iw{subswath}_overlap.geo'
        command = 'geo2rdr_reramp '+slavedb.strip()+' '+outfile + ' ' + \
                  overlapfile
        logger.info(command)
        os.system(command)

        # remove the original slc files
        os.remove(origslcfile)
        os.remove(derampedslcfile)
        logger.info('Swath processed to common coordinates and coregistered.')
    # Clean up zip files to lessen disk space requirements
    if rm_zipfile:
        os.remove(zip_file)
    # Clean up unzipped SAFE folders to lessen disk space requirements
    if rm_folder:
        shutil.rmtree(dir)

    logger.info('Loop over swaths complete.')

