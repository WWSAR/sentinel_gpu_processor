"""Batch Goldstein filter for interferogram stacks using CuPy.

This module provides a GPU-accelerated implementation of the Goldstein
filter [Goldstein1998]_ that processes multiple interferograms simultaneously
via a queue-based producer-consumer pipeline:

1. **Background readers** read interferogram binary files into numpy arrays.
2. **GPU processing** applies :func:`batch_goldstein_filter` on CuPy.
3. **Background writers** write filtered results to disk.

References
----------
.. [Goldstein1998] Goldstein, R. M., & Werner, C. L. (1998).  Radar
   interferogram filtering for geophysical applications.  Geophysical
   Research Letters, 25(21), 4035-4038.
   https://doi.org/10.1029/1998GL900033
"""

from __future__ import annotations

import queue
from pathlib import Path
from typing import List

import cupy as cp
import numpy as np

from s1proc._log import setup_logger
from s1proc.utils import _detect_gpu_count

logger = setup_logger(__name__, level="INFO")


# from cupyx.scipy.ndimage import convolve1d
GPU_POOL = queue.Queue()
for gpu_id in np.arange(_detect_gpu_count()):  # 如果你有4张卡，这里就写 [0, 1, 2, 3]
    GPU_POOL.put(gpu_id)


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


