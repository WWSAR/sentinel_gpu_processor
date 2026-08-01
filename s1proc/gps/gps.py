import datetime
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
import pandas as pd
import requests
from matplotlib import pyplot as plt
from numpy.typing import NDArray

from s1proc import get_cache_dir
from s1proc._log import logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.gps.midas import midas_velocity

# URL for the full NGL station holdings list (lat, lon, elevation for all stations)
GPS_STATION_LIST_URL = "https://geodesy.unr.edu/NGLStationPages/DataHoldings.txt"
GPS_STATION_LIST_FILE = "DataHoldings.txt"
# URLL for ascii file of 24-hour final GPS solutions in east-north-vertical
GPS_BASE_URL = (
    "https://geodesy.unr.edu/gps_timeseries/IGS20/tenv3/{plate}/{station}.{plate}.tenv3"
)
GPS_STATION_URL = "https://geodesy.unr.edu/NGLStationPages/stations/{station}.sta"
GPS_DIR = Path(get_cache_dir()) / "gps"
GPS_DIR.mkdir(exist_ok=True, parents=True)

# Invalid_value assigned to defromation velocity or displacement when the deformation
# data over a given time period are not availabe
INVALID_VALUE = -999.0


# code written by  Scott
def download_station_data(station_name: str, filename: str | Path | None = None):
    station_name = station_name.upper()
    plate = get_station_plate(station_name)
    url = GPS_BASE_URL.format(station=station_name, plate=plate)
    response = requests.get(url, timeout=60)
    response.raise_for_status()

    if filename is None:
        stationfile = url.split("/")[-1]
        stationfile = stationfile.split(".")[0] + ".tenv3"
        filename = GPS_DIR / stationfile
    logger.info(f"Save {station_name} to {filename}")

    with open(filename, "w") as f:
        f.write(response.text)


def download_stations_data(
    station_names: Iterable[str],
    max_workers: int = 8,
    skip_existing: bool = True,
) -> list[str]:
    """Download tenv3 time series for multiple GPS stations concurrently.

    Downloads are network-bound, so a thread pool overlaps the HTTP round
    trips instead of fetching stations one by one.

    Parameters
    ----------
    station_names : Iterable[str]
        Names of the stations to download.
    max_workers : int
        Number of concurrent download threads.
    skip_existing : bool
        If True, skip stations whose tenv3 file is already cached in `GPS_DIR`.

    Returns
    -------
    list of str
        Names of the stations whose download failed.
    """

    def _download(station_name: str) -> None:
        filename = GPS_DIR / f"{station_name.upper()}.tenv3"
        if skip_existing and filename.exists():
            return
        download_station_data(station_name, filename)

    failed = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download, name): name for name in station_names}
        for future in as_completed(futures):
            station_name = futures[future]
            try:
                future.result()
            except Exception as e:
                logger.error("Failed to download %s: %s", station_name, e)
                failed.append(station_name)
    if failed:
        logger.warning("Failed to download %d stations: %s", len(failed), failed)
    return failed


def get_station_plate(station_name: str) -> str:
    station_name = station_name.upper()
    url = GPS_STATION_URL.format(station=station_name)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    match = re.search(r"/plates/(?P<plate>[A-Z]{2})", response.text)
    if match is None:
        raise ValueError(f"Cannot determine tectonic plate for station {station_name}")
    return match.group("plate")


