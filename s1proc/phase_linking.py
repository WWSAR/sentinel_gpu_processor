from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Callable, Dict, Sequence

import cupy as cp
import dask.array as da
import numpy as np
import zarr
from dask.diagnostics import ProgressBar
from numpy.typing import NDArray

from s1proc._config import load_config
from s1proc._log import setup_logger
from s1proc.sario import _store_attr
from s1proc.utils import IfgList, _get_mask_chunk, get_files

logger = setup_logger(__name__, level="INFO")


def eigensar_block(
    ifg_chunk: NDArray[np.complex64],  # (nifg, chunk_rows, chunk_cols)
    correlation_chunk: NDArray[np.float32],  # (nifg, 1)
    ref_indices: NDArray[np.int_],  # (nifg,)
    sec_indices: NDArray[np.int_],  # (nifg,)
    ndate: int,
    mask: NDArray[np.bool_] = None,  # (nrow, ncol)
    block_info: dict = None,
) -> NDArray[np.float32]:  # (ndate, chunk_rows, chunk_cols)
    """
    Parameters
    ----------
    ifg_chunk: NDArray[np.complex64]
        A chunk of the interferogram stack (nifg, chunk_rows, chunk_cols)
    correlation_chunk: NDArray[np.float32]
        Correlation coefficients (nifg)
    ref_indices: NDArray[np.int_]
        Indices of reference dates
    sec_indices: NDArray[np.int_]
        Indices of secondary dates
    ndate: int
        Number of unique dates
    mask: NDArray[np.bool_]
        Boolean mask (True = valid pixel)
    block_info: dict
        Dask block information dictionary
    kwargs: dict
        Additional keyword arguments for the solver function

    Returns
    -------
    NDArray[np.float32]
        The result of the phase linking operation.
    """
    # Create the correlation matrix
    chunk_row, chunk_col, nifg = ifg_chunk.shape
    mask_chunk = _get_mask_chunk(block_info, mask, (chunk_row, chunk_col))
    if np.all(mask_chunk == 0):
        return np.zeros((ndate, chunk_row, chunk_col), dtype=np.complex64)

    npixels = chunk_row * chunk_col
    d_ifg_chunk = cp.array(ifg_chunk.reshape(-1, nifg))  # npixels, nifg
    d_correlation_chunk = cp.array(correlation_chunk).flatten()  # nifg

    corr_matrix = cp.zeros((npixels, ndate, ndate), dtype=cp.complex64)
    corr_matrix[:, :, cp.arange(ndate), cp.arange(ndate)] = 1.0  # diagonal elements

    weighted_ifg = (
        d_ifg_chunk / (np.abs(d_ifg_chunk) + 1e-8) * d_correlation_chunk[None, :]
    )
    pixel_idx = cp.arange(npixels)[:, None]
    corr_matrix[pixel_idx, ref_indices, sec_indices] = weighted_ifg
    corr_matrix[pixel_idx, sec_indices, ref_indices] = weighted_ifg.conj()

    # eigenvalue decomposition
    _, eigenvectors = cp.linalg.eigh(corr_matrix)

    # shape: (npixels, ndate)
    primary_eigenvector = eigenvectors[:, :, -1]

    # shape: (chunk_rows, chunk_cols, ndate)
    d_phase = cp.angle(primary_eigenvector).reshape(chunk_row, chunk_col, ndate)

    # transpose to (ndate, chunk_rows, chunk_cols) for dask
    d_phase_t = cp.transpose(d_phase, (2, 0, 1))

    return cp.asnumpy(d_phase_t)


def phase_linking_solver(
    ifg_files: Sequence[str],
    mask_file: Path | str | None,
    out_path: Path | str,
    nrow: int,
    ncol: int,
    solver_func: Callable,
    row_chunk_size: int | None = None,
    col_chunk_size: int | None = None,
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
    row_chunk_size : int or None
        Row chunk size for dask array.
    col_chunk_size : int or None
        Column chunk size for dask array.
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    ifg_list = IfgList(ifg_files)
    logger.info(
        "Phase linking with %d interferograms, %d unique dates",
        len(ifg_files),
        ifg_list.ndate,
    )
    logger.info("Creating image stacks with dask")

    ifg_memmaps = [
        np.memmap(f, dtype="complex64", mode="r", shape=(nrow, ncol)) for f in ifg_files
    ]
    col_chunk_size = int(np.minimum(ncol, 64 * 64))
    row_chunk_size = int(np.maximum(64 * 64 // ncol, 1))
    logger.info(
        f"Using row_chunk_size={row_chunk_size}, col_chunk_size={col_chunk_size}"
        + " for dask array"
    )
    ifg_dask_slices = [
        da.from_array(m, chunks=(row_chunk_size, col_chunk_size)) for m in ifg_memmaps
    ]
    ifg_stack = da.stack(ifg_dask_slices, axis=2)  # (nrow, ncol, nifg)

    # Load mask
    logger.info("Load mask from %s", mask_file)
    if mask_file is not None:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    ref_indices, sec_indices = ifg_list.ref_sec_indices()

    correlation_vector = np.exp(-ifg_list.df.tempbl / 60.0).astype(np.float32)
    corr_dask_1d = da.from_array(correlation_vector, chunks=(len(ifg_files),))
    correlation_stack = da.broadcast_to(
        corr_dask_1d[None, None, :], shape=(nrow, ncol, len(ifg_files))
    )

    # --- Run the dask computation -------------------------------------------
    ndate = ifg_list.ndate
    new_chunks = (ndate, *ifg_stack.chunks[0:2])  # (ndate, chunk_rows, chunk_cols)
    result = da.map_blocks(
        solver_func,
        ifg_stack,
        correlation_stack,
        dtype=np.float32,
        drop_axis=2,
        new_axis=0,
        chunks=new_chunks,
        block_info=True,
        mask=mask,
        ndate=ndate,
        ref_indices=ref_indices,
        sec_indices=sec_indices,
    )  # (ndate, nrow, ncol)

    out_path = Path(out_path)
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True, exist_ok=True)

    # --- Write to zarr ------------------------------------------------------
    store = str(out_path)
    logger.info("Writing displacement time series to %s", out_path)

    # Write 3D displacement
    with ProgressBar():
        da.to_zarr(result, store, overwrite=True)

    root = zarr.open(store, mode="a")
    if metadata:
        for key, value in metadata.items():
            _store_attr(root, key, value)

    logger.info("Phase linking computation complete.")


def run_phase_linking(
    ifg_path: Path | str | None = None,
    out_path: Path | str | None = None,
    config: Path | str = "config.yaml",
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
    """
    from s1proc.geocoordinates import GeoCoordinates

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
        out_path = Path(icfg.proc_path) / "phase_linking.zarr"
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
