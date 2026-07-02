# Sentinel GPU Processor — User Guide

## Overview

`s1proc` is a GPU-accelerated Sentinel-1 InSAR processing pipeline. It provides
a suite of CLI subcommands that take you from raw data discovery through to
deformation time series. Each subcommand reads a YAML configuration file
(`config.yaml`) that lives in the current working directory.

The typical workflow runs in order:

1. `s1proc init` — initialise a configuration and discover data
2. `cd` into a path folder (e.g. `ascending/path_123/`)
3. `s1proc preproc` — download Sentinel-1 data, orbits, and DEM
4. `s1proc stack` — geocode every Sentinel-1 scene
5. `s1proc amp` — multilook the geocoded SLCs to amplitude images
6. `s1proc integrity` — flag scenes with missing bursts
7. `s1proc slcpairs` — build the interferogram pair list
8. `s1proc interfere` — form wrapped interferograms (GPU)
9. `s1proc coh` — compute InSAR phase coherence

The remaining steps are **experimental** and have not been thoroughly tested:

10. `s1proc phasecorr` — tropospheric correction & Goldstein filtering
11. `s1proc unwrap` — phase unwrapping (whirlwind or SNAPHU)
12. `s1proc timeseries` — SBAS time-series inversion (GPU)

---

## What's new compared to the original Sentinel-1 L1 processor

### 1. Unified geocoding step

The original processor required three sequential steps: `readgeotiff`
(extract GeoTIFFs from zip files), `deramp_burst` (deramp individual
bursts), and `geo2rdr_reramp` (geocode and reramp to radar coordinates).
Each step wrote intermediate files to disk, consuming significant storage
and I/O bandwidth on large stacks.

`s1proc` combines all three into a single `geo2rdr` operation
(`s1proc.sentinel_scene.sentinel_scene` → `s1proc.sentinel_stack.run_stack`).
It reads raw TIFF data directly from the Sentinel-1 zip files — **no
unzipping is needed** — and writes geocoded SLCs straight to disk. No
intermediate per-burst GeoTIFFs or deramped files are stored, saving disk
space and reducing I/O load.

### 2. Burst-level geocoded SLCs (GSLCs)

