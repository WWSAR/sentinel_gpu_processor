import glob
import json
import os
import shutil
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

import numpy as np
import requests
import tqdm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")

METALINK_NS = "http://www.metalinker.org/"
METALINK_VERSION = "3.0"

BASE_URL = "https://api.daac.asf.alaska.edu/services/search/param?"
QUERY_TEMPLATE = (
    BASE_URL
    + "dataset={}&beamMode={}&processingLevel={}"
    + "&start={}T00:00:00Z&end={}T23:59:59Z&output={}"
)


def merge_asf_geojson(temp_dir, output_file):
    """
    Merge ASF GeoJSON scene files and remove duplicates.

    Deduplication is based on ``sceneName`` (preferred) or ``fileID``.
    """

    all_files = glob.glob(temp_dir + "/*.geojson")

    features = []
    seen = set()

    for fpath in all_files:
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)

        # ASF output may be FeatureCollection OR single Feature
        if data.get("type") == "FeatureCollection":
            geom_list = data["features"]
        elif data.get("type") == "Feature":
            geom_list = [data]
        else:
            continue

        for feat in geom_list:
            props = feat.get("properties", {})

            # best unique key
            scene_id = props.get("sceneName") or props.get("fileID")

            if scene_id is None:
                continue

            if scene_id in seen:
                continue

            seen.add(scene_id)
            features.append(feat)

    merged = {"type": "FeatureCollection", "features": features}

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    logger.info("Merged %d geojson files", len(all_files))
    logger.info("Unique scenes: %d", len(features))
    logger.info("Saved to %s", output_file)


def merge_asf_metalink(temp_dir, output_file):
    """
    Merge ASF metalink XML files and remove duplicates.

    Deduplication is based on the ``name`` attribute of each ``<file>``
    element.  The merged output preserves the ``<publisher>`` block from
    the first metalink file that has one.

    Parameters
    ----------
    temp_dir : str
        Directory containing ``*.metalink`` files to merge.
    output_file : str
        Path for the merged metalink output.
    """
    all_files = glob.glob(os.path.join(temp_dir, "*.metalink"))

    seen: set[str] = set()
    file_elements: list[ET.Element] = []
    publisher_el: Optional[ET.Element] = None

    for fpath in all_files:
        try:
            tree = ET.parse(fpath)
            root = tree.getroot()
        except ET.ParseError as exc:
            logger.warning("Skipping unparseable metalink %s: %s", fpath, exc)
            continue

        # Capture the publisher block from the first file that has one
        if publisher_el is None:
            ns = {"ml": METALINK_NS}
            pub = root.find("ml:publisher", ns)
            if pub is None:
                pub = root.find("publisher")
            if pub is not None:
                publisher_el = pub

        # Extract <file> elements from <files>
        files_el = root.find(f"{{{METALINK_NS}}}files")
        if files_el is None:
            files_el = root.find("files")
        if files_el is None:
            continue

        for fe in files_el.findall(f"{{{METALINK_NS}}}file"):
            name = fe.get("name")
            if name is None:
                continue
            if name in seen:
                continue
            seen.add(name)
            file_elements.append(fe)

    # Build merged metalink document
    root = ET.Element("metalink", xmlns=METALINK_NS, version=METALINK_VERSION)
    if publisher_el is not None:
        root.append(publisher_el)
    else:
        pub = ET.SubElement(root, "publisher")
        ET.SubElement(pub, "name").text = "Alaska Satellite Facility"
        ET.SubElement(pub, "url").text = "http://www.asf.alaska.edu/"

    files_el = ET.SubElement(root, "files")
    for fe in file_elements:
        files_el.append(fe)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(output_file, encoding="UTF-8", xml_declaration=True)

    logger.info("Merged %d metalink files", len(all_files))
    logger.info("Unique files: %d", len(file_elements))
    logger.info("Saved to %s", output_file)


