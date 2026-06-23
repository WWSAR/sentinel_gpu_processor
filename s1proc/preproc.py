#!/usr/bin/env python3
"""
Preprocessing module for the Sentinel-1 InSAR processing pipeline.

Handles ASF data query, path/frame filtering, metalink generation,
study area bounding-box computation, and COP DEM download via sardem.
"""

import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Sequence
from urllib.parse import urljoin

import numpy as np
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from s1proc import get_cache_dir
from s1proc._config import load_config
from s1proc._log import setup_logger
from s1proc.query import _geojson_to_metalink
from s1proc.sentinel_downloader import download_metalink
from s1proc.utils import gtiff2roipac

logger = setup_logger(name=__name__, level="INFO")


def download_vrt_file():
    """
    Recursively download COP vrt files

    """

    def create_session():
        session = requests.Session()

        retries = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )

        adapter = HTTPAdapter(max_retries=retries)
        session.mount("https://", adapter)

        return session

    def download_file(url, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        local_path = os.path.join(out_dir, os.path.basename(url))

        if os.path.exists(local_path):
            return local_path

        r = session.get(url, timeout=20)
        r.raise_for_status()

        with open(local_path, "wb") as f:
            f.write(r.content)

        return local_path

    def parse_vrt(file_path):
        tree = ET.parse(file_path)
        root = tree.getroot()

        files = []
        for elem in root.iter():
            if elem.tag == "SourceFilename":
                files.append(elem.text)

        return files

    def recursive_download(url, out_dir):
        if url in visited:
            return
        visited.add(url)

        local_file = download_file(url, out_dir)

        # Only recurse if it's a VRT
        if not url.endswith(".vrt"):
            return

        refs = parse_vrt(local_file)

        for ref in refs:
            if ref is None or "vsicurl" in ref:
                continue
            # Handle relative paths
            full_url = urljoin(url, ref)

            recursive_download(full_url, out_dir)

    session = create_session()
    visited = set()
    root_url = "https://raw.githubusercontent.com/scottstanie/sardem/master/sardem/data/cop_global.vrt"
    logger.info(f"Downloading vrt files from {root_url}, it may take a while.")
    cache_dir = get_cache_dir()
    recursive_download(root_url, cache_dir)
    logger.info("cop_global.vrt is successfully downloaded.")


def _compute_frames_bbox(features):
    """
    Compute the bounding box enclosing all frame footprints.

    Parameters
    ----------
    features : list
        List of GeoJSON feature dicts, each with a ``geometry`` field.

    Returns
    -------
    bbox : tuple of (west, south, east, north) or None
        Returns ``None`` if *features* is empty or no valid coordinates
        are found.
    """
    if not features:
        return None

    all_lons = []
    all_lats = []

    for feat in features:
        geom = feat.get("geometry", {})
        geom_type = geom.get("type")
        coords = geom.get("coordinates", [])

        if geom_type == "Polygon":
            rings = coords
        elif geom_type == "MultiPolygon":
            rings = [ring for poly in coords for ring in poly]
        else:
            continue

        for ring in rings:
            for lon, lat in ring:
                all_lons.append(lon)
                all_lats.append(lat)

    if not all_lons:
        return None

    return (min(all_lons), min(all_lats), max(all_lons), max(all_lats))


def _intersect_bbox(bbox1, bbox2):
    """
    Intersect two ``(west, south, east, north)`` bounding boxes.

    Parameters
    ----------
    bbox1, bbox2 : tuple of float
        Bounding boxes as ``(west, south, east, north)``.

    Returns
    -------
    tuple of float or None
        Intersection bounding box, or ``None`` if the boxes do not
        overlap.
    """
    west = max(bbox1[0], bbox2[0])
    south = max(bbox1[1], bbox2[1])
    east = min(bbox1[2], bbox2[2])
    north = min(bbox1[3], bbox2[3])

    if west < east and south < north:
        return (west, south, east, north)
    return None


def _download_dem(
    output_name,
    bbox,
    vrt_filename,
    xrate,
    yrate,
    output_format,
    output_type,
):
    """
    Download COP DEM via *sardem* in ROI_PAC / int16 format.

    Parameters
    ----------
    bbox : tuple of float
        ``(west, south, east, north)`` bounding box in degrees.
    output_dir : str or Path
        Directory that will receive ``elevation.dem`` and its
        accompanying ``.dem.rsc`` file.
    xrate : int
        Upsampling factor in the x (longitude) direction.
    yrate : int
        Upsampling factor in the y (latitude) direction.
    """
    from osgeo import gdal
    from sardem import conversions, utils
    from sardem.constants import DEFAULT_RES
    from sardem.cop_dem import _gdal_cmd_from_options

    code = conversions.EPSG_CODES["egm08"]
    s_srs = "epsg:4326+{}".format(code)
    t_srs = "epsg:4326"
    xres = DEFAULT_RES / xrate
    yres = DEFAULT_RES / yrate
    resamp = "bilinear" if (xrate > 1 or yrate > 1) else "nearest"

    option_dict = dict(
        format=output_format,
        outputBounds=utils.align_bounds_to_pixel_grid(bbox),
        dstSRS=t_srs,
        srcSRS=s_srs,
        xRes=xres,
        yRes=yres,
        outputType=gdal.GetDataTypeByName(output_type.title()),
        resampleAlg=resamp,
        multithread=True,
        warpMemoryLimit=5000,
        warpOptions=["NUM_THREADS=4"],
    )
    # Preserve ocean (value=0) as nodata during geoid-to-ellipsoid conversion

    logger.info("Creating {}".format(output_name))
    logger.info("Fetching remote tiles...")
    cmd = _gdal_cmd_from_options(vrt_filename, output_name, option_dict)
    logger.info("Running GDAL command:")
    logger.info(cmd)
    subprocess.check_call(cmd, shell=True)


def _filter_by_frame(s1_data: dict, frame_list: Sequence[int]) -> dict:
    """
    Filter Sentinel-1 data based on frame numbers

    Parameters
    ----------
    s1_data: dict
        Sentinel-1 data dictionary read from a geojson file
    frame_list: Sequence[int]
        Target path number
    """
    all_features = s1_data.get("features", [])
    filtered = []
    for feat in all_features:
        props = feat.get("properties", {})
        feat_frame = props.get("frameNumber")
        if int(feat_frame) in frame_list:
            filtered.append(feat)
    if len(filtered) == 0:
        logger.error(f"No Sentinel-1 scens found for frames: {frame_list}")
        return None
    s1_data["features"] = filtered
    return s1_data


def preprocess(config_file="config.yaml"):
    """
    Run the full preprocessing pipeline.

    Parameters
    ----------
    config_file : str or Path
        Path to the YAML configuration file.

    Notes
    -----
    Reads *config_file* and:

    1. Filters the GeoJSON (``roi.geojson``) to retain only scenes
       whose ``frameNumber`` belongs to ``area.frame_list``.
    2. Generates a metalink file (``roi.metalink``) from the filtered
       GeoJSON.
    3. Computes the study-area bounds as the intersection of the
       bounding box of all retained frame footprints with
       ``area.bbox``.
    4. Downloads the COP DEM as a GeoTIFF (int16, upsampled 6x/3x)
       and converts it to ROI_PAC format.
    5. Downloads Sentinel-1 SLC zip files via ``aria2c`` using the
       metalink, saving to ``io.slc_path``.
    6. Downloads precise orbit (EOF) files via *sentineleof*, scanning
       ``io.slc_path`` for Sentinel-1 products and saving orbits to
       ``io.eof_path``.
    """
    config_file = Path(config_file)
    root_dir = config_file.parent
    cfg = load_config(config_file)

    bbox = cfg.area.bbox
    frame_list = cfg.area.frame_list
    if bbox is None:
        logger.warning("area.bbox must be set to download DEM and radar data.")
    else:
        # Filter GeoJSON by frame list
        geojson_file = root_dir / "roi.geojson"
        if not geojson_file.is_file():
            raise FileNotFoundError(f"Cannot find {geojson_file}")

        with open(geojson_file, "r", encoding="utf-8") as f:
            s1_data = json.load(f)

        if frame_list is None or len(frame_list) == 0:
            logger.warning(
                "frame list is empty or not set, frame filter " + "will not be applied"
            )
        else:
            s1_data = _filter_by_frame(s1_data, frame_list)
        metalink_file = root_dir / "roi.metalink"
        _geojson_to_metalink(s1_data, metalink_file)
        logger.info(f"Write filtered Sentinel-1 metalinks to {metalink_file}")

        # Compute study area bounds
        frames_bbox = _compute_frames_bbox(s1_data["features"])
        if frames_bbox is None:
            raise RuntimeError(
                "No frame footprints found — cannot determine study area"
            )

        logger.info("Frames bounding box: %s", frames_bbox)

        bbox_tuple = (bbox[0], bbox[1], bbox[2], bbox[3])
        study_bbox = _intersect_bbox(frames_bbox, bbox_tuple)
        if study_bbox is None:
            raise RuntimeError(
                f"Frame footprints {frames_bbox} do not overlap with area.bbox "
                + f" {bbox_tuple}"
            )
        logger.info("Study area (intersection): %s", study_bbox)

        # Download COP DEM
        # check if cop_global.vrt is cahced
        if cfg.proc.download_dem:
            cache_dir = get_cache_dir()
            vrt_file = cache_dir / "cop_global.vrt"
            # download the vrt file if it does not exist
            if not vrt_file.exists():
                download_vrt_file()
            tif_file = root_dir / "roi_dem.tif"
            if tif_file.exists():
                os.remove(tif_file)
            _download_dem(tif_file, bbox, vrt_file, 6, 3, "GTiff", "int16")
            gtiff2roipac(tif_file, cfg.io.dem_file, cfg.io.rsc_file, np.int16)
        else:
            logger.warning(
                "DEM will not be downloaded automatically because "
                + "proc.download_dem is set to False."
            )

        # Download SLC data
        if cfg.proc.download_data:
            data_path = Path(cfg.io.data_path)
            data_path.mkdir(parents=True, exist_ok=True)
            download_metalink(str(metalink_file), output_dir=str(data_path))
        else:
            logger.warning(
                "Sentinel-1 data will not be downloaded automatcially"
                + " because proc.download_data is set to False."
            )

    # Download precise orbit files
    if cfg.proc.download_eof:
        from eof import download as eof_download

        eof_path = Path(cfg.io.eof_path)
        eof_path.mkdir(parents=True, exist_ok=True)
        eof_download.main(search_path=str(data_path), save_dir=str(eof_path))
    else:
        logger.warning(
            "Precise orbit data will not be downloaded automatically"
            + " because proc.download_eof is set to False."
        )
