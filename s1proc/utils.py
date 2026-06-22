import glob
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from numpy.typing import DTypeLike
from tqdm import tqdm

from s1proc import geocoordinates, geometry, orbit, sario
from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")


def sentinel_parser(filename: Path | str) -> dict:
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
    sent["start_time"] = words[4]
    sent["stop_time"] = words[5]
    sent["orbit_number"] = words[6]
    sent["mission_id"] = words[7]
    sent["unique_id"] = words[8]
    sent["date"] = sent["start_time"][0:8]
    return sent


def sentinel_acq_time(filename):
    sent = sentinel_parser(filename)
    start_time = datetime.strptime(sent["start_time"], "%Y%m%dT%H%M%S")
    stop_time = datetime.strptime(sent["stop_time"], "%Y%m%dT%H%M%S")
    t = start_time + (stop_time - start_time) / 2
    return t


def read_sentinel_orbit(orbfile):
    tt = None
    xx = None
    vv = None
    with open(orbfile, "r") as f:
        line = f.readline()
        nstatvec = int(line.strip())
        tt = np.zeros(nstatvec)
        xx = np.zeros((nstatvec, 3))
        vv = np.zeros((nstatvec, 3))
        for i in range(nstatvec):
            line = f.readline()
            words = line.split()
            num = np.array([float(s) for s in words])
            tt[i] = num[0]
            xx[i, :] = num[1:4]
            vv[i, :] = num[4:7]
    return tt, xx, vv


