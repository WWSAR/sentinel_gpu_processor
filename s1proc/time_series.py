from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Sequence, Tuple

import cupy as cp
import dask.array as da
import numpy as np
from matplotlib import pyplot as plt
from numpy.typing import NDArray

from s1proc._log import setup_logger
from s1proc.utils import IfgList

plt.rcParams["image.interpolation"] = "none"

logger = setup_logger(name=__name__, level="INFO")


def stack_plain(
    unw_stack: NDArray[np.float32],
    ref_phase: NDArray[np.float32],
    B: NDArray[np.float32],
    wvl: float,
) -> NDArray[np.float32]:
    nifg, chunk_row, chunk_col = unw_stack.shape
    npixels = chunk_row * chunk_col
    d_unw_stack = cp.array(unw_stack.reshape(nifg, npixels))
    d_ref_phase = cp.array(ref_phase.reshape(nifg, 1))
    # temporal baseline
    d_tempbl = cp.array(np.sum(B, axis=1))
    v = (
        cp.sum(d_unw_stack - d_ref_phase, axis=0)
        / cp.sum(d_tempbl)
        * (wvl / 4 / np.pi * 365.25)
    )
    return cp.asnumpy(v).reshape(chunk_row, chunk_col)


def stack_with_outlier_removal(
    unw_stack: NDArray[np.float32],
    ref_phase: NDArray[np.float32],
    B: NDArray[np.float32],
    wvl: float,
    mad_scalar: float,
) -> NDArray[np.float32]:
    nifg, chunk_row, chunk_col = unw_stack.shape
    npixels = chunk_row * chunk_col

    # remove reference phase
    d_unw_stack = cp.array(unw_stack.reshape(nifg, npixels))
    d_ref_phase = cp.array(ref_phase.reshape(nifg, 1))
    d_unw_stack -= d_ref_phase

    d_B = cp.array(B)
    d_tempbl = cp.sum(d_B, axis=1)

    numerator_v = cp.matmul(cp.sign(d_B).T, d_unw_stack)

    denominator_v = cp.matmul(
        (d_B.T != 0).astype(cp.float32), cp.abs(d_tempbl)[:, None]
    )

    d_v = numerator_v / (denominator_v + 1e-6)

    d_medv = cp.median(cp.abs(d_v), axis=0)
    d_devv = cp.abs(d_v - d_medv[None, :])
    d_madv = cp.median(d_devv, axis=0)

    d_valid_mask = d_devv <= (d_madv[None, :] * mad_scalar)

    has_outlier = cp.matmul(
        (d_B != 0).astype(cp.float32), (~d_valid_mask).astype(cp.float32)
    )
    d_ifg_mask = has_outlier == 0

    d_filtered_unw = d_unw_stack * d_ifg_mask
    d_filtered_bl_sum = cp.sum(d_tempbl[:, None] * d_ifg_mask, axis=0)

    v_final = cp.sum(d_filtered_unw, axis=0) / (d_filtered_bl_sum + 1e-6)
    v_final *= wvl / (4 * np.pi) * 365.25

    return cp.asnumpy(v_final).reshape(chunk_row, chunk_col)


