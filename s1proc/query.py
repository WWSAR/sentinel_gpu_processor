import glob
import json
import numpy as np
import os
import requests
import shutil
import tqdm
from datetime import datetime
from typing import List, Literal
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from s1proc._log import setup_logger, set_logging_level
logger = setup_logger(name = __name__, level = 'INFO')

BASE_URL = "https://api.daac.asf.alaska.edu/services/search/param?"
QUERY_TEMPLATE = BASE_URL + 'dataset={}&beamMode={}&processingLevel={}' + \
                 '&start={}T00:00:00Z&end={}T23:59:59Z&output={}'


def merge_asf_geojson(temp_dir, output_file):
    """
    Merge ASF GeoJSON scene files and remove duplicates.

    Deduplication is based on:
    - sceneName (preferred)
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

    merged = {
        "type": "FeatureCollection",
        "features": features
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    logger.info(f"Merged {len(all_files)} files")
    logger.info(f"Unique scenes: {len(features)}")
    logger.info(f"Saved to {output_file}")

def create_session(max_retries=5):
    retry_strategy = Retry(
        total=max_retries,          # total number of retries
        backoff_factor=1,           # delay: 1s, 2s, 4s, ...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )

    adapter = HTTPAdapter(max_retries=retry_strategy)

    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def query_asf(
    bbox: List[float],
    start_date: str,
    end_date: str,
    outfile: str = 'sentinel.geojson',
    dataset = "SENTINEL-1",
    beam_mode = "IW",
    processing_level = "SLC",
    flight_direction: Literal["ASCENDING", "DESCENDING", None] = None,
    output_type: str = "geojson",
):
    """
    Query Sentinel-1 IW SLC scenes from ASF and save results to a Metalink file.

    Parameters
    ----------
    bbox: List[float]
        List of [west, south, east, north] coordinates.
    start_date: str
        Start date in 'YYYY-MM-DD' format.
    end_date: str
        End date in 'YYYY-MM-DD' format.
    outfile: str
        Output file name (default 'sentinel.metalink').
    dataset: str
        Dataset name (default "SENTINEL-1").
    beam_mode: str
        Beam mode (default "IW").
    processing_level: str
        Processing level (default "SLC").
    flight_direction: Literal["ASCENDING", "DESCENDING", None]
        Optional flight direction filter ("ASCENDING", "DESCENDING", or None).
    output_type: str
        Output type for results (default "geojson").
    
    Example
    -------
    >>> bbox = [-123.5, 37.0, -122.0, 38.0]  # [west, south, east, north]
    >>> start_date = "2023-01-01"
    >>> end_date = "2023-12-31"
    >>> flight_direction = "ASCENDING"
    >>> query_asf(bbox, start_date, end_date, flight_direction=flight_direction)
    """
    # build query string
    if flight_direction not in ["ASCENDING", "DESCENDING", None]:
        raise ValueError("flight_direction must be 'ASCENDING', 'DESCENDING', or None")
    lat_min, lat_max = bbox[1], bbox[3]
    lon_min, lon_max = bbox[0], bbox[2] 
    start_date_obj = datetime.strptime(start_date, "%Y-%m-%d")
    end_date_obj = datetime.strptime(end_date, "%Y-%m-%d")
    first_year = start_date_obj.year
    last_year = end_date_obj.year
    query_string = QUERY_TEMPLATE.format(dataset, beam_mode, processing_level,
                                         start_date, end_date, output_type)
    if flight_direction:
        query_string += f"&flightDirection={flight_direction}"
    
    # create temp directory for metalink files
    temp_dir = 'temp_queries'
    os.makedirs(temp_dir, exist_ok=True)
    
    session = create_session()
    total_tiles = (int(np.ceil(lat_max)) - int(np.floor(lat_min))) * \
        (int(np.ceil(lon_max)) - int(np.floor(lon_min)))
    
    pbar = tqdm.tqdm(total=total_tiles, desc="Querying ASF", unit="tile")
    for lat in np.arange(lat_min, lat_max, 1):
        for lon in np.arange(lon_min, lon_max, 1):
            bbox_str = ",".join(map(str, 
                    [lon, lat, np.minimum(lon + 1, lon_max),
                      np.minimum(lat + 1, lat_max)]))
            query_string_curr = query_string + f"&bbox={bbox_str}"
            try:
                response = session.get(query_string_curr)
                response.raise_for_status()
                outfile_curr = os.path.join(
                    temp_dir, f'sentinel_{lat}_{lon}.{output_type}')
                with open(outfile_curr, 'wb') as f:
                    f.write(response.content)
            except requests.RequestException as e:
                logger.error(f"Error occurred while querying {bbox_str}: {e}")
                for year in range(first_year, last_year + 1):
                    logger.warning(f"Retrying for year {year}...")
                    query_string_year = query_string_curr.replace(
                        f"{start_date}", f"{year}-01-01").replace(
                        f"{end_date}", f"{year}-12-31")
                    try:
                        response = session.get(query_string_year)
                        response.raise_for_status()
                        outfile_curr = os.path.join(
                            temp_dir, f'sentinel_{lat}_{lon}_{year}.{output_type}')
                        with open(outfile_curr, 'wb') as f:
                            f.write(response.content)
                        break  # success, exit retry loop
                    except requests.RequestException as e:
                        logger.error(f"Retry failed for year {year}: {e}")
            pbar.update(1)
    pbar.close()
    # Merge all metalink files and deduplicate scenes
    merge_asf_geojson(temp_dir, outfile)
    shutil.rmtree(temp_dir) # clean up temp directory
    return
