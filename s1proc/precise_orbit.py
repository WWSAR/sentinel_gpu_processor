#  create a new orbtiming file using a precise orbit file

import os
import re
from datetime import datetime, timedelta


def readxmlparam(xmllines, param):
    for line in xmllines:
        if param in line:
            i = line.find(param)
            str1 = line[i:]
            istart = str1.find(">") + 1
            istop = str1.find("<")
            value = str1[istart:istop]
            # print(line[i:],'\n')
            # print(istart, istop, '\n')
            return value


def sentinel_parser(filename):
    filename = os.path.split(filename)[-1]
    words = re.split(r"[_]+|\.", filename)
    sent = {}
    sent["filename"] = filename
    sent["mission"] = words[0]
    sent["mode"] = words[1]
    sent["product_type"] = words[2]
    sent["level"] = words[3][0]
    sent["product_class"] = words[3][1]
    sent["polarization"] = words[3][2:4]
    sent["start_time"] = datetime.strptime(words[4], "%Y%m%dT%H%M%S")
    sent["stop_time"] = datetime.strptime(words[5], "%Y%m%dT%H%M%S")
    sent["orbit_number"] = words[6]
    sent["mission_id"] = words[7]
    sent["unique_id"] = words[8]
    return sent


def timeinseconds(timestring):
    dt = datetime.strptime(timestring, "UTC=%Y-%m-%dT%H:%M:%S.%f")
    secs = dt.hour * 3600 + dt.minute * 60 + dt.second + dt.microsecond / 1000000.0
    return dt, secs


def parse_orbit(orbfile, zipfile, outputfile):
    sent = sentinel_parser(zipfile)
    # expand start and stop times by 10 seconds
    start_time = sent["start_time"] - timedelta(seconds=10)
    stop_time = sent["stop_time"] + timedelta(seconds=10)

    # read the precise orbit file
    xmlfile = open(orbfile, "r")
    xmllines = xmlfile.readlines()
    xmlfile.close()

    #  save orbit and timing information
    #  extract each state vector
    start = []
    stop = []
    for i in range(len(xmllines)):
        if "<OSV>" in xmllines[i]:
            start.append(i)

        if "</OSV>" in xmllines[i]:
            stop.append(i)

    time = []
    x = []
    y = []
    z = []
    vx = []
    vy = []
    vz = []
    for i in range(len(start)):
        statelines = xmllines[start[i] : stop[i]]
        utc_str = readxmlparam(statelines, "UTC")
        utc_time, time_seconds = timeinseconds(utc_str)
        if utc_time < start_time or utc_time > stop_time:
            continue
        time.append(time_seconds)
        x.append(readxmlparam(statelines, "X unit"))
        y.append(readxmlparam(statelines, "Y unit"))
        z.append(readxmlparam(statelines, "Z unit"))
        vx.append(readxmlparam(statelines, "VX unit"))
        vy.append(readxmlparam(statelines, "VY unit"))
        vz.append(readxmlparam(statelines, "VZ unit"))

    orbinfo = open(outputfile, "w")
    orbinfo.write(str(len(time)) + "\n")
    for i in range(len(time)):
        orbinfo.write(
            str(time[i])
            + " "
            + str(x[i])
            + " "
            + str(y[i])
            + " "
            + str(z[i])
            + " "
            + str(vx[i])
            + " "
            + str(vy[i])
            + " "
            + str(vz[i])
            + " 0.0 0.0 0.0"
        )
        orbinfo.write("\n")
    orbinfo.close()
