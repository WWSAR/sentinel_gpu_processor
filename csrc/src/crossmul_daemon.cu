/**
 * crossmul_daemon — long-running GPU interferogram processor.
 *
 * Architecture (4-stage decoupled pipeline with double-buffering)
 * ---------------------------------------------------------------
 * 1.  Reads a burst-pair task list from a text file specified via
 *     ``--tasks-file <path>`` (one ``<ref_slc> <sec_slc> <out_ifg>``
 *     per line).
 * 2.  **Scan pass** — reads every SLC header to determine the
 *     maximum overlap dimensions AND maximum raw read size across
 *     all tasks.
 * 3.  Allocates two pools of **page-locked (pinned) host buffers**:
 *     - ``RawBuffer`` pool (Stage 1 uncropped I/O buffers).
 *     - ``TaskSlot`` pool (Stage 2+ cropped buffers + result staging).
 * 4.  **Stage 1** — Multi-threaded I/O producer(s): reads headers,
 *     computes overlap, checks the global reference-image cache.  On a
 *     cache hit the reference data is copied from a pre-loaded pinned
 *     buffer (zero disk I/O); on a miss the full reference image is
 *     read into the cache.  Secondary images are always read from disk.
 * 5.  **Stage 2** — CPU worker thread pool (4-8 threads): pops
 *     RawBuffers, crops them into TaskSlots via ``crop_memory_buffer``,
 *     releases the RawBuffer back to its free pool.
 * 6.  **Stage 3** — One GPU consumer thread per device: each manages
 *     multiple internal execution lanes with dedicated CUDA streams.
 *     Uses non-blocking ``cudaStreamQuery`` polling.  On completion,
 *     pushes a ``DiskWriteItem`` to the disk-write queue and
 *     immediately resets the lane to IDLE (NO synchronous disk I/O).
 * 7.  **Stage 4** — Single-threaded async disk writer: pops
 *     ``DiskWriteItem`` entries, calls ``save_binary``, releases the
 *     underlying TaskSlot back to the free pool.
 *
 * All memory is allocated once at startup via ``cudaMallocHost``.
 * No inner-loop heap allocations (``new``/``delete``, ``malloc``/
 * ``free``) are performed on any hot path.
 */

#include "gpu_device.hpp"
#include "sario.hpp"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cuda_runtime.h>
#include <fstream>
#include <iostream>
#include <mutex>
#include <queue>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#endif

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

// -----------------------------------------------------------------------
// CUDA error-check helper
// -----------------------------------------------------------------------
#define CHECK_CUDA(x)                                                          \
  do {                                                                         \
    cudaError_t err = (x);                                                     \
    if (err != cudaSuccess) {                                                  \
      std::cerr << "[crossmul_daemon] CUDA error " << cudaGetErrorString(err)  \
                << " at " << __FILE__ << ":" << __LINE__ << std::endl;         \
      std::abort();                                                            \
    }                                                                          \
  } while (0)

// -----------------------------------------------------------------------
// Scoped timer (debug / verbose output)
// -----------------------------------------------------------------------
class ScopedTimer {
public:
  explicit ScopedTimer(const std::string &name, bool enabled = true)
      : name_(name), enabled_(enabled),
        start_(std::chrono::steady_clock::now()) {}

  ~ScopedTimer() {
    if (!enabled_)
      return;
    auto end = std::chrono::steady_clock::now();
    auto duration = std::chrono::duration<double>(end - start_).count();
    std::cerr << "[crossmul_daemon|TIMER] " << name_ << " Elapsed: " << duration
              << " s" << std::endl;
  }

  ScopedTimer(const ScopedTimer &) = delete;
  ScopedTimer &operator=(const ScopedTimer &) = delete;

private:
  std::string name_;
  bool enabled_;
  std::chrono::steady_clock::time_point start_;
};

// -----------------------------------------------------------------------
// Data types
// -----------------------------------------------------------------------

/** A single burst-pair work item. */
struct Task {
  std::string ref_slc;
  std::string sec_slc;
  std::string out_ifg;
};

// -----------------------------------------------------------------------
// Stage 1 → Stage 2: RawBuffer (uncropped I/O buffer)
// -----------------------------------------------------------------------

/**
 * Holds raw (uncropped) SLC blocks read directly from disk by Stage 1.
 *
 * Sized to ``max_raw_elements`` — the maximum ``src_w * copy_h`` product
 * across all tasks.  Stage 2 crops from this buffer into a :struct:`TaskSlot`.
 */
struct RawBuffer {
  int id;

  // -- Pinned host buffers (pre-allocated, sized to max_raw_elements) --
  Complex *ref_data;
  Complex *sec_data;

  // -- Task identity --
  Task task;

  // -- Crop parameters for Stage 2 (set by Stage 1) --
  // Source widths (full image widths)
  int src_w_ref;
  int src_w_sec;

  // Bounds of the reference image
  int left_ref;
  int top_ref;
  int right_ref;
  int bottom_ref;

  // Bounds of the secondary image
  int left_sec;
  int top_sec;
  int right_sec;
  int bottom_sec;

  // Bounds of the cropped image
  int left_dst;
  int top_dst;
  int right_dst;
  int bottom_dst;

  // -- Overlap / output dimensions --
  int nrow;
  int ncol;
  int nrow_sm;
  int ncol_sm;

  // -- Pre-computed output header --
  std::int32_t ifg_header[NHEADER];

  // -- Lifecycle: 0 = free, 1 = filling (Stage 1), 2 = cpu_ready --
  std::atomic<int> state{0};
};

// -----------------------------------------------------------------------
// Stage 2 → Stage 4: TaskSlot (cropped data + result staging)
// -----------------------------------------------------------------------

/**
 * Holds cropped overlap data ready for GPU processing (Stage 2 output).
 *
 * Also owns the **result staging buffers** where Stage 3's D2H transfers
 * land.  This allows the GPU lane to be released immediately after the
 * stream completes — the staging buffer ownership stays with the slot
 * until Stage 4 finishes writing it to disk.
 */
struct TaskSlot {
  int id;

  // -- Pinned host buffers (pre-allocated, sized to max_elements) --
  Complex *ref_data;
  Complex *sec_data;

  // -- Pinned host result staging (D2H target, sized to max_elements_sm) --
  Complex *result_cpx;
  float *result_float;

  // -- Task identity --
  Task task;

  // -- Actual dimensions for this burst pair (<= max) --
  int nrow;
  int ncol;
  int nrow_sm;
  int ncol_sm;

  // -- Pre-computed output header --
  std::int32_t ifg_header[NHEADER];

  // -- Lifecycle: 0 = free, 1 = gpu_ready, 2 = processing --
  std::atomic<int> state{0};
};

// -----------------------------------------------------------------------
// Stage 3 → Stage 4: DiskWriteItem
// -----------------------------------------------------------------------

/**
 * Lightweight wrapper pushed by Stage 3 when a GPU lane completes.
 *
 * Carries a pointer into the owning :struct:`TaskSlot`'s staging buffer.
 * Stage 4 writes the data to disk, then releases the slot.
 */
struct DiskWriteItem {
  int slot_idx; // TaskSlot to release after write
  void *data;   // pointer to TaskSlot::result_cpx or result_float
  int n_elements;
  std::int32_t header[NHEADER];
  std::string out_ifg;
  bool is_float;
};

// -----------------------------------------------------------------------
// GPU execution lane (per-stream resources)
// -----------------------------------------------------------------------

/**
 * One internal execution lane within a GPU.
 *
 * Each lane owns a dedicated CUDA stream and its own set of device
 * buffers.  Staging buffers have been moved to :struct:`TaskSlot` so
 * that the lane can be freed immediately when the stream completes.
 */
struct GpuTaskSlot {
  int lane_id;

  // -- Lane state: 0 = IDLE, 1 = BUSY (async work in flight) --
  int state = 0;

  // -- Which TaskSlot this lane is currently processing (-1 if IDLE) --
  int slot_idx = -1;

  // -- Dedicated CUDA stream --
  cudaStream_t stream;

  // -- Device buffers (sized to max dimensions) --
  Complex *d_slc1;
  Complex *d_slc2;
  Complex *d_ifg;
  Complex *d_ifg_collook;
  Complex *d_ifglook;
  float *d_phase;
};

// -----------------------------------------------------------------------
// Per-GPU context
// -----------------------------------------------------------------------

/** Per-GPU context: owns multiple :struct:`GpuTaskSlot` lanes. */
struct GpuContext {
  int gpu_id;
  std::vector<GpuTaskSlot> lanes;
};

// -----------------------------------------------------------------------
// Top-level daemon context
// -----------------------------------------------------------------------

/**
 * Top-level daemon context — owns the buffer pools, task list,
 * GPU contexts, synchronisation primitives, and progress counters.
 */
