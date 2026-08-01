"""MIDAS (Median Interannual Difference Adjusted for Swaps) algorithm.

MIDAS is a robust GPS velocity estimator that computes all pairwise
velocity estimates between observations separated by approximately one
year. The median of these velocities is insensitive to seasonal
variation, outliers, and step discontinuities.

Reference
---------
Blewitt, G., Kreemer, C., Hammond, W. C., & Gazeaux, J. (2016).
MIDAS robust trend estimator for accurate GPS station velocities
without step detection.
Journal of Geophysical Research: Solid Earth, 121(3), 2054-2068.
https://doi.org/10.1002/2015JB012552
"""

import numpy as np
import pandas as pd

# Default tolerance window around 1 year, in days.
# The original MIDAS paper uses ~30-60 days.
DEFAULT_WINDOW_DAYS = 30
# Number of MAD multiples for outlier rejection.
DEFAULT_MAD_THRESHOLD = 3.0


def midas_velocity(
    dt: pd.Series,
    displacement: np.ndarray,
    window_days: float = DEFAULT_WINDOW_DAYS,
    mad_threshold: float = DEFAULT_MAD_THRESHOLD,
) -> float:
    """Estimate GPS station velocity using the MIDAS algorithm.

    Parameters
    ----------
    dt : pandas.Series
        Datetime column from the GPS DataFrame (returned by
        :func:`load_station_data`).
    displacement : array-like
        Displacement values in cm corresponding to each timestamp.
    window_days : float
        Half-width of the acceptance window around 1 year, in days.
        A pair (i, j) is used when its time separation lies within
        ``[365.25 - window_days, 365.25 + window_days]`` days.
    mad_threshold : float
        Number of median absolute deviations for outlier rejection.
        Velocity estimates farther than ``mad_threshold * MAD`` from the
        median are discarded before computing the final median.

    Returns
    -------
    float
        Estimated velocity in cm/yr.  Positive values indicate motion
        away from the satellite (for LOS) or in the positive component
        direction.

    Raises
    ------
    ValueError
        If fewer than 2 valid velocity pairs are found.

    Notes
    -----
    The algorithm:

    1. Converts datetimes to decimal years.
    2. For every pair of epochs separated by ~1 year (within the
       specified window), computes the velocity as
       ``(d[j] - d[i]) / (t[j] - t[i])``.
    3. Takes the median of all velocity estimates.
    4. Performs iterative MAD-based outlier rejection (typically 3 passes
       is sufficient for convergence).
    5. Returns the median of the cleaned velocity estimates, converted
       to cm/yr (multiplied by 365.25).
    """
    displacement = np.asarray(displacement, dtype=float)

    # Remove NaN / invalid entries from both arrays
    mask = np.isfinite(displacement)
    t_series = dt[mask].reset_index(drop=True)
    d_clean = displacement[mask]

    n = len(d_clean)
    if n < 4:
        raise ValueError(
            f"Need at least 4 valid data points to estimate velocity; got {n}"
        )

    # Convert datetime to decimal years
    t_decimal = t_series.apply(_to_decimal_year).to_numpy(dtype=float)

    # Time thresholds in years
    year_days = 365.25
    window_years = window_days / year_days
    t_low = 1.0 - window_years
    t_high = 1.0 + window_years

    # Collect all valid velocity estimates
    velocities = []
    for i in range(n):
        ti = t_decimal[i]
        di = d_clean[i]
        # Only consider j > i so we don't duplicate pairs
        for j in range(i + 1, n):
            dt_ij = t_decimal[j] - ti
            if t_low <= dt_ij <= t_high:
                velocities.append((d_clean[j] - di) / dt_ij)

    velocities = np.array(velocities)

    if len(velocities) < 2:
        raise ValueError(
            f"Only {len(velocities)} valid velocity pairs found. "
            "Try increasing window_days or check the data coverage."
        )

    # Iterative MAD-based outlier rejection
    for _ in range(3):
        medv = np.median(velocities)
        mad = np.median(np.abs(velocities - medv))
        if mad == 0:
            break
        sigma = mad * 1.4826  # scale to approximate standard deviation
        keep = np.abs(velocities - medv) < mad_threshold * sigma
        if np.all(keep):
            break
        velocities = velocities[keep]

    # Return median velocity in cm/yr
    # (the raw velocities are in displacement-units / year already since
    #  dt_ij is in years)
    return float(np.median(velocities))


def _to_decimal_year(ts: pd.Timestamp) -> float:
    """Convert a pandas Timestamp to decimal year."""
    year = ts.year
    start_of_year = pd.Timestamp(year=year, month=1, day=1)
    end_of_year = pd.Timestamp(year=year + 1, month=1, day=1)
    fraction = (ts - start_of_year) / (end_of_year - start_of_year)
    return year + fraction
