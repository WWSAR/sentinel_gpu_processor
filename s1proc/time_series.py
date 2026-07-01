from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Sequence, Tuple

import cupy as cp
import dask.array as da
import numpy as np
import zarr
from matplotlib import pyplot as plt
from numpy.typing import NDArray

from s1proc._log import setup_logger
from s1proc.utils import IfgList

plt.rcParams["image.interpolation"] = "none"

logger = setup_logger(name=__name__, level="INFO")

# ---------------------------------------------------------------------------
# Design matrix builders
# ---------------------------------------------------------------------------


def build_design_matrix_linear(ifg_list: IfgList) -> NDArray[np.float32]:
    """Build the design matrix for a constant-velocity (linear) model.

    Each row holds the total temporal baseline of the interferogram in days.

    Parameters
    ----------
    ifg_list : IfgList
        Parsed interferogram list.

    Returns
    -------
    G : ndarray of shape ``(nifg, 1)``
    """
    B = ifg_list.int_velocity_matrix()  # (nifg, ndate - 1)
    return B.sum(axis=1, keepdims=True).astype(np.float32)  # (nifg, 1)


def build_design_matrix_seasonal(
    ifg_list: IfgList,
    seasonal_terms: int = 1,
) -> NDArray[np.float32]:
    """Build the design matrix for a trend + seasonal harmonic model.

    Columns are: temporal baseline (days), then ``sin(2π·k·t/365.25)`` and
    ``cos(2π·k·t/365.25)`` for ``k = 1 … seasonal_terms``, mapped to
    interferogram-level differences via the A-matrix.

    Parameters
    ----------
    ifg_list : IfgList
    seasonal_terms : int
        Number of harmonic pairs (1 = annual, 2 = annual + semi-annual, …).

    Returns
    -------
    G : ndarray of shape ``(nifg, 1 + 2 * seasonal_terms)``
    """
    days = ifg_list.date2days()  # (ndate,)
    T = 365.25

    basis_cols = [days]  # (ndate,)
    for k in range(1, seasonal_terms + 1):
        omega = 2.0 * np.pi * k / T
        basis_cols.append(np.sin(omega * days))
        basis_cols.append(np.cos(omega * days))
    full_basis = np.column_stack(basis_cols).astype(np.float32)  # (ndate, nparam)

    A = ifg_list.diff_displacement_matrix()  # (nifg, ndate)
    return (A @ full_basis).astype(np.float32)  # (nifg, nparam)


def build_design_matrix_ls(ifg_list: IfgList) -> NDArray[np.float32]:
    """Build the design matrix for plain least-squares SBAS inversion.

    The returned matrix is the velocity-integration matrix **B**, so the
    solution gives the average velocity in each inter-acquisition interval.

    Parameters
    ----------
    ifg_list : IfgList

    Returns
    -------
    B : ndarray of shape ``(nifg, ndate - 1)``
    """
    return ifg_list.int_velocity_matrix().astype(np.float32)


# ---------------------------------------------------------------------------
# GPU kernels (CuPy)
# ---------------------------------------------------------------------------


def _compute_ifg_outlier_mask(
    d_unw: cp.ndarray,  # (nifg, npixels)
    B: cp.ndarray,  # (nifg, ndate - 1)
    mad_scalar: float,
) -> cp.ndarray:
    """Flag interferograms whose per-interval velocities are MAD outliers.

    Per-interval velocities are extracted from the raw, reference-corrected
    phase via the velocity-integration matrix **B**.  The MAD threshold is
    applied across intervals, and any outlier interval contaminates its
    contributing interferograms.

    Parameters
    ----------
    d_unw : cp.ndarray, shape ``(nifg, npixels)``
        Reference-corrected unwrapped phase (radians).
    B : cp.ndarray, shape ``(nifg, ndate - 1)``
        Velocity integration matrix.
    mad_scalar : float
        Threshold multiplier for the median absolute deviation.

    Returns
    -------
    d_ifg_mask : cp.ndarray, shape ``(nifg, npixels)``, dtype bool
        ``True`` where the interferogram should be kept.
    """
    d_tempbl = cp.sum(B, axis=1)  # (nifg,)

    numerator_v = cp.matmul(cp.sign(B).T, d_unw)  # (ndate - 1, npixels)
    denominator_v = cp.matmul(
        (B.T != 0).astype(cp.float32), cp.abs(d_tempbl)[:, None]
    )  # (ndate - 1, npixels)
    d_v = numerator_v / (denominator_v + 1e-6)  # (ndate - 1, npixels)

    d_medv = cp.median(cp.abs(d_v), axis=0)  # (npixels,)
    d_devv = cp.abs(d_v - d_medv[None, :])  # (ndate - 1, npixels)
    d_madv = cp.median(d_devv, axis=0)  # (npixels,)

    d_valid_mask = d_devv <= (d_madv[None, :] * mad_scalar)  # (ndate - 1, npixels)

    has_outlier = cp.matmul(
        (B != 0).astype(cp.float32), (~d_valid_mask).astype(cp.float32)
    )  # (nifg, npixels)
    return has_outlier == 0  # (nifg, npixels), True = keep


