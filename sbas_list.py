#!/usr/bin/env -S python3 -u
# create a list of sbas pairs

import glob
import numpy as np
import os
import sys
from datetime import datetime 

import geocoordinates
import geometry
import orbit

def read_sentinel_orbit(orbfile):
    tt = None
    xx = None
    vv = None
    with open(orbfile,"r") as f:
        line = f.readline()
        nstatvec = int(line.strip())
        tt = np.zeros(nstatvec)
        xx = np.zeros((nstatvec,3))
        vv = np.zeros((nstatvec,3))
        for i in range(nstatvec):
            line = f.readline()
            words = line.split()
            num = np.array([float(s) for s in words])
            tt[i]  = num[0]
            xx[i,:] = num[1:4]
            vv[i,:] = num[4:7]
    return tt,xx,vv

def estimatebaseline(orbfile1,orbfile2,demfile,demrscfile):
    rsc = geocoordinates.GeoCoordinates(demrscfile)
    nrow,ncol = rsc.nlat, rsc.nlon
    with open(demfile,"r") as f:
        f.seek(nrow*ncol//2*2)
        hmid = np.fromfile(f,dtype=np.int16,count=1)[0]
    latmid = rsc.latmax + nrow//2*rsc.dlat
    lonmid = rsc.lonmin + ncol//2*rsc.dlon
    llh = np.array([latmid,lonmid,hmid])

    tt1,xx1,vv1 = read_sentinel_orbit(orbfile1)
    mid_idx = len(tt1)//2
    tmid1= tt1[mid_idx]
    xmid1= xx1[mid_idx,:]
    vmid1= vv1[mid_idx,:]
    tt2,xx2,vv2 = read_sentinel_orbit(orbfile2)
    mid_idx = len(tt2)//2
    tmid2 = tt2[mid_idx]
    xmid2 = xx2[mid_idx,:]
    vmid2 = vv2[mid_idx,:]
    xyz = geometry.llh2xyz(llh)
    dr1,_ = orbit.orbitrangetime(tt1,xx1,vv1,xyz,tmid1,xmid1,vmid1)
    dr2,_ = orbit.orbitrangetime(tt2,xx2,vv2,xyz,tmid2,xmid2,vmid2)
    u1 = -dr1/np.linalg.norm(dr1)
    u2 = -dr2/np.linalg.norm(dr2)
    uperp = np.cross(u1,u2)
    theta = np.arcsin(np.linalg.norm(uperp)) 
    vdotuperp = np.dot(vmid1,uperp)
    if vdotuperp>0:
        bperp = np.linalg.norm(dr1)*theta
    else:
        bperp = -np.linalg.norm(dr1)*theta
    return bperp

if len(sys.argv) < 3:
    print ('Usage: sbas_list.py max_temporal max_spatial')
    sys.exit(1)

maxtemporal=float(sys.argv[1])
maxspatial=float(sys.argv[2])

# get the PATH of the script directory
PATH=os.path.dirname(os.path.abspath(sys.argv[0]))

#  get a list of the sorted geocoded slc files
# .geo format e.g. S1A_20150503.geo for char1=7
geos = glob.glob(os.path.join('..','*.geo'))
geos = np.sort(geos)
# print(geos)

#names_times=[]
jdlist = []
for geo in geos:
    # .geo format e.g. S1A_20150503.geo for char1=7
    char1=7+13
    scenedate=geo[char1:char1+8]
    jd = datetime.strptime(scenedate, '%Y%m%d').toordinal()+1721424.5
    print('Julian day ',jd)
    #names_times.append(geo+' '+str(jd))
    jdlist.append(jd)

#  estimate baseline and create a file for the time-baseline plot
ftb=open('sbas_list','w') 
demfile = os.path.join('..','elevation.dem')
demrscfile = os.path.join('..','elevation.dem.rsc')
#  call the spatial baseline estimator
for i in range(0,len(geos)-1):
    orbfile1 = geos[i].strip().replace('geo','orbtiming')
    for j in range(i+1,len(geos)):
        print ('Interferograms: '+str(i)+' '+str(j))
        orbfile2 = geos[j].strip().replace('geo','orbtiming')
        bperp = estimatebaseline(orbfile1,orbfile2,demfile,demrscfile) 
        if abs(bperp) <= maxspatial:
            temp_bl=abs(jdlist[j]-jdlist[i])
            if temp_bl <= maxtemporal:
                ftb.write(f"{geos[i].strip()} {geos[j].strip()} {temp_bl} {bperp}\n")

print('sbas_list written')
ftb.close()

