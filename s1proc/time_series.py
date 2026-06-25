#!/usr/bin/env python3
from __future__ import annotations

import glob
import os
from datetime import datetime
from pathlib import Path
from typing import List

import numpy as np
import zarr
from matplotlib import pyplot as plt
from osgeo import gdal, osr
from tqdm import tqdm

from s1proc import sario
from s1proc._config import load_config
from s1proc._log import set_logging_level, setup_logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.sario import img2zarr, readc
from s1proc.utils import IfgList

plt.rcParams["image.interpolation"] = "none"

logger = setup_logger(name=__name__, level="INFO")

# ============================================================
# INPUT
# ============================================================


def get_input_files(input_path: str) -> List[str]:
    """
    Get interferograms to be corrected

    Parameters
    ----------
    input_path: str
        Input path

    Returns
    -------
    ifg_list: List[str]
        A list of interferograms to prcoess
    """
    p = Path(input_path)
    if p.is_file():
        return [input_path]
    elif p.is_dir():
        int_list = glob.glob(os.path.join(input_path, "*.int"))
        unw_list = glob.glob(os.path.join(input_path, "*.unw"))
        if len(int_list) > 0 and len(unw_list) > 0:
            logger.warning(
                "Find both wrapped and unwrapped interferograms"
                + f" in the input directory {input_path}."
            )
        ifg_list = np.concatenate([int_list, unw_list])
        if len(ifg_list) == 0:
            logger.warning(
                "Cannot find any interferogram from the input "
                + f"directory {input_path}."
            )
    else:
        ifg_list = glob.glob(input_path)
        if len(ifg_list) == 0:
            logger.warning(
                "Cannot find any interferogram from the input "
                + f"pattern {input_path}."
            )
    return ifg_list


def load_unwrapped_interferograms(
    ifg_path: str | Path | None = None, config: str = "config.yaml"
):
    cfg = load_config(config)
    icfg = cfg.io

    if ifg_path is None:
        ifg_path = icfg.unw_path
    ifg_files = get_input_files(ifg_path)
    nifg = len(ifg_files)
    logger.debug(f"Number of interferograms: {nifg}")

    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    logger.debug(f"Image shape: {nrow} x {ncol}")

    ifg_list = IfgList(ifg_files)
    attrs = {
        "name": "unwrapped interferograms",
        "date1": ifg_list.df["date1"].tolist(),
        "date2": ifg_list.df["date2"].tolist(),
        "tempbl": ifg_list.df["tempbl"].tolist(),
        "filenames": [s for s in ifg_files],
    }

    def load_function(input_file: str, nrow: int, ncol: int):
        return readc(input_file, ncol)

    img2zarr(
        ifg_files,
        load_function,
        "unwrapped_interferograms.zarr",
        nrow,
        ncol,
        np.complex64,
        attrs,
    )


def plot_debug_phase(zarray, step_name):
    amp, phase, valid = split_unw(zarray[0])
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    im1 = axes[0].imshow(amp, cmap="gray")
    axes[0].set_title(f"[{step_name}] Amplitude")
    fig.colorbar(im1, ax=axes[0])
    im2 = axes[1].imshow(phase, cmap="jet")
    axes[1].set_title(f"[{step_name}] Phase")
    fig.colorbar(im2, ax=axes[1])
    plt.tight_layout()
    plt.show()


def build_pairs(zarray):
    dates1 = zarray.attrs["date1"]
    dates2 = zarray.attrs["date2"]
    pairs = [
        (d1, d2, fname)
        for d1, d2, fname in zip(dates1, dates2, zarray.attrs["filenames"])
    ]
    dates = sorted(set(dates1 + dates2))
    date_index = {d: i for i, d in enumerate(dates)}
    return pairs, dates, date_index


def split_unw(unw):
    amp = unw.real.astype(np.float32)
    phase = unw.imag.astype(np.float32)
    valid = np.isfinite(phase) & (amp != 0)
    return amp, phase, valid


def load_phase_from_zarray(zarray, pairs, height, width):
    """
    Build phase vector φ from zarray.

    Returns
    -------
    phi : (n_ifg, n_pixels)
    """
    n_ifg = len(pairs)
    n_pixels = height * width
    phi = np.zeros((n_ifg, n_pixels), dtype=np.float32)
    amp = np.zeros((n_ifg, n_pixels), dtype=np.float32)
    for i, (d1, d2, fname) in enumerate(pairs):
        amp_i, phi_i, valid = split_unw(zarray[i])
        phi_i = phi_i.reshape(n_pixels)
        amp_i = amp_i.reshape(n_pixels)
        valid = np.isfinite(phi_i) & (amp_i != 0)
        phi[i, valid] = phi_i[valid]
        amp[i, valid] = amp_i[valid]
    return phi, amp


