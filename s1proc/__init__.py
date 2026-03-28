import os
import sys
from pathlib import Path

__version__ = "0.1.0"

# 获取当前 s1proc 包的根目录（绝对路径）
BASE_DIR = Path(__file__).resolve().parent

# 定义二进制文件夹路径
BIN_DIR = os.path.join(BASE_DIR, "bin")

def get_bin_path(bin_name: str) -> str:
    """
    Get the absolute path of executable files
    """
    if sys.platform == "win32" and not bin_name.endswith(".exe"):
        bin_name += ".exe"

    bin_path = os.path.join(BIN_DIR, bin_name)

    if not bin_path.exists():
        # 这里可以抛出一个更友好的异常，提醒用户可能没编译成功
        raise FileNotFoundError(
            f"Cannot find executable: {bin_path}\n"
            f"Make sure the package is correctly installed via 'pip install -e .'"
        )

    return str(bin_path)
