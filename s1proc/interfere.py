import glob
import os
import subprocess
import threading
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


def _run_crossmul_daemon(
    task_items: List[Tuple[str, str, str]],
    rowlook: int,
    collook: int,
    out_float: bool,
    ngpu: int,
    max_slots: int,
) -> Tuple[int, int]:
    """
    Launch the ``crossmul_daemon`` long-running GPU processor.

    The daemon is spawned as a subprocess.  Burst-pair paths are
    written to its **stdin** (one per line); progress and completion
    status are read from its **stdout** in a background monitor
    thread.

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
    ngpu : int
        Number of GPU devices to use inside the daemon.
    max_slots : int
        Number of pre-allocated pinned-memory buffer slots.

    Returns
    -------
    succeeded : int
        Number of burst pairs that completed successfully.
    failed : int
        Number of burst pairs that failed.
    """
    daemon_bin = get_bin_path("crossmul_daemon")
    cmd = [
        daemon_bin,
        "--rowlook",
        str(rowlook),
        "--collook",
        str(collook),
        "--max-slots",
        str(max_slots),
        "--gpus",
        str(ngpu),
    ]
    if out_float:
        cmd.append("--out-float")

    logger.info("Starting crossmul_daemon: %s", " ".join(cmd))
    logger.info(
        "Feeding %d task(s) to daemon (max_slots=%d, ngpu=%d).",
        len(task_items),
        max_slots,
        ngpu,
    )

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )

    # -- Write task list to daemon's stdin in a background thread --
    #    (prevents deadlock if daemon's stderr pipe fills while we
    #     are blocked writing to stdin)
    def _feed_stdin():
        assert proc.stdin is not None
        for ref_slc, sec_slc, out_ifg in task_items:
            proc.stdin.write(f"{ref_slc} {sec_slc} {out_ifg}\n")
        proc.stdin.close()

    stdin_thread = threading.Thread(target=_feed_stdin, daemon=True)
    stdin_thread.start()

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

    # -- Drain stderr for diagnostics --
    stdin_thread.join(timeout=5)
    assert proc.stderr is not None
    stderr_text = proc.stderr.read()
    if stderr_text:
        for err_line in stderr_text.strip().splitlines():
            logger.debug("[daemon|stderr] %s", err_line)

    retcode = proc.wait()
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
    ngpu: int | None = None,
    max_slots: int | None = None,
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
    ngpu : int or None
        Number of GPUs used for interferogram generation.  If None,
        auto-detected via nvidia-smi.
    max_slots : int or None
        Number of pre-allocated pinned-memory buffer slots in the
        daemon.  Defaults to ``ngpu * 2`` when *None*.
    """
    from s1proc.utils import _detect_gpu_count

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
    with open("burst_pairs.txt", "w") as f:
        for burst_pair in burst_pairs:
            f.write(" ".join(burst_pair) + "\n")
    n_pair = len(burst_pairs)
    n_ifg = len(burst_pair_map)
    if n_pair == 0:
        logger.warning("Did not find any burst pairs.")
        return
    logger.info("Found %d burst pairs in %d interferograms.", n_pair, n_ifg)

    # -- GPU / slot validation --
    max_ngpu = _detect_gpu_count()
    if ngpu is None:
        ngpu = max_ngpu
    elif ngpu > max_ngpu:
        logger.warning(
            "User-specified GPUs (%d) > available (%d); clamping to %d.",
            ngpu,
            max_ngpu,
            max_ngpu,
        )
        ngpu = max_ngpu

    if max_slots is None:
        max_slots = ngpu * 2
    if max_slots < ngpu:
        logger.warning(
            "max_slots (%d) < ngpu (%d); raising to %d.",
            max_slots,
            ngpu,
            ngpu,
        )
        max_slots = ngpu

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

    # -- Launch daemon —
    succeeded, failed = _run_crossmul_daemon(
        task_items,
        rowlook=rowlook,
        collook=collook,
        out_float=out_float,
        ngpu=ngpu,
        max_slots=max_slots,
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
        ngpu=pcfg.ngpu,
        max_slots=pcfg.max_slots,
    )
    return