def _weighted_lstsq(
    G: cp.ndarray,  # (nifg, nparam)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels)
    regularization: float = 0.0,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` for every pixel in a batch.

    Parameters
    ----------
    G : cp.ndarray, shape ``(nifg, nparam)``
    d : cp.ndarray, shape ``(nifg, npixels)``
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel, per-ifg weights (0 = masked / outlier).
    regularization : float
        Tikhonov factor λ added to the diagonal of the normal equations.
        Should already be scaled by ``mean(tr(BᵀB))`` by the caller.

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``
    """
    nparam = G.shape[1]

    GTWG = cp.einsum("ip,ix,iq->pqx", G, w, G)  # (nparam, nparam, npixels)
    if regularization > 0:
        GTWG += float(regularization) * cp.eye(nparam, dtype=G.dtype)[:, :, None]

    GTWd = cp.einsum("ip,ix,ix->px", G, w, d)  # (nparam, npixels)

    # cp.linalg.solve expects batch dimensions first
    A = GTWG.transpose(2, 0, 1)  # (npixels, nparam, nparam)
    B = GTWd.T[..., None]  # (npixels, nparam, 1)
    x = cp.linalg.solve(A, B)  # (npixels, nparam, 1)
    return x[..., 0].T  # (nparam, npixels)


def _shrinkage(a: cp.ndarray, kappa: cp.ndarray | float) -> cp.ndarray:
    """Soft-thresholding (proximal) operator for the L1 norm.

    Evaluates ``sign(a) * max(|a| - kappa, 0)`` element-wise.

    Parameters
    ----------
    a : cp.ndarray
        Input array.
    kappa : cp.ndarray or float
        Threshold.  Broadcastable with *a*.

    Returns
    -------
    cp.ndarray
    """
    return cp.maximum(0, a - kappa) - cp.maximum(0, -a - kappa)


def _weighted_l1_admm(
    G: cp.ndarray,  # (nifg, nparam)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels)
    GTG_inv: cp.ndarray,  # (nparam, nparam)
    rho: float = 0.4,
    alpha: float = 1.0,
    max_iter: int = 20,
) -> cp.ndarray:  # (nparam, npixels)
    r"""Solve the weighted L1 problem per pixel via ADMM.

    For each pixel :math:`j`:

    .. math::

        \min_{x_j} \sum_i w_{ij} | G_i x_j - d_{ij} |

    using the alternating direction method of multipliers (ADMM).

    The algorithm follows the formulations in [Boyd2010]_ and the reference
    MATLAB implementation at
    https://web.stanford.edu/~boyd/papers/admm/least_abs_deviations/lad.html,
    as adapted by the `dolphin <https://github.com/isce-framework/dolphin>`_
    InSAR package (author: Scott Staniewicz).

    Parameters
    ----------
    G : cp.ndarray, shape ``(nifg, nparam)``
        Design matrix, shared across all pixels.
    d : cp.ndarray, shape ``(nifg, npixels)``
        Observation vector per pixel.
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel, per-ifg weights (0 = masked / outlier).
    GTG_inv : cp.ndarray, shape ``(nparam, nparam)``
        Precomputed inverse of ``Gᵀ G`` (or regularized version).
    rho : float
        Augmented Lagrangian parameter (default 0.4).
    alpha : float
        Over-relaxation parameter, typically in [1.0, 1.8] (default 1.0).
    max_iter : int
        Number of ADMM iterations (default 20).

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``

    References
    ----------
    .. [Boyd2010] Boyd, S., Parikh, N., Chu, E., Peleato, B., & Eckstein, J.
       (2010).  Distributed Optimization and Statistical Learning via the
       Alternating Direction Method of Multipliers.
       Foundations and Trends in Machine Learning, 3(1), 1–122.
       https://web.stanford.edu/~boyd/papers/admm/
    """
    nifg, _ = G.shape
    _, npixels = d.shape

    x = cp.zeros((GTG_inv.shape[0], npixels), dtype=cp.float32)  # (nparam, npixels)
    z = cp.zeros((nifg, npixels), dtype=cp.float32)  # (nifg, npixels)
    u = cp.zeros((nifg, npixels), dtype=cp.float32)  # (nifg, npixels)
    # Ravel weights once: entry (i, j) = w_ij / rho
    kappa_scale = w * (1.0 / rho)  # (nifg, npixels)

    for _ in range(max_iter):
        z_old = z

        # x-update: solve Gᵀ G x = Gᵀ (d + z - u)
        q = G.T @ (d + z - u)  # (nparam, npixels)
        x = GTG_inv @ q  # (nparam, npixels)

        # z-update with over-relaxation
        Ax_hat = alpha * (G @ x) + (1.0 - alpha) * (z_old + d)  # (nifg, npixels)
        z = _shrinkage(Ax_hat - d + u, kappa_scale)  # (nifg, npixels)

        # u-update (scaled form)
        u += Ax_hat - z - d  # (nifg, npixels)

    return x  # (nparam, npixels)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_mask_chunk(
    block_info: dict | None,
    mask: NDArray[np.bool_] | None,
    default_shape: Tuple[int, int],
) -> NDArray[np.bool_]:
    """Slice the full mask to match the current dask chunk extent."""
    if block_info is None or mask is None:
        return np.ones(default_shape, dtype=np.bool_)
    array_loc = block_info[0]["array-location"]
    row_slice = slice(int(array_loc[1][0]), int(array_loc[1][1]))
    col_slice = slice(int(array_loc[2][0]), int(array_loc[2][1]))
    return mask[row_slice, col_slice]


def _phase_to_displacement(
    phase_rad: cp.ndarray,
    wvl: float,
) -> cp.ndarray:
    """Convert phase (radians) to line-of-sight displacement (meters)."""
    return phase_rad * float(wvl) / (4.0 * cp.pi)


def _displacement_time_series_linear(
    x: cp.ndarray,  # (1, npixels)
    days: cp.ndarray,  # (ndate,)
    wvl: float,
) -> cp.ndarray:
    """Cumulative displacement from constant-velocity model parameters.

    Parameters
    ----------
    x : cp.ndarray, shape ``(1, npixels)``
        Velocity in rad / day.
    days : cp.ndarray, shape ``(ndate,)``
    wvl : float

    Returns
    -------
    ts : cp.ndarray, shape ``(ndate, npixels)``
        Cumulative displacement in meters.
    """
    disp_rad = x[0:1, :] * days[:, None]  # (ndate, npixels)
    return _phase_to_displacement(disp_rad, wvl)


def _displacement_time_series_seasonal(
    x: cp.ndarray,  # (1 + 2*seasonal_terms, npixels)
    days: cp.ndarray,  # (ndate,)
    wvl: float,
    seasonal_terms: int,
) -> cp.ndarray:
    """Cumulative displacement from seasonal model parameters.

    Parameters
    ----------
    x : cp.ndarray, shape ``(1 + 2*seasonal_terms, npixels)``
    days : cp.ndarray, shape ``(ndate,)``
    wvl : float
    seasonal_terms : int

    Returns
    -------
    ts : cp.ndarray, shape ``(ndate, npixels)``
        Cumulative displacement in meters.
    """
    T = 365.25
    ndate = len(days)
    nparam = 1 + 2 * seasonal_terms

    basis = cp.ones((ndate, nparam), dtype=cp.float32)
    basis[:, 0] = days.astype(cp.float32)
    for k in range(1, seasonal_terms + 1):
        omega = 2.0 * np.pi * k / T
        col_sin = 1 + 2 * (k - 1)
        col_cos = 2 + 2 * (k - 1)
        basis[:, col_sin] = cp.sin(omega * days)
        basis[:, col_cos] = cp.cos(omega * days)

    disp_rad = cp.matmul(basis, x)  # (ndate, npixels)
    return _phase_to_displacement(disp_rad, wvl)


def _displacement_time_series_ls(
    x: cp.ndarray,  # (ndate - 1, npixels)
    dt: NDArray[np.float32],  # (ndate - 1,)
    wvl: float,
) -> cp.ndarray:
    """Cumulative displacement from per-interval velocity parameters.

    Parameters
    ----------
    x : cp.ndarray, shape ``(ndate - 1, npixels)``
        Velocity in rad / day in each inter-acquisition interval.
    dt : ndarray, shape ``(ndate - 1,)``
        Interval durations in days.
    wvl : float

    Returns
    -------
    ts : cp.ndarray, shape ``(ndate, npixels)``
        Cumulative displacement in meters.
    """
    dt_gpu = cp.array(dt, dtype=cp.float32)  # (ndate - 1,)
    incr = x * dt_gpu[:, None]  # (ndate - 1, npixels)
    cum_disp_rad = cp.concatenate(
        [cp.zeros((1, x.shape[1]), dtype=cp.float32), cp.cumsum(incr, axis=0)],
        axis=0,
    )  # (ndate, npixels)
    return _phase_to_displacement(cum_disp_rad, wvl)


# ---------------------------------------------------------------------------
# Per-chunk solver functions  (called via dask ``map_blocks``)
# ---------------------------------------------------------------------------


def _stack_block(
    unw_chunk: NDArray[np.float32],  # (nifg, chunk_rows, chunk_cols)
    B: NDArray[np.float32] = None,  # (nifg, ndate - 1)
    ref_phase: NDArray[np.float32] = None,  # (nifg,)
    wvl: float = 0.055465763,
    mad_scalar: float = 0.0,
    mask: NDArray[np.bool_] = None,  # (nrow, ncol)
    block_info: dict = None,
    **kwargs: Any,
) -> NDArray[np.float32]:  # (chunk_rows, chunk_cols)
    """Stacking velocity estimator with optional MAD outlier removal.

    When ``mad_scalar <= 0`` all interferograms are used equally.  Otherwise
    per-interval velocities are screened and outlier interferograms are
    excluded per-pixel before the final weighted sum.
    """
    chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
    # (chunk_rows, chunk_cols)
    mask_chunk = _get_mask_chunk(block_info, mask, chunk_shape)
    nifg, chunk_row, chunk_col = unw_chunk.shape
    npixels = chunk_row * chunk_col

    d_unw = cp.array(unw_chunk.reshape(nifg, npixels))  # (nifg, npixels)
    d_ref = cp.array(ref_phase.reshape(nifg, 1))  # (nifg, 1)
    d_unw -= d_ref

    d_B = cp.array(B, dtype=cp.float32)  # (nifg, ndate - 1)
    d_tempbl = cp.sum(d_B, axis=1)  # (nifg,)

    if mad_scalar > 0:
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_B, mad_scalar)
        d_filtered_unw = d_unw * d_ifg_mask  # (nifg, npixels)
        # (npixels,)
        d_filtered_bl = cp.sum(d_tempbl[:, None] * d_ifg_mask, axis=0)
    else:
        d_filtered_unw = d_unw
        d_filtered_bl = cp.sum(d_tempbl)  # scalar

    v = cp.sum(d_filtered_unw, axis=0) / (d_filtered_bl + 1e-6)  # (npixels,)
    v *= wvl / (4.0 * cp.pi) * 365.25  # m / yr

    v[~cp.array(mask_chunk.ravel())] = np.nan
    return cp.asnumpy(v).reshape(chunk_row, chunk_col)  # (chunk_rows, chunk_cols)


def _sbas_solver_chunk(
    unw_chunk: NDArray[np.float32],  # (nifg, chunk_rows, chunk_cols)
    mask_chunk: NDArray[np.bool_],  # (chunk_rows, chunk_cols)
    G: NDArray[np.float32],  # (nifg, nparam)
    B: NDArray[np.float32],  # (nifg, ndate - 1)
    ref_phase: NDArray[np.float32],  # (nifg,)
    wvl: float,
    output_dim: str,
    days: NDArray[np.float32],  # (ndate,)
    dt: NDArray[np.float32] | None,  # (ndate - 1,) or None
    mad_scalar: float,
    regularization: float,
    seasonal_terms: int,
    solver_type: str,
) -> NDArray[np.float32]:
    """Generic SBAS solver for one dask chunk.

    Outlier detection operates on per-interval velocities extracted from the
    raw phase via the **B** matrix, matching the stacking outlier logic.

    Parameters
    ----------
    unw_chunk : ndarray, shape ``(nifg, chunk_rows, chunk_cols)``
    mask_chunk : ndarray, shape ``(chunk_rows, chunk_cols)``
        ``True`` for valid pixels.
    G : ndarray, shape ``(nifg, nparam)``
        Design matrix for the model being solved.
    B : ndarray, shape ``(nifg, ndate - 1)``
        Velocity integration matrix (used only for outlier detection).
    ref_phase : ndarray, shape ``(nifg,)``
    wvl : float
    output_dim : ``"2d"`` | ``"3d"``
    days : ndarray, shape ``(ndate,)``
    dt : ndarray, shape ``(ndate - 1,)`` or None
    mad_scalar : float
        Values <= 0 disable outlier removal.
    regularization : float
        Already scaled by ``mean(tr(BᵀB))`` by the caller.
    seasonal_terms : int
    solver_type : ``"linear"`` | ``"seasonal"`` | ``"ls"``

    Returns
    -------
    result : ndarray
        ``(chunk_rows, chunk_cols)`` if ``output_dim == "2d"``, else
        ``(ndate, chunk_rows, chunk_cols)``.
    """
    nifg, chunk_rows, chunk_cols = unw_chunk.shape
    npixels = chunk_rows * chunk_cols

    if not np.any(mask_chunk):
        if output_dim == "2d":
            return np.full((chunk_rows, chunk_cols), np.nan, dtype=np.float32)
        ndate = len(days)
        return np.full((ndate, chunk_rows, chunk_cols), np.nan, dtype=np.float32)

    d_unw = cp.array(unw_chunk.reshape(nifg, npixels))  # (nifg, npixels)
    d_ref = cp.array(ref_phase.reshape(nifg, 1))  # (nifg, 1)
    d_G = cp.array(G, dtype=cp.float32)  # (nifg, nparam)
    d_days = cp.array(days, dtype=cp.float32)  # (ndate,)

    d_unw -= d_ref  # reference-corrected phase

    # (npixels,)
    mask_flat = cp.array(mask_chunk.ravel())
    # (nifg, npixels)
    weights = cp.tile(mask_flat.astype(cp.float32)[None, :], (nifg, 1))

    if mad_scalar > 0:
        d_B = cp.array(B, dtype=cp.float32)  # (nifg, ndate - 1)
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_B, mad_scalar)
        weights = weights * d_ifg_mask.astype(cp.float32)  # (nifg, npixels)

    # (nparam, npixels)
    x = _weighted_lstsq(d_G, d_unw, weights, regularization=regularization)

    if output_dim == "2d":
        v_rad_per_day = x[0:1, :]  # (1, npixels)
        v_m_per_yr = _phase_to_displacement(v_rad_per_day, wvl) * 365.25  # (1, npixels)
        v_m_per_yr = v_m_per_yr[0, :]  # (npixels,)
        v_m_per_yr[~mask_flat] = np.nan
        return cp.asnumpy(v_m_per_yr).reshape(chunk_rows, chunk_cols)

    # 3D: cumulative displacement time series  (ndate, npixels)
    if solver_type == "linear":
        ts = _displacement_time_series_linear(x, d_days, wvl)
    elif solver_type == "seasonal":
        ts = _displacement_time_series_seasonal(x, d_days, wvl, seasonal_terms)
    elif solver_type == "ls":
        ts = _displacement_time_series_ls(x, dt, wvl)
    else:
        raise ValueError(f"Unknown solver_type: {solver_type}")

    ts[:, ~mask_flat] = np.nan
    ts = cp.asnumpy(ts)  # (ndate, npixels)
    # (ndate, chunk_rows, chunk_cols)
    return ts.reshape(ts.shape[0], chunk_rows, chunk_cols)


def _sbas_l1_chunk(
    unw_chunk: NDArray[np.float32],  # (nifg, chunk_rows, chunk_cols)
    mask_chunk: NDArray[np.bool_],  # (chunk_rows, chunk_cols)
    G: NDArray[np.float32],  # (nifg, nparam)
    B: NDArray[np.float32],  # (nifg, ndate - 1)
    ref_phase: NDArray[np.float32],  # (nifg,)
    wvl: float,
    days: NDArray[np.float32],  # (ndate,)
    dt: NDArray[np.float32],  # (ndate - 1,)
    mad_scalar: float,
    regularization: float,
    GTG_inv: NDArray[np.float32],  # (nparam, nparam)
    l1_rho: float,
    l1_alpha: float,
    l1_max_iter: int,
) -> NDArray[np.float32]:  # (ndate, chunk_rows, chunk_cols)
    """SBAS chunk solver using L1-norm minimization via ADMM.

    Solves ``minimize ||G x - d||_1`` per pixel with weighted observations,
    where *G* is the velocity-integration matrix **B** (``build_design_matrix_ls``).

    Outlier detection is applied to the raw phase before the L1 solve,
    matching the logic in the other SBAS solvers.

    Parameters
    ----------
    unw_chunk : ndarray, shape ``(nifg, chunk_rows, chunk_cols)``
    mask_chunk : ndarray, shape ``(chunk_rows, chunk_cols)``
    G : ndarray, shape ``(nifg, nparam)``
        Velocity-integration design matrix.
    B : ndarray, shape ``(nifg, ndate - 1)``
        Velocity integration matrix for outlier detection.
    ref_phase : ndarray, shape ``(nifg,)``
    wvl : float
    days : ndarray, shape ``(ndate,)``
    dt : ndarray, shape ``(ndate - 1,)``
        Interval durations in days, for time-series reconstruction.
    mad_scalar : float
    regularization : float
        Tikhonov regularization added to Gᵀ G before inversion.
    GTG_inv : ndarray, shape ``(nparam, nparam)``
        Precomputed ``(Gᵀ G + λ I)⁻¹``.
    l1_rho : float
        ADMM augmented Lagrangian parameter.
    l1_alpha : float
        ADMM over-relaxation parameter.
    l1_max_iter : int
        Number of ADMM iterations.

    Returns
    -------
    result : ndarray, shape ``(ndate, chunk_rows, chunk_cols)``
        Cumulative displacement time series in meters.
    """
    nifg, chunk_rows, chunk_cols = unw_chunk.shape
    npixels = chunk_rows * chunk_cols

    if not np.any(mask_chunk):
        ndate = len(days)
        return np.full((ndate, chunk_rows, chunk_cols), np.nan, dtype=np.float32)

    d_unw = cp.array(unw_chunk.reshape(nifg, npixels))  # (nifg, npixels)
    d_ref = cp.array(ref_phase.reshape(nifg, 1))  # (nifg, 1)
    d_G = cp.array(G, dtype=cp.float32)  # (nifg, nparam)
    d_GTG_inv = cp.array(GTG_inv, dtype=cp.float32)  # (nparam, nparam)

    d_unw -= d_ref  # reference-corrected phase

    # (npixels,)
    mask_flat = cp.array(mask_chunk.ravel())
    # (nifg, npixels)
    weights = cp.tile(mask_flat.astype(cp.float32)[None, :], (nifg, 1))

    if mad_scalar > 0:
        d_B = cp.array(B, dtype=cp.float32)  # (nifg, ndate - 1)
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_B, mad_scalar)
        weights = weights * d_ifg_mask.astype(cp.float32)  # (nifg, npixels)

    # (nparam, npixels) — L1-minimized interval velocities in rad / day
    x = _weighted_l1_admm(
        d_G,
        d_unw,
        weights,
        d_GTG_inv,
        rho=float(l1_rho),
        alpha=float(l1_alpha),
        max_iter=int(l1_max_iter),
    )

    # Reconstruct cumulative displacement time series
    ts = _displacement_time_series_ls(x, dt, wvl)  # (ndate, npixels)
    ts[:, ~mask_flat] = np.nan
    ts = cp.asnumpy(ts)
    # (ndate, chunk_rows, chunk_cols)
    return ts.reshape(ts.shape[0], chunk_rows, chunk_cols)


# ---------------------------------------------------------------------------
# Block wrappers for dask ``map_blocks``
#
#   ``_sbas_linear_block``  → always 2d (mean velocity).
#   ``_sbas_seasonal_block``,
#   ``_sbas_ls_block``,
#   ``_sbas_l1_block``       → always 3d (displacement time series).
# ---------------------------------------------------------------------------


def _sbas_linear_block(
    unw_chunk: NDArray[np.float32],
    G: NDArray[np.float32] = None,
    B: NDArray[np.float32] = None,
    ref_phase: NDArray[np.float32] = None,
    wvl: float = 0.055465763,
    days: NDArray[np.float32] = None,
    mask: NDArray[np.bool_] = None,
    mad_scalar: float = 0.0,
    block_info: dict = None,
    regularization: float = 0.0,
    **kwargs: Any,
) -> NDArray[np.float32]:
    chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
    mask_chunk = _get_mask_chunk(block_info, mask, chunk_shape)
    return _sbas_solver_chunk(
        unw_chunk,
        mask_chunk,
        G,
        B,
        ref_phase,
        wvl,
        "2d",
        days,
        dt=None,
        mad_scalar=mad_scalar,
        regularization=regularization,
        seasonal_terms=0,
        solver_type="linear",
    )


def _sbas_seasonal_block(
    unw_chunk: NDArray[np.float32],
    G: NDArray[np.float32] = None,
    B: NDArray[np.float32] = None,
    ref_phase: NDArray[np.float32] = None,
    wvl: float = 0.055465763,
    days: NDArray[np.float32] = None,
    mask: NDArray[np.bool_] = None,
    mad_scalar: float = 0.0,
    block_info: dict = None,
    regularization: float = 1e-3,
    seasonal_terms: int = 1,
    **kwargs: Any,
) -> NDArray[np.float32]:
    chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
    mask_chunk = _get_mask_chunk(block_info, mask, chunk_shape)
    return _sbas_solver_chunk(
        unw_chunk,
        mask_chunk,
        G,
        B,
        ref_phase,
        wvl,
        "3d",
        days,
        dt=None,
        mad_scalar=mad_scalar,
        regularization=regularization,
        seasonal_terms=seasonal_terms,
        solver_type="seasonal",
    )


def _sbas_ls_block(
    unw_chunk: NDArray[np.float32],
    G: NDArray[np.float32] = None,
    B: NDArray[np.float32] = None,
    ref_phase: NDArray[np.float32] = None,
    wvl: float = 0.055465763,
    days: NDArray[np.float32] = None,
    dt: NDArray[np.float32] = None,
    mask: NDArray[np.bool_] = None,
    mad_scalar: float = 0.0,
    block_info: dict = None,
    regularization: float = 1e-3,
    **kwargs: Any,
) -> NDArray[np.float32]:
    chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
    mask_chunk = _get_mask_chunk(block_info, mask, chunk_shape)
    return _sbas_solver_chunk(
        unw_chunk,
        mask_chunk,
        G,
        B,
        ref_phase,
        wvl,
        "3d",
        days,
        dt=dt,
        mad_scalar=mad_scalar,
        regularization=regularization,
        seasonal_terms=0,
        solver_type="ls",
    )


def _sbas_l1_block(
    unw_chunk: NDArray[np.float32],
    G: NDArray[np.float32] = None,
    B: NDArray[np.float32] = None,
    ref_phase: NDArray[np.float32] = None,
    wvl: float = 0.055465763,
    days: NDArray[np.float32] = None,
    dt: NDArray[np.float32] = None,
    mask: NDArray[np.bool_] = None,
    mad_scalar: float = 0.0,
    block_info: dict = None,
    regularization: float = 1e-3,
    GTG_inv: NDArray[np.float32] = None,
    l1_rho: float = 0.4,
    l1_alpha: float = 1.0,
    l1_max_iter: int = 20,
    **kwargs: Any,
) -> NDArray[np.float32]:
    chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
    mask_chunk = _get_mask_chunk(block_info, mask, chunk_shape)
    return _sbas_l1_chunk(
        unw_chunk,
        mask_chunk,
        G,
        B,
        ref_phase,
        wvl,
        days,
        dt=dt,
        mad_scalar=mad_scalar,
        regularization=regularization,
        GTG_inv=GTG_inv,
        l1_rho=l1_rho,
        l1_alpha=l1_alpha,
        l1_max_iter=l1_max_iter,
    )


# ---------------------------------------------------------------------------
# Time-series orchestrator
# ---------------------------------------------------------------------------


def time_series_solver(
    unw_files: Sequence[str],
    mask_file: Path | str | None,
    out_path: Path | str,
    nrow: int,
    ncol: int,
    solver_func: Callable,
    solver_kwargs: Dict[str, Any] | None = None,
    reference_point: Tuple[int, int] = (0, 0),
    reference_win: Tuple[int, int] = (11, 11),
    output_dim: Literal["2d", "3d"] = "2d",
    row_chunk_size: int | None = None,
    col_chunk_size: int | None = None,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """Run the time-series computation and write results to zarr.

    For 2D output (stack, sbas_linear) a single ``velocity`` dataset is
    written.  For 3D output (sbas_seasonal, sbas_ls, sbas_l1) three datasets are
    written: ``displacement`` (3D time series), ``cumulative_deformation``
    (final time step), and ``velocity`` (mean LOS velocity in m / yr).

    Parameters
    ----------
    unw_files : Sequence[str]
        Paths to unwrapped interferograms (binary float32).
    mask_file : str or None
        Boolean mask (True = valid pixel).
    out_path : Path or str
        Output directory.
    nrow : int
        Number of rows per interferogram.
    ncol : int
        Number of columns per interferogram.
    solver_func : Callable
        Per-chunk solver (a dask ``map_blocks``-compatible function).
    solver_kwargs : dict or None
        Keyword arguments forwarded to *solver_func*.
    reference_point : Tuple[int, int]
        Reference pixel (row, col).
    reference_win : Tuple[int, int]
        Window radius for reference phase estimation.
    output_dim : ``"2d"`` | ``"3d"``
    row_chunk_size : int or None
    col_chunk_size : int or None
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    if solver_kwargs is None:
        solver_kwargs = {}

    unw_list = IfgList(unw_files)
    logger.info(
        "Time series analysis with %d interferograms, %d unique dates",
        len(unw_files),
        unw_list.ndate,
    )
    logger.info("Creating image stacks with dask")

    ifg_memmaps = [
        np.memmap(f, dtype="float32", mode="r", shape=(nrow, ncol)) for f in unw_files
    ]
    unw_dask_slices = [da.from_array(m, chunks=(nrow, ncol)) for m in ifg_memmaps]
    unw_stack = da.stack(unw_dask_slices, axis=0)  # (nifg, nrow, ncol)

    # Load mask
    logger.info("Load mask from %s", mask_file)
    if mask_file is not None:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    # Rechunk
    if row_chunk_size is None:
        row_chunk_size = int(np.minimum(128, nrow))
    if col_chunk_size is None:
        col_chunk_size = int(np.minimum(128, ncol))
    unw_stack = unw_stack.rechunk({0: -1, 1: row_chunk_size, 2: col_chunk_size})
    logger.info(
        "Rechunked dask stack: row_chunk=%d, col_chunk=%d",
        row_chunk_size,
        col_chunk_size,
    )

    # --- Reference phase ----------------------------------------------------
    if (
        reference_point[0] < 0
        or reference_point[0] >= nrow
        or reference_point[1] < 0
        or reference_point[1] >= ncol
    ):
        raise ValueError(
            f"Reference point ({reference_point[0]}, {reference_point[1]}) "
            "out of boundary."
        )
    if not mask[reference_point[0], reference_point[1]]:
        raise RuntimeError(
            f"Reference point ({reference_point[0]}, {reference_point[1]}) "
            "is on the masked area."
        )
    if reference_win[0] < 0 or reference_win[1] < 0:
        raise ValueError("Reference window size must be positive.")

    half_row_win = (reference_win[0] + 1) // 2
    half_col_win = (reference_win[1] + 1) // 2
    top = int(np.maximum(0, reference_point[0] - half_row_win + 1))
    bottom = int(np.minimum(nrow, reference_point[0] + half_row_win))
    left = int(np.maximum(0, reference_point[1] - half_col_win + 1))
    right = int(np.minimum(ncol, reference_point[1] + half_col_win))
    logger.info(
        "Reference window: left=%d, right=%d, top=%d, bottom=%d",
        left,
        right,
        top,
        bottom,
    )
    ref_win_mask = mask[top:bottom, left:right]
    # (nifg, win_rows, win_cols)
    ref_tube = unw_stack[:, top:bottom, left:right].compute()
    ref_phase = np.mean(ref_tube * ref_win_mask[None, :, :], axis=(1, 2))  # (nifg,)

    # --- Build common kwargs for map_blocks ---------------------------------
    common_kwargs = dict(
        G=solver_kwargs.get("G"),
        B=solver_kwargs.get("B"),
        ref_phase=ref_phase,
        wvl=solver_kwargs.get("wvl", 0.055465763),
        days=solver_kwargs.get("days"),
        dt=solver_kwargs.get("dt"),
        mask=mask,
        mad_scalar=solver_kwargs.get("mad_scalar", 0.0),
        regularization=solver_kwargs.get("regularization", 0.0),
        seasonal_terms=solver_kwargs.get("seasonal_terms", 1),
        GTG_inv=solver_kwargs.get("GTG_inv"),
        l1_rho=solver_kwargs.get("l1_rho", 0.4),
        l1_alpha=solver_kwargs.get("l1_alpha", 1.0),
        l1_max_iter=solver_kwargs.get("l1_max_iter", 20),
        block_info=True,
    )

    # --- Run the dask computation -------------------------------------------
    if output_dim == "2d":
        result = da.map_blocks(
            solver_func,
            unw_stack,
            dtype=np.float32,
            drop_axis=0,
            **common_kwargs,
        )  # (nrow, ncol)
    else:
        ndate_out = solver_kwargs.get("ndate_out", unw_list.ndate)
        new_chunks = (ndate_out, *unw_stack.chunks[1:])
        result = da.map_blocks(
            solver_func,
            unw_stack,
            dtype=np.float32,
            drop_axis=0,
            new_axis=0,
            chunks=new_chunks,
            **common_kwargs,
        )  # (ndate, nrow, ncol)

    out_path = Path(out_path)
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True, exist_ok=True)

    # --- Write to zarr ------------------------------------------------------
    if output_dim == "2d":
        logger.info("Writing velocity to %s", out_path)
        result.to_zarr(out_path, overwrite=True)

        root = zarr.open(out_path, mode="a")
        if metadata:
            for key, value in metadata.items():
                _store_attr(root, key, value)
    else:
        total_days = float(solver_kwargs.get("days", np.zeros(1))[-1])
        store = str(out_path)
        logger.info("Writing displacement time series to %s", out_path)

        # Write 3D displacement
        da.to_zarr(result, store, component="displacement", overwrite=True)
        # Derive cumulative deformation (final time step)
        cumulative = result[-1]  # (nrow, ncol)
        da.to_zarr(
            cumulative,
            store,
            component="cumulative_deformation",
            overwrite=True,
        )
        # Derive mean velocity  v = disp_final / total_days * 365.25  (m / yr)
        velocity = cumulative / total_days * 365.25  # (nrow, ncol)
        da.to_zarr(velocity, store, component="velocity", overwrite=True)

        root = zarr.open(store, mode="a")
        if metadata:
            for key, value in metadata.items():
                _store_attr(root, key, value)
        root.attrs["total_days"] = total_days

    logger.info("Time series computation complete.")


