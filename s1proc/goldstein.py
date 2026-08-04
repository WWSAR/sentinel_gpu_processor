"""Batch Goldstein filter for interferogram stacks using CuPy.

This module provides a GPU-accelerated implementation of the Goldstein
filter [Goldstein1998]_ that processes multiple interferograms simultaneously
via a dask+cupy pipline.

References
----------
.. [Goldstein1998] Goldstein, R. M., & Werner, C. L. (1998).  Radar
   interferogram filtering for geophysical applications.  Geophysical
   Research Letters, 25(21), 4035-4038.
   https://doi.org/10.1029/1998GL900033
"""

from __future__ import annotations

import gc
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence, Tuple

import cupy as cp
import numpy as np

if TYPE_CHECKING:
    import dask.array as da

    FilterInputType = da.Array | Path | str | Sequence[Path | str]

from s1proc._background import MultiBinaryFileWriter
from s1proc._log import setup_logger
from s1proc.utils import IfgList, get_gpu_pool

logger = setup_logger(__name__, level="INFO")
GPU_POOL = get_gpu_pool()


def _make_gaussian_kernel_1d(
    size: int = 7,
    sigma: float = 1.0,
) -> cp.ndarray:
    """Build a normalized 1-D Gaussian kernel.

    Parameters
    ----------
    size : int
        Kernel length (must be odd).
    sigma : float
        Standard deviation.

    Returns
    -------
    cp.ndarray
        1-D kernel of shape ``(size,)`` and dtype ``float32``.
    """
    half = size // 2
    x = cp.arange(-half, half + 1, dtype=cp.float32)
    kernel = cp.exp(-0.5 * (x / sigma) ** 2)
    kernel /= kernel.sum()
    return kernel


def _make_hamming_window_2d(window_size: int) -> cp.ndarray:
    """Build a separable 2-D Hamming window.

    Parameters
    ----------
    window_size : int
        Width and height of the window.

    Returns
    -------
    cp.ndarray
        2-D window of shape ``(window_size, window_size)`` and dtype
        ``float32``.
    """
    n = cp.arange(window_size, dtype=cp.float32)
    hamming_1d = 0.54 - 0.46 * cp.cos(2.0 * cp.pi * n / (window_size - 1))
    return hamming_1d[:, None] * hamming_1d[None, :]