def load_station_data(
    station_name: str,
    start_date: str | datetime.date | None = None,
    end_date: str | datetime.date | None = None,
    download_if_missing=True,
    to_cm=True,
) -> pd.DataFrame:
    station_name = station_name.upper()
    gps_data_file = os.path.join(GPS_DIR, "%s.tenv3" % station_name)
    if not os.path.exists(gps_data_file):
        logger.warning("%s does not exist.", gps_data_file)
        if download_if_missing:
            logger.info("Downloading %s", station_name)
            download_station_data(station_name, gps_data_file)
    for i in range(2):
        try:
            df = pd.read_csv(gps_data_file, header=0, sep=r"\s+")
            break
        except Exception as e:
            logger.error(e)
            if i == 0:
                logger.error(
                    f"Failed to load GPS data for {station_name}, "
                    + "trying to redownload data"
                )
                download_station_data(station_name, gps_data_file)
            elif i == 1:
                logger.error(f"Failed to load GPS data for {station_name}, skipping")
                return None
    clean_df = _clean_gps_df(df, start_date, end_date)
    if to_cm:
        clean_df[["east", "north", "up"]] = 100 * clean_df[["east", "north", "up"]]
    return clean_df


def _clean_gps_df(
    df: pd.DataFrame,
    start_date: str | datetime.date | None,
    end_date: str | datetime.date | None,
) -> pd.DataFrame:
    """
    Clean a GPS data record to based on starting and ending dates.
    """
    df["dt"] = pd.to_datetime(df["YYMMMDD"], format="%y%b%d")

    df_ranged = None
    if start_date:
        df_ranged = df[df["dt"] >= pd.Timestamp(start_date)]
    if end_date:
        df_ranged = df_ranged[df_ranged["dt"] <= pd.Timestamp(end_date)]
    if df_ranged is None:
        df_ranged = df
    df_enu = df_ranged[["dt", "__east(m)", "_north(m)", "____up(m)"]]
    df_enu = df_enu.rename(
        mapper=lambda s: s.replace("_", "").replace("(m)", ""), axis="columns"
    )
    df_enu.reset_index(inplace=True, drop=True)
    return df_enu


def load_station_llh(cache_dir: str | Path = GPS_DIR, force_download: bool = False):
    """Load GPS station lat/lon/height information from the NGL station holdings list.

    Downloads the full station list from the NGL server and caches it locally.
    Subsequent calls use the cached copy unless *force_download* is True.

    Parameters
    ----------
    cache_dir : str
        Directory where the cached station list file is stored.
    force_download : bool
        If True, re-download the file even if a cached copy exists.

    Returns
    -------
    pandas.DataFrame
        DataFrame with columns [lat, lon, height], indexed by station name.
    """
    gps_llh_file = os.path.join(cache_dir, GPS_STATION_LIST_FILE)

    if not os.path.exists(gps_llh_file) or force_download:
        logger.info("Downloading GPS station list from %s", GPS_STATION_LIST_URL)
        response = requests.get(GPS_STATION_LIST_URL, timeout=30)
        response.raise_for_status()
        os.makedirs(cache_dir, exist_ok=True)
        with open(gps_llh_file, "w") as f:
            f.write(response.text)
        logger.info("Saved GPS station list to %s", gps_llh_file)

    names, lats, lons, heights = [], [], [], []
    with open(gps_llh_file) as f:
        next(f)  # skip header
        for line in f:
            parts = line.split()
            if len(parts) < 4:
                continue
            names.append(parts[0])
            lats.append(float(parts[1]))
            lons.append(float(parts[2]))
            heights.append(float(parts[3]))

    df = pd.DataFrame({"lat": lats, "lon": lons, "height": heights}, index=names)
    df.index.name = "name"
    df.loc[df["lon"] > 180, "lon"] -= 360
    return df


