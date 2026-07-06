from __future__ import annotations

import threading
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Dict, Tuple

import numpy as np
from numpy.typing import DTypeLike
from zarr.core.buffer.core import Buffer
from zarr.storage import MemoryStore

from s1proc._log import setup_logger

logger = setup_logger(__name__, level="INFO")


class MultiBinaryFileWriter:
    """Multi-file parallel writer for ``da.store``.

    Writes are dispatched to a pool of background writer threads via a bounded
    queue.  ``__setitem__`` returns as soon as the chunk is queued, freeing the
    calling dask thread to submit the next GPU task.  The bounded queue provides
    natural backpressure when I/O outruns compute.

    Per-file locks guard against concurrent writes to the same file during
    spatial chunking; the common full-frame fast path writes lock-free.
    """

    def __init__(
        self,
        file_map: Dict[int, Path],
        single_file_shape: Tuple[int, int],
        dtype: DTypeLike,
        nq: int = 2,
        n_writers: int = 4,
    ) -> None:
        self.file_map = {k: Path(v) for k, v in file_map.items()}
        self.rows, self.cols = single_file_shape
        self.dtype = np.dtype(dtype)
        self.row_stride = self.cols * self.dtype.itemsize
        self.single_file_size = self.rows * self.cols * self.dtype.itemsize

        # Ensure output directory exists.
        first = next(iter(self.file_map.values()))
        first.parent.mkdir(parents=True, exist_ok=True)

        # Per-file locks only needed for spatial-chunking (partial-write) path.
        self._locks: Dict[int, threading.Lock] = {
            idx: threading.Lock() for idx in self.file_map
        }

        # Bounded queue feeding a pool of writer threads.
        max_pending = max(1, nq * n_writers)
        self._queue: Queue = Queue(maxsize=max_pending)
        self._n_writers = n_writers
        self._writers: list[Thread] = []
        for i in range(n_writers):
            t = Thread(target=self._write_loop, daemon=True, name=f"mbfw-{i}")
            t.start()
            self._writers.append(t)

    # ------------------------------------------------------------------
    # Writer thread loop
    # ------------------------------------------------------------------

    def _write_loop(self) -> None:
        """Drain the queue, writing chunks until a sentinel is received."""
        while True:
            item = self._queue.get()
            if item is None:  # shutdown sentinel
                break
            key, data = item
            self._write_impl(key, data)

    # ------------------------------------------------------------------
    # Core write logic
    # ------------------------------------------------------------------

    def _write_impl(
        self,
        key: tuple[slice, slice, slice],
        data: np.ndarray,
    ) -> None:
        """Route chunk bands to target files and write them to disk.

        Parameters
        ----------
        key : tuple of slice
            Global 3-D slice ``(row_slice, col_slice, band_slice)``.
        data : np.ndarray
            3-D block of shape ``(chunk_rows, chunk_cols, N_bands)``.
        """
        r_slice, c_slice, b_slice = key

        b_start = b_slice.start if b_slice.start is not None else 0
        b_stop = b_slice.stop if b_slice.stop is not None else data.shape[2]

        r_start = r_slice.start if r_slice.start is not None else 0
        c_start = c_slice.start if c_slice.start is not None else 0

        is_full = (
            r_start == 0
            and c_start == 0
            and data.shape[0] == self.rows
            and data.shape[1] == self.cols
        )

        for local_idx, global_band_idx in enumerate(range(b_start, b_stop)):
            target_file = self.file_map[global_band_idx]
            band_data = data[:, :, local_idx]

            if not band_data.flags["C_CONTIGUOUS"]:
                band_data = np.ascontiguousarray(band_data)

            if is_full:
                # Fast path: full-frame contiguous write.  ``tofile`` creates
                # (or overwrites) the file in a single OS-level write.
                band_data.tofile(str(target_file))
            else:
                offset = r_start * self.row_stride + c_start * self.dtype.itemsize
                with self._locks[global_band_idx]:
                    # Lazy-create and size the file on first partial write.
                    if not target_file.exists():
                        with open(target_file, "wb") as f:
                            f.truncate(self.single_file_size)
                    with open(target_file, "r+b") as f:
                        f.seek(offset)
                        f.write(band_data.tobytes())

    # ------------------------------------------------------------------
    # Dask store protocol
    # ------------------------------------------------------------------

    def __setitem__(
        self,
        key: tuple[slice, slice, slice],
        data: np.ndarray,
    ) -> None:
        """Enqueue the chunk for asynchronous write.

        Blocks only when the queue is full (backpressure).
        """
        self._queue.put((key, data))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def notify_finished(self, timeout: float | None = None) -> None:
        """Signal writer threads to shut down and wait for completion."""
        for _ in range(self._n_writers):
            self._queue.put(None)  # sentinel
        for t in self._writers:
            t.join(timeout)

    def __del__(self) -> None:
        for _ in range(getattr(self, "_n_writers", 0)):
            try:
                self._queue.put_nowait(None)
            except Full:
                pass


class BinaryFileStore(MemoryStore):
    """A zarr v3 ``Store`` that routes chunk writes to flat binary files.

    Inherits from :class:`zarr.storage.MemoryStore` so that zarr v3
    metadata (``zarr.json``) lives in memory, but chunk data is written
    directly to individual flat binary files instead of being buffered in
    RAM.  Designed to be passed as the ``store`` argument to
    ``zarr.create_array`` / ``da.to_zarr``, inheriting zarr's native
    parallel I/O path.

    Chunk keys are of the form ``"c/{row}/{col}/{band}"`` (zarr v3
    default for a ``(nrow, ncol, nimg)`` array).  The band index selects
    the target file from *file_map*; row/col indices determine the in-file
    byte offset for spatial chunking.
    """

    def __init__(
        self,
        file_map: Dict[int, Path],
        single_file_shape: Tuple[int, int],
        dtype: DTypeLike,
        read_only: bool = False,
    ) -> None:
        super().__init__(read_only=read_only)
        self.file_map = {k: Path(v) for k, v in file_map.items()}
        self.rows, self.cols = single_file_shape
        self.dtype = np.dtype(dtype)
        self.bytes_per_elem = self.dtype.itemsize
        self._chunk_rows = self.rows  # may differ under spatial chunking
        self._chunk_cols = self.cols

        # Ensure the output directory exists.
        first = next(iter(self.file_map.values()))
        first.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Overrides: route chunk data to files
    # ------------------------------------------------------------------

    async def set(
        self,
        key: str,
        value: Buffer,
        byte_range: tuple[int, int] | None = None,
    ) -> None:
        """Store a (key, value) pair.

        Metadata keys (``zarr.json``) are stored in the parent
        ``MemoryStore``.  Chunk keys (``c/...``) are written directly
        to the target flat binary file, then cached in ``_store_dict``
        so that ``exists()`` and ``list()`` work without disk reads.
        """
        if not key.startswith("c/"):
            await super().set(key, value, byte_range)
            return

        # Parse "c/{row}/{col}/{band}".
        _prefix, row_str, _col_str, band_str = key.split("/")
        band_idx = int(band_str)
        row_chunk_idx = int(row_str)
        target_file = self.file_map[band_idx]
        data = value.to_bytes()

        if self._chunk_rows == self.rows:
            target_file.write_bytes(data)
        else:
            offset = row_chunk_idx * self._chunk_rows * self.cols * self.bytes_per_elem
            if not target_file.exists():
                with open(target_file, "wb") as f:
                    f.truncate(self.rows * self.cols * self.bytes_per_elem)
            with open(target_file, "r+b") as f:
                f.seek(offset)
                f.write(data)

        self._store_dict[key] = value