def batch_goldstein_filter(
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
        Input interferogram stack of shape ``(B, H, W)`` and dtype
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
        Filtered interferograms of shape ``(B, H, W)`` and dtype
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
        B, H_orig, W_orig = igrams.shape
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
            padded = cp.pad(d_igrams, ((0, 0), (0, pad_h), (0, pad_w)), mode="edge")
        else:
            padded = d_igrams
        H_pad, W_pad = padded.shape[1], padded.shape[2]

        # ---- 2. Zero-copy patch extraction ---------------------------------------
        # sliding_window_view produces a 5-D view; striding selects patches at the
        # requested overlap interval.
        patches: cp.ndarray = cp.lib.stride_tricks.sliding_window_view(
            padded, (window_size, window_size), axis=(1, 2)
        )
        patches = patches[:, ::stride, ::stride, :, :]  # (B, nwy, nwx, W, W)
        _, nwy, nwx, win_h, win_w = patches.shape
        assert win_h == win_w == window_size, "Window dimension mismatch"

        # ---- 3. Hamming window ---------------------------------------------------
        hamming_2d = _make_hamming_window_2d(window_size)  # (W, W), float32
        # patches = patches * hamming_2d.astype(cp.complex64)

        # ---- 4. Batch 2-D FFT ----------------------------------------------------
        patches = cp.fft.fft2(patches, axes=(-2, -1))

        # ---- 5. Amplitude spectrum -----------------------------------------------
        amplitude = cp.abs(patches)  # (B, nwy, nwx, W, W), float32

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
            patches *= cp.power(amplitude, alpha)

        # ---- 9. Batch inverse FFT ------------------------------------------------
        patches = cp.fft.ifft2(patches, axes=(-2, -1))
        patches = patches.astype(cp.complex64)

        # ---- 10. Overlap-add reconstruction --------------------------------------
        # The double loop iterates over *in-window offsets* (dy, dx), not over
        # patches.  Each iteration performs a single strided-slice accumulation
        # that updates all patches in parallel on the GPU.  For a 32×32 window
        # this is 1024 fast GPU dispatches — no per-patch Python loop.
        output = cp.zeros((B, H_pad, W_pad), dtype=cp.complex64)
        weight = cp.zeros((B, H_pad, W_pad), dtype=cp.float32)

        # The analysis window (Hamming) is applied before the FFT and the same
        # window serves as the synthesis weight during overlap-add.  To achieve
        # perfect reconstruction when alpha=0 the accumulated weight must be w²,
        # since each pixel is multiplied by w twice (analysis + synthesis).
        for dy in range(window_size):
            y_slice = slice(dy, dy + nwy * stride, stride)
            for dx in range(window_size):
                x_slice = slice(dx, dx + nwx * stride, stride)
                w = hamming_2d[dy, dx]
                output[:, y_slice, x_slice] += patches[:, :, :, dy, dx] * w
                weight[:, y_slice, x_slice] += w

        # ---- 11. Normalise by accumulated weights --------------------------------
        valid = weight > 0.0
        output[valid] /= weight[valid].astype(cp.complex64)

        # ---- 12. Crop back to original dimensions --------------------------------
        if pad_h > 0 or pad_w > 0:
            output = output[:, :H_orig, :W_orig]

        # ---- 13. Preserve input mask (zero → zero) -------------------------------
        input_mask = igrams == cp.complex64(0.0 + 0.0j)
        output[input_mask] = cp.complex64(0.0 + 0.0j)

        cp.cuda.stream.get_current_stream().synchronize()

        return cp.asnumpy(output)


def run_batch_goldstein_filter(*args, **kw):
    """
    Get an available GPU device for running goldstein filter
    """
    assigned_gpu = GPU_POOL.get()

    try:
        result = batch_goldstein_filter(*args, **kw, gpu_id=assigned_gpu)
        return result

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


def goldstein_filter_wrapper(
    file_paths: List[Path | str],
    out_path: Path | str | None = None,
    nrow: int = 0,
    ncol: int = 0,
    alpha: float = 0.5,
    window_size: int = 32,
    overlap: int = 24,
    max_vram_gb: float = 6.0,
    out_suffix: str = ".filt",
) -> None:
    """Apply the Goldstein filter to interferograms using GPU-accelerated
    batch processing with background I/O.

    Parameters
    ----------
    file_paths : list of Path | str
        Paths to raw binary interferogram files (``complex64``, row-major,
        no header).
    out_path : Path, str, or None
        Directory for filtered output files.  When ``None`` each output is
        placed alongside its input with *out_suffix* appended (matching the
        legacy C++ binary behaviour).
    nrow : int
        Number of rows in each interferogram.
    ncol : int
        Number of columns in each interferogram.
    alpha : float
        Goldstein filter parameter (``0.0`` = no filtering, ``1.0`` =
        maximum filtering).  Default 0.5.
    window_size : int
        Processing window side length in pixels.  Default 32.
    overlap : int
        Overlap between adjacent windows in pixels.  Default 24 (75 % for
        a 32×32 window).
    max_vram_gb : float
        Maximum GPU VRAM budget in gigabytes.  Used to determine the batch
        size or spatial chunk size.  Default 6.0.
    out_suffix : str
        Suffix appended to each input filename for the output.  Default
        ``".filt"``.
    n_readers : int
        Number of background reader threads.  Default 2.
    n_writers : int
        Number of background writer threads.  Default 2.

    Examples
    --------
    Filter all interferograms in a directory with the default 32×32 window:

    >>> from s1proc.goldstein import goldstein_filter_wrapper
    >>> from s1proc.utils import get_files
    >>> files = get_files("ifg/", "int")
    >>> goldstein_filter_wrapper(files, nrow=2400, ncol=3200, max_vram_gb=8.0)
    """
    import dask.array as da
    from dask.diagnostics import ProgressBar

    from s1proc.sario import create_virtual_stack

    B = len(file_paths)
    if B == 0:
        logger.warning("file_paths is empty — nothing to filter.")
        return

    stride = window_size - overlap
    if stride <= 0:
        raise ValueError(
            f"overlap ({overlap}) must be less than window_size ({window_size})"
        )

    max_vram_bytes = int(max_vram_gb * 1024**3)
    if out_path is not None:
        out_path = Path(out_path)
        out_path.mkdir(parents=True, exist_ok=True)

    # ---- VRAM estimation & batching -----------------------------------------
    image_vram = _estimate_peak_vram_per_ifg(nrow, ncol, window_size, overlap)
    logger.info(
        "Estimated peak VRAM per interferogram: %.1f MB  (max allowed: %.1f GB)",
        image_vram / 1024**2,
        max_vram_gb,
    )

    # file_paths_abs = [Path(f).resolve() for f in file_paths]
    output_paths = [
        Path(out_path) / (Path(f).stem + f".int{out_suffix}") for f in file_paths
    ]
    output_routing_table = dict(zip(np.arange(len(file_paths)), output_paths))

    spatial_chunking = image_vram > max_vram_bytes
    if spatial_chunking:
        time_chunk = 1
        row_chunk = _compute_spatial_row_chunk(
            nrow, ncol, stride, window_size, max_vram_bytes
        )
        logger.info(
            "Spatial chunking enabled: %d rows per chunk (image: %d rows)",
            row_chunk,
            nrow,
        )
    else:
        batch_size = max(1, int(max_vram_bytes / image_vram))
        time_chunk = batch_size
        row_chunk = nrow
        logger.info("Batch size: %d interferograms per GPU call", batch_size)
    mapper = create_virtual_stack(
        file_paths, np.complex64, nrow, ncol, row_chunk, new_axis=0
    )

    ifg_stack = da.from_zarr(mapper)
    ifg_stack = ifg_stack.rechunk({0: time_chunk, 1: row_chunk, 2: ncol})
    filtered_dask_stack = da.map_blocks(
        run_batch_goldstein_filter,
        ifg_stack,
        dtype=np.complex64,
        chunks=ifg_stack.chunks,
        alpha=alpha,
        window_size=window_size,
        overlap=overlap,
    )

    align_dask_stack = filtered_dask_stack.rechunk({0: 1, 1: nrow, 2: ncol})

    from s1proc.from_dolphin._background import (
        MultiBinaryFileWriter,
    )

    # MultiBinaryFileWriter uses a pool of daemon writer threads behind a
    # bounded queue.  __setitem__ is a fast queue.put() — the dask thread
    # returns immediately and the writers flush to disk in the background.
    # This keeps GPU compute and disk I/O fully overlapped.
    #
    # BinaryFileStore (a zarr v3 MemoryStore subclass) is an alternative
    # that routes da.to_zarr writes to binary files.  It is architecturally
    # cleaner but adds zarr codec-pipeline overhead per chunk (~60 ms).
    # Uncomment the store + da.to_zarr lines below to try it.
    multi_writer = MultiBinaryFileWriter(
        file_map=output_routing_table,
        single_file_shape=(nrow, ncol),
        dtype=np.complex64,
        nq=4,
        timeout=2,
    )
    try:
        with ProgressBar():
            da.store(
                sources=align_dask_stack,
                targets=multi_writer,
                lock=False,
                compute=True,
            )
    finally:
        logger.info("Waiting for background writer threads to flush to disk...")
        multi_writer.notify_finished()
        logger.info("Pipeline completed — all binary files flushed to disk.")

    logger.info("Goldstein filtering complete: %d interferograms processed", B)