def filter_gps_stations(
    df_stations: pd.DataFrame,
    start_date: str | datetime.date | None = None,
    end_date: str | datetime.date | None = None,
    min_span_days: float = 731.0,
    min_num_samples: int = 100,
    max_gap_days: float | None = None,
) -> pd.DataFrame:
    """Filter GPS stations on record span, sample count, and largest data gap.

    A station is kept only if all three criteria hold for its tenv3 record:
    the total time span exceeds `min_span_days`, the number of daily
    solutions exceeds `min_num_samples`, and the longest interval between
    consecutive solutions is shorter than `max_gap_days`. Stations whose
    data cannot be loaded are dropped.

    Parameters
    ----------
    df_stations : pandas.DataFrame
        Stations indexed by name, e.g. the output of
        `load_gps_stations_in_bbox`.
    start_date: str | datetime.date | None
        Starting date of the study period
    end_date: str | datetime.date | None
        Ending date of the study period
    min_span_days : float
        Minimum total span of the record in days (exclusive).
    min_num_samples : int
        Minimum number of daily solutions in the record (exclusive).
    max_gap_days : float
        Maximum tolerated interval without any solution in days (exclusive).

    Returns
    -------
    pandas.DataFrame
        The rows of `df_stations` that pass all three criteria.
    """
    keep = []
    if max_gap_days is None:
        max_gap_days = 1e10
    for station_name in df_stations.index:
        try:
            gpsdf = load_station_data(
                station_name, start_date=start_date, end_date=end_date
            )
        except Exception as e:
            logger.error("Dropping %s: cannot load its data (%s)", station_name, e)
            continue
        if gpsdf is None or len(gpsdf) < 2:
            continue
        span_days = (gpsdf["dt"].iloc[-1] - gpsdf["dt"].iloc[0]).days
        max_gap = gpsdf["dt"].diff().max().days
        if (
            span_days > min_span_days
            and len(gpsdf) > min_num_samples
            and max_gap < max_gap_days
        ):
            keep.append(station_name)
    logger.info(
        "Kept %d of %d GPS stations after quality filtering",
        len(keep),
        len(df_stations),
    )
    return df_stations.loc[keep]


def load_gps_stations_in_bbox(
    bbox: Sequence[float],
    cache_dir: str | Path = GPS_DIR,
    start_date: str | datetime.date | None = None,
    end_date: str | datetime.date | None = None,
    min_span_days: float = 731.0,
    min_num_samples: int = 100,
    max_gap_days: float | None = None,
    max_workers: int = 8,
) -> pd.DataFrame:
    """Load, download, and quality-filter GPS stations inside a bounding box.

    Stations inside `bbox` are read from the NGL holdings list, their tenv3
    time series are downloaded concurrently, and stations with short, sparse,
    or gappy records are removed via `filter_gps_stations`.

    Parameters
    ----------
    bbox : Sequence[float]
        Bounding box as [west, south, east, north] in decimal degrees.
    cache_dir : str or Path
        Directory where the cached station list file is stored.
    start_date: str | datetime.date | None
        Starting date of the study period
    end_date: str | datetime.date | None
        Ending date of the study period
    min_span_days : float
        Keep a station only if its record spans more than this many days.
    min_num_samples : int
        Keep a station only if it has more than this many daily solutions.
    max_gap_days : float
        Keep a station only if the longest interval between consecutive
        solutions is shorter than this many days.
    max_workers : int
        Number of threads used to download the station time series.

    Returns
    -------
    pandas.DataFrame
        DataFrame with columns [lat, lon, height], indexed by station name,
        containing the stations within the bounding box that pass the
        quality filter.
    """
    west, south, east, north = bbox
    df_all = load_station_llh(cache_dir=cache_dir)
    df_box = df_all[
        (df_all["lon"] > west)
        & (df_all["lon"] < east)
        & (df_all["lat"] > south)
        & (df_all["lat"] < north)
    ]
    logger.info("Found %d GPS stations in bbox %s", len(df_box), list(bbox))
    download_stations_data(df_box.index, max_workers=max_workers)
    return filter_gps_stations(
        df_box,
        start_date=start_date,
        end_date=end_date,
        min_span_days=min_span_days,
        min_num_samples=min_num_samples,
        max_gap_days=max_gap_days,
    )


