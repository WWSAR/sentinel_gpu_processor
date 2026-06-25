# Adaptive Pipeline Parameter Tuning for `crossmul_daemon`

This document describes the hardware-aware auto-configuration framework used by
`crossmul_daemon` to derive optimal multi-stage pipeline orchestration parameters
at startup.

---

## 1. Motivation

The daemon implements a four-stage decoupled pipeline:

| Stage | Role | Threading Model |
|-------|------|-----------------|
| 1     | Raw SLC disk read (I/O producer) | 1–2 threads |
| 2     | CPU burst cropping | `cpu_workers` threads |
| 3     | GPU cross-multiplication + multi-looking | 1 consumer per GPU, *n* lanes each |
| 4     | Async disk write | 1 dedicated thread |

Manually tuning `--io-workers`, `--cpu-workers`, `--streams-per-gpu`,
`--gpu-workers`, and `--max-slots` for every deployment is fragile. The
auto-tuning framework replaces guesswork with a deterministic derivation that
takes physical host constraints (RAM, VRAM, SMT threads) and task dimensions
(source/cropped/output sizes) as inputs and produces a balanced configuration
that avoids both under-subscription (idle hardware) and over-subscription (OOM
faults, PCIe contention, VRAM fragmentation).

---

## 2. CLI Contract — The Override Rule

All tuning parameters default to **`-1`** (meaning *auto-tune*):

| CLI Flag | Internal Name | Default |
|----------|---------------|---------|
| `--io-workers` | `producer_workers` | `-1` |
| `--cpu-workers` | `cpu_workers` | `-1` |
| `--streams-per-gpu` | `streams_per_gpu` | `-1` |
| `--gpu-workers` | `ngpus` | `-1` |
| `--max-slots` | `raw_slots` / `task_slots_count` | `-1` |

**All-or-Nothing enforcement:**

- **All `-1`** → The daemon proceeds with hardware auto-tuning (Section 3–5).
- **All positive integers** → The daemon honours the user override and skips
  auto-tuning. Basic sanity checks are still performed (`max_slots` is clamped
  to at least `ngpus * streams_per_gpu`).
- **Partial subset** → The daemon prints a fatal error to `stderr` and
  terminates with exit code `1`:

  ```
  [FATAL] Invalid parameter configuration. Please either omit all tuning
  arguments for hardware-managed auto-tuning, or provide the complete set of
  parameters (--io-workers, --cpu-workers, --streams-per-gpu, --gpu-workers,
  --max-slots). Partial overrides are not allowed.
  ```

The same contract is enforced at the Python layer in `s1proc.interfere` before
the daemon is even launched, providing a consistent user experience.

---

## 3. Hardware Telemetry

Before deriving parameters, the daemon queries the following environmental
constants (Step 0):

### 3.1 System Available RAM ($M_{sys}$)

- **Linux:** Reads `MemAvailable` from `/proc/meminfo`.
- **Windows:** Calls `GlobalMemoryStatusEx` to obtain `ullAvailPhys`.
- **Fallback:** 8 GiB if the OS query fails.

### 3.2 GPU Free VRAM ($M_{gpu}$)

Uses `cudaMemGetInfo` on every detected GPU and returns the **minimum** free
global memory across all devices. This ensures the auto-tuner never assumes
more VRAM than the most constrained GPU in a heterogeneous setup.

### 3.3 Physical CPU SMT Threads ($T_{hardware}$)

Calls `std::thread::hardware_concurrency()` to obtain the total number of
hardware execution contexts (logical cores / SMT threads).

### 3.4 Max Task Dimensions (from Scan Pass)

After reading every SLC header in a metadata scan pass, four size constants
are computed:

| Symbol | Formula | Description |
|--------|---------|-------------|
| $S_{raw}$ | $\max\_src\_nrow \times \max\_src\_ncol \times 8\text{ B} \times 2$ | Max raw (uncropped) input pair in bytes |
| $S_{crop}$ | $\max\_nrow \times \max\_ncol \times 8\text{ B} \times 2$ | Max cropped overlap pair in bytes |
| $S_{out}$ | $\max\_nrow\_sm \times \max\_ncol\_sm \times 4\text{ B}$ | Max output interferogram (float path) |