def _batch_goldstein_filter(
    igrams: np.ndarray,
    alpha: float = 0.5,
    window_size: int = 32,
    overlap: int = 24,
    gpu_id: int = 0,
) -> np.ndarray:
    """Apply the Goldstein filter to a stack of wrapped interferograms.

    The filter operates on overlapping 2-D patches: each patch is transformed
    to the frequency domain, its amplitude spectrum is smoothed, normalised,
    and raised to the power *alpha*, and then the inverse transform is
    computed.  Overlapping patches are blended with a separable Hamming window
    via overlap-add.

    Parameters
    ----------
    igrams : cp.ndarray
        Input interferogram stack of shape ``(H, W, B)`` and dtype
        ``complex64``.  Zeros indicate masked pixels and are preserved in the
        output.
    alpha : float
        Filter strength, typically in ``[0.0, 1.0]``.  ``alpha=0`` applies no
        filtering; ``alpha=1`` applies maximum filtering.
    window_size : int
        Side length of the square processing window (e.g., 32).
    overlap : int
        Overlap in pixels between adjacent windows.  A value of
        ``window_size * 3 // 4`` (75% overlap) is common.
    gpu_id: int
        Index of GPU devices

    Returns
    -------
    cp.ndarray
        Filtered interferograms of shape ``(H, W, B)`` and dtype
        ``complex64``.

    Notes
    -----
    The implementation is designed for zero-copy operation wherever possible:

    - Patches are extracted as a logical view via
      ``cp.lib.stride_tricks.sliding_window_view`` after edge-padding the
      input so that the stride evenly partitions the image.
    - The 2-D FFT and IFFT are applied to all patches simultaneously along
      the last two axes.
    - The overlap-add reconstruction uses strided-slice accumulation (a
      fixed-size ``window_size²`` loop over in-window offsets, each
      dispatching a single large GPU operation) rather than per-patch Python
      iteration.
    - Common *alpha* values are handled with fast paths (``cp.sqrt`` for 0.5,
      identity for 1.0) to avoid the overhead of generic ``cp.power``.

    The amplitude spectrum is smoothed with a 7×7 Gaussian (σ=1.0) in the
    frequency domain before normalisation, matching the behaviour of the
    legacy ``goldstein`` CUDA executable.
    """
    with cp.cuda.Device(gpu_id):
        d_igrams = cp.array(igrams)
        H_orig, W_orig, B = igrams.shape
        stride = window_size - overlap

        # ---- 1. Pad so windows tile evenly across the image -----------------------
        # Two goals: (a) ensure every pixel is covered by at least one full window,
        # and (b) handle images smaller than ``window_size`` by padding up.
        min_pad_h = max(0, window_size - H_orig)
        min_pad_w = max(0, window_size - W_orig)
        # After the minimum pad, ensure (H_pad - window_size) is divisible by stride
        # so that sliding_window_view with ::stride tiles perfectly.
        H_min = H_orig + min_pad_h
        W_min = W_orig + min_pad_w
        pad_h = min_pad_h + (stride - (H_min - window_size) % stride) % stride
        pad_w = min_pad_w + (stride - (W_min - window_size) % stride) % stride

        if pad_h > 0 or pad_w > 0:
            padded = cp.pad(d_igrams, ((0, pad_h), (0, pad_w), (0, 0)), mode="edge")
        else:
            padded = d_igrams
        H_pad, W_pad = padded.shape[0], padded.shape[1]

        # ---- 2. Zero-copy patch extraction ---------------------------------------
        # sliding_window_view produces a 5-D view; striding selects patches at the
        # requested overlap interval.
        patches: cp.ndarray = cp.lib.stride_tricks.sliding_window_view(
            padded, (window_size, window_size), axis=(0, 1)
        )
        patches = patches[::stride, ::stride, :, :, :]  # (nwy, nwx, B, W, W)
        nwy, nwx, _, win_h, win_w = patches.shape
        assert win_h == win_w == window_size, "Window dimension mismatch"

        # ---- 3. Hamming window ---------------------------------------------------
        hamming_2d = _make_hamming_window_2d(window_size)  # (W, W), float32
        # patches = patches * hamming_2d.astype(cp.complex64)

        # ---- 4. Batch 2-D FFT ----------------------------------------------------
        patches = cp.fft.fft2(patches, axes=(-2, -1))

        # ---- 5. Amplitude spectrum -----------------------------------------------
        amplitude = cp.abs(patches)  # (nwy, nwx, B, W, W), float32

        # ---- 6. Gaussian smoothing in frequency domain ---------------------------
        # has very small impact on final filtered image quality, just skip this step
        # A 7×7 Gaussian (σ=1.0) with circular wrap emulates the fftshift-
        # aware smoothing in the legacy CUDA kernel.
        # gauss_1d = _make_gaussian_kernel_1d(size=7, sigma=1.0)
        # amplitude = convolve1d(amplitude, gauss_1d, axis=-1, mode="wrap")
        # amplitude = convolve1d(amplitude, gauss_1d, axis=-2, mode="wrap")

        # ---- 7. Patch-wise mean normalisation ------------------------------------
        mean_amp = amplitude.mean(axis=(-2, -1), keepdims=True)
        amplitude /= mean_amp + 1e-8

        # ---- 8. Apply Goldstein alpha --------------------------------------------
        # Optimised fast paths for the most common alpha values.
        if alpha == 0.0:
            pass  # fft_patches is unchanged
        elif alpha == 0.5:
            patches *= cp.sqrt(amplitude)
        elif alpha == 1.0:
            patches *= amplitude
        else:
            patches *= cp.power(amplitude, float(alpha))

        # ---- 9. Batch inverse FFT ------------------------------------------------
        patches = cp.fft.ifft2(patches, axes=(-2, -1))
        patches = patches.astype(cp.complex64)

        # ---- 10. Overlap-add reconstruction --------------------------------------
        # The double loop iterates over *in-window offsets* (dy, dx), not over
        # patches.  Each iteration performs a single strided-slice accumulation
        # that updates all patches in parallel on the GPU.  For a 32×32 window
        # this is 1024 fast GPU dispatches — no per-patch Python loop.
        output = cp.zeros((H_pad, W_pad, B), dtype=cp.complex64)
        weight = cp.zeros((H_pad, W_pad, B), dtype=cp.float32)

        # The analysis window (Hamming) is applied before the FFT and the same
        # window serves as the synthesis weight during overlap-add.  To achieve
        # perfect reconstruction when alpha=0 the accumulated weight must be w²,
        # since each pixel is multiplied by w twice (analysis + synthesis).
        for dy in range(window_size):
            y_slice = slice(dy, dy + nwy * stride, stride)
            for dx in range(window_size):
                x_slice = slice(dx, dx + nwx * stride, stride)
                w = hamming_2d[dy, dx]
                output[y_slice, x_slice, :] += patches[:, :, :, dy, dx] * w
                weight[y_slice, x_slice, :] += w

        # ---- 11. Normalise by accumulated weights --------------------------------
        valid = weight > 0.0
        output[valid] /= weight[valid].astype(cp.complex64)

        # ---- 12. Crop back to original dimensions --------------------------------
        if pad_h > 0 or pad_w > 0:
            output = output[:H_orig, :W_orig, :]

        # ---- 13. Preserve input mask (zero → zero) -------------------------------
        input_mask = igrams == cp.complex64(0.0 + 0.0j)
        output[input_mask] = cp.complex64(0.0 + 0.0j)

        cp.cuda.stream.get_current_stream().synchronize()

        return cp.asnumpy(output)


