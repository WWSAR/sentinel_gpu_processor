import numpy as np
from numba import njit

RA = 6378137.0  # semi-major axis of Earth
RE2 = 0.0066943799901499996  # eccentricity of Earth ellipsoid
SOL = 299792458.0  # Speed of Light


def rotation3(d_theta, axis):
    r_theta = np.radians(d_theta)
    if axis == 1:
        return np.array(
            [
                [1, 0, 0],
                [0, np.cos(r_theta), np.sin(r_theta)],
                [0, -np.sin(r_theta), np.cos(r_theta)],
            ]
        )
    elif axis == 2:
        return np.array(
            [
                [np.cos(r_theta), 0, -np.sin(r_theta)],
                [0, 1, 0],
                [np.sin(r_theta), 0, np.cos(r_theta)],
            ]
        )
    elif axis == 3:
        return np.array(
            [
                [np.cos(r_theta), np.sin(r_theta), 0],
                [-np.sin(r_theta), np.cos(r_theta), 0],
                [0, 0, 1],
            ]
        )
    else:
        raise ValueError("axis must be 1, 2, or 3")


def geo_hdg(ll1, ll2, r_a=RA, r_e2=RE2):
    """
    Computes the heading along a geodesic for either an ellipitical or spherical
    earth given the initial latitude and longtitude and the final latitude and
    longitude.

    Notes: These results are based on the memo
    "Summary of Mocomp Referene Line Determination Study", IOM 3346-93-163
    and the paper
    "A Rigourous Non-iterative Procedure for Rapid Inverse Solution of Very Long
    Geodesics" by E. M. Sadano, Bulletine Geodesique 1958

    Adpated from Scott Hensley's Fortran code and isce2 python code

    Args:
        r_a: semi-major axis of Earth
        r_e2: eccentricity of Earth ellipsoid
        ll1: lat lon of the first point
        ll2: lat lon of the second point

    Returns:
        hdg: the azimuth at the first point

        (The azimuth is the heading measured clockwise from north)
        The definition of the inverse geodesic problem can be found at:
        https://geographiclib.sourceforge.io/2009-03/geodesic.html
        A figure that shows what the azimuth is:
        https://en.wikipedia.org/wiki/Geodesics_on_an_ellipsoid#/media/File:Geodesic_problem_on_an_ellipsoid.svg
    """
    if r_e2 == 0.0:
        return _spherical_hdg(ll1, ll2)
    else:
        return _ellipsoidal_hdg(ll1, ll2, r_a, r_e2)


def _spherical_hdg(ll1, ll2):
    r_sinlati = np.sin(np.radians(ll1[0]))
    r_coslati = np.cos(np.radians(ll1[0]))
    r_tanlatf = np.tan(np.radians(ll2[0]))

    r_t1 = np.radians(ll2[1]) - np.radians(ll1[1])
    if np.abs(r_t1) > np.pi:
        r_t1 = (2.0 * np.pi - np.abs(r_t1)) * np.copysign(1.0, -r_t1)
    r_sinlon = np.sin(r_t1)
    r_coslon = np.cos(r_t1)

    r_t2 = r_coslati * r_tanlatf - r_sinlati * r_coslon
    r_geohdg = np.arctan2(r_sinlon, r_t2)

    return r_geohdg


def _ellipsoidal_hdg(ll1, ll2, r_a=RA, r_e2=RE2):
    r_e = np.sqrt(r_e2)
    r_sqrtome2 = np.sqrt(1.0 - r_e2)
    r_f = 1.0 - r_sqrtome2
    r_ep = r_e * r_f / (r_e2 - r_f)
    r_n = r_f / r_e2
    r_k1 = (16.0 * r_e2 * r_n**2 + r_ep**2) / r_ep**2
    r_k2 = (16.0 * r_e2 * r_n**2) / (16.0 * r_e2 * r_n**2 + r_ep**2)
    r_k3 = (16.0 * r_e2 * r_n**2) / r_ep**2
    r_k4 = (16.0 * r_n - r_ep**2) / (16.0 * r_e2 * r_n**2 + r_ep**2)
    r_k5 = 16.0 / (r_e2 * (16.0 * r_e2 * r_n**2 + r_ep**2))

    r_tanlati = np.tan(np.radians(ll1[0]))
    r_tanlatf = np.tan(np.radians(ll2[0]))
    r_l = np.abs(np.radians(ll2[1]) - np.radians(ll1[1]))
    r_lsign = np.radians(ll2[1]) - np.radians(ll1[1])
    r_sinlon = np.sin(r_l)
    r_coslon = np.cos(r_l)

    r_tanbetai = r_sqrtome2 * r_tanlati
    r_tanbetaf = r_sqrtome2 * r_tanlatf

    r_cosbetai = 1.0 / np.sqrt(1.0 + r_tanbetai**2)
    r_cosbetaf = 1.0 / np.sqrt(1.0 + r_tanbetaf**2)
    r_sinbetai = r_tanbetai * r_cosbetai
    r_sinbetaf = r_tanbetaf * r_cosbetaf

    r_ac = r_sinbetai * r_sinbetaf
    r_bc = r_cosbetai * r_cosbetaf

    r_cosphi = r_ac + r_bc * r_coslon
    r_sinphi = np.copysign(1.0, r_sinlon) * np.sqrt(1.0 - min(r_cosphi**2, 1.0))

    r_phi = np.abs(np.arctan2(r_sinphi, r_cosphi))

    if r_a * np.abs(r_phi) > 1e-6:
        r_ca = (r_bc * r_sinlon) / r_sinphi
        r_cb = r_ca**2
        r_cc = (r_cosphi * (1.0 - r_cb)) / r_k1
        r_cd = (-2.0 * r_ac) / r_k1
        r_ce = -r_ac * r_k2
        r_cf = r_k3 * r_cc
        r_cg = r_phi**2 / r_sinphi

        r_x = (
            (r_phi * (r_k4 + r_cb) + r_sinphi * (r_cc + r_cd) + r_cg * (r_cf + r_ce))
            * r_ca
        ) / r_k5

        r_lambda = r_l + r_x

        r_sinlam = np.sin(r_lambda)
        r_coslam = np.cos(r_lambda)

        r_t2 = r_tanbetaf * r_cosbetai - r_coslam * r_sinbetai

        r_sinlon = r_sinlam * np.copysign(1.0, r_lsign)

        r_geohdg = np.arctan2(r_sinlon, r_t2)
    else:
        r_geohdg = None

    return r_geohdg


