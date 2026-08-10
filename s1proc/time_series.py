from __future__ import annotations

import gc
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
from tqdm.auto import tqdm

from s1proc._log import logger, set_logging_level
from s1proc.sario import _store_attr, create_virtual_stack
from s1proc.utils import IfgList, _get_mask_chunk, get_gpu_pool

plt.rcParams["image.interpolation"] = "none"
GPU_POOL = get_gpu_pool()


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
    unw: cp.ndarray,  # (nifg, npixels)
    date_mask: cp.ndarray,  # (ndate, nifg)
    tempbl: cp.ndarray,  # (nifg)
    mad_scalar: float,
) -> cp.ndarray:
    """Flag interferograms whose per-interval velocities are MAD outliers.

    Per-interval velocities are extracted from the raw, reference-corrected
    phase via the velocity-integration matrix **B**.  The MAD threshold is
    applied across intervals, and any outlier interval contaminates its
    contributing interferograms.

    Parameters
    ----------
    unw : cp.ndarray, shape ``(nifg, npixels)``
        Reference-corrected unwrapped phase (radians).
    date_mask: cp.ndarray, shape ``(ndate, nifg)``
        Interferograms mask for different date.
        date_mask[j, i] = 1 if the reference SAR image of the i-th interferogram is
        acquried on dates[j]>
        date_mask[j, i] = -1 if the secondary SAR image of the i-th interferogram is
        acquried on dates[j].
    tempbl : cp.ndarray, shape ``(nifg)``
        Temporal baseline of interferograms.
    mad_scalar : float
        Threshold multiplier for the median absolute deviation.

    Returns
    -------
    d_ifg_mask : cp.ndarray, shape ``(nifg, npixels)``, dtype bool
        ``True`` where the interferogram should be kept.
    """
    v = cp.matmul(date_mask, unw)
    sum_tempbl = cp.matmul(cp.abs(date_mask), tempbl)
    v = v / sum_tempbl[:, None]
    medv = cp.nanmedian(v, axis=0)
    mad = cp.nanmedian(cp.abs(v - medv), axis=0)
    bad_dates = v > (medv + mad_scalar * mad)
    mask = cp.matmul((date_mask != 0).T, bad_dates) == 0
    return mask  # (nifg, npixels), True = keep


