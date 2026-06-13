import os
import subprocess
import multiprocessing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Callable

from s1proc._log import setup_logger
from s1proc.geocoordinates import GeoCoordinates
logger = setup_logger(name = __name__, level = "INFO")

class WorkstationTaskScheduler:
    """
    A generic resource-managed scheduler that prevents CPU over-allocation 
    by dynamically balancing system limits against single-task configurations.
    """
    def __init__(self, total_cpus: int = None):
        self.total_cpus = total_cpus or multiprocessing.cpu_count()
        logger.info(f"Workstation Monitor: Managed capacity initialized with {self.total_cpus} CPU cores.")

    def execute_parallel_tasks(self, worker_function: Callable, task_items: List[Dict[str, Any]], cores_per_task: int):
        """
        Executes a batch of tasks concurrently without overcommitting system resources.

        Parameters
        ----------
        worker_function : Callable
            The top-level pure function handling a single item (e.g., unwrap_single_file).
        task_items : List[Dict[str, Any]]
            A list of dictionary items containing arguments required by the worker_function.
        cores_per_task : int
            The absolute number of CPU cores a single worker execution consumes.
        """
        if not task_items:
            logger.info("Task queue is empty. No operations performed.")
            return

        max_workers = max(1, self.total_cpus // cores_per_task)

        logger.info("=== Workstation Resource Allocation Strategy ===")
        logger.info(f"Total available processor pool: {self.total_cpus} cores")
        logger.info(f"Target profile footprint allocation: {cores_per_task} cores per worker")
        logger.info(f"Calculated maximum safe concurrency ceiling: {max_workers}")
        logger.info("================================================")

        # Execute safe processing blocks asynchronously outside the GIL
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {}
            
            for item_kwargs in task_items:
                future = executor.submit(worker_function, **item_kwargs)
                future_to_item[future] = item_kwargs.get("infile", "Unknown Target")

            # Collect processing tickets as they finish
            for future in as_completed(future_to_item):
                target_identity = future_to_item[future]
                try:
                    result_identifier = future.result()
                    logger.info(f"Success: Processing task for [ {result_identifier} ] finished successfully.")
                except Exception as exc:
                    logger.error(f"Failure: Processing task for [ {target_identity} ] crashed: {exc}")
                    
def unwrap_single_file(
        infile: Path, 
        outfile: str, 
        width: int, 
        cm: str, 
        rowtile: int, 
        coltile: int, 
        rowoverlap: int, 
        coloverlap: int, 
        tile_nproc: int):
    """
    Helper function to process a single .int file using SNAPHU.
    This pure function stands outside any class to ensure flawless process serialization.
    """
    cmd = [
        "snaphu",
        str(infile),
        str(width),
        cm,
        "-o", str(outfile),
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

    logger.info(f"Executing: {infile.name} -> {Path(outfile).name}\nCommand: " + " ".join(cmd))
    
    try:
        #subprocess.run(cmd, check=True)
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return infile.name
    except subprocess.CalledProcessError as e:
        error_message = (
            f"SNAPHU processing failure on [ {infile.name} ]. Exit code: {e.returncode}\n"
            f"--- Captured SNAPHU Error Logs ---\n{e.stderr}----------------------------------"
        )
        raise RuntimeError(error_message)

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
    cost_mode: str = "SMOOTH",
    total_cpus: int | None = None
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
    total_cpus : int
        Manually restrict total usable workstation CPU cores to prevent memory saturation (default: auto)
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

    # Build queue profiles
    tiles_count = rowtile * coltile
    if nproc == 1:
        final_nproc = nproc
    else:
        final_nproc = min(nproc, tiles_count)

    task_items = []
    for f in files:
        outfile = os.path.join(output_folder, (f.stem + ".unw"))
        if os.path.exists(outfile):
            logger.info(f"Output target {outfile} already exists. Skipping.")
            continue
            
        task_items.append({
            "infile": f,
            "outfile": outfile,
            "width": width,
            "cm": cm,
            "rowtile": rowtile,
            "coltile": coltile,
            "rowoverlap": rowoverlap,
            "coloverlap": coloverlap,
            "tile_nproc": final_nproc
        })

    if not task_items:
        logger.info("All files in queue have already been successfully processed.")
        return

    # Call the generic Class Scheduler tool
    scheduler = WorkstationTaskScheduler(total_cpus=total_cpus)
    scheduler.execute_parallel_tasks(
        worker_function=unwrap_single_file,
        task_items=task_items,
        cores_per_task=final_nproc
    )

def run_batch_snaphu(
    rowtile: int = 1,
    coltile: int = 1,
    rowoverlap: int = 200,
    coloverlap: int = 200,
    nproc: int = 1,
    file_extension: str = ".int",
    cost_mode: str = "SMOOTH",
    total_cpus: int | None = None,
    config: str = 'config.yaml'
):
    """
    Batch unwrap using SNAPHU

    Parameters
    ----------
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
    total_cpus : int
        Manually restrict total usable workstation CPU cores to prevent memory saturation (default: auto)
    """
    from s1proc._config import load_config
    icfg,pcfg = load_config(config)
    batch_snaphu(
            input_folder=icfg.ifg_path,
            output_folder=icfg.unw_path,
            rsc_file=icfg.multilook_rsc_file,
            rowtile=rowtile,
            coltile=coltile,
            rowoverlap=rowoverlap,
            coloverlap=coloverlap,
            nproc=nproc,
            file_extension=file_extension,
            cost_mode=cost_mode,
            total_cpus=total_cpus)
    return
