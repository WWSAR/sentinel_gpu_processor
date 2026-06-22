import glob
import os
import subprocess
from typing import List, Tuple

import numpy as np
from tqdm.auto import tqdm

from s1proc import geocoordinates, get_bin_path
from s1proc._log import set_logging_level, setup_logger
from s1proc.sario import BurstGroup, CroppedImage, Subswath

logger = setup_logger(name=__name__, level="INFO")


def match_bursts(
    ref_subswath: Subswath, sec_subswath: Subswath
) -> List[Tuple[str, str]]:
    """
    Match bursts in two subswaths to create interferometric image pairs

    Parameters
    ----------
    ref_subswath: Subswath
        Reference subswath
    sec_subswath: Subswath
        Secondary subswath

    Returns
    -------
    burst_pairs: List[Tuple[str,str]]
       List of burst pairs
    """
    burst_pairs = []
    if ref_subswath.is_empty() or sec_subswath.is_empty():
        return burst_pairs
    sec_tops = np.array([b.top for b in sec_subswath.bursts])
    sec_bottoms = np.array([b.bottom for b in sec_subswath.bursts])
    for ref_burst in ref_subswath.bursts:
        ref_top = ref_burst.top
        ref_bottom = ref_burst.bottom
        idx_diff = np.abs(sec_tops - ref_top) + np.abs(sec_bottoms - ref_bottom)
        best_match = np.argmin(idx_diff)
        burst_pairs.append((ref_burst.data, sec_subswath.bursts[best_match].data))
        logger.debug(burst_pairs[-1])
    return burst_pairs


def interfere_subswath(
    ref_subswath: Subswath, sec_subswath: Subswath, ifg_path: str
) -> List[Tuple[str, str, str]]:
    """
    Form an interferogram from two subswath geo-coded SLC images

    Parameters
    ----------
    ref_subswath: Subswath
        Reference subswath
    sec_subswath: Subswath
        Secondary subswath
    ifg_path: str
        Directory to save interferograms

    Returns
    -------
    burst_pairs: List[Tuple[str,str,str]]
        List of tuples, each tuple contains reference/secondary burst SLCs and
        the output interferogram
    """
    _burst_pairs = match_bursts(ref_subswath, sec_subswath)
    burst_pairs = []
    for main_img_file, sec_img_file in _burst_pairs:
        basename1 = os.path.basename(main_img_file)
        name1, _ = os.path.splitext(basename1)
        basename2 = os.path.basename(sec_img_file)
        name2, _ = os.path.splitext(basename2)
        outfile = os.path.join(ifg_path, name1 + "_" + name2 + ".int")
        burst_pairs.append((main_img_file, sec_img_file, outfile))
    return burst_pairs


def stitch_subswath(burst_ifgs, out_float):
    subswath = Subswath(burst_ifgs)
    left, top, right, bottom = subswath.bounds()
    mean_phase_diff = 0.0
    # unique_ids are used to check if the reference and secondary bursts used
    # to form the current interferogram are the same as those for the previous
    # interferogram
    prev_unique_idx1 = None
    prev_unique_idx2 = None
    for i, burst in enumerate(subswath.bursts):
        basename = os.path.basename(burst.data)
        words = basename.split("_")
        unique_idx1 = words[1] + words[2]
        unique_idx2 = words[7] + words[8]

        if i == 0:
            ifg_data = burst.load_data(left, top, right, bottom)
            prev_unique_idx1 = unique_idx1
            prev_unique_idx2 = unique_idx2
            continue

        old_data = ifg_data[
            burst.top - top : burst.bottom - top, burst.left - left : burst.right - left
        ]
        new_data = burst.load_data(burst.left, burst.top, burst.right, burst.bottom)
        if out_float:
            replace_mask = (old_data == 0) & (new_data != 0)
        else:
            replace_mask = (old_data.real == 0) & (new_data.real != 0)

        # calculate overlap mask between bursts
        overlap_mask = None
        if unique_idx1 != prev_unique_idx1 or unique_idx2 != prev_unique_idx2:
            if out_float:
                overlap_mask = (old_data != 0) & (new_data != 0)
            else:
                overlap_mask = (old_data.real != 0) & (new_data != 0)
        prev_unique_idx1 = unique_idx1
        prev_unique_idx2 = unique_idx2

        if overlap_mask is not None and np.any(overlap_mask):
            if out_float:
                phase_diff = np.exp(
                    1j * (-old_data[overlap_mask] + new_data[overlap_mask])
                )
            else:
                phase_diff = np.conj(old_data[overlap_mask]) * new_data[overlap_mask]

            # more robust than just computing the mean phase difference
            mean_phase_diff = np.angle(np.mean(phase_diff))
            phase_diff = phase_diff * np.exp(-1j * mean_phase_diff)
            med_phase_diff = np.median(np.angle(phase_diff))
            mean_phase_diff += med_phase_diff

        if mean_phase_diff != 0:
            logger.debug(f"mean phase offset: {mean_phase_diff} rad")
            if out_float:
                new_data = new_data - mean_phase_diff
                new_data[new_data > np.pi] -= 2 * np.pi
                new_data[new_data < -np.pi] += 2 * np.pi
            else:
                new_data = new_data * np.exp(-1j * mean_phase_diff)
        old_data[replace_mask] = new_data[replace_mask]

    ifg = CroppedImage(
        subswath.nrow0, subswath.ncol0, left, top, right, bottom, ifg_data
    )
    for burst_ifg in burst_ifgs:
        os.remove(burst_ifg)
    return ifg


def stitch(burst_pairs, outfile, out_float):
    subswath_ifgs = []
    for i in range(1, 4):
        _burst_pairs = [b for b in burst_pairs if (f"iw{i}" in b) and os.path.exists(b)]
        if len(_burst_pairs) > 0:
            subswath_ifgs.append(stitch_subswath(_burst_pairs, out_float))
    if len(subswath_ifgs) == 0:
        logger.warning(f"Empty subswaths for {outfile}")
        return
    nrow0, ncol0 = subswath_ifgs[0].nrow0, subswath_ifgs[0].ncol0
    if out_float:
        mmap_arr = np.memmap(outfile, dtype=np.float32, mode="w+", shape=(nrow0, ncol0))
    else:
        mmap_arr = np.memmap(
            outfile, dtype=np.complex64, mode="w+", shape=(nrow0, ncol0)
        )
    for subswath_ifg in subswath_ifgs:
        old_data = mmap_arr[
            subswath_ifg.top : subswath_ifg.bottom,
            subswath_ifg.left : subswath_ifg.right,
        ]
        new_data = subswath_ifg.data
        if out_float:
            replace_mask = new_data != 0
        else:
            replace_mask = new_data.real != 0
        old_data[replace_mask] = new_data[replace_mask]
    mmap_arr.flush()


def interfere_single_scene(
    ref_burst_group: BurstGroup, sec_burst_group: BurstGroup, ifg_path: str
) -> List[Tuple[str, str, str]]:
    """
    Form an interferogram for a single scene

    Parameters
    ----------
    ref_burst_group:
        bursts of the reference image
    sec_burst_group:
        bursts of the secondary image
    ifg_path: str
        Directory to save interferograms

    Returns
    -------
    burst_pairs:
        List of tuples, each tuple contains reference/secondary burst SLCs and
        the output interferogram
    """
    burst_pairs = []
    for i in range(3):
        burst_pairs.extend(
            interfere_subswath(
                ref_burst_group.subswaths[i], sec_burst_group.subswaths[i], ifg_path
            )
        )
    return burst_pairs


def parse_fname(fn: str) -> Tuple[str, str]:
    basename = os.path.basename(fn)
    date = basename[0:8]
    data_id = basename[9:20]
    return date, data_id


def crossmul_wrapper(
    burst_pair_file: str,
    rowlook: int,
    collook: int,
    out_float: bool,
    gpu_device: int | None = None,
) -> str:
    """
    Python wrapper for the CUDA-version crossmul

    Parameters
    ----------
    burst_pair_file: str
        The file containing pairs of bursts to form burst-level
        burst-level interferograms
    rowlook: int
        Number of looks along the row direction
    collook: int
        Number of looks along the column direction
    out_float: bool
        Only save the interferometric phase (ignoring amplitude)
    gpu_device: int | None  = None
        GPU device to run crossmul. If None, let the system to decide
        which GPU to use

    Returns
    -------
    result_identifier: str
        A message to be shown by the GPU task scheduler
    """
    crossmul = get_bin_path("crossmul")
    cmd = [
        crossmul,
        burst_pair_file,
        str(rowlook),
        str(collook),
        "1" if out_float else "0",
    ]
    if gpu_device is not None:
        cmd += ["--gpu", str(gpu_device)]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return f"Interferogram generation for {burst_pair_file}"


