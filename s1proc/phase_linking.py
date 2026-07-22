from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Sequence, Tuple

import cupy as cp
import dask.array as da
import numpy as np
import zarr
from dask.diagnostics import ProgressBar
from numpy.typing import NDArray

from s1proc.utils import get_gpu_pool

if TYPE_CHECKING:
    IfgInputType = da.Array | Path | str | Sequence[Path | str]

from s1proc._config import load_config
from s1proc._log import set_logging_level, setup_logger
from s1proc.geocoordinates import GeoCoordinates
from s1proc.sario import _store_attr
from s1proc.utils import IfgList, get_files

logger = setup_logger(__name__, level="INFO")
GPU_POOL = get_gpu_pool()


# ---------------------------------------------------------------------------
# Per-chunk solver (dask ``map_blocks`` kernel)
# ---------------------------------------------------------------------------


def eigensar_block(
    ifg_chunk: NDArray[np.complex64],  # (row_chunk, col_chunk, nifg)
    ref_indices: NDArray[np.int_],  # (nifg,)
    sec_indices: NDArray[np.int_],  # (nifg,)
    ndate: int,
    mask: NDArray[np.bool_] = None,
    correlation_vector: NDArray[np.float32] = None,  # (nifg,)
    block_info: dict = None,
) -> NDArray[np.float32]:  # (row_chunk, col_chunk, ndate+1)
    """
    Parameters
    ----------
    ifg_chunk: NDArray[np.complex64]
        A chunk of the interferogram stack (row_chunk, col_chunk, nifg)
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
        Combined array of shape ``(row_chunk, col_chunk, ndate+1)``.
        The first *ndate* slices are the optimised phase; the last slice
        is the temporal coherence (gamma).
    """
    # Create the correlation matrix
    chunk_row, chunk_col, nifg = ifg_chunk.shape
    array_loc = block_info[0]["array-location"]
    mask_chunk = mask[
        array_loc[0][0] : array_loc[0][1], array_loc[1][0] : array_loc[1][1]
    ]
    if np.all(mask_chunk == 0):
        return np.zeros((chunk_row, chunk_col, ndate + 1), dtype=np.float32)

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
    d_gamma_2d = d_gamma.reshape(chunk_row, chunk_col)  # (row_chunk, col_chunk)

    # shape: (row_chunk, col_chunk, ndate)
    d_phase = cp.angle(primary_eigenvector).reshape(chunk_row, chunk_col, ndate)

    phase_np = cp.asnumpy(d_phase)
    gamma_np = cp.asnumpy(d_gamma_2d)
    gamma_np[~mask_chunk] = 0
    gamma_np = gamma_np[:, :, None]  # (row_chunk, col_chunk, 1)

    combined_output = np.concatenate([phase_np, gamma_np], axis=2)

    return combined_output


def eigensar_block_wrapper(
    ifg_chunk: NDArray[np.complex64],  # (row_chunk, col_chunk, nifg)
    ref_indices: NDArray[np.int_],  # (nifg,)
    sec_indices: NDArray[np.int_],  # (nifg,)
    ndate: int,
    mask: NDArray[np.bool_] = None,
    correlation_vector: NDArray[np.float32] = None,  # (nifg,)
    block_info: dict = None,
) -> NDArray[np.float32]:  # (row_chunk, col_chunk, ndate+1)
    assigned_gpu = GPU_POOL.get()
    try:
        res = eigensar_block(
            ifg_chunk,
            ref_indices,
            sec_indices,
            ndate,
            mask,
            correlation_vector,
            block_info,
        )
        return res
    except Exception as e:
        logger.debug(e)
    finally:
        GPU_POOL.put(assigned_gpu)


# ---------------------------------------------------------------------------
# VRAM estimation helpers
# ---------------------------------------------------------------------------


