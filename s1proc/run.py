"""
End-to-end Sentinel-1 InSAR processing workflow.

Provides :func:`run` to execute the full pipeline — from preprocessing
through time-series analysis — in a single command.  Each stage reads
the same YAML configuration file and can be individually enabled or
disabled via boolean flags.
"""

from __future__ import annotations

from pathlib import Path

from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")


def run(
    config: str | Path = "config.yaml",
    *,
    preproc: bool = True,
    stack: bool = True,
    amp: bool = True,
    integrity: bool = True,
    slcpairs: bool = True,
    interfere: bool = True,
    coh: bool = True,
    phasecorr: bool = True,
    unwrap: bool = True,
    timeseries: bool = True,
    verbose: bool = False,
) -> None:
    """
    Run the complete Sentinel-1 InSAR processing workflow.

    Parameters
    ----------
    config : str or Path
        Path to the YAML configuration file (default ``"config.yaml"``).
    preproc : bool
        Run preprocessing: filter GeoJSON, generate metalink, download
        DEM / Sentinel-1 zip files / precise orbit files.
    stack : bool
        Run SLC stacking: geocode every Sentinel-1 scene to per-burst
        geocoded SLCs (``.gslc``).
    amp : bool
        Run amplitude multilooking: produce one ``.amp`` file per date.
    integrity : bool
        Check data integrity by flagging scenes whose non-zero pixel
        count deviates from the stack median.
    slcpairs : bool
        Generate the interferogram pair list using temporal and spatial
        baseline thresholds from the configuration.
    interfere : bool
        Form wrapped interferograms via the GPU-accelerated
        ``crossmul_daemon``.
    coh : bool
        Compute InSAR phase coherence for every interferogram.
    phasecorr : bool
        Apply phase corrections (tropospheric delay and/or Goldstein /
        EigenSAR filtering), gated by ``tropo.enable`` and
        ``filter.enable`` in the configuration.
    unwrap : bool
        Unwrap interferograms using the backend specified by
        ``unwrap.method`` (``"whirlwind"`` or ``"snaphu"``).
    timeseries : bool
        Run SBAS time-series inversion via the method specified by
        ``timeseries.method``.
    verbose : bool
        Forwarded to individual steps that support debug logging.

    Notes
    -----
    Each stage is **idempotent** in the sense that it skips outputs that
    already exist on disk (e.g. ``.done`` markers, existing ``.int`` or
    ``.unw`` files).  Re-running the full workflow is therefore safe and
    picks up where it left off.

    Examples
    --------
    Run everything from a single config file:

    >>> from s1proc.run import run
    >>> run("ascending/path_123/config.yaml")

    Skip preprocessing because data is already on disk:

    >>> run("ascending/path_123/config.yaml", preproc=False)

    Run only the interferogram-formation and downstream steps:

    >>> run("ascending/path_123/config.yaml",
    ...     preproc=False, stack=False, amp=False,
    ...     integrity=False, slcpairs=False)
    """
    # ------------------------------------------------------------------
    # 0.  Load configuration
    # ------------------------------------------------------------------

    config = str(config)
    logger.info("Loaded configuration from %s", config)

    # ------------------------------------------------------------------
    # 1.  Preprocessing
    # ------------------------------------------------------------------
    if preproc:
        logger.info("=" * 60)
        logger.info("Step 1/10 — Preprocessing")
        logger.info("=" * 60)
        from s1proc.preproc import preprocess

        preprocess(config_file=config)
    else:
        logger.info("Preprocessing skipped (preproc=False).")

    # ------------------------------------------------------------------
    # 2.  SLC stacking (geocode every scene)
    # ------------------------------------------------------------------
    if stack:
        logger.info("=" * 60)
        logger.info("Step 2/10 — SLC stacking")
        logger.info("=" * 60)
        from s1proc.sentinel_stack import run_stack

        run_stack(
            config=config,
            verbose=verbose,
        )
    else:
        logger.info("SLC stacking skipped (stack=False).")

    # ------------------------------------------------------------------
    # 3.  Amplitude multilooking
    # ------------------------------------------------------------------
    if amp:
        logger.info("=" * 60)
        logger.info("Step 3/10 — Amplitude multilooking")
        logger.info("=" * 60)
        from s1proc.coherence import run_multilook_amp

        run_multilook_amp(config=config)
    else:
        logger.info("Amplitude multilooking skipped (amp=False).")

    # ------------------------------------------------------------------
    # 4.  Data integrity check
    # ------------------------------------------------------------------
    if integrity:
        logger.info("=" * 60)
        logger.info("Step 4/10 — Data integrity check")
        logger.info("=" * 60)
        from s1proc.utils import run_check_integrity

        run_check_integrity(config=config)
    else:
        logger.info("Data integrity check skipped (integrity=False).")

    # ------------------------------------------------------------------
    # 5.  SLC pair-list generation
    # ------------------------------------------------------------------
    if slcpairs:
        logger.info("=" * 60)
        logger.info("Step 5/10 — SLC pair list generation")
        logger.info("=" * 60)
        from s1proc.utils import run_create_slc_pair_list

        run_create_slc_pair_list(config=config)
    else:
        logger.info("SLC pair-list generation skipped (slcpairs=False).")

    # ------------------------------------------------------------------
    # 6.  Interferogram formation (GPU)
    # ------------------------------------------------------------------
    if interfere:
        logger.info("=" * 60)
        logger.info("Step 6/10 — Interferogram formation")
        logger.info("=" * 60)
        from s1proc.interfere import run_interfere

        run_interfere(config=config, verbose=verbose)
    else:
        logger.info("Interferogram formation skipped (interfere=False).")

    # ------------------------------------------------------------------
    # 7.  Coherence computation
    # ------------------------------------------------------------------
    if coh:
        logger.info("=" * 60)
        logger.info("Step 7/10 — Coherence computation")
        logger.info("=" * 60)
        from s1proc.coherence import run_coherence

        run_coherence(config=config)
    else:
        logger.info("Coherence computation skipped (coh=False).")

    # ------------------------------------------------------------------
    # 8.  Phase correction
    # ------------------------------------------------------------------
    if phasecorr:
        logger.info("=" * 60)
        logger.info("Step 8/10 — Phase correction")
        logger.info("=" * 60)
        from s1proc.phase_correction import phase_correction

        phase_correction(config=config, verbose=verbose)
    else:
        logger.info("Phase correction skipped (phasecorr=False).")

    # ------------------------------------------------------------------
    # 9.  Phase unwrapping
    # ------------------------------------------------------------------
    if unwrap:
        logger.info("=" * 60)
        logger.info("Step 9/10 — Phase unwrapping")
        logger.info("=" * 60)
        from s1proc.unwrap import batch_unwrap

        batch_unwrap(config=config, verbose=verbose)
    else:
        logger.info("Phase unwrapping skipped (unwrap=False).")

    # ------------------------------------------------------------------
    # 10. Time-series analysis (GPU)
    # ------------------------------------------------------------------
    if timeseries:
        logger.info("=" * 60)
        logger.info("Step 10/10 — Time-series analysis")
        logger.info("=" * 60)
        from s1proc.time_series import run_time_series

        run_time_series(config=config)
    else:
        logger.info("Time-series analysis skipped (timeseries=False).")

    logger.info("=" * 60)
    logger.info("Pipeline finished.")
    logger.info("=" * 60)