struct DaemonContext {
  // -- Configuration --
  int raw_slots;        // number of RawBuffers (= max_slots)
  int task_slots_count; // number of TaskSlots (= max_slots)
  int streams_per_gpu;
  int cpu_workers;
  int producer_workers;
  int max_src_nrow; // maximum source nrow across all tasks
  int max_src_ncol; // maximum source ncol across all tasks
  int max_nrow;     // maximum overlap nrow across all tasks
  int max_ncol;
  int max_nrow_sm; // max_nrow / rowlook
  int max_ncol_sm; // max_ncol / collook
  std::size_t max_elements;
  std::size_t max_elements_sm;
  std::size_t max_raw_elements; // max src_w * copy_h across all tasks
  int rowlook;
  int collook;
  bool out_float;
  int ngpus;
  bool verbose;

  // -- Task list --
  std::vector<Task> tasks;
  int n_total;

  // -- RawBuffer pool (Stage 1 → Stage 2) --
  std::vector<RawBuffer> raw_buffers;
  std::queue<int> raw_free_queue;
  std::mutex raw_free_mutex;
  std::condition_variable raw_free_cv;

  // -- CPU work queue (RawBuffer indices ready for cropping) --
  std::queue<int> cpu_work_queue;
  std::mutex cpu_work_mutex;
  std::condition_variable cpu_work_cv;

  // -- TaskSlot pool (Stage 2 → Stage 4) --
  std::vector<TaskSlot> task_slots;
  std::queue<int> task_free_queue;
  std::mutex task_free_mutex;
  std::condition_variable task_free_cv;

  // -- GPU-ready queue (TaskSlot indices ready for H2D) --
  std::queue<int> gpu_ready_queue;
  std::mutex gpu_ready_mutex;
  std::condition_variable gpu_ready_cv;

  // -- Disk-write queue (completed results from GPU lanes) --
  std::queue<DiskWriteItem> disk_write_queue;
  std::mutex disk_write_mutex;
  std::condition_variable disk_write_cv;

  // -- Progress message --
  std::mutex emit_progress_mutex;

  // -- GPU workers --
  std::vector<GpuContext> gpus;

  // -- Global Reference Image Cache (eliminates redundant ref disk reads) --
  //    Protects cached_ref_path comparison and global_buffer load/store.
  //    Never held while reading secondary images or pushing to work queues.
  std::mutex cache_mutex;
  std::string cached_path;
  Complex *global_buffer = nullptr; // pinned, sized max_src_nrow * max_src_ncol

  // -- Progress & termination --
  std::atomic<size_t> next_task_id{0};
  std::atomic<int> completed{0};
  std::atomic<int> failed{0};
  std::atomic<bool> producer_done{false};
  std::atomic<bool> all_cpus_done{false};
  std::atomic<bool> all_gpus_done{false};
  std::atomic<int> cpu_active{0}; // count of running CPU workers
  std::atomic<int> gpu_active{0}; // count of running GPU consumers
  std::chrono::steady_clock::time_point t_start;
};

// =======================================================================
// RawBuffer pool helpers
// =======================================================================

/** Acquire a free RawBuffer.  Blocks until one is available. */
static int acquire_raw_buffer(DaemonContext &ctx) {
  std::unique_lock<std::mutex> lock(ctx.raw_free_mutex);
  ctx.raw_free_cv.wait(lock, [&ctx] { return !ctx.raw_free_queue.empty(); });
  int idx = ctx.raw_free_queue.front();
  ctx.raw_free_queue.pop();
  ctx.raw_buffers[idx].state.store(1); // filling
  return idx;
}

/** Push a filled RawBuffer to the CPU work queue. */
static void push_cpu_work(DaemonContext &ctx, int idx) {
  ctx.raw_buffers[idx].state.store(2); // cpu_ready
  {
    std::lock_guard<std::mutex> lock(ctx.cpu_work_mutex);
    ctx.cpu_work_queue.push(idx);
  }
  ctx.cpu_work_cv.notify_one();
}

/** Return a RawBuffer to its free pool (called by Stage 2). */
static void release_raw_buffer(DaemonContext &ctx, int idx) {
  ctx.raw_buffers[idx].state.store(0); // free
  {
    std::lock_guard<std::mutex> lock(ctx.raw_free_mutex);
    ctx.raw_free_queue.push(idx);
  }
  ctx.raw_free_cv.notify_one();
}

// =======================================================================
// TaskSlot pool helpers
// =======================================================================

/** Acquire a free TaskSlot.  Blocks until one is available. */
static int acquire_task_slot(DaemonContext &ctx) {
  std::unique_lock<std::mutex> lock(ctx.task_free_mutex);
  ctx.task_free_cv.wait(lock, [&ctx] { return !ctx.task_free_queue.empty(); });
  int idx = ctx.task_free_queue.front();
  ctx.task_free_queue.pop();
  ctx.task_slots[idx].state.store(1); // gpu_ready
  return idx;
}

/** Push a populated TaskSlot to the GPU-ready queue. */
static void push_gpu_ready(DaemonContext &ctx, int idx) {
  ctx.task_slots[idx].state.store(1); // gpu_ready
  {
    std::lock_guard<std::mutex> lock(ctx.gpu_ready_mutex);
    ctx.gpu_ready_queue.push(idx);
  }
  ctx.gpu_ready_cv.notify_one();
}

/**
 * Non-blocking try-pop from the GPU-ready queue.
 * Returns the slot index, or -1 if the queue is empty.
 */
static int try_acquire_gpu_ready(DaemonContext &ctx) {
  std::lock_guard<std::mutex> lock(ctx.gpu_ready_mutex);
  if (ctx.gpu_ready_queue.empty())
    return -1;
  int idx = ctx.gpu_ready_queue.front();
  ctx.gpu_ready_queue.pop();
  ctx.task_slots[idx].state.store(2); // processing
  return idx;
}

/** Return a TaskSlot to the free pool (called by Stage 4). */
static void release_task_slot(DaemonContext &ctx, int idx) {
  ctx.task_slots[idx].state.store(0); // free
  {
    std::lock_guard<std::mutex> lock(ctx.task_free_mutex);
    ctx.task_free_queue.push(idx);
  }
  ctx.task_free_cv.notify_one();
}

// =======================================================================
// Disk-write queue helpers
// =======================================================================

/** Push a completed result to the disk-write queue. */
static void push_disk_write(DaemonContext &ctx, DiskWriteItem item) {
  {
    std::lock_guard<std::mutex> lock(ctx.disk_write_mutex);
    ctx.disk_write_queue.push(std::move(item));
  }
  ctx.disk_write_cv.notify_one();
}

// =======================================================================
// Core I/O primitives (Stage 1 & Stage 2)
// =======================================================================

/**
 * Pure sequential disk read — NO cropping, NO allocation.
 *
 * Reads ``src_w * copy_h * type_size`` bytes from *filename* starting
 * at ``header_bytes + src_row0 * src_w * type_size`` directly into
 * *raw_buffer* (which must be pre-allocated pinned host memory).
 *
 * Parameters
 * ----------
 * filename : str
 *     Path to the binary SLC file.
 * raw_buffer : char*
 *     Pre-allocated destination buffer (pinned host memory).
 * src_w : size_t
 *     Full width of the source image in elements.
 * src_row0 : size_t
 *     First row to read (in source image coordinates, relative to the
 *     first data row in the file).
 * copy_h : size_t
 *     Number of contiguous rows to read.
 * type_size : size_t
 *     ``sizeof(element_type)``.
 */
static void read_file_rows(const std::string &filename, char *raw_buffer,
                           std::size_t src_w, std::size_t src_row0,
                           std::size_t copy_h, std::size_t type_size) {
  std::ifstream fin(filename, std::ios::binary);
  if (!fin)
    throw std::runtime_error("Cannot open file in read_file_rows");

  const std::size_t header_bytes = 64 * sizeof(std::int32_t);
  std::size_t offset = header_bytes + src_row0 * src_w * type_size;

  fin.seekg(offset, std::ios::beg);
  fin.read(raw_buffer, src_w * copy_h * type_size);

  if (!fin)
    throw std::runtime_error("Failed to read data from file");
}

/**
 * Pure memory crop — copies the overlapping region from an uncropped
 * source buffer into a destination buffer.  NO allocation.
 *
 * Parameters
 * ----------
 * src : const Complex*
 *     Uncropped source rows (raw disk read result).
 * dst : Complex*
 *     Destination buffer (TaskSlot cropped region).
 * src_w : int
 *     Full width of the source image (in elements).
 * dst_w : int
 *     Width of the destination (overlap) image in elements.
 * dst_h : int
 *     Number of rows to copy.
 * src_col0 : int
 *     Starting column offset within the source row.
 */
static void crop_memory_buffer(const Complex *src, Complex *dst, int src_w,
                               int dst_w, int dst_h, int src_col0) {
  for (int r = 0; r < dst_h; ++r) {
    const Complex *src_row =
        src + static_cast<std::size_t>(r) * src_w + src_col0;
    Complex *dst_row = dst + static_cast<std::size_t>(r) * dst_w;
    std::memcpy(dst_row, src_row, dst_w * sizeof(Complex));
  }
}

// -----------------------------------------------------------------------
// Progress reporting
// -----------------------------------------------------------------------

