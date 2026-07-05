from __future__ import annotations

import gc
import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Sequence

import cupy as cp
import dask
import dask.array as da
import numpy as np
import zarr
from dask.diagnostics import ProgressBar
from numpy.typing import NDArray

from s1proc._config import load_config
from s1proc._log import set_logging_level, setup_logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.sario import _store_attr
from s1proc.utils import IfgList, get_files

logger = setup_logger(__name__, level="INFO")


def eigensar_block(
    ifg_chunk: NDArray[np.complex64],  # (chunk_rows, chunk_cols, nifg)
    ref_indices: NDArray[np.int_],  # (nifg,)
    sec_indices: NDArray[np.int_],  # (nifg,)
    ndate: int,
    mask: NDArray[np.bool_] = None,
    correlation_vector: NDArray[np.float32] = None,  # (nifg,)
    block_info: dict = None,
) -> NDArray[np.float32]:  # (ndate, chunk_rows, chunk_cols)
    """
    Parameters
    ----------
    ifg_chunk: NDArray[np.complex64]
        A chunk of the interferogram stack (chunk_rows, chunk_cols, nifg)
    ref_indices: NDArray[np.int_]
        Indices of reference dates (nifg,)
    sec_indices: NDArray[np.int_]
        Indices of secondary dates (nifg,)
    ndate: int
        Number of unique dates
    mask: NDArray[np.bool_]
        Boolean mask (True = valid pixel)
    correlation_vector: NDArray[np.float32]
        Per-interferogram correlation coefficients (nifg,)
    block_info: dict
        Dask block information dictionary

    Returns
    -------
    NDArray[np.float32]
        The result of the phase linking operation.
    """
    # Create the correlation matrix
    chunk_row, chunk_col, nifg = ifg_chunk.shape
    array_loc = block_info[0]["array-location"]
    mask_chunk = mask[
        array_loc[0][0] : array_loc[0][1], array_loc[1][0] : array_loc[1][1]
    ]
    if np.all(mask_chunk == 0):
        return (np.zeros((ndate + 1, chunk_row, chunk_col), dtype=np.float32),)

    npixels = chunk_row * chunk_col
    d_ifg_chunk = cp.array(ifg_chunk.reshape(-1, nifg))  # npixels, nifg
    d_correlation = cp.array(correlation_vector)  # nifg

    d_corr_matrix = cp.zeros((npixels, ndate, ndate), dtype=cp.complex64)
    d_corr_matrix[:, cp.arange(ndate), cp.arange(ndate)] = 1.0  # diagonal elements

    d_ifg_chunk = d_ifg_chunk / (cp.abs(d_ifg_chunk) + 1e-8)
    d_weighted_ifg = d_ifg_chunk * d_correlation[None, :]
    d_pixel_idx = cp.arange(npixels)[:, None]
    d_corr_matrix[d_pixel_idx, ref_indices, sec_indices] = d_weighted_ifg
    d_corr_matrix[d_pixel_idx, sec_indices, ref_indices] = d_weighted_ifg.conj()

    # eigenvalue decomposition
    _, eigenvectors = cp.linalg.eigh(d_corr_matrix)

    # shape: (npixels, ndate)
    primary_eigenvector = eigenvectors[:, :, -1]
    primary_eigenvector = primary_eigenvector / (cp.abs(primary_eigenvector) + 1e-8)

    # reuse of d_corr_matrix to save memory
    # (npixels, ndate, ndate) = (npixels, ndate, 1) * (npixels, 1, ndate)
    # Reconstructed complex number at (:, i, j) is eigenvector[i].conj()*eigenvector[j]
    # This should be the conjugate of the complex value at the corresponding input
    # interferogram
    d_corr_matrix = (
        primary_eigenvector[:, :, None].conj() * primary_eigenvector[:, None, :]
    )
    # reuse of d_ifg_chunk to save memory
    d_ifg_chunk = d_ifg_chunk * d_corr_matrix[d_pixel_idx, ref_indices, sec_indices]

    d_gamma = cp.mean(cp.cos(cp.angle(d_ifg_chunk)), axis=1)  # shape: (npixels,)
    d_gamma_2d = d_gamma.reshape(chunk_row, chunk_col)  # (1, chunk_rows, chunk_cols)

    # shape: (chunk_rows, chunk_cols, ndate)
    d_phase = cp.angle(primary_eigenvector).reshape(chunk_row, chunk_col, ndate)

    # transpose to (ndate, chunk_rows, chunk_cols) for dask
    d_phase_t = cp.transpose(d_phase, (2, 0, 1))

    phase_np = cp.asnumpy(d_phase_t)
    gamma_np = cp.asnumpy(d_gamma_2d)
    gamma_np[~mask_chunk] = 0
    gamma_np = gamma_np[None, :, :]

    combined_output = np.concatenate([phase_np, gamma_np], axis=0)

    gc.collect()
    return combined_output


