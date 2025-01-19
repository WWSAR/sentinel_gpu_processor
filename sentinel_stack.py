#!/usr/bin/env -S python3 -u
#
#  process stack of sentinel files to coregistered geocoded slcs, single/dual pol

import sys
import os
import math
import string
import sql_mod
import time
import glob
import subprocess
from datetime import datetime
import sqlite3

if len(sys.argv) < 1:
    print('Usage: sentinel_stack.py')

print('Processing stack of sentinel geotiff products to coregistered geocoded slcs')

# get the PATH of the script directory
PATH=os.path.dirname(os.path.abspath(sys.argv[0]))

# Create a 'params' file
params=open('params','w')
p1 = os.path.join(os.getcwd(),'elevation.dem')
p2 = os.path.join(os.getcwd(),'elevation.dem.rsc')
params.write(p1+'\n')
params.write(p2+'\n')
params.close()
print('DEM file set to elevation.dem')
print('RSC file set to elevation.dem.rsc')

# get list of geotiff products
zips = glob.glob('*.zip')

print('zipfiles: ',zips)

# get the precise orbit files
preciseorbitlist = glob.glob('*.EOF')
with open('preciseorbitfiles','w') as f:
    f.write('\n'.join(preciseorbitlist))

print('Precise orbit list:')
print(preciseorbitlist)

# loop over directories and process each with sentinel_scene.py
#   sentinel_scene needs zipfile and precise orbit if available
for ifile,zipfile in enumerate(zips):
    #  which precise orbit file for this scene?
    print('zipfile: ',zipfile)
    # Finding the date of acqusition following the namign rule
    char1= 13
    char2= 12
    scenedate=zipfile[char1+4:char1+char2]
    
    doy=datetime.strptime(scenedate, '%Y%m%d').timetuple().tm_yday
    year=scenedate[0:4]   # day of year and year for scene
    print('doy ',doy,' ',year)
    if doy > 1:
        orbitfilestartdate = datetime.strptime(year+' '+str(doy-1),'%Y %j').strftime('%Y%m%d')
    else:
        lastdoy=datetime.strptime(str(int(year)-1)+'1231', '%Y%m%d').timetuple().tm_yday
        orbitfilestartdate = datetime.strptime(str(int(year)-1)+' '+str(lastdoy),'%Y %j').strftime('%Y%m%d')
    print('orbit file start date: ',orbitfilestartdate)
    orbitfilecandidates = [s for s in preciseorbitlist if ('V'+orbitfilestartdate) in s]
    orbitfilename = orbitfilecandidates[0]
    print('Precise orbit file found:', orbitfilename)
    sentinel_scene = os.path.join(PATH, 'sentinel_scene.py')

    if len(orbitfilename) < 1:
        command='python '+sentinel_scene+' '+zipfile
    else:
        command='python '+sentinel_scene+' '+zipfile+' '+orbitfilename

    print(command)
    ret = subprocess.check_call(command, shell=True)
    if ifile > 1:
        break

print('Loop over scenes complete.')






