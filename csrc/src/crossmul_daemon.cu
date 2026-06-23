/**
 * crossmul_daemon — long-running GPU interferogram processor.
 *
 * Architecture
 * ------------
 * 1.  Reads a burst-pair task list from **stdin** (one
 *     ``<ref_slc> <sec_slc> <out_ifg>`` per line).
 * 2.  **Scan pass** — reads every SLC header to determine the
 *     maximum overlap dimensions across all tasks.
 * 3.  Allocates a pool of **page-locked (pinned) host buffers**
 *     (``max_slots`` slots, each sized to max dimensions).
 * 4.  Spawns a **single I/O producer thread** that reads SLC data
 *     sequentially into free pinned slots, avoiding disk contention.
 * 5.  Spawns **one GPU consumer thread per device**; each pulls
 *     the next ready slot from a bounded queue, performs async
 *     H2D → conj_mul → multi-look → D2H, writes the result, and
 *     returns the slot to the free pool.
 *
 * This design eliminates static work imbalance (cafeteria-style
 * scheduling), prevents I/O thrash (single producer), and enables
 * CUDA-stream overlap between consecutive H2D/D2H transfers.
 */

#include "gpu_device.hpp"
#include "sario.hpp"

#include <algorithm>
#include <array>
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

/**
 * One slot in the pre-allocated pinned host buffer pool.
 *
 * The producer fills ``ref_data`` / ``sec_data`` and metadata;
 * a GPU consumer fills ``result_data``, writes it to disk, and
 * returns the slot to the free queue.
 */
struct BufferSlot {
  int id;

  // -- Pinned host buffers (pre-allocated, sized to max elements) --
  Complex *ref_data;   // max_elements
  Complex *sec_data;   // max_elements
  Complex *result_buf; // max_elements_sm  (complex output path)
  float *result_float; // max_elements_sm  (float output path)

  // -- Task identity --
  Task task;

  // -- Actual dimensions for this burst pair (<= max) --
  int nrow;
  int ncol;
  int nrow_sm;
  int ncol_sm;

  // -- Pre-computed output header --
  std::int32_t ifg_header[NHEADER];

  // -- Slot lifecycle: 0 = free, 1 = filling, 2 = ready, 3 = processing --
  std::atomic<int> state{0};
};

/**
 * Per-GPU context: device buffers, CUDA streams, and a pinned
 * staging buffer for D2H results.
 */
struct GpuContext {
  int gpu_id;

  // CUDA streams for pipeline overlap
  cudaStream_t stream_compute;

  // Pre-allocated device buffers (sized to max dimensions)
  Complex *d_slc1;
  Complex *d_slc2;
  Complex *d_ifg;
  Complex *d_ifg_collook;
  Complex *d_ifglook;
  float *d_phase;

  // Pinned staging for D2H (one buffer per GPU, max looked size)
  Complex *staging_cpx;
  float *staging_float;
};

/**
 * Top-level daemon context — owns the buffer pool, task list,
 * GPU contexts, synchronisation primitives, and progress counters.
 */
struct DaemonContext {
  // -- Configuration --
  int max_slots;
  int max_nrow; // maximum overlap nrow across all tasks
  int max_ncol;
  int max_nrow_sm; // max_nrow / rowlook
  int max_ncol_sm; // max_ncol / collook
  std::size_t max_elements;
  std::size_t max_elements_sm;
  int rowlook;
  int collook;
  bool out_float;
  int ngpus;
  bool verbose;

  // -- Task list --
  std::vector<Task> tasks;
  int n_total;

  // -- Buffer pool --
  std::vector<BufferSlot> slots;

  // -- Free-slot queue (producer waits here) --
  std::queue<int> free_queue;
  std::mutex free_mutex;
  std::condition_variable free_cv;

  // -- Ready-slot queue (consumers wait here) --
  std::queue<int> ready_queue;
  std::mutex ready_mutex;
  std::condition_variable ready_cv;

  // -- GPU workers --
  std::vector<GpuContext> gpus;

  // -- Progress --
  std::atomic<int> completed{0};
  std::atomic<int> failed{0};
  std::atomic<bool> producer_done{false};
  std::chrono::steady_clock::time_point t_start;
};

// -----------------------------------------------------------------------
// Buffer-pool helpers
// -----------------------------------------------------------------------

/**
 * Acquire a free slot from the pool.
 * Blocks (with a brief spin) until a slot is released by a consumer.
 * Returns the slot index.
 */
