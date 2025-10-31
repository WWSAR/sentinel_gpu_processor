import numpy as np
import os
import shutil
from matplotlib import pyplot as plt
plt.rcParams['image.interpolation'] = 'none'

import sario
import geocoordinates

def stitch(intfile1,intfile2,ncol):
    ifg1 = sario.readslc(intfile1,ncol)
    ifg2 = sario.readslc(intfile2,ncol)
    mask1 = np.abs(ifg1)>1e-3
    mask2 = np.abs(ifg2)>1e-3
    common_mask = mask1 & mask2
    ifg_diff = np.conj(ifg1)*ifg2
    phase_diff = np.angle(np.mean(ifg_diff[common_mask]))
    #print(phase_diff)
    ifg2 = ifg2 * np.exp(-1j*phase_diff)
    ifg_stitch = ifg1*mask1 + ifg2*mask2*(~mask1)
    return ifg_stitch

def main():
    rsc = geocoordinates.GeoCoordinates('dem.rsc')
    os.makedirs('old_int',exist_ok=True)
    _,ncol = rsc.nlat,rsc.nlon
    with open('intlist','r') as f:
        intlist = f.readlines() 
    intlist = [x.strip() for x in intlist]
    for i in range(len(intlist)-1):
        intfile1 = intlist[i]
        intfile2 = intlist[i+1]
        if intfile1 == intfile2[1:]:
            print('Stitching ',intfile1,intfile2)
            ifg_stitch = stitch(intfile1,intfile2,ncol)
            shutil.move(intfile1,os.path.join('old_int',intfile1))
            shutil.move(intfile2,os.path.join('old_int',intfile2))
            sario.saveslc(ifg_stitch,intfile1)

if __name__ == "__main__":
    main()
