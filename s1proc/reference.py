"""Reference-point selection for unwrapped interferograms.

The module provides tools for identifying optimal GPS reference stations
by extracting small patches of unwrapped phase data around each GPS site
and evaluating their consistency across interferogram stacks.
"""

import datetime
import shutil
from pathlib import Path
from typing import Any, Dict, Sequence, Tuple

import numpy as np
import pandas as pd
import zarr
from numpy.typing import NDArray

from s1proc._config import load_config
from s1proc._log import logger, set_logging_level
from s1proc.geocoordinates import GeoCoordinates
from s1proc.gps import gps
from s1proc.sario import readc
from s1proc.utils import IfgList, get_files

# Sentinel for pixels outside the valid radar footprint.
_INVALID = 0.0


def _station_pixel_indices(
    df_stations: pd.DataFrame, rsc: GeoCoordinates
) -> Tuple[NDArray[np.intp], NDArray[np.intp]]:
    """Return (rows, cols) pixel indices for every station in *df_stations*."""
    lat = df_stations["lat"].to_numpy()
    lon = df_stations["lon"].to_numpy()
    rows, cols = rsc.ll2xy(lat, lon)
    return rows, cols


def _crop_patch(
    full_image: NDArray[np.float32],
    center_row: int,
    center_col: int,
    patch_size: int,
) -> NDArray[np.float32]:
    """Crop a square patch of side `patch_size` centered at a pixel coordinate.

    When `patch_size` is odd the centre pixel maps to the geometric centre of
    the patch; when even the patch extends ``patch_size // 2`` rows/cols to
    the left and top of the centre.

    Pixels outside the image bounds are filled with ``_INVALID``.
    """
    nrow, ncol = full_image.shape
    r0 = center_row - patch_size // 2
    r1 = r0 + patch_size
    c0 = center_col - patch_size // 2
    c1 = c0 + patch_size

    patch = np.full((patch_size, patch_size), _INVALID, dtype=np.float32)

    # Intersection with the image domain
    src_r0 = max(r0, 0)
    src_r1 = min(r1, nrow)
    src_c0 = max(c0, 0)
    src_c1 = min(c1, ncol)

    dst_r0 = src_r0 - r0
    dst_r1 = dst_r0 + (src_r1 - src_r0)
    dst_c0 = src_c0 - c0
    dst_c1 = dst_c0 + (src_c1 - src_c0)

    patch[dst_r0:dst_r1, dst_c0:dst_c1] = full_image[src_r0:src_r1, src_c0:src_c1]
    return patch


def _choose_ifg_chunk_size(patch_size: int, target_bytes: int = 2_000_000) -> int:
    """Return the number of interferograms to bundle per zarr chunk.

    Parameters
    ----------
    patch_size : int
        Side length of a square patch in pixels.
    target_bytes : int
        Approximate uncompressed size (bytes) targeted for a single chunk.

    Returns
    -------
    int
        Number of interferograms along the second axis packed into one chunk.
        Clamped to [1, 4096].
    """
    bytes_per_ifg = patch_size * patch_size * 4  # float32
    n = int(target_bytes / bytes_per_ifg) if bytes_per_ifg > 0 else 1
    return max(1, min(n, 4096))