static int acquire_free_slot(DaemonContext &ctx) {
  std::unique_lock<std::mutex> lock(ctx.free_mutex);
  ctx.free_cv.wait(lock, [&ctx] { return !ctx.free_queue.empty(); });
  int idx = ctx.free_queue.front();
  ctx.free_queue.pop();
  ctx.slots[idx].state.store(1); // FILLING
  return idx;
}

/** Push a slot that has been filled by the producer to the ready queue. */
static void push_ready_slot(DaemonContext &ctx, int idx) {
  ctx.slots[idx].state.store(2); // READY
  {
    std::lock_guard<std::mutex> lock(ctx.ready_mutex);
    ctx.ready_queue.push(idx);
  }
  ctx.ready_cv.notify_one();
}

/**
 * Acquire a ready slot from the queue.
 * Blocks until a slot is available, or returns -1 if the producer
 * has finished and the ready queue is empty.
 */
static int acquire_ready_slot(DaemonContext &ctx) {
  std::unique_lock<std::mutex> lock(ctx.ready_mutex);
  ctx.ready_cv.wait(lock, [&ctx] {
    return !ctx.ready_queue.empty() || ctx.producer_done.load();
  });
  if (ctx.ready_queue.empty()) {
    // Producer finished and queue drained
    return -1;
  }
  int idx = ctx.ready_queue.front();
  ctx.ready_queue.pop();
  ctx.slots[idx].state.store(3); // PROCESSING
  return idx;
}

/** Return a slot to the free pool after the consumer is done with it. */
static void release_slot(DaemonContext &ctx, int idx) {
  ctx.slots[idx].state.store(0); // FREE
  {
    std::lock_guard<std::mutex> lock(ctx.free_mutex);
    ctx.free_queue.push(idx);
  }
  ctx.free_cv.notify_one();
}

// -----------------------------------------------------------------------
// Producer thread
// -----------------------------------------------------------------------

/**
 * Single I/O producer: sequentially reads SLC data for each task
 * into the next available pinned buffer slot, then pushes the filled
 * slot to the ready queue.
 */
