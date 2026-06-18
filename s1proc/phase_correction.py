import glob
import numpy as np
import os
import shutil
import subprocess

from matplotlib import pyplot as plt
from pathlib import Path
from typing import List

from s1proc import get_bin_path
from s1proc.utils import IfgList
from s1proc._log import setup_logger, set_logging_level
from s1proc.tropo import tropo_preproc, _era5_correction
logger = setup_logger(name = __name__, level = 'INFO')

def get_input_files(input_path:str) -> List[str]:
    """
    Get interferograms to be corrected

    Parameters
    ----------
    input_path: str
        Input path
    
    Returns
    -------
    ifg_list: List[str]
        A list of interferograms to prcoess
    """
    p = Path(input_path)
    if p.is_file():
        return [input_path]
    elif p.is_dir():
        ifg_list = glob.glob(os.path.join(input_path,'*.int'))
        if len(ifg_list) == 0:
            logger.warning('Cannot find any interferogram from the input ' +
                    f'directory {input_path}.')
    else:
        ifg_list = glob.glob(input_path)
        if len(ifg_list) == 0:
            logger.warning('Cannot find any interferogram from the input ' +
                    f'pattern {input_path}.')
    return ifg_list

def phase_correction(
        ifg_path: str|None = None,
        config: str = 'config.yaml',
        verbose: bool = False):
    """
    Run phase correction for wrapped interferograms

    Parameters
    ----------
    ifg_path: str
        Interferograms to be corrected
    config: str
        Configuration file
    verbose: bool
        If True, set the logging level to DEBUG
    """
    if verbose:
        set_logging_level(logger, 'DEBUG')

    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates
    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc

    if ifg_path is None:
        ifg_path = icfg.ifg_path

    ifg_files = get_input_files(ifg_path)  
    nifg = len(ifg_files)
    logger.debug(f'Number of interferograms: {nifg}')
    
    if not os.path.exists(icfg.multilook_rsc_file):
        rsc = GeoCoordinates(icfg.rsc_file)
        rsc = rsc.take_look(pcfg.rowlook, pcfg.collook)
        rsc.save_as_rsc(icfg.multilook_rsc_file)
    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    logger.debug(f'Image shape: {nrow} x {ncol}')

    ifg_list = IfgList(ifg_files)
    output_files = []
    os.makedirs(icfg.ifg_corr_path, exist_ok = True)
    if cfg.tropo.enable:
        logger.info('Tropospheric noise correction')
        tropo_preproc(ifg_path, config, verbose)
        for ifg_file in ifg_files:
            output_file = os.path.join(icfg.ifg_corr_path,
                    os.path.basename(ifg_file))
            _era5_correction(ifg_file, output_file, nrow, ncol,
                    cfg.tropo.parameters, cfg.proc.wavelength)
            output_files.append(output_file)
    
    if len(output_files) == 0:
        output_files = ifg_files
    if cfg.filter.enable:
        fcfg = cfg.filter
        temp_dir = 'temp_filter'
        os.makedirs(temp_dir, exist_ok = True)
        ifglist_file = os.path.join(temp_dir, 'ifg_list')
        with open(ifglist_file, 'w') as f:
            f.write('\n'.join(output_files))
        goldstein = get_bin_path('goldstein')
        command = f'{goldstein} {ifglist_file} {nrow} {ncol} ' + \
                  f'{fcfg.parameters.window_size} ' + \
                  f'{fcfg.parameters.goldstein_alpha}'
        logger.info(command)
        subprocess.check_call(command, shell = True) 

