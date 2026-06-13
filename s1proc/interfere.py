#!/usr/bin/env python3
import glob
import numpy as np
import os
from typing import Tuple, Sequence, Literal, List

from s1proc import get_bin_path
from s1proc import geocoordinates
from s1proc.sario import CroppedImage, Subswath, BurstGroup, NHEAD
from s1proc._log import setup_logger, set_logging_level
logger = setup_logger(name=__name__,level='INFO')

def match_bursts(
        ref_subswath: Subswath,
        sec_subswath: Subswath)->List[Tuple[str, str]]:
    """
    Match bursts in two subswaths to create interferometric image pairs

    Parameters
    ----------
    ref_subswath: Subswath
        Reference subswath
    sec_subswath: Subswath
        Secondary subswath

    Returns
    -------
    burst_pairs: List[Tuple[str,str]]
       List of burst pairs 
    """
    burst_pairs = []
    if ref_subswath.is_empty() or sec_subswath.is_empty():
        return burst_pairs
    sec_tops = np.array([b.top for b in sec_subswath.bursts])
    sec_bottoms = np.array([b.bottom for b in sec_subswath.bursts])
    for ref_burst in ref_subswath.bursts:
        ref_top = ref_burst.top
        ref_bottom = ref_burst.bottom
        idx_diff = np.abs(sec_tops - ref_top) + \
                np.abs(sec_bottoms - ref_bottom)
        best_match = np.argmin(idx_diff)
        burst_pairs.append(
                (ref_burst.data, sec_subswath.bursts[best_match].data))
        print(burst_pairs[-1])
    return burst_pairs

def interfere_subswath(
    ref_subswath: Subswath,
    sec_subswath: Subswath,
    ifg_path: str,
    rowlook: int,
    collook: int,
    out_float: bool)->List[str]:
    """
    Form an interferogram from two subswath geo-coded SLC images

    Parameters
    ----------
    ref_subswath: Subswath
        Reference subswath
    sec_subswath: Subswath
        Secondary subswath
    rowlook: int
        Number of looks in the row direction
    collook: int
        Number of looks in the column direction
    out_float: bool
        Only output phase

    Returns
    -------
    ifg_list: List[str]
        List of output burst interferograms
    """
    burst_pairs = match_bursts(ref_subswath, sec_subswath)
    crossmul = get_bin_path('crossmul')
    out_float_flag = 1 if out_float else 0 
    burst_ifgs = []
    unique_indices1 = []
    unique_indices2 = []
    for main_img_file, sec_img_file in burst_pairs:
        basename1 = os.path.basename(main_img_file)
        name1,_ = os.path.splitext(basename1)
        unique_indices1.append(name1[0:20])
        basename2 = os.path.basename(sec_img_file)
        name2,_ = os.path.splitext(basename2)
        unique_indices2.append(name2[0:20])
        outfile = os.path.join(ifg_path, name1+'_'+name2+'.int')
        burst_ifgs.append(outfile)
        if os.path.exists(outfile):
            continue
        command = f'{crossmul} {main_img_file} {sec_img_file} {outfile} ' + \
                  f'{rowlook} {collook} {out_float_flag}'
        logger.info(command)
        os.system(command)
    if len(burst_pairs) > 0:
        subswath = Subswath(burst_ifgs)
        left, top, right, bottom = subswath.bounds()
        for i, burst in enumerate(subswath.bursts):
            if i == 0:
                ifg_data = burst.load_data(left, top, right, bottom)
                continue
            old_data = ifg_data[
                    burst.top - top : burst.bottom - top,
                    burst.left - left: burst.right - left]
            new_data = burst.load_data(
                    burst.left, burst.top, burst.right, burst.bottom)
            if out_float:
                replace_mask = (old_data == 0) & (new_data != 0)
            else:
                replace_mask = (old_data.real == 0) & \
                        (new_data.real != 0)
            overlap_mask = None
            if unique_indices1[i] != unique_indices1[i-1] or \
               unique_indices2[i] != unique_indices2[i-1]:
                if out_float:
                    overlap_mask = (old_data != 0) & (new_data != 0)
                else:
                    overlap_mask = (old_data.real != 0) & (new_data != 0)
            if overlap_mask is not None and np.any(overlap_mask): 
                if out_float:
                    phase_diff = np.exp(1j*(-old_data[overlap_mask] + \
                            new_data[overlap_mask]))
                else:
                    phase_diff = np.conj(old_data[overlap_mask]) * \
                            new_data[overlap_mask]
                mean_phase_diff = np.angle(np.mean(phase_diff)) 
                phase_diff = phase_diff * np.exp(-1j*mean_phase_diff)
                med_phase_diff = np.median(np.angle(phase_diff))
                mean_phase_diff += med_phase_diff
                logger.info(f'mean phase offset: {mean_phase_diff} rad')
                if out_float:
                    new_data = new_data - mean_phase_diff
                    new_data[ifg > np.pi] -= 2*np.pi
                    new_data[ifg < -np.pi] += 2*np.pi
                else:
                    new_data = new_data * np.exp(-1j*mean_phase_diff)
            old_data[replace_mask] = new_data[replace_mask]
        ifg = CroppedImage(subswath.nrow0, subswath.ncol0, left, top, right,
                bottom, ifg_data)
        #for subifg_file in subifg_files:
        #    os.remove(subifg_file)
        return ifg
    else:
        return None