def phase_to_los_displacement(unw_phase, LAMBDA=0.05546576):
    """
    Convert unwrapped phase (radians)
    to LOS displacement (meters)
    """
    return unw_phase * LAMBDA / (4.0 * np.pi)


def generate_geotransform(rsc_file):
    rsc = GeoCoordinates(rsc_file)
    x_first = rsc.lonmin
    y_first = rsc.latmax
    x_step = rsc.dlon
    y_step = rsc.dlat

    geotransform = (x_first, x_step, 0.0, y_first, 0.0, y_step)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    projection = srs.ExportToWkt()
    return geotransform, projection


def prepare_stack_period(periods):
    """
    Prepare stack periods from config.

    Parameters
    ----------
    periods : list[tuple[str, str]]
        List of (start_date, end_date).

    Returns
    -------
    list[tuple[datetime, datetime, str, str]]
        Parsed periods.
    """
    parsed_periods = []
    for period_start, period_end in periods:
        parsed_periods.append(
            (
                datetime.strptime(period_start, "%Y%m%d"),
                datetime.strptime(period_end, "%Y%m%d"),
                period_start,
                period_end,
            )
        )
    return parsed_periods


def save_geotiff(outfile, data, geotransform, projection):
    rows, cols = data.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        outfile, cols, rows, 1, gdal.GDT_Float32, options=["COMPRESS=LZW", "TILED=YES"]
    )
    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection)
    band = ds.GetRasterBand(1)
    band.WriteArray(data.astype(np.float32))
    band.SetNoDataValue(np.nan)
    band.FlushCache()
    ds.FlushCache()
    ds = None


# ============================================================
# CORRECT FOR REFERENCE PHASE
# ============================================================
def remove_reference_phase(zarray, rsc_file, ref_lon, ref_lat):
    rsc = GeoCoordinates(rsc_file)
    width = rsc.nlon
    height = rsc.nlat
    x0 = rsc.lonmin
    y0 = rsc.latmax
    dx = rsc.dlon
    dy = rsc.dlat
    ref_col = int(round(abs(ref_lon - x0) / abs(dx)))
    ref_row = int(round(abs(ref_lat - y0) / abs(dy)))
    logger.info(f"Input Ref: lon={ref_lon}, lat={ref_lat}")
    logger.info(f"Calculated Indices: col={ref_col}, row={ref_row}")

    for i in tqdm(range(zarray.shape[0]), desc="reference phase"):
        amp, phase, valid = split_unw(zarray[i])

        ref = np.nanmean(
            phase[
                max(ref_row - 2, 0) : min(ref_row + 3, height),
                max(ref_col - 2, 0) : min(ref_col + 3, width),
            ]
        )
        if np.isnan(ref):
            logger.warning(f"Unw file {i} has invalid reference phase.")
            continue
        phase -= ref
        phase[~valid] = np.nan
        zarray[i] = amp + 1j * phase


# ============================================================
# PLANAR REMOVAL
# ============================================================
def remove_planar_trend(phase, valid_mask):
    rows, cols = phase.shape
    yy, xx = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    mask = valid_mask & np.isfinite(phase)
    if np.sum(mask) < 10:
        return phase
    x = xx[mask]
    y = yy[mask]
    z = phase[mask]
    G = np.stack([x, y, np.ones_like(x)], axis=1)
    m, _, _, _ = np.linalg.lstsq(G, z, rcond=None)
    plane = m[0] * xx + m[1] * yy + m[2]
    return phase - plane


def apply_planar_to_stack(zarray):
    for i in tqdm(range(zarray.shape[0]), desc="planar"):
        amp, phase, valid = split_unw(zarray[i])
        phase_corr = remove_planar_trend(phase, valid)
        zarray[i] = amp + 1j * phase_corr