/** Print a periodic progress line to stdout. */
static void emit_progress(DaemonContext &ctx) {
  std::lock_guard<std::mutex> lock(ctx.emit_progress_mutex);
  auto now = std::chrono::steady_clock::now();
  double elapsed = std::chrono::duration<double>(now - ctx.t_start).count();
  std::cout << "PROGRESS " << ctx.completed.load() << " " << ctx.failed.load()
            << " " << ctx.n_total << " " << static_cast<long long>(elapsed)
            << std::endl;
}

// =======================================================================
// Stage 1: Multi-Threaded I/O Producers
// =======================================================================

/**
 * Stage 1 — Pure I/O producer with reference-image caching.
 *
 * Sequentially iterates over the task list.  For each task:
 * 1. Reads ref + sec headers, computes overlap dimensions.
 * 2. Acquires a free RawBuffer and stores image bounds.
 * 3. Checks the global cache under ``cache_mutex``:
 *    - **Cache hit:** copies the overlap rows from
 *      ``global_buffer`` into ``raw.ref_data`` (zero disk I/O).
 *    - **Cache miss:** reads the *full* reference image from disk into
 *      ``global_buffer``, updates the cache metadata, then copies
 *      the overlap subset into ``raw.ref_data``.
 * 4. Reads the secondary image from disk normally (always unique).
 * 5. Pushes the RawBuffer index to the CPU work queue.
 *
 * The lock is scoped tightly to the cache check and ref load — it is
 * never held during secondary I/O or queue push operations.
 */
