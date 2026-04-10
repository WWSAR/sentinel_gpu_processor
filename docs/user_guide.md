# Sentinel GPU Processor - User Guide

## Overview

The Sentinel GPU Processor is a command-line tool for processing Sentinel-1 SAR (Synthetic Aperture Radar) data to generate interferograms and perform phase unwrapping. It provides a suite of commands for a complete InSAR (Interferometric SAR) processing workflow.

## Available Commands

The tool provides four main subcommands for different processing stages:

1. **`stack`**  
   Process a Sentinel-1 data stack into geocoded SLCs (Single Look Complex images).  

2. **`slcpairs`**  
   Generate a list of synchronized SLC image pairs.

3. **`interfere`**  
   Create interferograms from paired SLC images using multilooking.

4. **`unwrap`**  
   Unwrap phase using SNAPHU with parallel-supported tiling.

---

## 1. Command Details: `stack`

### Description
Processes Sentinel-1 files into coregistered, geocoded SLC images.

**Example Usage:**
```bash
s1proc stack [-h] [STACK OPTIONS]

Process a stack of sentinel products to coregistered geocoded SLCS

╭─ options ──────────────────────────────────────────────────────────────────────────────────╮
│ -h, --help      show this help message and exit                                            │
│ --data-dir STR  Data folder of Sentinel-1 zipfiles (default: .)                            │
│ --eof-dir STR   Data folder of precise orbit EOF files (default: .)                        │
│ --proc-dir STR  Data folder to store temporary files (default: proc)                       │
│ --slc-dir STR   Data folder to store geocoded SLCs (default: slc)                          │
│ --demfile STR   DEM file (default: elevation.dem)                                          │
│ --rscfile STR   rsc file (default: elevation.dem.rsc)                                      │
│ --polarization {hh,hv,vh,vv}                                                               │
│                 Polarization to process (default: vv)                                      │
│ --subswath-list [INT [INT ...]]                                                            │
│                 Subswaths to process (default: 1 2 3)                                      │
│ --rm-zipfile, --no-rm-zipfile                                                              │
│                 Remove the zipfile after image processing is done (default: False)         │
│ --rm-folder, --no-rm-folder                                                                │
│                 Remove the unzipped folder after image processing is done (default: False) │
│ --reprocess, --no-reprocess                                                                │
│                 Reprocess the geo file if it already exists (default: False)               │
╰────────────────────────────────────────────────────────────────────────────────────────────╯
```

## 2. Command Details: `slcpairs`

### Description
Generate a list of SLC image pairs for interferogram generation based on temporal and spatial baseline thresholds.

**Example Usage:**
```bash
s1proc slcpairs [-h] [SLCPAIRS OPTIONS]

Create a list of SLC pairs for interferogram generation

╭─ options ─────────────────────────────────────────────────────────────╮
│ -h, --help      show this help message and exit                       │
│ --min-tbl INT   minimum temporal baseline threshold (default: 0)      │
│ --max-tbl INT   maximum temporal baseline threshold (default: 30000)  │
│ --min-sbl INT   minimum temporal baseline threshold (default: 0)      │
│ --max-sbl INT   maximum temporal baseline threshold (default: 10000)  │
│ --slc-dir STR   SLC directory (default: slc)                          │
│ --proc-dir STR  Directory storing auxilary parameters (default: proc) │
│ --ifg-dir STR   Directory storing interferograms (default: igrams)    │
│ --demfile STR   DEM file (default: elevation.dem)                     │
│ --rscfile STR   rsc file (default: elevation.dem.rsc)                 │
╰───────────────────────────────────────────────────────────────────────╯
```

## 3. Command Details: `interfere`

### Description
Performs the interference analysis on image pairs.

**Example Usage:**
```bash
s1proc interfere [-h] STR STR [--ifg-dir STR] [--rowlook INT] [--collook INT]

Form interferograms from a subswath list

╭─ positional arguments ─────────────────────────────────────────────╮
│ STR            File containing pairs of subswath images (required) │
│ STR            rsc file (required)                                 │
╰────────────────────────────────────────────────────────────────────╯
╭─ options ──────────────────────────────────────────────────────────╮
│ -h, --help     show this help message and exit                     │
│ --ifg-dir STR  Directory to save interferograms (default: igrams)  │
│ --rowlook INT  Number of look in row direction (default: 1)        │
│ --collook INT  Number of look in column direction (default: 1)     │
╰────────────────────────────────────────────────────────────────────╯
```

## 4. Command Details: `unwrap`

### Description
Unwrap interferograms.

**Example Usage:**
```bash
s1proc unwrap [-h] [UNWRAP OPTIONS]

Batch unwrap using SNAPHU

╭─ options ──────────────────────────────────────────────────────────────────────────╮
│ -h, --help            show this help message and exit                              │
│ --input-folder STR    Folder containing wrapped interferograms (required)          │
│ --output-folder STR   Folder to save unwrapped results (required)                  │
│ --rsc-file STR        Path to .rsc file for image width (default: dem.rsc)         │
│ --rowtile INT         Number of tiles in row direction (default: 1)                │
│ --coltile INT         Number of tiles in column direction (default: 1)             │
│ --rowoverlap INT      Overlap in row direction (default: 200)                      │
│ --coloverlap INT      Overlap in column direction (default: 200)                   │
│ --nproc INT           Number of parallel processes (default: 1)                    │
│ --file-extension STR  File extension filter (default: .int) (default: .int)        │
│ --cost-mode STR       SNAPHU cost mode: 'DEFO', 'SMOOTH', 'TOPO' (default: SMOOTH) │
╰────────────────────────────────────────────────────────────────────────────────────╯
```
