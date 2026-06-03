import numpy as np
from numba import njit

from s1proc import geometry

@njit
def orbithermite(tt, xx, vv, t):
    """
    Hermite polynomial interpolation of orbits

    Parameters
    ----------
    tt : 1-D sequence of length n
        The acquisition time of data points
    xx : 2-D array (n by 3)
        The xyz position of the platform at time `tt`
    vv : 2-D array (n by 3)
        The xyz velocity of the platform at time `tt`
    t : scalar
        The time at which to evaluate the interpolated values

    Returns
    -------
    x_interp : 1-D array with three elements
        Interpolated position at time `t`
    v_interp : 1-D array with three elements
        Interpolated velocity at time `t`
    """
    n = len(tt)
    li = np.ones(n)  # Lagrange basis polynomials
    a = np.zeros(n)  # basis polynomials alpha(t)
    b = np.zeros(n)  # basis polynomials beta(t)
    a2 = np.zeros(n)  # derivative of alpha(t)
    b2 = np.zeros(n)  # derivative of beta(t)
    for i in range(n):
        dl = 0.0  # derivative of Lagrange basis at tt[i]
        hdot = 0.0  # derivative of Lagrange basis at t
        for j in range(n):
            if i == j:
                continue
            dl = dl + 1 / (tt[i] - tt[j])
            li[i] = li[i] * (t - tt[j]) / (tt[i] - tt[j])
            p = 1 / (tt[i] - tt[j])
            for k in range(n):
                if k == i or k == j:
                    continue
                p = p * (t - tt[k]) / (tt[i] - tt[k])
            hdot = hdot + p
        l2 = li[i] ** 2
        a[i] = (1 - 2 * (t - tt[i]) * dl) * l2
        b[i] = (t - tt[i]) * l2
        a2[i] = -2 * dl * l2 + (1 - 2 * (t - tt[i]) * dl) * li[i] * 2 * hdot
        b2[i] = l2 + (t - tt[i]) * li[i] * 2 * hdot

    # Hermite interpolation polynomial H(t)
    #x_interp = np.dot(a, xx) + np.dot(b, vv)
    x_interp = a[0]*xx[0] + a[1]*xx[1] + a[2]*xx[2] + a[3]*xx[3] + \
               b[0]*vv[0] + b[1]*vv[1] + b[2]*vv[2] + b[3]*vv[3]
    # Hermite interpolation of the derivative
    #v_interp = np.dot(a2, xx) + np.dot(b2, vv)
    v_interp = a2[0]*xx[0] + a2[1]*xx[1] + a2[2]*xx[2] + a2[3]*xx[3] + \
               b2[0]*vv[0] + b2[1]*vv[1] + b2[2]*vv[2] + b2[3]*vv[3]

    return x_interp, v_interp

@njit
def interp_orbit(timeorbit, xx, vv, t):
    """
    Calculate the position and the velocity of the platform at time t

    Parameters
    ----------
    timeorbit : 1-D array of length n
        A sequence of time when the platform position and the velocity are
        recorded
    xx : 2-D array (n by 3)
        Platform position records
    vv : 2-D array (n by 3)
        Platform velocity records
    t : float
        Time to interpolate

    Returns
    -------
    x_interp : 1-D array with 3 elements
        Interpolated position at time `t`
    v_interp : 1-D array with 3 elements
        Interpolated velocity at time `t`
    """
    n = len(timeorbit)
    # find the location of the sampling time that is closest to t
    ilocation = np.abs(t - timeorbit).argmin()
    # Four points are needed for the Hermite interpolation
    ilocation = np.minimum(np.maximum(ilocation, 1), n - 3)
    x = xx[ilocation - 1 : ilocation + 3, :]
    v = vv[ilocation - 1 : ilocation + 3, :]
    return orbithermite(timeorbit[ilocation - 1 : ilocation + 3], x, v, t)

