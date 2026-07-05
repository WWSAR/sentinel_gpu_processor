from __future__ import annotations

import abc
import concurrent.futures
import threading
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread, main_thread
from typing import Dict, Tuple

import numpy as np
from numpy.typing import DTypeLike

from s1proc._log import setup_logger

logger = setup_logger(__name__, level="INFO")

_DEFAULT_TIMEOUT = 0.5

__all__ = [
    "BackgroundReader",
    "BackgroundWorker",
    "BackgroundWriter",
]


class BackgroundWorker(abc.ABC):
    """Base class for doing work in a background thread.

    After instantiating an object, a client sends it work with the `queue_work`
    method and retrieves the result with the `get_result` method (hopefully
    after doing something else useful in between).  The worker remains active
    until `notify_finished` is called.  Subclasses must define the `process`
    method.

    Parameters
    ----------
    num_work_queue : int
        Max number of work items to queue before blocking, <= 0 for unbounded.
    num_results_queue : int
        Max number of results to generate before blocking, <= 0 for unbounded.
    store_results : bool
        Whether to store return values of `process` method.  If True then
        `get_result` must be called once for every `queue_work` call.
    timeout : float
        Interval in seconds used to check for finished notification once work
        queue is empty.

    Notes
    -----
    The usual caveats about Python threading apply.  It's typically a poor
    choice for concurrency unless the global interpreter lock (GIL) has been
    released, which can happen in IO calls and compiled extensions.

    """

    def __init__(
        self,
        num_work_queue=0,
        num_results_queue=0,
        store_results=True,
        drop_unfinished_results=False,
        timeout=_DEFAULT_TIMEOUT,
        name="BackgroundWorker",
    ):
        self.name = name
        self.store_results = store_results
        self.timeout = timeout
        self._finished_event = Event()
        self._work_queue = Queue(num_work_queue)
        if self.store_results:
            self._results_queue = Queue(num_results_queue)
        self._thread = Thread(target=self._consume_work_queue, name=name)
        self._thread.start()
        self._drop_unfinished_results = drop_unfinished_results

    def _consume_work_queue(self):
        while True:
            if not main_thread().is_alive():
                break

            logger.debug(f"{self.name} getting work")
            if self._finished_event.is_set():
                do_exit = self._drop_unfinished_results or (
                    self._work_queue.unfinished_tasks == 0
                )
                if do_exit:
                    break
                else:
                    # Keep going even if finished event is set
                    logger.debug(
                        f"{self.name} Finished... but waiting for work queue to empty,"
                        f" {self._work_queue.qsize()} items left,"
                        f" {self._work_queue.unfinished_tasks} unfinished"
                    )
            try:
                args, kw = self._work_queue.get(timeout=self.timeout)
                logger.debug(f"{self.name} processing")
                result = self.process(*args, **kw)
                self._work_queue.task_done()
                # Notify the queue that processing is done
                logger.debug(f"{self.name} got result")
            except Empty:
                logger.debug(f"{self.name} timed out, checking if done")
                continue

            if self.store_results:
                logger.debug(f"{self.name} saving result in queue")
                while True:
                    try:
                        self._results_queue.put(result, timeout=2)
                        break
                    except Full:
                        logger.debug(f"{self.name} result queue full, waiting...")
                        continue

    @abc.abstractmethod
    def process(self, *args, **kw):
        """User-defined task to operate in background thread."""

    def queue_work(self, *args, **kw):
        """Add a job to the work queue to be executed.

        Blocks if work queue is full.
        Same input interface as `process`.
        """
        if self._finished_event.is_set():
            msg = "Attempted to queue_work after notify_finished!"
            raise RuntimeError(msg)
        self._work_queue.put((args, kw))

    def get_result(self):
        """Get the least-recent value from the result queue.

        Blocks until a result is available.
        Same output interface as `process`.
        """
        while True:
            try:
                result = self._results_queue.get(timeout=self.timeout)
                self._results_queue.task_done()
                break
            except Empty as e:
                logger.debug(f"{self.name} get_result timed out, checking if done")
                if self._finished_event.is_set():
                    msg = "Attempted to get_result after notify_finished!"
                    raise RuntimeError(msg) from e
                continue
        return result

    def notify_finished(self, timeout=None):
        """Signal that all work has finished.

        Indicate that no more work will be added to the queue, and block until
        all work has been processed.
        If `store_results=True` also block until all results have been retrieved.
        """
        self._finished_event.set()
        if self.store_results and not self._drop_unfinished_results:
            self._results_queue.join()
        self._thread.join(timeout)

    def __del__(self):
        logger.debug(f"{self.name} notifying of exit")
        self.notify_finished()


