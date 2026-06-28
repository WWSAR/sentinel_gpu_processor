#  process one sentinel scene files to coregistered geocoded slc, single/dual pol

import glob
import os
import shutil
import sqlite3
import subprocess
import zipfile
from typing import List, Sequence

import numpy as np
from osgeo import gdal

from s1proc import get_bin_path, sql_mod
from s1proc._log import set_logging_level, setup_logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.orbit import rah2ll
from s1proc.precise_orbit import parse_orbit
from s1proc.sario import sentinel_parser
from s1proc.sentinel_roidb import create_db

logger = setup_logger(name=__name__, level="INFO")
SOL = 299792458.0


def clip_polygon_with_rect(poly, lon_min, lat_min, lon_max, lat_max):
    def inside_left(p):
        return p[0] >= lon_min

    def inside_right(p):
        return p[0] <= lon_max

    def inside_bottom(p):
        return p[1] >= lat_min

    def inside_top(p):
        return p[1] <= lat_max

    def intersect(p1, p2, edge):
        x1, y1 = p1
        x2, y2 = p2

        if edge == "left":
            x = lon_min
            y = y1 + (y2 - y1) * (lon_min - x1) / (x2 - x1)
        elif edge == "right":
            x = lon_max
            y = y1 + (y2 - y1) * (lon_max - x1) / (x2 - x1)
        elif edge == "bottom":
            y = lat_min
            x = x1 + (x2 - x1) * (lat_min - y1) / (y2 - y1)
        elif edge == "top":
            y = lat_max
            x = x1 + (x2 - x1) * (lat_max - y1) / (y2 - y1)

        return (x, y)

    def clip(poly, inside, edge):
        result = []
        for i in range(len(poly)):
            curr = poly[i]
            prev = poly[i - 1]

            if inside(curr):
                if not inside(prev):
                    result.append(intersect(prev, curr, edge))
                result.append(curr)
            elif inside(prev):
                result.append(intersect(prev, curr, edge))

        return result

    poly = clip(poly, inside_left, "left")
    poly = clip(poly, inside_right, "right")
    poly = clip(poly, inside_bottom, "bottom")
    poly = clip(poly, inside_top, "top")

    return poly


def dem_bounds(footprint, rsc):
    """
    Get the bounds of the overlapped area between Sentinel-1 footprint and
    the study area defined by the RSC
    """
    overlap_poly = clip_polygon_with_rect(
        footprint, rsc.lonmin, rsc.latmin, rsc.lonmax, rsc.latmax
    )

    if overlap_poly:
        lons = [p[0] for p in overlap_poly]
        lats = [p[1] for p in overlap_poly]

        overlap_bbox = (min(lons), min(lats), max(lons), max(lats))
    else:
        overlap_bbox = None
        return None
    rowmin, colmin = rsc.ll2xy(overlap_bbox[3], overlap_bbox[0])
    rowmax, colmax = rsc.ll2xy(overlap_bbox[1], overlap_bbox[2])
    rowmax = rowmax + 1
    colmax = colmax + 1
    rowmin = int(np.maximum(rowmin, 0))
    colmin = int(np.maximum(colmin, 0))
    rowmax = int(np.minimum(rowmax, rsc.nlat))
    colmax = int(np.minimum(colmax, rsc.nlon))
    return colmin, rowmin, colmax, rowmax