def filter_by_mask(
    df_stations: pd.DataFrame, mask: NDArray[np.bool_], rsc: GeoCoordinates
) -> pd.DataFrame:
    """
    Filter GPS stations by a mask. GPS stations at invalid radar pixels are removed.

    Paramters
    ---------
    df_stations: pandas.DataFrame
        A list of input GPS stations
    mask: np.ndarray
        A boolean mask showing valid radar pixels. True: valid, False: invalid
    rsc: GeoCoordinates
        A GeoCoordinates object to convert latitude/longitude to row/column indices

    Returns
    -------
    pandas.DataFrame
        Filtered GPS list
    """
    lat = df_stations["lat"].to_numpy()
    lon = df_stations["lon"].to_numpy()
    rr, cc = rsc.ll2xy(lat, lon)
    keep = mask[rr, cc]
    logger.info(
        "Kept %d of %d GPS stations after mask filtering",
        np.sum(keep),
        len(df_stations),
    )
    return df_stations.loc[keep]


def append_los_vector(
    df_stations: pd.DataFrame, los: NDArray[np.float32], rsc: GeoCoordinates
) -> pd.DataFrame:
    """
    Append Line-Of-Sight vecotrs to a GPS station DataFrame, this will create three new
    columns: ``los_e``, ``los_n``, ``los_u``, corresponding to the east, north, and up
    components of LOS vectors.

    Paramters
    ---------
    df_stations: pandas.DataFrame
        A list of input GPS stations
    los: np.ndarray
        Line of sight vectors at all radar pixels (nrow, ncol, 3) in ECEF coordinates
    rsc: GeoCoordinates
        A GeoCoordinates object to convert latitude/longitude to row/column indices

    Returns
    -------
    pandas.DataFrame
        Augmented GPS list
    """
    from s1proc.geometry import xyz2enu

    gps_los = np.zeros((len(df_stations), 3), dtype=np.float32)
    i = 0
    for _, row in df_stations.iterrows():
        lat = row["lat"]
        lon = row["lon"]
        r, c = rsc.ll2xy(lat, lon)
        gps_los[i, :] = xyz2enu(los[r, c, :], lat, lon)
        i += 1
    df_stations["los_e"] = gps_los[:, 0]
    df_stations["los_n"] = gps_los[:, 1]
    df_stations["los_u"] = gps_los[:, 2]
    return df_stations


def plot_gps_station(
    station_name: str,
    start_date: str | datetime.date | None = None,
    end_date: str | datetime.date | None = None,
    download_if_missing: bool = True,
    to_cm: bool = True,
    directions: Sequence[Literal["east", "north", "up"]] = ["east", "north", "up"],
    show: bool = True,
):
    gpsdf = load_station_data(
        station_name, start_date, end_date, download_if_missing, to_cm
    )
    if gpsdf is None:
        return
    if len(gpsdf) < 10:
        return
    if start_date is None:
        start_date = gpsdf["dt"][0].date()
    if isinstance(start_date, str):
        start_date = datetime.date.fromisoformat(start_date)
    dt = np.array([
        (gpsdf["dt"][k].date() - start_date).days for k in range(len(gpsdf))
    ])
    fig, axs = plt.subplots(
        ncols=1, nrows=len(directions), figsize=(12, 3 * len(directions))
    )
    if len(directions) > 1:
        plt.subplots_adjust(hspace=0.4)
    for i in range(len(directions)):
        d = directions[i]
        y = gpsdf[d].to_numpy()
        v = np.polyfit(dt, y, 1)
        y_fit = v[0] * dt + v[1]
        axs[i].plot(gpsdf["dt"].to_numpy(), y, "r.", markersize=5)
        axs[i].plot(gpsdf["dt"].to_numpy(), y_fit, "b", linewidth=2)
        try:
            v_midas = midas_velocity(gpsdf["dt"], y) / 365.25
            intercept = np.mean(y - v_midas * dt)
            y_fit_midas = intercept + v_midas * dt
            axs[i].plot(gpsdf["dt"].to_numpy(), y_fit_midas, "g", linewidth=2)
        except Exception as e:
            print(e)
            pass
        axs[i].set_title("{} {} {:0.3f} cm/yr".format(station_name, d, v[0] * 365))
        axs[i].set_xlabel("Date")
        axs[i].set_ylabel("Deformation [cm]")
    if show:
        plt.show()
    return fig