def _store_attr(group: zarr.hierarchy.Group, key: str, value: Any) -> None:
    """Write a metadata value to a zarr group, handling non-scalar types."""
    if isinstance(value, (np.ndarray, list)):
        group.attrs[key] = value
    elif isinstance(value, Path):
        group.attrs[key] = str(value)
    else:
        group.attrs[key] = value


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_time_series(
    unw_path: Path | str | None = None,
    outpath: Path | str | None = None,
    config: str = "config.yaml",
) -> None:
    """Run time series analysis.

    Supports five methods via the configuration key ``timeseries.method``:

    - ``"stack"`` — velocity stacking (always 2D).
    - ``"sbas_linear"`` — SBAS with a constant-velocity model (always 2D).
    - ``"sbas_seasonal"`` — SBAS with trend + seasonal harmonics (always 3D).
    - ``"sbas_ls"`` — SBAS plain least-squares inversion (always 3D).
    - ``"sbas_l1"`` — SBAS L1-norm (LAD) inversion via ADMM (always 3D).

    The ``"sbas_l1"`` solver minimizes the L1-norm of the observation
    residuals using the alternating direction method of multipliers (ADMM),
    following [Boyd2010]_ and the MATLAB reference implementation
    https://web.stanford.edu/~boyd/papers/admm/least_abs_deviations/lad.html,
    as adapted for InSAR in the `dolphin
    <https://github.com/isce-framework/dolphin>`_ package.

    Parameters
    ----------
    unw_path : Path or str or None
        Directory containing unwrapped interferograms.  When *None* the
        paths from the configuration are used.
    outpath : Path or str or None
        Output directory.  Falls back to ``io.time_series_path``.
    config : str
        Path to the YAML configuration file.
    """
    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates
    from s1proc.utils import get_files

    cfg = load_config(config)
    icfg = cfg.io
    pcfg = cfg.proc
    tcfg = cfg.timeseries

    # Locate unwrapped interferograms
    if unw_path is None:
        unw_files = get_files(icfg.unw_corr_path, "unw")
        if len(unw_files) == 0:
            unw_files = get_files(icfg.unw_path, "unw")
        if len(unw_files) == 0:
            logger.warning("Cannot find unwrapped interferograms.")
            return
    else:
        unw_files = get_files(unw_path, "unw")

    if outpath is None:
        outpath = icfg.time_series_path

    # Geometry
    mask_file = icfg.mask_file
    rsc_file = icfg.multilook_rsc_file
    rsc = GeoCoordinates(rsc_file)
    nrow, ncol = rsc.nlat, rsc.nlon
    reference_point = rsc.ll2xy(tcfg.parameters.ref_lat, tcfg.parameters.ref_lon)
    reference_win = (11, 11)

    # ------- Build solver kwargs from config --------------------------------
    unw_list = IfgList(unw_files)
    method = tcfg.method
    config_reg = tcfg.parameters.regularization

    mad_scalar = (
        tcfg.parameters.mad_scalar
        if tcfg.parameters.mad_scalar is not None and tcfg.parameters.mad_scalar > 0
        else 0.0
    )

    B = unw_list.int_velocity_matrix().astype(np.float32)  # (nifg, ndate - 1)
    # Scale factor for Tikhonov regularization:  mean(trace(Bᵀ B))
    reg_scale = float(np.mean(np.sum(B.astype(np.float64) ** 2)))
    logger.info(f"Scale factor for Tikhonov regularization: {reg_scale:5.3f}")

    solver_kwargs: Dict[str, Any] = dict(
        wvl=pcfg.wavelength,
        days=unw_list.date2days().astype(np.float32),  # (ndate,)
        mad_scalar=mad_scalar,
        regularization=0.0,  # default; overridden below for methods that need it
    )

    # ------- Dispatch -------------------------------------------------------
    if method == "stack":
        output_dim: Literal["2d", "3d"] = "2d"
        solver_func = _stack_block
        solver_kwargs["B"] = B
        solver_kwargs["ndate_out"] = 1
        solver_kwargs["seasonal_terms"] = 0
        solver_kwargs["regularization"] = 0.0
        logger.info("Stacking velocity (mad_scalar=%.1f)", mad_scalar)

    elif method == "sbas_linear":
        output_dim = "2d"
        solver_func = _sbas_linear_block
        solver_kwargs["G"] = build_design_matrix_linear(unw_list)  # (nifg, 1)
        solver_kwargs["B"] = B
        solver_kwargs["ndate_out"] = unw_list.ndate
        solver_kwargs["seasonal_terms"] = 0
        solver_kwargs["regularization"] = 0.0
        solver_kwargs["dt"] = unw_list.date_interval(
            drop_first_date=True,
        ).astype(np.float32)  # (ndate - 1,)
        logger.info("SBAS linear (mad_scalar=%.1f)", mad_scalar)

    elif method == "sbas_seasonal":
        output_dim = "3d"
        seasonal_terms = tcfg.parameters.seasonal_terms
        solver_func = _sbas_seasonal_block
        solver_kwargs["G"] = build_design_matrix_seasonal(
            unw_list,
            seasonal_terms=seasonal_terms,
        )  # (nifg, 1 + 2*seasonal_terms)
        solver_kwargs["B"] = B
        solver_kwargs["ndate_out"] = unw_list.ndate
        solver_kwargs["seasonal_terms"] = seasonal_terms
        solver_kwargs["dt"] = None
        solver_kwargs["regularization"] = config_reg * reg_scale
        logger.info(
            "SBAS seasonal (terms=%d, reg=%.3e, mad_scalar=%.1f)",
            seasonal_terms,
            solver_kwargs["regularization"],
            mad_scalar,
        )

    elif method == "sbas_ls":
        output_dim = "3d"
        solver_func = _sbas_ls_block
        solver_kwargs["G"] = build_design_matrix_ls(unw_list)  # (nifg, ndate - 1)
        solver_kwargs["B"] = B
        solver_kwargs["ndate_out"] = unw_list.ndate
        solver_kwargs["seasonal_terms"] = 0
        solver_kwargs["dt"] = unw_list.date_interval(
            drop_first_date=True,
        ).astype(np.float32)  # (ndate - 1,)
        solver_kwargs["regularization"] = config_reg * reg_scale
        logger.info(
            "SBAS plain least-squares (reg=%.3e, mad_scalar=%.1f)",
            solver_kwargs["regularization"],
            mad_scalar,
        )

    elif method == "sbas_l1":
        output_dim = "3d"
        solver_func = _sbas_l1_block
        solver_kwargs["G"] = build_design_matrix_ls(unw_list)  # (nifg, ndate - 1)
        solver_kwargs["B"] = B
        solver_kwargs["ndate_out"] = unw_list.ndate
        solver_kwargs["seasonal_terms"] = 0
        solver_kwargs["dt"] = unw_list.date_interval(
            drop_first_date=True,
        ).astype(np.float32)  # (ndate - 1,)
        solver_kwargs["regularization"] = config_reg * reg_scale
        # Precompute (Gᵀ G + λ I)⁻¹ for the ADMM x-update
        G_l1 = solver_kwargs["G"]
        GTG_l1 = G_l1.astype(np.float64).T @ G_l1.astype(np.float64)
        GTG_l1 += np.eye(GTG_l1.shape[0]) * float(solver_kwargs["regularization"])
        solver_kwargs["GTG_inv"] = np.linalg.inv(GTG_l1).astype(np.float32)
        solver_kwargs["l1_rho"] = tcfg.parameters.l1_rho
        solver_kwargs["l1_alpha"] = tcfg.parameters.l1_alpha
        solver_kwargs["l1_max_iter"] = tcfg.parameters.l1_max_iter
        logger.info(
            "SBAS L1-ADMM (reg=%.3e, rho=%.2f, iter=%d, mad_scalar=%.1f)",
            solver_kwargs["regularization"],
            solver_kwargs["l1_rho"],
            solver_kwargs["l1_max_iter"],
            mad_scalar,
        )

    else:
        raise ValueError(
            f"Unknown time series method: {method!r}. "
            "Expected one of: stack, sbas_linear, sbas_seasonal, sbas_ls, sbas_l1."
        )

    time_series_solver(
        unw_files,
        mask_file,
        Path(outpath) / "time_series.zarr",
        nrow,
        ncol,
        solver_func,
        solver_kwargs=solver_kwargs,
        reference_point=reference_point,
        reference_win=reference_win,
        output_dim=output_dim,
        metadata={
            "method": method,
            "dates": list(unw_list.dates),
            "mask_file": str(mask_file) if mask_file else "none",
            "reference_point": list(reference_point),
            "wavelength": float(pcfg.wavelength),
            "seasonal_terms": int(tcfg.parameters.seasonal_terms),
            "mad_scalar": float(mad_scalar),
            "regularization": float(solver_kwargs["regularization"]),
            "reg_scale": float(reg_scale),
            "l1_rho": float(tcfg.parameters.l1_rho),
            "l1_alpha": float(tcfg.parameters.l1_alpha),
            "l1_max_iter": int(tcfg.parameters.l1_max_iter),
        },
    )