def _estimate_eigensar_vram_bytes(
    row_chunk: int,
    ncol: int,
    nifg: int,
    ndate: int,
) -> int:
    """Estimate peak GPU memory (bytes) for one EigenSAR chunk.

    The dominant cost is the ``(npixels, ndate, ndate)`` complex64 correlation
    matrix and the same-sized eigenvector output from ``cp.linalg.eigh``.

    Parameters
    ----------
    row_chunk : int
        Number of rows in this spatial chunk.
    ncol : int
        Number of columns (full image width).
    nifg : int
        Number of interferograms.
    ndate : int
        Number of unique acquisition dates.

    Returns
    -------
    int
        Estimated peak bytes of GPU memory for one chunk.
    """
    npixels = row_chunk * ncol
    bytes_complex64 = 8
    bytes_float32 = 4

    # d_corr_matrix + eigenvectors + eigh workspace (~3x the matrix)
    vram_corr = 3 * npixels * ndate * ndate * bytes_complex64

    # d_ifg_chunk (complex64) + d_correlation (float32) + weighted_ifg
    vram_ifg = npixels * nifg * (bytes_complex64 + bytes_float32)

    # primary_eigenvector (complex64)
    vram_eigvec = npixels * ndate * bytes_complex64

    # output: phase (ndate floats) + gamma (1 float) per pixel
    vram_output = npixels * (ndate + 1) * bytes_float32

    # safety factor of 1.5 for CuPy allocator overhead and temporaries
    return int((vram_corr + vram_ifg + vram_eigvec + vram_output) * 1.5)


def get_eigensar_chunks(
    nrow: int,
    ncol: int,
    nifg: int,
    ndate: int,
    max_vram_gb: float | None = None,
) -> Tuple[int, int, int]:
    """Determine safe row-chunk size for EigenSAR phase linking.

    Parameters
    ----------
    nrow : int
        Number of rows in each interferogram.
    ncol : int
        Number of columns in each interferogram.
    nifg : int
        Number of interferograms.
    ndate : int
        Number of unique acquisition dates.
    max_vram_gb : float | None
        Maximum GPU VRAM budget in gigabytes.  When *None*, queries
        ``pynvml`` for the total VRAM of the GPU with the most headroom.

    Returns
    -------
    Tuple[int, int, int]
        ``(row_chunk, col_chunk, img_chunk)`` for the dask array.
    """
    from s1proc.utils import _query_gpu_info

    if max_vram_gb is None:
        gpu_info = _query_gpu_info()
        max_vram_gb = max(
            gpu_info[i]["total_vram_gb"] for i in range(gpu_info["gpu_count"])
        )

    max_vram_bytes = int(max_vram_gb * 1024**3)

    # Estimate VRAM for a single row to compute how many rows fit in VRAM
    vram_per_row = _estimate_eigensar_vram_bytes(1, ncol, nifg, ndate)

    row_chunk = max(1, int(max_vram_bytes / vram_per_row))
    row_chunk = min(row_chunk, nrow)

    col_chunk = ncol

    logger.info(
        "EigenSAR chunking: row_chunk=%d, col_chunk=%d (VRAM budget: %.1f GB, "
        "estimated per row: %.1f MB)",
        row_chunk,
        col_chunk,
        max_vram_gb,
        vram_per_row / 1024**2,
    )
    img_chunk = min(nifg, max(int(1e9 / (nrow * ncol * 8)), 1))

    return row_chunk, col_chunk, img_chunk


# ---------------------------------------------------------------------------
# Input normalisation helpers
# ---------------------------------------------------------------------------


def _is_zarr_path(path: Path) -> bool:
    """Check whether *path* points to a valid Zarr dataset (v2 or v3)."""
    if path.is_dir():
        if (path / ".zarray").exists():
            return True
        if (path / "zarr.json").exists():
            return True
        if (path / ".zgroup").exists():
            return True
        if len(list(path.glob("*"))) == 0:
            return True
    return False


