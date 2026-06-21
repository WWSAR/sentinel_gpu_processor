# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Python Coding & Editing Guidelines

## Table of Contents

1. Philosophy
1. Docstrings & Comments
1. Type Hints
1. Documentation

---

## Philosophy

- **Readability, reproducibility, performance – in that order.**
- Prefer explicit over implicit; avoid hidden state and global flags.
- Measure before you optimize (`time.perf_counter`, `line_profiler`).
- Each module holds a **single responsibility**; keep public APIs minimal.

## Docstrings & Comments

- Style: NumPyDoc.
- Start with a one‑sentence summary in the imperative mood.
- Sections: Parameters, Returns, Raises, Examples, References.
- Use backticks for code or referring to variables (e.g. `xarray.DataArray`).
- Do not use emojis, or non-unicode characters in comments/print statements.
- Cite peer‑reviewed papers with DOI links when relevant.
- Write code that explains itself rather than needs comments.
- For the inline you do add, explain *why*, not what. For example, *don't* write:

```python
# open the file
f = open(filename)
```

- The comments should be things which are not obvious to a reader with typical background knowledge.

## Tools

- ruff is use for most code maintenance, black for formatting, mypy for type checking, pytest for testing
- You can run `pre-commit run -a` to run all pre-commit hooks and check for style violations

## Code Style

- Annotate all public functions (PEP 484).
- Prefer `Protocol` over `ABC`s when only an interface is needed.
- Validate external inputs via Pydantic models (if existing); otherwise, use `dataclasses`
- Parse, don't validate, with your dataclasses. Checks should be at the serialization boundaries, not scattered everywhere in the code.
- If you need to add an ignore, ignore a specific check like # type: ignore[specific]
- Don't write error handing code or smooth over exceptions/errors unless they are expected as part of control flow.
- In general, write code that will raise an exception early if something isn't expected.
- Enforce important expectations with asserts, but raise errors for user-facing problems.

## Documentation

- mkdocs + Jupyter. Hosted on ReadTheDocs.
- Auto API from type hints.
- Provide tutorial notebooks covering common workflows.
- Include examples in docstrings.
- Add high-level guides for key functionality.

## Project Overview

`s1proc` is a GPU-accelerated Sentinel-1 InSAR processing pipeline. The Python package (`s1proc/`) orchestrates a workflow that calls CUDA C++ executables for heavy computation. The package is distributed as a conda package via rattler-build.

**Package name on PyPI/conda**: `s1proc`
**CLI entry point**: `s1proc` → `s1proc.cli:main`

## Build & Development Commands

### Full conda build (production)
```bash
# Windows
bld.bat

# Linux
./build.sh
```
These scripts: (1) compile CUDA/C++ code via CMake+Ninja into `s1proc/bin/`, (2) copy external deps from `extern/`, (3) `pip install .`

### C/CUDA compilation only
```bash
cmake -S csrc -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=$CONDA_PREFIX
cmake --build build
```
Requires: CUDA Toolkit 12.4, libtiff, sqlite3, cmake, ninja. On Linux, SNAPHU is also compiled from `csrc/snaphu/`.

### Python package only (editable install)
```bash
pip install -e .
```
Use this for iterating on Python code when CUDA binaries are already built.

### Conda package via rattler-build
```bash
rattler-build build --recipe recipe.yaml -c nvidia -c conda-forge
```

### Running the CLI
```bash
s1proc --help           # list all subcommands
s1proc init             # create default config.yaml
s1proc stack            # process Sentinel-1 zip stack to geocoded SLCs
s1proc slcpairs         # generate SLC pair list
s1proc interfere        # form interferograms
s1proc unwrap           # phase unwrapping via SNAPHU
s1proc phasecorr        # tropospheric correction + filtering
s1proc coh              # compute coherence (also runs multilook amplitude)
s1proc amp              # multilook amplitude only
s1proc integrity        # check data completeness
s1proc query            # query ASF for Sentinel-1 data
```

## Architecture

### Two-layer design

1. **Python orchestration** (`s1proc/`) — CLI, I/O, configuration, workflow control, orbit math, and light preprocessing. Python code never touches the GPU directly.
2. **CUDA C++ executables** (`csrc/src/`) — compiled binaries installed into `s1proc/bin/` at build time. Called via `subprocess`/`os.system` from Python. Each is a standalone program.

### Processing pipeline (InSAR workflow)

The standard workflow follows these stages:

1. **`init`** → Write `config.yaml` from `s1proc/config/default.yaml`
2. **`query`** → Download Sentinel-1 metadata from ASF (Alaska Satellite Facility API)
3. **`stack`** → For each Sentinel-1 zip: extract GeoTIFF → deramp bursts → geocode to lat/lon grid → output `.gslc` files per subswath. Calls `readgeotiff`, `deramp_burst`, `geo2rdr_reramp`.
4. **`slcpairs`** → Pair SLCs by temporal/spatial baseline thresholds, write pair list
5. **`interfere`** → Cross-multiply paired SLCs → burst stitching with phase offset correction → output `.int` interferograms. Calls `crossmul`.
6. **`phasecorr`** → Tropospheric correction (ERA5 via pyaps3) → optional Goldstein filtering (calls `goldstein`). Outputs to `ifg_corrected/`.
7. **`unwrap`** → Phase unwrapping via SNAPHU with tiling and parallel execution (`WorkstationTaskScheduler` using `ThreadPoolExecutor`)
8. **`coh`** / **`amp`** → Amplitude multilooking and coherence computation