def project_los(
    gpsdf: pd.DataFrame,
    los_vec: NDArray[np.float64] | Sequence[float],
    window_days: float = 30,
    mad_threshold: float = 3.0,
) -> tuple[pd.DataFrame, float]:
    """Project 3D GPS displacement onto the InSAR line-of-sight direction.

    Projects the east, north, up displacement components onto *los_vec*
    via a dot product, then estimates the station velocity using the
    MIDAS algorithm.

    Parameters
    ----------
    gpsdf : pandas.DataFrame
        Single-station GPS time series from :func:`load_station_data`,
        with columns ``["dt", "east", "north", "up"]``.  Displacements
        are expected in cm.
    los_vec : array-like of float, shape (3,)
        Line-of-sight unit vector in ENU order
        ``[east, north, up]``.
    window_days : float
        Half-width of the MIDAS 1-year acceptance window in days.
    mad_threshold : float
        MAD-sigma multiplier for MIDAS outlier rejection.

    Returns
    -------
    gpsdf_los : pandas.DataFrame
        A copy of *gpsdf* with an additional ``"los"`` column holding
        the LOS-projected displacement in cm.
    velocity : float
        MIDAS-estimated LOS velocity in cm / yr.  Positive indicates
        motion away from the satellite.  ``INVALID_VALUE`` if the
        estimation fails.

    Raises
    ------
    ValueError
        If *los_vec* does not have shape ``(3,)``.
    """
    los_vec = np.asarray(los_vec, dtype=float)
    if los_vec.shape != (3,):
        raise ValueError("los_vec must have shape (3,), got %s" % str(los_vec.shape))

    los = np.dot(gpsdf[["east", "north", "up"]].to_numpy(), los_vec)

    result = gpsdf.copy()
    result["los"] = los

    try:
        velocity = midas_velocity(
            result["dt"], los, window_days=window_days, mad_threshold=mad_threshold
        )
    except Exception:
        logger.warning("MIDAS velocity estimation failed, returning INVALID_VALUE")
        velocity = float(INVALID_VALUE)

    return result, velocity


def calc_los_velocity(
    df_stations: pd.DataFrame,
    start_date: str | datetime.date | None = None,
    end_date: str | datetime.date | None = None,
    save_time_series: bool = False,
    out_path: Path | str = "gps_time_series",
):
    """
    Calculate GPS-derived Line-Of-Sight velocity for all GPS stations

    Parameters
    ----------
    df_stations: pandas.DataFrame
        List of GPS stations (LOS unit vectors must be added using
        ``append_los_vector`` for LOS velocity calculation.
    start_date: str | datetime.date | None
        Starting date of the study period
    end_date: str | datetime.date | None
        Ending date of the study period
    save_time_series: bool
        Save LOS-projected time series as csv files
    out_path: Path|str
        The directory to put saved time series data
    """
    v_list = []
    if save_time_series:
        Path(out_path).mkdir(exist_ok=True, parents=True)
    for station, row in df_stations.iterrows():
        gpsdf = load_station_data(station, start_date=start_date, end_date=end_date)
        gpsdf, v = project_los(gpsdf, row[["los_e", "los_n", "los_u"]].to_numpy())
        v_list.append(v)
        if save_time_series:
            gpsdf.to_csv(Path(out_path) / (station + ".csv"))
    df_stations["gps_los_velocity"] = v_list
    return df_stations


def save_gps_stations(gps_list):
    for index, _ in gps_list.iterrows():
        try:
            fig = plot_gps_station(
                index, directions=["east", "north", "up"], show=False
            )
            fig.savefig(index + ".png", bbox_inches="tight")
            plt.close(fig)
        except Exception as _:
            print("{} cannot be plotted".format(index))
            pass
