# MultiBinaryFileWriter: evolution across four revisions

The `MultiBinaryFileWriter` class (in `s1proc/from_dolphin/_background.py`) has gone
through four distinct designs across commits `8c08caf` → `aadfe28` → `6938974` →
`aa6e927`.  This document explains what each version did and why the final version
works.

---

## Shared context (unchanged across all versions)

- The output side of `goldstein_filter_wrapper` re-chunks the filtered dask array to
  `{0: 1, 1: nrow, 2: ncol}` — i.e. **each chunk is one full interferogram** (band
  dimension = 1, spatial dims = full image).  The 200 input files therefore become
  200 independent output chunks.
- Every chunk targets a **different** output file.  No two chunks ever write to the
  same file concurrently in the common code path.
- The `GPU_POOL` inside `run_batch_goldstein_filter` releases the GPU back to the pool
  *before* the numpy array is returned to the dask scheduler, so the GPU is free for
  another worker while the current chunk is being written.
- The baseline to beat: `da.to_zarr(mapper)` finishes in ~60 seconds.

---

## Version 1 — `BackgroundWriter` base class (commit `8c08caf`)

```
Architecture:
  dask worker → __setitem__ → queue_write(key, data)
                                  │
                                  ▼
                        Queue(maxsize=nq=2)  ← bounded
                                  │
                                  ▼
                     BackgroundWorker._thread (1 thread)
                                  │
                                  ▼
                           write(key, data)
```

**How it worked:** `MultiBinaryFileWriter` inherited from `BackgroundWriter`, which
launches a single `BackgroundWorker` daemon thread consuming from a plain
`queue.Queue`.  `__setitem__` called `queue_write`, which dropped the `(key, data)`
tuple into the queue.  The single worker thread popped items and called `write()`.

**Why it was slow:** The queue was bounded to `nq=2`.  After two chunks were
enqueued, the third `queue_write` blocked — and because dask's scheduler calls
`__setitem__` from its own worker threads, the GPU-scheduling thread itself blocked.
All writes to all 200 files were serialized through **one thread**.  For a 30 MB
interferogram the write takes a few hundred milliseconds; the GPU finished its next
chunk in less time and then sat idle waiting for the queue to drain.  Total runtime
was **~100 seconds** (40 % slower than `to_zarr`).

**Other issues:**
- `notify_finished()` was called in the `finally` block, but `__del__` on the
  parent class tried to call it again (double-join).
- The `r+b` / `wb` branching in `write()` was fragile on Windows (file sharing
  semantics).

---

## Version 2 — `ThreadPoolExecutor` + `BoundedSemaphore` (commit `aadfe28`)

```
Architecture:
  dask worker → __setitem__ → semaphore.acquire()
                               executor.submit(write)
                               return immediately
                                  │
                                  ▼
                     ThreadPoolExecutor (≤8 threads)
                                  │
                                  ▼
                           write(key, data)
                               semaphore.release()
```

**How it worked:** `MultiBinaryFileWriter` stopped inheriting from `BackgroundWriter`
and became standalone.  It created a `ThreadPoolExecutor` with up to 8 worker
threads.  `__setitem__` acquired a `BoundedSemaphore`, submitted `write()` to the
executor, and returned.  A future list was tracked for `notify_finished()`.  It also
pre-allocated **all** output files at init time with `f.truncate()`.

**Why it was even slower:**

1. **Pre-allocation disaster** — `f.truncate(file_size)` on Windows zero-fills the
   file.  For 430+ files at 30 MB each that is ~13 GB of synchronous sequential
   writes at init time.  This alone added **3+ minutes** before any GPU work began.

2. **Executor overhead** — Submitting 200 futures, tracking them in a list with a
   lock, acquiring/releasing a semaphore, and the executor's internal task queue
   added significant per-chunk latency.  The `notify_finished()` call waited on all
   200 futures plus `executor.shutdown(wait=True)`, taking **30+ seconds** to drain.

3. **Too many threads for the actual workload** — 8 threads writing to the same
   physical disk (sometimes the same disk controller) created contention.

Total runtime was **>85 seconds** plus the 3+ minute init — far worse than v1.

---

## Version 3 — Synchronous inline write (commit `6938974`)

```
Architecture:
  dask worker → __setitem__ → write(key, data)  [blocks worker]
```

**How it worked:** All threading was removed.  `__setitem__` called `write()`
directly from the calling dask thread.  No executor, no semaphore, no pre-allocation
— just `band_data.tofile(str(target_file))`.