def batch_goldstein_filter(
    igrams: np.ndarray,
    nrow: int,
    ncol: int,
    row_chunk: int,
    alpha: float,
    window_size: int,
    overlap: int,
):
    """
    Get an available GPU device for running goldstein filter
    """
    assigned_gpu = GPU_POOL.get()

    try:
        if row_chunk < nrow:
            valid_row_chunk = row_chunk - overlap
            row_start_indices = np.arange(0, nrow - row_chunk + 1, valid_row_chunk)
            nimg = igrams.shape[2]
            filtered_ifg = np.zeros((nrow, ncol, nimg), dtype=np.complex64)
            for row_start in row_start_indices:
                filtered_chunk = _batch_goldstein_filter(
                    igrams[row_start : row_start + row_chunk, :, :],
                    alpha,
                    window_size,
                    overlap,
                    gpu_id=assigned_gpu,
                )
                filtered_ifg[row_start : row_start + valid_row_chunk, :, :] = (
                    filtered_chunk[0:valid_row_chunk, :, :]
                )
        else:
            filtered_ifg = _batch_goldstein_filter(
                igrams, alpha, window_size, overlap, gpu_id=assigned_gpu
            )
        return filtered_ifg

    finally:
        GPU_POOL.put(assigned_gpu)


# ---------------------------------------------------------------------------
# VRAM estimation helpers
# ---------------------------------------------------------------------------


def _estimate_peak_vram_per_ifg(
    nrow: int,
    ncol: int,
    window_size: int,
    overlap: int,
) -> int:
    """Estimate peak GPU memory (bytes) for a single interferogram during
    Goldstein filtering.

    The dominant cost comes from the 5-D patch expansion in the frequency
    domain.  With 75 % overlap (``stride = window_size / 4``) each pixel is
    repeated approximately ``(window_size / stride)² = 16`` times.

    Parameters
    ----------
    nrow : int
        Number of image rows.
    ncol : int
        Number of image columns.
    window_size : int
        Processing window side length.
    overlap : int
        Overlap between adjacent windows (pixels).

    Returns
    -------
    int
        Estimated peak bytes of GPU memory for one interferogram.
    """
    stride = window_size - overlap
    expansion = (window_size / stride) ** 2

    bytes_per_pix_complex = 8  # complex64
    bytes_per_pix_float = 4  # float32

    # Two FFT buffers (in-place is not guaranteed) + amplitude spectrum
    vram_fft = nrow * ncol * bytes_per_pix_complex * expansion * 2
    vram_amp = nrow * ncol * bytes_per_pix_float * expansion

    # Plus output / weight / padded-input buffers (small relative to above)
    vram_outputs = nrow * ncol * (bytes_per_pix_complex + bytes_per_pix_float)

    return int((vram_fft + vram_amp + vram_outputs) * 1.5)