# ============================================================
# MAD OUTLIER REMOVAL
# ============================================================
def remove_outliers_mad(zarray, rsc_file, MAD_SCALE=1.483, THRESHOLD_SIGMA=4.0):
    pairs, dates, _ = build_pairs(zarray)
    n_dates = len(dates)
    rsc = GeoCoordinates(rsc_file)
    width, height = rsc.nlon, rsc.nlat
    logger.info("Running vectorized MAD outlier detection...")

    date_mask = np.zeros((n_dates, len(pairs)), dtype=bool)
    for ifg_idx, (d1, d2, fname) in enumerate(pairs):
        for date_idx, date in enumerate(dates):
            if d1 == date or d2 == date:
                date_mask[date_idx, ifg_idx] = True

    phi_abs = np.abs(zarray[:])
    ubar = np.full((n_dates, height, width), np.nan, dtype=np.float32)
    for d_idx in range(n_dates):
        ifg_indices = np.where(date_mask[d_idx])[0]
        if len(ifg_indices) == 0:
            continue
        sub_data = phi_abs[ifg_indices, :, :]
        ubar[d_idx, :, :] = np.nanmean(sub_data, axis=0)
    valid_count = np.sum(np.isfinite(ubar), axis=0)
    pixel_mask = valid_count >= 5

    med = np.nanmedian(ubar, axis=0)  # (height, width)
    mad = np.nanmedian(np.abs(ubar - med), axis=0)  # (height, width)
    sigma_mad = MAD_SCALE * mad
    threshold = med + THRESHOLD_SIGMA * sigma_mad  # (height, width)
    is_bad_date = (ubar > threshold) & pixel_mask

    for ifg_idx, (d1, d2, fname) in enumerate(pairs):
        d1_idx = dates.index(d1)
        d2_idx = dates.index(d2)
        bad_pixel_mask = is_bad_date[d1_idx] | is_bad_date[d2_idx]
        if np.any(bad_pixel_mask):
            unw = zarray[ifg_idx]
            unw.imag[bad_pixel_mask] = np.nan
            unw.real[bad_pixel_mask] = 0
            zarray[ifg_idx] = unw
    logger.info("MAD outlier detection completed.")


# ============================================================
# SAVE CORRECTED UNWRAPPED IFGS
# ============================================================
def save_corrected_unws(zarray, out_dir):
    filenames = zarray.attrs["filenames"]
    for i, infile in tqdm(enumerate(filenames), desc="saving corrected unws"):
        amp, phase, valid = split_unw(zarray[i])
        unw_corr = amp + 1j * phase
        unw_corr[~valid] = 0 + 1j * np.nan
        outfile = os.path.join(out_dir, os.path.basename(infile))
        sario.savec(unw_corr, outfile)


# ============================================================
# METHOD: STACKING
# ============================================================
def stack_one_period(zarray, period):
    """
    Stack interferograms within one time period.

    Parameters
    ----------
    zarray : ndarray
        Preallocated array or zarray dataset containing interferograms.
    period : tuple
        (t_start, t_end, period_start, period_end)

    Returns
    -------
    velocity : ndarray
        Average LOS velocity (cm/year).
    cumulative : ndarray
        Cumulative LOS displacement (cm).
    n_used : int
        Number of interferograms used.
    """
    t_start, t_end, _, _ = period
    date1 = zarray.attrs["date1"]
    date2 = zarray.attrs["date2"]
    height, width = zarray.shape[-2:]
    stack_sum = np.zeros((height, width), dtype=np.float64)
    time_sum = np.zeros((height, width), dtype=np.float64)
    n_used = 0
    for i, (d1, d2) in enumerate(zip(date1, date2)):
        d1_dt = datetime.strptime(d1, "%Y%m%d")
        d2_dt = datetime.strptime(d2, "%Y%m%d")
        if d1_dt < t_start or d2_dt > t_end:
            continue
        baseline_days = (d2_dt - d1_dt).days

        amp, phase, valid = split_unw(zarray[i])
        los_disp = np.full_like(phase, np.nan, dtype=np.float32)
        los_disp[valid] = phase_to_los_displacement(phase[valid])
        stack_sum[valid] += los_disp[valid]
        time_sum[valid] += baseline_days
        n_used += 1
    if n_used == 0:
        return None, None, 0

    velocity = np.divide(
        stack_sum,
        time_sum,
        out=np.full_like(stack_sum, np.nan),
        where=time_sum > 0,
    )

    total_days = (t_end - t_start).days
    cumulative = velocity * total_days
    velocity = velocity * 100.0 * 365.25
    cumulative = cumulative * 100.0
    return velocity, cumulative, n_used