static void producer_thread(DaemonContext &ctx) {
  for (int t = 0; t < ctx.n_total; ++t) {
    const Task &task = ctx.tasks[t];

    // -- Acquire a free slot (blocks if pool is exhausted) --
    int slot_idx = acquire_free_slot(ctx);
    BufferSlot &slot = ctx.slots[slot_idx];
    slot.task = task;

    // -- Read headers --
    std::int32_t header1[NHEADER], header2[NHEADER];
    try {
      read_binary<std::int32_t>(task.ref_slc, NHEADER, header1);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " — cannot read header: " << task.ref_slc << std::endl;
      slot.state.store(0);
      {
        std::lock_guard<std::mutex> lk(ctx.free_mutex);
        ctx.free_queue.push(slot_idx);
      }
      ctx.free_cv.notify_one();
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    try {
      read_binary<std::int32_t>(task.sec_slc, NHEADER, header2);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " — cannot read header: " << task.sec_slc << std::endl;
      slot.state.store(0);
      {
        std::lock_guard<std::mutex> lk(ctx.free_mutex);
        ctx.free_queue.push(slot_idx);
      }
      ctx.free_cv.notify_one();
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    // -- Compute overlap dimensions (same logic as crossmul) --
    int left1 = header1[2], top1 = header1[3];
    int right1 = header1[4], bottom1 = header1[5];
    int left2 = header2[2], top2 = header2[3];
    int right2 = header2[4], bottom2 = header2[5];

    int left = (left1 < left2 ? left1 : left2);
    left = (left + ctx.collook - 1) / ctx.collook * ctx.collook;
    int right = (right1 > right2 ? right1 : right2);
    right = right / ctx.collook * ctx.collook;
    int top = (top1 < top2 ? top1 : top2);
    top = (top + ctx.rowlook - 1) / ctx.rowlook * ctx.rowlook;
    int bottom = (bottom1 > bottom2 ? bottom1 : bottom2);
    bottom = bottom / ctx.rowlook * ctx.rowlook;

    int nrow = bottom - top;
    int ncol = right - left;

    if (nrow <= 0 || ncol <= 0) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " — empty overlap region" << std::endl;
      slot.state.store(0);
      {
        std::lock_guard<std::mutex> lk(ctx.free_mutex);
        ctx.free_queue.push(slot_idx);
      }
      ctx.free_cv.notify_one();
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    int nrow_sm = nrow / ctx.rowlook;
    int ncol_sm = ncol / ctx.collook;

    slot.nrow = nrow;
    slot.ncol = ncol;
    slot.nrow_sm = nrow_sm;
    slot.ncol_sm = ncol_sm;

    // Fill output header
    slot.ifg_header[0] = header1[0] / ctx.rowlook;
    slot.ifg_header[1] = header1[1] / ctx.collook;
    slot.ifg_header[2] = left / ctx.collook;
    slot.ifg_header[3] = top / ctx.rowlook;
    slot.ifg_header[4] = right / ctx.collook;
    slot.ifg_header[5] = bottom / ctx.rowlook;

    // -- Read SLC pixel data into pinned host buffers --

    try {
      read_and_resample<Complex>(task.ref_slc, slot.ref_data, left, top, right,
                                 bottom, 0, bottom1 - top1);
      read_and_resample<Complex>(task.sec_slc, slot.sec_data, left, top, right,
                                 bottom, 0, bottom2 - top2);
    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " — I/O error reading SLC data" << std::endl;
      slot.state.store(0);
      {
        std::lock_guard<std::mutex> lk(ctx.free_mutex);
        ctx.free_queue.push(slot_idx);
      }
      ctx.free_cv.notify_one();
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
      continue;
    }

    // -- Push to ready queue --
    push_ready_slot(ctx, slot_idx);

    if (ctx.verbose) {
      std::cerr << "[crossmul_daemon|producer] slot " << slot_idx
                << " ready: " << task.out_ifg << " (" << nrow << "x" << ncol
                << " -> " << nrow_sm << "x" << ncol_sm << ")" << std::endl;
    }
  }

  ctx.producer_done.store(true);
  ctx.ready_cv.notify_all(); // wake consumers so they see producer_done
  std::cerr << "[crossmul_daemon] Producer finished — all " << ctx.n_total
            << " tasks read." << std::endl;
}

// -----------------------------------------------------------------------
// GPU consumer thread
// -----------------------------------------------------------------------

/** Print a periodic progress line to stdout. */
static void emit_progress(const DaemonContext &ctx) {
  auto now = std::chrono::steady_clock::now();
  double elapsed = std::chrono::duration<double>(now - ctx.t_start).count();
  std::cout << "PROGRESS " << ctx.completed.load() << " " << ctx.failed.load()
            << " " << ctx.n_total << " " << static_cast<long long>(elapsed)
            << std::endl;
}

/** GPU consumer: pulls ready slots, runs kernels, writes results. */
static void gpu_consumer_thread(DaemonContext &ctx, int gpu_idx) {
  GpuContext &g = ctx.gpus[gpu_idx];
  set_gpu(g.gpu_id);

  int blockSize = 256;

  while (true) {
    int slot_idx = acquire_ready_slot(ctx);
    if (slot_idx < 0)
      break; // producer done + queue drained

    BufferSlot &slot = ctx.slots[slot_idx];
    const Task &task = slot.task;

    std::size_t elem_bytes = sizeof(Complex) * slot.nrow * slot.ncol;

    try {
      CHECK_CUDA(cudaMemcpyAsync(g.d_slc1, slot.ref_data, elem_bytes,
                                 cudaMemcpyHostToDevice, g.stream_compute));
      CHECK_CUDA(cudaMemcpyAsync(g.d_slc2, slot.sec_data, elem_bytes,
                                 cudaMemcpyHostToDevice, g.stream_compute));
      CHECK_CUDA(cudaStreamSynchronize(g.stream_compute));

      Complex *current_d_ifg = g.d_ifg;

      // -- conj_mul kernel --
      int numBlocks = (slot.nrow * slot.ncol + blockSize - 1) / blockSize;
      conj_mul<<<numBlocks, blockSize, 0, g.stream_compute>>>(
          g.d_slc1, g.d_slc2, g.d_ifg, slot.nrow * slot.ncol);

      // -- Column multi-look --
      numBlocks = (slot.nrow * slot.ncol_sm + blockSize - 1) / blockSize;
      if (ctx.collook > 1) {
        cpx_col_look<<<numBlocks, blockSize, 0, g.stream_compute>>>(
            g.d_ifg, g.d_ifg_collook, ctx.collook, slot.ncol,
            slot.nrow * slot.ncol_sm);
        current_d_ifg = g.d_ifg_collook;
      }

      // -- Row multi-look --
      numBlocks = (slot.nrow_sm * slot.ncol_sm + blockSize - 1) / blockSize;
      if (ctx.rowlook > 1) {
        cpx_row_look<<<numBlocks, blockSize, 0, g.stream_compute>>>(
            current_d_ifg, g.d_ifglook, ctx.rowlook, slot.ncol_sm,
            slot.nrow_sm * slot.ncol_sm);
        current_d_ifg = g.d_ifglook;
      }

      // -- Phase extraction (float path) --
      if (ctx.out_float) {
        point_angle<<<numBlocks, blockSize, 0, g.stream_compute>>>(
            current_d_ifg, g.d_phase, slot.nrow_sm * slot.ncol_sm);
      }

      // -- Async D2H on stream_d2h --
      if (ctx.out_float) {
        CHECK_CUDA(cudaMemcpyAsync(g.staging_float, g.d_phase,
                                   sizeof(float) * slot.nrow_sm * slot.ncol_sm,
                                   cudaMemcpyDeviceToHost, g.stream_compute));
      } else {
        CHECK_CUDA(
            cudaMemcpyAsync(g.staging_cpx, current_d_ifg,
                            sizeof(Complex) * slot.nrow_sm * slot.ncol_sm,
                            cudaMemcpyDeviceToHost, g.stream_compute));
      }

      CHECK_CUDA(cudaStreamSynchronize(g.stream_compute));

      // -- Write output to disk --
      if (ctx.out_float) {
        save_binary<float>(g.staging_float, slot.nrow_sm * slot.ncol_sm,
                           slot.ifg_header, NHEADER, task.out_ifg);
      } else {
        save_binary<Complex>(g.staging_cpx, slot.nrow_sm * slot.ncol_sm,
                             slot.ifg_header, NHEADER, task.out_ifg);
      }

      // -- Success --
      std::cout << "OK " << task.out_ifg << std::endl;
      ctx.completed.fetch_add(1);

    } catch (const std::exception &e) {
      std::cerr << "[crossmul_daemon] FAIL " << task.out_ifg
                << " — GPU error: " << e.what() << std::endl;
      ctx.failed.fetch_add(1);
      ctx.completed.fetch_add(1);
    }

    // -- Return slot to free pool --
    release_slot(ctx, slot_idx);

    // Emit progress every few completions (avoid stdout spam)
    int done = ctx.completed.load();
    if (done % 10 == 0 || done == ctx.n_total) {
      emit_progress(ctx);
    }
  }

  std::cerr << "[crossmul_daemon] GPU " << g.gpu_id << " consumer exiting."
            << std::endl;
}

// -----------------------------------------------------------------------
// Initialisation helpers
// -----------------------------------------------------------------------

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
 * Scan all SLC headers across every task to determine the maximum
 * overlap dimensions.  This mirrors the first pass in the legacy
 * ``crossmul()`` batch function.
 */
static void scan_max_dimensions(DaemonContext &ctx) {
  int max_nrow = 0, max_ncol = 0;

  for (const auto &task : ctx.tasks) {
    std::int32_t header1[NHEADER], header2[NHEADER];

    read_binary<std::int32_t>(task.ref_slc, NHEADER, header1);
    read_binary<std::int32_t>(task.sec_slc, NHEADER, header2);

    int left1 = header1[2], top1 = header1[3];
    int right1 = header1[4], bottom1 = header1[5];
    int left2 = header2[2], top2 = header2[3];
    int right2 = header2[4], bottom2 = header2[5];

    max_nrow = std::max(bottom1 - top1, max_nrow);
    max_ncol = std::max(right1 - left1, max_ncol);

    int left = (left1 < left2 ? left1 : left2);
    int right = (right1 > right2 ? right1 : right2);
    int top = (top1 < top2 ? top1 : top2);
    int bottom = (bottom1 > bottom2 ? bottom1 : bottom2);

    max_nrow = std::max(bottom - top, max_nrow);
    max_ncol = std::max(right - left, max_ncol);
  }

  ctx.max_nrow = max_nrow;
  ctx.max_ncol = max_ncol;
  ctx.max_nrow_sm = max_nrow / ctx.rowlook;
  ctx.max_ncol_sm = max_ncol / ctx.collook;
  ctx.max_elements = static_cast<std::size_t>(max_nrow) * max_ncol;
  ctx.max_elements_sm =
      static_cast<std::size_t>(ctx.max_nrow_sm) * ctx.max_ncol_sm;

  std::cerr << "[crossmul_daemon] Max overlap dimensions: " << max_nrow << " x "
            << max_ncol << "  (looked: " << ctx.max_nrow_sm << " x "
            << ctx.max_ncol_sm << ")" << std::endl;
  std::cerr << "[crossmul_daemon] Buffer size per slot: "
            << (ctx.max_elements * sizeof(Complex) * 2 / (1024 * 1024))
            << " MB (ref+sec)" << std::endl;
}

/** Allocate the pinned host buffer pool. */
static void allocate_buffer_pool(DaemonContext &ctx) {
  // ctx.slots.resize(ctx.max_slots);
  ctx.slots = std::vector<BufferSlot>(ctx.max_slots);
  for (int i = 0; i < ctx.max_slots; ++i) {
    BufferSlot &s = ctx.slots[i];
    s.id = i;
    CHECK_CUDA(cudaMallocHost((void **)&s.ref_data,
                              sizeof(Complex) * ctx.max_elements));
    CHECK_CUDA(cudaMallocHost((void **)&s.sec_data,
                              sizeof(Complex) * ctx.max_elements));
    if (ctx.out_float) {
      s.result_float = nullptr;
      s.result_buf = nullptr;
    } else {
      CHECK_CUDA(cudaMallocHost((void **)&s.result_buf,
                                sizeof(Complex) * ctx.max_elements_sm));
      s.result_float = nullptr;
    }
    // All slots start in the free queue
    ctx.free_queue.push(i);
  }
  std::cerr << "[crossmul_daemon] Allocated " << ctx.max_slots
            << " pinned buffer slots." << std::endl;
}

/** Allocate per-GPU device buffers and CUDA streams. */
static void allocate_gpu_contexts(DaemonContext &ctx) {
  ctx.gpus.resize(ctx.ngpus);
  for (int i = 0; i < ctx.ngpus; ++i) {
    GpuContext &g = ctx.gpus[i];
    g.gpu_id = i;

    // Select GPU before allocating
    int orig_dev;
    cudaGetDevice(&orig_dev);
    cudaSetDevice(i);

    // CUDA streams
    cudaStreamCreate(&g.stream_compute);

    // Device buffers
    CHECK_CUDA(
        cudaMalloc((void **)&g.d_slc1, sizeof(Complex) * ctx.max_elements));
    CHECK_CUDA(
        cudaMalloc((void **)&g.d_slc2, sizeof(Complex) * ctx.max_elements));
    CHECK_CUDA(
        cudaMalloc((void **)&g.d_ifg, sizeof(Complex) * ctx.max_elements));

    if (ctx.collook > 1) {
      CHECK_CUDA(cudaMalloc((void **)&g.d_ifg_collook,
                            sizeof(Complex) * ctx.max_nrow * ctx.max_ncol_sm));
    } else {
      g.d_ifg_collook = nullptr;
    }

    if (ctx.rowlook > 1) {
      CHECK_CUDA(
          cudaMalloc((void **)&g.d_ifglook,
                     sizeof(Complex) * ctx.max_nrow_sm * ctx.max_ncol_sm));
    } else {
      g.d_ifglook = nullptr;
    }

    if (ctx.out_float) {
      CHECK_CUDA(
          cudaMalloc((void **)&g.d_phase, sizeof(float) * ctx.max_elements_sm));
      CHECK_CUDA(cudaMallocHost((void **)&g.staging_float,
                                sizeof(float) * ctx.max_elements_sm));
      g.staging_cpx = nullptr;
    } else {
      g.d_phase = nullptr;
      CHECK_CUDA(cudaMallocHost((void **)&g.staging_cpx,
                                sizeof(Complex) * ctx.max_elements_sm));
      g.staging_float = nullptr;
    }

    cudaSetDevice(orig_dev);

    std::cerr << "[crossmul_daemon] GPU " << i << " device buffers allocated ("
              << (ctx.max_elements * sizeof(Complex) * 3 / (1024 * 1024))
              << " MB)." << std::endl;
  }
}

/** Free the buffer pool. */
static void free_buffer_pool(DaemonContext &ctx) {
  for (auto &s : ctx.slots) {
    if (s.ref_data)
      cudaFreeHost(s.ref_data);
    if (s.sec_data)
      cudaFreeHost(s.sec_data);
    if (s.result_buf)
      cudaFreeHost(s.result_buf);
    if (s.result_float)
      cudaFreeHost(s.result_float);
  }
  ctx.slots.clear();
}

/** Free per-GPU resources. */
static void free_gpu_contexts(DaemonContext &ctx) {
  for (auto &g : ctx.gpus) {
    cudaSetDevice(g.gpu_id);
    if (g.d_slc1)
      cudaFree(g.d_slc1);
    if (g.d_slc2)
      cudaFree(g.d_slc2);
    if (g.d_ifg)
      cudaFree(g.d_ifg);
    if (g.d_ifg_collook)
      cudaFree(g.d_ifg_collook);
    if (g.d_ifglook)
      cudaFree(g.d_ifglook);
    if (g.d_phase)
      cudaFree(g.d_phase);
    if (g.staging_cpx)
      cudaFreeHost(g.staging_cpx);
    if (g.staging_float)
      cudaFreeHost(g.staging_float);
    cudaStreamDestroy(g.stream_compute);
  }
  ctx.gpus.clear();
}

/** Read all task lines from stdin.  Lines are ``ref_slc sec_slc out_ifg``. */
static std::vector<Task> read_tasks_from_stdin() {
  std::vector<Task> tasks;
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty())
      continue;
    std::istringstream iss(line);
    Task t;
    if (iss >> t.ref_slc >> t.sec_slc >> t.out_ifg) {
      tasks.push_back(t);
      std::cout << t.ref_slc << " " << t.sec_slc << " " << t.out_ifg
                << std::endl;
    } else {
      std::cerr << "[crossmul_daemon] WARNING: skipping malformed "
                << "task line: " << line << std::endl;
    }
  }
  return tasks;
}

