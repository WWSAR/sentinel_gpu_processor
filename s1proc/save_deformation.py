from pathlib import Path
from typing import Literal

import numpy as np
import zarr
from matplotlib import pyplot as plt
from mpl_toolkits.axes_grid1 import make_axes_locatable

from s1proc._log import logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.utils import save_as_geotiff


def recalibrate(
    deformation_path: Path | str, rsc_file: Path | str, gps_csv_file: Path | str
) -> float:
    """
    Calculate a constant long-term surface deformation rate offset between GPS- and
    InSAR-derived deformation results. Write the offset to the input zarr file.

    Parameters
    ----------
    deformation_path: Path | str
        The zarray file of deformation results.
    rsc_file: Path | str
        An rsc file for lat/lon -> row/col conversion.
    gps_csv_file: Path | str
        A csv file with LOS deformation rate derived from GPS data
        see ``prepare_gps_df`` in reference.py for more details.

    Returns
    -------
    offset: float
        A long-term deformation rate offset between GPS and InSAR results
        (v_gps - v_insar).
    """
    import pandas as pd

    rsc = GeoCoordinates(rsc_file)
    # read GPS DataFrame
    df_stations = pd.read_csv(gps_csv_file, index_col="name")
    z = zarr.open(deformation_path, mode="r+")
    dim = len(z.shape)
    # calculate average LOS deformation rate
    if dim == 2:
        v = -z[:, :]
    elif dim == 3:
        total_disp = -z[-1, :, :]
        total_days = z.attrs["days"][-1]
        v = total_disp / total_days * 365.25
    else:
        raise RuntimeError(
            "Only 2D or 3D deformation data set can be calibrated. The "
            + f"dimension of the input dataset is {dim}."
        )

    # compute offset between InSAR and GPS measurements
    v_gps = []
    v_insar = []
    for _, row in df_stations.iterrows():
        lat = row["lat"]
        lon = row["lon"]
        r, c = rsc.ll2xy(lat, lon)
        v_gps.append(row["gps_los_velocity"])
        v_insar.append(np.nanmean(v[r - 2 : r + 3, c - 2 : c + 3]))
    v_gps = np.array(v_gps)
    v_insar = np.array(v_insar)
    offset = np.nanmedian(v_gps - v_insar)
    v_insar += offset
    df_stations["insar_los_velocity"] = v_insar
    z.attrs["offset"] = offset
    logger.info(
        f"LOS velocity offset between GPS and InSAR measurements: {offset} cm/yr."
    )
    df_stations.to_csv(gps_csv_file)
    return offset


def save_image(
    img: np.ndarray,
    out_file: Path | str,
    title: str | None = None,
    cbar_label: str | None = "Deformation (mm)",
    vmin: float | None = None,
    vmax: float | None = None,
    cmap: str = "RdYlBu_r",
):
    """Save a deformation map as an image file with an aligned colorbar."""
    out_file = Path(out_file)

    ext = out_file.suffix.lower().lstrip(".")
    if ext in ["png", "pdf", "jpg", "jpeg"]:
        fmt = ext
    else:
        fmt = "pdf"
        out_file = out_file.with_suffix(".pdf")

    valid_mask = ~np.isnan(img)
    if not np.any(valid_mask):
        print(f"Warning: {out_file.name} contains only NaNs. Skipping.")
        return

    if vmin is None or vmax is None:
        vmin, vmax = np.percentile(img[valid_mask], (1, 99))
        vmax = max(np.abs(vmin), np.abs(vmax))
        vmin = -vmax

    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(img, vmin=vmin, vmax=vmax, cmap=cmap)

    divider = make_axes_locatable(ax)
    cax = divider.append_axes("right", size="5%", pad=0.1)
    cbar = fig.colorbar(im, cax=cax)

    if cbar_label is not None:
        cbar.set_label(cbar_label, fontsize=10)

    if title is not None:
        ax.set_title(title, fontsize=12)

    ax.set_xticks([])
    ax.set_yticks([])

    plt.savefig(out_file, format=fmt, dpi=300, bbox_inches="tight")
    plt.close(fig)