$S_{raw}$ and $S_{crop}$ include both reference and secondary images
(hence the factor of 2). Each complex element is 8 bytes (`float2`).

---

## 4. Mathematical Derivation

### Step A: GPU Concurrent Execution Lanes (`streams_per_gpu`)

Each asynchronous GPU lane requires a persistent VRAM staging allocation:

$$M_{lane} \approx S_{crop} + \text{(Interferogram Staging)} + \text{(Multilook Buffers)} \approx 4 \times S_{crop}$$

Reserving a 15 % safety margin for CUDA context overhead, the maximum safe
number of lanes per GPU is:

$$\text{stream\_per\_gpu} = \max\left(1,\ \min\left(4,\ \left\lfloor \frac{M_{gpu} \times 0.85}{M_{lane}} \right\rfloor\right)\right)$$

**Reasoning for the [1, 4] bound:**

- **Minimum 1:** Every GPU must have at least one lane to make progress.
- **Maximum 4:** Beyond 4 concurrent streams, PCIe bus contention and VRAM
  fragmentation typically yield diminishing or negative returns for burst-pair
  cross-multiplication workloads.

### Step B: Staging Buffer Pool Size (`max_slots`)

To completely mask compute and Host-to-Device transfer latency with
double-buffering across all concurrent GPU engines, `max_slots` must span at
least one full iteration plus slack for every execution lane, capped at two
full iterations to avoid over-allocating:

$$\text{max\_slots} = \min\left((N_{gpu} \times \text{stream\_per\_gpu}) + 2,\ (N_{gpu} \times \text{stream\_per\_gpu}) \times 2\right)$$

The `+ 2` term provides slack for the I/O producer to stay ahead of the CPU
workers even when the GPU-ready queue is momentarily full. The upper bound
(`× 2`) prevents excessive pinned-memory allocation on systems with many GPUs.

### Step C: Pipeline Thread Budgets (`producer_workers` and `cpu_workers`)

The global compute footprint across Stage 1 (Read I/O), Stage 2 (CPU Crop),
and Stage 4 (Disk Write) must stay strictly bounded by the total physical SMT
hardware threads to avoid core oversubscription:

$$T_{total\_footprint} = \text{producer\_workers} + \text{cpu\_workers} + \text{disk\_writers}$$

Given that Stage 4 (`disk_writers`) is fixed at 1:

$$\text{producer\_workers} + \text{cpu\_workers} \le T_{hardware} - 1$$

**`producer_workers`** — Defaults to **1** to maintain linear sequential disk
sectors and avoid HDD head thrashing. On solid-state media it may be scaled up
to 2 in future revisions.

**`cpu_workers`** — Targeted to keep the GPU queue continuously fed:

$$\text{cpu\_workers\_ideal} = \max\left(4,\ N_{gpu} \times \text{stream\_per\_gpu}\right)$$

$$\text{cpu\_workers} = \min\left(\text{cpu\_workers\_ideal},\ T_{hardware} - 1 - \text{producer\_workers}\right)$$

$$\text{cpu\_workers} = \max(1,\ \text{cpu\_workers})$$

The minimum of 1 guarantees forward execution progress even on single-core
machines.

---

## 5. Host Backpressure Safety Guardrail (OOM Protection)

Before allocating pinned-memory pools (`RawBuffer` pool and `TaskSlot` pool),
the daemon computes the absolute peak virtual resident memory footprint
required by the runtime pipelines:

$$M_{total\_predict} = (\text{max\_slots} \times S_{crop}) + ((\text{cpu\_workers} + 2) \times S_{raw})$$

The first term accounts for all `TaskSlot` cropped-pair buffers plus staging.
The second term accounts for `RawBuffer` uncropped read buffers: `cpu_workers`
in-flight raw buffers from Stage 1 → Stage 2 handoff, plus 2 extra buffers for
double-buffered I/O reads.

