#!/usr/bin/env -S python3 -u

#  ps_sbas_igrams - create set of sentinel interferogram subsets 
#    allow looks
#    create the intlist

import sys
import os
import geocoordinates

if len(sys.argv) < 4:
    print('Usage: crossmul.py sbas_list dem_rsc_file <xlooks=1> <ylooks=xlooks>')
    sys.exit(1)

# get the PATH of the script directory
PATH=os.path.dirname(os.path.abspath(sys.argv[0]))

sbaslist=sys.argv[1]
demrscfile=sys.argv[2]
xlooks=1

if len(sys.argv) > 3:
    xlooks=sys.argv[3]

ylooks=xlooks
if len(sys.argv) > 4:
    ylooks=sys.argv[4]

# sbaslist
rsc = geocoordinates.GeoCoordinates(demrscfile)
rsclook = rsc.take_look(int(ylooks),int(xlooks))
rsclook.save_as_rsc('dem.rsc')

fintlist=open('intlist','w')
sbasfiles=[]
fsbas=open(sbaslist,'r')
sbas=fsbas.readlines()
for line in sbas:
    words=line.split()
    master=words[0]
    slave=words[1]
#  get a short names for master and slave files
    first=master.find('20')
    mastername=master[first:first+8]
    first=slave.find('20')
    slavename=slave[first:first+8]

    intfile=mastername+'_'+slavename+'.int'
    if os.path.exists(os.path.join('int',intfile)):
        continue
    #ampfile=mastername+'_'+slavename+'.amp'
    #ccfile=mastername+'_'+slavename+'.cc'

    fintlist.write(intfile)
    fintlist.write('\n')

    flag=0
    command = 'D:\\sentinel\\sentinel_processor\\csrc\\build\\Debug\\crossmul.exe '+ \
              master+' '+slave+' '+demrscfile+' '+str(ylooks)+' '+str(xlooks)
    print(command)
    os.system(command)

    # correlation file next
    #command = PATH + '/bin/makecc ' + ' ' + intfile + ' ' + ampfile + ' ' + ccfile + ' ' + str(int((int(xsize) / int(xlooks))))
    #ret = subprocess.check_call(command, shell=True)
fintlist.close()