static void producer_worker_thread(DaemonContext &ctx, int worker_id) {
  while (true) {
    size_t task_idx = ctx.next_task_id.fetch_add(1);
    if (task_idx >= ctx.n_total) {
      break;
    }
    const Task &task = ctx.tasks[task_idx];
    Task *next_task = nullptr;
    if (task_idx < ctx.n_total - 1) {
      next_task = &ctx.tasks[task_idx + 1];
    }
    // -- Acquire a free RawBuffer --
    int raw_idx = acquire_raw_buffer(ctx);
    RawBuffer &raw = ctx.raw_buffers[raw_idx];
    raw.task = task;

    // -- Read headers --
    std::int32_t header1[NHEADER], header2[NHEADER];
    try {
      read_binary<std::int32_t>(task.ref_slc, NHEADER, header1);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " - cannot read header: " << task.ref_slc << std::endl;
      release_raw_buffer(ctx, raw_idx);
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    try {
      read_binary<std::int32_t>(task.sec_slc, NHEADER, header2);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " - cannot read header: " << task.sec_slc << std::endl;
      release_raw_buffer(ctx, raw_idx);
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    // -- Source bounds --
    int left1 = header1[2], top1 = header1[3];
    int right1 = header1[4], bottom1 = header1[5];
    int left2 = header2[2], top2 = header2[3];
    int right2 = header2[4], bottom2 = header2[5];

    // -- Compute output overlap region (aligned) --
    int left = (left1 > left2 ? left1 : left2);
    left = (left + ctx.collook - 1) / ctx.collook * ctx.collook;
    int right = (right1 < right2 ? right1 : right2);
    right = right / ctx.collook * ctx.collook;
    int top = (top1 > top2 ? top1 : top2);
    top = (top + ctx.rowlook - 1) / ctx.rowlook * ctx.rowlook;
    int bottom = (bottom1 < bottom2 ? bottom1 : bottom2);
    bottom = bottom / ctx.rowlook * ctx.rowlook;

    int nrow = bottom - top;
    int ncol = right - left;

    if (nrow <= 0 || ncol <= 0) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " - empty overlap region" << std::endl;
      release_raw_buffer(ctx, raw_idx);
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    int nrow_sm = nrow / ctx.rowlook;
    int ncol_sm = ncol / ctx.collook;

    // -- Store crop metadata in RawBuffer --
    raw.left_ref = left1;
    raw.top_ref = top1;
    raw.right_ref = right1;
    raw.bottom_ref = bottom1;
    raw.src_w_ref = right1 - left1;

    raw.left_sec = left2;
    raw.top_sec = top2;
    raw.right_sec = right2;
    raw.bottom_sec = bottom2;
    raw.src_w_sec = right2 - left2;

    raw.left_dst = left;
    raw.top_dst = top;
    raw.right_dst = right;
    raw.bottom_dst = bottom;

    raw.nrow = nrow;
    raw.ncol = ncol;
    raw.nrow_sm = nrow_sm;
    raw.ncol_sm = ncol_sm;

    // -- Fill output header --
    raw.ifg_header[0] = header1[0] / ctx.rowlook;
    raw.ifg_header[1] = header1[1] / ctx.collook;
    raw.ifg_header[2] = left / ctx.collook;
    raw.ifg_header[3] = top / ctx.rowlook;
    raw.ifg_header[4] = right / ctx.collook;
    raw.ifg_header[5] = bottom / ctx.rowlook;

    // -- Read SLC blocks into RawBuffer (first try cache) --
    bool ref_cache_hit = false, sec_cache_hit = false;
    try {
      // --- Reference image: thread-safe cache interception ---
      {
        std::lock_guard<std::mutex> lock(ctx.cache_mutex);
        ref_cache_hit = (ctx.cached_path == task.ref_slc);
        sec_cache_hit = (ctx.cached_path == task.sec_slc);

        if (!ref_cache_hit && !sec_cache_hit) {
          // Case 1: neither reference or secondary image hits the cache
          if (next_task != nullptr && (task.sec_slc == next_task->sec_slc ||
                                       task.sec_slc == next_task->ref_slc)) {
            // Case 1.1: sec_slc is the current seed
            int src_w = right2 - left2;
            int src_h = bottom2 - top2;
            read_file_rows(task.sec_slc,
                           reinterpret_cast<char *>(ctx.global_buffer),
                           static_cast<std::size_t>(src_w),
                           0, // start from row 0 of the source image
                           static_cast<std::size_t>(src_h), sizeof(Complex));
            ctx.cached_path = task.sec_slc;
            // now set sec_cache_hit to true because we just loaded the
            // reference image to cache
            sec_cache_hit = true;
            if (ctx.verbose) {
              std::cerr << "[I/O Cache] Image loaded: " << task.sec_slc << " ("
                        << (right1 - left1) << " x " << (bottom1 - top1) << ")"
                        << std::endl;
            }
          } else {
            // Case 1.2: ref_slc is the current seed
            // Case 1.3: next task is also a cold start
            // Case 1.4: we are processing the last task
            int src_w = right1 - left1;
            int src_h = bottom1 - top1;
            read_file_rows(task.ref_slc,
                           reinterpret_cast<char *>(ctx.global_buffer),
                           static_cast<std::size_t>(src_w),
                           0, // start from row 0 of the source image
                           static_cast<std::size_t>(src_h), sizeof(Complex));
            ctx.cached_path = task.ref_slc;
            // now set ref_cache_hit to true because we just loaded the
            // reference image to cache
            ref_cache_hit = true;
            if (ctx.verbose) {
              std::cerr << "[I/O Cache] Image loaded: " << task.ref_slc << " ("
                        << (right1 - left1) << " x " << (bottom1 - top1) << ")"
                        << std::endl;
            }
          }
        } else if (ctx.verbose) {
          std::cerr << "[I/O Cache] Cache Hit for task " << task.out_ifg
                    << ", skipping disk read." << std::endl;
        }

        if (ref_cache_hit) {
          // Case 1.2-1.4: neither reference nor secondary image hits the cache,
          // we just reset the globl cached image, and we just cached the
          // reference image, OR Case 2: reference image hits the cache.
          int src_w = right1 - left1;
          std::size_t row_offset = static_cast<std::size_t>(top - top1) * src_w;
          std::size_t copy_elems =
              static_cast<std::size_t>(src_w) * (bottom - top);
          std::memcpy(raw.ref_data, ctx.global_buffer + row_offset,
                      copy_elems * sizeof(Complex));
        }

        if (sec_cache_hit) {
          // Case 1.1: neither reference nor secondary image hits the cache, we
          // just reset the globl cached image, and we just cached the secondary
          // image, OR Case 3: secondary image hits the cache.
          int src_w = right2 - left2;
          std::size_t row_offset = static_cast<std::size_t>(top - top2) * src_w;
          std::size_t copy_elems =
              static_cast<std::size_t>(src_w) * (bottom - top);
          std::memcpy(raw.sec_data, ctx.global_buffer + row_offset,
                      copy_elems * sizeof(Complex));
        }
      }
      // --- Lock released — secondary I/O proceeds independently ---

      if (!ref_cache_hit) {
        // Case 3, the secondary image hits the cache, so we need to read the
        // reference image from disk
        read_file_rows(task.ref_slc, reinterpret_cast<char *>(raw.ref_data),
                       static_cast<std::size_t>(right1 - left1),
                       static_cast<std::size_t>(top - top1),
                       static_cast<std::size_t>(bottom - top), sizeof(Complex));
      }

      if (!sec_cache_hit) {
        // Case 2, the reference image hits the cache, so we need to read the
        // secondary image from disk
        read_file_rows(task.sec_slc, reinterpret_cast<char *>(raw.sec_data),
                       static_cast<std::size_t>(right2 - left2),
                       static_cast<std::size_t>(top - top2),
                       static_cast<std::size_t>(bottom - top), sizeof(Complex));
      }
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " - I/O error: " << e.what() << std::endl;
      release_raw_buffer(ctx, raw_idx);
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    // -- Push to CPU work queue --
    push_cpu_work(ctx, raw_idx);

    if (ctx.verbose) {
      std::cerr << "[crossmul_daemon|producer] raw[" << raw_idx
                << "] -> cpu_work: " << task.out_ifg
                << " (raw ref: " << (right1 - left1) << "x" << (bottom1 - top1)
                << " sec: " << (right2 - left2) << "x" << (bottom2 - top2)
                << " -> crop: " << nrow << "x" << ncol << ")" << std::endl;
    }
  }

  {
    std::unique_lock<std::mutex> lock(ctx.cpu_work_mutex);
    ctx.producer_done.store(true);
  }
  ctx.cpu_work_cv.notify_all();
  std::cerr << "[crossmul_daemon] Producer (Stage 1) finished - all "
            << ctx.n_total << " tasks read." << std::endl;
}

// =======================================================================
// Stage 2: CPU Worker Thread Pool
// =======================================================================

/**
 * Stage 2 — CPU crop worker.
 *
 * Continuously pops RawBuffer indices from the CPU work queue, acquires
 * a free TaskSlot, calls ``crop_memory_buffer`` for both ref and sec
 * images, releases the RawBuffer, and pushes the populated TaskSlot to
 * the GPU-ready queue.
 *
 * Exits when the producer is done AND the CPU work queue is empty.
 */
static void cpu_worker_thread(DaemonContext &ctx, int worker_id) {
  if (ctx.verbose) {
    std::cerr << "[crossmul_daemon] CPU worker " << worker_id << " started."
              << std::endl;
  }

  while (true) {
    // -- Pop next RawBuffer from CPU work queue --
    int raw_idx = -1;
    {
      std::unique_lock<std::mutex> lock(ctx.cpu_work_mutex);
      ctx.cpu_work_cv.wait(lock, [&ctx] {
        return !ctx.cpu_work_queue.empty() || ctx.producer_done.load();
      });

      if (!ctx.cpu_work_queue.empty()) {
        raw_idx = ctx.cpu_work_queue.front();
        ctx.cpu_work_queue.pop();
      } else if (ctx.producer_done.load()) {
        break; // no more work will arrive
      }
    }

    if (raw_idx < 0)
      continue;

    RawBuffer &raw = ctx.raw_buffers[raw_idx];

    // -- Acquire a free TaskSlot --
    int slot_idx = acquire_task_slot(ctx);
    TaskSlot &slot = ctx.task_slots[slot_idx];

    // -- Copy task identity and metadata --
    slot.task = raw.task;
    slot.nrow = raw.nrow;
    slot.ncol = raw.ncol;
    slot.nrow_sm = raw.nrow_sm;
    slot.ncol_sm = raw.ncol_sm;
    std::memcpy(slot.ifg_header, raw.ifg_header,
                NHEADER * sizeof(std::int32_t));

    // -- Crop ref image: RawBuffer.ref_data → TaskSlot.ref_data --
    crop_memory_buffer(raw.ref_data, slot.ref_data, raw.src_w_ref, raw.ncol,
                       raw.nrow, raw.left_dst - raw.left_ref);

    // -- Crop sec image: RawBuffer.sec_data → TaskSlot.sec_data --
    crop_memory_buffer(raw.sec_data, slot.sec_data, raw.src_w_sec, raw.ncol,
                       raw.nrow, raw.left_dst - raw.left_sec);

    // -- Release RawBuffer back to free pool --
    release_raw_buffer(ctx, raw_idx);

    // -- Push populated TaskSlot to GPU-ready queue --
    push_gpu_ready(ctx, slot_idx);

    if (ctx.verbose) {
      std::cerr << "[crossmul_daemon|cpu:" << worker_id << "] raw[" << raw_idx
                << "] cropped -> task[" << slot_idx
                << "]: " << slot.task.out_ifg << " (" << slot.nrow << "x"
                << slot.ncol << ")" << std::endl;
    }
  }

  // -- Last CPU worker to exit signals GPU consumers --
  int remaining = --ctx.cpu_active;
  if (remaining == 0) {
    {
      std::lock_guard<std::mutex> lock(ctx.gpu_ready_mutex);
      ctx.all_cpus_done.store(true);
    }
    ctx.gpu_ready_cv.notify_all();
  }

  if (ctx.verbose) {
    std::cerr << "[crossmul_daemon] CPU worker " << worker_id << " exiting."
              << std::endl;
  }
}

// =======================================================================
// Stage 3: GPU Consumer Thread
// =======================================================================

/**
 * Launch the full H2D → kernels → D2H pipeline onto *lane* and
 * return immediately (no synchronisation).
 *
 * D2H now targets ``slot.result_cpx`` / ``slot.result_float`` so that
 * the lane does not own the staging buffer — the TaskSlot does.
 */
static void launch_async_pipeline(DaemonContext &ctx, GpuTaskSlot &lane,
                                  TaskSlot &slot) {
  int blockSize = 256;
  std::size_t elem_bytes = sizeof(Complex) * slot.nrow * slot.ncol;

  // -- Async H2D from TaskSlot --
  CHECK_CUDA(cudaMemcpyAsync(lane.d_slc1, slot.ref_data, elem_bytes,
                             cudaMemcpyHostToDevice, lane.stream));
  CHECK_CUDA(cudaMemcpyAsync(lane.d_slc2, slot.sec_data, elem_bytes,
                             cudaMemcpyHostToDevice, lane.stream));

  // -- conj_mul kernel --
  int numBlocks = (slot.nrow * slot.ncol + blockSize - 1) / blockSize;
  conj_mul<<<numBlocks, blockSize, 0, lane.stream>>>(
      lane.d_slc1, lane.d_slc2, lane.d_ifg, slot.nrow * slot.ncol);

  // Track which device pointer holds the current pipeline stage output
  Complex *current = lane.d_ifg;

  // -- Column multi-look --
  if (ctx.collook > 1) {
    numBlocks = (slot.nrow * slot.ncol_sm + blockSize - 1) / blockSize;
    cpx_col_look<<<numBlocks, blockSize, 0, lane.stream>>>(
        lane.d_ifg, lane.d_ifg_collook, ctx.collook, slot.ncol,
        slot.nrow * slot.ncol_sm);
    current = lane.d_ifg_collook;
  }

  // -- Row multi-look --
  if (ctx.rowlook > 1) {
    numBlocks = (slot.nrow_sm * slot.ncol_sm + blockSize - 1) / blockSize;
    cpx_row_look<<<numBlocks, blockSize, 0, lane.stream>>>(
        current, lane.d_ifglook, ctx.rowlook, slot.ncol_sm,
        slot.nrow_sm * slot.ncol_sm);
    current = lane.d_ifglook;
  }

  // -- Phase extraction (float path) --
  if (ctx.out_float) {
    numBlocks = (slot.nrow_sm * slot.ncol_sm + blockSize - 1) / blockSize;
    point_angle<<<numBlocks, blockSize, 0, lane.stream>>>(
        current, lane.d_phase, slot.nrow_sm * slot.ncol_sm);
  }

  // -- Async D2H into TaskSlot's staging buffer (not the lane's) --
  if (ctx.out_float) {
    CHECK_CUDA(cudaMemcpyAsync(slot.result_float, lane.d_phase,
                               sizeof(float) * slot.nrow_sm * slot.ncol_sm,
                               cudaMemcpyDeviceToHost, lane.stream));
  } else {
    CHECK_CUDA(cudaMemcpyAsync(slot.result_cpx, current,
                               sizeof(Complex) * slot.nrow_sm * slot.ncol_sm,
                               cudaMemcpyDeviceToHost, lane.stream));
  }
}

/**
 * Stage 3 — GPU consumer.
 *
 * Manages an array of internal :struct:`GpuTaskSlot` lanes.  Each lane
 * can execute one burst pair at a time.  Uses non-blocking
 * ``cudaStreamQuery`` polling to overlap multiple burst pairs on the
 * same GPU.
 *
 * When a BUSY lane completes, the result is wrapped into a
 * :struct:`DiskWriteItem` and pushed to the disk-write queue —
 * **no synchronous disk I/O is performed here**.  The lane is
 * immediately reset to IDLE.
 */
static void gpu_consumer_thread(DaemonContext &ctx, int gpu_idx) {
  GpuContext &g = ctx.gpus[gpu_idx];
  set_gpu(g.gpu_id);

  const int n_lanes = static_cast<int>(g.lanes.size());

  while (true) {
    bool made_progress = false;

    // -- Phase 1: poll BUSY lanes for completion --
    for (int li = 0; li < n_lanes; ++li) {
      GpuTaskSlot &lane = g.lanes[li];
      if (lane.state != 1)
        continue; // not BUSY

      cudaError_t err = cudaStreamQuery(lane.stream);
      if (err == cudaSuccess) {
        // -- Lane finished: wrap result and push to disk-write queue --
        TaskSlot &slot = ctx.task_slots[lane.slot_idx];

        DiskWriteItem item;
        item.slot_idx = lane.slot_idx;
        item.n_elements = slot.nrow_sm * slot.ncol_sm;
        std::memcpy(item.header, slot.ifg_header,
                    NHEADER * sizeof(std::int32_t));
        item.out_ifg = slot.task.out_ifg;
        item.is_float = ctx.out_float;
        if (ctx.out_float) {
          item.data = static_cast<void *>(slot.result_float);
        } else {
          item.data = static_cast<void *>(slot.result_cpx);
        }

        push_disk_write(ctx, std::move(item));

        // Reset lane to IDLE immediately (do NOT wait for disk I/O)
        lane.state = 0;
        lane.slot_idx = -1;
        made_progress = true;
      } else if (err != cudaErrorNotReady) {
        // Real CUDA error on the stream
        TaskSlot &slot = ctx.task_slots[lane.slot_idx];
        std::cerr << "[crossmul_daemon] FAIL " << slot.task.out_ifg
                  << " - CUDA stream error: " << cudaGetErrorString(err)
                  << std::endl;
        ctx.failed.fetch_add(1);
        ctx.completed.fetch_add(1);
        release_task_slot(ctx, lane.slot_idx);
        lane.state = 0;
        lane.slot_idx = -1;
        made_progress = true;
      }
    }

    // -- Phase 2: fill IDLE lanes with new work --
    for (int li = 0; li < n_lanes; ++li) {
      GpuTaskSlot &lane = g.lanes[li];
      if (lane.state != 0)
        continue; // not IDLE

      int slot_idx = try_acquire_gpu_ready(ctx);
      if (slot_idx < 0)
        break; // nothing ready yet

      TaskSlot &slot = ctx.task_slots[slot_idx];
      lane.slot_idx = slot_idx;

      try {
        launch_async_pipeline(ctx, lane, slot);
        lane.state = 1; // BUSY
        made_progress = true;
      } catch (const std::exception &e) {
        std::cerr << "[crossmul_daemon] FAIL " << slot.task.out_ifg
                  << " - launch error: " << e.what() << std::endl;
        ctx.failed.fetch_add(1);
        ctx.completed.fetch_add(1);
        release_task_slot(ctx, slot_idx);
        lane.slot_idx = -1;
        lane.state = 0;
        made_progress = true;
      }
    }

    // -- Termination check --
    bool cpu_done = ctx.all_cpus_done.load();
    if (cpu_done) {
      // Are all lanes idle AND the GPU-ready queue empty?
      // bool all_idle = true;
      // for (int li = 0; li < n_lanes; ++li) {
      //  if (g.lanes[li].state != 0) {
      //    all_idle = false;
      //    break;
      //  }
      //}

      // bool queue_empty = false;
      //{
      //   std::lock_guard<std::mutex> lk(ctx.gpu_ready_mutex);
      //   queue_empty = ctx.gpu_ready_queue.empty();
      // }

      if (ctx.completed.load() >= ctx.n_total)
        break;
    }

    // -- Avoid CPU starvation when nothing progressed --
    if (!made_progress) {
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    } else {
      std::this_thread::yield();
    }
  }

  // -- Last GPU consumer to exit signals the disk writer --
  int remaining = --ctx.gpu_active;
  if (remaining == 0) {
    {
      std::lock_guard<std::mutex> lk(ctx.disk_write_mutex);
      ctx.all_gpus_done.store(true);
    }
    ctx.disk_write_cv.notify_all();
  }

  emit_progress(ctx);

  std::cerr << "[crossmul_daemon] GPU " << g.gpu_id << " consumer (Stage 3)"
            << " exiting." << std::endl;
}

// =======================================================================
// Stage 4: Dedicated Async Disk Writer
// =======================================================================

/**
 * Stage 4 — Single-threaded async disk writer.
 *
 * Sequentially pops :struct:`DiskWriteItem` entries from the disk-write
 * queue and calls ``save_binary`` to stream interferogram outputs to
 * disk.  Only after the write completes does it release the underlying
 * TaskSlot back to the free pool.
 *
 * Exits when all GPU consumers have terminated AND the disk-write queue
 * is empty.
 */
static void disk_writer_thread(DaemonContext &ctx) {
  if (ctx.verbose) {
    std::cerr << "[crossmul_daemon] Disk writer (Stage 4) started."
              << std::endl;
  }

  while (true) {
    DiskWriteItem item;
    bool has_item = false;
    {
      std::unique_lock<std::mutex> lock(ctx.disk_write_mutex);
      ctx.disk_write_cv.wait(lock, [&ctx] {
        return !ctx.disk_write_queue.empty() || ctx.all_gpus_done.load();
      });

      if (!ctx.disk_write_queue.empty()) {
        item = std::move(ctx.disk_write_queue.front());
        ctx.disk_write_queue.pop();
        has_item = true;
      } else if (ctx.all_gpus_done.load()) {
        // All GPU consumers done and queue is empty — we're finished.
        break;
      }
    }

    if (!has_item)
      continue;

    // -- Write to disk --
    try {
      if (item.is_float) {
        save_binary<float>(static_cast<float *>(item.data), item.n_elements,
                           item.header, NHEADER, item.out_ifg);
      } else {
        save_binary<Complex>(static_cast<Complex *>(item.data), item.n_elements,
                             item.header, NHEADER, item.out_ifg);
      }
      std::cout << "OK " << item.out_ifg << std::endl;
      ctx.completed.fetch_add(1);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << item.out_ifg
                << " - write error: " << e.what() << std::endl;
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
    }

    // -- Release TaskSlot back to free pool --
    release_task_slot(ctx, item.slot_idx);

    if (ctx.verbose) {
      std::cerr << "[crossmul_daemon|disk] wrote task[" << item.slot_idx
                << "]: " << item.out_ifg << std::endl;
    }
  }

  emit_progress(ctx);

  std::cerr << "[crossmul_daemon] Disk writer (Stage 4) exiting." << std::endl;
}

// =======================================================================
// Initialisation helpers
// =======================================================================

/** Parse simple ``--key value`` arguments. */
static std::string get_arg(int argc, char *argv[], const std::string &key,
                           const std::string &default_val = "") {
  for (int i = 1; i < argc - 1; ++i) {
    if (std::string(argv[i]) == key)
      return std::string(argv[i + 1]);
  }
  return default_val;
}

static bool has_flag(int argc, char *argv[], const std::string &flag) {
  for (int i = 1; i < argc; ++i) {
    if (std::string(argv[i]) == flag)
      return true;
  }
  return false;
}

/**
 * Scan all SLC headers across every task to determine:
 * - Maximum source frame dimensions (for RawBuffer / OOM prediction).
 * - Maximum raw read element count (for RawBuffer sizing).
 *
 * Source dimensions are used as a conservative upper bound for the
 * cropped overlap region (``max_nrow`` / ``max_ncol``) since the
 * actual per-burst-pair overlap is always a subset of the source.
 */
static void scan_max_dimensions(DaemonContext &ctx) {
  int max_src_nrow = 0, max_src_ncol = 0;
  int max_nrow = 0, max_ncol = 0;
  std::size_t max_raw_elements = 0;

  for (const auto &task : ctx.tasks) {
    std::int32_t header1[NHEADER], header2[NHEADER];

    read_binary<std::int32_t>(task.ref_slc, NHEADER, header1);
    read_binary<std::int32_t>(task.sec_slc, NHEADER, header2);

    int left1 = header1[2], top1 = header1[3];
    int right1 = header1[4], bottom1 = header1[5];
    int left2 = header2[2], top2 = header2[3];
    int right2 = header2[4], bottom2 = header2[5];

    // Track maximum source dimensions (for OOM predictions)
    max_src_nrow = std::max(bottom1 - top1, max_src_nrow);
    max_src_ncol = std::max(right1 - left1, max_src_ncol);
    max_src_nrow = std::max(bottom2 - top2, max_src_nrow);
    max_src_ncol = std::max(right2 - left2, max_src_ncol);

    // Compute output overlap region (aligned)
    // int left = (left1 < left2 ? left1 : left2);
    // left = (left + ctx.collook - 1) / ctx.collook * ctx.collook;
    // int right = (right1 > right2 ? right1 : right2);
    // right = right / ctx.collook * ctx.collook;
    // int top = (top1 < top2 ? top1 : top2);
    // top = (top + ctx.rowlook - 1) / ctx.rowlook * ctx.rowlook;
    // int bottom = (bottom1 > bottom2 ? bottom1 : bottom2);
    // bottom = bottom / ctx.rowlook * ctx.rowlook;

    // Track maximum overlap dimensions
    max_nrow = max_src_nrow;
    max_ncol = max_src_ncol;

    // --- Compute raw read size for ref image ---
    max_raw_elements = static_cast<std::size_t>(max_nrow) * max_ncol;
  }

  ctx.max_src_nrow = max_src_nrow;
  ctx.max_src_ncol = max_src_ncol;
  ctx.max_nrow = max_nrow;
  ctx.max_ncol = max_ncol;
  ctx.max_nrow_sm = max_nrow / ctx.rowlook;
  ctx.max_ncol_sm = max_ncol / ctx.collook;
  ctx.max_elements = static_cast<std::size_t>(max_nrow) * max_ncol;
  ctx.max_elements_sm =
      static_cast<std::size_t>(ctx.max_nrow_sm) * ctx.max_ncol_sm;
  ctx.max_raw_elements = max_raw_elements;

  std::cerr << "[crossmul_daemon] Max source dimensions: " << max_src_nrow
            << " x " << max_src_ncol << std::endl;
  std::cerr << "[crossmul_daemon] Max overlap dimensions: " << max_nrow << " x "
            << max_ncol << "  (looked: " << ctx.max_nrow_sm << " x "
            << ctx.max_ncol_sm << ")" << std::endl;
  std::cerr << "[crossmul_daemon] TaskSlot size per slot: "
            << (ctx.max_elements * sizeof(Complex) * 2 / (1024 * 1024))
            << " MB (ref+sec)" << std::endl;
  std::cerr << "[crossmul_daemon] Max raw read: " << max_raw_elements
            << " elements ("
            << (max_raw_elements * sizeof(Complex) / (1024 * 1024))
            << " MB per image)" << std::endl;
}

// -----------------------------------------------------------------------
// Memory allocation (all pinned host, one-time at startup)
// -----------------------------------------------------------------------

/** Allocate the RawBuffer pool (Stage 1 uncropped I/O buffers). */
static void allocate_raw_buffers(DaemonContext &ctx) {
  ctx.raw_buffers = std::vector<RawBuffer>(ctx.raw_slots);
  for (int i = 0; i < ctx.raw_slots; ++i) {
    RawBuffer &rb = ctx.raw_buffers[i];
    rb.id = i;
    CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&rb.ref_data),
                              sizeof(Complex) * ctx.max_raw_elements));
    CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&rb.sec_data),
                              sizeof(Complex) * ctx.max_raw_elements));
    ctx.raw_free_queue.push(i);
  }
  std::cerr << "[crossmul_daemon] Allocated " << ctx.raw_slots
            << " RawBuffers ("
            << (ctx.max_raw_elements * sizeof(Complex) * 2 / (1024 * 1024))
            << " MB each)." << std::endl;
}