def save_stack(velocity, cumulative, period, out_dir, rsc_file):
    """
    Save stack results.

    Parameters
    ----------
    velocity : ndarray
        Velocity (cm/year).
    cumulative : ndarray
        Cumulative displacement (cm).
    period : tuple
        (t_start, t_end, period_start, period_end)
    out_dir : str
        Output directory.
    rsc_file : str
        ROI_PAC rsc file.
    """

    _, _, period_start, period_end = period
    tag = f"{period_start}_{period_end}"

    vel_tif = os.path.join(
        out_dir,
        f"stack_velocity_cm_per_year_{tag}.tif",
    )
    cum_tif = os.path.join(
        out_dir,
        f"stack_cumulative_cm_{tag}.tif",
    )
    geotransform, projection = generate_geotransform(rsc_file)
    save_geotiff(
        vel_tif,
        velocity,
        geotransform,
        projection,
    )
    save_geotiff(
        cum_tif,
        cumulative,
        geotransform,
        projection,
    )
    logger.info("\nSaved:")
    logger.info(vel_tif)
    logger.info(cum_tif)


# ============================================================
# METHOD: SBAS
# ============================================================
def build_design_matrix(pairs, date_index):
    """
    SBAS design matrix A (IFG x dates-1)
    """
    n_ifg = len(pairs)
    n_dates = len(date_index)
    A = np.zeros((n_ifg, n_dates), dtype=np.float32)
    idx = np.arange(n_ifg)
    d1_idx = [date_index[d1] for d1, d2, fname in pairs]
    d2_idx = [date_index[d2] for d1, d2, fname in pairs]
    A[idx, d1_idx] = -1
    A[idx, d2_idx] = 1
    A = A[:, 1:]
    return A


def sbas_inversion(phi, A_pinv):
    """
    Solve d = A⁺ φ
    and restore reference epoch
    """
    d = A_pinv @ phi
    d_full = np.vstack([np.zeros((1, d.shape[1])), d])
    return d_full


def save_sbas(
    tag,
    velocity_map,
    out_dir,
    rsc_file,
):
    """
    Save SBAS velocity + intercept maps
    """
    geotransform, projection = generate_geotransform(rsc_file)
    vel_path = os.path.join(out_dir, f"sbas_velocity_cm_per_year_{tag}.tif")
    save_geotiff(vel_path, velocity_map * 100.0 * 365.25, geotransform, projection)
    logger.info("Saved SBAS maps:")
    logger.info(vel_path)


def save_sbas_timeseries(
    tag,
    dates,
    times,
    d_full,
    out_dir,
):
    """
    Save SBAS time series output
    """
    ts_path = os.path.join(out_dir, f"sbas_timeseries_{tag}.npz")
    np.savez(
        ts_path,
        dates=np.array(dates),
        times=times,
        d_full=d_full,
    )
    logger.info(f"Saved time series: {ts_path}")


