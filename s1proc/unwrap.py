import os
import subprocess
from pathlib import Path

from s1proc._log import setup_logger
from s1proc.geocoordinates import GeoCoordinates
logger = setup_logger(name = __name__, level = "INFO")

def batch_snaphu(
    input_folder: str,
    output_folder: str,
    rsc_file: str = 'dem.rsc',
    rowtile: int = 1,
    coltile: int = 1,
    rowoverlap: int = 200,
    coloverlap: int = 200,
    nproc: int = 1,
    file_extension: str = ".int",
    cost_mode: str = "SMOOTH"
):
    """
    Batch unwrap using SNAPHU

    Parameters
    ----------
    input_folder : str
        Folder containing wrapped phase files
    output_folder : str
        Folder to save unwrapped results
    rsc_file: str
        Path to .rsc file for image width 
    rowtile : int
        Number of tiles in row direction 
    coltile : int
        Number of tiles in column direction 
    rowoverlap : int
        Overlap in row direction 
    coloverlap : int
        Overlap in column direction 
    nproc : int
        Number of parallel processes 
    file_extension : str
        File extension filter (default: .int)
    cost_mode : str
        SNAPHU cost mode: 'DEFO', 'SMOOTH', 'TOPO'
    """

    rsc = GeoCoordinates(rsc_file)
    width = rsc.nlon
    input_folder = Path(input_folder)
    output_folder = Path(output_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    files = list(input_folder.glob(f"*{file_extension}"))

    if not files:
        logger.info("No input files found.")
        return

    logger.info(f"Found {len(files)} files.")
    if cost_mode == "DEFO":
        cm = "-d"
    elif cost_mode == "SMOOTH":
        cm = "-s"
    elif cost_mode == "TOPO":
        cm = "-t"
    else:
        logger.error(f"Invalid cost mode: {cost_mode}. Use 'DEFO', 'SMOOTH', or 'TOPO'.")
        return

    for f in files:
        infile = f
        outfile = os.path.join(output_folder, (f.stem + ".unw"))
        if os.path.exists(outfile):
            continue

        cmd = [
            "snaphu",
            str(infile),
            str(width),
            cm,
            "-o", str(outfile),
        ]

        # Tile parameters
        if rowtile > 1 or coltile > 1:
            cmd += [
                "--tile",
                str(rowtile),
                str(coltile),
                str(rowoverlap),
                str(coloverlap),
            ]

        # Parallel processes
        if nproc > 1:
            cmd += ["--nproc", str(nproc)]

        logger.info(f"\nProcessing: {infile.name}")
        logger.info("Command:" +  " ".join(cmd))

        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            logger.error(f"Error processing {infile.name}: {e}")