def phase_linking_solver(
    ifg_files: Sequence[str],
    mask_file: Path | str | None,
    out_path: Path | str,
    nrow: int,
    ncol: int,
    solver_func: Callable,
    row_chunk: int | None = None,
    col_chunk: int | None = None,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """
    Run phase linking algorithms (e.g., EMI, EigenSAR) on a stack of wrapped
    interferograms.

    Parameters
    ----------
    ifg_files : Sequence[str]
        Paths to wrapped interferograms (Complex64, .int)
    mask_file : str or None
        Boolean mask (True = valid pixel).
    out_path : Path or str
        Output directory.
    nrow : int
        Number of rows per interferogram.
    ncol : int
        Number of columns per interferogram.
    solver_func : Callable
        Per-chunk solver (a dask ``map_blocks``-compatible function).
    row_chunk : int or None
        Row chunk size for dask array.
    col_chunk : int or None
        Column chunk size for dask array.
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    from s1proc.sario import create_virtual_stack

    ifg_list = IfgList(ifg_files)
    logger.info(
        "Phase linking with %d interferograms, %d unique dates",
        len(ifg_files),
        ifg_list.ndate,
    )
    logger.info("Creating image stacks with dask")

    if row_chunk is None:
        row_chunk = int(max(1, np.ceil(512 * 512 / ncol)))  # default row chunk size
    logger.info(f"Using row_chunk={row_chunk}, col_chunk={ncol}" + " for dask array")

    mapper = create_virtual_stack(
        ifg_files,
        dtype=np.complex64,
        nrow=nrow,
        ncol=ncol,
        row_chunk=row_chunk,
        new_axis=2,
    )

    ifg_stack = da.from_zarr(mapper)
    ifg_stack = ifg_stack.rechunk({0: row_chunk, 1: ncol, 2: -1})
    logger.info(f"Total Chunks/Tasks: {ifg_stack.npartitions}")

    # Load mask
    logger.info("Load mask from %s", mask_file)
    if mask_file is not None:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    ref_indices, sec_indices = ifg_list.ref_sec_indices()

    correlation_vector = np.exp(-ifg_list.df.tempbl / 60.0).astype(np.float32)

    # --- Run the dask computation -------------------------------------------
    ndate = ifg_list.ndate
    result_chunks = (ndate + 1, *ifg_stack.chunks[0:2])
    res = da.map_blocks(
        solver_func,
        ifg_stack,
        dtype=np.float32,
        drop_axis=2,
        new_axis=0,
        chunks=result_chunks,
        block_info=True,
        mask=mask,
        correlation_vector=correlation_vector,
        ndate=ndate,
        ref_indices=ref_indices,
        sec_indices=sec_indices,
    )  # (ndate+1, nrow, ncol)

    opt_phase = res[:-1, :, :]
    gamma = res[-1, :, :]

    out_path = Path(out_path)
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True, exist_ok=True)

    write_phase_task = da.to_zarr(
        opt_phase, out_path, component="opt_phase", overwrite=True, compute=False
    )
    write_gamma_task = da.to_zarr(
        gamma, out_path, component="gamma", overwrite=True, compute=False
    )

    # --- Write to zarr ------------------------------------------------------
    # Write optimized phase and gamma to zarr using single-threaded scheduler
    # with dask.config.set(scheduler="single-threaded"):
    with ProgressBar():
        dask.compute(write_phase_task, write_gamma_task)

    # Write metadata to zarr group
    root = zarr.open(out_path, mode="a")
    if metadata:
        for key, value in metadata.items():
            _store_attr(root, key, value)
    if "date" not in metadata:
        _store_attr(root, "date", list(ifg_list.dates))

    logger.info("Phase linking computation complete.")


def run_phase_linking(
    ifg_path: Path | str | None = None,
    out_path: Path | str | None = None,
    config: Path | str = "config.yaml",
    verbose: bool = False,
) -> None:
    """
    Run phase linking algorithms (e.g., EMI, EigenSAR) on a stack of wrapped
    interferograms.

    Parameters
    ----------
    ifg_path : Path or str or None
        Path to the directory containing wrapped interferograms.
    out_path : Path or str or None
        Path to the output directory where results will be saved.
    config : Path or str
        Path to the configuration file (YAML format) containing parameters for
        the phase linking process.
    verbose : bool
        If True, enables verbose logging for debugging purposes.
    """

    if verbose:
        set_logging_level(logger, "DEBUG")

    cfg = load_config(config)
    icfg = cfg.io
    if ifg_path is None:
        ifg_files = get_files(icfg.ifg_corr_path, "int")
        if len(ifg_files) == 0:
            ifg_files = get_files(icfg.ifg_path, "int")
            if len(ifg_files) == 0:
                raise ValueError(
                    f"No interferogram files found in {icfg.ifg_corr_path} or "
                    + f"{icfg.ifg_path}"
                )
    else:
        ifg_files = get_files(ifg_path, "int")
        if len(ifg_files) == 0:
            raise ValueError(f"No interferogram files found in {ifg_path}")

    if out_path is None:
        out_path = Path(icfg.proc_path) / "phase_linking" / "opt_phase.zarr"
    out_path.mkdir(parents=True, exist_ok=True)

    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon

    metadata = {
        "method": "eigensar",
    }

    phase_linking_solver(
        ifg_files=ifg_files,
        mask_file=icfg.mask_file,
        out_path=out_path,
        nrow=nrow,
        ncol=ncol,
        solver_func=eigensar_block,
        metadata=metadata,
    )
