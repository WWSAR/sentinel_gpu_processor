#!/usr/bin/env -S python3 -u
# create a list of sbas pairs

import sys
import subprocess
from datetime import datetime 

import os
import glob

if len(sys.argv) < 3:
    print ('Usage: sbas_list.py max_temporal max_spatial')
    sys.exit(1)

maxtemporal=float(sys.argv[1])
maxspatial=float(sys.argv[2])

# get the PATH of the script directory
PATH=os.path.dirname(os.path.abspath(sys.argv[0]))

#  get a list of the sorted geocoded slc files
# .geo format e.g. S1A_20150503.geo for char1=7
ret=os.system('ls -1 ../*geo | cat > geolist')
flist=open('geolist','r')
geos=flist.readlines()
# print(geos)

names_times=[]
jdlist = []
for geo in geos:
    # .geo format e.g. S1A_20150503.geo for char1=7
    char1=7+13
    scenedate=geo[char1:char1+8]
    jd = datetime.strptime(scenedate, '%Y%m%d').toordinal()+1721424.5
    print('Julian day ',jd)
    names_times.append(geo+' '+str(jd))
    jdlist.append(jd)

#  estimate baseline and create a file for the time-baseline plot
ftb=open('sbas_list','w') 
#  call the spatial baseline estimator
for i in range(0,len(geos)-1):
    for j in range(i+1,len(geos)):
        print ('Interferograms: '+str(i)+' '+str(j))
        #  spatial baseline estimator
        command = PATH+'/bin/estimatebaseline '+geos[j].strip().replace('geo','orbtiming')+' '+geos[i].strip().replace('geo','orbtiming')
        # print(command)

        proc = subprocess.Popen(command, stdout=subprocess.PIPE, shell=True)
        (baseline1, err) = proc.communicate()
        baseline1 = float(baseline1.decode().strip())
        if abs(float(baseline1)) <= maxspatial:
            baseline2=abs(jdlist[j]-jdlist[i])
            if baseline2 <= maxtemporal:
                ftb.write(f"{geos[i].strip()} {geos[j].strip()} {baseline2} {baseline1}\n")

print('sbas_list written')
ftb.close()



        