/**
 * Allocate the global pinned-host-memory cache for the reference image.
 *
 * Sized to hold the largest possible reference frame
 * (``max_src_nrow * max_src_ncol`` complex elements).  Only one frame
 * is cached at any time; consecutive tasks with the same ref_slc path
 * copy from this buffer instead of re-reading from disk.
 */
static void allocate_global_cache(DaemonContext &ctx) {
  std::size_t n_elements =
      static_cast<std::size_t>(ctx.max_src_nrow) * ctx.max_src_ncol;
  CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&ctx.global_buffer),
                            sizeof(Complex) * n_elements));
  ctx.cached_path.clear();
  std::cerr << "[crossmul_daemon] Allocated global reference cache: "
            << (sizeof(Complex) * n_elements / (1024 * 1024)) << " MB."
            << std::endl;
}

/** Allocate the TaskSlot pool (Stage 2+ cropped buffers + result staging). */
static void allocate_task_slots(DaemonContext &ctx) {
  ctx.task_slots = std::vector<TaskSlot>(ctx.task_slots_count);
  for (int i = 0; i < ctx.task_slots_count; ++i) {
    TaskSlot &ts = ctx.task_slots[i];
    ts.id = i;
    CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&ts.ref_data),
                              sizeof(Complex) * ctx.max_elements));
    CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&ts.sec_data),
                              sizeof(Complex) * ctx.max_elements));
    if (ctx.out_float) {
      CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&ts.result_float),
                                sizeof(float) * ctx.max_elements_sm));
      ts.result_cpx = nullptr;
    } else {
      CHECK_CUDA(cudaMallocHost(reinterpret_cast<void **>(&ts.result_cpx),
                                sizeof(Complex) * ctx.max_elements_sm));
      ts.result_float = nullptr;
    }
    ctx.task_free_queue.push(i);
  }
  std::cerr << "[crossmul_daemon] Allocated " << ctx.task_slots_count
            << " TaskSlots ("
            << (ctx.max_elements * sizeof(Complex) * 2 / (1024 * 1024))
            << " MB data + "
            << (ctx.max_elements_sm *
                (ctx.out_float ? sizeof(float) : sizeof(Complex)) /
                (1024 * 1024))
            << " MB staging each)." << std::endl;
}