### CUDA executables (`csrc/src/`)

| Executable | Source | Purpose |
|---|---|---|
| `readgeotiff` | `readgeotiff.cpp` | Extract GeoTIFF to raw floating-point SLC |
| `deramp_burst` | `deramp_burst.cu` + `orbit.cu` + `sario.cu` + `sql_mod.cpp` | Deramp TOPS burst SLC data |
| `geo2rdr_reramp` | `geo2rdr_reramp.cu` + `orbit.cu` + `sario.cu` + `bounds.cu` + `sql_mod.cpp` | Geocode (DEM→radar coordinates) and reramp |
| `crossmul` | `crossmul.cu` + `sario.cu` | Complex cross-multiplication for interferogram formation |
| `phase_similarity` | `phase_similarity.cu` | Phase similarity for PS (Persistent Scatterer) selection |
| `goldstein` | `goldstein.cu` | Goldstein adaptive phase filtering (requires cuFFT) |

CUDA architectures targeted: sm_75, sm_80, sm_89, sm_90.

### External binaries (`extern/`)

- `snaphu` (Linux: compiled from `csrc/snaphu/`; Windows: pre-built `snaphu.exe`) — SNAPHU phase unwrapping
- `aria2c` — parallel download utility

### Key Python modules

| Module | Role |
|---|---|
| `cli.py` | Tyro-based CLI with subcommands |
| `_config.py` | Dataclass config hierarchy (`S1Config` → `IoConfig`, `ProcessingConfig`, `FilteringConfig`, `TroposphericConfig`, `DetrendingConfig`). Loaded from YAML via `dacite`. |
| `sentinel_scene.py` | Core SLC processing: unzip → extract GeoTIFF → deramp → geocode for one Sentinel-1 scene |
| `sentinel_stack.py` | Batch processing of many Sentinel-1 scenes |
| `interfere.py` | Interferogram formation with burst matching and phase stitching |
| `unwrap.py` | SNAPHU batch unwrapping with CPU-aware parallel scheduling |
| `sario.py` | SAR I/O: `CroppedImage` (tiled image with header), `Subswath`, `BurstGroup`, file read/write utilities |
| `orbit.py` | Numba-JIT orbit interpolation (Hermite polynomials), zero-Doppler time computation |
| `geocoordinates.py` | `GeoCoordinates` class for lat/lon ↔ pixel transforms, RSC file I/O, multilooking |
| `geometry.py` | Numba-JIT coordinate transforms: llh↔xyz, TCN basis, ellipsoidal geodesics |
| `precise_orbit.py` | Parse ESA precise orbit EOF files into orbtiming format |
| `sentinel_roidb.py` | Parse Sentinel-1 XML annotation into SQLite metadata databases |
| `sql_mod.py` | SQLite helper for the parameter database used by CUDA executables |
| `tropo.py` | ERA5 tropospheric delay correction via pyaps3 |
| `coherence.py` | Multilooking and coherence computation |
| `utils.py` | SLC pair list generation, baseline estimation, LOS calculation, data integrity checks |
| `phase_correction.py` | Orchestrates tropospheric correction + Goldstein filtering |
| `psps.py` | Persistent scatterer selection |
| `query.py` | ASF data discovery API client |
| `_log.py` | Logging setup via `logging.config.dictConfig` |

### Configuration system

The entire pipeline is driven by a single `config.yaml`. The schema is defined by dataclasses in `_config.py` and parsed strictly via `dacite.from_dict`. The default template lives at `s1proc/config/default.yaml`. CLI subcommands that need config accept `--config config.yaml`.

### Data formats

- **SLC**: Complex float32 raw binary, interleaved real/imaginary. Cropped images have a 64-int32 header (`CroppedImage`).
- **Interferograms**: `.int` files — complex64 or float32 raw binary
- **Amplitude**: `.amp` files — float32 raw binary
- **RSC files**: Key-value text files defining geo grid (WIDTH, FILE_LENGTH, X_FIRST, Y_FIRST, X_STEP, Y_STEP)
- **DEM**: int16 raw binary + `.rsc` description
- **Orbit timing**: Text files with state vector count followed by `time x y z vx vy vz` lines
- **SQLite DBs**: Parameter databases consumed by CUDA executables (`deramp_burst`, `geo2rdr_reramp`)

## CI

Two GitHub Actions workflows:
- **`rattler_build.yml`**: Cross-platform conda package build (Windows + Linux) via rattler-build. No GPU tests (GitHub runners lack GPUs).
- **`wheel_build.yml`**: Python wheel build using micromamba for dependencies.

## Platform Notes

- **Windows**: CUDA executables have `.exe` extension. `get_bin_path()` in `__init__.py` handles this automatically. MSVC compiler required.
- **Linux**: SNAPHU is compiled from source. Uses GCC. `_GLIBCXX_USE_CXX11_ABI=1` is set.
- CUDA toolkit 12.4 required for both platforms.
