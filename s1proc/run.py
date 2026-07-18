"""
End-to-end Sentinel-1 InSAR processing workflow with resume support.

Provides :class:`Pipeline` — a resumable multi-stage processor with
marker-file state tracking, output validation, and configuration
fingerprinting — and :func:`run` as the CLI entry point.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")

# ---------------------------------------------------------------------------
# Config sections hashed for fingerprinting (everything except IO paths).
# Area and date are excluded because they are discovery parameters, not
# processing parameters — changing them does not invalidate results.
# ---------------------------------------------------------------------------
_PARAMETER_SECTIONS = (
    "proc",
    "filter",
    "tropo",
    "detrend",
    "unwrap",
    "timeseries",
)


def _compute_config_fingerprint(raw_config: dict) -> str:
    """Compute a stable SHA-256 fingerprint of processing-parameter sections.

    Only sections listed in :data:`_PARAMETER_SECTIONS` contribute to the
    hash.  IO paths, area, and date are deliberately excluded so that
    moving data directories or adjusting the study period does not
    invalidate cached results.

    Parameters
    ----------
    raw_config : dict
        The raw YAML configuration as returned by :func:`yaml.safe_load`.

    Returns
    -------
    str
        Hexadecimal SHA-256 digest.
    """
    params = {}
    for key in _PARAMETER_SECTIONS:
        if key in raw_config:
            params[key] = raw_config[key]
    serialized = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Stage definition
# ---------------------------------------------------------------------------


@dataclass
class Stage:
    """Definition of a single pipeline stage.

    Attributes
    ----------
    name : str
        Human-readable stage name used in log messages and marker filenames.
    fn : Callable
        The callable that executes the stage.  It receives ``**kwargs``.
    kwargs : dict
        Keyword arguments forwarded to *fn*.
    output_patterns : list of str
        Glob patterns (relative to the current working directory) that
        must match at least one non-empty file for the stage to be
        considered complete.
    enabled : bool
        If *False* the stage is skipped entirely.
    """

    name: str
    fn: Callable[..., None]
    kwargs: Dict[str, Any] = field(default_factory=dict)
    output_patterns: List[str] = field(default_factory=list)
    enabled: bool = True


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Pipeline:
    """A resumable multi-stage InSAR processing pipeline.

    Each stage is guarded by a ``.done`` marker file written to a state
    directory.  When ``resume`` is *True*, the pipeline validates three
    conditions before skipping a stage:

    1. **Marker file** — ``.stage_<name>.done`` must exist.
    2. **Output files** — every glob pattern in ``output_patterns`` must
       match at least one file with size > 0.
    3. **Configuration fingerprint** — the SHA-256 hash of the
       processing-parameter sections (``proc``, ``filter``, ``tropo``,
       ``detrend``, ``unwrap``, ``timeseries``) must match the hash
       stored in the marker.  IO paths, area, and date are intentionally
       excluded so that data moves do not force re-processing.

    If all three checks pass the stage is skipped; otherwise it is
    re-executed and a fresh marker is written on success.

    Parameters
    ----------
    config : str or Path
        Path to the YAML configuration file.
    state_dir : str or Path or None
        Directory where ``.done`` marker files are stored.  Defaults to
        ``<config_dir>/.pipeline_state/``.
    resume : bool
        If *True* (default), check markers and skip stages whose outputs
        are intact.  Set to *False* to force a full re-run.

    Examples
    --------
    Build a custom pipeline programmatically:

    >>> from s1proc.run import Pipeline, Stage
    >>> pipeline = Pipeline("config.yaml")
    >>> pipeline.add_stage(
    ...     name="preproc",
    ...     fn=my_preproc_func,
    ...     kwargs={"config_file": "config.yaml"},
    ...     output_patterns=["roi.metalink", "elevation.dem"],
    ... )
    >>> pipeline.run()
    """

    STATE_DIR_NAME = ".pipeline_state"

    def __init__(
        self,
        config: str | Path = "config.yaml",
        *,
        state_dir: str | Path | None = None,
        resume: bool = True,
    ) -> None:
        self._config_path = Path(config).resolve()
        self._config_root = self._config_path.parent
        self._resume = resume

        if state_dir is None:
            state_dir = self._config_root / self.STATE_DIR_NAME
        self._state_dir = Path(state_dir)

        self._stages: List[Stage] = []
        self._config_fingerprint: Optional[str] = None

        # Load the raw YAML tree once for fingerprinting.
        from ruamel.yaml import YAML

        yaml = YAML()
        yaml.default_flow_style = False
        with open(self._config_path, "r") as f:
            self._raw_config = yaml.load(f)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_stage(
        self,
        name: str,
        fn: Callable[..., None],
        kwargs: Dict[str, Any] | None = None,
        output_patterns: List[str] | None = None,
        enabled: bool = True,
    ) -> None:
        """Register a pipeline stage.

        Parameters
        ----------
        name : str
            Stage name (used in logs and marker filenames).
        fn : Callable
            Callable that executes the stage (receives ``**kwargs``).
        kwargs : dict or None
            Keyword arguments forwarded to *fn*.
        output_patterns : list of str or None
            Glob patterns that must match at least one non-empty file
            for the stage to be considered complete.
        enabled : bool
            If *False* the stage is skipped.
        """
        self._stages.append(
            Stage(
                name=name,
                fn=fn,
                kwargs=kwargs or {},
                output_patterns=output_patterns or [],
                enabled=enabled,
            )
        )

    def run(self) -> None:
        """Execute all enabled stages in registration order.

        Stages whose markers and outputs are valid are skipped when
        ``resume`` is *True*.
        """
        enabled = [s for s in self._stages if s.enabled]
        n_total = len(enabled)
        step = 0

        for stage in self._stages:
            if not stage.enabled:
                logger.info("Stage [%s] — disabled. Skipping.", stage.name)
                continue

            step += 1

            if self._resume and self._should_skip(stage):
                logger.info(
                    "Stage [%s] (%d/%d) — outputs found and verified. Skipping.",
                    stage.name,
                    step,
                    n_total,
                )
                continue

            logger.info("%s", "=" * 60)
            logger.info("Stage [%s] (%d/%d) — running", stage.name, step, n_total)
            logger.info("%s", "=" * 60)

            stage.fn(**stage.kwargs)

            self._write_marker(stage)
            logger.info("Stage [%s] — complete. Marker written.", stage.name)

    # ------------------------------------------------------------------
    # Marker-file helpers
    # ------------------------------------------------------------------

    def _marker_path(self, stage: Stage) -> Path:
        """Return the absolute path to the ``.done`` marker for *stage*."""
        return self._state_dir / f".stage_{stage.name}.done"

    def _write_marker(self, stage: Stage) -> None:
        """Persist a ``.done`` marker with metadata.

        The marker is a JSON file containing:

        - ``stage`` — stage name.
        - ``config_fingerprint`` — SHA-256 of processing-parameter sections.
        - ``timestamp`` — UNIX epoch when the marker was written.
        - ``outputs`` — sorted list of files matched by the stage's
          ``output_patterns`` at the time the marker was created.
        """
        self._state_dir.mkdir(parents=True, exist_ok=True)
        marker = self._marker_path(stage)

        resolved_outputs: List[str] = []
        for pattern in stage.output_patterns:
            resolved_outputs.extend(glob.glob(pattern))

        meta = {
            "stage": stage.name,
            "config_fingerprint": self.config_fingerprint,
            "timestamp": time.time(),
            "outputs": sorted(resolved_outputs),
        }
        with open(marker, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def _read_marker(self, stage: Stage) -> Optional[dict]:
        """Read and return the marker metadata for *stage*, or *None*."""
        marker = self._marker_path(stage)
        if not marker.exists():
            return None
        try:
            with open(marker, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    # ------------------------------------------------------------------
    # Resume / skip logic
    # ------------------------------------------------------------------

    @property
    def config_fingerprint(self) -> str:
        """Lazily-computed SHA-256 fingerprint of processing-parameter sections."""
        if self._config_fingerprint is None:
            self._config_fingerprint = _compute_config_fingerprint(self._raw_config)
        return self._config_fingerprint

    def _should_skip(self, stage: Stage) -> bool:
        """Return *True* if *stage* can be skipped based on cached artifacts.

        Performs three checks:

        1. Marker file exists and is readable.
        2. Every ``output_pattern`` matches at least one non-empty file.
        3. The stored config fingerprint matches the current config.
        """
        # (1) Marker file must exist and be readable.
        meta = self._read_marker(stage)
        if meta is None:
            logger.debug("Stage [%s]: no valid marker file.", stage.name)
            return False

        # (2) Expected outputs must exist on disk with size > 0.
        if not self._validate_outputs(stage):
            logger.debug(
                "Stage [%s]: output validation failed — "
                "some expected files are missing or empty.",
                stage.name,
            )
            return False

        # (3) Config fingerprint must match.
        stored_fp = meta.get("config_fingerprint")
        current_fp = self.config_fingerprint
        if stored_fp != current_fp:
            stored_short = stored_fp[:12] if stored_fp else "None"
            current_short = current_fp[:12]
            logger.info(
                "Stage [%s]: config fingerprint changed (%s... -> %s...). Re-running.",
                stage.name,
                stored_short,
                current_short,
            )
            return False

        return True

    def _validate_outputs(self, stage: Stage) -> bool:
        """Check that every output pattern matches at least one non-empty file.

        Patterns are evaluated with :func:`glob.glob` relative to the
        current working directory.  Directories matched by a pattern are
        accepted as-is (they are not recursed into).

        Returns
        -------
        bool
            *True* if all patterns match at least one non-empty file
            (or a directory), or if the stage declares no patterns.
        """
        if not stage.output_patterns:
            logger.debug(
                "Stage [%s]: no output patterns declared; trusting marker alone.",
                stage.name,
            )
            return True

        for pattern in stage.output_patterns:
            matches = glob.glob(pattern)
            if not matches:
                logger.debug(
                    "Stage [%s]: pattern '%s' matched no files.",
                    stage.name,
                    pattern,
                )
                return False

            # Require that at least one match is a non-empty file or a
            # non-empty directory (handles zarr stores).
            valid = False
            for fpath in matches:
                if os.path.isdir(fpath):
                    # Accept a non-empty directory (e.g. a zarr store).
                    if len(os.listdir(fpath)) > 0:
                        valid = True
                        break
                elif os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
                    valid = True
                    break
                else:
                    logger.debug(
                        "Stage [%s]: '%s' is empty or not a regular file.",
                        stage.name,
                        fpath,
                    )
            if not valid:
                logger.debug(
                    "Stage [%s]: pattern '%s' matched %d entries but "
                    "none are non-empty files/directories.",
                    stage.name,
                    pattern,
                    len(matches),
                )
                return False

        return True


# ---------------------------------------------------------------------------
# Thin per-stage wrappers (lazy imports so CLI start-up stays fast)
# ---------------------------------------------------------------------------


def _run_preproc(config_file: str) -> None:
    """Thin wrapper around :func:`s1proc.preproc.preprocess`."""
    from s1proc.preproc import preprocess

    preprocess(config_file=config_file)


def _run_stack(config: str, verbose: bool) -> None:
    """Thin wrapper around :func:`s1proc.sentinel_stack.run_stack`."""
    from s1proc.sentinel_stack import run_stack

    run_stack(config=config, verbose=verbose)


def _run_amp(config: str) -> None:
    """Thin wrapper around :func:`s1proc.coherence.run_multilook_amp`."""
    from s1proc.coherence import run_multilook_amp

    run_multilook_amp(config=config)


def _run_integrity(config: str) -> None:
    """Thin wrapper around :func:`s1proc.utils.run_check_integrity`."""
    from s1proc.utils import run_check_integrity

    run_check_integrity(config=config, movedata=True)


def _run_slcpairs(config: str) -> None:
    """Thin wrapper around :func:`s1proc.utils.run_create_slc_pair_list`."""
    from s1proc.utils import run_create_slc_pair_list

    run_create_slc_pair_list(config=config)


def _run_interfere(config: str, verbose: bool) -> None:
    """Thin wrapper around :func:`s1proc.interfere.run_interfere`."""
    from s1proc.interfere import run_interfere

    run_interfere(config=config, verbose=verbose)


def _run_coh(config: str) -> None:
    """Thin wrapper around :func:`s1proc.coherence.run_coherence`."""
    from s1proc.coherence import run_coherence

    run_coherence(config=config)


def _run_phasecorr(config: str, verbose: bool) -> None:
    """Thin wrapper around :func:`s1proc.phase_correction.phase_correction`."""
    from s1proc.phase_correction import phase_correction

    phase_correction(config=config, verbose=verbose)


def _run_unwrap(config: str, verbose: bool) -> None:
    """Thin wrapper around :func:`s1proc.unwrap.batch_unwrap`."""
    from s1proc.unwrap import batch_unwrap

    batch_unwrap(config=config, verbose=verbose)


def _run_timeseries(config: str) -> None:
    """Thin wrapper around :func:`s1proc.time_series.run_time_series`."""
    from s1proc.time_series import run_time_series

    run_time_series(config=config)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


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
    resume: bool = True,
) -> None:
    """Run the complete Sentinel-1 InSAR processing workflow.

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
    resume : bool
        If *True* (default), skip stages whose ``.done`` markers and
        output files are intact and whose configuration fingerprint
        matches the current config.  Set to *False* to force a full
        re-run of every enabled stage.

    Notes
    -----
    Each stage is **idempotent** — it skips outputs that already exist
    on disk.  Combined with the resume mechanism, re-running the
    workflow is safe and picks up where it left off.

    Examples
    --------
    Run everything from a single config file:

    >>> from s1proc.run import run
    >>> run("ascending/path_123/config.yaml")

    Skip preprocessing because data is already on disk:

    >>> run("ascending/path_123/config.yaml", preproc=False)

    Force a clean re-run (ignore cached markers):

    >>> run("ascending/path_123/config.yaml", resume=False)

    Run only the interferogram-formation and downstream steps:

    >>> run("ascending/path_123/config.yaml",
    ...     preproc=False, stack=False, amp=False,
    ...     integrity=False, slcpairs=False)
    """
    from s1proc._config import load_config

    config = str(config)
    cfg = load_config(config)
    icfg = cfg.io

    logger.info("Loaded configuration from %s", config)

    # ------------------------------------------------------------------
    # Build the pipeline
    # ------------------------------------------------------------------
    pipeline = Pipeline(config, resume=resume)

    pipeline.add_stage(
        name="preproc",
        fn=_run_preproc,
        kwargs={"config_file": config},
        output_patterns=[
            "roi.metalink",
            "elevation.dem",
            "elevation.dem.rsc",
        ],
        enabled=preproc,
    )

    pipeline.add_stage(
        name="stack",
        fn=_run_stack,
        kwargs={"config": config, "verbose": verbose},
        output_patterns=[os.path.join(icfg.slc_path, "*.gslc")],
        enabled=stack,
    )

    pipeline.add_stage(
        name="amp",
        fn=_run_amp,
        kwargs={"config": config},
        output_patterns=[os.path.join(icfg.amp_path, "*.amp")],
        enabled=amp,
    )

    pipeline.add_stage(
        name="integrity",
        fn=_run_integrity,
        kwargs={"config": config},
        output_patterns=[
            "incomplete_date.txt",
            icfg.mask_file,
        ],
        enabled=integrity,
    )

    pipeline.add_stage(
        name="slcpairs",
        fn=_run_slcpairs,
        kwargs={"config": config},
        output_patterns=[
            os.path.join(icfg.ifg_path, icfg.img_pair_file),
        ],
        enabled=slcpairs,
    )

    pipeline.add_stage(
        name="interfere",
        fn=_run_interfere,
        kwargs={"config": config, "verbose": verbose},
        output_patterns=[
            os.path.join(icfg.ifg_path, "*.int"),
            icfg.multilook_rsc_file,
        ],
        enabled=interfere,
    )

    pipeline.add_stage(
        name="coh",
        fn=_run_coh,
        kwargs={"config": config},
        output_patterns=[os.path.join(icfg.ifg_path, "*.cc")],
        enabled=coh,
    )

    pipeline.add_stage(
        name="phasecorr",
        fn=_run_phasecorr,
        kwargs={"config": config, "verbose": verbose},
        output_patterns=[os.path.join(icfg.ifg_corr_path, "*")],
        enabled=phasecorr,
    )

    pipeline.add_stage(
        name="unwrap",
        fn=_run_unwrap,
        kwargs={"config": config, "verbose": verbose},
        output_patterns=[os.path.join(icfg.unw_path, "*.unw")],
        enabled=unwrap,
    )

    pipeline.add_stage(
        name="timeseries",
        fn=_run_timeseries,
        kwargs={"config": config},
        output_patterns=[
            os.path.join(icfg.time_series_path, "time_series.zarr"),
        ],
        enabled=timeseries,
    )

    pipeline.run()

    logger.info("%s", "=" * 60)
    logger.info("Pipeline finished.")
    logger.info("%s", "=" * 60)