Inspired by the [COMPASS](https://github.com/isce-framework/compass)
processor, `s1proc` outputs **per-burst** geocoded SLCs (`.gslc` files)
rather than full-scene images. This has two important advantages:

- **Cross-frame interferometry.** In Sentinel-1 acquisitions, the same burst
  may belong to different frames on different dates. The original
  frame-by-frame processor cannot form interferograms between these
  acquisitions without extra reprocessing. Burst-level GSLCs allow any burst
  to be paired with its counterpart regardless of frame assignment.

- **Seamless phase stitching.** The original processor forms interferograms
  from full-subswath images, which introduces decorrelated lines at burst
  and subswath boundaries. In `s1proc`, burst-level GSLCs are paired to
  generate per-burst interferograms (`s1proc.interfere.interfere_subswath`),
  which are then stitched within each subswath (`stitch_subswath`) and
  finally merged across subswaths (`stitch`). The phase-offset correction
  applied during stitching produces a seamless interferogram free of
  boundary artifacts.

---

## Prerequisites

- **aria2c** — required for downloading Sentinel-1 data. This is **essential
  for users in China** because the ASF-provided Python download script is
  extremely slow and frequently breaks up.
  - **Windows users**: an `aria2c.exe` is provided in the `extern/` folder.
    Copy it to `s1proc/bin/` after installing the package:
    ```bash
    cp extern/aria2c.exe s1proc/bin/
    ```
  - **Linux (CentOS / RHEL)**:
    ```bash
    sudo yum install aria2 -y
    ```
  - **Linux (Ubuntu / Debian)**:
    ```bash
    sudo apt install aria2 -y
    ```
- **NASA Earthdata Login** — a free account at
  [urs.earthdata.nasa.gov](https://urs.earthdata.nasa.gov) is required to
  download Sentinel-1 data and COP DEM tiles. Store your credentials in a
  `.netrc` file:
  ```
  machine urs.earthdata.nasa.gov login YOUR_USERNAME password YOUR_PASSWORD
  ```
- **GPU** — an NVIDIA GPU is required for interferogram
  formation (`interfere`) and time-series analysis (`timeseries`).

---

## Step 1 — Initialise the configuration

```bash
s1proc init
```

**What it does** (see `s1proc._config.initialize_config`):

Writes a default `config.yaml` to the current directory, copied from the
package's built-in template. Every subsequent subcommand reads this file to
know where to find data, which processing parameters to use, and so on.

**Files created:**

| File | Description |
|---|---|
| `config.yaml` | Main configuration file (I/O paths, processing parameters, unwrapping, time-series settings) |

### Providing date and bounding box

You can supply the study period and area on the command line:

```bash
s1proc init \
    --start-date 2023-01-01 \
    --end-date 2023-12-31 \
    --bbox -123.5 37.0 -122.0 38.0 \
    --flight-direction ASCENDING
```

When date and bbox are provided, `s1proc init` will:

1. Write `config.yaml` with those values filled in.
2. Query the **ASF Data Search API** (via `s1proc.query.query_asf`) to
   discover all Sentinel-1 IW SLC scenes overlapping your area.
3. Save the full result set as `roi.geojson`.
4. Group scenes by orbit **path number**, create one sub-folder per path
   (e.g. `ascending/path_123/`), and write a tailored `config.yaml` and
   `roi.geojson` into each.

**Files created (with date & bbox):**

| File | Description |
|---|---|
| `config.yaml` | Main config with date, bbox, and flight direction filled in |
| `roi.geojson` | All Sentinel-1 scenes matching the query |
| `ascending/path_NNN/config.yaml` | Per-path configuration |
| `ascending/path_NNN/roi.geojson` | Scenes belonging to that path |

### The `--setup-only` flag

If you want to inspect or edit the configuration before the per-path
sub-folders are populated, use `--setup-only`:

```bash
s1proc init \
    --start-date 2023-01-01 \
    --end-date 2023-12-31 \
    --bbox -123.5 37.0 -122.0 38.0 \
    --setup-only
```

This writes a single `config.yaml` and **stops** — no ASF query, no
sub-folders. You can now edit the file (for instance, remove paths from
`area.path_list` that you do not want to process, or adjust multilooking
factors). When you are ready, run:

```bash
s1proc subconfig
```

This calls `s1proc._config.populate_config`, which reads `config.yaml`,
queries ASF, and creates the per-path sub-folders. It respects any changes
you made to the main config.

### Without any arguments

```bash
s1proc init
```

Writes the default `config.yaml` with empty `date` and `area` sections. You
edit the file manually to fill in `start`, `end`, `bbox`, and
`flight_direction`, then run `s1proc subconfig` yourself.

### Skipping downloads — using pre-downloaded data

If you already have Sentinel-1 zip files, a DEM in ROI_PAC int16 format
(`elevation.dem` + `elevation.dem.rsc`), and precise orbit EOF files on
disk, you can skip the `preproc` step entirely:

```bash
s1proc init
```

Then edit `config.yaml` to point the `io` section at your existing data
directories:

```yaml
io:
    data_path: /path/to/your/sentinel_zip_files
    eof_path: /path/to/your/eof_files
    dem_file: /path/to/your/elevation.dem
    rsc_file: /path/to/your/elevation.dem.rsc
```

Set `proc.download_data`, `proc.download_eof`, and `proc.download_dem` to
`false`, then jump straight to Step 4:

```bash
s1proc stack
```



## Step 2 — Move into a path folder

After `s1proc init` (or `s1proc subconfig`), your directory tree looks like:

```
.
├── config.yaml
├── roi.geojson
├── ascending/
│   ├── path_123/
│   │   ├── config.yaml
│   │   └── roi.geojson
│   └── path_156/
│       ├── config.yaml
│       └── roi.geojson
└── descending/
    └── path_42/
        ├── config.yaml
        └── roi.geojson
```

All downstream commands operate on **one path at a time** and expect
`config.yaml` in the current working directory:

```bash
cd ascending/path_123
```

From here on, every `s1proc` command reads `./config.yaml` automatically.

---

## Step 3 — Preprocessing (download data, orbits, DEM)

```bash
s1proc preproc
```

**What it does** (see `s1proc.preproc.preprocess`):

1. **Filters** `roi.geojson` to retain only the frame numbers listed in
   `area.frame_list`, then generates a `roi.metalink` file (an XML descriptor
   understood by `aria2c`).
2. **Downloads the COP DEM** (if `proc.download_dem` is `true`):
   - Fetches the COP global VRT tile tree into the local cache.
   - Computes the intersection of your `area.bbox` with the bounding box of
     all frame footprints.
   - Downloads the DEM as a GeoTIFF (`roi_dem.tif`) at 6x/3x upsampling via
     GDAL, then converts it to ROI_PAC binary format.
3. **Downloads Sentinel-1 SLC zip files** (if `proc.download_data` is
   `true`) using `aria2c` with the metalink file. Files land in the
   directory specified by `io.data_path` (default: `data/`).
4. **Downloads precise orbit (EOF) files** (if `proc.download_eof` is
   `true`) via the `sentineleof` library. Files land in `io.eof_path`
   (default: `eof/`).

**Files created:**

| File | Description |
|---|---|
| `roi.metalink` | Metalink XML with download URLs for all scenes |
| `roi_dem.tif` | COP DEM GeoTIFF (int16, 6x/3x upsampled) |
| `elevation.dem` | DEM in ROI_PAC binary format |
| `elevation.dem.rsc` | RSC georeferencing file for the DEM |
| `data/*.zip` | Sentinel-1 SLC zip files |
| `eof/*.EOF` | Precise orbit files |

**Example with custom config path:**

```bash
s1proc preproc --config-file another_config.yaml
```

### Controlling what gets downloaded

Each download step is gated by a boolean in the `proc` section of
`config.yaml`:

```yaml
proc:
    download_dem: true
    download_data: true
    download_eof: true
```

Set any to `false` if you already have the data on disk.

### aria2c notes

The metalink-based download uses `aria2c` under the hood (see
`s1proc.sentinel_downloader.download_metalink`). This is far more reliable
than the ASF Python download script, especially from within China where
direct HTTP downloads from ASF are throttled or drop frequently.

On Windows the `aria2c.exe` shipped in `extern/` must be copied into
`s1proc/bin/` so that `get_bin_path("aria2c")` can find it. On Linux, install
`aria2` via your system package manager.

---

## Step 4 — Geocode the SLC stack

```bash
s1proc stack
```

**What it does** (see `s1proc.sentinel_stack.run_stack` → `stack`):

Processes every Sentinel-1 zip file into a coregistered, geocoded SLC image.
For each scene it:

1. Reads the acquisition time and matches it to the correct precise orbit
   (EOF) file.
2. Calls `s1proc.sentinel_scene.sentinel_scene`, which unzips the product,
   extracts the GeoTIFF for each subswath, applies deramping, and geocodes
   to the DEM grid.
3. Writes per-burst geocoded SLC files (`.gslc`) to `io.slc_path` (default:
   `slc/`).
4. Records a `.done` marker in `io.proc_path` (default: `proc/`) so the
   scene is skipped on re-runs.

**Files created:**

| File | Description |
|---|---|
| `slc/YYYYMMDD_*.gslc` | Per-burst geocoded SLC images (complex64) |
| `proc/*.done` | Completion markers (JSON) |

**Key options:**

| Option | Default | Description |
|---|---|---|
| `--polarization` | `vv` | Polarisation to process (`hh`, `hv`, `vh`, `vv`) |
| `--subswath-list` | `1 2 3` | Subswaths to process |
| `--rm-zipfile` | `false` | Delete the zip after processing |
| `--rm-folder` | `false` | Delete the unzipped folder after processing |
| `--reprocess` | `false` | Re-process even if a `.done` marker exists |
| `--zip-list` | (all) | Process only the named zip files |
| `--verbose` | `false` | Set logging to DEBUG |

**Example:**

```bash
s1proc stack --polarization vv --subswath-list 1 2 --verbose
```

---

## Step 5 — Multilook amplitude images

```bash
s1proc amp
```

**What it does** (see `s1proc.coherence.run_multilook_amp` →
`multilook_amp`):

Reads all `.gslc` files from `io.slc_path`, groups them by acquisition date,
multilooks each group (applying the `rowlook` and `collook` factors from
`config.yaml`), and writes a single `.amp` file per date. The multilooking
reduces speckle and produces a human-viewable amplitude image.

**Files created:**

| File | Description |
|---|---|
| `amp/YYYYMMDD.amp` | Multilooked amplitude image per date (float32) |

**Example:**

```bash
s1proc amp
```

The look factors are set in `config.yaml`:

```yaml
proc:
    rowlook: 10
    collook: 20
```

---

## Step 6 — Check data integrity

```bash
s1proc integrity
```

**What it does** (see `s1proc.utils.run_check_integrity` →
`check_integrity`):

Counts the number of non-zero pixels in every amplitude image. Images whose
non-zero pixel count deviates from the median by more than `max_deviation`
(default 5%) are flagged as incomplete. This is **particularly helpful for
West Texas cases** where Sentinel-1 scenes often have missing bursts that
produce blank stripes in the imagery.

It also writes a boolean **mask file** (`io.mask_file`, default
`dem/mask.bin`) where `1` marks pixels that are consistently valid across
the stack and `0` marks pixels that are always nodata.

**Files created:**

| File | Description |
|---|---|
| `incomplete_date.txt` | List of dates with significant data loss |
| `dem/mask.bin` | Boolean mask (1 = valid pixel, 0 = invalid) |

**Key options:**

| Option | Default | Description |
|---|---|---|
| `--max-deviation` | `0.05` | Fractional deviation threshold (0.05 = 5%) |
| `--outfile` | `incomplete_date.txt` | Output file for bad dates |
| `--movedata` | `false` | If set, move bad files to `--out-dir` |
| `--out-dir` | `incomplete` | Destination for moved files |

**Example:**

```bash
s1proc integrity --max-deviation 0.03 --movedata
```

If `--movedata` is used, the bad scenes' `.amp`, `.gslc`, `.int`, and
`.unw` files are moved into `incomplete/` so they do not contaminate
downstream processing.

---

## Step 7 — Generate the interferogram pair list

```bash
s1proc slcpairs
```

**What it does** (see `s1proc.utils.run_create_slc_pair_list` →
`create_slc_pair_list`):

Scans all geocoded SLC files in `io.slc_path`, extracts the unique
acquisition dates, and enumerates all date pairs that fall within the
temporal and spatial baseline thresholds defined in `config.yaml`. For each
candidate pair it estimates the perpendicular baseline via orbit geometry
(`s1proc.utils.estimatebaseline`).

The result is written to `io.ifg_path / io.img_pair_file` (default:
`igrams/subswath_list`). Each line contains:

```
ref_date sec_date temporal_baseline_days perpendicular_baseline_m
```

**Files created:**

| File | Description |
|---|---|
| `igrams/subswath_list` | Interferogram pair list (text) |

**Configuration (in `config.yaml`):**

```yaml
proc:
    min_tbl: 6       # minimum temporal baseline (days)
    max_tbl: 360     # maximum temporal baseline (days)
    min_sbl: 0       # minimum spatial baseline (m)
    max_sbl: 300     # maximum spatial baseline (m)
```

**Example:**

```bash
s1proc slcpairs
```

---

## Step 8 — Form interferograms

```bash
s1proc interfere
```

**What it does** (see `s1proc.interfere.run_interfere` → `interfere`):

This is the main GPU-accelerated step. It:

1. Reads the pair list and loads the burst groups for each date.
2. Matches corresponding bursts between reference and secondary images
   (`s1proc.interfere.match_bursts`).
3. Launches the `crossmul_daemon` CUDA executable, a long-running GPU
   process that computes cross-multiplication (interferogram formation) for
   each burst pair.
4. After all burst-pair interferograms are computed, **stitches** them
   across subswaths into full-scene interferograms
   (`s1proc.interfere.stitch`).

The daemon supports automatic hardware tuning — leave `io_workers`,
`cpu_workers`, `gpu_workers`, `streams_per_gpu`, and `max_slots` blank in
the config to let it detect your GPU count, VRAM, and CPU core count.

**Files created:**

| File | Description |
|---|---|
| `igrams/YYYYMMDD_YYYYMMDD.int` | Stitched wrapped interferogram (complex64) |
| `dem/multilook.rsc` | Multilooked RSC file |
| `crossmul_daemon_stderr.log` | Daemon error log (in working directory) |

**Example:**

```bash
s1proc interfere --verbose
```

---

## Step 9 — Compute InSAR phase coherence

```bash
s1proc coh
```

**What it does** (see `s1proc.coherence.run_coherence` → `coherence`):

First ensures multilooked amplitude images exist (runs the same logic as
`s1proc amp` if needed). Then, for each interferogram, computes the
correlation coefficient between the reference and secondary amplitude images
and the interferometric phase. The coherence is written as a complex64 file
where the real part holds the amplitude product and the imaginary part holds
the correlation.

**Files created:**

| File | Description |
|---|---|
| `igrams/YYYYMMDD_YYYYMMDD.cc` | Coherence file per interferogram (complex64) |

**Example:**

```bash
s1proc coh
```

---

## Experimental steps

The following commands are implemented but have **not been thoroughly
tested**. Use with caution and expect rough edges.

---

## Step 10 — Phase correction (experimental)

```bash
s1proc phasecorr
```

**What it does** (see `s1proc.phase_correction.phase_correction`):

Two optional corrections, each gated by a boolean in `config.yaml`:

1. **Tropospheric delay correction** (`tropo.enable: true`):
   - Downloads ERA5 atmospheric reanalysis data via `pyaps3`.
   - Projects the tropospheric delay into the radar line-of-sight for each
     interferogram.
   - Subtracts the delay from the wrapped phase
     (`s1proc.tropo._era5_correction`).
   - Output files land in `io.ifg_corr_path` (default: `ifg_corrected/`).

2. **Goldstein filtering** (`filter.enable: true`):
   - Applies the Goldstein adaptive phase filter to reduce phase noise.
   - Uses the `goldstein` CUDA executable in `s1proc/bin/`.

**Files created (if tropo enabled):**

| File | Description |
|---|---|
| `ifg_corrected/*.int` | Tropospheric-corrected interferograms |
| `tropo_delay/` | Per-date tropospheric delay maps |

**Configuration:**

```yaml
tropo:
    enable: true
    method: "era5"
filter:
    enable: false
    method: "goldstein"
    parameters:
        window_size: 32
        goldstein_alpha: 0.5
```

**Example:**

```bash
s1proc phasecorr --verbose
```

---

## Step 11 — Phase unwrapping (experimental)

```bash
s1proc unwrap
```

**What it does** (see `s1proc.unwrap.batch_unwrap`):

Unwraps each wrapped interferogram to recover the absolute phase. Two
unwrapping backends are supported, selected via `unwrap.method` in
`config.yaml`:

- **`whirlwind`** (default, recommended): A fast Rust-based 2D phase unwrapper
  ([whirlwind-insar](https://github.com/scottstanie/whirlwind-insar)).
  Whirlwind achieves agreement with SNAPHU on 2π ambiguities with
  significantly lower runtime. It returns both unwrapped phase and
  connected-component labels, and handles multiple I/O formats (GeoTIFF,
  snaphu-style complex64 `.int`, ROI_PAC, ISCE2, GAMMA).

  **Users must install whirlwind separately** and place the executable (or
  a symlink to it) in `s1proc/bin/`. Installation options:

  - **pip**: `pip install whirlwind-insar`
  - **conda-forge**: `conda install -c conda-forge whirlwind-insar`
  - **Prebuilt binary**: download the standalone executable from
    [GitHub Releases](https://github.com/scottstanie/whirlwind-insar/releases)
    (no Python or Rust toolchain required)

- **`snaphu`**: The classic SNAPHU unwrapper. Supports tiled unwrapping with
  configurable overlap and per-tile parallelism. Managed by
  `s1proc.unwrap.TaskScheduler` to avoid CPU over-subscription.

  **SNAPHU source code is included in the package** for both Linux and
  Windows (the Windows version is a Claude-adapted port). The `snaphu`
  executable is compiled automatically when the package is installed via
  `pip install -e .`, so it is always available as a fallback. If you find
  that whirlwind is unavailable on your system (e.g., on CentOS 7), you can
  switch to SNAPHU by editing `config.yaml`:

  ```yaml
  unwrap:
      method: "snaphu"
  ```

The unwrapper reads interferograms from `io.ifg_path` (or
`io.ifg_corr_path` if phase correction was run) and correlation files
(`.cc`) from the same directory. Output files are written to
`io.unw_path` (default: `unw/`).

**Files created:**

| File | Description |
|---|---|
| `unw/*.unw` | Unwrapped interferogram (float32 phase only, or complex64) |

**Configuration:**

```yaml
unwrap:
    method: "whirlwind"
    parameters:
        only_save_phase: true   # write float32 phase only (saves disk space)
        # whirlwind-specific
        conncomp: false
        bridge: false
        # snaphu-specific
        cost_mode: "smooth"     # smooth, topo, or defo
        rowtile: null           # auto-computed if null
        coltile: null
        rowoverlap: 200
        coloverlap: 200
        tile_nproc: null
```

**Example:**

```bash
s1proc unwrap --cc-path igrams --ifg-path ifg_corrected --verbose
```

---

## Step 12 — Time series analysis (experimental)

```bash
s1proc timeseries
```

**What it does** (see `s1proc.time_series.run_time_series`):

Solves for surface deformation over time using GPU-accelerated SBAS (Small
Baseline Subset) inversion via CuPy and dask. Five solver methods are
available, selected by `timeseries.method` in `config.yaml`:

| Method | Output | Description |
|---|---|---|
| `stack` | 2D velocity | Simple weighted velocity stacking |
| `sbas_linear` | 2D velocity | SBAS with constant-velocity model |
| `sbas_seasonal` | 3D time series | SBAS with trend + seasonal harmonics |
| `sbas_ls` | 3D time series | SBAS L2 least-squares per-interval velocity |
| `sbas_l1` | 3D time series | SBAS L1-norm (LAD) inversion via ADMM |

All methods support MAD-based outlier removal (`mad_scalar`) and Tikhonov
regularisation. The L1 solver uses the ADMM algorithm following
[Boyd et al. (2010)](https://web.stanford.edu/~boyd/papers/admm/).

**Key configuration parameters:**

```yaml
timeseries:
    method: "sbas_l1"
    parameters:
        ref_lon: -123.0       # longitude of reference point (required)
        ref_lat: 37.5         # latitude of reference point (required)
        mad_scalar: 4         # MAD outlier threshold (0 = disabled)
        seasonal_terms: 1     # number of harmonic pairs
        regularization: 0.001 # Tikhonov factor
        l1_rho: 0.4           # ADMM augmented Lagrangian parameter
        l1_alpha: 1.0         # ADMM over-relaxation
        l1_max_iter: 20       # ADMM iterations
```

Results are written as a [zarr](https://zarr.readthedocs.io/) store. For 2D
outputs a single `velocity` array is saved; for 3D outputs the store
contains `displacement` (3D time series), `cumulative_deformation` (final
time step), and `velocity` (mean LOS velocity in m/yr).

**Files created:**

| File | Description |
|---|---|
| `analysis/time_series.zarr/` | Zarr store with velocity and/or displacement |

**Example:**

```bash
s1proc timeseries
```

### Plotting utilities

The `s1proc.time_series` module also provides helper functions for
visualising results:

- `plot_velocity_map` — render a geocoded velocity or displacement map as a
  PNG/PDF.
- `plot_time_series_at_points` — extract and plot displacement time series
  at specific pixel locations.
- `plot_time_series_map` — panel plot of cumulative displacement at multiple
  dates.

These are Python functions, not CLI commands — use them from a script or
notebook:

```python
from s1proc.time_series import plot_velocity_map, plot_time_series_at_points
import zarr
import numpy as np

store = zarr.open("analysis/time_series.zarr")
velocity = np.array(store["velocity"])

plot_velocity_map(
    velocity,
    rsc_file="dem/multilook.rsc",
    outfile="velocity.png",
    title="Mean LOS Velocity",
    cmap="RdBu_r",
)
```

---

## Configuration quick reference

The default `config.yaml` (written by `s1proc init`) contains commented
sections for every parameter. Key sections:

| Section | Controls |
|---|---|
| `io` | All input/output directory and file paths |
| `proc` | Multilooking factors, baseline thresholds, GPU/CPU worker counts |
| `filter` | Goldstein phase filtering |
| `tropo` | ERA5 tropospheric correction |
| `detrend` | Phase ramp removal |
| `unwrap` | Unwrapping method (whirlwind/snaphu) and parameters |
| `timeseries` | SBAS inversion method and parameters |
| `date` | Study period (`start`, `end`) |
| `area` | Study area (`bbox`, `flight_direction`, path/frame filtering) |

All paths in `io` are relative to the directory containing `config.yaml`.
This means you can freely move or copy a path folder without breaking
internal references.

---

## Typical directory layout after a full run

```
ascending/path_123/
├── config.yaml
├── roi.geojson
├── roi.metalink
├── roi_dem.tif
├── elevation.dem
├── elevation.dem.rsc
├── crossmul_daemon_stderr.log
├── incomplete_date.txt
├── data/                  # Sentinel-1 zip files
├── eof/                   # Precise orbit files
├── proc/                  # Intermediate files, .done markers, orbit timing
├── slc/                   # Geocoded per-burst SLCs (*.gslc)
├── amp/                   # Multilooked amplitude images (*.amp)
├── igrams/                # Wrapped interferograms (*.int) + coherence (*.cc)
├── dem/
│   ├── multilook.dem
│   ├── multilook.rsc
│   └── mask.bin
├── unw/                   # Unwrapped interferograms (*.unw)
├── ifg_corrected/         # Tropospheric-corrected interferograms
├── tropo_delay/           # ERA5 delay maps
├── geometry/              # LOS vectors, look angles
└── analysis/
    └── time_series.zarr/  # Deformation time series
```