/**
 * Allocate per-GPU execution lanes.
 *
 * Each lane gets its own CUDA stream and full set of device buffers.
 * Staging buffers are NO LONGER allocated per-lane — they live in
 * :struct:`TaskSlot` so that D2H targets survive lane reuse.
 */
static void allocate_gpu_contexts(DaemonContext &ctx) {
  ctx.gpus.resize(ctx.ngpus);
  for (int i = 0; i < ctx.ngpus; ++i) {
    GpuContext &g = ctx.gpus[i];
    g.gpu_id = i;

    int orig_dev;
    cudaGetDevice(&orig_dev);
    cudaSetDevice(i);

    g.lanes.resize(ctx.streams_per_gpu);

    std::size_t dev_mb_per_lane =
        (sizeof(Complex) * ctx.max_elements * 3 +
         sizeof(Complex) * ctx.max_nrow * ctx.max_ncol_sm +
         sizeof(Complex) * ctx.max_nrow_sm * ctx.max_ncol_sm) /
        (1024 * 1024);

    for (int li = 0; li < ctx.streams_per_gpu; ++li) {
      GpuTaskSlot &lane = g.lanes[li];
      lane.lane_id = li;
      lane.state = 0;
      lane.slot_idx = -1;

      cudaStreamCreate(&lane.stream);

      // Device buffers
      CHECK_CUDA(cudaMalloc(reinterpret_cast<void **>(&lane.d_slc1),
                            sizeof(Complex) * ctx.max_elements));
      CHECK_CUDA(cudaMalloc(reinterpret_cast<void **>(&lane.d_slc2),
                            sizeof(Complex) * ctx.max_elements));
      CHECK_CUDA(cudaMalloc(reinterpret_cast<void **>(&lane.d_ifg),
                            sizeof(Complex) * ctx.max_elements));
      if (ctx.collook > 1) {
        CHECK_CUDA(
            cudaMalloc(reinterpret_cast<void **>(&lane.d_ifg_collook),
                       sizeof(Complex) * ctx.max_nrow * ctx.max_ncol_sm));
      } else {
        lane.d_ifg_collook = nullptr;
      }
      if (ctx.rowlook > 1) {
        CHECK_CUDA(
            cudaMalloc(reinterpret_cast<void **>(&lane.d_ifglook),
                       sizeof(Complex) * ctx.max_nrow_sm * ctx.max_ncol_sm));
      } else {
        lane.d_ifglook = nullptr;
      }

      // Staging buffers are now in TaskSlot — lane only needs device-side
      // phase buffer for the float output path
      if (ctx.out_float) {
        CHECK_CUDA(cudaMalloc(reinterpret_cast<void **>(&lane.d_phase),
                              sizeof(float) * ctx.max_elements_sm));
      } else {
        lane.d_phase = nullptr;
      }
    }

    cudaSetDevice(orig_dev);

    std::cerr << "[crossmul_daemon] GPU " << i << ": " << ctx.streams_per_gpu
              << " lane(s), ~" << dev_mb_per_lane << " MB device memory/lane ("
              << (dev_mb_per_lane * ctx.streams_per_gpu) << " MB total/GPU)."
              << std::endl;
  }
}

// -----------------------------------------------------------------------
// Cleanup
// -----------------------------------------------------------------------

/** Free the RawBuffer pool. */
static void free_raw_buffers(DaemonContext &ctx) {
  for (auto &rb : ctx.raw_buffers) {
    if (rb.ref_data)
      cudaFreeHost(rb.ref_data);
    if (rb.sec_data)
      cudaFreeHost(rb.sec_data);
  }
  ctx.raw_buffers.clear();
}

/** Free the global reference image cache. */
static void free_global_cache(DaemonContext &ctx) {
  if (ctx.global_buffer) {
    cudaFreeHost(ctx.global_buffer);
    ctx.global_buffer = nullptr;
  }
  ctx.cached_path.clear();
}

/** Free the TaskSlot pool. */
static void free_task_slots(DaemonContext &ctx) {
  for (auto &ts : ctx.task_slots) {
    if (ts.ref_data)
      cudaFreeHost(ts.ref_data);
    if (ts.sec_data)
      cudaFreeHost(ts.sec_data);
    if (ts.result_cpx)
      cudaFreeHost(ts.result_cpx);
    if (ts.result_float)
      cudaFreeHost(ts.result_float);
  }
  ctx.task_slots.clear();
}

/** Free per-GPU resources. */
static void free_gpu_contexts(DaemonContext &ctx) {
  for (auto &g : ctx.gpus) {
    cudaSetDevice(g.gpu_id);
    for (auto &lane : g.lanes) {
      if (lane.d_slc1)
        cudaFree(lane.d_slc1);
      if (lane.d_slc2)
        cudaFree(lane.d_slc2);
      if (lane.d_ifg)
        cudaFree(lane.d_ifg);
      if (lane.d_ifg_collook)
        cudaFree(lane.d_ifg_collook);
      if (lane.d_ifglook)
        cudaFree(lane.d_ifglook);
      if (lane.d_phase)
        cudaFree(lane.d_phase);
      cudaStreamDestroy(lane.stream);
    }
    g.lanes.clear();
  }
  ctx.gpus.clear();
}