class BackgroundWriter(BackgroundWorker):
    """Base class for writing data in a background thread.

    After instantiating an object, a client sends it data with the `queue_write`
    method.  The writer remains active until `notify_finished` is called.
    Subclasses must define the `write` method.

    Parameters
    ----------
    nq : int
        Number of write jobs that can be queued before blocking, <= 0 for
        unbounded.  Default is 1.
    timeout : float
        Interval in seconds used to check for finished notification once write
        queue is empty.

    """

    def __init__(self, nq=1, timeout=_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(
            num_work_queue=nq,
            store_results=False,
            timeout=timeout,
            **kwargs,
        )

    # rename queue_work -> queue_write
    def queue_write(self, *args, **kw):
        """Add data to the queue to be written.

        Blocks if write queue is full.
        Same interfaces as `write`.
        """
        self.queue_work(*args, **kw)

    # rename process -> write
    def process(self, *args, **kw):
        self.write(*args, **kw)

    @property
    def num_queued(self):
        """Number of items waiting in the queue to be written."""
        return self._work_queue.qsize()

    @abc.abstractmethod
    def write(self, *args, **kw):
        """User-defined method for writing data."""


class BackgroundReader(BackgroundWorker):
    """Base class for reading data in a background thread (pre-fetching).

    After instantiating an object, a client sends it data selection parameters
    (slices, indices, etc.) via the `queue_read` method and retrieves the result
    with the `get_data` method.  In order to get useful concurrency, that
    usually means you'll want to queue the read for the next data block before
    starting work on the current block.  The reader remains active until
    `notify_finished` is called and all blocks have been retrieved.  Subclasses
    must define the `read` method.

    Parameters
    ----------
    nq : int
        Number of read results that can be stored before blocking, <= 0 for
        unbounded.  Default is 1.
    timeout : float
        Interval in seconds used to check for finished notification once write
        queue is empty.

    """

    def __init__(self, nq=1, timeout=_DEFAULT_TIMEOUT, **kwargs):
        super().__init__(
            num_results_queue=nq,
            timeout=timeout,
            store_results=True,
            # If we're reading data, we don't care about the result queue
            drop_unfinished_results=True,
            **kwargs,
        )

    # rename queue_work -> queue_read
    def queue_read(self, *args, **kw):
        """Add selection parameters (slices, etc.) to the read queue to be processed.

        Same input interface as `read`.
        """
        self.queue_work(*args, **kw)

    # rename get_result -> get_data
    def get_data(self):
        """Retrieve the least-recently read chunk of data.

        Blocks until a result is available.
        Same output interface as `read`.
        """
        return self.get_result()

    # rename process -> read
    def process(self, *args, **kw):
        return self.read(*args, **kw)

    @abc.abstractmethod
    def read(self, *args, **kw):
        """User-defined method for reading a chunk of data."""


class MultiBinaryFileWriter:
    """Multi-file parallel routing writer for ``da.store``.

    Routes and writes incoming Dask chunks into multiple independent flat binary
    files concurrently using a thread pool.  Each chunk's band dimension is
    unpacked and each band is written to the corresponding file via a dedicated
    writer thread.

    A bounded semaphore provides backpressure so that GPU computation does not
    outrun disk I/O by an unbounded margin.  Per-file locks guard against
    concurrent writes to the same file that can occur during spatial chunking.
    """

    def __init__(
        self,
        file_map: Dict[int, Path],
        single_file_shape: Tuple[int, int],
        dtype: DTypeLike,
        nq: int = 4,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self.file_map = {k: Path(v) for k, v in file_map.items()}
        self.rows, self.cols = single_file_shape
        self.dtype = np.dtype(dtype)
        self.row_stride = self.cols * self.dtype.itemsize
        self.single_file_size = self.rows * self.cols * self.dtype.itemsize

        # Pre-allocate all output files to final size so that partial spatial
        # writes can seek into an existing file without a create-or-open race.
        for path in self.file_map.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists() or path.stat().st_size != self.single_file_size:
                with open(path, "wb") as f:
                    f.truncate(self.single_file_size)

        # One worker per file, capped so we don't oversubscribe the disk.
        n_workers = min(len(self.file_map), 8)
        self._executor: concurrent.futures.ThreadPoolExecutor = (
            concurrent.futures.ThreadPoolExecutor(max_workers=n_workers)
        )

        # Backpressure: block __setitem__ once too many writes are in flight.
        # The semaphore value is (nq × workers) so the user can tune the queue
        # depth independently of the worker count.
        self._max_pending = max(1, nq * n_workers)
        self._write_sem: threading.BoundedSemaphore = threading.BoundedSemaphore(
            self._max_pending,
        )

        # Per-file locks serialise writes to the same file when multiple spatial
        # chunks target the same band (spatial chunking mode).
        self._file_locks: Dict[int, threading.Lock] = {
            idx: threading.Lock() for idx in self.file_map
        }

        self._futures: list[concurrent.futures.Future] = []
        self._futures_lock: threading.Lock = threading.Lock()
        self.timeout = timeout

        logger.debug(
            "MultiBinaryFileWriter initialised: %d workers, max %d in-flight writes",
            n_workers,
            self._max_pending,
        )

    # ------------------------------------------------------------------
    # Dask store protocol
    # ------------------------------------------------------------------

    def __setitem__(self, key: tuple[slice, slice, slice], data: np.ndarray) -> None:
        """Dask entry point: ``target[key] = data``.

        Submits the write to the thread pool and returns immediately so that
        GPU computation can proceed.  If the in-flight write limit is reached
        the calling thread blocks here, providing natural backpressure.
        """
        self._write_sem.acquire()
        try:
            future: concurrent.futures.Future = self._executor.submit(
                self._write_with_cleanup, key, data
            )
            with self._futures_lock:
                self._futures.append(future)
        except Exception:
            self._write_sem.release()
            raise

    def _write_with_cleanup(
        self, key: tuple[slice, slice, slice], data: np.ndarray
    ) -> None:
        """Wrapper that releases the backpressure semaphore after write."""
        try:
            self.write(key, data)
        finally:
            self._write_sem.release()

    # ------------------------------------------------------------------
    # Core write logic
    # ------------------------------------------------------------------

    def write(self, key: tuple[slice, slice, slice], data: np.ndarray) -> None:
        """Route chunk bands to target files and write them to disk.

        Parameters
        ----------
        key : tuple of slice
            Global 3-D slice ``(band_slice, row_slice, col_slice)``.
        data : np.ndarray
            3-D block of shape ``(N_bands, chunk_rows, chunk_cols)``.
        """
        b_slice, r_slice, c_slice = key

        b_start = b_slice.start if b_slice.start is not None else 0
        b_stop = b_slice.stop if b_slice.stop is not None else data.shape[0]

        r_start = r_slice.start if r_slice.start is not None else 0
        c_start = c_slice.start if c_slice.start is not None else 0

        is_full = (
            r_start == 0
            and c_start == 0
            and data.shape[1] == self.rows
            and data.shape[2] == self.cols
        )

        for local_idx, global_band_idx in enumerate(range(b_start, b_stop)):
            target_file = self.file_map.get(global_band_idx)
            single_band_data = data[local_idx, :, :]

            if not single_band_data.flags["C_CONTIGUOUS"]:
                single_band_data = np.ascontiguousarray(single_band_data)

            with self._file_locks[global_band_idx]:
                if is_full:
                    # Fast path: single contiguous write of the full image.
                    single_band_data.tofile(str(target_file))
                else:
                    spatial_offset = (
                        r_start * self.row_stride + c_start * self.dtype.itemsize
                    )
                    with open(target_file, "r+b") as f:
                        f.seek(spatial_offset)
                        f.write(single_band_data.tobytes())

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def notify_finished(self, timeout: float | None = None) -> None:
        """Block until all queued writes have been flushed to disk."""
        if timeout is None:
            timeout = getattr(self, "timeout", None)
        with self._futures_lock:
            futures_snapshot = list(self._futures)
        if futures_snapshot:
            concurrent.futures.wait(futures_snapshot, timeout=timeout)
        self._executor.shutdown(wait=True)
        logger.debug("MultiBinaryFileWriter: all writes flushed, executor shut down.")

    def __del__(self) -> None:
        if hasattr(self, "_executor"):
            self._executor.shutdown(wait=False)