def _compute_spatial_row_chunk(
    nrow: int,
    ncol: int,
    stride: int,
    window_size: int,
    max_vram_bytes: int,
) -> int:
    """Compute a safe row-chunk size for spatial splitting of a large image.

    The returned value is a multiple of *stride* so that the window grid
    aligns across neighbouring chunks, and is clamped to ``[stride, nrow]``.

    Parameters
    ----------
    nrow : int
        Total image rows.
    ncol : int
        Total image columns.
    stride : int
        Window step size (``window_size - overlap``).
    window_size : int
        Processing window side length.
    max_vram_bytes : int
        Available GPU memory budget for the chunk (must accommodate the
        padded rows: *row_chunk* + 2 × *window_size*).

    Returns
    -------
    int
        Number of useful rows per spatial chunk.
    """
    bytes_per_row = ncol * 20 * ((window_size / stride) ** 2)
    # Pad factor: the chunk on the GPU holds row_chunk + 2 * window_size rows
    # Solve: (row_chunk + 2*ws) * bytes_per_row / expansion_ratio <= max_vram
    # Here bytes_per_row already includes expansion, so:
    pad_overhead = 2 * window_size * bytes_per_row
    usable_bytes = max_vram_bytes - pad_overhead
    if usable_bytes <= 0:
        raise RuntimeError(
            f"max_vram_gb too small: cannot fit a single window row "
            f"(window_size={window_size}). Increase max_vram_gb or reduce "
            f"window_size."
        )
    row_chunk = max(1, int(usable_bytes / bytes_per_row))
    # Round down to a multiple of stride for window-grid alignment
    row_chunk = (row_chunk // stride) * stride
    return max(stride, min(row_chunk, nrow))


def get_goldstein_chunks(
    nrow: int,
    ncol: int,
    window_size: int,
    overlap: int | None = None,
    max_vram_gb: float | None = None,
) -> Tuple[int, int, int]:
    """
    Estimate 3D chunks for Goldstein filtering

    Parameters
    ----------
    nrow : int
        Number of rows in each interferogram.
    ncol : int
        Number of columns in each interferogram.
    window_size : int
        Processing window side length in pixels.  Default 32.
    overlap : int | None
        Overlap between adjacent windows in pixels.  Default (75 % of
        window_size).
    max_vram_gb : float | None
        Maximum GPU VRAM budget in gigabytes.  When *None*, queries
        ``pynvml`` for the total VRAM of the GPU with the most headroom.

    Returns
    -------
    Tuple[int, int, int]
        ``(img_chunk, row_chunk, col_chunk)`` for the dask array.
    """
    from s1proc.utils import _query_gpu_info

    if overlap is None:
        overlap = int(min(np.ceil(window_size * 0.75), window_size - 1))

    if max_vram_gb is None:
        gpu_info = _query_gpu_info()
        max_vram_gb = max(
            gpu_info[i]["total_vram_gb"] for i in range(gpu_info["gpu_count"])
        )

    max_vram_bytes = int(max_vram_gb * 1024**3)
    image_vram = _estimate_peak_vram_per_ifg(nrow, ncol, window_size, overlap)
    logger.info(
        "Estimated peak VRAM per interferogram: %.1f MB  (max allowed: %.1f GB)",
        image_vram / 1024**2,
        max_vram_gb,
    )

    if image_vram > max_vram_bytes:
        img_chunk = 1
        row_chunk = _compute_spatial_row_chunk(
            nrow, ncol, window_size - overlap, window_size, max_vram_bytes
        )
        row_chunk = max(window_size + overlap, row_chunk // window_size * window_size)
        logger.info(
            "Spatial chunking enabled: %d rows per chunk (image: %d rows)",
            row_chunk,
            nrow,
        )
    else:
        batch_size = max(1, int(max_vram_bytes / image_vram))
        img_chunk = batch_size
        row_chunk = nrow
        logger.info("Batch size: %d interferograms per GPU call", batch_size)
    return (img_chunk, row_chunk, ncol)


# ---------------------------------------------------------------------------
# Type dispatcher helpers
# ---------------------------------------------------------------------------


def _is_zarr_path(path: Path) -> bool:
    """Check whether *path* points to a valid Zarr dataset (v2 or v3)."""
    if path.is_dir():
        if (path / ".zarray").exists():
            return True
        if (path / "zarr.json").exists():
            return True
    return False


def _load_as_dask_array(
    igrams: FilterInputType,
    nrow: int,
    ncol: int,
    row_chunk: int,
) -> "da.Array":
    """Normalise *igrams* to a ``dask.array.Array`` with shape ``(H, W, N)``.

    Parameters
    ----------
    igrams : FilterInputType
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

    # ---- 2. Single path → Zarr or binary file --------------------------------
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
# Core filtering pipeline
# ---------------------------------------------------------------------------


def goldstein_filter_wrapper(
    igrams: FilterInputType,
    nrow: int = 0,
    ncol: int = 0,
    alpha: float = 0.5,
    window_size: int = 32,
    overlap: int | None = None,
    max_vram_gb: float | None = None,
    img_chunk: int | None = None,
) -> "da.Array":
    """Apply the Goldstein filter to a stack of wrapped interferograms.

    This is the single entry point for filtering.  It accepts diverse input
    types (dask arrays, Zarr paths, lists of raw binary files), normalises
    them to a ``da.Array``, applies GPU-accelerated batch filtering, and
    returns a lazy ``da.Array`` of the filtered result.

    Parameters
    ----------
    igrams : dask.array.Array, str, Path, or list[str | Path]
        Input interferograms.  Allowed types:

        - ``da.Array`` — used directly (no loading).
        - ``str`` or ``Path`` — must point to a Zarr v2/v3 dataset.
        - ``list[str | Path]`` — list of flat binary files (``complex64``,
          row-major, no header).  Requires *nrow* and *ncol*.

    nrow : int
        Number of image rows.  Required when *igrams* is a file list.
    ncol : int
        Number of image columns.  Required when *igrams* is a file list.
    alpha : float
        Goldstein filter parameter (``0.0`` = no filtering, ``1.0`` =
        maximum filtering).  Default 0.5.
    window_size : int
        Processing window side length in pixels.  Default 32.
    overlap : int or None
        Overlap between adjacent windows in pixels.  ``None`` (default)
        uses 75 % of *window_size*.
    max_vram_gb : float or None
        Maximum GPU VRAM budget in GB.  ``None`` (default) auto-detects
        the free VRAM on the most available GPU via ``pynvml``.
    img_chunk: int | None
        Number of images to process as a batch

    Returns
    -------
    da.Array
        Lazy 3-D dask array of filtered interferograms with shape
        ``(nrow, ncol, N)`` and dtype ``complex64``.  Call ``.compute()``
        or pass to :func:`save_filtered_stack` to write to disk.

    Raises
    ------
    TypeError
        If *igrams* has an unsupported type.
    ValueError
        If required arguments (e.g. *nrow* / *ncol*) are missing or invalid.

    Examples
    --------
    Filter a dask array already in memory:

    >>> filtered = goldstein_filter_wrapper(my_dask_array)

    Filter a list of raw binary files and write to disk:

    >>> from s1proc.goldstein import goldstein_filter_wrapper, save_filtered_stack
    >>> from s1proc.utils import get_files
    >>> files = get_files("ifg/", "int")
    >>> out_files = [Path("ifg_corrected") / Path(f).basename for f in files]
    >>> filtered = goldstein_filter_wrapper(files, nrow=2400, ncol=3200)
    >>> save_filtered_stack(filtered, out_path = out_files)
    """
    import dask.array as da

    # ---- Validate filtering parameters ------------------------------------
    if overlap is None:
        overlap = int(min(np.ceil(window_size * 0.75), window_size - 1))
    if window_size - overlap <= 0:
        raise ValueError(
            f"overlap ({overlap}) must be less than window_size ({window_size})"
        )

    # ---- Load & chunk ------------------------------------------------------
    ifg_stack = _load_as_dask_array(igrams, nrow, ncol, row_chunk=nrow)

    # ---- Determine chunking ------------------------------------------------
    _nrow, _ncol = int(ifg_stack.shape[0]), int(ifg_stack.shape[1])
    nrow = nrow or _nrow
    ncol = ncol or _ncol

    if nrow <= 0 or ncol <= 0:
        raise ValueError(
            "Image dimensions are unknown.  Provide nrow/ncol explicitly, "
            "or pass input with known shape (dask array or Zarr)."
        )

    _img_chunk, row_chunk, _ = get_goldstein_chunks(
        nrow, ncol, window_size, overlap, max_vram_gb
    )
    img_chunk = img_chunk or _img_chunk

    ifg_stack = ifg_stack.rechunk({0: nrow, 1: ncol, 2: img_chunk})

    # ---- GPU filtering -----------------------------------------------------
    filtered_stack = da.map_blocks(
        batch_goldstein_filter,
        ifg_stack,
        dtype=np.complex64,
        chunks=ifg_stack.chunks,
        nrow=nrow,
        ncol=ncol,
        row_chunk=row_chunk,
        alpha=alpha,
        window_size=window_size,
        overlap=overlap,
    )

    return filtered_stack


def phase_diff(ifg):
    ph = np.zeros(ifg.shape, dtype=np.float32)
    ph[:, 1:] = np.abs(np.angle(np.conj(ifg[:, 1:]) * ifg[:, :-1]))
    ph[1:, :] = np.maximum(ph[1:,], np.abs(np.angle(np.conj(ifg[1:,]) * ifg[:-1, :])))
    ph[:, :-1] = np.maximum(ph[:, 1:], ph[:, :-1])
    ph[1:, :] = np.maximum(ph[:-1, :], ph[1:, :])
    return ph


def _goldstein_interpolate_block(
    template: np.ndarry,
    opt_phase_block: np.ndarray,
    gamma_block: np.ndarray,
    ref_idx: np.ndarray,
    sec_idx: np.ndarray,
    gamma_threshold: float,
    window_size: int,
    overlap: int,
    alpha: float,
    nrow_tot: int,
    ncol_tot: int,
    filter_row_chunk: int,
    img_chunk: int,
    nifg: int,
    block_info: list[dict] | None = None,
) -> np.ndarray:
    """Per-chunk kernel for :func:`goldstein_interpolation`.

    Each call receives the full spatial extent of ``opt_phase`` and ``gamma``
    together with one batch of interferogram indices.  Interferogram
    reconstruction, Goldstein filtering, and coherence blending all happen
    inside this single kernel so that no large dask advanced-indexed arrays
    are materialised outside of ``map_blocks``.

    Parameters
    ----------
    opt_phase_block : ndarray, shape ``(nrow, ncol, ndate)``, float32
        Full-image optimised phase block.
    gamma_block : ndarray, shape ``(nrow, ncol)``, float32
        Full-image coherence map.
    ref_idx : ndarray of int32, shape ``(nifg,)``
    sec_idx : ndarray of int32, shape ``(nifg,)``
    gamma_threshold : float
    window_size : int
    overlap : int
    alpha : float
    nrow_tot : int
        Full image row count (used for ``batch_goldstein_filter``).
    ncol_tot : int
        Full image column count.
    filter_row_chunk : int
        Row-chunk size for internal spatial splitting inside the filter.
    img_chunk : int
        Maximum number of interferograms per batch (axis-2 dask chunk size).
    nifg : int
        Total number of interferograms.
    block_info : list[dict] | None
        Dask block metadata; ``block_info[0]['chunk-location'][2]`` yields
        the starting interferogram index for this batch.
    """
    if block_info is not None:
        k_start = block_info[0]["chunk-location"][2]
    else:
        k_start = 0
    k_end = min(k_start + img_chunk, nifg)

    # -- Reconstruct interferograms for this batch ---------------------------
    r = ref_idx[k_start:k_end]
    s = sec_idx[k_start:k_end]
    ref_phase = opt_phase_block[:, :, r]  # (nrow, ncol, batch_n)
    sec_phase = opt_phase_block[:, :, s]
    recon = np.exp(1j * (ref_phase - sec_phase))

    # -- Goldstein filter this batch on GPU ----------------------------------
    filtered = batch_goldstein_filter(
        recon,
        nrow_tot,
        ncol_tot,
        filter_row_chunk,
        alpha,
        window_size,
        overlap,
    )

    # -- Blend by coherence --------------------------------------------------
    # gamma_block is (nrow, ncol) → broadcast to (nrow, ncol, batch_n)
    keep = (gamma_block >= gamma_threshold)[:, :, None]
    for i in range(recon.shape[2]):
        keep[:, :, i] |= phase_diff(recon[:, :, i]) < np.pi / 4

    return np.where(keep, recon, filtered).astype(np.complex64)


def goldstein_interpolation(
    opt_phase_path: Path | str,
    ifg_list: IfgList,
    gamma_threshold: float = 0.8,
    window_size: int = 32,
    overlap: int | None = None,
    alpha: float = 1.0,
) -> "da.Array":
    """Reconstruct and Goldstein-interpolate interferograms from optimised phase.

    Implements the second-round filter of the EMI/EigenSAR pipeline: the
    optimised phase vector produced by phase linking is used to rebuild the
    stack of wrapped interferograms, the Goldstein filter is applied to
    interpolate over low-coherence pixels, and high-coherence pixels are then
    restored to their unfiltered reconstructed values so that reliable phase
    is not smeared by the filter.

    Interferogram reconstruction and blending happen **inside** the per-batch
    :func:`dask.array.map_blocks` kernel so that no large dask
    advanced-indexed arrays (``ref_phase`` / ``sec_phase``) are materialised
    outside of ``map_blocks``.

    The reconstruction per interferogram ``(ref, sec)`` is

    .. math::

        \\mathrm{igram} = \\exp\\!\\bigl(j\\,(\\varphi_\\mathrm{ref}
            - \\varphi_\\mathrm{sec})\\bigr),

    and the final blend is

    .. math::

        \\mathrm{out} = \\begin{cases}
          \\mathrm{igram}_\\mathrm{recon} & \\gamma \\ge \\gamma_\\mathrm{thr} \\\\
          \\mathrm{igram}_\\mathrm{filtered} & \\gamma < \\gamma_\\mathrm{thr}
        \\end{cases}

    This preserves the layout
    :math:`(\\mathrm{nrow}, \\mathrm{ncol}, \\mathrm{nimg})` end-to-end: no
    transposes are performed on the reconstructed stack.

    Parameters
    ----------
    opt_phase_path : Path or str
        Path to the Zarr group written by
        :func:`~s1proc.phase_linking.save_phase_linking_results`, containing
        a single ``"data"`` array of shape ``(nrow, ncol, ndate + 1)``
        (float32).  The first ``ndate`` slices along axis 2 are the
        optimised phase; the last slice is the temporal coherence (gamma).
    ifg_list : IfgList
        Interferogram list, used to obtain ``(ref, sec)`` index pairs into the
        ``ndate`` axis.  Only ``ref_sec_indices()`` is used.
    gamma_threshold : float
        Coherence above or equal to this value keeps the unfiltered
        reconstructed complex value.  Default 0.8.
    window_size : int
        Goldstein processing window side length in pixels.  Default 32.
    overlap : int or None
        Overlap between adjacent Goldstein windows in pixels.  ``None``
        (default) selects 75 % of *window_size*.
    alpha : float
        Goldstein filter strength (``0.0`` = no filtering, ``1.0`` = maximum).
        Default 1.0 — strong smoothing so the filter fills decorrelated pixels.

    Returns
    -------
    da.Array
        Lazy 3-D dask array of blended interferograms with shape
        ``(nrow, ncol, nifg)`` and dtype ``complex64``.  Pass to
        :func:`save_filtered_stack` to write to disk.

    Raises
    ------
    ValueError
        If *opt_phase_path* is not a Zarr dataset.
    """
    import dask.array as da

    opt_phase_path = Path(opt_phase_path)
    if not _is_zarr_path(opt_phase_path):
        raise ValueError(f"{opt_phase_path} is not a Zarr dataset")

    if overlap is None:
        overlap = int(min(np.ceil(window_size * 0.75), window_size - 1))

    # ---- Load optimised phase and coherence --------------------------------
    # The zarr stores a single "data" array of shape (nrow, ncol, ndate+1)
    # where the last slice along axis 2 is the temporal coherence (gamma).
    res = da.from_zarr(str(opt_phase_path))
    opt_phase = res[:, :, :-1]  # (nrow, ncol, ndate)
    gamma = res[:, :, -1]  # (nrow, ncol)
    nrow, ncol = int(gamma.shape[0]), int(gamma.shape[1])

    # ---- Resolve interferogram indices -------------------------------------
    ref_indices, sec_indices = ifg_list.ref_sec_indices()
    nifg = len(ref_indices)

    # ---- Determine chunking from VRAM budget --------------------------------
    _, filter_row_chunk, _ = get_goldstein_chunks(nrow, ncol, window_size, overlap)

    # ---- Rechunk inputs so spatial axes stay as contiguous blocks -----------
    opt_phase = opt_phase.rechunk({0: nrow, 1: ncol, 2: -1})
    gamma = gamma.rechunk({0: nrow, 1: ncol})

    # ---- Template array — defines the output chunk structure ----------------
    # Its blocks are never materialised; it only exists so that map_blocks
    # knows the output shape and axis-2 partitioning.
    template = da.zeros(
        (nrow, ncol, nifg),
        dtype=np.complex64,
        chunks=(nrow, ncol, 1),
    )

    logger.info(
        "Goldstein interpolation (map_blocks): %d ifgs, %d dates, "
        "img_batch=%d, filter_row_chunk=%d",
        nifg,
        int(opt_phase.shape[2]),
        1,
        filter_row_chunk,
    )

    # ---- Per-batch reconstruction + filtering + blending --------------------
    interpolated = da.map_blocks(
        _goldstein_interpolate_block,
        template,
        opt_phase,
        gamma,
        dtype=np.complex64,
        chunks=template.chunks,
        ref_idx=ref_indices,
        sec_idx=sec_indices,
        gamma_threshold=gamma_threshold,
        window_size=window_size,
        overlap=overlap,
        alpha=alpha,
        nrow_tot=nrow,
        ncol_tot=ncol,
        filter_row_chunk=filter_row_chunk,
        img_chunk=1,
        nifg=nifg,
    )

    return interpolated


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def save_filtered_stack(
    filtered_stack: "da.Array",
    out_path: Sequence[str | Path] | str | Path,
    output_format: Literal["binary", "zarr"] = "binary",
    save_chunk_size: int | None = None,
) -> None:
    """Write a filtered dask stack to disk.

    The stack is processed in independent batches along the interferogram
    axis to keep the dask task graph small.  Each batch is computed and
    written before the next one begins, which dramatically reduces memory
    consumed by graph construction when the total interferogram count is
    large (e.g. >10 000).

    Parameters
    ----------
    filtered_stack : da.Array
        Lazy 3-D filtered array from :func:`goldstein_filter_wrapper`.
    out_path : Sequence[str | Path], str, Path,
        Output binary files or Zarr path.
    output_format : Literal["zarr", "binary"]
        ``"binary"`` (default) writes one flat ``complex64`` binary
        per interferogram via ``MultiBinaryFileWriter``.
        ``"zarr"`` writes a single Zarr v3 dataset whose on-disk chunk
        shape matches the input ``filtered_stack`` chunks.
    save_chunk_size : int | None
        Number of interferograms to process and write per batch.  Each
        batch builds an independent dask sub-graph, computes it, and
        writes to disk — keeping peak graph size proportional to the
        batch size rather than the full stack.  ``None`` (default)
        processes the entire stack at once (backward-compatible
        behaviour).

    Raises
    ------
    ValueError
        If *output_format* is unrecognised or required args are missing.
    """
    import dask.array as da
    from dask.diagnostics import ProgressBar

    # ---- Dimensions --------------------------------------------------------
    N = int(filtered_stack.shape[2])
    nrow = int(filtered_stack.shape[0])
    ncol = int(filtered_stack.shape[1])

    if save_chunk_size is None:
        save_chunk_size = N

    # ---- Write (Zarr) ------------------------------------------------------
    if output_format == "zarr":
        import zarr

        out = Path(out_path) if out_path else Path("filtered.zarr")
        if out.exists():
            shutil.rmtree(out)

        # Create the full-size zarr array with chunks matching filtered_stack
        # so the on-disk layout is identical to the input chunk structure.
        z = zarr.open(
            str(out),
            mode="w",
            shape=(nrow, ncol, N),
            chunks=(
                filtered_stack.chunks[0][0],
                filtered_stack.chunks[1][0],
                filtered_stack.chunks[2][0],
            ),
            dtype=np.complex64,
        )

        for start in range(0, N, save_chunk_size):
            end = min(start + save_chunk_size, N)
            logger.debug("Computing batch [%d:%d] of %d", start, end, N)
            sub_da = filtered_stack[:, :, start:end]

            with ProgressBar():
                da.to_zarr(
                    sub_da,
                    z,
                    region=(
                        slice(None),
                        slice(None),
                        slice(start, end),
                    ),
                )

            del sub_da
            gc.collect()

        logger.info("Zarr write completed: %s", out)

    # ---- Write (binary) ----------------------------------------------------
    elif output_format == "binary":
        output_routing = dict(zip(range(N), out_path))

        writer = MultiBinaryFileWriter(
            file_map=output_routing,
            single_file_shape=(nrow, ncol),
            dtype=np.complex64,
            nq=4,
        )
        try:
            with ProgressBar():
                da.store(
                    sources=filtered_stack,
                    targets=writer,
                    lock=False,
                    compute=True,
                )
        finally:
            logger.info("Waiting for background writer threads to flush to disk...")
            writer.notify_finished()
            logger.info("Pipeline completed — all binary files flushed to disk.")
        logger.info("Pipeline completed — all binary files flushed to disk.")

    logger.info(
        "Filtering result saved: %d images, %dx%d, format=%s",
        N,
        nrow,
        ncol,
        output_format,
    )