def _load_ifg_as_dask(
    igrams: IfgInputType,
    nrow: int,
    ncol: int,
    row_chunk: int,
) -> "da.Array":
    """Normalise *igrams* to a ``dask.array.Array`` with shape ``(H, W, N)``.

    Parameters
    ----------
    igrams : IfgInputType
        One of: a ``dask.array.Array``, a path to a Zarr dataset, or a
        list of paths to flat binary files (``complex64``, row-major).
    nrow : int
        Image rows.  Required when *igrams* is a file list.
    ncol : int
        Image columns.  Required when *igrams* is a file list.
    row_chunk : int
        Rows per chunk for the virtual stack.  Required when *igrams* is a
        file list.

    Returns
    -------
    da.Array
        3-D dask array of shape ``(nrow, ncol, N)`` and dtype
        ``complex64``.

    Raises
    ------
    TypeError
        If *igrams* has an unsupported type.
    ValueError
        If *nrow* / *ncol* are missing when needed.
    """
    import dask.array as da

    from s1proc.sario import create_virtual_stack

    # ---- 1. dask.array.Array ------------------------------------------------
    if isinstance(igrams, da.Array):
        return igrams

    # ---- 2. Single path -> Zarr or binary file -------------------------------
    if isinstance(igrams, (str, Path)):
        path = Path(igrams)
        if _is_zarr_path(path):
            return da.from_zarr(str(path))
        raise ValueError(
            f"Single path '{path}' is not a Zarr dataset. "
            "For a single binary file pass it as a list: [path]."
        )

    # ---- 3. List of paths ---------------------------------------------------
    if isinstance(igrams, list):
        if len(igrams) == 0:
            return da.zeros((nrow, ncol, 0), dtype=np.complex64, chunks=(nrow, ncol, 1))

        first = Path(igrams[0])
        if _is_zarr_path(first):
            raise TypeError(
                "A list was passed but the first element is a Zarr dataset. "
                "Pass the directory path as a string/Path instead of wrapping "
                "it in a list."
            )

        if nrow <= 0 or ncol <= 0:
            raise ValueError(
                "nrow and ncol must be positive when loading from a file list. "
                f"Got nrow={nrow}, ncol={ncol}."
            )
        if row_chunk <= 0:
            raise ValueError(f"row_chunk must be positive, got {row_chunk}.")

        mapper = create_virtual_stack(
            [Path(f) for f in igrams],
            np.complex64,
            nrow,
            ncol,
            row_chunk,
            new_axis=2,
        )
        return da.from_zarr(mapper)

    raise TypeError(
        f"igrams must be a dask Array, a Zarr path, or a list of file paths. "
        f"Got {type(igrams).__name__}."
    )


# ---------------------------------------------------------------------------
# Core phase linking pipeline
# ---------------------------------------------------------------------------


