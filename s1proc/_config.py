import importlib.resources as ir
import yaml

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple
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
    tropo_delay_path: str
    tropo_corr_path: str
    multilook_dem_file: str
    multilook_rsc_file: str

@dataclass
class ProcessingConfig:
    rowlook: int
    collook: int
    wavelength: float
    hour: int
    flip_sign: bool

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
        config_file: Path|str) -> Tuple[IoConfig, ProcessingConfig]:
    with open(config_file, 'r') as f:
        cfg = yaml.safe_load(f)
    io_config = IoConfig(**cfg['io'])
    proc_config = ProcessingConfig(**cfg['processing'])
    return io_config, proc_config