def plot_sbas_checkpoints(d_full, times, rsc_file, checkpoints_file, out_dir, tag):
    """
    Extract and plot displacement time series for custom checkpoints.
    """
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.weight": "bold",
            "axes.labelweight": "bold",
            "axes.titleweight": "bold",
            "font.size": 8,
            "axes.labelsize": 9,
            "axes.titlesize": 10,
            "legend.fontsize": 7,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
        }
    )

    if not os.path.exists(checkpoints_file):
        logger.warning(f"Checkpoint file not found: {checkpoints_file}")
        return
    points = []
    with open(checkpoints_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) >= 3:
                idx = int(parts[0])
                lon = float(parts[1])
                lat = float(parts[2])
                points.append((idx, lon, lat))

    rsc = GeoCoordinates(rsc_file)
    width = rsc.nlon
    height = rsc.nlat
    x0 = rsc.lonmin
    y0 = rsc.latmax
    dx = rsc.dlon
    dy = rsc.dlat

    n_points = len(points)
    n_cols = 2
    n_rows = (n_points + 1) // 2
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(12, 3.5 * n_rows))
    axes = np.atleast_1d(axes).flatten()

    window_size = 5
    ymin, ymax = 1e9, -1e9
    all_ts = []
    valid_plots = []
    for i, (pid, lon, lat) in enumerate(points):
        ax = axes[i]
        col = int(round(abs(lon - x0) / abs(dx)))
        row = int(round(abs(lat - y0) / abs(dy)))
        ts_stack = []
        for dr in range(-(window_size // 2), window_size // 2 + 1):
            for dc in range(-(window_size // 2), window_size // 2 + 1):
                r = row + dr
                c = col + dc
                if 0 <= r < height and 0 <= c < width:
                    flat_idx = r * width + c
                    ts_pixel = d_full[:, flat_idx]
                    if not np.isnan(ts_pixel).all():
                        ts_stack.append(ts_pixel)
        if len(ts_stack) == 0:
            logger.warning(f"Point {pid}: no valid pixels in 5x5 window")
            ax.text(0.5, 0.5, f"Point {pid}\nNo Valid Data", ha="center", va="center")
            continue
        ts_stack = np.array(ts_stack)
        ts_mean = np.nanmean(ts_stack, axis=0)
        ts_mean = -ts_mean * 1000.0
        all_ts.append(ts_mean)
        valid_plots.append(ax)

        n_used = ts_stack.shape[0]
        coef = np.polyfit(times, ts_mean, 1)
        ax.plot(times, ts_mean, marker="o", markersize=4, label=f"P{pid}")
        ax.text(
            0.05,
            0.88,
            f"Slope={coef[0]:.1f} mm/yr",
            transform=ax.transAxes,
            verticalalignment="top",
        )
        ax.set_title(f"Point {pid} (lon={lon:.5f}, lat={lat:.5f}, n={n_used})")
        ax.set_xlabel("Time (years)")
        ax.set_ylabel("Displacement (mm)")
        ax.grid(True)
    for ts in all_ts:
        ymin = min(ymin, np.nanmin(ts))
        ymax = max(ymax, np.nanmax(ts))
    for ax in valid_plots:
        ax.set_ylim(ymin * 1.1 - 1, ymax * 1.1 + 1)
    for j in range(n_points, len(axes)):
        fig.delaxes(axes[j])
    plt.tight_layout()
    plot_out_path = os.path.join(out_dir, f"sbas_checkpoints_{tag}.png")
    plt.savefig(plot_out_path, dpi=300)
    logger.info(f"Saved checkpoints plot to: {plot_out_path}")
    plt.close()


def sbas_one_period(
    zarray,
    period,
    out_dir,
    rsc_file=None,
    check_points: bool = False,
    check_points_list: str | None = None,
):
    """
    SBAS time series pipeline (zarray-based).
    """
    t_start, t_end, period_start, period_end = period
    all_pairs, _, _ = build_pairs(zarray)
    filtered_pairs = []
    filtered_indices = []
    for i, (d1, d2, fname) in enumerate(all_pairs):
        d1_dt = datetime.strptime(d1, "%Y%m%d")
        d2_dt = datetime.strptime(d2, "%Y%m%d")
        if d1_dt >= t_start and d2_dt <= t_end:
            filtered_pairs.append((d1, d2, fname))
            filtered_indices.append(i)
    if not filtered_pairs:
        return 0
    d1_list = [p[0] for p in filtered_pairs]
    d2_list = [p[1] for p in filtered_pairs]
    dates = sorted(set(d1_list + d2_list))
    date_index = {d: idx for idx, d in enumerate(dates)}
    height, width = zarray.shape[-2:]
    n_pixels = height * width
    logger.info(f"IFG数量: {len(filtered_pairs)}, 时间点: {len(dates)}")

    A = build_design_matrix(filtered_pairs, date_index)
    A_pinv = np.linalg.pinv(A)

    dates_dt = [datetime.strptime(d, "%Y%m%d") for d in dates]
    times = (
        np.array(
            [(dt - dates_dt[0]).days for dt in dates_dt],
            dtype=np.float32,
        )
        / 365.25
    )

    n_ifg = len(filtered_pairs)
    phi = np.zeros((n_ifg, n_pixels), dtype=np.float32)
    amp_mask = np.zeros_like(phi, dtype=bool)
    for local_i, original_i in enumerate(filtered_indices):
        amp_i, phi_i, valid_2d = split_unw(zarray[original_i])
        phi_i = phi_i.reshape(n_pixels)
        amp_i = amp_i.reshape(n_pixels)
        valid = valid_2d.reshape(n_pixels)
        phi[local_i, valid] = phi_i[valid]
        amp_mask[local_i, valid] = True
    valid_pixels = amp_mask.any(axis=0)

    phi_valid = phi[:, valid_pixels]
    d_full_valid = sbas_inversion(phi_valid, A_pinv)
    disp_full_valid = phase_to_los_displacement(d_full_valid)
    G = np.vstack([times, np.ones_like(times)]).T
    G_pinv = np.linalg.pinv(G)
    coef = G_pinv @ disp_full_valid

    velocity_valid = coef[0, :]
    velocity_map = np.full(n_pixels, np.nan, dtype=np.float32)
    velocity_map[valid_pixels] = velocity_valid
    velocity_map = velocity_map.reshape(height, width)

    d_full = np.full((len(dates), n_pixels), np.nan, dtype=np.float32)
    d_full[:, valid_pixels] = disp_full_valid

    tag = f"{period_start}_{period_end}"
    save_sbas(
        tag,
        velocity_map,
        out_dir,
        rsc_file,
    )
    save_sbas_timeseries(
        tag,
        dates,
        times,
        d_full,
        out_dir,
    )

    if check_points and check_points_list:
        logger.info("Generating checkpoints time series plots...")
        plot_sbas_checkpoints(
            d_full=d_full,
            times=times,
            rsc_file=rsc_file,
            checkpoints_file=check_points_list,
            out_dir=out_dir,
            tag=tag,
        )

    return n_ifg


# ============================================================
# MAIN PIPELINE
# ============================================================


def time_series(
    verbose: bool = False,
    ploton: bool = False,
    config: str = "config.yaml",
):
    """
    Run time series analysis.

    Parameters
    ----------
    verbose: bool
        If True, set the logging level to DEBUG
    config: Path|str
        Configuration file
    ploton: bool
        If True, show debug phase plots during processing
    """
    if verbose:
        set_logging_level(logger, "DEBUG")
    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.timeseries

    ifg_path = icfg.unw_path
    rsc_file = icfg.multilook_rsc_file
    out_dir = icfg.unw_corr_path
    os.makedirs(out_dir, exist_ok=True)
    result_dir = icfg.time_series_path
    os.makedirs(result_dir, exist_ok=True)

    ref_lon = pcfg.parameters.ref_lon
    ref_lat = pcfg.parameters.ref_lat
    method = pcfg.method
    mad_scale = (
        pcfg.parameters.mad_scale if pcfg.parameters.mad_scale is not None else 1.483
    )
    threshold_sigma = (
        pcfg.parameters.threshold_sigma
        if pcfg.parameters.threshold_sigma is not None
        else 4.0
    )
    periods = pcfg.parameters.periods

    # ========================================================
    # STEP 1: load IFGs and form zarray
    # ========================================================
    load_unwrapped_interferograms(ifg_path, config)

    # ========================================================
    # STEP 2: prepare IFGs
    # ========================================================
    z = zarr.open("unwrapped_interferograms.zarr", mode="r+")
    if ploton:
        plot_debug_phase(z, "1. Initial Load")

    remove_reference_phase(z, rsc_file, ref_lon, ref_lat)
    if ploton:
        plot_debug_phase(z, "2. Post_Reference Phase")
    apply_planar_to_stack(z)
    if ploton:
        plot_debug_phase(z, "3. Post-Planar Trend")
    remove_outliers_mad(z, rsc_file, mad_scale, threshold_sigma)
    if ploton:
        plot_debug_phase(z, "4. Post-MAD Filter")

    save_corrected_unws(z, out_dir)

    # ========================================================
    # STEP 3: time series analysis
    # ========================================================
    if method == "stack":
        all_periods = prepare_stack_period(periods)
        for period in all_periods:
            _, _, period_start, period_end = period
            logger.info("\n================================================")
            logger.info(f"Stack Processing: {period_start} -> {period_end}")
            logger.info("================================================")
            velocity, cumulative, n_used = stack_one_period(
                zarray=z,
                period=period,
            )
            if n_used == 0:
                logger.info("No IFGs used.")
                continue
            save_stack(
                velocity=velocity,
                cumulative=cumulative,
                period=period,
                out_dir=result_dir,
                rsc_file=rsc_file,
            )
            logger.info("\nDONE")
            logger.info(f"Used IFGs: {n_used}")

    elif method == "sbas":
        all_periods = prepare_stack_period(periods)
        for period in all_periods:
            _, _, period_start, period_end = period
            logger.info("\n================================================")
            logger.info(f"SBAS Processing: {period_start} -> {period_end}")
            logger.info("================================================")
            cp_enabled = getattr(pcfg.parameters, "check_points", False)
            cp_list = getattr(pcfg.parameters, "check_points_list", None)
            n_used = sbas_one_period(
                zarray=z,
                period=period,
                out_dir=result_dir,
                rsc_file=rsc_file,
                check_points=cp_enabled,
                check_points_list=cp_list,
            )
            if n_used == 0:
                logger.info("No IFGs used.")
                continue


if __name__ == "__main__":
    import tyro

    tyro.cli(time_series)