def time_series_solver(
    unw_files: Sequence[str],
    mask_file: Path | str | None,
    out_path: Path | str,
    nrow: int,
    ncol: int,
    solver_func: Callable,
    reference_point: Tuple[int, int],
    reference_win: Tuple[int, int],
    output_dim: Literal["2d", "3d"] = "3d",
    row_chunk_size: int | None = None,
    col_chunk_size: int | None = None,
):
    """
    Solve InSAR Timeseries

    Parameters
    ----------
    unw_files: Sequence[str]
        List of unwrapped interferograms (binary float32)
    mask_file: str | None
        A boolean mask. True means valid radar pixel, on which the time series analysis
        should be performed
    nrow: int
        Number of rows of each interferogram
    ncol: int
        Number of columns of each interferogram
    solver_func: Callable
        Core function to solve the time series of a single block
    reference_point: Tuple[int, int]
        Row and column indices of the reference point
    reference_win: Tuple[int, int]
        Window size used for reference phase calculation
    output_dim: Literal["2d, "3d"]
        2d: return velocity map
        3d: return time series
    row_chunk_size: int | None
        Number of rows of a single data chunk
    col_chunk_size: int | None
        Number of columns of a single data chunk
    """
    unw_list = IfgList(unw_files)
    logger.debug(f"Time series analysis with {len(unw_list)} interferograms")
    logger.debug("Create image stacks with dask")
    ifg_memmaps = [
        np.memmap(f, dtype="float32", mode="r", shape=(nrow, ncol)) for f in unw_files
    ]
    unw_dask_slices = [da.from_array(m, chunks=(nrow, ncol)) for m in ifg_memmaps]
    unw_stack = da.stack(unw_dask_slices, axis=0)  # shape (nifg, nrow, ncol)
    logger.debug(f"Load mask from {mask_file}")
    if mask_file is not None:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    # rechunk dask stack
    if row_chunk_size is None:
        row_chunk_size = np.minimum(128, nrow)
    if col_chunk_size is None:
        col_chunk_size = np.minimum(128, ncol)
    unw_stack = unw_stack.rechunk({0: -1, 1: row_chunk_size, 2: col_chunk_size})
    logger.debug(
        f"Rechunk dask stack, row chunk size: {row_chunk_size}"
        + f"column chunk size: {col_chunk_size}"
    )

    internal_kwargs = {}
    ref_phase = None
    # check the validity of reference point
    if (
        reference_point[0] < 0
        or reference_point[0] >= nrow
        or reference_point[1] < 0
        or reference_point[1] >= ncol
    ):
        raise ValueError(
            f"Reference point ({reference_point[0], reference_point[1]}) out "
            + "of boundary."
        )
    elif not mask[reference_point[0], reference_point[1]]:
        raise RuntimeError(
            f"Reference point ({reference_point[0], reference_point[1]}) is on "
            + "the masked area."
        )
    elif reference_win[0] < 0 or reference_win[1] < 0:
        raise ValueError("Reference window size must be positive.")
    else:
        half_row_win = (reference_win[0] + 1) // 2
        half_col_win = (reference_win[1] + 1) // 2
        top = int(np.maximum(0, reference_point[0] - half_row_win + 1))
        bottom = int(np.minimum(nrow, reference_point[0] + half_row_win))
        left = int(np.maximum(0, reference_point[1] - half_col_win + 1))
        right = int(np.minimum(ncol, reference_point[1] + half_col_win))
        logger.debug(
            f"Reference point window, left: {left}, right: {right},"
            + f"top: {top}, bottom: {bottom}."
        )
        ref_win_mask = mask[top:bottom, left:right]
        ref_win_mask = ref_win_mask[None, :, :]
        ref_tube = unw_stack[:, top:bottom, left:right].compute()
        ref_phase = np.mean((ref_tube * ref_win_mask), axis=(1, 2))

    B = unw_list.int_velocity_matrix()

    if output_dim == "2d":
        time_series = da.map_blocks(
            solver_func,
            unw_stack,
            dtype=np.float32,
            drop_axis=0,
            B=B,
            ref_phase=ref_phase,
        )
    else:
        new_chunks = (unw_list.ndate, unw_stack.chunks[1], unw_stack.chunks[2])
        time_series = da.map_blocks(
            solver_func,
            unw_stack,
            dtype=np.float32,
            drop_axis=0,
            new_axis=0,
            chunks=new_chunks,
            kwargs=internal_kwargs,
        )
    out_path = Path(out_path)
    out_path.mkdir(parents=True, exist_ok=True)
    time_series.to_zarr(out_path, overwrite=True)


def run_time_series(
    unw_path: Path | str | None = None,
    outpath: Path | str | None = None,
    config: str = "config.yaml",
):
    """
    Run time series analysis

    Parameters
    ----------
    unw_path: Path | str | None
        Input unwrapped interferograms for time series analysis
    outpath: Path|str|None
        Time series output path
    config: str
        Configuration file
    """
    from functools import partial

    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates
    from s1proc.utils import get_files

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    tcfg = cfg.timeseries
    if unw_path is None:
        unw_files = get_files(icfg.unw_corr_path, "unw")
        if len(unw_files) == 0:
            unw_files = get_files(icfg.unw_path, "unw")
        if len(unw_files) == 0:
            logger.warning("Cannot find unwrapped interferograms.")
            return
    else:
        unw_files = get_files(unw_path, "unw")
    if outpath is None:
        outpath = icfg.time_series_path
    mask_file = icfg.mask_file
    ref_lat = tcfg.parameters.ref_lat
    ref_lon = tcfg.parameters.ref_lon
    rsc_file = icfg.multilook_rsc_file
    rsc = GeoCoordinates(rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    reference_point = rsc.ll2xy(ref_lat, ref_lon)
    reference_win = (11, 11)
    mad_scalar = tcfg.parameters.mad_scalar
    if mad_scalar is not None and mad_scalar < 0:
        raise ValueError(f"mad_scalar cannot be negative, current value: {mad_scalar}.")
    if tcfg.method == "stack":
        if mad_scalar is None or mad_scalar == 0:
            solver_func = partial(stack_plain, wvl=pcfg.wavelength)
        else:
            solver_func = partial(
                stack_with_outlier_removal,
                mad_scalar=mad_scalar,
                wvl=pcfg.wavelength,
            )
    time_series_solver(
        unw_files,
        mask_file,
        (Path(outpath) / "time_series.zarr"),
        nrow,
        ncol,
        solver_func,
        reference_point,
        reference_win,
        output_dim="2d",
    )


def generate_geotransform(rsc_file):
    from osgeo import osr

    from s1proc.geocoordinates import GeoCoordinates

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


def save_geotiff(outfile, data, geotransform, projection):
    from osgeo import gdal

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


#
#
# def prepare_stack_period(periods):
#    """
#    Prepare stack periods from config.
#
#    Parameters
#    ----------
#    periods : list[tuple[str, str]]
#        List of (start_date, end_date).
#
#    Returns
#    -------
#    list[tuple[datetime, datetime, str, str]]
#        Parsed periods.
#    """
#    parsed_periods = []
#    for period_start, period_end in periods:
#        parsed_periods.append(
#            (
#                datetime.strptime(period_start, "%Y%m%d"),
#                datetime.strptime(period_end, "%Y%m%d"),
#                period_start,
#                period_end,
#            )
#        )
#    return parsed_periods
