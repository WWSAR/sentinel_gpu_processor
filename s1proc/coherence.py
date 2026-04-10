import glob
import numpy as np
import os
import shutil
from typing import Sequence
from tqdm import tqdm

from s1proc import geocoordinates
from s1proc import get_bin_path
from s1proc.sario import CroppedImage
from s1proc._log import setup_logger, set_logging_level
logger = setup_logger(name=__name__,level='INFO')

def multilook(slc_list: Sequence[str],
              rowlook: int,
              collook: int):
    """
    multilook a geocoded SLC image 

    Parameters
    ----------
    slc_list: Sequence[str]
        List of subswath images
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    exe = get_bin_path('multilook_amp')
    temp_dir = 'temp'
    os.makedirs(temp_dir, exist_ok = True)
    amp_list = []
    for slc_file in slc_list:
        basename = os.path.basename(slc_file)
        outfile = os.path.join(temp_dir, basename+'.amp')
        amp_list.append(outfile)
        command = f'{exe} {slc_file} {outfile} {rowlook} {collook}'
        os.system(command)
    nsubswath = len(slc_list)
    if nsubswath == 1:
        ci1 = CroppedImage.fromfile(amp_list[0], load_data = True,
                dtype = np.float32)
        amp = ci1.data
    else:
        ci1 = CroppedImage.from_file(amp_list[0], dtype = np.float32)
        amp = ci1.load_data()
        for i in range(1, nsubswath):
            ci2 = CroppedImage.from_file(amp_list[i], dtype = np.float32)
            amp2 = ci2.load_data()
            mask = amp == 0
            amp[mask] = amp2[mask]
    shutil.rmtree(temp_dir)
    return amp

def multilook_amp(
        slc_dir: str,
        rscfile: str,
        /,
        amp_dir: str = 'amp',
        rowlook: int = 1,
        collook: int = 1):
    """
    multilook all geocoded SLC images

    Parameters
    ----------
    slc_dir: str
        Directory of geocoded SLC images
    rscfile: str
        rsc file
    amp_dir: str
        Directory to save multilooked amplitude images
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    os.makedirs(amp_dir, exist_ok = True)
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook,collook)
    rsclook.save_as_rsc(os.path.join(amp_dir,'dem.rsc'))

    slc_list = glob.glob(os.path.join(slc_dir, '*main.geo'))
    date_list = sorted(np.unique([os.path.basename(s)[0:8] for s in slc_list]))
    nslc = len(date_list)
    for date in tqdm(date_list, desc = 'multilook'):
        sub_slc_list = glob.glob(os.path.join(slc_dir, f'{date}*main.geo'))
        outfile = os.path.join(amp_dir, f'{date}.amp')
        amp = multilook(sub_slc_list, rowlook, collook)
        amp.tofile(outfile)

def coherence(
        ifg_dir: str,
        slc_dir: str,
        rscfile: str,
        /,
        amp_dir: str = 'amp',
        rowlook: int = 1,
        collook: int = 1):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    ifg_dir: str
        Directory of interferograms
    slc_dir: str
        Directory of geocoded SLC images
    rscfile: str
        rsc file
    amp_dir: str
        Directory to save multilooked amplitude images
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    os.makedirs(amp_dir, exist_ok = True)
    if len(glob.glob(os.path.join(amp_dir, '*.amp'))) == 0:
        multilook_amp(slc_dir, rscfile, amp_dir, rowlook, collook)
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook,collook)
    ifg_list = sorted(glob.glob(os.path.join(ifg_dir,'*.int')))
    prev_date1 = None
    for ifg_file in tqdm(ifg_list, desc = 'coherence'):
        out_file = ifg_file.replace('.int', '.cc')
        ifg = np.fromfile(ifg_file, dtype = np.complex64)
        basename = os.path.basename(ifg_file)
        date1 = basename[0:8]
        date2 = basename[9:17]
        if date1 != prev_date1:
            amp1 = np.fromfile(os.path.join(amp_dir, f'{date1}.amp'),
                               dtype = np.float32)
            amp1[amp1 == 0] = 1e-10
        amp2 = np.fromfile(os.path.join(amp_dir, f'{date2}.amp'),
                           dtype = np.float32)
        amp2[amp2 == 0] = 1e-10
        c = np.abs(ifg)/amp1/amp2 
        c.astype(np.float32).tofile(out_file)