def estimatebaseline(orbfile1, orbfile2, demfile, demrscfile):
    rsc = geocoordinates.GeoCoordinates(demrscfile)
    nrow, ncol = rsc.nlat, rsc.nlon
    with open(demfile, "r") as f:
        f.seek(nrow * ncol // 2 * 2)
        hmid = np.fromfile(f, dtype=np.int16, count=1)[0]
    latmid = rsc.latmax + nrow // 2 * rsc.dlat
    lonmid = rsc.lonmin + ncol // 2 * rsc.dlon
    llh = np.array([latmid, lonmid, hmid])

    tt1, xx1, vv1 = read_sentinel_orbit(orbfile1)
    mid_idx = len(tt1) // 2
    tmid1 = tt1[mid_idx]
    xmid1 = xx1[mid_idx, :]
    vmid1 = vv1[mid_idx, :]
    tt2, xx2, vv2 = read_sentinel_orbit(orbfile2)
    mid_idx = len(tt2) // 2
    tmid2 = tt2[mid_idx]
    xmid2 = xx2[mid_idx, :]
    vmid2 = vv2[mid_idx, :]
    xyz = geometry.llh2xyz(llh)
    dr1, _ = orbit.orbitrangetime(tt1, xx1, vv1, xyz, tmid1, xmid1, vmid1)
    dr2, _ = orbit.orbitrangetime(tt2, xx2, vv2, xyz, tmid2, xmid2, vmid2)
    u1 = -dr1 / np.linalg.norm(dr1)
    u2 = -dr2 / np.linalg.norm(dr2)
    uperp = np.cross(u1, u2)
    theta = np.arcsin(np.linalg.norm(uperp))
    vdotuperp = np.dot(vmid1, uperp)
    if vdotuperp > 0:
        bperp = np.linalg.norm(dr1) * theta
    else:
        bperp = -np.linalg.norm(dr1) * theta
    return bperp


def create_slc_pair_list(
    min_tbl: int = 6,
    max_tbl: int = 360,
    min_sbl: int = 0,
    max_sbl: int = 300,
    slc_dir: str = "slc",
    proc_dir: str = "proc",
    ifg_dir: str = "igrams",
    img_pair_file: str = "intlist",
    demfile: str = "elevation.dem",
    rscfile: str = "elevation.dem.rsc",
):
    """
    Create a list of SLC pairs for interferogram generation

    Parameters
    ----------
    min_tbl: int
        minimum temporal baseline threshold
    max_tbl: int
        maximum temporal baseline threshold
    min_sbl: int
        minimum temporal baseline threshold
    max_sbl: int
        maximum temporal baseline threshold
    slc_dir: str
        SLC directory
    proc_dir: str
        Directory storing auxilary parameters
    ifg_dir: str
        Directory storing interferograms
    img_pair_file: str
        File containing pairs of subswath images
    demfile: str
        DEM file
    rscfile: str
        rsc file
    """
    os.makedirs(ifg_dir, exist_ok=True)
    # find all slc images in the parent directory
    burst_list = glob.glob(os.path.join(slc_dir, "*.gslc"))
    date_list = []
    for burst_file in burst_list:
        basename = os.path.basename(burst_file)
        date_str = basename[0:8]
        date_list.append(date_str)
    date_list = sorted(np.unique(date_list))

    f = open(os.path.join(ifg_dir, img_pair_file), "w")
    ndates = len(date_list)
    for i in range(ndates - 1):
        date_str_ref = date_list[i]
        date_ref = datetime.strptime(date_str_ref, "%Y%m%d")
        slc_ref = [s for s in burst_list if date_str_ref in s][0]
        basename1 = os.path.basename(slc_ref)
        orbfile1 = os.path.join(proc_dir, basename1[0:20] + ".orbtiming")
        for j in range(i + 1, ndates):
            date_str_sec = date_list[j]
            date_sec = datetime.strptime(date_str_sec, "%Y%m%d")
            slc_sec = [s for s in burst_list if date_str_sec in s][0]
            basename2 = os.path.basename(slc_sec)
            orbfile2 = os.path.join(proc_dir, basename2[0:20] + ".orbtiming")
            tempbl = (date_sec - date_ref).days
            if tempbl > max_tbl or tempbl < min_tbl:
                continue
            bperp = estimatebaseline(orbfile1, orbfile2, demfile, rscfile)
            if np.abs(bperp) > max_sbl or np.abs(bperp) < min_sbl:
                continue
            f.write(f"{date_str_ref} {date_str_sec} {tempbl} {bperp}\n")
    f.close()


def run_create_slc_pair_list(config: str = "config.yaml"):
    """
    Create a list of SLC pairs for interferogram generation

    Parameters
    ----------
    config: str
        Configuration file
    """
    from s1proc._config import load_config

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    create_slc_pair_list(
        min_tbl=pcfg.min_tbl,
        max_tbl=pcfg.max_tbl,
        min_sbl=pcfg.min_sbl,
        max_sbl=pcfg.max_sbl,
        slc_dir=icfg.slc_path,
        proc_dir=icfg.proc_path,
        ifg_dir=icfg.ifg_path,
        img_pair_file=icfg.img_pair_file,
        demfile=icfg.dem_file,
        rscfile=icfg.rsc_file,
    )
    return


def mid_orbit(
    orb_list: List[Path | str] | None = None,
    dem_file: Path | str = "elevation.dem",
    rsc_file: Path | str = "elevation.dem.rsc",
) -> str:
    """
    Find the middle orbit

    Parameters
    ----------
    orb_list: List[Path|str]|None
        List of orbit files. If None, use all precise orbit files
    dem_file: Path|str
        DEM file
    rsc_file: Path|str
        rsc file

    Returns
    -------
    mid_orb_file
        File name of the middle orbit
    """
    if orb_list is None:
        orb_list = glob.glob("*.precise_orbtiming")

    norb = len(orb_list)
    bperps = np.zeros(norb, dtype=np.float32)
    for i in range(1, norb):
        bperps[i] = estimatebaseline(orb_list[0], orb_list[i], dem_file, rsc_file)
    idx = np.argsort(bperps)
    mid_orb_file = orb_list[idx[norb // 2]]
    logger.info(f"Middle orbit file: {mid_orb_file}")
    return mid_orb_file


def _los(
    orb: np.ndarray, dem: np.ndarray, rsc: geocoordinates.GeoCoordinates
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Calculate Line-Of-Sight (LOS) vectors for all grid points

    Parameters
    ----------
    orb: np.ndarray
        orbit array containing seven columns:
        time, x, y, z, vx, vy, vz
    dem: np.ndarray
        DEM (potentially multilooked)
    rsc: geocoordinates.GeoCoordinates
        rsc object describing the geographic information of DEM

    Returns
    -------
    los: np.ndarray
        LOS vectors
    theta: np.ndarray
        look angle

    Notes
    -------
    This function first read orbit information from an orbtiming file in the
    parent directory. For each ground point, it calculates the corresponding
    Zero-Doppler time and the associated LOS vector and look angle.
    """
    nrow, ncol = rsc.nlat, rsc.nlon
    lat, lon = rsc.grid()
    # calculate the LOS vectors in the ECEF coordinate
    logger.info("Converting DEM grid into ECEF coordinates")
    llh = np.column_stack((lat.flatten(), lon.flatten(), dem.flatten()))
    logger.info("Computing LOS vectors")
    losvec = orbit.orbitrangetime_vec(llh, orb[:, 0], orb[:, 1:4], orb[:, 4:7])
    losvec = losvec.astype(np.float32)
    logger.info("Computing LOS vectors, done.")

    # calculate look angle
    logger.info("Computing look angles")
    r_lat = np.radians(lat.flatten())
    r_lon = np.radians(lon.flatten())
    r_n = -np.column_stack(
        [np.cos(r_lat) * np.cos(r_lon), np.cos(r_lat) * np.sin(r_lon), np.sin(r_lat)]
    )
    costheta = np.sum(losvec * r_n, axis=1).reshape(nrow, ncol)
    costheta = np.clip(costheta, -1, 1)
    theta = np.rad2deg(np.arccos(costheta)).astype(np.float32)
    logger.info("Computing look angles, done.")
    return losvec, theta


def multilook_dem(
    dem_in_file: str,
    rsc_in_file: str,
    dem_out_file: str,
    rsc_out_file: str,
    rowlook: int,
    collook: int,
):
    """
    Generate multilooked DEM for los calculation and visualization

    Parameters
    ----------
    dem_in_file: str
        DEM file for SLC generation (int16)
    rsc_in_file: str
        rsc file associated with dem_in_file
    dem_out_file: str
        Output DEM file for the output dem (int16)
    rsc_out_file: str
        rsc file associated with the output dem
    rowlook: int
        Number of looks in row direction
    collook: int
        Number of looks in column direction
    """
    dem_out_path = Path(dem_out_file).resolve().parent
    os.makedirs(dem_out_path, exist_ok=True)
    rsc_out_path = Path(rsc_out_file).resolve().parent
    os.makedirs(rsc_out_path, exist_ok=True)

    rsc = geocoordinates.GeoCoordinates(rsc_in_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    if rowlook > 1 or collook > 1:
        rsclook = rsc.take_look(rowlook, collook)
        rsclook.save_as_rsc(rsc_out_file)
        logger.info("Multilooking dem file")
        sario.multilooks(
            dem_in_file, dem_out_file, np.int16, nrow, ncol, rowlook, collook
        )
        logger.info(f"Multilooked DEM is saved to {dem_out_file}")
    else:
        shutil.copy(dem_in_file, dem_out_file)
        shutil.copy(rsc_in_file, rsc_out_file)


def los(
    dem_file: str,
    rsc_file: str,
    /,
    proc_dir: str = "proc",
    losvec_file: str | None = None,
    theta_file: str | None = None,
):
    """
    Calculate normalized Line-Of-Sight (LOS) vectors

    Parameters
    ----------
    dem_file: Path|str
        dem file (int16)
    rsc_file: Path|str
        rsc file that defines the grid
    proc_dir: str
        Directory to save output files
    losvec_file: str|None
        Output file of the LOS vectors
    theta_file: str|None
        Outupt file of look angles
    """
    rsc = geocoordinates.GeoCoordinates(rsc_file)
    dem = np.fromfile(dem_file, dtype=np.int16)
    orb_list = glob.glob(os.path.join(proc_dir, "*.orbtiming"))
    orb_file = mid_orbit(orb_list, dem_file, rsc_file)
    orb = sario.read_orbit(orb_file)
    losvec, theta = _los(orb, dem, rsc)
    if losvec_file is None:
        losvec_file = os.path.join(proc_dir, "losvec")
    losvec.tofile(losvec_file)
    logger.info(f"LOS vectors are saved to {losvec_file}")
    if theta_file is None:
        theta_file = os.path.join(proc_dir, "look_angle")
    theta.tofile(theta_file)
    logger.info(f"Look angles are saved to {theta_file}")


def move_files(src_dir: str, dst_dir: str, pattern: str):
    for src_file in glob.glob(os.path.join(src_dir, pattern)):
        basename = os.path.basename(src_file)
        dst_file = os.path.join(dst_dir, basename)
        logger.info(f"move {src_file} to {dst_file}")
        shutil.move(src_file, dst_file)


def check_integrity(
    amp_dir: str,
    max_deviation: float = 0.05,
    outfile: str = "incomplete_date.txt",
    movedata: bool = False,
    slc_dir: str = "slc",
    ifg_dir: str = "igrams",
    unw_dir: str = "unw",
    out_dir: str = "incomplete",
) -> List[str]:
    """
    Check data integrity based on the number of nonzero pixels in amplitude
    images.

    Parameters
    ----------
    amp_dir: str
        Amplitude image directory
    max_diviation: float
        For a given amplitude image, if its number of nonzero pixels is smaller
        than (1-max_diviation) * the meidan of all images, then it is
        considered as an incomplete image
    outfile: str
        A txt file containing all dates with data loss
    movedata: bool
        If True, move all incomplete files to out_dir
    slc_dir: str
        Directory of Geocoded SLC images
    ifg_dir: str
        Directory of wrapped interferograms
    unw_dir: str
        Directory of unwrapped interferograms
    out_dir: str
        Output directory

    Returns
    -------
    bad_dates: List[str]
        A list of dates with data loss
    """
    amp_list = np.array(glob.glob(os.path.join(amp_dir, "*.amp")))
    nimg = len(amp_list)
    non_zero_pixels = np.zeros(nimg, dtype=int)
    for i, amp_file in tqdm(enumerate(amp_list), total=nimg, desc="non-zero pixels"):
        a = np.fromfile(amp_file, dtype=np.float32)
        non_zero_pixels[i] = np.sum(a != 0)
    median_non_zero_pixel = np.median(non_zero_pixels)
    threshold = median_non_zero_pixel * max_deviation
    incomplete_idx = (median_non_zero_pixel - non_zero_pixels) > threshold
    bad_pixels = median_non_zero_pixel - non_zero_pixels[incomplete_idx]
    bad_pixels = bad_pixels / median_non_zero_pixel * 100
    bad_dates = []
    with open(outfile, "w") as f:
        for i, fn in enumerate(amp_list[incomplete_idx]):
            basename = os.path.basename(fn)
            date = basename[0:8]
            bad_dates.append(date)
            logger.info(f"date: {date}, data loss: {bad_pixels[i]:3.2f}%")
            f.write(date + "\n")
    if not movedata:
        return bad_dates
    os.makedirs(out_dir, exist_ok=True)
    for date in bad_dates:
        move_files(amp_dir, out_dir, f"*{date}*.amp")
        move_files(slc_dir, out_dir, f"*{date}*.geo")
        move_files(ifg_dir, out_dir, f"*{date}*.int")
        move_files(unw_dir, out_dir, f"*{date}*.unw")
    return bad_dates


def run_check_integrity(
    max_deviation: float = 0.05,
    outfile: str = "incomplete_date.txt",
    movedata: bool = False,
    out_dir: str = "incomplete",
    config: str = "config.yaml",
):
    """
    Check data integrity based on the number of nonzero pixels in amplitude
    images.

    Parameters
    ----------
    max_diviation: float
        For a given amplitude image, if its number of nonzero pixels is smaller
        than (1-max_diviation) * the meidan of all images, then it is
        considered as an incomplete image
    outfile: str
        A txt file containing all dates with data loss
    movedata: bool
        If True, move all incomplete files to out_dir
    out_dir: str
        Output directory
    config: Path|str
        Configuration file

    Returns
    -------
    bad_dates: List[str]
        A list of dates with data loss
    """
    from s1proc._config import load_config

    cfg = load_config(config)
    icfg = cfg.io
    check_integrity(
        amp_dir=icfg.amp_path,
        max_deviation=max_deviation,
        outfile=outfile,
        movedata=movedata,
        slc_dir=icfg.slc_path,
        ifg_dir=icfg.ifg_path,
        unw_dir=icfg.unw_path,
        out_dir=out_dir,
    )
    return


def get_gpu_count():
    import subprocess

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        return len(result.stdout.decode().strip().split("\n"))
    except FileNotFoundError:
        logger.error(
            "Failed to run nvidia-smi to count GPUs. " + "Assume there is only one GPU."
        )
        return 1


def get_files(input_path: str, extension: str | None = None) -> List[str]:
    """
    Get files from input_path, input_path can either be a single file,
    a folder, or a pattern.

    Parameters
    ----------
    input_path: str
        Input path
    extension: str
        Input file extension

    Returns
    -------
    file_list: List[str]
        A list of files
    """
    p = Path(input_path)
    if p.is_file():
        return [input_path]
    elif p.is_dir():
        if extension is None:
            file_list = glob.glob(os.path.join(input_path, "*"))
        else:
            file_list = glob.glob(os.path.join(input_path, "*" + extension))
        if len(file_list) == 0:
            logger.warning(
                "Cannot find any file from the input " + f"directory {input_path}."
            )
    else:
        file_list = glob.glob(input_path)
        if len(file_list) == 0:
            logger.warning(
                "Cannot find any file using the input " + f"pattern {input_path}."
            )
    return file_list


def gtiff2roipac(
    tif_file: Path | str,
    output_image: Path | str | None = None,
    output_rsc: Path | str | None = None,
    output_type: DTypeLike = np.float32,
    shift: bool = True,
):
    """
    Convert a GeoTIFF image to a binary image and an rsc file

    Parameters
    ----------
    tif_file: Path|str
        Input GeoTIFF file
    output_image: Path|str|None
        Output binary image file
    output_rsc: Path|str|None
        Output rsc file
    output_type: DTypeLike
        Output image type
    shift: bool
        If true, shift lonmin, latmin by a half pixel to respect the convention
        of rsc definition
    """
    from osgeo import gdal

    ds = gdal.Open(tif_file)
    gt = ds.GetGeoTransform()
    if shift:
        lonmin = gt[0] + gt[1] * 0.5
        latmax = gt[3] + gt[5] * 0.5
    else:
        lonmin = gt[0]
        latmax = gt[3]
    dlon = gt[1]
    dlat = gt[5]
    nlon = ds.RasterXSize
    nlat = ds.RasterYSize
    rscparams = {}
    rscparams["nlon"] = nlon
    rscparams["nlat"] = nlat
    rscparams["lonmin"] = lonmin
    rscparams["latmax"] = latmax
    rscparams["dlon"] = dlon
    rscparams["dlat"] = dlat
    rsc = geocoordinates.GeoCoordinates(rscparams=rscparams)
    rsc.save_as_rsc(output_rsc)
    img = ds.ReadAsArray()
    img.astype(output_type).tofile(output_image)


def roipac2gtiff(
    imgfile: str,
    rscfile: str,
    input_type: DTypeLike,
    output_type: int,
    shift: bool = True,
    output_file: str | None = None,
):
    """
    Convert a binary image to a GeoTiff

    Parameters
    ----------
    rscfile: Path|str
        An rsc file
    imgfile: Path|str
        Input image file
    input_type: DTypeLike
        Type of the input image (e.g., np.float32)
    output_type: int
        Type of the output iamge (e.g., gdal.GDT_Float32)
    shfit: bool
        If True, shift edge coordinates by a half pixel such following the
        edge convention of gdal
    output_file: Path|str|None
        Output image file
    """
    from osgeo import gdal, osr

    rsc = geocoordinates.GeoCoordinates(rscfile)
    if output_file is None:
        output_file = imgfile + ".tif"
    nrow, ncol = rsc.nlat, rsc.nlon
    dlon = rsc.dlon
    dlat = rsc.dlat
    if shift:
        lonmin = rsc.lonmin - 0.5 * dlon
        latmax = rsc.latmax - 0.5 * dlat

    img = np.fromfile(imgfile, dtype=input_type).reshape(nrow, ncol)

    geotransform = (lonmin, dlon, 0, latmax, 0, dlat)
    outtif = gdal.GetDriverByName("GTiff").Create(
        output_file, ncol, nrow, 1, output_type
    )
    outtif.SetGeoTransform(geotransform)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    outtif.SetProjection(srs.ExportToWkt())
    outtif.GetRasterBand(1).WriteArray(img)
    outtif.FlushCache()
    outtif = None


class IfgList:
    def __init__(self, imglist: Sequence[str]) -> pd.DataFrame:
        """
        Generate a pandas DataFrame from a list of interferogram files
        """
        ref_date = []
        sec_date = []
        tempbl = []
        for imgfile in imglist:
            basename = os.path.basename(imgfile)
            basename = os.path.splitext(basename)[0]
            words = basename.split("_")
            ref_date.append(words[0])
            sec_date.append(words[1])
            _ref_date = datetime.strptime(ref_date[-1], "%Y%m%d")
            _sec_date = datetime.strptime(sec_date[-1], "%Y%m%d")
            tempbl.append((_sec_date - _ref_date).days)
        df = pd.DataFrame(
            {"date1": ref_date, "date2": sec_date, "tempbl": tempbl, "image": imglist}
        )
        self.df = df

    def get_date_list(self):
        date_list1 = self.df["date1"].tolist()
        date_list2 = self.df["date2"].tolist()
        date_list = np.concatenate((date_list1, date_list2))
        date_list = np.sort(np.unique(date_list))
        return date_list


def _detect_gpu_count() -> int:
    """
    Detect the number of available CUDA-capable GPUs on the system.

    Queries ``nvidia-smi`` first; falls back to counting devices listed in
    the ``CUDA_VISIBLE_DEVICES`` environment variable.  Returns 1 when
    neither method succeeds so that callers can always proceed with at least
    one logical device.

    Returns
    -------
    int
        Number of available GPUs (minimum 1).
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            gpu_ids = [
                line.strip() for line in result.stdout.splitlines() if line.strip()
            ]
            count = len(gpu_ids)
            if count > 0:
                logger.info("Detected %d GPU(s) via nvidia-smi.", count)
                return count
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass

    cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES", "")
    if cuda_visible:
        devices = [d for d in cuda_visible.split(",") if d.strip()]
        count = len(devices)
        logger.info("Detected %d GPU(s) via CUDA_VISIBLE_DEVICES.", count)
        return count

    logger.warning(
        "Could not detect GPU count; defaulting to 1. "
        "Set CUDA_VISIBLE_DEVICES to override."
    )
    return 1


class GpuTaskScheduler:
    """
    A generic resource-managed scheduler that prevents GPU over-allocation
    by dynamically balancing available devices against single-task
    requirements.

    Each worker receives a ``gpu_device`` key in its keyword arguments,
    which can be used to set ``CUDA_VISIBLE_DEVICES`` or pass a ``--gpu``
    flag to a CUDA-compiled executable.

    Parameters
    ----------
    total_gpus : int or None
        Maximum number of GPUs to manage.  When *None* (default), the
        count is auto-detected via :func:`_detect_gpu_count`.
    """

    def __init__(self, total_gpus: int | None = None):
        self.total_gpus = total_gpus if total_gpus is not None else _detect_gpu_count()
        self.total_gpus = max(1, self.total_gpus)
        logger.info("GPU scheduler initialized with %d device(s).", self.total_gpus)

    def execute_parallel_tasks(
        self,
        worker_function: Callable[..., Any],
        task_items: List[Dict[str, Any]],
        task_per_gpu: int,
        identifier: str,
    ):
        """
        Execute a batch of GPU-accelerated tasks concurrently without
        overcommitting GPU resources.

        Each task item is augmented with a ``gpu_device`` key containing
        a zero-based logical GPU index assigned round-robin.  The worker
        function is responsible for routing work to that device (e.g., by
        setting ``CUDA_VISIBLE_DEVICES`` in a subprocess environment or
        passing ``--gpu`` to a CUDA executable).

        Parameters
        ----------
        worker_function : Callable
            The top-level function handling a single task item.  It must
            accept ``gpu_device`` as a keyword argument.
        task_items : List[Dict[str, Any]]
            A list of dictionaries containing arguments for
            *worker_function*.
        task_per_gpu : int
            The number of tasks working on the same GPU devices
        identifier : str
            The dictionary key used to identify each task in log messages.
        """
        if not task_items:
            logger.info("Task queue is empty.  No operations performed.")
            return

        max_workers = self.total_gpus * task_per_gpu

        logger.info("=== GPU Resource Allocation Strategy ===")
        logger.info("Total available GPU device pool: %d", self.total_gpus)
        logger.info(
            "Target footprint allocation: %d worker(s) per GPU",
            task_per_gpu,
        )
        logger.info("=========================================")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item: Dict[Any, Dict[str, Any]] = {}

            for idx, item_kwargs in enumerate(task_items):
                item_kwargs = dict(item_kwargs)
                item_kwargs.setdefault("gpu_device", idx % self.total_gpus)
                future = executor.submit(worker_function, **item_kwargs)
                future_to_item[future] = item_kwargs

            for future in as_completed(future_to_item):
                item_kwargs = future_to_item[future]
                target_identity = item_kwargs.get(identifier, "Unknown Target")
                try:
                    result_identifier = future.result()
                    logger.info(
                        "Success: task for [ %s ] finished on GPU %s.",
                        result_identifier,
                        item_kwargs.get("gpu_device", "?"),
                    )
                except Exception:
                    logger.exception(
                        "Failure: task for [ %s ] crashed.", target_identity
                    )
