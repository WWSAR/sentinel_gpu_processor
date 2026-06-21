import os
import sys
from pathlib import Path
import platformdirs

__version__ = "0.1.0"

BASE_DIR = Path(__file__).resolve().parent

BIN_DIR = os.path.join(BASE_DIR, "bin")

def get_bin_path(bin_name: str) -> str:
    """
    Get the absolute path of executable files
    """
    if sys.platform == "win32" and not bin_name.endswith(".exe"):
        bin_name += ".exe"

    bin_path = os.path.join(BIN_DIR, bin_name)

    if not os.path.exists(bin_path):
        raise FileNotFoundError(
            f"Cannot find executable: {bin_path}\n"
            f"Make sure the package is correctly installed via 'pip install -e .'"
        )

    return str(bin_path)

def get_cache_dir() -> Path:
    """
    Get a cache dir for the package

    Returns:
        cache_dir: Path
    """
    cache_dir = Path(platformdirs.user_cache_dir("s1proc", "wuhu_meiri_tech"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir