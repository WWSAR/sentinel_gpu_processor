import glob
import os
import subprocess
from typing import List, Optional, Tuple

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


def _reorder_tasks_for_cache_locality(
    task_items: List[Tuple[str, str, str]],
) -> List[Tuple[str, str, str]]:
    """
    Reorder ``(ref_slc, sec_slc, out_ifg)`` triples so that
    consecutive entries share an image whenever possible, maximising
    hits in the daemon's single-slot host-memory cache.

    Each task is an undirected edge between two image nodes.  The
    algorithm repeatedly drains all remaining tasks that touch the
    current cached image, then uses the *other* image of the last
    drained task as the next cache key.  Original ref/sec column
    ordering is never swapped.

    Parameters
    ----------
    task_items : List[Tuple[str, str, str]]
        Unsorted task triples.

    Returns
    -------
    List[Tuple[str, str, str]]
        Reordered triples with original ref/sec columns intact.
    """
    from collections import defaultdict

    n = len(task_items)
    if n <= 1:
        return list(task_items)

    # -- Image → set of still-unconsumed task indices --
    img_to_indices: dict[str, set[int]] = defaultdict(set)
    for i, (ref_slc, sec_slc, _out) in enumerate(task_items):
        img_to_indices[ref_slc].add(i)
        img_to_indices[sec_slc].add(i)

    remaining: set[int] = set(range(n))
    sorted_tasks: list[tuple[str, str, str]] = []

    # pick the first remaining index as the next seed
    def _pop_first_remaining() -> int:
        idx = min(remaining)
        remaining.discard(idx)
        return idx

    # remove a task index from the adjacency index entirely
    def _remove_index(idx: int) -> None:
        r, s, _ = task_items[idx]
        img_to_indices[r].discard(idx)
        img_to_indices[s].discard(idx)

    # ---- Bootstrap ----
    seed = _pop_first_remaining()
    _remove_index(seed)
    sorted_tasks.append(task_items[seed])
    cached = task_items[seed][0]  # reference image of the seed task

    cold_starts = 0

    while remaining:
        # -- Collect every remaining task that touches *cached* --
        batch: list[int] = []
        for idx in list(img_to_indices.get(cached, ())):
            if idx in remaining:
                batch.append(idx)

        if batch:
            # Move the entire batch into sorted_tasks.
            # The daemon processes them sequentially; each task is
            # served from cache because its ref or sec matches
            # *cached*.
            for idx in batch:
                remaining.discard(idx)
                _remove_index(idx)
                sorted_tasks.append(task_items[idx])

            # Use the *other* image of the last task in the batch
            # as the new cache key for the next iteration.
            last_r, last_s, _ = sorted_tasks[-1]
            cached = last_s if last_r == cached else last_r
        else:
            # Dead end — no remaining task touches *cached*.
            # Pick the first remaining task as a cold-start seed.
            cold_starts += 1
            idx = _pop_first_remaining()
            _remove_index(idx)
            sorted_tasks.append(task_items[idx])
            cached = task_items[idx][0]

    # -- Log statistics --
    # Count how many times the ref column changes across the sequence
    # (this equals the number of reference-image cache loads).
    predicted_loads = 1  # first task always triggers a load
    prev_ref: Optional[str] = None
    for t in sorted_tasks:
        if t[0] != prev_ref:
            predicted_loads += 1
            prev_ref = t[0]

    logger.info(
        "I/O Cache reordering: %d tasks, %d predicted cache loads "
        "(optimal clustering: %.0f%% reuse).",
        n,
        predicted_loads,
        (1.0 - predicted_loads / max(n, 1)) * 100,
    )
    if cold_starts > 0:
        logger.info(
            "  Cold-start cluster jumps: %d (no task in the remaining "
            "pool touched the current cached image).",
            cold_starts,
        )

    return sorted_tasks


def _run_crossmul_daemon(
    task_items: List[Tuple[str, str, str]],
    rowlook: int,
    collook: int,
    out_float: bool,
    io_workers: int,
    cpu_workers: int,
    gpu_workers: int,
    streams_per_gpu: int,
    max_slots: int,
    verbose: bool,
) -> Tuple[int, int]:
    """
    Launch the ``crossmul_daemon`` long-running GPU processor.

    Burst-pair paths are written to a temporary file and passed to the
    daemon via ``--tasks-file``.  Progress and completion status are
    read from daemon **stdout** line-by-line.

    Parameters
    ----------
    task_items : List[Tuple[str, str, str]]
        (ref_slc, sec_slc, out_ifg) triples.
    rowlook : int
        Row multi-looking factor.
    collook : int
        Column multi-looking factor.
    out_float : bool
        Write phase-only (float) output.
    io_workers: int
        Number of threads to read SLC data.  Pass ``-1`` for auto-tune.
    cpu_workers : int
        Number of CPU crop worker threads inside the daemon.
        Pass ``-1`` for auto-tune.
    gpu_workers : int
        Number of GPU devices to use inside the daemon.
        Pass ``-1`` for auto-detect.
    max_slots : int
        Number of pre-allocated pinned-memory buffer slots.
        Pass ``-1`` for auto-tune.
    streams_per_gpu : int
        Number of internal CUDA execution lanes per GPU.
        Pass ``-1`` for auto-tune.
    verbose: bool
        Print more debug messages

    Returns
    -------
    succeeded : int
        Number of burst pairs that completed successfully.
    failed : int
        Number of burst pairs that failed.
    """
    import tempfile

    daemon_bin = get_bin_path("crossmul_daemon")

    # -- Greedy DFS graph traversal: reorder tasks so that consecutive
    #    entries share at least one image whenever possible, maximising
    #    hits in the daemon's single-slot reference-image cache.
    #
    #    Each task is treated as an undirected edge between two image
    #    nodes.  The traversal walks the graph depth-first, backtracking
    #    when the current node has no remaining incident edges.
    #    Original ref/sec column ordering is preserved.
    task_items = _reorder_tasks_for_cache_locality(task_items)

    # -- Write tasks to a temporary file --
    #    Use delete=False because the daemon process reads the file
    #    independently.  The file is cleaned up after the subprocess exits.
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        prefix="crossmul_tasks_",
        delete=False,
    )
    tasks_path = tmp.name
    for ref_slc, sec_slc, out_ifg in task_items:
        tmp.write(f"{ref_slc} {sec_slc} {out_ifg}\n")
    tmp.close()

    cmd = [
        daemon_bin,
        "--rowlook",
        str(rowlook),
        "--collook",
        str(collook),
        "--io-workers",
        str(io_workers),
        "--cpu-workers",
        str(cpu_workers),
        "--gpu-workers",
        str(gpu_workers),
        "--streams-per-gpu",
        str(streams_per_gpu),
        "--max-slots",
        str(max_slots),
        "--tasks-file",
        tasks_path,
    ]
    if out_float:
        cmd.append("--out-float")
    if verbose:
        cmd.append("--verbose")

    logger.info("Starting crossmul_daemon: %s", " ".join(cmd))
    logger.info(
        "Tasks file: %s (%d tasks, max_slots=%d, io_workers=%d, "
        + "cpu_workers=%d, gpus_workers=%d, streams_per_gpu=%d).",
        tasks_path,
        len(task_items),
        max_slots,
        io_workers,
        cpu_workers,
        gpu_workers,
        streams_per_gpu,
    )

    with open("crossmul_daemon_stderr.log", "w", encoding="utf-8") as f_err:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=f_err,
            encoding="utf-8",
            errors="ignore",
            text=True,
            bufsize=1,
        )

        # -- Read daemon stdout (progress / completion lines) --
        succeeded = 0
        failed = 0
        total = len(task_items)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if line.startswith("OK "):
                succeeded += 1
                logger.debug("[daemon] %s", line)
            elif line.startswith("FAIL "):
                failed += 1
                logger.error("[daemon] %s", line)
            elif line.startswith("PROGRESS "):
                parts = line.split()
                if len(parts) >= 5:
                    done = int(parts[1])
                    _failed = int(parts[2])
                    _total = int(parts[3])
                    elapsed_s = int(parts[4])
                    rate = done / max(elapsed_s, 1)
                    logger.info(
                        "Daemon progress: %d/%d done, %d failed, "
                        "elapsed %d s (%.1f pairs/s)",
                        done,
                        _total,
                        _failed,
                        elapsed_s,
                        rate,
                    )
            elif line.startswith("SUMMARY "):
                parts = line.split()
                if len(parts) >= 5:
                    succeeded = int(parts[1])
                    failed = int(parts[2])
                    elapsed_s = int(parts[4])
                    rate = total / max(elapsed_s, 1)
                    logger.info(
                        "Daemon summary: %d succeeded, %d failed, "
                        "total %d tasks in %d s (%.1f pairs/s)",
                        succeeded,
                        failed,
                        total,
                        elapsed_s,
                        rate,
                    )

        retcode = proc.wait()

    if retcode != 0 or failed > 0:
        logger.error(
            "Daemon exited with errors. Check "
            + "crossmul_daemon_stderr.log for details."
        )

    # -- Clean up the temporary tasks file --
    try:
        os.unlink(tasks_path)
    except OSError:
        pass

    if retcode != 0:
        logger.warning("crossmul_daemon exited with code %d.", retcode)

    return succeeded, failed


def interfere(
    img_pair_file: str,
    slc_path: str,
    rscfile: str,
    small_rsc_file: str,
    ifg_path: str = "igrams",
    rowlook: int = 1,
    collook: int = 1,
    out_float: bool = False,
    io_workers: int = -1,
    cpu_workers: int = -1,
    gpu_workers: int = -1,
    streams_per_gpu: int = -1,
    max_slots: int = -1,
    verbose: bool = False,
):
    """
    Form interferograms from a subswath list.

    Parameters
    ----------
    img_pair_file : str
        File containing pairs of dates to form interferogram.
    slc_path : str
        Directory of SLC files.
    rscfile : str
        rsc file.
    small_rsc_file : str
        Multilooked rsc file.
    ifg_path : str
        Directory to save interferograms.
    rowlook : int
        Number of looks in row direction.
    collook : int
        Number of looks in column direction.
    out_float : bool
        Output float rather than cpx images.
    io_workers : int
        Number of threads used for concurrent image reading.
        Set to ``-1`` (default) to let the daemon auto-tune this value
        based on available hardware and task dimensions.
    cpu_workers : int
        Number of CPU threads for burst cropping (Stage 2).
        Set to ``-1`` (default) for hardware-aware automatic tuning.
    gpu_workers : int
        Number of GPUs to use for interferogram generation.
        Set to ``-1`` (default) for automatic detection via CUDA.
    streams_per_gpu : int
        Number of internal CUDA execution lanes per GPU in the daemon.
        Set to ``-1`` (default) for automatic tuning based on VRAM
        headroom and burst-pair dimensions.
    max_slots : int
        Number of pre-allocated pinned-memory buffer slots in the
        daemon.  Set to ``-1`` (default) for automatic tuning from
        GPU count and streams-per-GPU.
    verbose: bool
        Print more debug information
    """
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
        # skip a set of bursts if all their associated interferograms
        # already exist
        if not all([os.path.exists(b[2]) for b in _burst_pairs]):
            burst_pairs.extend(_burst_pairs)

    n_pair = len(burst_pairs)
    n_ifg = len(burst_pair_map)
    if n_pair == 0:
        logger.warning("Did not find any burst pairs.")
        return
    logger.info("Found %d burst pairs in %d interferograms.", n_pair, n_ifg)

    # -- Validate all-or-nothing tuning parameter contract --
    tuning_params = {
        "io_workers": io_workers,
        "cpu_workers": cpu_workers,
        "gpu_workers": gpu_workers,
        "streams_per_gpu": streams_per_gpu,
        "max_slots": max_slots,
    }
    auto_tune = all(v == -1 for v in tuning_params.values())
    manual = all(isinstance(v, int) and v > 0 for v in tuning_params.values())

    if not (auto_tune or manual):
        import sys

        print(
            "[FATAL] Invalid parameter configuration. Please either omit all "
            "tuning arguments for hardware-managed auto-tuning, or provide the "
            "complete set of parameters (--io-workers, --cpu-workers, "
            "--streams-per-gpu, --gpu-workers, --max-slots). "
            "Partial overrides are not allowed.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # -- Filter out already-completed burst pairs --
    task_items: List[Tuple[str, str, str]] = []
    for ref_slc, sec_slc, out_ifg in burst_pairs:
        if os.path.exists(out_ifg):
            continue
        task_items.append((ref_slc, sec_slc, out_ifg))
    del burst_pairs

    if not task_items:
        logger.info("All burst-pair interferograms already exist; nothing to do.")
        for outfile in tqdm(burst_pair_map, desc="stitching"):
            stitch(burst_pair_map[outfile], outfile, out_float)
        logger.info("All interferograms are generated.")
        return

    # -- Launch daemon --
    succeeded, failed = _run_crossmul_daemon(
        task_items,
        rowlook=rowlook,
        collook=collook,
        out_float=out_float,
        io_workers=io_workers,
        cpu_workers=cpu_workers,
        gpu_workers=gpu_workers,
        streams_per_gpu=streams_per_gpu,
        max_slots=max_slots,
        verbose=verbose,
    )

    if failed > 0:
        logger.warning(
            "%d burst pair(s) failed.  Check log output for FAIL lines.", failed
        )

    # -- Stitch per-burst interferograms into full subswath images --
    for outfile in tqdm(burst_pair_map, desc="stitching"):
        stitch(burst_pair_map[outfile], outfile, out_float)
    logger.info("All interferograms are generated.")


def run_interfere(
    config: str = "config.yaml",
    verbose: bool = False,
):
    """
    Form interferograms from a subswath list.

    Parameters
    ----------
    config : str
        Configuration file.
    verbose : bool
        Set logging level to 'DEBUG'.
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
        io_workers=pcfg.io_workers if pcfg.io_workers is not None else -1,
        cpu_workers=pcfg.cpu_workers if pcfg.cpu_workers is not None else -1,
        gpu_workers=pcfg.gpu_workers if pcfg.gpu_workers is not None else -1,
        streams_per_gpu=(
            pcfg.streams_per_gpu if pcfg.streams_per_gpu is not None else -1
        ),
        max_slots=pcfg.max_slots if pcfg.max_slots is not None else -1,
        verbose=verbose,
    )
    return
