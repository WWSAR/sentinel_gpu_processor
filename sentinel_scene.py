#!/usr/bin/env -S python3 -u
#
#  process one sentinel scene files to coregistered geocoded slc, single/dual pol

import glob
import sys
import os
import shutil
import sql_mod
import time
from datetime import datetime
import numpy as np
import sqlite3
import subprocess
import zipfile

if len(sys.argv) < 1:
    print('Usage: sentinel_scene.py zipfile <precise orbit file>')

print(sys.argv)
print(len(sys.argv))

zip_file=sys.argv[1]
precise='NULL'
if len(sys.argv)>2:
    precise=sys.argv[2]

print('Zipfile: ',zip_file)
print('Processing sentinel zipfile product to coregistered geocoded slc, precise orbit=',precise)

# get the PATH of the script directory
PATH=os.path.dirname(os.path.abspath(sys.argv[0]))
dir=zip_file.replace('.zip','.SAFE')

if not os.path.exists(dir):
    with zipfile.ZipFile(zip_file,'r') as zip_ref:
        zip_ref.extractall('.')
    print(f"Contents extracted to current folder")

# first, preprocess the Sentinel products:
#   1.  Get the number of subswaths
#   2.  Create a db file for each subswath and each orbit
#   3.  Create orbtiming file with orbit state vectors
#   4.  Rename and save the ancillary files needed for deramping the slc
#   5.  Unpack the geotiff product into floating point slc

#  how many subswaths?
swathfiles = glob.glob(os.path.join(dir, 'measurement','*vv*.tiff'))
swathfiles = np.sort(swathfiles)
with open('swathfiles','w') as f:
    f.write('\n'.join(swathfiles))

nswaths=str(len(swathfiles))
print('Swaths to process: ',nswaths)
# find the xml files for each subswath
xmlfiles = glob.glob(os.path.join(dir,'annotation','*vv*.xml'))
xmlfiles = np.sort(xmlfiles)
with open('xmlfiles','w') as f:
    f.write('\n'.join(xmlfiles))

#  loop over subswaths
for ifile,file in enumerate(swathfiles):
#    for itemp in range(1):  # remove to go back to usual
#        file=swathfiles[ifile-1] # remove to go back to usual
    # create the orbtiming file, roi.db.X file with metadata, file table for each subswath
    print('ifile= ',ifile,': ',xmlfiles[ifile])
    sentinel_roidb = os.path.join(PATH,'preproc','sentinel_roidb.py')

    command = 'python '+ sentinel_roidb+' '+dir+' '+str(ifile+1)+' '+xmlfiles[ifile]
    print(command)
    ret = subprocess.check_call(command, shell=True)
    if ifile == 0:
        #  retrieve scene start and stop times from scene name
        print(dir)
        dt=datetime.strptime(dir[dir.find('T')+1:dir.find('T')+7],'%H%M%S')
        scenestart=dt.hour*3600+dt.minute*60+dt.second
        dt=datetime.strptime(dir[dir.find('T')+17:dir.find('T')+23],'%H%M%S')
        scenestop=dt.hour*3600+dt.minute*60+dt.second
        print('Scene limits: ',scenestart,scenestop)
        shutil.copy('orbtiming',f'{dir}.datafile_orbtiming')
        print(f'copy orbtiming to {dir}.datafile_orbtiming')
        #  insert the precise orbit
        if precise == 'NULL':
            print('*** Using predict orbit ***')
            if os.path.exists(f'{dir}.orbtiming'):
                os.remove(f'{dir}.orbtiming')
            os.rename('orbtiming',f'{dir}.orbtiming')
            print(f'rename orbtiming to {dir}.orbtiming')
        else: 
            precise_orbit = os.path.join(PATH,'preproc','precise_orbit.py')
            command = 'python '+precise_orbit+' '+precise.rstrip()+' '+str(scenestart-10)+' '+str(scenestop+10)
            print(command)
            os.system(command)
            print('*** Using precise orbit ***')
            shutil.copy('precise_orbtiming',f'{dir}.precise_orbtiming')
            print(f'copy precise_orbtiming to {dir}.precise_orbtiming')
            if os.path.exists(f'{dir}.orbtiming'):
                os.remove(f'{dir}.orbtiming')
            os.rename('precise_orbtiming',f'{dir}.orbtiming')
            print(f'rename precise_orbtiming to {dir}.orbtiming')

    if os.path.exists(f'{dir}.dcinfo.{ifile+1}'):
        os.remove(f'{dir}.dcinfo.{ifile+1}')
    os.rename('dcinfo',f'{dir}.dcinfo.{ifile+1}') # save TOPS doppler centroid steering file
    if os.path.exists(f'{dir}.fmrateinfo.{ifile+1}'):
        os.remove(f'{dir}.fmrateinfo.{ifile+1}')
    os.rename('fmrateinfo',f'{dir}.fmrateinfo.{ifile+1}') # save fm rate file for TOPS deramping

    #  save parameters in database file
    dbname=dir+'.db.'+str(ifile+1)
    con = sqlite3.connect(dbname.strip())

    # create a cursor
    c = con.cursor()
    swathfile='file'
    # get slc product times (may not need these two)
    firsttime=sql_mod.valuef(c,swathfile,'raw_slc_first_line_time')
    lasttime=sql_mod.valuef(c,swathfile,'raw_slc_last_line_time')
    #  Reversing lines or pixels?
    lineTimeOrdering=sql_mod.valuec(c,swathfile,'lineTimeOrdering')
    pixelTimeOrdering=sql_mod.valuec(c,swathfile,'pixelTimeOrdering')
    lineTimeOrdering='Increasing'
    pixelTimeOrdering='Increasing'

    # add ancillary data file names to database
    sql_mod.add_param(c,swathfile,'orbinfo')
    sql_mod.edit_param(c,swathfile,'orbinfo',dir+'.orbtiming','-','char','')
    sql_mod.add_param(c,swathfile,'dcinfo')
    sql_mod.edit_param(c,swathfile,'dcinfo',dir+'.dcinfo.'+str(ifile+1),'-','char','')
    sql_mod.add_param(c,swathfile,'fmrateinfo')
    sql_mod.edit_param(c,swathfile,'fmrateinfo',dir+'.fmrateinfo.'+str(ifile+1),'-','char','')
    con.commit()

    # extract geotiff file, reversing lines and pixels if necessary
    linereverse='n'
    pixelreverse='n'
    if lineTimeOrdering == 'Decreasing':
        linereverse='y'
            
    if pixelTimeOrdering == 'Decreasing':
        pixelreverse='y'

    print(file.rstrip())
    #command= PATH+'/bin/readgeotiff.exe '+file.rstrip()+' '+((file.replace('.tiff','.rawslc')).replace('/measurement','')).rstrip()+' '+linereverse+' '+pixelreverse
    basename = os.path.basename(file);
    output_slc_file = os.path.join(dir,basename.replace('tiff','rawslc'))
    command= 'D:\\sentinel\\sentinel_processor\\src\\build\\Debug\\readgeotiff.exe '+file.rstrip()+' '+output_slc_file+' '+linereverse+' '+pixelreverse
    print(time.ctime())
    print(command)
    ret = subprocess.check_call(command, shell=True)
    con.commit()
    c.close()

    con.close()