def _geojson_to_metalink(data: dict, metalink_path: Path | str):
    """
    Helper function to convert a geojson dictionary to metalink

    Parameters
    ----------
    data: dict
        geojson data
    metalink_path: Path | str
        Destination path for the generated metalink file.
    """
    if data.get("type") != "FeatureCollection":
        logger.warning(
            "GeoJSON is not a FeatureCollection; skipping metalink extraction."
        )
        return

    root = ET.Element("metalink", xmlns=METALINK_NS, version=METALINK_VERSION)
    pub = ET.SubElement(root, "publisher")
    ET.SubElement(pub, "name").text = "Alaska Satellite Facility"
    ET.SubElement(pub, "url").text = "http://www.asf.alaska.edu/"
    files_el = ET.SubElement(root, "files")

    count = 0

    for feat in data.get("features", []):
        props = feat.get("properties", {})
        scene_name = props.get("sceneName") or props.get("fileID")
        if scene_name is None:
            continue

        download_url = props.get("url")
        if download_url is None:
            logger.debug("No download URL for scene %s; skipping.", scene_name)
            continue

        file_name = props.get("fileName") or f"{scene_name}.zip"

        file_el = ET.SubElement(files_el, "file", name=file_name)
        resources_el = ET.SubElement(file_el, "resources")
        ET.SubElement(resources_el, "url", type="http").text = download_url

        md5 = props.get("md5sum")
        if md5:
            verification_el = ET.SubElement(file_el, "verification")
            ET.SubElement(verification_el, "hash", type="md5").text = md5

        file_bytes = props.get("bytes")
        if file_bytes is not None:
            ET.SubElement(file_el, "size").text = str(file_bytes)

        count += 1

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(metalink_path, encoding="UTF-8", xml_declaration=True)

    logger.info("Extracted %d download URLs to %s", count, metalink_path)


