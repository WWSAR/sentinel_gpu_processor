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
ref_list = []
sec_list = []
ref_date_list = []
sec_date_list = []
for line in sbas:
    words=line.split()
    master=words[0]
    slave=words[1]
#  get a short names for master and slave files
    first=master.find('20')
    mastername=master[first:first+8]
    first=slave.find('20')
    slavename=slave[first:first+8]
    ref_list.append(words[0])
    sec_list.append(words[1])
    ref_date_list.append(mastername)
    sec_date_list.append(slavename)

for i in range(len(ref_list)):
    ref_date = ref_date_list[i]
    sec_date = sec_date_list[i]
    intfile=ref_date+'_'+sec_date+'.int'
    j = i-1
    while True:
        if j >= 0 and ref_date == ref_date_list[j] and \
           sec_date == sec_date_list[j]:
            intfile = 'a' + intfile
            j -= 1
        else:
            break
    if os.path.exists(intfile):
        continue

    fintlist.write(intfile)
    fintlist.write('\n')

    flag=0
    command = 'D:\\sentinel\\sentinel_processor\\csrc\\build\\Debug\\crossmul.exe '+ \
              ref_list[i]+' '+sec_list[i]+' '+demrscfile+' '+str(ylooks)+' '+ \
              str(xlooks) + ' ' + intfile
    print(command)
    os.system(command)

fintlist.close()
