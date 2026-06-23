import importlib.resources as ir
from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import List, Literal, Optional

import numpy as np

from s1proc._log import setup_logger
from s1proc.query import query_asf

logger = setup_logger(__name__, "INFO")


@dataclass
class AreaConfig:
    bbox: Optional[List[float]] = None
    path_list: Optional[List[int]] = None
    path_number: Optional[int] = None
    frame_list: Optional[List[int]] = None
    flight_direction: Optional[Literal["ASCENDING", "DESCENDING"]] = None


@dataclass
class DateConfig:
    start: Optional[str] = None
    end: Optional[str] = None


@dataclass
class IoConfig:
    data_path: str
    eof_path: str
    proc_path: str
    slc_path: str
    amp_path: str
    ifg_path: str
    unw_path: str
    dem_file: str
    rsc_file: str
    img_pair_file: str
    geometry_path: str
    ifg_corr_path: str
    unw_corr_path: str
    multilook_dem_file: str
    multilook_rsc_file: str


@dataclass
class ProcessingConfig:
    download_dem: bool
    download_data: bool
    download_eof: bool
    rowlook: int
    collook: int
    wavelength: float
    min_tbl: int  # minimum temporal baseline
    max_tbl: int  # maximum temporal baseline
    min_sbl: int  # minimum spatial baseline
    max_sbl: int  # maximum spatial baseline
    ngpu: Optional[int]  # number of GPUs for parallel interferogram processing
    task_per_gpu: Optional[int]  # number of tasks running on each GPU (legacy)
    max_slots: Optional[int] = None  # pre-allocated pinned-memory buffer slots


@dataclass
class FilteringParams:
    window_size: int = 32
    goldstein_alpha: float = 0.5


@dataclass
class FilteringConfig:
    enable: bool = False
    method: Literal["goldstein"] = "goldstein"
    parameters: FilteringParams = field(default_factory=FilteringParams)


@dataclass
class TroposphericParams:
    flip_sign: bool = False
    hour: Optional[int] = None
    delay_path: Optional[str] = "tropo_delay"


@dataclass
class TroposphericConfig:
    enable: bool = True
    method: Literal["era5"] = "era5"
    parameters: TroposphericParams = field(default_factory=TroposphericParams)


@dataclass
class DetrendingConfig:
    enable: bool = False
    type: Literal["plane", "quadratic"] = "plane"


@dataclass
class UnwrapParams:
    only_save_phase: bool = True
    cost_mode: Optional[Literal["smooth", "topo", "defo"]] = "smooth"
    rowtile: Optional[int] = None
    coltile: Optional[int] = None
    rowoverlap: Optional[int] = 200
    coloverlap: Optional[int] = 200
    tile_nproc: Optional[int] = None
    conncomp: Optional[bool] = False
    bridge: Optional[bool] = False


@dataclass
class UnwrapConfig:
    method: Literal["snaphu", "whirlwind"] = "whirlwind"
    parameters: UnwrapParams = field(default_factory=UnwrapParams)


@dataclass
class S1Config:
    io: IoConfig = field(default_factory=IoConfig)
    proc: ProcessingConfig = field(default_factory=ProcessingConfig)
    filter: FilteringConfig = field(default_factory=FilteringConfig)
    tropo: TroposphericConfig = field(default_factory=TroposphericConfig)
    detrend: DetrendingConfig = field(default_factory=DetrendingConfig)
    unwrap: UnwrapConfig = field(default_factory=UnwrapConfig)
    area: AreaConfig = field(default_factory=AreaConfig)
    date: DateConfig = field(default_factory=DateConfig)


def _filter_by_path(s1_data: dict, path_number: int) -> dict:
    """
    Filter Sentinel-1 data based on path_number

    Parameters
    ----------
    s1_data: dict
        Sentinel-1 data dictionary read from a geojson file
    path_number: int
        Target path number
    """
    all_features = s1_data.get("features", [])
    filtered = []
    for feat in all_features:
        props = feat.get("properties", {})
        feat_path = props.get("pathNumber")
        if int(feat_path) == path_number:
            filtered.append(feat)
    if len(filtered) == 0:
        logger.error(f"No Sentinel-1 scens found for path {path_number}")
        return None
    filtered_s1_data = deepcopy(s1_data)
    filtered_s1_data["features"] = filtered
    return filtered_s1_data