def geojson_to_metalink(geojson_path: Path | str, metalink_path: Path | str):
    """
    Extract download metadata from a merged ASF GeoJSON file and write a
    matching metalink XML file.

    The generated metalink mirrors the structure returned by the ASF API
    when ``output=metalink``, so it can be fed directly to ``aria2c`` via
    ``--metalink-file``.

    Parameters
    ----------
    geojson_path : Path|str|dict
        Path to the merged GeoJSON file.
    metalink_path : str
        Destination path for the generated metalink file.
    """
    with open(geojson_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    _geojson_to_metalink(data, metalink_path)


def create_session(max_retries=5):
    retry_strategy = Retry(
        total=max_retries,  # total number of retries
        backoff_factor=1,  # delay: 1s, 2s, 4s, ...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)

    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _build_query(
    dataset,
    beam_mode,
    processing_level,
    start_date,
    end_date,
    output_type,
    flight_direction=None,
    bbox_str=None,
):
    """Build an ASF query URL with the given parameters."""
    query_string = QUERY_TEMPLATE.format(
        dataset, beam_mode, processing_level, start_date, end_date, output_type
    )
    if flight_direction:
        query_string += f"&flightDirection={flight_direction}"
    if bbox_str:
        query_string += f"&bbox={bbox_str}"
    return query_string


def _do_query(session, query_string, outfile):
    """Execute a single ASF query and save the response to *outfile*."""
    response = session.get(query_string)
    response.raise_for_status()
    with open(outfile, "wb") as f:
        f.write(response.content)


def query_asf(
    bbox: List[float],
    start_date: str,
    end_date: str,
    outfile: str = "sentinel.geojson",
    dataset="SENTINEL-1",
    beam_mode="IW",
    processing_level="SLC",
    flight_direction: Optional[Literal["ASCENDING", "DESCENDING"]] = None,
    output_type: Literal["geojson", "metalink"] = "geojson",
):
    """
    Query Sentinel-1 IW SLC scenes from ASF and save results to a file.

    The function first queries with ``output=count`` for the full spatial
    and temporal extent.  If the result count is below 2000, a single
    query with the requested *output_type* is issued.  Otherwise the
    query is split by year; if a single year still exceeds 2000 scenes
    the bounding box is further subdivided into 1x1-degree tiles.

    Parameters
    ----------
    bbox: List[float]
        List of [west, south, east, north] coordinates.
    start_date: str
        Start date in 'YYYY-MM-DD' format.
    end_date: str
        End date in 'YYYY-MM-DD' format.
    outfile: str
        Output file name (default 'sentinel.geojson').
    dataset: str
        Dataset name (default "SENTINEL-1").
    beam_mode: str
        Beam mode (default "IW").
    processing_level: str
        Processing level (default "SLC").
    flight_direction: Literal["ASCENDING", "DESCENDING", None]
        Optional flight direction filter ("ASCENDING", "DESCENDING", or None).
    output_type: Literal["geojson", "metalink"]
        Output type for results (default "geojson").

    Example
    -------
    >>> bbox = [-123.5, 37.0, -122.0, 38.0]  # [west, south, east, north]
    >>> start_date = "2023-01-01"
    >>> end_date = "2023-12-31"
    >>> flight_direction = "ASCENDING"
    >>> query_asf(bbox, start_date, end_date, flight_direction=flight_direction)
    """
    if flight_direction not in ["ASCENDING", "DESCENDING", None]:
        raise ValueError("flight_direction must be 'ASCENDING', 'DESCENDING', or None")

    lat_min, lat_max = bbox[1], bbox[3]
    lon_min, lon_max = bbox[0], bbox[2]
    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")

    # ---- shared state -------------------------------------------------------
    temp_dir = f"temp_queries_{start_date}_{end_date}"
    os.makedirs(temp_dir, exist_ok=True)
    session = create_session()

    # ---- helper: run a count query for a given time window and bbox ---------
    def _get_count(s_date: str, e_date: str, bbox_str: str) -> int:
        q = _build_query(
            dataset,
            beam_mode,
            processing_level,
            s_date,
            e_date,
            "count",
            flight_direction,
            bbox_str,
        )
        resp = session.get(q)
        resp.raise_for_status()
        return int(resp.text.strip())

    # ---- Step 1: count query for the full extent ----------------------------
    bbox_full = ",".join(map(str, [lon_min, lat_min, lon_max, lat_max]))
    total_count = _get_count(start_date, end_date, bbox_full)

    if total_count == 0:
        logger.info("No scenes found for the given query parameters.")
        shutil.rmtree(temp_dir)
        return

    if total_count < 2000:
        logger.info("Total scene count %d < 2000; querying all at once.", total_count)
        q = _build_query(
            dataset,
            beam_mode,
            processing_level,
            start_date,
            end_date,
            output_type,
            flight_direction,
            bbox_full,
        )
        _do_query(session, q, os.path.join(temp_dir, f"sentinel_full.{output_type}"))

    else:
        # ---- Step 2: split by year ------------------------------------------
        logger.warning(
            "Total scene count %d >= 2000. Splitting query by year.", total_count
        )
        start_year = start_date_obj.year
        end_year = end_date_obj.year

        n_years = end_year - start_year + 1
        pbar = tqdm.tqdm(total=n_years, desc="Querying ASF by year", unit="year")

        for year in range(start_year, end_year + 1):
            # Clip year boundaries to the original date range
            y_start = f"{year}-01-01"
            y_end = f"{year}-12-31"
            if y_start < start_date:
                y_start = start_date
            if y_end > end_date:
                y_end = end_date

            year_count = _get_count(y_start, y_end, bbox_full)

            if year_count == 0:
                logger.debug("Year %d: 0 scenes, skipping.", year)
                pbar.update(1)
                continue

            if year_count < 2000:
                logger.info(
                    "Year %d: %d scenes < 2000; querying full bbox.", year, year_count
                )
                q = _build_query(
                    dataset,
                    beam_mode,
                    processing_level,
                    y_start,
                    y_end,
                    output_type,
                    flight_direction,
                    bbox_full,
                )
                _do_query(
                    session, q, os.path.join(temp_dir, f"sentinel_{year}.{output_type}")
                )

            else:
                # ---- Step 3: split bbox into 1x1-degree tiles ----------------
                logger.warning(
                    "Year %d: %d scenes >= 2000; splitting by 1x1-degree tiles.",
                    year,
                    year_count,
                )
                for lat in np.arange(lat_min, lat_max, 1):
                    for lon in np.arange(lon_min, lon_max, 1):
                        tile_bbox = ",".join(
                            map(
                                str,
                                [
                                    lon,
                                    lat,
                                    np.minimum(lon + 1, lon_max),
                                    np.minimum(lat + 1, lat_max),
                                ],
                            )
                        )
                        q = _build_query(
                            dataset,
                            beam_mode,
                            processing_level,
                            y_start,
                            y_end,
                            output_type,
                            flight_direction,
                            tile_bbox,
                        )
                        outfile_tile = os.path.join(
                            temp_dir,
                            f"sentinel_{year}_{lat:.0f}_{lon:.0f}.{output_type}",
                        )
                        try:
                            _do_query(session, q, outfile_tile)
                        except requests.RequestException as exc:
                            logger.error(
                                "Query failed for tile %s in year %d: %s",
                                tile_bbox,
                                year,
                                exc,
                            )

            pbar.update(1)
        pbar.close()

    # ---- merge & cleanup ----------------------------------------------------
    if output_type == "metalink":
        merge_asf_metalink(temp_dir, outfile)
    else:
        merge_asf_geojson(temp_dir, outfile)
    shutil.rmtree(temp_dir)