def stitch_patches(patch, ifg, left, right, out_float):
    temp = patch[:,left:right]
    if out_float:
        overlap_mask = (temp!=0) & (ifg!=0)
    else:
        overlap_mask = (temp.real!=0) & (ifg.real!=0)
    if np.any(overlap_mask):
        if out_float:
            phase_diff = np.exp(1j*(-temp[overlap_mask] + ifg[overlap_mask]))
        else:
            phase_diff = np.conj(temp[overlap_mask]) * ifg[overlap_mask]
        mean_phase_diff = np.angle(np.mean(phase_diff)) 
        phase_diff = phase_diff * np.exp(-1j*mean_phase_diff)
        med_phase_diff = np.median(np.angle(phase_diff))
        mean_phase_diff += med_phase_diff
        logger.info(f'mean phase offset: {mean_phase_diff} rad')
        if out_float:
            replace_mask = (temp == 0) & (ifg != 0)
            ifg = ifg - mean_phase_diff
            ifg[ifg > np.pi] -= 2*np.pi
            ifg[ifg < -np.pi] += 2*np.pi
        else:
            replace_mask = temp.real == 0
            ifg = ifg * np.exp(-1j*mean_phase_diff)
    else:
        if out_float:
            replace_mask = (temp == 0) & (ifg != 0)
        else:
            replace_mask = temp.real == 0
    temp[replace_mask] = ifg[replace_mask]
    return

def interfere_single_scene(
        ref_burst_group: BurstGroup,
        sec_burst_group: BurstGroup,
        ifg_path: str,
        outfile: str,
        rowlook: int,
        collook: int,
        out_float: bool = False) -> str:
    """
    Form an interferogram for a single scene

    Parameters
    ----------
    ref_burst_group:
        bursts of the reference image
    sec_burst_group:
        bursts of the secondary image
    ifg_path: str
        Directory to save interferograms
    outfile: str
        Output interferogram
    rowlook: int
        Number of looks in the row direction
    collook: int
        Number of looks in the column direction
    out_float: bool
        Only write phase to disk
    """
    subswath_ifgs = []
    for i in range(3):
        subswath_ifgs.append(interfere_subswath(
                ref_burst_group.subswaths[i],
                sec_burst_group.subswaths[i],
                ifg_path, rowlook, collook, out_float))
    for subswath_ifg in subswath_ifgs:
        if subswath_ifg is not None:
            nrow0, ncol0 = subswath_ifg.nrow0, subswath_ifg.ncol0
    if out_float:
        mmap_arr = np.memmap(outfile, dtype = np.float32, mode = 'w+',
               shape = (nrow0, ncol0))
    else:
        mmap_arr = np.memmap(outfile, dtype = np.complex64, mode = 'w+',
               shape = (nrow0, ncol0))
    for subswath_ifg in subswath_ifgs:
        if subswath_ifg is None:
            continue
        old_data = mmap_arr[subswath_ifg.top:subswath_ifg.bottom,
                        subswath_ifg.left:subswath_ifg.right]
        new_data = subswath_ifg.data
        if out_float:
            replace_mask = new_data != 0
        else:
            replace_mask = new_data.real != 0
        old_data[replace_mask] = new_data[replace_mask]
    mmap_arr.flush()

