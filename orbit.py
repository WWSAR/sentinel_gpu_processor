import numpy as np
import geometry
from numba import njit

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
    x_interp = np.dot(a, xx) + np.dot(b, vv)
    # Hermite interpolation of the derivative
    v_interp = np.dot(a2, xx) + np.dot(b2, vv)

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
        fn = np.dot(dr, satv)
        fnprime = -np.dot(satv, satv)
        tline = tline - fn / fnprime
        satx, satv = interp_orbit(timeorbit, xx, vv, tline)
        if np.abs(tline - tprev) < 5.0e-9:
            break
    dr = xyz - satx
    return dr, tline

def rah2ll(meta,rgidx,azidx,h,zero_doppler=True,look_dir='RIGHT'):
    """
    Given the range/azimuth indices and the elevation of a set of radar pixels,
    calculate their latitue and longitude coordinates
    """
    xyzsatstart,velsatstart = \
            interp_orbit(meta.tt,meta.xx,meta.vv,meta.start_time)
    xyzsatend,velsatend = \
            interp_orbit(meta.tt,meta.xx,meta.vv,
                         meta.start_time+meta.naz/meta.prf)
    xyzsatmid,velsatmid = \
            interp_orbit(meta.tt,meta.xx,meta.vv,
                         meta.start_time+meta.naz/2/meta.prf)
    lati,loni,_ = geometry.xyz2llh(xyzsatstart)
    latf,lonf,_ = geometry.xyz2llh(xyzsatend)
    r_geohdg = geometry.geo_hdg([lati,loni],[latf,lonf])
    latm,lonm,heightm = geometry.xyz2llh(xyzsatmid)
    ptm = geometry.sch2xyz((latm,lonm,r_geohdg))
    rcurv = ptm['radcur']

    tline = azidx/meta.prf+meta.start_time 
    rng = rgidx*meta.drg+meta.near_range
    tline = np.atleast_1d(tline)
    rng = np.atleast_1d(rng)
    h = np.atleast_1d(h)
    n = len(tline)
    lats = np.zeros(n)
    lons = np.zeros(n)
    if zero_doppler:
        dopfact = 0.
    else:
        vmean = np.mean(np.linalg.norm(meta.vv,axis=1))
        squint_angle = meta.fdc_ref*meta.wavelength/2/vmean
        dopfact = np.sin(squint_angle)

    for i in range(n):
        xyzsat,velsat = interp_orbit(meta.tt,meta.xx,meta.vv,tline[i])
        llhsat = geometry.xyz2llh(xyzsat)
        vhat = velsat/np.linalg.norm(velsat)
        that,chat,nhat = geometry.tcnbasis(xyzsat,velsat,look_dir)
        aa = rcurv + llhsat[2]
        bb = rcurv + h[i]
        costheta = 0.5*((aa/rng[i]) + (rng[i]/aa) - (bb/aa)*(bb/rng[i]))
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
        gamm = costheta*rng[i]
        alpha = (dopfact*rng[i] - gamm*np.dot(nhat,vhat))/np.dot(vhat,that)
        beta = np.sqrt((rng[i]*sintheta)**2 - alpha**2)
        delta = gamm*nhat + alpha*that + beta*chat
        xyz = xyzsat + delta
        llh = geometry.xyz2llh(xyz)
        lats[i] = llh[0]
        lons[i] = llh[1]
    if len(lats) > 1 or len(lats) == 0:
        return lats, lons
    else:
        return lats[0],lons[0]