def _extract_gps_patches(
    unw_files: Sequence[str | Path],
    df_stations: pd.DataFrame,
    rsc: GeoCoordinates,
    patch_size: int = 11,
    output_file: str | Path = "gps_patches.zarr",
) -> Path:
    """Crop unwrapped phase patches around each GPS station and save to zarr.

    For each interferogram in `unw_files`, a small square patch of unwrapped
    phase is cropped around every GPS station.  The resulting array has shape
    ``(ngps, nifg, patch_size, patch_size)`` and is written to a zarr store so
    that individual patches can be read without loading the full stack.

    Parameters
    ----------
    unw_files : Sequence[str | Path]
        Paths to unwrapped interferograms (flat binary float32, row-major,
        shape ``(rsc.nlat, rsc.nlon)``).
    df_stations : pandas.DataFrame
        GPS stations indexed by name with columns ``["lat", "lon"]``, e.g.
        output of ``load_gps_stations_in_bbox()``.
    rsc : GeoCoordinates
        Geo-referencing information matching `unw_files`.
    patch_size : int
        Side length (in pixels) of each cropped patch.  Must be odd so that
        the patch centres cleanly on the station pixel.
    output_file : str | Path
        Path for the output zarr store.

    Returns
    -------
    pathlib.Path
        The path to the saved zarr store.
    """
    if patch_size % 2 == 0:
        raise ValueError("patch_size must be odd, got %d" % patch_size)

    output_file = Path(output_file)

    # -- Build the interferogram list to capture temporal baselines --
    ifg_list = IfgList([str(p) for p in unw_files])
    tempbl = ifg_list.df["tempbl"].tolist()
    nifg = ifg_list.nifg

    # -- Pixel coordinates for each station --
    rows, cols = _station_pixel_indices(df_stations, rsc)
    ngps = len(df_stations)

    if ngps == 0:
        raise ValueError("No GPS stations in `df_stations`")

    # -- Chunk size: bundle interferograms so each chunk is ~2 MB --
    chunk_n = _choose_ifg_chunk_size(patch_size)
    chunk_n = int(min(chunk_n, nifg))

    logger.info(
        "Extracting GPS patches: %d stations x %d interferograms, "
        "patch_size=%d -> (%d, %d, %d, %d), chunk_n=%d",
        ngps,
        nifg,
        patch_size,
        ngps,
        nifg,
        patch_size,
        patch_size,
        chunk_n,
    )

    # -- Zarr store --
    store = zarr.open(
        str(output_file),
        mode="w",
        shape=(ngps, nifg, patch_size, patch_size),
        chunks=(1, chunk_n, patch_size, patch_size),
        dtype=np.float32,
    )

    # -- Metadata (persisted as JSON strings to fit zarr attr limits) --
    store.attrs["station_names"] = list(df_stations.index)
    store.attrs["unw_files"] = [str(p) for p in unw_files]
    store.attrs["temporal_baselines"] = tempbl
    store.attrs["patch_size"] = patch_size
    store.attrs["chunk_n"] = chunk_n

    nrow, ncol = rsc.nlat, rsc.nlon

    # -- Buffer to accumulate patches across interferograms before flushing to
    #    zarr.  Each flush writes whole chunks (ngps × chunk_n × patch²) or the
    #    final partial batch, avoiding repeated small writes to the same chunk.
    buf = np.empty((ngps, chunk_n, patch_size, patch_size), dtype=np.float32)
    buf_pos = 0
    buf_start = 0

    for i, imgfile in enumerate(unw_files):
        data = np.fromfile(str(imgfile), dtype=np.float32).reshape(nrow, ncol)
        for j in range(ngps):
            buf[j, buf_pos, :, :] = _crop_patch(
                data, int(rows[j]), int(cols[j]), patch_size
            )
        buf_pos += 1
        if buf_pos == chunk_n or i == nifg - 1:
            store[:, buf_start : buf_start + buf_pos, :, :] = buf[:, :buf_pos, :, :]
            buf_start += buf_pos
            buf_pos = 0

    logger.info(
        "GPS patches saved to %s (shape: %s)",
        output_file,
        store.shape,
    )
    return output_file


def _prepare_station_df(
    start_date: str | datetime.date,
    end_date: str | datetime.date,
    bbox: Tuple[float, float, float, float],
    rsc: GeoCoordinates,
    mask: NDArray[np.bool_],
    los: NDArray[np.float32],
):
    """
    Prepare a GPS list DataFrame for reference point selection

    Parameters
    ----------
    start_date: str | datetime.date
        Starting date of the study period
    end_date: str | datetime.date
        Ending date of the study period
    bbox: Tuple[float, float, float, float]
        Bounding box of the study area (west, south, east, north)
    rsc: GeoCoordinates
        A GeoCoordinates object defining the convertion from latitude/longitude to pixel
        coordinates
    mask: np.ndarray
        Mask of valid pixels (True for valid ones, nrow x ncol)
    los: np.ndarray
        Unit LOS vector in ECEF coordinates at all radar pixels (nrow x ncol x 3)

    Returns
    -------
    df_stations: pandas.DataFrame
        A DataFrame of all GPS stations
    """
    df_stations = gps.load_gps_stations_in_bbox(
        bbox, start_date=start_date, end_date=end_date
    )
    df_stations = gps.filter_by_mask(df_stations, mask, rsc)
    df_stations = gps.append_los_vector(df_stations, los, rsc)
    df_stations = gps.calc_los_velocity(
        df_stations, start_date=start_date, end_date=end_date
    )
    return df_stations