# ---------------------------------------------------------------------------
# Plotting utilities
# ---------------------------------------------------------------------------


def plot_velocity_map(
    data: NDArray[np.float32],
    rsc_file: Path | str,
    outfile: Path | str,
    title: str = "Mean Velocity",
    cmap: str = "RdBu_r",
    vmin: float | None = None,
    vmax: float | None = None,
    dpi: int = 150,
) -> None:
    """Plot and save a geocoded velocity or displacement map.

    Parameters
    ----------
    data : ndarray, shape ``(nrows, ncols)``
    rsc_file : Path or str
        RSC file providing georeferencing (used for extent).
    outfile : Path or str
        Output image path (PNG, PDF, …).
    title : str
    cmap : str
    vmin : float or None
    vmax : float or None
    dpi : int
    """
    from s1proc.geocoordinates import GeoCoordinates

    rsc = GeoCoordinates(rsc_file)
    extent = [rsc.lonmin, rsc.lonmax, rsc.latmin, rsc.latmax]

    if vmin is None:
        vmin = float(np.nanpercentile(data, 2))
    if vmax is None:
        vmax = float(np.nanpercentile(data, 98))

    fig, ax = plt.subplots(figsize=(10, 8))
    im = ax.imshow(
        data,
        extent=extent,
        cmap=cmap,
        vmin=vmin,
        vmax=vmax,
        aspect="auto",
        origin="upper",
    )
    cbar = fig.colorbar(im, ax=ax, shrink=0.85, pad=0.03)
    cbar.set_label("Velocity (m/yr)")
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")
    ax.set_title(title)
    fig.tight_layout()

    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(outfile, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.info("Velocity map saved to %s", outfile)


def plot_time_series_at_points(
    data: NDArray[np.float32],
    dates: List[str],
    points: List[Tuple[int, int]],
    labels: List[str] | None = None,
    outfile: Path | str | None = None,
    title: str = "Displacement Time Series",
    ylabel: str = "Displacement (m)",
    dpi: int = 150,
) -> plt.Figure:
    """Plot displacement time series at selected pixel locations.

    Parameters
    ----------
    data : ndarray, shape ``(ndate, nrows, ncols)``
        Displacement time series in meters.
    dates : list of str
        Acquisition dates as ``"YYYYMMDD"`` strings.
    points : list of (row, col)
        Pixel coordinates to extract.
    labels : list of str or None
        Legend labels.  Defaults to ``"Point 1"``, … .
    outfile : Path or str or None
        If given, save the figure to this path.
    title : str
    ylabel : str
    dpi : int

    Returns
    -------
    fig : matplotlib.figure.Figure
    """
    date_dt = [datetime.strptime(d, "%Y%m%d") for d in dates]
    if labels is None:
        labels = [f"Point {i + 1}" for i in range(len(points))]

    fig, ax = plt.subplots(figsize=(10, 5))
    for (row, col), label in zip(points, labels):
        ts = data[:, row, col]
        ax.plot(date_dt, ts, marker="o", markersize=3, linewidth=1.2, label=label)

    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8)
    ax.set_xlabel("Date")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    if outfile is not None:
        outfile = Path(outfile)
        outfile.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(outfile, dpi=dpi, bbox_inches="tight")
        logger.info("Time series plot saved to %s", outfile)

    return fig