def ra2llh(meta,rgidx,azidx,dem,rsc,zero_doppler=True,look_dir='RIGHT'):
    """
    Given the azimuth and range coordinates of radar pixels, calculate their
    latitude, longitude and elevation

    Parameters
    ----------
    meta: meta object
        Metadata
    rgidx: int array
        range indices
    azidx: int array
        azimuth indices
    dem: 2D array
        A DEM grid covering the area of interest
    rsc: GeoCoordinates object
        description of the DEM grid
    zero_doppler: boolean
        calculating llh assuming the squint angle is zero if zero_doppler is
        True. Otherwise, calculating `dopfact` from `meta.fdc_ref`.
    look_dir: string (optional)
        look direction of the radar sensor (RIGHT by default)
    
    Returns
    -------
    lat: float array
        latitude of radar pixels
    lon: float array
        longitude of radar pixels
    h: float array
        elevation of radar pixels
    """
    xyzsatstart,velsatstart = \
            interp_orbit(meta.tt,meta.xx,meta.vv,meta.start_time)
    xyzsatend,velsatend = \
            interp_orbit(meta.tt,meta.xx,meta.vv,
                         meta.start_time+meta.naz/meta.prf)
    xyzsatmid,velsatmid = \
            interp_orbit(meta.tt,meta.xx,meta.vv,
                         meta.start_time+meta.naz/2/meta.prf)
    lati,loni,_ = geometry.xyz2llh(xyzsatstart)
    latf,lonf,_ = geometry.xyz2llh(xyzsatend)
    r_geohdg = geometry.geo_hdg([lati,loni],[latf,lonf])
    latm,lonm,heightm = geometry.xyz2llh(xyzsatmid)
    ptm = geometry.sch2xyz((latm,lonm,r_geohdg))
    rcurv = ptm['radcur']
    tline = azidx/meta.prf+meta.start_time 
    rng = rgidx*meta.drg+meta.near_range
    tline = np.atleast_1d(tline)
    rng = np.atleast_1d(rng)

    # mean elevation
    h0 = np.mean(dem)

    # check if orbit data need to be downsampled
    if len(meta.tt) > 2000:
        idx = np.linspace(0,len(meta.tt)-1,1000).astype(int)
        tt = meta.tt[idx]
        xx = meta.xx[idx]
    else:
        tt = meta.tt
        xx = meta.xx

    # recalculate velocity to improve the stability of this function
    vv = np.zeros_like(xx)
    vv[0,:] = (xx[1,:] - xx[0,:])/(tt[1]-tt[0])
    vv[-1,:] = (xx[-1,:] - xx[-2,:])/(tt[-1]-tt[-2])
    vv[1:-1,:] = (xx[2:,:] - xx[0:-2])/np.expand_dims((tt[2:]-tt[0:-2]),axis=1)

    # Doppler factor
    if zero_doppler:
        dopfact = 0.
    else:
        vmean = np.mean(np.linalg.norm(vv,axis=1))
        dopfact = meta.fdc_ref*meta.wavelength/2/vmean

    lats,lons,hs = ra2llh_(tline,rng,tt,xx,vv,rcurv,heightm,h0,dem,
                           rsc.latmax,rsc.lonmin,rsc.nlat,rsc.nlon,
                           rsc.dlat,rsc.dlon,dopfact,look_dir)

    return np.squeeze(lats),np.squeeze(lons),np.squeeze(hs)

@njit
def ra2llh_(tline,rng,timeorbit,xx,vv,rcurv,height,h0,dem,
            latmax,lonmin,nlat,nlon,dlat,dlon,dopfact,
            look_dir='RIGHT',res_tol=0.1,maxiter=10):
    n = len(tline)

    # initialize lat lon h
    lats = np.zeros(n)
    lons = np.zeros(n)
    hs = np.zeros(n)

    # interate over all points
    for i in range(n):
        # get the position and velocity of platform at current azimuth time
        xyzsat,velsat = interp_orbit(timeorbit,xx,vv,tline[i])
        # get the unit vector in the velocity direction
        vhat = velsat/np.linalg.norm(velsat)
        # create tcn basis
        that,chat,nhat = geometry.tcnbasis(xyzsat,velsat,look_dir)
        # intialize parameters used  in h calculation
        h_prev = -1e10
        h = h0
        niter = 0
        aa = rcurv + height
        if height - h > rng[i]:
            h = height - rng[i] + 1.
        while np.abs(h-h_prev)>res_tol and niter < maxiter:
            bb = rcurv + h
            costheta = 0.5*((aa/rng[i]) + (rng[i]/aa) - (bb/aa)*(bb/rng[i]))
            if np.abs(costheta) > 1:
                llh =  np.array([-999.,-999.,-999.])
                break
            sintheta = np.sqrt(1. - costheta**2)
            gamm = costheta*rng[i]
            alpha = (dopfact*rng[i] - gamm*np.dot(nhat,vhat))/np.dot(vhat,that)
            beta = np.sqrt((rng[i]*sintheta)**2 - alpha**2)
            if np.isnan(beta):
                alpha = 0
                beta = rng[i]*sintheta
            delta = gamm*nhat + alpha*that + beta*chat
            xyz = xyzsat + delta
            llh = geometry.xyz2llh(xyz)
            h_prev = h
            x = int((llh[0] - latmax)/dlat)
            y = int((llh[1] - lonmin)/dlon)
            if x < 0 or x >= nlat or y < 0  or y >= nlon:
                llh = np.array([-999.,-999.,-999.])
                break
            h = dem[x,y] - llh[2] + h_prev
            niter += 1
        lats[i] = llh[0]
        lons[i] = llh[1]
        hs[i] = llh[2]
    return lats,lons,hs

def look_angle(meta,llh):
    npts = llh.shape[0]
    xyz = geometry.llh2xyz_vec(llh)
    tmid = meta.start_time + meta.naz/2/meta.prf
    xmid,vmid = interp_orbit(meta.tt,meta.xx,meta.vv,tmid)
    theta = np.zeros(npts)
    for i in range(npts):
        dr,tline = orbitrangetime(meta.tt,meta.xx,meta.vv,
                                  xyz[i,:],tmid,xmid,vmid)
        xsat,_ = interp_orbit(meta.tt,meta.xx,meta.vv,tline)
        llhsat = geometry.xyz2llh(xsat)
        theta[i] = np.arccos((llhsat[2]-llh[i,2])/np.linalg.norm(dr))
    return theta