def populate_config(config: str = "config.yaml"):
    """
    Extract study period and study area from the input configuration file,
    query Sentinel-1 data to find all paths overlapping with the study area.
    Create a processing folder for each path, and finally write the
    corresponding configuration files.

    Parameters
    ----------
    config: str | None
        Input configuration file
    """
    import json

    import pandas as pd
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.sort_keys = False
    yaml.indent(mapping=4, sequence=4, offset=2)
    with open(config, "r") as f:
        cfg = yaml.load(f)
    try:
        start_date = cfg["date"]["start"]
        end_date = cfg["date"]["end"]
        bbox = cfg["area"]["bbox"]
        flight_direction = cfg["area"]["flight_direction"]
    except Exception as e:
        logger.error(e)
        logger.error(
            "start and end dates, bbox, and flight direction are "
            + "required to populate the configuration files"
        )
        return

    roi_geojson_file = "roi.geojson"
    query_asf(
        bbox,
        start_date,
        end_date,
        roi_geojson_file,
        flight_direction=flight_direction,
        output_type="geojson",
    )
    with open(roi_geojson_file, "r") as f:
        s1_data = json.load(f)

    properties_list = [feature["properties"] for feature in s1_data["features"]]
    s1_df = pd.DataFrame(properties_list)
    path_list = np.unique(s1_df["pathNumber"].to_numpy())
    for path_number in path_list:
        sub_s1_df = s1_df[s1_df["pathNumber"] == path_number]
        frame_list = np.unique(sub_s1_df["frameNumber"].to_numpy())
        path_flight_direction = sub_s1_df.iloc[0]["flightDirection"]
        cfg["area"]["path_list"] = None
        cfg["area"]["path_number"] = int(path_number)
        cfg["area"]["frame_list"] = [int(f) for f in frame_list]
        cfg["area"]["flight_direction"] = path_flight_direction
        path_s1_data = _filter_by_path(s1_data, path_number)
        path_dir = Path(path_flight_direction.lower()) / Path(f"path_{path_number}")
        path_dir.mkdir(parents=True, exist_ok=True)
        path_config_file = path_dir / "config.yaml"
        with open(path_config_file, "w") as f:
            yaml.dump(cfg, f)
        logger.info(f"Find {len(sub_s1_df)} images in path {path_number}")
        logger.info(f"Write configuration to {path_config_file}")
        path_geojson_file = path_dir / "roi.geojson"
        with open(path_geojson_file, "w", encoding="utf-8") as f:
            json.dump(path_s1_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Write filtered Sentinel-1 data to {path_geojson_file}")


def ensure_config(path="config.yaml", overwrite=False):
    path = Path(path)

    if path.exists() and not overwrite:
        return path

    with ir.open_text("s1proc.config", "default.yaml") as f:
        path.write_text(f.read())

    return path


def initialize_config(
    config_file: Path | str = "config.yaml",
    start_date: str | None = None,
    end_date: str | None = None,
    bbox: List[float] | None = None,
    flight_direction: Literal["ASCENDING", "DESCENDING"] | None = None,
    overwrite: bool = False,
    setup_only: bool = False,
):
    """
    Create a default configuration file

    Parameters
    ----------
    config_file: Path|str
        Main configuration file
    start_date: str
        Start date in 'YYYY-MM-DD' format.
    end_date: str
        End date in 'YYYY-MM-DD' format.
    bbox: List[float]
        List of [west, south, east, north] coordinates.
    flight_direction: Literal["ASCENDING", "DESCENDING"] | None
        Optional flight direction filter ("ASCENDING", "DESCENDING", or None).
        If None, Sentinel-1 data acquired with both ascending and descending
        geometries will be downloaded and processed
    overwrite: bool
        Overwrite current configuration file
    setup_only: bool
        Only create the main configuration file, do not create path folders and
        populate configuration for each folder
    """
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.sort_keys = False
    yaml.indent(mapping=4, sequence=4, offset=2)
    if not all([start_date, end_date, bbox]):
        logger.warning(
            "start_date, end_date, and bbox need to be provided to"
            + "populate the configuration files for all paths."
        )
        if not setup_only:
            logger.warning(
                "setup-only option is ignored due to incomplete roi"
                + "and study period information"
            )
            setup_only = True
    cfg_path = ensure_config(config_file, overwrite)
    with open(cfg_path, "r") as f:
        cfg = yaml.load(f)
    if "date" not in cfg:
        cfg["date"] = {}
    if start_date is not None:
        cfg["date"]["start"] = start_date
    if end_date is not None:
        cfg["date"]["end"] = end_date

    if "area" not in cfg:
        cfg["area"] = {}
    if bbox is not None:
        cfg["area"]["bbox"] = list(bbox)
    cfg["area"]["flight_direction"] = flight_direction
    with open(cfg_path, "w") as f:
        yaml.dump(cfg, f)
    if not setup_only:
        populate_config(config_file)
    logger.info(f"Created the main configuration file: {cfg_path}")


def load_config(config_file: Path | str, relative_path: bool = True) -> S1Config:
    """
    Load a configuration file

    Parameters
    ----------
    config_file: Path|str
        configuration file
    relative_path: bool
        Treat paths in the configuration file as relative path

    Returns
    -------
    s1cfg: S1Config
        A Sentinel-1 configuration class object
    """
    from dacite import Config, from_dict
    from ruamel.yaml import YAML

    yaml = YAML()
    yaml.default_flow_style = False
    yaml.sort_keys = False
    yaml.indent(mapping=4, sequence=4, offset=2)
    with open(config_file, "r") as f:
        cfg = yaml.load(f)
    s1cfg = from_dict(data_class=S1Config, data=cfg, config=Config(strict=True))
    if relative_path:
        root = Path(config_file).parent
        # loop over all fields in s1cfg.io
        for f in fields(IoConfig):
            current_value = getattr(s1cfg.io, f.name)
            if current_value is not None and current_value != "":
                setattr(s1cfg.io, f.name, str(root / current_value))
        # do not forget to update tropo_delay_path
        s1cfg.tropo.parameters.delay_path = str(
            root / s1cfg.tropo.parameters.delay_path
        )
    return s1cfg