def footprint_bounds(orbfname: str, dbfname: str, hmin=0, hmax=10000):
    """
    Get the lat/lon bounds of current subswath
    """
    # read orbit
    with open(orbfname, "r") as f:
        firstline = f.readline()
        nline = int(firstline.strip())
        tt = np.zeros(nline, dtype=np.float64)
        xx = np.zeros((nline, 3), dtype=np.float64)
        vv = np.zeros((nline, 3), dtype=np.float64)
        for i in range(nline):
            line = f.readline()
            words = line.split()
            val = np.array([float(w) for w in words])
            tt[i] = val[0]
            xx[i, :] = val[1:4]
            vv[i, :] = val[4:7]
    con = sqlite3.connect(dbfname)
    # create a cursor
    c = con.cursor()
    tblname = "file"
    prf = sql_mod.valuef(c, tblname, "prf")
    azimuth_bursts = sql_mod.valuei(c, tblname, "azimuthBursts")
    lines_per_burst = sql_mod.valuei(c, tblname, "linesPerBurst")
    slant_range_time = sql_mod.valuef(c, tblname, "slantRangeTime")
    range_sampling_rate = sql_mod.valuef(c, tblname, "rangeSamplingRate")
    nrange = sql_mod.valuei(c, tblname, "samplesPerBurst")
    starttime = sql_mod.valuef(c, tblname, "azimuthTimeSeconds1")
    starttime_last = sql_mod.valuef(c, tblname, f"azimuthTimeSeconds{azimuth_bursts}")
    c.close()
    con.close()
    stoptime = starttime_last + lines_per_burst / prf
    rngstart = slant_range_time * SOL / 2.0
    dmrg = SOL / 2.0 / range_sampling_rate
    rngend = rngstart + (nrange - 1) * dmrg

    rah1 = []
    rah2 = []
    corners = [
        (starttime, rngstart),
        (starttime, rngend),
        (stoptime, rngend),
        (stoptime, rngstart),
        (starttime, rngstart),
    ]

    for t, r in corners:
        rah1.append([r, t, hmin])
        rah2.append([r, t, hmax])
    lats1, lons1 = rah2ll(tt, xx, vv, starttime, stoptime, np.array(rah1))
    lats2, lons2 = rah2ll(tt, xx, vv, starttime, stoptime, np.array(rah2))
    return np.array(list(zip(lons1, lats1))), np.array(list(zip(lons2, lats2)))


def _stream_tiff_from_zip(
    zip_path: str,
    tiff_name: str,
    proc: subprocess.Popen,
    nrange: int,
    lines_per_burst: int,
    nbursts: int,
) -> None:
    """
    Read Sentinel-1 SLC TIFF directly from zip via GDAL /vsizip/ and stream
    raw complex64 (float32 interleaved) burst data to *proc*'s stdin.

    Data is read and written one burst at a time to keep peak memory low.
    """
    gdal.UseExceptions()
    vsizip_path = f"/vsizip/{zip_path.replace(os.sep, '/')}/{tiff_name}"
    ds = gdal.Open(vsizip_path)
    if ds is None:
        raise RuntimeError(f"GDAL cannot open {vsizip_path}")

    w = ds.RasterXSize
    h = ds.RasterYSize
    expected_h = lines_per_burst * nbursts
    if w != nrange or h != expected_h:
        raise RuntimeError(
            f"TIFF dimensions ({w}x{h}) do not match DB metadata "
            f"(nrange={nrange}, lines_per_burst={lines_per_burst}, "
            f"nbursts={nbursts}, expected_h={expected_h})"
        )

    for burst_idx in range(nbursts):
        start_line = burst_idx * lines_per_burst
        data = ds.ReadAsArray(0, start_line, w, lines_per_burst)

        try:
            proc.stdin.write(data.flatten().tobytes())
        except BrokenPipeError:
            raise RuntimeError(
                "geo2rdr process closed stdin prematurely "
                "(possibly due to an error in the executable)"
            )

    proc.stdin.close()
    ds = None  # close GDAL dataset