def _save_deformation(
    deformation_path: Path | str,
    look_angle_file: Path | str | None = None,
    out_prefix: str | None = None,
    out_path: Path | str | None = None,
    format: Literal["png", "jpg", "jpeg", "pdf"] = "pdf",
):
    """
    Save deformation results. The function saves the average Line-of-Sight (LOS)
    deforamtion rate, average vertical deformation rate (assuming negligible
    horizontal motinos), total LOS deformation, and total vertical deformation to png
    and geotiff files.

    Parameters
    ----------
    deformation_path: Path | str
        The zarray of deformation results
    look_angle_file: Path | str | None
        Path to the look angle file. If None, vertical deforamtion results will not be
        saved.
    out_prefix: str | None
        Prefix of the saved images.
    out_path: Path | str
        Directory to save figures.
    format: Literal["png", "pdf"]
        Image format
    """
    out_path = Path(out_path)
    out_path.mkdir(exist_ok=True, parents=True)
    if out_prefix is None:
        out_prefix = ""
    else:
        out_prefix == out_prefix + "_"
    v_los_img_file = out_path / f"{out_prefix}los_deformation_rate.{format}"
    d_los_img_file = out_path / f"{out_prefix}los_displacement.{format}"
    v_vertical_img_file = out_path / f"{out_prefix}vertical_deformation_rate.{format}"
    d_vertical_img_file = out_path / f"{out_prefix}vertical_displacement.{format}"
    v_los_tif_file = out_path / f"{out_prefix}los_deformation_rate.tif"
    d_los_tif_file = out_path / f"{out_prefix}los_displacement.tif"
    v_vertical_tif_file = out_path / f"{out_prefix}vertical_deformation_rate.tif"
    d_vertical_tif_file = out_path / f"{out_prefix}vertical_displacement.tif"

    z = zarr.open(deformation_path, "r")
    days = z.attrs["days"]
    if "offset" in z.attrs:
        offset = z.attrs["offset"]
    else:
        offset = 0
    total_days = days[-1]
    total_years = total_days / 365.25
    dim = len(z.shape)
    # calculate average LOS deformation rate
    if dim == 2:
        v_los = -z[:, :] + offset  # average LOS velocity in cm / yr
        d_los = v_los * total_years
        nrow, ncol = z.shape
    elif dim == 3:
        d_los = -z[-1, :, :] + offset * total_years
        v_los = d_los / total_years
        nrow, ncol = z.shape[1:3]
    else:
        raise RuntimeError(
            "Only 2D or 3D deformation data set can be calibrated. The "
            + f"dimension of the input dataset is {dim}."
        )
    rsc_params = {
        "latmax": z.attrs["latmax"],
        "lonmin": z.attrs["lonmin"],
        "dlat": z.attrs["dlat"],
        "dlon": z.attrs["dlon"],
        "nlat": nrow,
        "nlon": ncol,
    }
    rsc = GeoCoordinates(rscparams=rsc_params)
    save_image(v_los, v_los_img_file, "LOS Deformation Rate", "cm/yr")
    save_image(d_los, d_los_img_file, "LOS Displacement", "cm")
    save_as_geotiff(v_los, rsc, np.float32, shift=True, output_file=v_los_tif_file)
    save_as_geotiff(d_los, rsc, np.float32, shift=True, output_file=d_los_tif_file)

    if look_angle_file is not None:
        look_angle = np.deg2rad(
            np.fromfile(look_angle_file, dtype=np.float32).reshape(nrow, ncol)
        )
        v_vertical = -v_los / np.cos(look_angle)
        d_vertical = -d_los / np.cos(look_angle)
        save_image(
            v_vertical, v_vertical_img_file, "Vertical Deformation Rate", "cm/yr"
        )
        save_image(d_vertical, d_vertical_img_file, "Vertical Displacement", "cm")
        save_as_geotiff(
            v_vertical, rsc, np.float32, shift=True, output_file=v_vertical_tif_file
        )
        save_as_geotiff(
            d_vertical, rsc, np.float32, shift=True, output_file=d_vertical_tif_file
        )


def save_deformation(
    deformation_path: Path | str | None = None,
    out_path: Path | str = "figures",
    recali: bool = True,
    gps_csv_file: Path | str | None = None,
    config: Path | str = "config.yaml",
):
    """
    Wrapper function to save deformation results.

    Parameters
    ----------
    deformation_path: Path | str | None
        The input zarray file of deformation results
    out_path: Path | str | None
        Directory to save figures.
    recali: bool
        Recalibrate deformation map to match with GPS results
    config: Path | str
        Configuration file
    """
    from s1proc._config import load_config

    cfg = load_config(config)
    rsc_file = cfg.io.multilook_rsc_file
    if recali:
        if gps_csv_file is None:
            logger.debug(
                "Input GPS csv file is None, try to use the default path of csv file."
            )
            proc_path = Path(cfg.io.proc_path)
            gps_csv_file = proc_path / "gps_stations.csv"
        if not gps_csv_file.exists():
            logger.warning(
                f"Cannot find the GPS csv file: {gps_csv_file}, "
                + "skipping recalibration."
            )
        else:
            recalibrate(deformation_path, rsc_file, gps_csv_file)
    _save_deformation(
        deformation_path,
        look_angle_file=Path(cfg.io.geometry_path) / "look_angle",
        out_path=out_path,
    )