@njit
def llh2xyz(llh, r_a=RA, r_e2=RE2):
    """
    Convert (lat, lon, height) to (x,y,z)
    """
    lat = np.radians(llh[0])
    lon = np.radians(llh[1])
    h = llh[2]
    re = r_a / np.sqrt(1.0 - r_e2 * np.sin(lat) ** 2)
    x = (re + h) * np.cos(lat) * np.cos(lon)
    y = (re + h) * np.cos(lat) * np.sin(lon)
    z = (re * (1 - r_e2) + h) * np.sin(lat)
    return np.array([x, y, z])


@njit
def xyz2llh(xyz, r_a=RA, r_e2=RE2):
    x = xyz[0]
    y = xyz[1]
    z = xyz[2]

    r_q2 = 1.0 / (1.0 - r_e2)
    r_q = np.sqrt(r_q2)
    r_q3 = r_q2 - 1.0
    r_b = r_a * np.sqrt(1 - r_e2)

    lon = np.arctan2(y, x)
    if y < 0 and lon > 0:
        lon = lon - np.pi
    if y > 0 and lon < 0:
        lon = lon + np.pi
    r_p = np.sqrt(x**2 + y**2)
    r_tant = (z / r_p) * r_q
    r_theta = np.arctan(r_tant)
    r_tant = (z + r_q3 * r_b * np.sin(r_theta) ** 3) / (
        r_p - r_e2 * r_a * np.cos(r_theta) ** 3
    )
    lat = np.arctan(r_tant)
    r_re = r_a / np.sqrt(1.0 - r_e2 * np.sin(lat) ** 2)
    h = r_p / np.cos(lat) - r_re
    lat = np.degrees(lat)
    lon = np.degrees(lon)
    return np.array([lat, lon, h])


@njit
def llh2xyz_vec(llh, r_a=RA, r_e2=RE2):
    """
    Convert (lat, lon, height) to (x,y,z)
    """
    # if len(llh.shape) == 1:
    #    llh = np.expand_dims(llh,axis=0)
    lat = np.radians(llh[:, 0])
    lon = np.radians(llh[:, 1])
    h = llh[:, 2]
    re = r_a / np.sqrt(1.0 - r_e2 * np.sin(lat) ** 2)
    x = (re + h) * np.cos(lat) * np.cos(lon)
    y = (re + h) * np.cos(lat) * np.sin(lon)
    z = (re * (1 - r_e2) + llh[:, 2]) * np.sin(lat)
    return np.column_stack((x, y, z))


@njit
def xyz2llh_vec(xyz, r_a=RA, r_e2=RE2):
    # if len(xyz.shape) == 1:
    #    xyz = np.expand_dims(xyz,axis=0)
    x = xyz[:, 0]
    y = xyz[:, 1]
    z = xyz[:, 2]

    r_q2 = 1.0 / (1.0 - r_e2)
    r_q = np.sqrt(r_q2)
    r_q3 = r_q2 - 1.0
    r_b = r_a * np.sqrt(1 - r_e2)

    lon = np.arctan2(y, x)
    lonshift = np.zeros_like(lon)
    lonshift[(y < 0) & (lon > 0)] = -np.pi
    lonshift[(y > 0) & (lon < 0)] = np.pi
    lon = lon + lonshift
    r_p = np.sqrt(x**2 + y**2)
    r_tant = (z / r_p) * r_q
    r_theta = np.arctan(r_tant)
    r_tant = (z + r_q3 * r_b * np.sin(r_theta) ** 3) / (
        r_p - r_e2 * r_a * np.cos(r_theta) ** 3
    )
    lat = np.arctan(r_tant)
    r_re = r_a / np.sqrt(1.0 - r_e2 * np.sin(lat) ** 2)
    h = r_p / np.cos(lat) - r_re
    lat = np.degrees(lat)
    lon = np.degrees(lon)
    return np.column_stack((lat, lon, h))


