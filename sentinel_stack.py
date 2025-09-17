#!/usr/bin/env python3
#
#  process stack of sentinel files to coregistered geocoded slcs, single/dual pol

import sys
import os
import glob
import subprocess
import shutil
from datetime import datetime

def parse_orbitfilename(orbitfilelist):
    start_date = []
    end_date = []
    for orbitfile in orbitfilelist:
        words = orbitfile.split('_')
        s1 = words[-2]
        start_date_str = s1[1:9]
        s2 = words[-1]
        end_date_str = s2[0:8]
        start_date.append(datetime.strptime(start_date_str,"%Y%m%d"))
        end_date.append(datetime.strptime(end_date_str,"%Y%m%d"))
    return start_date,end_date

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

print('zip_files: ',zips)

# get the precise orbit files
preciseorbitlist = glob.glob('*.EOF')
with open('preciseorbitfiles','w') as f:
    f.write('\n'.join(preciseorbitlist))

print('Precise orbit list:')
print(preciseorbitlist)
start_date,end_date = parse_orbitfilename(preciseorbitlist)
norbit = len(preciseorbitlist)

# loop over directories and process each with sentinel_scene.py
#   sentinel_scene needs zip_file and precise orbit if available
for ifile,zip_file in enumerate(zips):
    #  which precise orbit file for this scene?
    print('zip_file: ',zip_file)
    geofile = zip_file.replace('zip','SAFE.geo')
    #if os.path.exists(geofile):
        #print('geo_file exists')
        #continue
        #try:
        #    os.remove(zip_file)
        #except Exception as e:
        #    print(e)
        #try:
        #    shutil.rmtree(zip_file.replace('zip','SAFE'))
        #except Exception as e:
        #    print(e)
        #continue
    # Finding the date of acqusition following the namign rule
    char1= 13
    char2= 12
    scenedate=zip_file[char1+4:char1+char2]
    
    current_date = datetime.strptime(scenedate,"%Y%m%d")
    #doy=datetime.strptime(scenedate, '%Y%m%d').timetuple().tm_yday
    #year=scenedate[0:4]   # day of year and year for scene
    #print('doy ',doy,' ',year)
    #if doy > 1:
    #    orbitfilestartdate = datetime.strptime(year+' '+str(doy-1),'%Y %j').strftime('%Y%m%d')
    #else:
    #    lastdoy=datetime.strptime(str(int(year)-1)+'1231', '%Y%m%d').timetuple().tm_yday
    #    orbitfilestartdate = datetime.strptime(str(int(year)-1)+' '+str(lastdoy),'%Y %j').strftime('%Y%m%d')
    #print('orbit file start date: ',orbitfilestartdate)
    #orbitfilecandidates = [s for s in preciseorbitlist if ('V'+orbitfilestartdate) in s]
    #orbitfilename = orbitfilecandidates[0]
    orbitfilename = None
    for j in range(norbit):
        if start_date[j] <= current_date and end_date[j] >= current_date:
            orbitfilename = preciseorbitlist[j]
            print('Precise orbit file found:', orbitfilename)
            break
    sentinel_scene = os.path.join(PATH, 'sentinel_scene.py')

    if orbitfilename is None:
        print(f'Cannot find a precise orbit file for {zip_file}')
        command='python '+sentinel_scene+' '+zip_file
    else:
        command='python '+sentinel_scene+' '+zip_file+' '+orbitfilename

    print(command)
    ret = subprocess.check_call(command, shell=True)

print('Loop over scenes complete.')