def prepare_station_df(cfg: Dict[str, Any]) -> pd.DataFrame:
    """
    Prepare a pandas DataFrame that contains all GPS stations within the study area

    Parameters
    ----------
    cfg: Dict[str, Any]
        A configuration dictionary

    Returns
    -------
    pandas.DataFrame
        A pandas DataFrame taht containss all GPS stations within the study area
    """
    geometry_path = Path(cfg.io.geometry_path)
    if not geometry_path.exists():
        geometry_path.mkdir(exist_ok=True, parents=True)
    losvec_file = geometry_path / "los"
    theta_file = geometry_path / "look_angle"
    dem_file = Path(cfg.io.multilook_dem_file)
    mask_file = Path(cfg.io.mask_file)
    df_stations_out_file = Path(cfg.io.proc_path) / "gps_stations.csv"
    df_stations_out_file.parent.mkdir(exist_ok=True, parents=True)
    if not dem_file.exists():
        from s1proc.utils import multilook_dem

        multilook_dem(
            cfg.io.dem_file,
            cfg.io.rsc_file,
            dem_file,
            cfg.io.multilook_rsc_file,
            cfg.proc.rowlook,
            cfg.proc.collook,
        )

    if not losvec_file.exists():
        from s1proc.utils import los

        los(
            cfg.io.multilook_dem_file,
            cfg.io.multilook_rsc_file,
            proc_dir=cfg.io.proc_path,
            losvec_file=losvec_file,
            theta_file=theta_file,
        )
    rsc = GeoCoordinates(cfg.io.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    if not mask_file.exists():
        logger.warning(f"Cannot find the mask file: {mask_file}")
        mask = np.ones((nrow, ncol), dtype=np.bool_)
    else:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    losvec = np.fromfile(losvec_file, dtype=np.float32).reshape(nrow, ncol, 3)
    if cfg.date.start is None:
        raise ValueError(
            "Users must provide starting date (date.start) in the configuration file"
        )
    if cfg.date.end is None:
        raise ValueError(
            "Users must provide ending date (date.end) in the configuration file"
        )
    df_stations = _prepare_station_df(
        cfg.date.start, cfg.date.end, cfg.area.bbox, rsc, mask, losvec
    )
    df_stations = df_stations[
        ~np.isclose(df_stations["gps_los_velocity"], gps.INVALID_VALUE)
    ]
    return df_stations


def write_reference_point(
    ref_lat: float, ref_lon: float, config: Path | str = "config.yaml"
):
    """
    Write the latitude and longitude coordinates of a reference point to the
    configuration file.

    Parameters
    ----------
    ref_lat: float
        Latitude of the reference point
    ref_lon: float
        Longitude of the reference point
    config: Path | str
        Path to the configuration file
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.sort_keys = False
    yaml.indent(mapping=4, sequence=4, offset=2)

    with open(config, "r") as f:
        cfg_dict = yaml.load(f)
        cfg_dict["timeseries"]["parameters"]["ref_lat"] = ref_lat
        cfg_dict["timeseries"]["parameters"]["ref_lon"] = ref_lon
    temp_config = str(config) + ".temp"
    try:
        with open(temp_config, "w") as f:
            yaml.dump(cfg_dict, f)
    except Exception as e:
        logger.error(e)
        return
    shutil.move(temp_config, config)
    time_series_path = Path(cfg_dict["io"]["time_series_path"])
    if not time_series_path.exists():
        time_series_path.mkdir(exist_ok=True, parents=True)
    with open(time_series_path / "reference_point.txt", "w") as f:
        f.write(f"Reference point: {ref_lat}, {ref_lon}")


def select_reference_station(
    config: Path | str = "config.yaml", verbose: bool = False
) -> bool:
    """
    Select a GPS station within the region of interest as a reference point, and write
    it to the input configuration file.

    Parameters
    ----------
    config: Path | str
        Configuration file
    verbose: bool
        If True, setting logging level to DEBUG

    Returns
    -------
    bool
        True if a reference GPS station was selected. False if there is no suitable GPS
        station was found.
    """
    if verbose:
        set_logging_level(logger, "DEBUG")
    from s1proc.time_series import _sbas_block, build_design_matrix_linear

    # create a DataFrame of GPS stations
    cfg = load_config(config)
    df_stations_out_file = Path(cfg.io.proc_path) / "gps_stations.csv"
    if not df_stations_out_file.exists():
        df_stations = prepare_station_df(cfg)
        df_stations.to_csv(df_stations_out_file, index_label="name")
    else:
        df_stations = pd.read_csv(df_stations_out_file, index_col="name")
    if len(df_stations) == 0:
        return False

    # Extract unwrapped phase stacks at GPS stations
    rsc = GeoCoordinates(cfg.io.multilook_rsc_file)
    unw_files = get_files(cfg.io.unw_path, "unw")
    unw_list = IfgList(unw_files)
    gps_patch_file = Path(cfg.io.proc_path) / "gps.zarr"
    if not gps_patch_file.exists():
        _extract_gps_patches(unw_files, df_stations, rsc, output_file=gps_patch_file)
    gps_patch = zarr.open(gps_patch_file, "r")
    gps_phase = np.nanmean(gps_patch, axis=(2, 3))  # (ngps, nifg)
    gps_phase = np.transpose(gps_phase)  # (nifg, ngps)
    gps_phase = gps_phase[:, :, np.newaxis]  # (nifg, ngps, 1)
    stations = gps_patch.attrs["station_names"]

    B = unw_list.int_velocity_matrix()
    G = build_design_matrix_linear(unw_list)
    los_velocity_err = np.full(len(df_stations), np.nan)
    good_gps_stations = np.zeros(len(df_stations), dtype=int)
    idx = 0
    for station, row in df_stations.iterrows():
        if station in stations:
            station_idx = stations.index(station)
        else:
            logger.warning(f"Phase patch was not found for station {station}.")
            idx += 1
            continue
        ref_phase = np.nanmean(gps_patch[station_idx, :, :, :], axis=(1, 2))
        v_est = -_sbas_block(
            gps_phase,
            G=G,
            B=B,
            ref_phase=ref_phase,
            mad_scalar=cfg.timeseries.parameters.mad_scalar,
            solver_type="linear",
            output_dim="2d",
        )
        v_est = v_est.flatten() * 100  # m/yr -> cm/yr
        v_est = v_est + row["gps_los_velocity"]
        v_gps = df_stations["gps_los_velocity"].to_numpy()
        los_velocity_err[idx] = np.median(np.abs(v_est - v_gps))
        good_gps_stations[idx] = np.sum(np.abs(v_est - v_gps) < 0.2)
        # if good_gps_stations[idx] > 80:
        #    print(station)
        #    from matplotlib import pyplot as plt

        #    plt.plot(v_gps, v_est, "o")
        #    plt.plot([-0.5, 2], [-0.5, 2], "k-")
        #    plt.plot([-0.5, 2], [-0.3, 2.2], "r--")
        #    plt.plot([-0.5, 2], [-0.7, 1.8], "r--")
        #    plt.xlim([-0.5, 2])
        #    plt.ylim([-0.5, 2])
        #    plt.title(station)
        #    plt.show()
        #    plt.close()
        logger.debug(
            f"Median LOS velocity error estimated using {station} as reference: "
            + f"{los_velocity_err[idx]} cm/yr"
        )
        idx += 1

    df_stations["ref_los_vel_err"] = los_velocity_err
    df_stations["good_gps_stations"] = good_gps_stations
    temp = np.copy(los_velocity_err)
    temp[np.isnan(temp)] = 1e10
    min_idx = np.argmin(temp)
    del temp
    ref_station = df_stations.index[min_idx]
    ref_lat = df_stations["lat"][ref_station]
    ref_lon = df_stations["lon"][ref_station]
    logger.info(
        f"Selected reference station: {ref_station} (lat:{ref_lat}, lon:{ref_lon})."
    )
    logger.info(
        f"Median absolute LOS velocity error: {np.nanmin(los_velocity_err)} cm/yr."
    )
    df_stations.to_csv(df_stations_out_file, index_label="name")
    logger.info(f"GPS station list is saved to {df_stations_out_file}")
    write_reference_point(float(ref_lat), float(ref_lon), config=config)
    return True


def local_variation(v: np.ndarray, nwin: int = 11):
    from scipy.ndimage import convolve

    mask = np.isnan(v)
    kernel = np.ones((nwin, nwin), dtype=np.float32) / nwin**2
    v[mask] = 0
    v_smooth = convolve(v, kernel, mode="constant")
    v_residue = np.abs(v - v_smooth)
    v_residue[mask] = np.nan
    return v_residue


def select_reference_point_from_image(config: Path | str, verbose: bool):
    """
    Select a reference point, and write it to the input configuration file.

    Parameters
    ----------
    config: Path | str
        Configuration file
    verbose: bool
        If True, setting logging level to DEBUG
    """
    from s1proc.time_series import _sequential_time_series_2d

    if verbose:
        set_logging_level(logger, "DEBUG")

    # load the configuration file
    cfg = load_config(config)
    # temporary deformation rate is saved to proc_path
    proc_path = Path(cfg.io.proc_path)
    proc_path.mkdir(exist_ok=True, parents=True)
    out_path = Path(cfg.io.proc_path) / "stacking_reference.zarr"
    # Load unwrapped interferograms, first try corrected ones then uncorrected ones
    unw_files = get_files(cfg.io.unw_corr_path, "unw")
    if len(unw_files) == 0:
        logger.debug(f"Cannot find any unwrapped images in {cfg.io.unw_corr_path}")
        unw_files = get_files(cfg.io.unw_path, "unw")
        if len(unw_files) == 0:
            raise RuntimeError(f"Cannot find any unwrapped images in {cfg.io.unw_path}")
    # parse nrow and ncol
    rsc = GeoCoordinates(cfg.io.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    mask = np.fromfile(cfg.io.mask_file, dtype=bool).reshape(nrow, ncol)

    # estimate a coarse deformation rate map using the stacking method
    unw_list = IfgList(unw_files)
    tempbl = unw_list.df["tempbl"].to_numpy()
    _sequential_time_series_2d(
        unw_files, mask, out_path, nrow, ncol, temp_bl=tempbl, method="stack"
    )
    z = zarr.open(out_path, "r")
    deformation_rate = z[:, :]
    deformation_variation = local_variation(deformation_rate, nwin=3)
    deformation_variation += local_variation(deformation_rate, nwin=11)
    deformation_variation += local_variation(deformation_rate, nwin=51)
    threshold = np.percentile(
        deformation_variation[~np.isnan(deformation_variation)], 5
    )
    candidate_pixels = (deformation_variation < threshold) & mask
    logger.debug(
        "Threshold of deformation variation used for candidate reference point "
        + f"selection : {threshold} cm/yr."
    )
    rank_deformation = np.argsort(deformation_variation[candidate_pixels])

    # estimate average InSAR phase coherence
    cc_files = get_files(cfg.io.ifg_path, "cc")
    tempbl_indices = np.argsort(np.argsort(tempbl))
    n_cc_imgs = int(min(100, unw_list.nifg * 0.3))
    sum_coherence = np.zeros((nrow, ncol), dtype=np.float32)
    for i in range(n_cc_imgs):
        c = readc(cc_files[tempbl_indices[i]], ncol).imag
        sum_coherence += c
    sum_coherence /= n_cc_imgs
    sum_coherence[~mask] = 0
    candidate_coherence = sum_coherence[candidate_pixels]
    rank_coherence = np.argsort(-np.argsort(candidate_coherence))

    # calculate distance to the center of images
    rr, cc = np.where(candidate_pixels)
    candidate_dist = (rr - nrow / 2) ** 2 + (cc - ncol / 2) ** 2
    rank_dist = np.argsort(np.argsort(candidate_dist))
    rank_sum = rank_deformation + rank_coherence + rank_dist
    candidate_idx = np.argmin(rank_sum)
    ref_lat, ref_lon = rsc.xy2ll(rr[candidate_idx], cc[candidate_idx])
    logger.info(f"Selected reference point: ({ref_lon},{ref_lat})")
    write_reference_point(float(ref_lat), float(ref_lon), config=config)

    return


def select_reference_point(
    config: Path | str = "config.yaml",
    from_gps: bool = True,
    overwrite: bool = False,
    verbose: bool = False,
):
    """
    Select a reference point

    Paramters
    ---------
    config: Path | str
        Path to the configuration file
    from_gps: bool
        Select a GPS station as reference point
    overwrite: bool
        Overwrite existing reference point in the configuration file
    verbose: bool
        If True, set logging level to DEBUG
    """
    cfg = load_config(config)
    ref_lat = cfg.timeseries.parameters.ref_lat
    ref_lon = cfg.timeseries.parameters.ref_lon
    if ref_lat is not None and ref_lon is not None and not overwrite:
        logger.warning(
            "Reference point exists in the configuration file, set overwrite"
            + " to True to reselect one."
        )
        return
    if from_gps:
        if select_reference_station(config, verbose):
            return
    select_reference_point_from_image(config, verbose)