def _weighted_lstsq_exact(
    G: cp.ndarray,  # (nifg, nparam)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels)
    regularization: float = 0.0,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` per pixel via a batched dense solve.

    Forms the full ``(nparam, nparam, npixels)`` normal-equation tensor
    ``Gᵀ W G`` explicitly, so its GPU memory scales as
    ``O(nparam² · npixels)``.  Prefer ``_weighted_lstsq`` (which dispatches to
    the matrix-free CG solver when ``nparam`` is large) unless ``nparam`` is
    small enough that the tensor is cheap.

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
    A = cp.einsum("ip,ix,iq->xpq", G, w, G)  # (npixels, nparam, nparam)
    if regularization > 0:
        idx = cp.arange(nparam)
        A[:, idx, idx] += float(regularization)
    B = cp.einsum("ip,ix,ix->xp", G, w, d)[..., None]  # (npixels, nparam, 1)
    x = cp.linalg.solve(A, B)  # (npixels, nparam, 1)
    return x[..., 0].T  # (nparam, npixels)


def _weighted_lstsq_cg(
    G: cp.ndarray,  # (nifg, nparam)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels)
    regularization: float = 0.0,
    tol: float = 1e-5,
    max_iter: int = 50,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` per pixel via preconditioned CG.

    Matrix-free: the ``(nparam, nparam, npixels)`` normal-equation tensor is
    never materialized.  Each iteration applies the system matrix through two
    batched matmuls

    .. math::

        (Gᵀ W G) p = Gᵀ ( W ⊙ (G p) ),

    so GPU memory scales as ``O(nparam · npixels + nifg · npixels)`` instead of
    ``O(nparam² · npixels)``.  This makes ``sbas_ls`` (where
    ``nparam = ndate - 1`` can be 50-200+) tractable on a GPU.  Each pixel
    carries its own Jacobi-preconditioned conjugate-gradient run because the
    weight matrix ``W`` differs per pixel; the per-pixel step sizes
    ``alpha``/``beta`` keep the runs independent.

    The solver starts at ``x = 0`` and iterates toward the minimum-norm
    least-squares solution, which matches the exact solve on singular systems.
    When ``max_iter`` is reached before the tolerance, the current iterate is
    returned — an approximation whose quality is controlled by ``tol`` and
    ``max_iter``.

    Parameters
    ----------
    G : cp.ndarray, shape ``(nifg, nparam)``
        Design matrix, shared across pixels.
    d : cp.ndarray, shape ``(nifg, npixels)``
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel, per-ifg weights (0 = masked / outlier).
    regularization : float
        Tikhonov factor λ added to the diagonal of the normal equations.
    tol : float
        Relative-residual tolerance on the global (all-pixel) residual norm.
    max_iter : int
        Maximum number of CG iterations.

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``
    """
    nparam = G.shape[1]
    npixels = d.shape[1]

    # Work only on pixels with at least one non-zero weight; the rest keep the
    # zero inital guess
    active = cp.sum(w, axis=0) > 0
    n_active = int(cp.sum(active))
    if n_active == 0:
        return cp.zeros((nparam, npixels), dtype=cp.float32)

    d_a = d[:, active]  # (nifg, n_active)
    w_a = w[:, active]  # (nifg, n_active)

    # Right-hand side b = Gᵀ (W ⊙ d)
    b = G.T @ (w_a * d_a)
    del d_a

    # Jacobi preconditioner: diagonal of Gᵀ W G.  Entries far below the pixel's
    # own diagonal scale are left unpreconditioned (identity) instead of being
    # inverted, which keeps rounding noise in the null space of a singular
    # system from being amplified by a huge 1/diag.
    diag = (G * G).T @ w_a
    diag_floor = cp.maximum(diag.max(axis=0, keepdims=True) * 1e-7, 1e-12)
    m_inv = cp.where(
        diag + regularization > diag_floor,
        1.0 / cp.maximum(diag + regularization, diag_floor),
        cp.float32(1.0),
    ).astype(cp.float32)

    x = cp.zeros((nparam, n_active), dtype=cp.float32)
    r = b.copy()
    z = m_inv * r
    p = z.copy()
    rho = cp.sum(r * z, axis=0)

    # Preallocate GPU buffers, avoid frequent alloc/free
    tmp = cp.empty((G.shape[0], n_active), dtype=cp.float32)
    Ap = cp.empty((nparam, n_active), dtype=cp.float32)

    # Track the lowest-residual iterate.  On an ill-conditioned or singular
    # system float32 prevents reaching ``tol``, and once CG loses orthogonality
    # the residual can grow again; returning the best iterate keeps such cases
    # stable instead of letting the late iterations diverge.
    r2_pixel = cp.sum(r * r, axis=0)
    best_r2_pixel = r2_pixel.copy()
    x_best = x.copy()

    # initialize the total residual
    r0_norm2_total = float(cp.sum(r2_pixel))
    tol_sq = (tol * tol) * r0_norm2_total + 1e-12

    reg_fp32 = cp.float32(regularization)
    eps_fp32 = cp.float32(1e-12)

    for _ in range(max_iter):
        # pixel-wize residual update and keep the best iterate
        r2_pixel = cp.sum(r * r, axis=0)
        improved = r2_pixel < best_r2_pixel
        best_r2_pixel = cp.where(improved, r2_pixel, best_r2_pixel)
        x_best = cp.where(improved[None, :], x, x_best)

        # Check if the residual converges
        total_r2 = cp.sum(r2_pixel)
        if total_r2 <= tol_sq:
            break

        # matrix-free operator: Ap = Gᵀ (W ⊙ (G p)) + λ p
        cp.matmul(G, p, out=tmp)
        tmp *= w_a
        cp.matmul(G.T, tmp, out=Ap)
        if regularization > 0:
            Ap += reg_fp32 * p

        # update alpha
        pAp = cp.sum(p * Ap, axis=0)
        safe = pAp > eps_fp32
        alpha = cp.where(safe, rho / cp.maximum(pAp, eps_fp32), cp.float32(0.0))

        # update solution and residual
        x += alpha[None, :] * p
        r -= alpha[None, :] * Ap

        # update beta and search direction p
        z = m_inv * r
        rho_new = cp.sum(r * z, axis=0)
        beta = cp.where(safe, rho_new / cp.maximum(rho, eps_fp32), cp.float32(0.0))
        p = z + beta[None, :] * p
        rho = rho_new

    x_out = cp.zeros((nparam, npixels), dtype=cp.float32)
    x_out[:, active] = x_best
    return x_out


def _weighted_lstsq(
    G: cp.ndarray,  # (nifg, nparam)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels)
    regularization: float = 0.0,
    solver: Literal["auto", "exact", "cg"] = "auto",
    cg_tol: float = 1e-5,
    cg_max_iter: int = 50,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` for every pixel in a batch.

    Dispatches to a batched dense solve (``_weighted_lstsq_exact``) or to a
    matrix-free preconditioned CG solve (``_weighted_lstsq_cg``).  The CG
    solver avoids materializing the ``(nparam, nparam, npixels)`` normal
    equation tensor, which is essential for ``sbas_ls`` where
    ``nparam = ndate - 1`` can be large.

    Parameters
    ----------
    G : cp.ndarray, shape ``(nifg, nparam)``
    d : cp.ndarray, shape ``(nifg, npixels)``
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel, per-ifg weights (0 = masked / outlier).
    regularization : float
        Tikhonov factor λ added to the diagonal of the normal equations.
        Should already be scaled by ``mean(tr(BᵀB))`` by the caller.
    solver : ``"auto"`` | ``"exact"`` | ``"cg"``
        - ``"exact"``: form ``Gᵀ W G`` and use a batched dense solve.
        - ``"cg"``: matrix-free Jacobi-preconditioned CG (never forms ``Gᵀ W G``).
        - ``"auto"``: use ``"exact"`` while the normal-equation tensor fits in
          roughly 1 GiB, otherwise use ``"cg"``.
    cg_tol : float
        Relative-residual tolerance for the CG solver.
    cg_max_iter : int
        Maximum number of CG iterations.

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``
    """
    if solver == "exact":
        return _weighted_lstsq_exact(G, d, w, regularization=regularization)
    if solver == "cg":
        return _weighted_lstsq_cg(
            G,
            d,
            w,
            regularization=regularization,
            tol=cg_tol,
            max_iter=cg_max_iter,
        )
    # solver == "auto"
    nparam = G.shape[1]
    npixels = d.shape[1]
    gtwg_bytes = nparam * nparam * npixels * G.dtype.itemsize
    if gtwg_bytes <= 1e9:  # normal-equation tensor up to ~1 GiB
        return _weighted_lstsq_exact(G, d, w, regularization=regularization)
    return _weighted_lstsq_cg(
        G,
        d,
        w,
        regularization=regularization,
        tol=cg_tol,
        max_iter=cg_max_iter,
    )


# ---------------------------------------------------------------------------
# Optimised solvers for uniform per-pixel weights  (w[i,j] ≡ mask[j])
#
# When MAD outlier detection is disabled the per-pixel weights are identical
# across all interferograms for a given pixel.  In that case the normal
# equations factorise and the design matrix *G* can be collapsed to its
# Gramian ``GᵀG`` (``nparam × nparam``) and transposed form ``Gᵀ``
# (``nparam × nifg``), avoiding the per-pixel expansion of *G*.
# ---------------------------------------------------------------------------


def _weighted_lstsq_exact_uniform(
    GTG: cp.ndarray,  # (nparam, nparam)
    GT: cp.ndarray,  # (nparam, nifg)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels) — uniform across axis 0
    regularization: float = 0.0,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` when weights are uniform across IFGs.

    When ``w[i, j] = mask[j]`` for all interferograms *i*, the per-pixel
    system matrix ``Gᵀ W G`` collapses to ``mask[j] · GᵀG``.  For a binary
    mask (value 1 on valid pixels) the solution for all valid pixels is

    .. math::

        x = (GᵀG + λ I)^{-1} @ Gᵀ d,

    computed as a single ``(nparam, nparam) @ (nparam, npixels)`` matmul.
    This avoids materializing the ``(npixels, nparam, nparam)`` tensor
    entirely.

    Parameters
    ----------
    GTG : cp.ndarray, shape ``(nparam, nparam)``
        Precomputed ``Gᵀ G``.
    GT : cp.ndarray, shape ``(nparam, nifg)``
        Precomputed ``Gᵀ``.
    d : cp.ndarray, shape ``(nifg, npixels)``
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel weights, identical across axis 0 (``w[0, :]`` is the
        mask for all interferograms).
    regularization : float
        Tikhonov factor λ added to the diagonal.

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``
    """
    nparam = GTG.shape[0]
    # Binary mask: mask[j] ∈ {0, 1} for all pixels
    mask = w[0, :]  # (npixels,)
    active = mask > 0.0
    n_active = int(cp.sum(active))
    if n_active == 0:
        return cp.zeros((nparam, d.shape[1]), dtype=cp.float32)

    # Regularise the Gramian
    GTG_reg = GTG.copy()
    if regularization > 0:
        GTG_reg += float(regularization) * cp.eye(nparam, dtype=GTG.dtype)

    # Solve x = (GᵀG + λI)⁻¹ @ Gᵀ d  for valid pixels
    GTd = GT @ d[:, active]  # (nparam, n_active)
    x_active = cp.linalg.solve(GTG_reg, GTd)  # (nparam, n_active)

    x = cp.zeros((nparam, d.shape[1]), dtype=cp.float32)
    x[:, active] = x_active
    return x


def _weighted_lstsq_cg_uniform(
    GTG: cp.ndarray,  # (nparam, nparam)
    GT: cp.ndarray,  # (nparam, nifg)
    G2_diag: cp.ndarray,  # (nparam,) — diag of GᵀG , i.e. sum_i(G[i,:]²)
    d: cp.ndarray,  # (nifg, npixels)
    w: cp.ndarray,  # (nifg, npixels) — uniform across axis 0
    regularization: float = 0.0,
    tol: float = 1e-5,
    max_iter: int = 50,
) -> cp.ndarray:
    """Solve ``(Gᵀ W G + λ I) x = Gᵀ W d`` via CG when weights are uniform
    across interferograms.

    When ``w[i, j] = mask[j]``, the weight matrix is the scalar 1 on every
    active pixel, so the CG operator simplifies to a single Gramian matmul

    .. math::

        (Gᵀ W G) p = (GᵀG) @ p,

    the Jacobi preconditioner is identical for all pixels, and the
    right-hand side reduces to ``Gᵀ d``.  This is substantially faster than
    the general CG solver because it replaces two ``(nifg × nparam)``
    matmuls per iteration with one ``(nparam × nparam)`` matmul.

    Parameters
    ----------
    GTG : cp.ndarray, shape ``(nparam, nparam)``
        Precomputed ``Gᵀ G``.
    GT : cp.ndarray, shape ``(nparam, nifg)``
        Precomputed ``Gᵀ``.
    G2_diag : cp.ndarray, shape ``(nparam,)``
        Diagonal of ``Gᵀ G`` (sum of squared entries per parameter row).
    d : cp.ndarray, shape ``(nifg, npixels)``
    w : cp.ndarray, shape ``(nifg, npixels)``
        Per-pixel weights, identical across axis 0.
    regularization : float
    tol : float
    max_iter : int

    Returns
    -------
    x : cp.ndarray, shape ``(nparam, npixels)``
    """
    nparam = GTG.shape[0]
    npixels = d.shape[1]

    mask = w[0, :]  # (npixels,)
    active = mask > 0.0
    n_active = int(cp.sum(active))
    if n_active == 0:
        return cp.zeros((nparam, npixels), dtype=cp.float32)

    d_a = d[:, active]  # (nifg, n_active)

    # Right-hand side: Gᵀ d  (w_a ≡ 1 for active pixels)
    b = GT @ d_a  # (nparam, n_active)
    del d_a

    # Jacobi preconditioner: identical for all active pixels
    diag = G2_diag[:, None]  # (nparam, 1)
    diag_floor = cp.maximum(cp.max(diag) * 1e-7, 1e-12)
    m_inv = cp.where(
        diag + regularization > diag_floor,
        1.0 / cp.maximum(diag + regularization, diag_floor),
        cp.float32(1.0),
    ).astype(cp.float32)  # (nparam, 1)

    x = cp.zeros((nparam, n_active), dtype=cp.float32)
    r = b.copy()
    z = m_inv * r
    p = z.copy()
    rho = cp.sum(r * z, axis=0)

    # Preallocate buffer for Ap = GTG @ p  (replaces the two G/Gᵀ matmuls)
    Ap = cp.empty((nparam, n_active), dtype=cp.float32)

    r2_pixel = cp.sum(r * r, axis=0)
    best_r2_pixel = r2_pixel.copy()
    x_best = x.copy()

    r0_norm2_total = float(cp.sum(r2_pixel))
    tol_sq = (tol * tol) * r0_norm2_total + 1e-12

    reg_fp32 = cp.float32(regularization)
    eps_fp32 = cp.float32(1e-12)

    for _ in range(max_iter):
        r2_pixel = cp.sum(r * r, axis=0)
        improved = r2_pixel < best_r2_pixel
        best_r2_pixel = cp.where(improved, r2_pixel, best_r2_pixel)
        x_best = cp.where(improved[None, :], x, x_best)

        total_r2 = cp.sum(r2_pixel)
        if total_r2 <= tol_sq:
            break

        # Matrix-free operator: Ap = (GᵀG) @ p + λ p
        cp.matmul(GTG, p, out=Ap)
        if regularization > 0:
            Ap += reg_fp32 * p

        pAp = cp.sum(p * Ap, axis=0)
        safe = pAp > eps_fp32
        alpha = cp.where(safe, rho / cp.maximum(pAp, eps_fp32), cp.float32(0.0))

        x += alpha[None, :] * p
        r -= alpha[None, :] * Ap

        z = m_inv * r
        rho_new = cp.sum(r * z, axis=0)
        beta = cp.where(safe, rho_new / cp.maximum(rho, eps_fp32), cp.float32(0.0))
        p = z + beta[None, :] * p
        rho = rho_new

    x_out = cp.zeros((nparam, npixels), dtype=cp.float32)
    x_out[:, active] = x_best
    return x_out


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


def _phase_to_displacement(
    phase_rad: cp.ndarray,
    wvl: float,
) -> cp.ndarray:
    """Convert phase (radians) to line-of-sight displacement (meters)."""
    return phase_rad * float(wvl) / (4.0 * cp.pi)


def _displacement_time_series(
    x: cp.ndarray,  # (nparam, npixels)
    days: cp.ndarray | None,  # (ndate,)
    dt: NDArray[np.float32] | None,  # (ndate - 1,)
    wvl: float,
    solver_type: str,
    seasonal_terms: int = 0,
) -> cp.ndarray:
    """Cumulative displacement (meters) from model parameters *x*.

    The per-date basis is built according to the model type: ``"linear"``
    projects a constant velocity onto the day axis, ``"seasonal"`` adds
    annual (and sub-annual) harmonics, and ``"ls"`` accumulates the
    per-interval velocities with a cumulative sum.

    Parameters
    ----------
    x : cp.ndarray, shape ``(nparam, npixels)``
        Model parameters in rad / day.
    days : cp.ndarray or None, shape ``(ndate,)``
        Days since the first acquisition.  Unused for ``"ls"``.
    dt : ndarray or None, shape ``(ndate - 1,)``
        Interval durations in days, used only for ``"ls"``.
    wvl : float
        Radar wavelength in meters.
    solver_type : ``"linear"`` | ``"seasonal"`` | ``"ls"``
    seasonal_terms : int
        Number of harmonic pairs for the ``"seasonal"`` model.

    Returns
    -------
    ts : cp.ndarray, shape ``(ndate, npixels)``
        Cumulative displacement in meters.
    """
    if solver_type == "ls":
        incr = x * cp.array(dt, dtype=cp.float32)[:, None]  # (ndate - 1, npixels)
        disp_rad = cp.concatenate(
            [cp.zeros((1, x.shape[1]), dtype=cp.float32), cp.cumsum(incr, axis=0)],
            axis=0,
        )  # (ndate, npixels)
    else:
        basis = cp.ones((len(days), x.shape[0]), dtype=cp.float32)
        basis[:, 0] = days.astype(cp.float32)
        if solver_type == "seasonal":
            T = 365.25
            for k in range(1, seasonal_terms + 1):
                omega = 2.0 * np.pi * k / T
                basis[:, 1 + 2 * (k - 1)] = cp.sin(omega * days)
                basis[:, 2 + 2 * (k - 1)] = cp.cos(omega * days)
        disp_rad = basis @ x  # (ndate, npixels)
    return _phase_to_displacement(disp_rad, wvl)


# ---------------------------------------------------------------------------
# Per-chunk solver functions  (called via dask ``map_blocks``)
# ---------------------------------------------------------------------------


def _stack_block(
    unw_chunk: NDArray[np.float32],  # (nifg, chunk_rows, chunk_cols)
    B: NDArray[np.float32] = None,  # (nifg, ndate - 1)
    ref_phase: NDArray[np.float32] = None,  # (nifg,)
    wvl: float = 0.055465763,
    mad_scalar: float = 0.0,
    mask: NDArray[np.bool_] | None = None,  # (nrow, ncol)
    date_mask: NDArray[np.float32] | None = None,
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

    d_date_mask = cp.array(date_mask, dtype=cp.float32)
    if mad_scalar > 0:
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_date_mask, d_tempbl, mad_scalar)
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
    wvl: float = 0.055465763,
    output_dim: str = "2d",
    days: NDArray[np.float32] | None = None,  # (ndate,)
    dt: NDArray[np.float32] | None = None,  # (ndate - 1,) or None
    mad_scalar: float = 0.0,
    regularization: float = 0.0,
    seasonal_terms: int = 0,
    solver_type: str = "linear",
    cg_solver: Literal["auto", "exact", "cg"] = "auto",
    cg_tol: float = 1e-5,
    cg_max_iter: int = 50,
    date_mask: NDArray[np.float32] | None = None,
    **kwargs: Any,  # absorbs model-irrelevant keys forwarded by ``_sbas_block``
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
    date_mask: ndarray, shape ``(ndate, nifg)``

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
        d_tempbl = cp.sum(d_B, axis=1)  # (nifg,)
        d_date_mask = cp.array(date_mask, dtype=cp.float32)
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_date_mask, d_tempbl, mad_scalar)
        weights = weights * d_ifg_mask.astype(cp.float32)  # (nifg, npixels)

        # Per-pixel weights → general solver
        x = _weighted_lstsq(
            d_G,
            d_unw,
            weights,
            regularization=regularization,
            solver=cg_solver,
            cg_tol=cg_tol,
            cg_max_iter=cg_max_iter,
        )
    else:
        # Uniform weights across IFGs (w[i,j] = mask[j]).
        # Precompute the Gramian GᵀG and transposed Gᵀ so the solver can
        # avoid expanding G across the pixel dimension.
        d_GTG = d_G.T @ d_G  # (nparam, nparam)
        d_GT = d_G.T  # (nparam, nifg)
        d_G2_diag = cp.sum(d_G * d_G, axis=0)  # (nparam,)
        del d_G  # free the full design matrix

        nparam = d_GTG.shape[0]
        if cg_solver == "exact":
            use_exact = True
        elif cg_solver == "cg":
            use_exact = False
        else:  # "auto" — same ~1 GiB tensor threshold as _weighted_lstsq
            use_exact = nparam * nparam * npixels * 4 <= 1e9

        if use_exact:
            x = _weighted_lstsq_exact_uniform(
                d_GTG,
                d_GT,
                d_unw,
                weights,
                regularization=regularization,
            )
        else:
            x = _weighted_lstsq_cg_uniform(
                d_GTG,
                d_GT,
                d_G2_diag,
                d_unw,
                weights,
                regularization=regularization,
                tol=cg_tol,
                max_iter=cg_max_iter,
            )

    if output_dim == "2d":
        v_rad_per_day = x[0:1, :]  # (1, npixels)
        v_m_per_yr = _phase_to_displacement(v_rad_per_day, wvl) * 365.25  # (1, npixels)
        v_m_per_yr = v_m_per_yr[0, :]  # (npixels,)
        v_m_per_yr[~mask_flat] = np.nan
        return cp.asnumpy(v_m_per_yr).reshape(chunk_rows, chunk_cols)

    # 3D: cumulative displacement time series  (ndate, npixels)
    if solver_type not in ("linear", "seasonal", "ls"):
        raise ValueError(f"Unknown solver_type: {solver_type}")
    ts = _displacement_time_series(x, d_days, dt, wvl, solver_type, seasonal_terms)

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
    GTG_inv: NDArray[np.float32],  # (nparam, nparam)
    l1_rho: float,
    l1_alpha: float,
    l1_max_iter: int,
    date_mask: NDArray[np.float32] | None = None,
    **kwargs: Any,  # absorbs model-irrelevant keys forwarded by ``_sbas_block``
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
    date_mask: ndarray, shape ``(ndate, nifg)``
        Interferogram indices for each date, must be provided when mad_scalar > 0.

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
        d_tempbl = cp.sum(d_B, axis=1)  # (nifg)
        d_date_mask = cp.array(date_mask, dtype=cp.float32)  # (ndate, nifg)
        # (nifg, npixels)
        d_ifg_mask = _compute_ifg_outlier_mask(d_unw, d_date_mask, d_tempbl, mad_scalar)
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
    ts = _displacement_time_series(x, None, dt, wvl, "ls")  # (ndate, npixels)
    ts[:, ~mask_flat] = np.nan
    ts = cp.asnumpy(ts)
    # (ndate, chunk_rows, chunk_cols)
    return ts.reshape(ts.shape[0], chunk_rows, chunk_cols)


# ---------------------------------------------------------------------------
# Block wrapper for dask ``map_blocks``
#
#   ``_sbas_block``  → dispatches on ``solver_type`` to ``_sbas_solver_chunk``
#                      (least-squares models; 2d velocity or 3d displacement)
#                      or ``_sbas_l1_chunk`` (L1-ADMM model, always 3d).
#   ``_stack_block``  → always 2d (mean velocity).
# ---------------------------------------------------------------------------


def _sbas_block(
    unw_chunk: NDArray[np.float32],  # (nifg, chunk_rows, chunk_cols)
    block_info: dict | None = None,
    **kwargs: Any,
) -> NDArray[np.float32]:
    """dask ``map_blocks`` wrapper that dispatches to an SBAS chunk solver.

    Resolves the per-chunk mask from ``block_info`` and forwards the keyword
    arguments to ``_sbas_solver_chunk`` (``solver_type`` in ``"linear"``,
    ``"seasonal"``, ``"ls"``) or ``_sbas_l1_chunk`` (``solver_type="l1"``).
    """
    assigned_gpu = GPU_POOL.get()
    try:
        chunk_shape = (unw_chunk.shape[1], unw_chunk.shape[2])
        kwargs["mask_chunk"] = _get_mask_chunk(
            block_info, kwargs.pop("mask", None), chunk_shape
        )
        solver_type = kwargs.pop("solver_type", "linear")
        if solver_type == "l1":
            return _sbas_l1_chunk(unw_chunk, **kwargs)
        kwargs["solver_type"] = solver_type
        return _sbas_solver_chunk(unw_chunk, **kwargs)
    finally:
        GPU_POOL.put(assigned_gpu)


# ---------------------------------------------------------------------------
# Sequential time-series solver (no MAD, no 3-D stack)
# ---------------------------------------------------------------------------


def _sequential_time_series_2d(
    unw_files: Sequence[str],
    mask: NDArray[np.bool_],
    out_path: Path,
    nrow: int,
    ncol: int,
    temp_bl: NDArray[np.float32],
    method: Literal["stack", "sbas_linear"],
    ref_point: Tuple[int, int] | None = None,
    ref_win: Tuple[int, int] = (11, 11),
    wvl: float = 0.055465763,
    row_chunk_size: int | None = None,
    metadata: Dict[str, Any] | None = None,
) -> None:
    """Compute mean velocity by sequentially reading unwrapped interferograms.

    This avoids loading the full 3-D phase stack into memory and skips MAD
    outlier filtering.  It is used when ``mad_scalar == 0`` and the time series
    method is ``"stack"`` or ``"sbas_linear"`` — in both cases the per-pixel
    solution reduces to a weighted sum and a scalar division, so a full
    least-squares solve per pixel is unnecessary.

    Parameters
    ----------
    unw_files : Sequence[str]
        Paths to unwrapped interferograms (binary float32, shape ``(nrow, ncol)``).
    mask : ndarray, shape ``(nrow, ncol)``
        Boolean mask where ``True`` indicates a valid pixel.
    out_path : Path
        Output zarr directory.
    nrow : int
        Number of rows per interferogram.
    ncol : int
        Number of columns per interferogram.
    temp_bl : ndarray, shape ``(nifg,)``
        Total temporal baseline (days) per interferogram — the sum of each
        row of the velocity-integration matrix **B**.
    wvl : float
        Radar wavelength in meters.
    ref_point : (row, col)
        Reference pixel coordinates (0-indexed).
    ref_win : (row_half, col_half)
        Reference window half-sizes in pixels.
    method : ``"stack"`` | ``"sbas_linear"``
        - ``"stack"``:  ``v = sum(phase_corrected) / sum(temp_bl)``
        - ``"sbas_linear"``:  ``v = sum(temp_bl * phase_corrected) / sum(temp_bl²)``
    row_chunk_size : int or None
        Number of rows to process at a time.  If *None*, a chunk size is
        chosen to keep the working set near 1 GB.
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    nifg = len(unw_files)

    # --- Validate reference point -----------------------------------------
    if ref_point is not None:
        if (
            ref_point[0] < 0
            or ref_point[0] >= nrow
            or ref_point[1] < 0
            or ref_point[1] >= ncol
        ):
            raise ValueError(
                f"Reference point ({ref_point[0]}, {ref_point[1]}) out of boundary."
            )
        if not mask[ref_point[0], ref_point[1]]:
            raise RuntimeError(
                f"Reference point ({ref_point[0]}, {ref_point[1]}) is masked out."
            )
        if ref_win[0] < 0 or ref_win[1] < 0:
            raise ValueError("Reference window size must be positive.")

        # --- Compute reference phase ------------------------------------------
        half_row_win = (ref_win[0] + 1) // 2
        half_col_win = (ref_win[1] + 1) // 2
        top = int(np.maximum(0, ref_point[0] - half_row_win + 1))
        bottom = int(np.minimum(nrow, ref_point[0] + half_row_win))
        left = int(np.maximum(0, ref_point[1] - half_col_win + 1))
        right = int(np.minimum(ncol, ref_point[1] + half_col_win))
        logger.info(
            "Reference window: left=%d, right=%d, top=%d, bottom=%d",
            left,
            right,
            top,
            bottom,
        )
        ref_win_mask = mask[top:bottom, left:right]
        ref_phase = np.empty(nifg, dtype=np.float32)
        for i, f in enumerate(unw_files):
            data = np.memmap(f, dtype=np.float32, mode="r", shape=(nrow, ncol))
            ref_tube = np.array(data[top:bottom, left:right])
            ref_phase[i] = np.nanmean(ref_tube * ref_win_mask)
    else:
        ref_phase = np.zeros(nifg, dtype=np.float32)

    # --- Precompute denominator -------------------------------------------
    if method == "stack":
        denominator = float(np.sum(temp_bl.astype(np.float64)))
    elif method == "sbas_linear":
        denominator = float(np.sum(temp_bl.astype(np.float64) ** 2))
    else:
        raise ValueError(f"Unsupported method for sequential solver: {method!r}")

    logger.info("Denominator: %.3f (method=%s)", denominator, method)

    # --- Row chunk size ---------------------------------------------------
    if row_chunk_size is None:
        row_chunk_size = nrow
    logger.debug("Row chunk size: %d", row_chunk_size)

    # --- Prepare output ---------------------------------------------------
    out_path = Path(out_path)
    if out_path.exists():
        shutil.rmtree(out_path)
    out_path.mkdir(parents=True, exist_ok=True)

    root = zarr.open(
        str(out_path),
        mode="w",
        shape=(nrow, ncol),
        chunks=(row_chunk_size, ncol),
        dtype=np.float32,
    )

    # --- Process row chunks -----------------------------------------------
    scale = wvl / (4.0 * np.pi) * 365.25  # rad / day  ->  m / yr

    for row_start in range(0, nrow, row_chunk_size):
        row_end = min(row_start + row_chunk_size, nrow)
        chunk_rows = row_end - row_start
        logger.debug("Processing rows [%d:%d] of %d", row_start, row_end, nrow)

        # Accumulate weighted phase in float64 to preserve precision
        numerator = np.zeros((chunk_rows, ncol), dtype=np.float64)

        for i, f in enumerate(unw_files):
            data = np.memmap(f, dtype=np.float32, mode="r", shape=(nrow, ncol))
            chunk = np.array(data[row_start:row_end, :])
            chunk -= ref_phase[i]

            if method == "stack":
                numerator += chunk.astype(np.float64)
            else:  # sbas_linear
                numerator += temp_bl[i] * chunk.astype(np.float64)

        # velocity in m / yr
        velocity = (numerator / (denominator + 1e-6)) * scale

        # Apply mask
        chunk_mask = mask[row_start:row_end, :]
        velocity[~chunk_mask] = np.nan

        root[row_start:row_end, :] = velocity.astype(np.float32)

    # --- Store metadata ---------------------------------------------------
    if metadata:
        for key, value in metadata.items():
            _store_attr(root, key, value)

    logger.info("Sequential time series computation complete.")


# ---------------------------------------------------------------------------
# VRAM-aware row chunk estimation
# ---------------------------------------------------------------------------


def _compute_ts_row_chunk(
    nrow: int,
    ncol: int,
    nifg: int,
    nparam: int,
    ndate: int,
    solver_type: str,
    cg_solver: str = "auto",
    mad_scalar: float = 0.0,
    output_dim: str = "2d",
    max_vram_gb: float | None = None,
) -> int:
    """Compute a safe row-chunk size for time-series inversion.

    Estimates the peak GPU memory (bytes) per row based on the inversion
    method, solver type, and optional MAD outlier detection, then divides the
    available VRAM budget to determine a row count that keeps the working set
    within GPU memory limits.

    The per-row memory model counts every per-pixel array that coexists at the
    solver's peak allocation point (input phase, weights, solver-specific
    working buffers, MAD intermediate arrays, and the output displacement
    tensor for 3-D output), then applies a 2× safety margin for CuPy
    temporaries and allocator overhead.

    Parameters
    ----------
    nrow : int
        Total number of image rows.
    ncol : int
        Number of image columns.
    nifg : int
        Number of interferograms.
    nparam : int
        Number of model parameters (columns of the design matrix *G*).
        For the ``"stack"`` solver this value is ignored.
    ndate : int
        Number of acquisition dates.
    solver_type : str
        One of ``"stack"``, ``"linear"``, ``"seasonal"``, ``"ls"``, ``"l1"``.
    cg_solver : str
        Least-squares solver dispatch: ``"auto"``, ``"exact"``, or ``"cg"``.
        When ``"auto"`` the decision uses the same ``~1 GiB`` normal-equation
        tensor threshold as :func:`_weighted_lstsq`, evaluated at full-image
        scale.  Ignored for ``"stack"`` and ``"l1"``.
    mad_scalar : float
        Values > 0 enable MAD outlier detection, which adds intermediate
        per-date and per-ifg boolean arrays on the GPU.
    output_dim : str
        ``"2d"`` (velocity only) or ``"3d"`` (displacement time series).
    max_vram_gb : float or None
        Maximum GPU VRAM budget in gigabytes.  When *None*, queries the
        GPU with the most total memory via :func:`~s1proc.utils._query_gpu_info`.

    Returns
    -------
    int
        Safe number of rows per chunk, clamped to ``[1, nrow]``.
    """
    from s1proc.utils import _query_gpu_info

    if max_vram_gb is None:
        gpu_info = _query_gpu_info()
        max_vram_gb = max(
            gpu_info[i]["total_vram_gb"] for i in range(gpu_info["gpu_count"])
        )
    max_vram_bytes = int(max_vram_gb * 1024**3)

    # ---- count per-pixel float32 arrays at peak memory ----------------------
    # each "unit" = one float32 value per pixel = 4 bytes
    f32_units = 0

    # common input: d_unw + weights  (2 arrays, nifg slices each per pixel)
    f32_units += 2 * nifg

    # MAD outlier detection
    if mad_scalar > 0:
        # v: (ndate,) per pixel
        f32_units += ndate
        # bad_dates (ndate,) + d_ifg_mask (nifg,) — bool, count as ~¼ float32
        f32_units += (ndate + nifg) // 4
        # inside _compute_ifg_outlier_mask: temp `v - medv` before median
        f32_units += ndate

    # solver-specific working set
    if solver_type == "stack":
        # _stack_block: d_filtered_unw copy when MAD is active; otherwise the
        # fast path bypasses time_series_solver entirely, so MAD is always on.
        f32_units += nifg

    elif solver_type == "l1":
        # _weighted_l1_admm: x (nparam), z/u/kappa_scale (3×nifg),
        # q (nparam), Ax_hat (nifg), z_old (nifg)
        f32_units += (2 * nparam + 5 * nifg) * 1.5

    else:
        # "linear", "seasonal", "ls" — all go through _weighted_lstsq
        if mad_scalar > 0:
            # Per-pixel weights → general solver.
            # Decide exact vs CG at full-image scale for the "auto" case.
            if cg_solver == "exact":
                use_exact = True
            elif cg_solver == "cg":
                use_exact = False
            else:  # "auto"
                gtwg_bytes_full = nparam * nparam * nrow * ncol * 4
                use_exact = gtwg_bytes_full <= 1e9

            if use_exact:
                # GTWG + A: 2 × nparam²; GTWd + B + x: 3 × nparam
                # (plus CuPy temporaries from the einsum)
                f32_units += 6 * nparam * nparam + 3 * nparam
            else:
                # w_a copy (nifg) + tmp (nifg) + x, r, z, p, b, diag, m_inv,
                # Ap, x_best, x_out (10 × nparam), plus misc temporaries
                f32_units += int((2 * nifg + 10 * nparam) * 1.5)
        else:
            # Uniform weights across IFGs → optimised Gramian-based solvers.
            # The full G matrix is collapsed to GTG (nparam², shared) and
            # GT (nparam × nifg, shared); per-pixel arrays drop substantially.
            if cg_solver == "exact":
                use_exact = True
            elif cg_solver == "cg":
                use_exact = False
            else:  # "auto"
                gtwg_bytes_full = nparam * nparam * nrow * ncol * 4
                use_exact = gtwg_bytes_full <= 1e9

            if use_exact:
                # _weighted_lstsq_exact_uniform: GTd (nparam) + x (nparam)
                # The (nparam, nparam) matrices are shared, not per-pixel.
                f32_units += 2 * nparam
            else:
                # _weighted_lstsq_cg_uniform: b, x, r, z, p, Ap, x_best, x_out
                # (8 × nparam).  No nifg-sized per-pixel arrays.
                f32_units += 8 * nparam

    # output reconstruction (3-D only)
    if output_dim == "3d":
        f32_units += ndate  # ts array

    # ---- convert to bytes per row -------------------------------------------
    bytes_per_pixel = f32_units * 4
    safety_factor = 2.0
    bytes_per_row = int(ncol * bytes_per_pixel * safety_factor)

    if bytes_per_row <= 0:
        return nrow

    row_chunk = max(1, int(max_vram_bytes / bytes_per_row))
    return max(1, min(row_chunk, nrow))


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
    metadata : dict or None
        Attributes stored on the output zarr group.
    """
    import warnings

    import dask
    from dask.array.core import PerformanceWarning

    if solver_kwargs is None:
        solver_kwargs = {}

    unw_list = IfgList(unw_files)
    nifg = unw_list.nifg
    logger.info(
        "Time series analysis with %d interferograms, %d unique dates",
        len(unw_files),
        unw_list.ndate,
    )

    if row_chunk_size is None:
        solver_type = solver_kwargs.get("solver_type", "ls")
        _G = solver_kwargs.get("G")
        nparam = _G.shape[1] if _G is not None else 0
        ndate = unw_list.ndate
        mad_scalar = float(solver_kwargs.get("mad_scalar", 0.0))
        cg_solver = solver_kwargs.get("cg_solver", "auto")
        row_chunk_size = _compute_ts_row_chunk(
            nrow=nrow,
            ncol=ncol,
            nifg=nifg,
            nparam=nparam,
            ndate=ndate,
            solver_type=solver_type,
            cg_solver=cg_solver,
            mad_scalar=mad_scalar,
            output_dim=output_dim,
        )
    col_chunk_size = ncol
    _G = solver_kwargs.get("G")
    _nparam = _G.shape[1] if _G is not None else 0
    logger.info(
        "Row chunk size: %d (method=%s, nifg=%d, nparam=%d).",
        row_chunk_size,
        solver_kwargs.get("solver_type", "ls"),
        nifg,
        _nparam,
    )
    logger.info("Column chunk size: %d.", col_chunk_size)

    logger.info("Creating a virtual Zarr stack")
    mapper = create_virtual_stack(
        unw_files, np.float32, nrow, ncol, row_chunk_size, new_axis=0
    )
    unw_stack = da.from_zarr(mapper)  # (nifg, nrow, ncol)

    # Load mask
    logger.info("Load mask from %s", mask_file)
    if mask_file is not None:
        mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
    else:
        mask = np.ones((nrow, ncol), dtype=np.bool_)

    logger.info("Calculate reference phase")
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
    ref_phase = np.nanmean(ref_tube * ref_win_mask[None, :, :], axis=(1, 2))  # (nifg,)

    # --- Build common kwargs for map_blocks ---------------------------------
    # ``solver_kwargs`` already carries every per-model parameter; only the
    # chunk-dependent values are computed here.
    date_mask = unw_list.get_date_mask()
    common_kwargs = dict(solver_kwargs)
    common_kwargs.update(
        ref_phase=ref_phase,
        mask=mask,
        date_mask=date_mask,
        output_dim=output_dim,
        block_info=True,
    )

    # --- Run the dask computation -------------------------------------------
    if output_dim == "2d":
        ndate_out = 1
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
        root = zarr.open(
            str(out_path),
            mode="w",
            shape=(nrow, ncol),
            chunks=(result.chunks[0][0], result.chunks[1][0]),
            dtype=np.float32,
        )

        for start in tqdm(range(0, nrow, row_chunk_size), desc="Solve(2D)"):
            end = min(start + row_chunk_size, nrow)
            logger.debug("Computing batch [%d:%d] of %d", start, end, nrow)
            sub_da = result[start:end, :]

            with (
                dask.config.set({
                    "array.chunk-size": result.chunks[0][0] * result.chunks[1][0] * 4
                }),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter("ignore", category=PerformanceWarning)
                da.to_zarr(
                    sub_da,
                    root,
                    region=(
                        slice(start, end),
                        slice(None),
                    ),
                )

            del sub_da
            gc.collect()
        del result
        gc.collect()

        root = zarr.open(out_path, mode="a")
        if metadata:
            for key, value in metadata.items():
                _store_attr(root, key, value)
    else:
        total_days = float(solver_kwargs.get("days", np.zeros(1))[-1])
        store = str(out_path)
        logger.info("Writing displacement time series to %s", out_path)

        # Write 3D displacement
        root = zarr.open(
            str(out_path),
            mode="w",
            shape=(ndate_out, nrow, ncol),
            chunks=(result.chunks[0][0], result.chunks[1][0], result.chunks[2][0]),
            dtype=np.float32,
        )

        for start in tqdm(range(0, nrow, row_chunk_size), desc="Solve(3D)"):
            end = min(start + row_chunk_size, nrow)
            logger.debug("Computing batch [%d:%d] of %d", start, end, nrow)
            sub_da = result[:, start:end, :]

            with (
                dask.config.set({
                    "array.chunk-size": result.chunks[0][0]
                    * result.chunks[1][0]
                    * result.chunks[2][0]
                    * 4
                }),
                warnings.catch_warnings(),
            ):
                warnings.simplefilter("ignore", category=PerformanceWarning)
                da.to_zarr(
                    sub_da,
                    root,
                    region=(
                        slice(None),
                        slice(start, end),
                        slice(None),
                    ),
                )

            del sub_da
            gc.collect()
        del result
        gc.collect()

        root = zarr.open(store, mode="a")
        if metadata:
            for key, value in metadata.items():
                _store_attr(root, key, value)
        root.attrs["total_days"] = total_days

    logger.info("Time series computation complete.")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_time_series(
    unw_path: Path | str | None = None,
    outpath: Path | str | None = None,
    config: str = "config.yaml",
    verbose: bool = False,
) -> None:
    """Run time series analysis.

    Parameters
    ----------
    unw_path : Path or str or None
        Directory containing unwrapped interferograms.  When *None* the
        paths from the configuration are used.
    outpath : Path or str or None
        Output directory.  Falls back to ``io.time_series_path``.
    config : str
        Path to the YAML configuration file.
    verbose: bool
        If True, set logging level to DEBUG

    Notes
    -----
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
    """
    from s1proc._config import load_config
    from s1proc.geocoordinates import GeoCoordinates
    from s1proc.utils import get_files

    if verbose:
        set_logging_level(logger, "DEBUG")
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

    # --- Sequential fast path (no MAD, no 3-D stack) --------------------------
    if method in ("stack", "sbas_linear") and mad_scalar == 0.0:
        logger.info(
            "Using sequential solver for %s (mad_scalar=0) — "
            "skipping 3-D phase stack load.",
            method,
        )
        # Load mask
        if mask_file is not None:
            mask = np.fromfile(mask_file, dtype=np.bool_).reshape(nrow, ncol)
        else:
            mask = np.ones((nrow, ncol), dtype=np.bool_)

        temp_bl = B.sum(axis=1).astype(np.float32)

        _sequential_time_series_2d(
            unw_files=unw_files,
            mask=mask,
            out_path=Path(outpath) / "time_series.zarr",
            nrow=nrow,
            ncol=ncol,
            ref_point=reference_point,
            ref_win=reference_win,
            temp_bl=temp_bl,
            wvl=pcfg.wavelength,
            method=method,
            metadata={
                "method": method,
                "dates": list(unw_list.dates),
                "mask_file": str(mask_file) if mask_file else "none",
                "reference_point": list(reference_point),
                "wavelength": float(pcfg.wavelength),
                "mad_scalar": 0.0,
            },
        )
        return

    solver_kwargs: Dict[str, Any] = dict(
        wvl=pcfg.wavelength,
        days=unw_list.date2days().astype(np.float32),  # (ndate,)
        mad_scalar=mad_scalar,
        regularization=0.0,  # default; overridden below for methods that need it
        cg_solver=tcfg.parameters.cg_solver,
        cg_tol=tcfg.parameters.cg_tol,
        cg_max_iter=tcfg.parameters.cg_max_iter,
    )

    # ------- Dispatch -------------------------------------------------------
    if method == "stack":
        output_dim: Literal["2d", "3d"] = "2d"
        solver_func = _stack_block
        solver_kwargs.update(
            B=B, ndate_out=1, seasonal_terms=0, regularization=0.0, solver_type="stack"
        )
        logger.info("Stacking velocity (mad_scalar=%.1f)", mad_scalar)

    elif method == "sbas_linear":
        output_dim = "2d"
        solver_func = _sbas_block
        solver_kwargs.update(
            G=build_design_matrix_linear(unw_list),  # (nifg, 1)
            B=B,
            dt=unw_list.date_interval(drop_first_date=True).astype(
                np.float32
            ),  # (ndate - 1,)
            ndate_out=1,
            seasonal_terms=0,
            regularization=0.0,
            solver_type="linear",
        )
        logger.info("SBAS linear (mad_scalar=%.1f)", mad_scalar)

    elif method == "sbas_seasonal":
        output_dim = "3d"
        seasonal_terms = tcfg.parameters.seasonal_terms
        reg = config_reg * reg_scale
        solver_func = _sbas_block
        solver_kwargs.update(
            G=build_design_matrix_seasonal(  # (nifg, 1 + 2*seasonal_terms)
                unw_list, seasonal_terms=seasonal_terms
            ),
            B=B,
            dt=None,
            ndate_out=unw_list.ndate,
            seasonal_terms=seasonal_terms,
            regularization=reg,
            solver_type="seasonal",
        )
        logger.info(
            "SBAS seasonal (terms=%d, reg=%.3e, mad_scalar=%.1f)",
            seasonal_terms,
            reg,
            mad_scalar,
        )

    elif method in ("sbas_ls", "sbas_l1"):
        output_dim = "3d"
        G_ls = build_design_matrix_ls(unw_list)  # (nifg, ndate - 1)
        reg = config_reg * reg_scale
        solver_func = _sbas_block
        solver_kwargs.update(
            G=G_ls,
            B=B,
            dt=unw_list.date_interval(drop_first_date=True).astype(np.float32),
            ndate_out=unw_list.ndate,
            seasonal_terms=0,
            regularization=reg,
            solver_type="l1" if method == "sbas_l1" else "ls",
        )
        if method == "sbas_l1":
            # Precompute (Gᵀ G + λ I)⁻¹ for the ADMM x-update
            GTG = G_ls.astype(np.float64).T @ G_ls.astype(np.float64)
            GTG += np.eye(GTG.shape[0]) * reg
            solver_kwargs.update(
                GTG_inv=np.linalg.inv(GTG).astype(np.float32),
                l1_rho=tcfg.parameters.l1_rho,
                l1_alpha=tcfg.parameters.l1_alpha,
                l1_max_iter=tcfg.parameters.l1_max_iter,
            )
            logger.info(
                "SBAS L1-ADMM (reg=%.3e, rho=%.2f, iter=%d, mad_scalar=%.1f)",
                reg,
                tcfg.parameters.l1_rho,
                tcfg.parameters.l1_max_iter,
                mad_scalar,
            )
        else:
            # When MAD is enabled, weights become per-pixel and the exact
            # solver would materialize an (nparam, nparam, npixels) tensor
            # that is both wasteful and likely to OOM for large nparam.
            if mad_scalar > 0:
                solver_kwargs["cg_solver"] = "cg"
            logger.info(
                "SBAS plain least-squares (reg=%.3e, mad_scalar=%.1f, cg=%s)",
                reg,
                mad_scalar,
                solver_kwargs.get("cg_solver", "auto"),
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
            "cg_max_iter": int(tcfg.parameters.cg_max_iter),
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
