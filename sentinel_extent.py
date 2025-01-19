import numpy as np
import pandas as pd
import sys

SARDEM_COMMAND = 'sardem --bbox {} {} {} {} --xrate 10 --yrate 3 --shift-rsc'
if len(sys.argv) < 2:
    print('Usage: sentinel_extent.py csvfile')
    exit()

csvfile = sys.argv[1]
df = pd.read_csv(csvfile)

df.columns = [s.replace(' ','_') for s in df.columns]
near_start_lat  = df['Near_Start_Lat'].to_numpy()
near_start_lon  = df['Near_Start_Lon'].to_numpy()
far_start_lat  = df['Far_Start_Lat'].to_numpy()
far_start_lon  = df['Far_Start_Lon'].to_numpy()
near_end_lat  = df['Near_End_Lat'].to_numpy()
near_end_lon  = df['Near_End_Lon'].to_numpy()
far_end_lat  = df['Far_End_Lat'].to_numpy()
far_end_lon  = df['Far_End_Lon'].to_numpy()
nimg = len(near_start_lat)
latmin = np.zeros(nimg)
latmax = np.zeros(nimg)
lonmin = np.zeros(nimg)
lonmax = np.zeros(nimg)
for i in range(nimg):
   latmin[i] = np.min([near_start_lat[i],far_start_lat[i],near_end_lat[i],
                      far_end_lat[i]])
   latmax[i] = np.max([near_start_lat[i],far_start_lat[i],near_end_lat[i],
                      far_end_lat[i]])
   lonmin[i] = np.min([near_start_lon[i],far_start_lon[i],near_end_lon[i],
                      far_end_lon[i]])
   lonmax[i] = np.max([near_start_lon[i],far_start_lon[i],near_end_lon[i],
                      far_end_lon[i]])
latmin_med = np.median(latmin)
latmax_med = np.median(latmax)
lonmin_med = np.median(lonmin)
lonmax_med = np.median(lonmax)
command = SARDEM_COMMAND.format(lonmin_med,latmin_med,
                                lonmax_med,latmax_med)
print(command)