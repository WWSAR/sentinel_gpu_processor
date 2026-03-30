import numpy as np
import os
import shutil
from pathlib import Path
from matplotlib import pyplot as plt
plt.rcParams['image.interpolation'] = 'none'

from s1proc import sario
from s1proc import geocoordinates
from s1proc._log import setup_logger, set_logging_level
logger = setup_logger(name=__name__,level='INFO')

def stitch(intfile1:Path|str,
           intfile2:Path|str,
           ncol:int)->np.ndarray:
    """
    Stitch two interferograms on the same path

    Parameters
    ----------
    intfile1: Path|str
        Path to the first interferogram
    intfile2: Path|str
        Path to the second interferogram
    ncol: int
        Number of columns of each interferogram

    Returns
    -------
    ifg_stitch: np.ndarray
        Stitched interferogram
    """
    ifg1 = sario.readslc(intfile1,ncol)
    ifg2 = sario.readslc(intfile2,ncol)
    mask1 = np.abs(ifg1)>1e-3
    mask2 = np.abs(ifg2)>1e-3
    common_mask = mask1 & mask2
    ifg_diff = np.conj(ifg1)*ifg2
    phase_diff = np.angle(np.mean(ifg_diff[common_mask]))
    logger.debug(f'phase difference between {intfile1} and {intfile2} :'
                 f'{phase_diff} rad')
    ifg2 = ifg2 * np.exp(-1j*phase_diff)
    ifg_stitch = ifg1*mask1 + ifg2*mask2*(~mask1)
    return ifg_stitch

def main():
    """
    Stitch interferograms with the same path number
    """
    rsc = geocoordinates.GeoCoordinates('dem.rsc')
    os.makedirs('old_int',exist_ok=True)
    _,ncol = rsc.nlat,rsc.nlon
    with open('intlist','r') as f:
        intlist = f.readlines() 
    intlist = [x.strip() for x in intlist]
    for i in range(len(intlist)):
        intfile1 = intlist[i]
        intfile2 = 'a'+intfile1
        if intfile2 in intlist:
            logger.info(f'Stitching {intfile1} and {intfile2}')
            ifg_stitch = stitch(intfile1,intfile2,ncol)
            shutil.move(intfile1,os.path.join('old_int',intfile1))
            shutil.move(intfile2,os.path.join('old_int',intfile2))
            sario.saveslc(ifg_stitch,intfile1)

if __name__ == "__main__":
    main()
