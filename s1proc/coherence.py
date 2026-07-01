import glob
import os
from typing import Sequence

import numpy as np
from tqdm import tqdm

from s1proc._log import setup_logger
from s1proc.sario import CroppedImage, powlooks, savec

logger = setup_logger(name=__name__, level="INFO")


def multilook(slc_list: Sequence[str], outfile: str, rowlook: int, collook: int):
    """
    multilook a geocoded SLC image

    Parameters
    ----------
    slc_list: Sequence[str]
        List of subswath images
    outfile: str
        Output amplitude image
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    mmap_arr = None
    for i in range(len(slc_list)):
        ci = CroppedImage.from_file(slc_list[i], load_data=True)
        left_sm = (ci.left + collook - 1) // collook
        top_sm = (ci.top + rowlook - 1) // rowlook
        right_sm = ci.right // collook
        bottom_sm = ci.bottom // rowlook
        if mmap_arr is None:
            nrow_sm = ci.nrow0 // rowlook
            ncol_sm = ci.ncol0 // collook
            mmap_arr = np.memmap(
                outfile, dtype=np.float32, mode="w+", shape=(nrow_sm, ncol_sm)
            )
        row_start = top_sm * rowlook - ci.top
        row_end = bottom_sm * rowlook - ci.top
        col_start = left_sm * collook - ci.left
        col_end = right_sm * collook - ci.left
        new_data = np.sqrt(
            powlooks(ci.data[row_start:row_end, col_start:col_end], rowlook, collook)
        )
        if i == 0:
            mmap_arr[top_sm:bottom_sm, left_sm:right_sm] = new_data
        else:
            old_data = mmap_arr[top_sm:bottom_sm, left_sm:right_sm]
            replace_mask = (old_data == 0) & (new_data != 0)
            old_data[replace_mask] = new_data[replace_mask]
    mmap_arr.flush()


def multilook_amp(
    slc_dir: str, amp_dir: str = "amp", rowlook: int = 1, collook: int = 1
):
    """
    multilook all geocoded SLC images

    Parameters
    ----------
    slc_dir: str
        Directory of geocoded SLC images
    amp_dir: str
        Directory to save multilooked amplitude images
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    os.makedirs(amp_dir, exist_ok=True)
    slc_list = glob.glob(os.path.join(slc_dir, "*.gslc"))
    date_list = sorted(np.unique([os.path.basename(s)[0:8] for s in slc_list]))
    for date in tqdm(date_list, desc="multilook"):
        sub_slc_list = glob.glob(os.path.join(slc_dir, f"{date}*.gslc"))
        outfile = os.path.join(amp_dir, f"{date}.amp")
        if os.path.exists(outfile):
            continue
        multilook(sub_slc_list, outfile, rowlook, collook)


def run_multilook_amp(config: str = "config.yaml"):
    """
    multilook all geocoded SLC images

    Parameters
    ----------
    config: Path|str
        Configuration file
    """
    from s1proc._config import load_config

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    multilook_amp(
        slc_dir=icfg.slc_path,
        amp_dir=icfg.amp_path,
        rowlook=pcfg.rowlook,
        collook=pcfg.collook,
    )
    return


def coherence(
    ifg_dir: str, amp_dir: str, nrow: int, ncol: int, rowlook: int, collook: int
):
    """
    Compute InSAR phase coherence

    Parameters
    ----------
    ifg_dir: str
        Directory of interferograms
    amp_dir: str
        Directory to save multilooked amplitude images
    nrow: int
        Number of rows of each interferogram
    ncol: int
        Number of columns of each interferogram
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    os.makedirs(amp_dir, exist_ok=True)
    ifg_list = sorted(glob.glob(os.path.join(ifg_dir, "*.int")))
    prev_date1 = None
    for ifg_file in tqdm(ifg_list, desc="coherence"):
        out_file = ifg_file.replace(".int", ".cc")
        ifg = np.fromfile(ifg_file, dtype=np.complex64)
        basename = os.path.basename(ifg_file)
        date1 = basename[0:8]
        date2 = basename[9:17]
        if date1 != prev_date1:
            amp1 = np.fromfile(os.path.join(amp_dir, f"{date1}.amp"), dtype=np.float32)
        amp2 = np.fromfile(os.path.join(amp_dir, f"{date2}.amp"), dtype=np.float32)
        c = np.abs(ifg) / (amp1 + 1e-12) / (amp2 + 1e-12)
        invalid_mask = c > 1
        c[invalid_mask] = np.random.rand(np.sum(invalid_mask))
        c = amp1 * amp2 + 1j * c
        c = np.reshape(c, (nrow, ncol))
        savec(c, out_file)


def run_coherence(config: str = "config.yaml"):
    """
    Compute InSAR phase coherence

    Parameters
    ----------
    config: Path|str
        Configuration file
    """
    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    multilook_amp(
        slc_dir=icfg.slc_path,
        amp_dir=icfg.amp_path,
        rowlook=pcfg.rowlook,
        collook=pcfg.collook,
    )
    coherence(
        ifg_dir=icfg.ifg_path,
        amp_dir=icfg.amp_path,
        nrow=rsc.nlat,
        ncol=rsc.nlon,
        rowlook=pcfg.rowlook,
        collook=pcfg.collook,
    )
    return