# Clean up tiff files to lessen disk space requirements
#command="rm `find "+dir+" -name *tiff*`"
#print(command)
#ret = subprocess.check_call(command, shell=True)

# Now, process each subswath to a geocoded slc
ifile=0
print('*** swathfiles: ',swathfiles)
for file in swathfiles:
    ifile=ifile+1
    slavedb = dir.strip()+'.db.'+str(ifile)

    # remove ramp, resample to lat lon, reinsert ramp

    #  save parameters in database file
    con = sqlite3.connect(slavedb.strip())
    # create a cursor
    c = con.cursor()  # update slc entry in database
    sql_mod.add_param(c,'file','raw_slc_file')
    origslcfile=sql_mod.valuec(c,'file','slc_file')
    sql_mod.edit_param(c,'file','raw_slc_file',origslcfile,'-','char','raw, nonderamped slc')
    derampedslcfile=origslcfile.replace('rawslc','rawslc.deramp')
    sql_mod.edit_param(c,'file','slc_file',derampedslcfile,'-','char','deramped slc')
    rawslcfile=sql_mod.valuec(c,'file','raw_slc_file')
    nrange = sql_mod.valuef(c,'file','samplesPerBurst')
    nazimuth = sql_mod.valuef(c,'file','linesPerBurst')
    con.commit()
    c.close()
    con.close()
    print(f'nrange:{nrange}, nazimuth:{nazimuth}')
    print(origslcfile, derampedslcfile)
    # deramp the slave file
    command='D:\\sentinel\\sentinel_processor\\src\\build\\Debug\\deramp_burst.exe '+slavedb.strip()+' '+rawslcfile
    print(command)
    os.system(command)

    # and geocode/reramp the slave
    outfile=slavedb.replace('db','geo').strip()
    outgeo=outfile[0:outfile.find('geo.')+3]
    command = "D:\\sentinel\\sentinel_processor\\src\\build\\Debug\\geo2rdr_reramp.exe "+outfile.replace('geo','db')+' '+outgeo
    print(time.ctime())
    print(command)
    os.system(command)

    # remove the original slc files
    print ('Original slc: ',origslcfile)
    os.remove(origslcfile)
    print ('Deramped slc: ',derampedslcfile)
    os.remove(derampedslcfile)
    
    print(time.ctime())
    print('Swath processed to common coordinates and coregistered.')

print('Loop over swaths complete.')