**Why it was still slow (~85 seconds):**  The dask scheduler has a **finite thread
pool** (default = number of CPU cores).  When a dask worker thread calls
`__setitem__` and blocks on `tofile()` for 200–400 ms, that thread is **unavailable**
to schedule or execute the next GPU task.  With, say, 8 dask worker threads and 200
chunks, the pipeline throttled down to however many threads were not currently stuck
in `tofile()`.  The GPU — which could process a chunk in ~150 ms — spent much of its
time idle because the thread that should have fed it the next chunk was busy writing
the previous one to disk.

In other words: v3 fixed the pre-allocation problem but re-introduced the blocking
problem that v1's queue was *trying* (but failing) to solve.

---

## Version 4 — Current: Multi-threaded daemon writer pool (commit `aa6e927`)

```
Architecture:
  dask worker → __setitem__ → queue.put(key, data)  [~0 μs, returns immediately]
       │                              │
       │  GPU released,               ▼
       │  worker free to         Queue(maxsize=nq×n_writers=8)
       │  acquire next GPU             │
       │  task immediately        ┌────┼────┬────┐
       ▼                         ▼    ▼    ▼    ▼
  [next GPU task]              Writer threads (4 daemons)
                                   │
                                   ▼
                              write_impl(key, data)
                                   │
                                   ▼
                              tofile() → disk
```

**How it works:**

1. `__setitem__` does a single `queue.put((key, data))` and **returns immediately**.
   The dask thread that called it is free to acquire the GPU and start the next
   chunk.

2. Four daemon writer threads drain the queue in parallel and call `write_impl()`.
   Since the rechunk guarantees each chunk targets a different file, no locking is
   needed on the fast path (`tofile()`).

3. The bounded queue (`maxsize = nq × n_writers = 4 × 4 = 16` for typical defaults)
   provides **automatic backpressure** — if the writers fall behind, `queue.put()`
   blocks until a slot opens.  This prevents unbounded memory growth without any
   semaphore, future, or executor machinery.

4. `notify_finished()` pushes one `None` sentinel per writer thread into the queue
   and joins each thread.  This is instantaneous when the queue is already empty.

5. No files are pre-allocated — `tofile()` creates the file on first write.

**Why this is fast:**

| Property | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| Init time (430 files) | ~0 s | 3+ min | ~0 s | **~0 s** |
| GPU-blocking writes | yes (via queue) | no | yes (inline) | **no** |
| Write parallelism | 1 thread | 8 threads | 1 thread | **4 threads** |
| Per-chunk overhead | Queue put | Semaphore + Future | none | **Queue put** |
| Shutdown drain time | ~0 s | 30+ s | ~0 s | **~0 s** |
| Backpressure mechanism | Queue(nq=2) | Semaphore | none | **Queue(nq×n)** |

The critical insight: **write I/O, GPU compute, and thread scheduling must overlap**.
The dask worker thread exists to shuttle data between the GPU and the writer — it
should never block on either.  v4 achieves this with the simplest possible
abstraction: a shared queue and a pool of writer daemons.

---

## Key design decisions in v4

### Why daemon threads?

Daemon threads are killed when the process exits.  If the pipeline crashes, the
writer threads do not keep the process alive.  The `__del__` fallback attempts to
send sentinels but does not block — it is a best-effort cleanup.

### Why `Queue` instead of `ThreadPoolExecutor`?

`ThreadPoolExecutor.submit()` returns a `Future` that must be tracked and waited on.
A plain `Queue` with sentinel-based shutdown has zero per-item allocation overhead
and the writers are simple `while get() != None` loops.

### Why `queue.Queue` instead of `collections.deque`?

`queue.Queue` is thread-safe with blocking `put`/`get` — exactly the semantics
needed.  A `deque` would require an external `Condition` or `Semaphore` for the
same behaviour.

### Why 4 writer threads?

Empirically, 4 threads saturate a single SATA SSD or NVMe drive for sequential
writes of 30 MB chunks.  More threads create contention without additional
throughput.  The value is configurable via `n_writers=`.

### Why no per-file locks on the fast path?

The `rechunk({0: 1, 1: nrow, 2: ncol})` guarantees that each chunk writes exactly
one full interferogram to a unique file.  No two chunks share a target file, so the
lock is unnecessary overhead.  The lock exists only for the spatial-chunking code
path (`row_chunk < nrow`), which is triggered when the image is too large to fit in
GPU memory as a single chunk.