def parse_fname(fn:str)->Tuple[str, str]:
    basename = os.path.basename(fn)
    date = basename[0:8]
    data_id = basename[9:20]
    return date, data_id

def interfere(
        img_pair_file: str,
        slc_path: str,
        rscfile: str,
        multirscfile: str,
        direction: Literal['asc','desc'],
        ifg_path: str = 'igrams',
        rowlook: int = 1,
        collook: int = 1,
        out_float: bool = False,
        verbose: bool = False):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    img_pair_file: str
        File containing pairs of subswath images
    slc_path: str
        Directory of SLC files
    rscfile: str
        rsc file
    direction: Literal['asc','desc']
        Flight direction
    ifg_path: str
        Directory to save interferograms
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    out_float: bool
        Output float rather than cpx images
    verbose: bool
        Set the logging level to debug
    """
    if verbose:
        set_logging_level(logger, 'DEBUG')
    os.makedirs(ifg_path, exist_ok = True)
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook,collook)
    if os.path.dirname(multirscfile):
        os.makedirs(os.path.dirname(multirscfile),exist_ok=True)
    rsclook.save_as_rsc(multirscfile)
    
    ref_dates = []
    sec_dates = []
    date_burst_map = {}
    with open(os.path.join(ifg_path, img_pair_file), 'r') as f:
        for line in f.readlines():
            words = line.split()
            ref_date = words[0]
            sec_date = words[1]
            ref_dates.append(ref_date)
            sec_dates.append(sec_date)
            if not (ref_date in date_burst_map):
                burst_files = glob.glob(
                        os.path.join(slc_path,f'{ref_date}*.gslc'))
                burst_group = BurstGroup(burst_files)
                date_burst_map[ref_date] = burst_group
            if not (sec_date in date_burst_map):
                burst_files = glob.glob(
                        os.path.join(slc_path,f'{sec_date}*.gslc'))
                burst_group = BurstGroup(burst_files)
                date_burst_map[sec_date] = burst_group

    for ref_date, sec_date in zip(ref_dates, sec_dates):
        ref_burst_group = date_burst_map[ref_date]
        sec_burst_group = date_burst_map[sec_date]
        outfile = os.path.join(ifg_path, f'{ref_date}_{sec_date}.int')
        interfere_single_scene(
                ref_burst_group, sec_burst_group,
                ifg_path, outfile, rowlook, collook, out_float)

def run_interfere(
        out_float: bool = False,
        verbose: bool = False,
        config: str = 'config.yaml'):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    out_float: bool
        Output float rather than cpx images
    verbose: bool
        Set the logging level to debug
    """
    from s1proc._config import load_config
    icfg,pcfg = load_config(config)
    interfere(
        img_pair_file=icfg.img_pair_file,
        slc_path=icfg.slc_path,
        rscfile=icfg.rsc_file,
        multirscfile=icfg.multilook_rsc_file,
        direction=pcfg.direction,
        ifg_path=icfg.ifg_path,
        rowlook=pcfg.rowlook,
        collook=pcfg.collook,
        out_float=out_float,
        verbose=verbose)
    return