/** Read all task lines from a file.  One ``ref_slc sec_slc out_ifg`` per line.
 */
static std::vector<Task> read_tasks_from_file(const std::string &path) {
  std::vector<Task> tasks;
  std::ifstream fin(path);
  if (!fin.is_open()) {
    std::cerr << "[crossmul_daemon] ERROR: cannot open tasks file: " << path
              << std::endl;
    return tasks;
  }
  std::string line;
  while (std::getline(fin, line)) {
    if (line.empty())
      continue;
    std::istringstream iss(line);
    Task t;
    if (iss >> t.ref_slc >> t.sec_slc >> t.out_ifg) {
      tasks.push_back(t);
    } else {
      std::cerr << "[crossmul_daemon] WARNING: skipping malformed "
                << "task line: " << line << std::endl;
    }
  }
  fin.close();
  return tasks;
}

// =======================================================================
// Hardware-aware auto-tuning
// =======================================================================

/**
 * Query available system RAM in bytes.
 *
 * On Linux reads ``/proc/meminfo`` for ``MemAvailable``; on Windows calls
 * ``GlobalMemoryStatusEx``.  Falls back to 8 GiB when the query fails.
 */
static std::size_t query_system_ram() {
#ifdef _WIN32
  MEMORYSTATUSEX mem_info;
  mem_info.dwLength = sizeof(MEMORYSTATUSEX);
  if (GlobalMemoryStatusEx(&mem_info))
    return static_cast<std::size_t>(mem_info.ullAvailPhys);
#else
  std::ifstream meminfo("/proc/meminfo");
  if (meminfo.is_open()) {
    std::string line;
    while (std::getline(meminfo, line)) {
      if (line.find("MemAvailable:") == 0) {
        std::istringstream iss(line);
        std::string key;
        std::size_t kb;
        iss >> key >> kb;
        return kb * 1024; // kB -> bytes
      }
    }
  }
#endif
  // Fallback: assume 8 GiB available
  return static_cast<std::size_t>(8ULL) * 1024 * 1024 * 1024;
}

/**
 * Query minimum free VRAM across all detected GPUs.
 *
 * Returns the smallest ``cudaMemGetInfo`` free-bytes value across
 * devices 0 .. *ngpus*-1.
 */
static std::size_t query_gpu_free_vram(int ngpus) {
  int orig_dev;
  cudaGetDevice(&orig_dev);
  std::size_t min_free = 0;
  for (int d = 0; d < ngpus; ++d) {
    cudaSetDevice(d);
    std::size_t free_bytes, total_bytes;
    if (cudaMemGetInfo(&free_bytes, &total_bytes) == cudaSuccess) {
      if (d == 0 || free_bytes < min_free)
        min_free = free_bytes;
    }
  }
  cudaSetDevice(orig_dev);
  return min_free;
}

/**
 * Auto-tune pipeline parameters from hardware telemetry and burst-pair
 * dimensions discovered during the scan pass.
 *
 * Derives ``streams_per_gpu``, ``max_slots``, ``producer_workers``,
 * ``cpu_workers``, and ``ngpus`` following the cascading formulae
 * described in the tuning documentation.  Applies a progressive
 * scale-down loop when the predicted host memory footprint exceeds
 * 80 % of available system RAM.
 */
static void auto_tune_parameters(DaemonContext &ctx) {
  // ---- Step 0: Hardware telemetry ----

  unsigned int T_hw = std::thread::hardware_concurrency();
  if (T_hw == 0)
    T_hw = 1;

  int ngpus;
  cudaError_t err = cudaGetDeviceCount(&ngpus);
  if (err != cudaSuccess || ngpus <= 0)
    ngpus = 1;
  ctx.ngpus = ngpus;

  std::size_t M_gpu_free = query_gpu_free_vram(ngpus);
  std::size_t M_sys = query_system_ram();

  // ---- Step 0: Task dimension constants ----

  // S_raw  = max_src_nrow * max_src_ncol * sizeof(Complex) * 2 (Ref + Sec)
  std::size_t S_raw = static_cast<std::size_t>(ctx.max_src_nrow) *
                      ctx.max_src_ncol * sizeof(Complex) * 2;

  // S_crop = max_nrow * max_ncol * sizeof(Complex) * 2 (Ref + Sec)
  std::size_t S_crop = static_cast<std::size_t>(ctx.max_nrow) * ctx.max_ncol *
                       sizeof(Complex) * 2;

  std::cerr << "[crossmul_daemon|auto-tune] Hardware telemetry:" << std::endl;
  std::cerr << "  CPU SMT threads                = " << T_hw << std::endl;
  std::cerr << "  GPUs detected                  = " << ngpus << std::endl;
  std::cerr << "  GPU free VRAM                  = "
            << (M_gpu_free / (1024 * 1024)) << " MB" << std::endl;
  std::cerr << "  System available RAM           = " << (M_sys / (1024 * 1024))
            << " MB" << std::endl;
  std::cerr << "  S_raw (max source pair)        = " << (S_raw / (1024 * 1024))
            << " MB" << std::endl;
  std::cerr << "  S_crop (max cropped pair)      = " << (S_crop / (1024 * 1024))
            << " MB" << std::endl;

  // ---- Step A: Determine streams_per_gpu ----
  //
  // M_lane ~= 4 * S_crop  (cropped pair + IFG staging + multilook buffers)
  std::size_t M_lane = 4 * S_crop;

  int streams_per_gpu = 1;
  if (M_lane > 0 && M_gpu_free > 0) {
    double safe_vram = static_cast<double>(M_gpu_free) * 0.85;
    int max_by_vram = static_cast<int>(safe_vram / static_cast<double>(M_lane));
    streams_per_gpu = std::max(1, std::min(4, max_by_vram));
  }

  std::cerr << "  [Step A] M_lane = " << (M_lane / (1024 * 1024))
            << " MB, streams_per_gpu = " << streams_per_gpu << std::endl;

  // ---- Step B: Deduce max_slots ----
  //
  // max_slots = (ngpus * streams_per_gpu) + 2
  int max_slots =
      std::min((ngpus * streams_per_gpu) + 2, (ngpus * streams_per_gpu) * 2);

  std::cerr << "  [Step B] max_slots = " << max_slots << std::endl;

  // ---- Step C: Allocate producer_workers & cpu_workers ----
  //
  // producer_workers defaults to 1
  int producer_workers = 1;

  // cpu_workers_ideal = max(4, ngpus * streams_per_gpu)
  int cpu_workers_ideal = std::max(4, ngpus * streams_per_gpu);

  // Apply SMT cap: cpu_workers <= T_hw - 1 - producer_workers
  int cpu_workers = std::min(cpu_workers_ideal,
                             static_cast<int>(T_hw) - 1 - producer_workers);

  // Ensure at least 1 CPU worker for forward progress
  cpu_workers = std::max(1, cpu_workers);

  // ---- Step D: OOM Safety Guardrail ----
  //
  // M_total_predict = (max_slots * S_crop) + ((cpu_workers + 2) * S_raw)
  //                  + S_ref_single
  //
  // The ``+ S_ref_single`` term accounts for the persistent global
  // reference-image cache (max_src_nrow * max_src_ncol elements).
  //
  // If the predicted host footprint exceeds 80 % of available RAM, apply
  // a two-phase progressive scale-down:
  //   Phase 1. Reduce streams_per_gpu (and recalculate max_slots) first
  //            — this lowers both host and device pressure together.
  //   Phase 2. If still over-budget, reduce ngpus (fewer GPU lanes
  //            → fewer slots → lower host footprint).
  std::size_t S_ref_single = static_cast<std::size_t>(ctx.max_src_nrow) *
                             ctx.max_src_ncol * sizeof(Complex);
  std::size_t M_total_predict =
      (static_cast<std::size_t>(max_slots) * S_crop) +
      (static_cast<std::size_t>(cpu_workers + 2) * S_raw) + S_ref_single;

  std::cerr << "  S_ref_single (global ref cache)  = "
            << (S_ref_single / (1024 * 1024)) << " MB" << std::endl;

  // Phase 1: scale down CUDA lanes per GPU
  while (M_total_predict >
             static_cast<std::size_t>(static_cast<double>(M_sys) * 0.80) &&
         streams_per_gpu > 1) {
    streams_per_gpu--;
    max_slots = (ngpus * streams_per_gpu) + 2;
    cpu_workers = std::min(cpu_workers, ngpus * streams_per_gpu);
    M_total_predict = (static_cast<std::size_t>(max_slots) * S_crop) +
                      (static_cast<std::size_t>(cpu_workers + 2) * S_raw) +
                      S_ref_single;
    std::cerr << "  [Guardrail] Predicted host footprint "
              << (M_total_predict / (1024 * 1024))
              << " MB > 80% of available RAM; "
              << "scaling down -> streams_per_gpu=" << streams_per_gpu
              << ", max_slots=" << max_slots << std::endl;
  }

  // Phase 2: scale down number of GPUs
  while (M_total_predict >
             static_cast<std::size_t>(static_cast<double>(M_sys) * 0.80) &&
         ngpus > 1) {
    ngpus--;
    max_slots =
        std::min((ngpus * streams_per_gpu) + 2, ngpus * streams_per_gpu * 2);
    cpu_workers = std::min(cpu_workers, ngpus * streams_per_gpu);
    M_total_predict = (static_cast<std::size_t>(max_slots) * S_crop) +
                      (static_cast<std::size_t>(cpu_workers + 2) * S_raw) +
                      S_ref_single;
    std::cerr << "  [Guardrail] Predicted host footprint "
              << (M_total_predict / (1024 * 1024))
              << " MB > 80% of available RAM; "
              << "scaling down -> ngpus=" << ngpus
              << ", max_slots=" << max_slots << std::endl;
  }

  std::cerr << "  [Step C] producer_workers = " << producer_workers
            << ", cpu_workers = " << cpu_workers << std::endl;
  std::cerr << "  Predicted host footprint = "
            << (M_total_predict / (1024 * 1024)) << " MB" << std::endl;

  // ---- Apply ----
  ctx.streams_per_gpu = streams_per_gpu;
  ctx.raw_slots = max_slots;
  ctx.task_slots_count = max_slots;
  ctx.producer_workers = producer_workers;
  ctx.cpu_workers = cpu_workers;
}

