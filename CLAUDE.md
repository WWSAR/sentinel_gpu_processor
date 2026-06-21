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

### Key Python modules

| Module | Role |
|---|---|
| `cli.py` | Tyro-based CLI with subcommands |
| `_config.py` | Dataclass config hierarchy,  Loaded from YAML via `dacite`, query Sentinel-1 data and divide them based on paths, configure a subfolder for each path |
| `sentinel_scene.py` | Core SLC processing: unzip → extract GeoTIFF → deramp → geocode for one Sentinel-1 scene |
| `sentinel_stack.py` | Batch processing of many Sentinel-1 scenes |
| `interfere.py` | Interferogram formation with burst matching and phase stitching |
| `unwrap.py` | SNAPHU batch unwrapping with CPU-aware parallel scheduling |
| `sario.py` | SAR I/O: `CroppedImage` (tiled image with header), `Subswath`, `BurstGroup`, file read/write utilities |
| `orbit.py` | Numba-JIT orbit interpolation (Hermite polynomials), zero-Doppler time computation |
| `geocoordinates.py` | `GeoCoordinates` class for lat/lon ↔ pixel transforms, RSC file I/O, multilooking |
| `geometry.py` | Numba-JIT coordinate transforms: llh↔xyz, TCN basis, ellipsoidal geodesics |
| `tropo.py` | ERA5 tropospheric delay correction via pyaps3 |
| `coherence.py` | Multilooking and coherence computation |
| `utils.py` | SLC pair list generation, baseline estimation, LOS calculation, data integrity checks, geotiff and ROI_PAC conversion |
| `phase_correction.py` | Orchestrates tropospheric correction + Goldstein filtering |
| `query.py` | ASF data discovery API client |
| `_log.py` | Logging setup via `logging.config.dictConfig` |
| `preproc.py` | generate metalinks for Sentinel-1 data downloading, download DEM |