def _reast(r_lat, r_a=RA, r_e2=RE2):
    return r_a / np.sqrt(1.0 - r_e2 * np.sin(r_lat) ** 2)


def _rnorth(r_lat, r_a=RA, r_e2=RE2):
    return r_a * (1 - r_e2) / (1.0 - r_e2 * np.sin(r_lat) ** 2) ** 1.5


def _rdir(r_lat, hdg, r_a=RA, r_e2=RE2):
    re = _reast(r_lat, r_a, r_e2)
    rn = _rnorth(r_lat, r_a, r_e2)
    rdir = (re * rn) / (re * np.cos(hdg) ** 2 + rn * np.sin(hdg) ** 2)
    return rdir


def sch2xyz(llg, r_a=RA, r_e2=RE2):
    """
    Computes the transformation matrix and translation vector needed to get between
    (s,c,h) coordinates and (x,y,z) WGS-84 coordinates
    I still don't know how to get the transformation matrix. What I got is very
    similar to this one. But they are different.
    """

    r_lat = np.radians(llg[0])
    r_lon = np.radians(llg[1])
    hdg = llg[2]
    r_clt = np.cos(r_lat)
    r_slt = np.sin(r_lat)
    r_clo = np.cos(r_lon)
    r_slo = np.sin(r_lon)
    r_chg = np.cos(hdg)
    r_shg = np.sin(hdg)

    r_mat = np.array(
        [
            [
                r_clt * r_clo,
                -r_shg * r_slo - r_slt * r_clo * r_chg,
                r_slo * r_chg - r_slt * r_clo * r_shg,
            ],
            [
                r_clt * r_slo,
                r_clo * r_shg - r_slt * r_slo * r_chg,
                -r_clo * r_chg - r_slt * r_slo * r_shg,
            ],
            [r_slt, r_clt * r_chg, r_clt * r_shg],
        ]
    )

    r_matinv = r_mat.T

    r_radcur = _rdir(r_lat, hdg, r_a, r_e2)
    xyz = llh2xyz([llg[0], llg[1], 0.0])
    r_up = [r_clt * r_clo, r_clt * r_slo, r_slt]
    r_ov = [xyz[k] - r_radcur * r_up[k] for k in range(3)]
    return {"mat": r_mat, "matinv": r_matinv, "radcur": r_radcur, "ov": r_ov}


@njit
def tcnbasis(pos, vel, look_dir="RIGHT", r_a=RA, r_e2=RE2):
    llh = xyz2llh(pos, r_a, r_e2)
    r_lat = np.radians(llh[0])
    r_lon = np.radians(llh[1])
    r_n = -np.array(
        [np.cos(r_lat) * np.cos(r_lon), np.cos(r_lat) * np.sin(r_lon), np.sin(r_lat)]
    )
    if look_dir.lower() == "right":
        r_temp = np.cross(r_n, vel)
    else:
        r_temp = -np.cross(r_n, vel)
    r_c = r_temp / np.linalg.norm(r_temp)
    r_temp = np.cross(r_c, r_n)
    r_t = r_temp / np.linalg.norm(r_temp)
    return r_t, r_c, r_n


@njit
def tcnbasis_vec(pos, vel, look_dir="RIGHT", r_a=RA, r_e2=RE2):
    llh = xyz2llh_vec(pos, r_a, r_e2)
    r_lat = np.radians(llh[:, 0])
    r_lon = np.radians(llh[:, 1])
    r_n = -np.column_stack(
        (np.cos(r_lat) * np.cos(r_lon), np.cos(r_lat) * np.sin(r_lon), np.sin(r_lat))
    )
    if look_dir.lower() == "right":
        r_temp = np.cross(r_n, vel)
    else:
        r_temp = -np.cross(r_n, vel)
    r_c = r_temp / np.linalg.norm(r_temp)
    r_temp = np.cross(r_c, r_n)
    r_t = r_temp / np.linalg.norm(r_temp)
    return r_t, r_c, r_n


def enu2xyz(xyz, lat, lon):
    R1 = rotation3(-(90 - lat), 1)
    R3 = rotation3(-(90 + lon), 3)
    return np.dot(np.matmul(R3, R1), xyz)


def xyz2enu(enu, lat, lon):
    """
    transform a vector in ECEF coordinate to an ENU
    coordinate with latitude=lat, longitude=lon

    reference:
    https://gssc.esa.int/navipedia/index.php/Transformations_between_ECEF_and_ENU_coordinates
    """
    R1 = rotation3(90 - lat, 1)
    R3 = rotation3(90 + lon, 3)
    return np.dot(np.matmul(R1, R3), enu)