def plot_time_series_map(
    data: NDArray[np.float32],
    dates: List[str],
    rsc_file: Path | str,
    outfile: Path | str,
    date_indices: List[int] | None = None,
    cmap: str = "RdBu_r",
    vmin: float | None = None,
    vmax: float | None = None,
    ncols: int = 4,
    dpi: int = 150,
) -> None:
    """Plot a panel of displacement maps at selected dates.

    Parameters
    ----------
    data : ndarray, shape ``(ndate, nrows, ncols)``
    dates : list of str
    rsc_file : Path or str
    outfile : Path or str
    date_indices : list of int or None
        Which date indices to plot.  If *None*, up to 8 evenly spaced dates
        are chosen.
    cmap : str
    vmin : float or None
    vmax : float or None
    ncols : int
    dpi : int
    """
    from s1proc.geocoordinates import GeoCoordinates

    rsc = GeoCoordinates(rsc_file)
    extent = [rsc.lonmin, rsc.lonmax, rsc.latmin, rsc.latmax]

    if date_indices is None:
        ndate = len(dates)
        n_plots = min(8, ndate)
        date_indices = np.linspace(0, ndate - 1, n_plots, dtype=int).tolist()

    if vmin is None:
        vmin = float(np.nanpercentile(data, 2))
    if vmax is None:
        vmax = float(np.nanpercentile(data, 98))

    n_plots = len(date_indices)
    nrows = int(np.ceil(n_plots / ncols))

    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(3.5 * ncols, 3.0 * nrows),
        squeeze=False,
    )

    for idx, di in enumerate(date_indices):
        row, col = divmod(idx, ncols)
        ax = axes[row][col]
        im = ax.imshow(
            data[di],
            extent=extent,
            cmap=cmap,
            vmin=vmin,
            vmax=vmax,
            aspect="auto",
            origin="upper",
        )
        ax.set_title(dates[di])
        ax.set_xlabel("Lon")
        ax.set_ylabel("Lat")

    for idx in range(n_plots, nrows * ncols):
        row, col = divmod(idx, ncols)
        axes[row][col].set_visible(False)

    fig.colorbar(
        im,
        ax=axes.ravel().tolist(),
        shrink=0.8,
        pad=0.03,
        label="Displacement (m)",
    )
    fig.suptitle("Cumulative Displacement", fontsize=13, y=1.01)
    fig.tight_layout()

    outfile = Path(outfile)
    outfile.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(outfile, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    logger.info("Time series map panel saved to %s", outfile)


# ---------------------------------------------------------------------------
# GeoTIFF export
# ---------------------------------------------------------------------------


def generate_geotransform(rsc_file):
    """Build GDAL GeoTransform and WKT projection from an RSC file."""
    from osgeo import osr

    from s1proc.geocoordinates import GeoCoordinates

    rsc = GeoCoordinates(rsc_file)
    geotransform = (rsc.lonmin, rsc.dlon, 0.0, rsc.latmax, 0.0, rsc.dlat)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    return geotransform, srs.ExportToWkt()


def save_geotiff(outfile, data, geotransform, projection):
    """Write a 2D array to a GeoTIFF with LZW compression."""
    from osgeo import gdal

    rows, cols = data.shape
    driver = gdal.GetDriverByName("GTiff")
    ds = driver.Create(
        outfile,
        cols,
        rows,
        1,
        gdal.GDT_Float32,
        options=["COMPRESS=LZW", "TILED=YES"],
    )
    ds.SetGeoTransform(geotransform)
    ds.SetProjection(projection)
    band = ds.GetRasterBand(1)
    band.WriteArray(data.astype(np.float32))
    band.SetNoDataValue(np.nan)
    band.FlushCache()
    ds.FlushCache()
    ds = None
