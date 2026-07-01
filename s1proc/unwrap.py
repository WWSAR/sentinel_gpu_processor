import multiprocessing
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, List

import numpy as np

from s1proc import get_bin_path
from s1proc._log import set_logging_level, setup_logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.sario import readc, savec
from s1proc.utils import get_files

logger = setup_logger(name=__name__, level="INFO")


class TaskScheduler:
    """
    A generic resource-managed scheduler that prevents CPU over-allocation
    by dynamically balancing system limits against single-task configurations.
    """

    def __init__(self, total_cpus: int = None):
        self.total_cpus = total_cpus or multiprocessing.cpu_count()
        logger.info(
            "Managed capacity initialized with " + f"{self.total_cpus} CPU cores."
        )

    def execute_parallel_tasks(
        self,
        worker_function: Callable,
        task_items: List[Dict[str, Any]],
        cores_per_task: int,
        identifier: str,
    ):
        """
        Executes a batch of tasks concurrently without overcommitting
        system resources.

        Parameters
        ----------
        worker_function : Callable
            The top-level pure function handling a single item
            (e.g., unwrap_snaphu).
        task_items : List[Dict[str, Any]]
            A list of dictionary items containing arguments required by
            the worker_function.
        cores_per_task : int
            The absolute number of CPU cores a single worker execution consumes.
        identifier: str
            The argument serving as an identifier of the work_function
        """
        if not task_items:
            logger.info("Task queue is empty. No operations performed.")
            return

        max_workers = max(1, self.total_cpus // cores_per_task)

        logger.info("=== Workstation Resource Allocation Strategy ===")
        logger.info(f"Total available processor pool: {self.total_cpus} cores")
        logger.info(
            "Target profile footprint allocation: "
            + f"{cores_per_task} cores per worker"
        )
        logger.info("Calculated maximum safe concurrency ceiling: " + f"{max_workers}")
        logger.info("================================================")

        # Execute safe processing blocks asynchronously outside the GIL
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {}

            for item_kwargs in task_items:
                future = executor.submit(worker_function, **item_kwargs)
                future_to_item[future] = item_kwargs.get(identifier, "Unknown Target")

            # Collect processing tickets as they finish
            for future in as_completed(future_to_item):
                target_identity = future_to_item[future]
                try:
                    result_identifier = future.result()
                    logger.info(
                        "Success: Processing task for [ "
                        + f"{result_identifier} ] finished successfully."
                    )
                except Exception as exc:
                    logger.error(
                        "Failure: Processing task for [ "
                        + f"{target_identity} ] crashed: {exc}"
                    )


def apply_zero_mask(
    ifg_file: Path | str,
    unw_file: Path | str,
    outfile: Path | str,
    only_save_phase: bool,
    nrow: int,
    ncol: int,
):
    """
    Mask nodata area of the unwrapped interferogram to zero

    Parameters
    ----------
    ifg_file: Path|str
        Input wrapped interferogram. Pixels with zero amplitude are used as
        the zero mask
    unw_file: Path|str
        Unwrapped interferogram
    outfile: Path|str
        Masked interferogram
    only_save_phase: Path|str
        Only save unwrapped phase to save disk space
    nrow: int
        Number of rows of the interferogram
    ncol: int
        Number of columns of the interfergram
    """
    ifg = np.fromfile(ifg_file, dtype=np.complex64)
    ifg = np.reshape(ifg, (nrow, ncol))
    unw = readc(unw_file, ncol)
    zero_mask = np.abs(ifg) == 0
    unw[zero_mask] = 0
    if only_save_phase:
        unw.imag.tofile(outfile)
    else:
        savec(unw, outfile)


def unwrap_snaphu(
    snaphu_executable: Path | str,
    ifg_file: Path | str,
    corr_file: Path | str | None,
    outfile: Path | str,
    nrow: int,
    ncol: int,
    cost_mode: str,
    rowtile: int,
    coltile: int,
    rowoverlap: int,
    coloverlap: int,
    tile_nproc: int,
    only_save_phase: bool,
) -> str:
    """
    Helper function to process a single .int file using SNAPHU.

    Parameters
    ----------
    snpahu_executable: str
        Path to snaphu executable
    ifg_file: Path|str
        Wrapped interferogram to unwrap
    corr_file: Path|str
        InSAR correlation file
    outfile: Path|str
        Unwrapped interferogram
    nrow: int
        Number of rows of the input interferogram
    ncol: int
        Number of columns of the input interferogram
    cost_mode: str
        SNAPHU cost mode
    rowtile: int
        Number of row tiles
    coltile: int
        Number of col tiles
    rowoverlap: int
        Number of overlapped lines between tiles
    coloverlap: int
        Number of overlapped lines between tiles
    tile_nproc: int
        Number of threads for parallelized tile unwrapping
    only_save_phase: bool
        Only save unwrapped phase to disk (ignoring amplitude)
    """
    if cost_mode.lower() == "smooth":
        _cost_mode = "-s"
    elif cost_mode.lower() == "topo":
        _cost_mode = "-t"
    elif cost_mode.lower() == "defo":
        _cost_mode = "-d"
    else:
        raise ValueError(f"Unrecognized cost mode {cost_mode} for SNAPHU")

    cmd = [
        snaphu_executable,
        str(ifg_file),
        str(ncol),
        _cost_mode,
        "-o",
        str(outfile),
    ]

    if rowtile > 1 or coltile > 1:
        cmd += [
            "--tile",
            str(rowtile),
            str(coltile),
            str(rowoverlap),
            str(coloverlap),
        ]

    if tile_nproc > 1:
        cmd += ["--nproc", str(tile_nproc)]

    if corr_file is not None:
        cmd += ["-c", str(corr_file)]

    logger.debug(f"Executing: {ifg_file} -> {outfile}")
    logger.debug("Command: " + " ".join(cmd))

    try:
        # subprocess.run(cmd, check=True)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        apply_zero_mask(ifg_file, outfile, outfile, only_save_phase, nrow, ncol)

    except subprocess.CalledProcessError as e:
        error_message = (
            f"SNAPHU processing failure on [ {str(ifg_file)} ]. "
            + f"Exit code: {e.returncode} \n"
            + e.stderr
        )
        raise RuntimeError(error_message)
    return str(ifg_file)


def unwrap_whirlwind(
    whirlwind_executable: str,
    ifg_file: Path | str,
    corr_file: Path | str,
    outfile: Path | str,
    ncol: int,
    only_save_phase: bool,
    conncomp: bool,
    bridge: bool,
) -> str:
    """
    Helper function to process a single .int file using whirlwind.

    Parameters
    ----------
    whirlwind: str
        Path to whirlwind executable
    ifg_file: Path|str
        Wrapped interferogram to unwrap
    corr_file: Path|str
        InSAR correlation file
    outfile: Path|str
        Unwrapped interferogram
    ncol: int
        Number of columns of the input interferogram
    only_save_phase: bool
        Only save unwrapped phase
    conncomp: bool
        Save connected components
    bridge: bool
        If False, disable the integration-component bridge post-pass
    """
    cmd = [
        whirlwind_executable,
        "--ifg",
        str(ifg_file),
        "--cor",
        str(corr_file),
        "--cols",
        str(ncol),
        "--out",
        str(outfile),
    ]

    if only_save_phase:
        cmd += ["--out-format", "float"]
    if not conncomp:
        cmd += ["--no-conncomp"]
    if not bridge:
        cmd += ["--no-bridge"]

    logger.debug(f"Executing: {ifg_file} -> {outfile}")
    logger.debug("Command: " + " ".join(cmd))

    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    except subprocess.CalledProcessError as e:
        error_message = (
            f"Whirlwind processing failure on [ {str(ifg_file)} ]. "
            + f"Exit code: {e.returncode} \n"
            + e.stderr
        )
        raise RuntimeError(error_message)
    return str(ifg_file)


def batch_unwrap(
    ifg_path: str | None = None,
    cc_path: str | None = None,
    unw_path: str | None = None,
    max_cpus: int | None = None,
    config: str = "config.yaml",
    verbose: bool = False,
):
    """
    Batch phase unwrapping using whirlwind or snaphu.

    Parameters
    ----------
    ifg_path: str | None
        Input interferogram path
    cc_path: str | None
        Correlation path
    unw_path: str | None
        Output unwrapped interferogram path
    max_cpus: int | None
        Maximum number of cores used for phase unwrapping. If None, set to
        total number of CPU cores
    config: str
        Configuration file
    verbose: bool
        Set logging level to DEBUG
    """
    if verbose:
        set_logging_level(logger, "DEBUG")
    if max_cpus is None:
        max_cpus = multiprocessing.cpu_count()
    else:
        max_cpus = np.minimum(max_cpus, multiprocessing.cpu_count())

    from s1proc._config import load_config

    cfg = load_config(config)
    icfg = cfg.io
    ucfg = cfg.unwrap
    unwrap_executable = get_bin_path(ucfg.method.lower())
    if not os.path.exists(unwrap_executable):
        raise RuntimeError(f"Cannot find {unwrap_executable}")

    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    if ucfg.method.lower() == "snaphu":
        # Snaphu parameter setting
        if ucfg.parameters.rowtile is None:
            rowtile = int(np.maximum(np.floor(nrow / 1024), 1))
        else:
            rowtile = ucfg.parameters.rowtile
        if ucfg.parameters.coltile is None:
            coltile = int(np.maximum(np.floor(ncol / 1024), 1))
        else:
            coltile = ucfg.parameters.coltile
        ntile = rowtile * coltile
        if ucfg.parameters.tile_nproc is None:
            tile_nproc = np.min([ntile, max_cpus])
        else:
            tile_nproc = np.min([ntile, max_cpus, ucfg.parameters.tile_nproc])
        cores_per_task = tile_nproc
        unwrap_func = unwrap_snaphu
        logger.debug("Snaphu parameters")
        logger.debug(f"rowtile set to {rowtile}")
        logger.debug(f"coltile set to {coltile}")
        logger.debug(f"tile_nproc set to {tile_nproc}")
    elif ucfg.method.lower() == "whirlwind":
        cores_per_task = 4
        unwrap_func = unwrap_whirlwind
    else:
        raise ValueError(f"Unrecognized unwrapping method: {ucfg.method}")

    if ifg_path is None:
        ifg_path = os.path.join(icfg.ifg_path, "*.int")
    if cc_path is None:
        cc_path = icfg.ifg_path
    if unw_path is None:
        unw_path = icfg.unw_path

    ifg_files = get_files(ifg_path)
    os.makedirs(unw_path, exist_ok=True)

    if len(ifg_files) == 0:
        logger.warning("No input files found.")
        return

    logger.debug(f"Found {len(ifg_files)} interferograms to unwrap.")

    task_items = []
    for ifg_file in ifg_files:
        corr_file = os.path.join(cc_path, Path(ifg_file).stem + ".cc")
        outfile = os.path.join(unw_path, Path(ifg_file).stem + ".unw")
        # avoid data writing error in SNAPHu
        corr_file = Path(corr_file).as_posix()
        outfile = Path(outfile).as_posix()
        if os.path.exists(outfile):
            logger.info(f"Output target {outfile} already exists. Skipping.")
            continue
        if ucfg.method == "whirlwind":
            task_items.append({
                "whirlwind_executable": unwrap_executable,
                "ifg_file": ifg_file,
                "corr_file": corr_file,
                "outfile": outfile,
                "ncol": ncol,
                "only_save_phase": ucfg.parameters.only_save_phase,
                "conncomp": ucfg.parameters.conncomp,
                "bridge": ucfg.parameters.bridge,
            })
        elif ucfg.method == "snaphu":
            task_items.append({
                "snaphu_executable": unwrap_executable,
                "ifg_file": ifg_file,
                "corr_file": corr_file,
                "outfile": outfile,
                "nrow": nrow,
                "ncol": ncol,
                "cost_mode": ucfg.parameters.cost_mode,
                "rowtile": rowtile,
                "coltile": coltile,
                "rowoverlap": ucfg.parameters.rowoverlap,
                "coloverlap": ucfg.parameters.coloverlap,
                "tile_nproc": tile_nproc,
                "only_save_phase": ucfg.parameters.only_save_phase,
            })

    if not task_items:
        logger.warning("All files in queue have already been successfully processed.")
        return

    # Call the generic Class Scheduler tool
    scheduler = TaskScheduler(total_cpus=max_cpus)
    scheduler.execute_parallel_tasks(
        worker_function=unwrap_func,
        task_items=task_items,
        cores_per_task=cores_per_task,
        identifier="ifg_file",
    )

    return