// -----------------------------------------------------------------------
// main
// -----------------------------------------------------------------------

int main(int argc, char *argv[]) {
  // -- Parse arguments --
  DaemonContext ctx{};

  ctx.rowlook = std::stoi(get_arg(argc, argv, "--rowlook", "1"));
  ctx.collook = std::stoi(get_arg(argc, argv, "--collook", "1"));
  ctx.max_slots = std::stoi(get_arg(argc, argv, "--max-slots", "8"));
  ctx.out_float = has_flag(argc, argv, "--out-float");
  ctx.verbose = has_flag(argc, argv, "--verbose");

  // GPU count: from --gpus flag, else auto-detect
  {
    std::string gpus_str = get_arg(argc, argv, "--gpus", "");
    if (!gpus_str.empty()) {
      ctx.ngpus = std::stoi(gpus_str);
    } else {
      int count;
      cudaError_t err = cudaGetDeviceCount(&count);
      if (err != cudaSuccess) {
        std::cerr << "[crossmul_daemon] Cannot detect GPU count; "
                  << "defaulting to 1." << std::endl;
        count = 1;
      }
      ctx.ngpus = count;
    }
  }

  if (ctx.max_slots < ctx.ngpus) {
    std::cerr << "[crossmul_daemon] WARNING: max_slots (" << ctx.max_slots
              << ") < ngpus (" << ctx.ngpus
              << "); increasing max_slots to ngpus." << std::endl;
    ctx.max_slots = ctx.ngpus;
  }

  std::cerr << "[crossmul_daemon] Configuration:" << std::endl;
  std::cerr << "  rowlook   = " << ctx.rowlook << std::endl;
  std::cerr << "  collook   = " << ctx.collook << std::endl;
  std::cerr << "  out_float = " << (ctx.out_float ? "yes" : "no") << std::endl;
  std::cerr << "  max_slots = " << ctx.max_slots << std::endl;
  std::cerr << "  ngpus     = " << ctx.ngpus << std::endl;

  // -- Read task list from stdin --
  ctx.tasks = read_tasks_from_stdin();
  ctx.n_total = static_cast<int>(ctx.tasks.size());

  if (ctx.n_total == 0) {
    std::cerr << "[crossmul_daemon] No tasks received.  Exiting." << std::endl;
    return 0;
  }
  std::cerr << "[crossmul_daemon] Received " << ctx.n_total << " tasks."
            << std::endl;

  // -- Scan pass: determine max dimensions --
  scan_max_dimensions(ctx);

  // -- Allocate resources --
  allocate_buffer_pool(ctx);
  allocate_gpu_contexts(ctx);

  // -- Launch producer + GPU consumer threads --
  ctx.t_start = std::chrono::steady_clock::now();

  std::thread producer(producer_thread, std::ref(ctx));

  std::vector<std::thread> consumers;
  consumers.reserve(ctx.ngpus);
  for (int i = 0; i < ctx.ngpus; ++i) {
    consumers.emplace_back(gpu_consumer_thread, std::ref(ctx), i);
  }

  // -- Wait for completion --
  producer.join();
  for (auto &t : consumers)
    t.join();

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
  free_buffer_pool(ctx);

  return (ctx.failed.load() > 0) ? 1 : 0;
}