If $M_{total\_predict}$ exceeds **80 %** of the host's actual available RAM
($M_{sys}$), a two-phase progressive scale-down loop runs:

**Phase 1 — Reduce streams per GPU:**

```cpp
while (M_total_predict > M_sys * 0.80 && stream_per_gpu > 1) {
    stream_per_gpu--;
    max_slots = (ngpus * stream_per_gpu) + 2;
    cpu_workers = min(cpu_workers, ngpus * stream_per_gpu);
    M_total_predict = (max_slots * S_crop) + ((cpu_workers + 2) * S_raw);
}
```

Lowering `stream_per_gpu` reduces both VRAM pressure (fewer device buffers per
GPU) and host memory pressure (fewer pipeline slots).  This is the preferred
leverage point.

**Phase 2 — Reduce number of GPUs:**

```cpp
while (M_total_predict > M_sys * 0.80 && ngpus > 1) {
    ngpus--;
    max_slots = min((ngpus * streams_per_gpu) + 2, ngpus * streams_per_gpu * 2);
    cpu_workers = min(cpu_workers, ngpus * streams_per_gpu);
    M_total_predict = (max_slots * S_crop) + ((cpu_workers + 2) * S_raw);
}
```

If scaling down lanes is insufficient, the daemon reduces the number of active
GPUs.  This is a coarser knob that drops both device and host allocations
proportionally.

---

## 6. Complete Flow Diagram

```
Parse CLI args (defaults = -1)
        │
        ▼
All-or-Nothing check
  ├── Partial → [FATAL] exit(1)
  ├── All positive → Manual override (skip tuning)
  └── All -1 → Auto-tune path
                  │
                  ▼
          Read task file
                  │
                  ▼
          Scan Pass (all SLC headers)
          → max_src_nrow, max_src_ncol
          → max_nrow, max_ncol
          → max_raw_elements
                  │
                  ▼
          Hardware Telemetry
          → T_hw (SMT threads)
          → M_gpu (free VRAM)
          → M_sys (available RAM)
                  │
                  ▼
          Step A: streams_per_gpu
          Step B: max_slots
          Step C: producer_workers, cpu_workers
                  │
                  ▼
          OOM Guardrail
          (progressive scale-down if needed)
                  │
                  ▼
          Allocate buffers with final parameters
```

---

## 7. Example Output

```
[crossmul_daemon|auto-tune] Hardware telemetry:
  CPU SMT threads                = 32
  GPUs detected                  = 2
  GPU free VRAM                  = 11264 MB
  System available RAM           = 65472 MB
  S_raw (max source pair)        = 512 MB
  S_crop (max cropped pair)      = 384 MB
  [Step A] M_lane = 1536 MB, streams_per_gpu = 4
  [Step B] max_slots = 10
  [Step C] producer_workers = 1, cpu_workers = 8
  Predicted host footprint = 8960 MB
```

---

## 8. Manual Override Use Cases

Manual overrides are appropriate in these scenarios:

- **Heterogeneous clusters** where different nodes have different hardware and
  the user wants exact control for reproducibility.
- **Shared systems** where only a subset of resources should be consumed.
- **Benchmarking** to isolate the effect of a specific parameter.

To use manual overrides, provide **all five** tuning flags with positive
integer values (and optionally `--verbose` for per-stage logging):

```bash
crossmul_daemon \
    --io-workers 1 \
    --cpu-workers 6 \
    --gpu-workers 2 \
    --streams-per-gpu 3 \
    --max-slots 16 \
    --rowlook 4 \
    --collook 4 \
    --tasks-file tasks.txt
```

---

## References

- CUDA C Programming Guide — Device Management (`cudaMemGetInfo`, `cudaGetDeviceProperties`)
- Linux `proc(5)` man page — `/proc/meminfo`
- `std::thread::hardware_concurrency` (C++11, ISO/IEC 14882:2011 §30.3.1.1)
- Sentinel-1 SAR Technical Guide — Interferometric Wide Swath (IW) burst geometry
