#!/usr/bin/env -S python3 -u

#  create a new orbtiming file using a precise orbit file

import sys
import sqlite3
import sql_mod,string
import os
import math
from datetime import datetime

def readxmlparam(xmllines, param):
    for line in xmllines:
        if param in line:
            i=line.find(param)
            str1=line[i:]
            istart=str1.find('>')+1
            istop=str1.find('<')
            value=str1[istart:istop]
            #print(line[i:],'\n')
            #print(istart, istop, '\n')
            return value

def timeinseconds(timestring):
#    dt = datetime.strptime(timestring, 'TAI=%Y-%m-%dT%H:%M:%S.%f')
    dt = datetime.strptime(timestring, 'UTC=%Y-%m-%dT%H:%M:%S.%f')
#    dt = datetime.strptime(timestring, 'UT1=%Y-%m-%dT%H:%M:%S.%f')
    secs=dt.hour*3600+dt.minute*60+dt.second+dt.microsecond/1000000.0
    return secs

if len(sys.argv) < 2:
    print('Usage: precise_orbit.py preciseorbitfile <start time def=entire file> <end time>')
    sys.exit(1)

starttime=-99999
orbitfile=sys.argv[1]
if len(sys.argv) > 2:
    starttime=sys.argv[2]
stoptime=starttime
if len(sys.argv) > 3:
    stoptime=sys.argv[3]

print('Times: ',starttime,stoptime)

# read the precise file
xmlfile=open(orbitfile,'r')
xmllines=xmlfile.readlines()
xmlfile.close()

#  save orbit and timing information

#  extract each state vector
start=[]
stop=[]
for i in range(len(xmllines)):
    if '<OSV>' in xmllines[i]:
        start.append(i)
        
    if '</OSV>' in xmllines[i]:
        stop.append(i)

time=[]
x=[]
y=[]
z=[]
vx=[]
vy=[]
vz=[]
for i in range(len(start)):
    statelines=xmllines[start[i]:stop[i]]
    #print(statelines,'\n\n')
#    time.append(readxmlparam(statelines,'TAI'))
    time.append(readxmlparam(statelines,'UTC'))
#    time.append(readxmlparam(statelines,'UT1'))
    x.append(readxmlparam(statelines,'X unit'))
    y.append(readxmlparam(statelines,'Y unit'))
    z.append(readxmlparam(statelines,'Z unit'))
    vx.append(readxmlparam(statelines,'VX unit'))
    vy.append(readxmlparam(statelines,'VY unit'))
    vz.append(readxmlparam(statelines,'VZ unit'))
    #print(posstart,posstop,velstart,velstop)
    #print(statelines[posstart:posstop])
    #print(statelines[velstart:velstop])
    
orbinfo=open('precise_orbtiming','w')
orbinfo.write('0 \n')
orbinfo.write('0 \n')
orbinfo.write('0 \n')
orbinfo.write(str(len(start))+'\n')
for i in range(len(start)):
    orbinfo.write(str(timeinseconds(time[i]))+' '+str(x[i])+' '+str(y[i])+' '+str(z[i])+' '+str(vx[i])+' '+str(vy[i])+' '+str(vz[i])+' 0.0 0.0 0.0')
    orbinfo.write('\n')

orbinfo.close()

# filter orbtiming file to get desired range only

if len(sys.argv) < 3:
    sys.exit(0)

#ret=os.system('mv precise_orbtiming orbtiming.full')
src_fname = 'precise_orbtiming'
tar_fname = 'orbtiming.full'
if os.path.exists(tar_fname):
    os.remove(tar_fname)
os.rename(src_fname,tar_fname)
orbinfofull=open('orbtiming.full','r')
orbinfo=open('precise_orbtiming','w')
qstr=orbinfofull.readline()  # read three lines at beginning first
#orbinfo.write(qstr)
qstr=orbinfofull.readline()
#orbinfo.write(qstr)
qstr=orbinfofull.readline()
#orbinfo.write(qstr)

# read number of lines in full file
qstr=orbinfofull.readline()

# get times bracketing desired times
for i in range(len(time)):
    #print(timeinseconds(time[i]),timeinseconds(time[0]))
    if timeinseconds(time[i]) < timeinseconds(time[0]):
        timeflag=i
        break

outvectors=[]
for i in range(len(time)):
    qstr=orbinfofull.readline()
    #print(qstr,i,timeflag)
    if i > 0:
        if i > timeflag:
            if timeinseconds(time[i]) < timeinseconds(time[i-1]):
                break
            #print(timeinseconds(time[i]),float(starttime))
            if timeinseconds(time[i+1]) > float(starttime):
                #print('time greater than starttime')
                if timeinseconds(time[i-1]) < float(stoptime):
                    #print('write output record ',timeinseconds(time[i]))
                    outvectors.append(qstr)

orbinfo.write(str(len(outvectors))+'\n')
for i in range(len(outvectors)):
    orbinfo.write(outvectors[i])

orbinfo.close()
orbinfofull.close()
sys.exit(0)