def phase_linking_solver(
    igrams: "IfgInputType",
    mask_file: Path | str | None,
    nrow: int = 0,
    ncol: int = 0,
    solver_func: Callable = eigensar_block_wrapper,
    row_chunk: int | None = None,
    max_vram_gb: float | None = None,
    ifg_list: IfgList | None = None,
) -> Tuple["da.Array", IfgList]:
    """Run phase linking on a stack of wrapped interferograms.

    Accepts binary files, Zarr datasets, or ``dask.array.Array`` as input
    and returns a single lazy dask array containing both the optimised
    phase and temporal coherence.  Use :func:`save_phase_linking_results`
    to write to disk and :func:`load_phase_linking_results` to read back.

    Parameters
    ----------
    igrams : dask.array.Array, str, Path, or list[str | Path]
        Input interferograms.  Allowed types:

        - ``da.Array`` — used directly (no loading).  *ifg_list* must be
          provided.
        - ``str`` or ``Path`` — must point to a Zarr v2/v3 dataset.
          *ifg_list* must be provided.
        - ``list[str | Path]`` — list of flat binary files (``complex64``,
          row-major, no header).  Requires *nrow* and *ncol*.  The
          *ifg_list* is built automatically from filenames.

    mask_file : str or None
        Path to a boolean mask file (True = valid pixel).  When *None*,
        all pixels are treated as valid.
    nrow : int
        Number of image rows.  Required when *igrams* is a file list.
    ncol : int
        Number of image columns.  Required when *igrams* is a file list.
    solver_func : Callable
        Per-chunk solver (a dask ``map_blocks``-compatible function).
        Defaults to :func:`eigensar_block_wrapper`.
    row_chunk : int or None
        Row chunk size for the dask array.  When *None*, automatically
        determined from available GPU VRAM.
    max_vram_gb : float or None
        Maximum GPU VRAM budget in GB.  ``None`` (default) auto-detects
        the free VRAM on the most available GPU via ``pynvml``.
    ifg_list : IfgList or None
        Pre-built interferogram list.  Required when *igrams* is a dask
        array or Zarr path.  When *igrams* is a list of file paths this
        is auto-created and the argument is ignored.

    Returns
    -------
    Tuple[da.Array, IfgList]
        ``(res, ifg_list)`` — a single lazy dask array of shape
        ``(nrow, ncol, ndate + 1)`` and dtype ``float32``.  The first
        ``ndate`` slices along axis 2 are the optimised phase; the last
        slice is the temporal coherence (gamma).  Use the helper
        :func:`load_phase_linking_results` to split on read.

    Raises
    ------
    TypeError
        If *igrams* has an unsupported type.
    ValueError
        If required arguments (e.g. *nrow* / *ncol*) are missing or invalid,
        or if *ifg_list* is missing for a dask/Zarr input.
    """
    import dask.array as da

    # ---- Resolve nrow / ncol from input ------------------------------------
    if isinstance(igrams, da.Array):
        _nrow = int(igrams.shape[0])
        _ncol = int(igrams.shape[1])
    elif isinstance(igrams, (str, Path)):
        _nrow = 0
        _ncol = 0
    elif isinstance(igrams, list):
        _nrow = 0
        _ncol = 0
    else:
        raise TypeError(
            f"igrams must be a dask Array, a Zarr path, or a list of file paths. "
            f"Got {type(igrams).__name__}."
        )

    nrow = nrow or _nrow
    ncol = ncol or _ncol

    if nrow <= 0 or ncol <= 0:
        raise ValueError(
            "Image dimensions are unknown.  Provide nrow/ncol explicitly, "
            "or pass input with known shape (dask array or Zarr)."
        )

    # ---- Build IfgList -----------------------------------------------------
    if isinstance(igrams, list):
        # Auto-build IfgList from file names
        ifg_list = IfgList([str(p) for p in igrams])
    elif ifg_list is None:
        raise ValueError(
            "ifg_list is required when igrams is a dask Array or Zarr path. "
            "Build it from the original file list with IfgList(file_paths)."
        )

    logger.info(
        "Phase linking with %d interferograms, %d unique dates",
        ifg_list.nifg,
        ifg_list.ndate,
    )

    # ---- Auto-determine row_chunk from VRAM ---------------------------------
    if row_chunk is None:
        row_chunk, _, _ = get_eigensar_chunks(
            nrow, ncol, ifg_list.nifg, ifg_list.ndate, max_vram_gb
        )
    logger.info("Using row_chunk=%d, col_chunk=%d for dask array", row_chunk, ncol)

    # ---- Load & chunk -------------------------------------------------------
    ifg_stack = _load_ifg_as_dask(igrams, nrow, ncol, row_chunk)
    ifg_stack = ifg_stack.rechunk({0: row_chunk, 1: ncol, 2: -1})

    # ---- Load mask -----------------------------------------------------------
    if mask_file is not None:
        logger.info("Load mask from %s", mask_file)
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    ref_indices, sec_indices = ifg_list.ref_sec_indices()
    correlation_vector = np.exp(-ifg_list.df.tempbl / 60.0).astype(np.float32)

    # ---- Run the dask computation -------------------------------------------
    ndate = ifg_list.ndate
    result_chunks = (ifg_stack.chunks[0], ifg_stack.chunks[1], ndate + 1)
    res = da.map_blocks(
        solver_func,
        ifg_stack,
        dtype=np.float32,
        chunks=result_chunks,
        block_info=True,
        mask=mask,
        correlation_vector=correlation_vector,
        ndate=ndate,
        ref_indices=ref_indices,
        sec_indices=sec_indices,
    )  # (nrow, ncol, ndate+1)

    logger.info("Phase linking computation graph built (lazy).")
    return res, ifg_list


# ---------------------------------------------------------------------------
# Output writer
# ---------------------------------------------------------------------------


