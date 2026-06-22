#!/usr/bin/env python3
"""
Download Sentinel-1 data products using aria2c.

This module provides functions to download data from metalink files
obtained from ASF Vertex / DAAC, with automatic credential handling
for NASA Earthdata Login.
"""

from __future__ import annotations

import getpass
import netrc
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from s1proc import get_bin_path
from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")

NASA_MACHINE = "urs.earthdata.nasa.gov"


def _find_netrc_path() -> Optional[Path]:
    """
    Locate the netrc file on the current platform.

    Returns
    -------
    Path or None
        Path to the netrc file, or None if not found.
    """
    if sys.platform == "win32":
        # Python's netrc module looks for %HOME%\\_netrc on Windows,
        # but HOME is often unset. Also check %USERPROFILE%.
        candidates = []
        home = os.environ.get("HOME")
        if home:
            candidates.append(Path(home) / "_netrc")
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            candidates.append(Path(userprofile) / "_netrc")
        # Also accept .netrc (some tools ship it this way on Windows)
        if userprofile:
            candidates.append(Path(userprofile) / ".netrc")
        for p in candidates:
            if p.is_file():
                return p
        return None
    else:
        p = Path.home() / ".netrc"
        return p if p.is_file() else None


def _check_earthdata_netrc() -> Optional[Tuple[str, str]]:
    """
    Read NASA Earthdata credentials from the netrc file.

    Returns
    -------
    (login, password) or None
        Credentials if found, None otherwise.
    """
    netrc_path = _find_netrc_path()
    if netrc_path is None:
        logger.debug("No netrc file found.")
        return None

    try:
        auth = netrc.netrc(str(netrc_path))
        entry = auth.authenticators(NASA_MACHINE)
        if entry is None:
            logger.debug("netrc file found but no entry for %s.", NASA_MACHINE)
            return None
        login, _, password = entry
        if login and password:
            return login, password
    except (netrc.NetrcParseError, OSError) as exc:
        logger.warning("Failed to parse netrc file: %s", exc)

    return None


def _prompt_earthdata_credentials(netrc_path: Path) -> Tuple[str, str]:
    """
    Prompt the user for Earthdata credentials and persist them to netrc.

    Parameters
    ----------
    netrc_path : Path
        Path where the netrc file will be written.

    Returns
    -------
    (login, password)
    """
    print(
        "NASA Earthdata Login credentials are required to download data.\n"
        "If you do not have an account, register at: "
        "https://urs.earthdata.nasa.gov/users/new"
    )
    username = input("Earthdata username: ").strip()
    password = getpass.getpass("Earthdata password: ").strip()

    if not username or not password:
        raise ValueError("Username and password must not be empty.")

    netrc_path.parent.mkdir(parents=True, exist_ok=True)

    # Preserve existing entries if netrc already exists
    existing_entries: list[str] = []
    if netrc_path.is_file():
        with open(netrc_path, encoding="utf-8") as f:
            existing_entries = f.read().rstrip().splitlines()

    # Remove any stale entry for this machine
    new_entries: list[str] = []
    skip_block = False
    for line in existing_entries:
        if line.strip().startswith(f"machine {NASA_MACHINE}"):
            skip_block = True
        elif skip_block and line.strip().startswith("machine "):
            skip_block = False
            new_entries.append(line)
        elif not skip_block:
            new_entries.append(line)

    new_entries.append(f"machine {NASA_MACHINE}")
    new_entries.append(f"  login {username}")
    new_entries.append(f"  password {password}")
    new_entries.append("")  # trailing newline

    with open(netrc_path, "w", encoding="utf-8") as f:
        f.write("\n".join(new_entries))

    # Restrict permissions on Unix
    if sys.platform != "win32":
        os.chmod(netrc_path, 0o600)

    logger.info("Earthdata credentials saved to %s.", netrc_path)
    return username, password


def ensure_earthdata_credentials() -> Tuple[str, str]:
    """
    Ensure Earthdata Login credentials are available in netrc.

    Checks the netrc file for ``urs.earthdata.nasa.gov`` credentials.
    If missing, prompts the user interactively and writes the entry.

    Returns
    -------
    (login, password)
    """
    creds = _check_earthdata_netrc()
    if creds is not None:
        logger.debug("Found existing Earthdata credentials in netrc.")
        return creds

    netrc_path = _netrc_write_path()
    logger.info("Earthdata credentials not found. Prompting for input...")
    return _prompt_earthdata_credentials(netrc_path)


def _netrc_write_path() -> Path:
    """Return the canonical netrc path for the current platform."""
    if sys.platform == "win32":
        userprofile = os.environ.get("USERPROFILE", "")
        if userprofile:
            return Path(userprofile) / "_netrc"
        return Path.home() / "_netrc"
    else:
        return Path.home() / ".netrc"


def _find_aria2c() -> str:
    """
    Locate the aria2c executable.

    On Windows, looks for ``aria2c.exe`` inside the package ``bin/``
    directory. On Linux, checks whether ``aria2c`` is on ``PATH`` and
    raises an error with installation instructions if it is not.

    Returns
    -------
    str
        Absolute path to the aria2c executable.

    Raises
    ------
    FileNotFoundError
        If aria2c cannot be located.
    """
    if sys.platform == "win32":
        return get_bin_path("aria2c")

    # Linux / macOS
    aria2c_path = shutil.which("aria2c")
    if aria2c_path is None:
        msg = (
            "aria2c was not found on the system PATH.\n"
            "Install it with one of the following commands:\n"
            "  - Debian/Ubuntu:     sudo apt install aria2\n"
            "  - RHEL/CentOS/Fedora: sudo yum install aria2\n"
            "  - Conda:              conda install -c conda-forge aria2\n"
            "  - Source:             https://github.com/aria2/aria2/releases"
        )
        raise FileNotFoundError(msg)
    return aria2c_path


def download_metalink(
    metalink_file: str,
    output_dir: str = ".",
    *,
    continue_on_error: bool = True,
) -> None:
    """
    Download data products from a metalink file using aria2c.

    Before downloading, this function ensures that NASA Earthdata Login
    credentials are present in the user's netrc file (``~/.netrc`` on
    Linux, ``%USERPROFILE%\\_netrc`` on Windows).  If they are missing,
    the user is prompted to enter them interactively.

    Parameters
    ----------
    metalink_file : str
        Path to the ``.metalink`` or ``.metalink4`` file listing the
        products to download.
    output_dir : str
        Directory where downloaded files will be saved.  Defaults to
        the current working directory.
    continue_on_error : bool
        If ``True`` (default), aria2c continues downloading remaining
        files after an individual download failure.

    Raises
    ------
    FileNotFoundError
        If ``metalink_file`` does not exist.
    subprocess.CalledProcessError
        If aria2c exits with a non-zero return code.
    """
    metalink_path = Path(metalink_file).resolve()
    if not metalink_path.is_file():
        raise FileNotFoundError(f"Metalink file not found: {metalink_path}")

    out_path = Path(output_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    # Ensure credentials are available before launching aria2c
    ensure_earthdata_credentials()

    aria2c = _find_aria2c()
    cmd = [
        aria2c,
        "--metalink-file",
        str(metalink_path),
        "--dir",
        str(out_path),
        "-j",
        "3",
        "-x",
        "16",
        "-s",
        "16",
        "--console-log-level",
        "error",
        "--check-integrity=true",
    ]
    if continue_on_error:
        cmd.append("--continue=true")

    logger.info("Launching aria2c download: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    logger.info("Download complete. Files saved to %s.", out_path)