@njit
def orbitrangetime(timeorbit, xx, vv, xyz, tline0, satx0, satv0):
    """
    Use orbit state vectors `timeorbit`, `xx`, and `vv` to find the
    zero-Doppler orbit location for an image point at `xyz`

    Parameters
    ----------
    timeorbit : 1-D array of length n
        A sequence of time when the platform position and the velocity are
        recorded
    xx : 2-D array (n by 3)
        Platform position records
    vv : 2-D array (n by 3)
        Platform velocity records
    xyz : 1-D array with 3 elements
        Position of the image point
    tline0 : float
        Initial guess of the zero-Doppler orbit time for the image point
    satx0 : 1-D array with 3 elements
        Initial guess of the zero-Doppler orbit location for the image point
    satv0 : 1-D array with 3 elements
        Initial guess of the platform (satellite) velocity at the zero-Doppler
        location for the image point

    Returns
    -------
    dr : 1-D array with 3 elements
        The vector pointing from the zero-Doppler orbit location to the image
        point (Line of Sight vector)
    tline : float
        Estimated zero-Doppler orbit time for the image point
    """
    tline = tline0
    satx = satx0
    satv = satv0
    for k in range(51):
        tprev = tline
        dr = xyz - satx
        fn = dr[0]*satv[0] + dr[1]*satv[1] + dr[2]*satv[2]
        fnprime = -satv[0]*satv[0] - satv[1]*satv[1] - satv[2]*satv[2]
        tline = tline - fn / fnprime
        satx, satv = interp_orbit(timeorbit, xx, vv, tline)
        if np.abs(tline - tprev) < 5.0e-9:
            break
    dr = xyz - satx
    return dr, tline

@njit
def orbitrangetime_vec(llh,tt,xx,vv):
    """
    Do orbitrangetime for multiple points

    Args:
        llh (2d numpy array): lat/lon/h of the points to do orbitrangetime
        tt (1d numpy array): time vector
        xx (2d numpy array): position vector
        vv (2d numpy array): velocity vector
    """
    nstatvec = len(tt)
    nmid = nstatvec//2
    tmid = tt[nmid]
    xmid = xx[nmid,:]
    vmid = vv[nmid,:]
    xyz = geometry.llh2xyz_vec(llh) 
    losvec = np.zeros(xyz.shape)
    for i in range(len(xyz)):
       dr,_ = orbitrangetime(tt,xx,vv,xyz[i,:],tmid,xmid,vmid)
       losvec[i,:] = dr/np.sqrt(dr[0]*dr[0]+dr[1]*dr[1]+dr[2]*dr[2])
    return losvec

def rah2ll(tt,xx,vv,start_time,stop_time,rah,look_dir='RIGHT'):
    """
    Given the range/azimuth indices and the elevation of a set of radar pixels,
    calculate their latitue and longitude coordinates
    """
    xyzsatstart,velsatstart = \
            interp_orbit(tt,xx,vv,start_time)
    xyzsatend,velsatend = \
            interp_orbit(tt,xx,vv,stop_time)
    xyzsatmid,velsatmid = \
            interp_orbit(tt,xx,vv,start_time+(stop_time-start_time)/2)
    lati,loni,_ = geometry.xyz2llh(xyzsatstart)
    latf,lonf,_ = geometry.xyz2llh(xyzsatend)
    r_geohdg = geometry.geo_hdg([lati,loni],[latf,lonf])
    latm,lonm,heightm = geometry.xyz2llh(xyzsatmid)
    ptm = geometry.sch2xyz((latm,lonm,r_geohdg))
    rcurv = ptm['radcur']

    dopfact = 0.
    n = len(rah)
    lats = np.zeros(n)
    lons = np.zeros(n)

    for i in range(n):
        xyzsat,velsat = interp_orbit(tt,xx,vv,rah[i,1])
        llhsat = geometry.xyz2llh(xyzsat)
        vhat = velsat/np.linalg.norm(velsat)
        that,chat,nhat = geometry.tcnbasis(xyzsat,velsat,look_dir)
        aa = rcurv + llhsat[2]
        bb = rcurv + rah[i,2]
        costheta = 0.5*((aa/rah[i,0]) + (rah[i,0]/aa) - (bb/aa)*(bb/rah[i,0]))
        sintheta = np.sqrt(1. - costheta**2)
        """
        consider rng as a vector which can be decomposed as:
        rng = alpha * that + beta * chat + gamm * nhat
        then dot(rng,vhat) = alpha * dot(that,vhat) + 
                             beta * dot(chat,vhat) +
                             gamm * dot(nhat,vhat)
        Because chat is perpendicular to vhat (by the definition,
        chat is parallel with cross(nhat, vhat)), we have
        dot(chat,vhat) = 0. We also have dot(rng, vhat) = dopfact.
        Therefore, we finally obtain:
            alpha = dopfact-gamm * dot(nhat,vhat)/dot(that,vhat)
        """
        gamm = costheta*rah[i,0]
        alpha = (dopfact*rah[i,0] - gamm*np.dot(nhat,vhat))/np.dot(vhat,that)
        beta = np.sqrt((rah[i,0]*sintheta)**2 - alpha**2)
        delta = gamm*nhat + alpha*that + beta*chat
        xyz = xyzsat + delta
        llh = geometry.xyz2llh(xyz)
        lats[i] = llh[0]
        lons[i] = llh[1]
    return lats.squeeze(), lons.squeeze()