def save_phase_linking_results(
    res: "da.Array",
    out_path: Path | str,
    ifg_list: IfgList | None = None,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """Write phase-linking results to a single Zarr dataset.

    The array has shape ``(nrow, ncol, ndate + 1)``.  The first
    ``ndate`` slices along axis 2 are the optimised phase; the final
    slice is the temporal coherence (gamma).  ``ndate`` is stored as a
    zarr attribute so that :func:`load_phase_linking_results` can split
    correctly on read.

    Writing proceeds in spatial batches via ``da.to_zarr(region=...)``
    so that the dask task graph stays small and the store-map
    :func:`~dask.array.core.concatenate3` path is never exercised on the
    entire array at once.

    Parameters
    ----------
    res : da.Array
        Lazy dask array of shape ``(nrow, ncol, ndate + 1)`` and dtype
        ``float32``, as returned by :func:`phase_linking_solver`.
    out_path : Path or str
        Output Zarr directory.
    ifg_list : IfgList or None
        Interferogram list metadata.  When provided, the ``"date"``
        attribute is written to the output zarr group (unless overridden
        by *metadata*).
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    import gc

    out_path = Path(out_path)
    if out_path.exists():
        if _is_zarr_path(out_path):
            shutil.rmtree(out_path)
        else:
            raise RuntimeError(f"{out_path} exists and is not a zarr path")
    out_path.mkdir(parents=True, exist_ok=True)

    ndate = int(res.shape[2]) - 1
    nrow, ncol = int(res.shape[0]), int(res.shape[1])
    row_chunk = res.chunks[0][0]

    # Create the empty zarr array with the same chunk shape as res
    z = zarr.open(
        str(out_path),
        mode="w",
        shape=(nrow, ncol, ndate + 1),
        chunks=(res.chunks[0][0], res.chunks[1][0], res.chunks[2][0]),
        dtype=np.float32,
    )
    for row_start in range(0, nrow, row_chunk):
        row_end = min(row_start + row_chunk, nrow)
        logger.debug("Writing rows [%d:%d] to zarr", row_start, row_end)
        sub_da = res[row_start:row_end, :, :]
        with ProgressBar():
            da.to_zarr(
                sub_da,
                z,
                region=(
                    slice(row_start, row_end),
                    slice(None),
                    slice(None),
                ),
            )
        del sub_da
        gc.collect()

    # Write metadata to zarr group
    root = zarr.open(out_path, mode="a")
    if metadata:
        for key, value in metadata.items():
            _store_attr(root, key, value)

    _store_attr(root, "ndate", ndate)

    if ifg_list is not None and "date" not in (metadata or {}):
        _store_attr(root, "date", list(ifg_list.dates))

    logger.info("Phase linking results saved to %s", out_path)
    return None


def load_phase_linking_results(
    path: Path | str,
) -> Tuple["da.Array", "da.Array"]:
    """Load phase-linking results from a Zarr dataset.

    The on-disk array has shape ``(nrow, ncol, ndate + 1)``.  This
    function splits it into the two logical components.

    Parameters
    ----------
    path : Path or str
        Path to the Zarr directory written by
        :func:`save_phase_linking_results`.

    Returns
    -------
    Tuple[da.Array, da.Array]
        ``(opt_phase, gamma)`` — lazy dask arrays with shapes
        ``(nrow, ncol, ndate)`` (float32) and ``(nrow, ncol)``
        (float32).
    """
    import dask.array as da

    path = Path(path)
    if not _is_zarr_path(path):
        raise ValueError(f"{path} is not a Zarr dataset")

    res = da.from_zarr(str(path))
    opt_phase = res[:, :, :-1]  # (nrow, ncol, ndate)
    gamma = res[:, :, -1]  # (nrow, ncol)
    return opt_phase, gamma


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def run_phase_linking(
    ifg_path: Path | str | None = None,
    out_path: Path | str | None = None,
    config: Path | str = "config.yaml",
    verbose: bool = False,
) -> None:
    """Run phase linking on a stack of wrapped interferograms.

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

    rsc = GeoCoordinates(icfg.multilook_rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon

    metadata = {
        "method": "eigensar",
    }

    res, ifg_list = phase_linking_solver(
        igrams=ifg_files,
        mask_file=icfg.mask_file,
        nrow=nrow,
        ncol=ncol,
        solver_func=eigensar_block_wrapper,
    )

    save_phase_linking_results(
        res=res,
        out_path=out_path,
        ifg_list=ifg_list,
        metadata=metadata,
    )