// =======================================================================
// main
// =======================================================================

int main(int argc, char *argv[]) {
  // -- Parse arguments --
  DaemonContext ctx{};

  ctx.rowlook = std::stoi(get_arg(argc, argv, "--rowlook", "1"));
  ctx.collook = std::stoi(get_arg(argc, argv, "--collook", "1"));
  ctx.producer_workers = std::stoi(get_arg(argc, argv, "--io-workers", "-1"));
  ctx.cpu_workers = std::stoi(get_arg(argc, argv, "--cpu-workers", "-1"));
  ctx.raw_slots = std::stoi(get_arg(argc, argv, "--max-slots", "-1"));
  ctx.task_slots_count = ctx.raw_slots;
  ctx.streams_per_gpu =
      std::stoi(get_arg(argc, argv, "--streams-per-gpu", "-1"));
  ctx.out_float = has_flag(argc, argv, "--out-float");
  ctx.verbose = has_flag(argc, argv, "--verbose");

  // GPU count: parse from --gpu-workers, default to -1 (auto-detect)
  ctx.ngpus = std::stoi(get_arg(argc, argv, "--gpu-workers", "-1"));

  // -- All-or-nothing tuning parameter validation --
  bool producer_auto = (ctx.producer_workers == -1);
  bool cpu_auto = (ctx.cpu_workers == -1);
  bool streams_auto = (ctx.streams_per_gpu == -1);
  bool slots_auto = (ctx.raw_slots == -1);
  bool gpu_auto = (ctx.ngpus == -1);

  bool all_auto =
      producer_auto && cpu_auto && streams_auto && slots_auto && gpu_auto;
  bool all_manual = (!producer_auto && ctx.producer_workers > 0) &&
                    (!cpu_auto && ctx.cpu_workers > 0) &&
                    (!streams_auto && ctx.streams_per_gpu > 0) &&
                    (!slots_auto && ctx.raw_slots > 0) &&
                    (!gpu_auto && ctx.ngpus > 0);

  if (!all_auto && !all_manual) {
    std::cerr << "[FATAL] Invalid parameter configuration. Please either "
              << "omit all tuning arguments for hardware-managed "
              << "auto-tuning, or provide the complete set of parameters "
              << "(--io-workers, --cpu-workers, --streams-per-gpu, "
              << "--gpu-workers, --max-slots). "
              << "Partial overrides are not allowed." << std::endl;
    return 1;
  }

  // -- Read task list from file --
  std::string tasks_file = get_arg(argc, argv, "--tasks-file", "");
  if (tasks_file.empty()) {
    std::cerr << "[crossmul_daemon] ERROR: --tasks-file <path> is required."
              << std::endl;
    return 1;
  }

  std::cerr << "[crossmul_daemon] Configuration:" << std::endl;
  std::cerr << "  rowlook         = " << ctx.rowlook << std::endl;
  std::cerr << "  collook         = " << ctx.collook << std::endl;
  std::cerr << "  out_float       = " << (ctx.out_float ? "yes" : "no")
            << std::endl;
  std::cerr << "  io_workers      = " << ctx.producer_workers << std::endl;
  std::cerr << "  cpu_workers     = " << ctx.cpu_workers << std::endl;
  std::cerr << "  gpu_workers     = " << ctx.ngpus << std::endl;
  std::cerr << "  streams_per_gpu = " << ctx.streams_per_gpu << std::endl;
  std::cerr << "  max_slots       = " << ctx.raw_slots << std::endl;
  std::cerr << "  tasks_file      = " << tasks_file << std::endl;

  ctx.tasks = read_tasks_from_file(tasks_file);
  ctx.n_total = static_cast<int>(ctx.tasks.size());

  if (ctx.n_total == 0) {
    std::cerr << "[crossmul_daemon] No tasks found in file.  Exiting."
              << std::endl;
    return 0;
  }
  std::cerr << "[crossmul_daemon] Loaded " << ctx.n_total << " tasks."
            << std::endl;

  // -- Scan pass: determine max dimensions --
  {
    ScopedTimer t("Scan max dimensions");
    scan_max_dimensions(ctx);
  }

  // Guard against zero raw elements (should not happen with valid data)
  if (ctx.max_raw_elements == 0) {
    std::cerr << "[crossmul_daemon] ERROR: max_raw_elements is 0 — "
              << "no valid overlap regions found.  Exiting." << std::endl;
    return 1;
  }

  // -- Auto-tune or validate manual overrides --
  if (all_auto) {
    std::cerr << "[crossmul_daemon] Auto-tuning pipeline parameters..."
              << std::endl;
    auto_tune_parameters(ctx);
  } else {
    // Manual override path — apply basic sanity checks
    ctx.raw_slots = std::max(ctx.raw_slots, ctx.ngpus * ctx.streams_per_gpu);
    ctx.task_slots_count = ctx.raw_slots;
    std::cerr << "[crossmul_daemon] Using manual parameter override."
              << std::endl;
  }
  {
    ScopedTimer t("Allocate buffers");
    allocate_global_cache(ctx);
    allocate_raw_buffers(ctx);
    allocate_task_slots(ctx);
    allocate_gpu_contexts(ctx);
  }

  // -- Initialise active worker counts for termination signalling --
  ctx.cpu_active.store(ctx.cpu_workers);
  ctx.gpu_active.store(ctx.ngpus);

  // -- Launch threads --
  ctx.t_start = std::chrono::steady_clock::now();

  // Stage 1: single I/O producer
  std::vector<std::thread> producer_workers;
  producer_workers.reserve(ctx.producer_workers);
  for (int i = 0; i < ctx.producer_workers; ++i) {
    producer_workers.emplace_back(producer_worker_thread, std::ref(ctx), i);
  }

  // Stage 2: CPU worker thread pool
  std::vector<std::thread> cpu_workers;
  cpu_workers.reserve(ctx.cpu_workers);
  for (int i = 0; i < ctx.cpu_workers; ++i) {
    cpu_workers.emplace_back(cpu_worker_thread, std::ref(ctx), i);
  }

  // Stage 3: one GPU consumer per device
  std::vector<std::thread> gpu_consumers;
  gpu_consumers.reserve(ctx.ngpus);
  for (int i = 0; i < ctx.ngpus; ++i) {
    gpu_consumers.emplace_back(gpu_consumer_thread, std::ref(ctx), i);
  }

  // Stage 4: single async disk writer
  std::thread disk_writer(disk_writer_thread, std::ref(ctx));

  // -- Wait for completion (in pipeline order: producers → CPU → GPU → disk) --
  for (auto &t : producer_workers)
    t.join();
  for (auto &t : cpu_workers)
    t.join();
  for (auto &t : gpu_consumers)
    t.join();
  disk_writer.join();

  // -- Summary --
  double elapsed = std::chrono::duration<double>(
                       std::chrono::steady_clock::now() - ctx.t_start)
                       .count();
  int succeeded = ctx.n_total - ctx.failed.load();
  std::cout << "SUMMARY " << succeeded << " " << ctx.failed.load() << " "
            << ctx.n_total << " " << static_cast<long long>(elapsed)
            << std::endl;

  std::cerr << "[crossmul_daemon] Done.  " << succeeded << " succeeded, "
            << ctx.failed.load() << " failed out of " << ctx.n_total
            << " tasks in " << elapsed << " s." << std::endl;

  // -- Cleanup --
  free_gpu_contexts(ctx);
  free_task_slots(ctx);
  free_raw_buffers(ctx);
  free_global_cache(ctx);

  return (ctx.failed.load() > 0) ? 1 : 0;
}
