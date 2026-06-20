import importlib.resources as ir
import yaml

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple, Optional, Literal
from s1proc._log import setup_logger
logger = setup_logger(__name__, 'INFO')

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
    rowlook: int = 10
    collook: int = 20
    wavelength: float = 0.055465763
    min_tbl: int = 6       # minimum temporal baseline
    max_tbl: int = 360     # maximum temporal baseline
    min_sbl: int = 0       # minimum spatial baseline
    max_sbl: int = 300     # maximum spatial baseline

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
    parameters: TroposphericParams = field(default_factory = TroposphericParams)

@dataclass
class DetrendingConfig:
    enable: bool = False
    type: Literal["plane", "quadratic"] = "plane"

@dataclass
class UnwrapParams:
    only_save_phase: bool = True
    cost_mode: Optional[Literal["smooth","topo","defo"]] = "smooth"
    rowtile: Optional[int] = None
    coltile: Optional[int] = None
    rowoverlap: Optional[int] = 200
    coloverlap: Optional[int] = 200
    tile_nproc: Optional[int] = None
    conncomp: Optional[bool] = False
    bridge: Optional[bool] = False

@dataclass
class UnwrapConfig:
    method: Literal['snaphu','whirlwind'] = 'whirlwind'
    parameters: UnwrapParams = field(default_factory = UnwrapParams)
    
@dataclass
class S1Config:
    io: IoConfig = field(default_factory = IoConfig)
    proc: ProcessingConfig = field(default_factory = ProcessingConfig)
    filter: FilteringConfig = field(default_factory = FilteringConfig)
    tropo: TroposphericConfig = field(default_factory = TroposphericConfig)
    detrend: DetrendingConfig = field(default_factory = DetrendingConfig)
    unwrap: UnwrapConfig = field(default_factory = UnwrapConfig)

def ensure_config(path="config.yaml", overwrite=False):
    path = Path(path)

    if path.exists() and not overwrite:
        return path

    with ir.open_text("s1proc.config", "default.yaml") as f:
        path.write_text(f.read())

    return path

def initialize_config(
        config_file: Path|str = 'config.yaml',
        overwrite: bool = False
        ):
    """
    Create a default configuration file

    Parameters
    ----------
    overwrite: bool
        Overwrite current configuration file
    """
    cfg_path = ensure_config(config_file, overwrite)
    logger.info(f'Creating a default configuration file: {cfg_path}')

def load_config(
        config_file: Path|str) -> S1Config:
    from dacite import from_dict, Config
    with open(config_file, 'r') as f:
        cfg = yaml.safe_load(f)
    return from_dict(data_class = S1Config, data = cfg,
            config = Config(strict=True))