def interfere(
    img_pair_file: str,
    slc_path: str,
    rscfile: str,
    small_rsc_file: str,
    ifg_path: str = "igrams",
    rowlook: int = 1,
    collook: int = 1,
    out_float: bool = False,
    ngpu: int | None = None,
    task_per_gpu: int = 1,
):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    img_pair_file: str
        File containing pairs of dates to form interferogram
    slc_path: str
        Directory of SLC files
    rscfile: str
        rsc file
    small_rsc_file: str
        Multilooked rsc file
    ifg_path: str
        Directory to save interferograms
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    out_float: bool
        Output float rather than cpx images
    ngpu: int
        Number of GPUs used for interferogram generation. If None, set to
        the total number of GPUs on current machine
    task_per_gpu: int
        Number of tasks running on each GPU in parallel
    """
    from s1proc.utils import GpuTaskScheduler, get_gpu_count

    os.makedirs(ifg_path, exist_ok=True)
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook, collook)
    if os.path.dirname(small_rsc_file):
        os.makedirs(os.path.dirname(small_rsc_file), exist_ok=True)
    rsclook.save_as_rsc(small_rsc_file)

    ref_dates = []
    sec_dates = []
    date_burst_map = {}
    with open(os.path.join(ifg_path, img_pair_file), "r") as f:
        for line in f.readlines():
            words = line.split()
            ref_date = words[0]
            sec_date = words[1]
            ref_dates.append(ref_date)
            sec_dates.append(sec_date)
            if ref_date not in date_burst_map:
                burst_files = glob.glob(os.path.join(slc_path, f"{ref_date}*.gslc"))
                burst_group = BurstGroup(burst_files)
                date_burst_map[ref_date] = burst_group
            if sec_date not in date_burst_map:
                burst_files = glob.glob(os.path.join(slc_path, f"{sec_date}*.gslc"))
                burst_group = BurstGroup(burst_files)
                date_burst_map[sec_date] = burst_group

    # match all burst pairs
    burst_pairs = []
    burst_pair_map = {}
    for ref_date, sec_date in zip(ref_dates, sec_dates):
        ref_burst_group = date_burst_map[ref_date]
        sec_burst_group = date_burst_map[sec_date]
        outfile = os.path.join(ifg_path, f"{ref_date}_{sec_date}.int")
        if os.path.exists(outfile):
            continue
        _burst_pairs = interfere_single_scene(
            ref_burst_group, sec_burst_group, ifg_path
        )
        burst_pair_map[outfile] = [b[2] for b in _burst_pairs]
        # skip a set of bursts if all their associated interferograms already
        # exist
        if not all([os.path.exists(b[2]) for b in _burst_pairs]):
            burst_pairs.extend(_burst_pairs)

    # Deduce appropriate parameters for GpuTaskScheduler
    n_pair = len(burst_pairs)
    n_ifg = len(burst_pair_map)
    if n_pair == 0:
        logger.warning("Did not find any burst pairs.")
        return
    logger.info(f"Find {n_pair} burst pairs in {n_ifg} interferograms.")

    # Run crossmul to form interferograsm
    max_ngpu = get_gpu_count()
    if ngpu is None:
        ngpu = max_ngpu
    elif ngpu > max_ngpu:
        logger.warning(
            f"User-specified number of GPUs ({ngpu}) exceeds the number of "
            + f"available GPUs on this machine ({max_ngpu})."
        )
        ngpu = max_ngpu
    if task_per_gpu is None:
        task_per_gpu = 1
    if task_per_gpu <= 0:
        raise ValueError("task_per_gpu must be positive.")

    n_sublist = ngpu * task_per_gpu
    n_sublist = np.minimum(n_sublist, n_pair)
    logger.info(f"Divide burst pairs into {n_sublist} groups.")
    n_pair_per_sublist = int(np.ceil(n_pair / n_sublist))
    logger.info(f"Each group contains {n_pair_per_sublist} to process")
    task_items = []
    for i in range(n_sublist):
        burst_pair_file = os.path.join(ifg_path, f"burst_pair_list_{i}.txt")
        with open(burst_pair_file, "w") as f:
            start_idx = n_pair_per_sublist * i
            end_idx = int(np.minimum(n_pair_per_sublist * (i + 1), n_pair))
            for i in range(start_idx, end_idx - 1):
                f.write(" ".join(burst_pairs[i]) + "\n")
            f.write(" ".join(burst_pairs[end_idx - 1]))
        task_item = {
            "burst_pair_file": burst_pair_file,
            "rowlook": rowlook,
            "collook": collook,
            "out_float": out_float,
        }
        task_items.append(task_item)
    del burst_pairs

    gpu_task_scheduler = GpuTaskScheduler(ngpu)
    gpu_task_scheduler.execute_parallel_tasks(
        crossmul_wrapper,
        task_items=task_items,
        task_per_gpu=task_per_gpu,
        identifier="burst_pair_file",
    )

    for outfile in tqdm(burst_pair_map, desc="stitching"):
        stitch(burst_pair_map[outfile], outfile, out_float)
    logger.info("All interferograms are generated.")


def run_interfere(
    config: str = "config.yaml",
    verbose: bool = False,
):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    config: str
        Configuration file
    verbose: bool
        Set logging level to 'DEBUG'
    """
    from s1proc._config import load_config

    if verbose:
        set_logging_level(logger, "DEBUG")

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    interfere(
        img_pair_file=icfg.img_pair_file,
        slc_path=icfg.slc_path,
        rscfile=icfg.rsc_file,
        small_rsc_file=icfg.multilook_rsc_file,
        ifg_path=icfg.ifg_path,
        rowlook=pcfg.rowlook,
        collook=pcfg.collook,
        out_float=False,
        ngpu=pcfg.ngpu,
        task_per_gpu=pcfg.task_per_gpu,
    )
    return