def sentinel_scene(
    zip_file: str,
    demfile: str,
    rscfile: str,
    eof_file: str | None = None,
    polarization: str = "vv",
    subswath_list: Sequence[int] = [1, 2, 3],
    proc_dir: str = "stack",
    slc_dir: str = "slc",
    rm_zipfile: bool = False,
    rm_folder: bool = False,
    hmin: float = 0,
    hmax: float = 1e4,
    verbose: bool = False,
) -> List[str]:
    """
    Process one sentinel scene files to coregistered geocoded slc

    Parameters
    ----------
    zip_file: str
        Input zip file
    demfile: str
        DEM file
    rscfile: str
        rsc file
    eof_file: str|None
        Precise orbit file. If none, coarse orbit will be used
    polarization: str
        Polarization(s) to process
    subswath_list: Sequence[int]
        Subswaths to process
    proc_dir: str
        Directory to store temporary files
    slc_dir: str
        Directory to store geocoded SLC files
    rm_zipfile: bool
        Remove the zipfile after image processing is done
    rm_folder: bool
        Remove the unzipped data folder after image processing is done
    hmin: int
        Minimum elevation of the study area
    hmax: int
        Maximum elevation of the study area
    verbose: bool
        Set logging level to DEBUG

    Returns
    -------
    slc_files: List[str]
        Output SLC list
    """
    if verbose:
        set_logging_level(logger, "DEBUG")
    logger.info(f"Processing: {zip_file} to a geocoded SLC")
    logger.debug(f"input orbit file: {eof_file}")

    geo2rdr = get_bin_path("geo2rdr")

    sent = sentinel_parser(zip_file)
    mission_id = sent["mission_id"]
    unique_id = sent["unique_id"]
    acq_date = sent["start_time"][0:8]
    basename = os.path.basename(zip_file)
    data_dir = os.path.join(proc_dir, basename.replace(".zip", ".SAFE"))
    os.makedirs(slc_dir, exist_ok=True)

    if not os.path.exists(data_dir):
        os.makedirs(data_dir, exist_ok=True)
        with zipfile.ZipFile(zip_file, "r") as zip_ref:
            for member in zip_ref.namelist():
                if "/annotation/" in member and member.endswith(".xml"):
                    zip_ref.extract(member, proc_dir)
    logger.debug("Annotation metadata extracted from zip")

    # read image size
    rsc = GeoCoordinates(rscfile)

    # first, preprocess the Sentinel products:
    #   1.  Get the number of subswaths
    #   2.  Create a db file for each subswath and each orbit
    #   3.  Create orbtiming file with orbit state vectors
    #   4.  Rename and save the ancillary files needed for deramping the slc
    #   5.  Stream TIFF data directly from zip into geo2rdr
    precise_orbfname = os.path.join(proc_dir, f"{acq_date}.orbtiming")
    if eof_file is not None:
        logger.debug("*** Using precise orbit ***")
        parse_orbit(eof_file.strip(), zip_file, precise_orbfname)
    swathfiles = []
    xmlfiles = []
    with zipfile.ZipFile(zip_file, "r") as zf:
        zip_members = zf.namelist()
    for subswath in sorted(subswath_list):
        tiff_members = [
            m
            for m in zip_members
            if f"iw{subswath}" in m
            and polarization.lower() in m.lower()
            and m.endswith(".tiff")
            and "/measurement/" in m
        ]
        if not tiff_members:
            raise FileNotFoundError(
                f"No TIFF found in zip for iw{subswath}/{polarization}"
            )
        # swathfiles are actually tiff images
        swathfiles.append(tiff_members[0])
        xmlfiles.append(
            glob.glob(
                os.path.join(
                    data_dir, "annotation", f"*iw{subswath}*{polarization}*.xml"
                )
            )[0]
        )

    #  loop over subswaths
    bounds_list = []
    for ifile, tiff_path in enumerate(swathfiles):
        subswath = subswath_list[ifile]
        dbfname = os.path.join(
            proc_dir, f"{acq_date}_{mission_id}_{unique_id}_iw{subswath}.db"
        )
        orbfname = os.path.join(
            proc_dir, f"{acq_date}_{mission_id}_{unique_id}.orbtiming"
        )
        dcfname = os.path.join(
            proc_dir, f"{acq_date}_{mission_id}_{unique_id}_iw{subswath}.dcinfo"
        )
        fmratefname = os.path.join(
            proc_dir, f"{acq_date}_{mission_id}_{unique_id}_iw{subswath}.fmrateinfo"
        )
        create_db(
            tiff_path,
            xmlfiles[ifile],
            dbfname,
            orbfname,
            dcfname,
            fmratefname,
        )
        con = sqlite3.connect(dbfname)
        c = con.cursor()
        swathfile = "file"

        # add ancillary data file names to database
        sql_mod.add_param(c, swathfile, "orbinfo")
        if eof_file is not None:
            sql_mod.edit_param(
                c, swathfile, "orbinfo", precise_orbfname, "-", "char", ""
            )
        else:
            sql_mod.edit_param(c, swathfile, "orbinfo", orbfname, "-", "char", "")
        sql_mod.add_param(c, swathfile, "dcinfo")
        sql_mod.edit_param(c, swathfile, "dcinfo", dcfname, "-", "char", "")
        sql_mod.add_param(c, swathfile, "fmrateinfo")
        sql_mod.edit_param(c, swathfile, "fmrateinfo", fmratefname, "-", "char", "")
        con.commit()
        c.close()
        con.close()

        footprint1, footprint2 = footprint_bounds(orbfname, dbfname, hmin, hmax)
        bounds1 = dem_bounds(footprint1, rsc)
        bounds2 = dem_bounds(footprint2, rsc)
        if bounds1 is None:
            bounds = bounds2
        elif bounds2 is None:
            bounds = bounds1
        else:
            bounds = [
                min(bounds1[0], bounds2[0]),
                min(bounds1[1], bounds2[1]),
                max(bounds1[2], bounds2[2]),
                max(bounds1[3], bounds2[3]),
            ]
        bounds_list.append(bounds)
        if bounds is None:
            continue

    # Now, process each subswath to a geocoded slc
    slc_files = []
    for ifile, _fn in enumerate(swathfiles):
        bounds = bounds_list[ifile]
        if bounds is None:
            continue
        subswath = subswath_list[ifile]
        slavedb = os.path.join(
            proc_dir, f"{acq_date}_{mission_id}_{unique_id}_iw{subswath}.db"
        )
        # save dem/rsc parameters for geo2rdr
        con = sqlite3.connect(slavedb.strip())
        c = con.cursor()
        sql_mod.add_param(c, "file", "demfile")
        sql_mod.add_param(c, "file", "rscfile")
        sql_mod.edit_param(c, "file", "demfile", demfile, "-", "char", "DEM file")
        sql_mod.edit_param(c, "file", "rscfile", rscfile, "-", "char", "RSC file")
        sql_mod.edit_param(c, "file", "hmin", hmin, "m", "real*8", "Minimum elevation")
        sql_mod.edit_param(c, "file", "hmax", hmax, "m", "real*8", "Maximum elevation")

        # read burst dimensions needed for stdin streaming
        nrange = sql_mod.valuei(c, "file", "samplesPerBurst")
        lines_per_burst = sql_mod.valuei(c, "file", "linesPerBurst")
        azimuth_bursts = sql_mod.valuei(c, "file", "azimuthBursts")
        con.commit()
        c.close()
        con.close()

        slc_file = os.path.join(
            slc_dir, f"{acq_date}_{mission_id}_{unique_id}_iw{subswath}"
        )
        # unified deramp + geocode + reramp: stream TIFF from zip via stdin
        cmd = [geo2rdr, slavedb.strip(), slc_file, "--stdin"]
        logger.info(" ".join(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            _stream_tiff_from_zip(
                zip_file,
                swathfiles[ifile],
                proc,
                nrange,
                lines_per_burst,
                azimuth_bursts,
            )
        except Exception:
            proc.kill()
            proc.wait()
            raise
        proc.wait()
        if proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd)

        slc_files.extend(glob.glob(slc_file + "*.gslc"))
    # Clean up zip files to lessen disk space requirements
    if rm_zipfile:
        os.remove(zip_file)
    # Clean up unzipped SAFE folders to lessen disk space requirements
    if rm_folder:
        shutil.rmtree(data_dir)

    logger.info("Loop over swaths complete.")
    return slc_files
