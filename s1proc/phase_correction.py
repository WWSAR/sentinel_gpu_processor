import gc
import os
from pathlib import Path

import cupy as cp

from s1proc._log import set_logging_level, setup_logger
from s1proc.goldstein import (
    goldstein_filter_wrapper,
    goldstein_interpolation,
    save_filtered_stack,
)
from s1proc.phase_linking import (
    get_eigensar_chunks,
    phase_linking_solver,
    save_phase_linking_results,
)
from s1proc.tropo import _era5_correction, tropo_preproc
from s1proc.utils import IfgList, get_files

logger = setup_logger(name=__name__, level="INFO")


def phase_correction(
    ifg_path: str | None = None, config: str = "config.yaml", verbose: bool = False
):
    """
    Run phase correction for wrapped interferograms

    Parameters
    ----------
    ifg_path: str
        Interferograms to be corrected
    config: str
        Configuration file
    verbose: bool
        If True, set the logging level to DEBUG
    """
    if verbose:
        set_logging_level(logger, "DEBUG")

    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc

    mask_file = icfg.mask_file
    if ifg_path is None:
        ifg_path = icfg.ifg_path
    ifg_files = get_files(ifg_path, "int")
    ifg_list = IfgList(ifg_files)
    nifg = len(ifg_files)
    ndate = ifg_list.ndate
    logger.debug(f"Number of interferograms: {nifg}")

    if not os.path.exists(icfg.multilook_rsc_file):
        rsc = GeoCoordinates(icfg.rsc_file)
        rsc = rsc.take_look(pcfg.rowlook, pcfg.collook)
        rsc.save_as_rsc(icfg.multilook_rsc_file)
    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    logger.debug(f"Image shape: {nrow} x {ncol}")

    ifg_corr_path = Path(icfg.ifg_corr_path)
    ifg_corr_path.mkdir(parents=True, exist_ok=True)

    previous_output = ifg_files
    final_output = [ifg_corr_path / Path(f).name for f in ifg_files]
    current_output = []
    intermediate_files = []
    if cfg.tropo.enable:
        logger.info("Tropospheric noise correction")
        tropo_preproc(ifg_path, config, verbose)
        for ifg_file in previous_output:
            output_file = os.path.join(icfg.ifg_corr_path, os.path.basename(ifg_file))
            _era5_correction(
                ifg_file,
                output_file,
                nrow,
                ncol,
                cfg.tropo.parameters,
                cfg.proc.wavelength,
            )
            current_output.append(output_file)
    else:
        current_output = previous_output

    if cfg.filter.enable:
        fcfg = cfg.filter
        filter_method = fcfg.method.lower()
        if filter_method in ["eigensar", "goldstein"]:
            filtered_stack = goldstein_filter_wrapper(
                igrams=current_output,
                nrow=nrow,
                ncol=ncol,
                alpha=fcfg.parameters.goldstein_alpha,
                window_size=fcfg.parameters.window_size,
            )
        if filter_method == "eigensar":
            eigensar_first_round_output = ifg_corr_path / "eigensar_first_round.zarr"
            eigensar_chunk_size = get_eigensar_chunks(nrow, ncol, nifg, ndate)
            intermediate_files.append(eigensar_first_round_output)
            logger.info(f"EigenSAR phase linking chunk size: {eigensar_chunk_size}")
            filtered_stack = filtered_stack.rechunk({
                0: eigensar_chunk_size[0],
                1: eigensar_chunk_size[1],
                2: eigensar_chunk_size[2],
            })
            save_filtered_stack(
                filtered_stack,
                eigensar_first_round_output,
                output_format="zarr",
            )
            del filtered_stack
            gc.collect()
            cp.get_default_memory_pool().free_all_blocks()
            cp.get_default_pinned_memory_pool().free_all_blocks()
            eigensar_opt_phase_output = ifg_corr_path / "eigensar_opt_phase.zarr"
            opt_phase, gamma, ifg_list = phase_linking_solver(
                eigensar_first_round_output,
                mask_file,
                nrow,
                ncol,
                row_chunk=eigensar_chunk_size[0],
                ifg_list=ifg_list,
            )
            opt_phase = opt_phase.rechunk({
                0: nrow,
                1: ncol,
                2: 1,
            })
            gamma = gamma.rechunk({0: nrow, 1: ncol})
            intermediate_files.append(eigensar_opt_phase_output)
            save_phase_linking_results(
                opt_phase, gamma, eigensar_opt_phase_output, ifg_list
            )
            del opt_phase
            del gamma
            gc.collect()
            cp.get_default_memory_pool().free_all_blocks()
            cp.get_default_pinned_memory_pool().free_all_blocks()
            filtered_stack = goldstein_interpolation(
                eigensar_opt_phase_output,
                ifg_list,
                window_size=fcfg.parameters.window_size * 2,
                alpha=min(1, fcfg.parameters.goldstein_alpha * 2),
            )
            filtered_stack = filtered_stack.rechunk({0: nrow, 1: ncol, 2: 1})

        save_filtered_stack(filtered_stack, out_path=final_output)
        gc.collect()
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()
