import numpy as np
import os
import pandas as pd

RA = 6371000.0 # radius of Earth

class GeoCoordinates:
    def __init__(self,rscfile=None,
                 rscparams=None,
                 metadatafile=None):
        if not rscfile is None:
            rsc = {}
            with open(os.path.join(os.getcwd(),rscfile),'r') as f:
                for line in f:
                    parts = line.split()
                    param,val = parts[0], parts[1]
                    try:
                        if '.' in val or 'e' in val.lower():
                            rsc[param] = float(val)
                        else:
                            rsc[param] = int(val)
                    except ValueError:
                        rsc[param] = val
            self.nlon = rsc['WIDTH']
            self.nlat = rsc['FILE_LENGTH']
            self.lonmin = rsc['X_FIRST']
            self.latmax = rsc['Y_FIRST']
            self.dlon = rsc['X_STEP']
            self.dlat = rsc['Y_STEP']
        elif not rscparams is None:
            self.nlon = rscparams['nlon']
            self.nlat = rscparams['nlat']
            self.lonmin = rscparams['lonmin']
            self.latmax = rscparams['latmax']
            self.dlon = rscparams['dlon']
            self.dlat = rscparams['dlat']
        elif not metadatafile is None:
            df = pd.read_csv(metadatafile,header=0,index_col=False)
            df = df[['Near Start Lat', 'Near Start Lon', 'Far Start Lat', \
                     'Far Start Lon', 'Near End Lat', 'Near End Lon', \
                     'Far End Lat', 'Far End Lon']]
            lls = []
            for (_,c) in df.iteritems():
                lls.append(np.median(c.to_numpy()))
            lat = lls[0::2]
            lon = lls[1::2]
            self.latmin = np.min(lat)
            self.latmax = np.max(lat)
            self.lonmin = np.min(lon)
            self.lonmax = np.max(lon)
            self.dlon = 1./7200
            self.dlat = -1./7200
            self.nlon = np.round((self.lonmax - self.lonmin)/self.dlon + 1).astype(int)
            self.nlat = np.round((self.latmin - self.latmax)/self.dlat + 1).astype(int)
        else:
            raise Exception("rsc/meta file or rsc parameters need to be specified")
        self.lonmax = self.lonmin + self.dlon * (self.nlon - 1)
        self.latmin = self.latmax + self.dlat * (self.nlat - 1)
        self.latspacing = RA*np.pi/180
        self.lonspacing = RA*np.cos((self.latmin+self.latmax)*np.pi/180/2)*np.pi/180
        self.dlatspacing = np.abs(self.dlat)*self.latspacing
        self.dlonspacing = self.dlon*self.lonspacing
        try:
            self.zoffset = rsc['Z_OFFSET']
            self.zscale = rsc['Z_SCALE']
            self.projection = rsc['PROJECTION']
        except Exception:
            self.zoffset = 0
            self.zscale = 1
            self.projection = 'LATLON'

    def save_as_rsc(self,filename='out.rsc'):
        with open(os.path.join(os.getcwd(),filename),'w') as f:
            f.write("{0: <16}{1:d}\n".format("WIDTH",self.nlon))
            f.write("{0: <16}{1:d}\n".format("FILE_LENGTH",self.nlat))
            f.write("{0: <16}{1:.14f}\n".format("X_FIRST",self.lonmin))
            f.write("{0: <16}{1:.14f}\n".format("Y_FIRST",self.latmax))
            f.write("{0: <16}{1:.14f}\n".format("X_STEP",self.dlon))
            f.write("{0: <16}{1:.14f}\n".format("Y_STEP",self.dlat))
            f.write("{0: <16}{1}\n".format("Z_OFFSET",self.zoffset))
            f.write("{0: <16}{1}\n".format("Z_SCALE",self.zscale))
            f.write("{0: <16}{1}".format("PROJECTION",self.projection))
            f.close()

    def ll2xy(self, lat, lon):
        if isinstance(lat,(tuple,list,np.ndarray)):
            x = ((lat-self.latmax)/self.dlat).astype(int)
            y = ((lon-self.lonmin)/self.dlon).astype(int)
        else:
            x = int((lat-self.latmax)/self.dlat)
            y = int((lon-self.lonmin)/self.dlon)
        return x, y

    def xy2ll(self, x, y):
        lat = self.latmax + x*self.dlat
        lon = self.lonmin + y*self.dlon
        return lat, lon

    def grid(self):
        return np.mgrid[self.latmax:self.latmin:(self.nlat*1j),
                self.lonmin:self.lonmax:(self.nlon*1j)]

    def tokml(self,
              kmlfilename="roi",
              img=None,
              name="roi",
              description="Region of Interest"):
        if len(kmlfilename)<4 or kmlfilename[-4:] != '.kml':
            kmlfilename = kmlfilename+'.kml'
        with open(kmlfilename,'w') as f:
            f.write("""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://earth.google.com/kml/2.2">
<GroundOverlay>
<name>{}</name>
<description>{}</description>
<Icon>
      <href>{}</href>
</Icon>
<LatLonBox>
    <north> {}</north>
    <south> {} </south>
    <east> {} </east>
    <west> {} </west>
</LatLonBox>
</GroundOverlay>
</kml>""".format(name,
                 description,
                 img,
                 self.latmax,
                 self.latmin,
                 self.lonmax,
                 self.lonmin))

    def take_look(self,latlook,lonlook):
        rscparams = {}
        rscparams['nlat'] = self.nlat//latlook
        rscparams['nlon'] = self.nlon//lonlook
        rscparams['latmax'] = self.latmax+self.dlat*(latlook-1.)/2
        rscparams['lonmin'] = self.lonmin+self.dlon*(lonlook-1.)/2
        rscparams['dlat'] = self.dlat*latlook
        rscparams['dlon'] = self.dlon*lonlook
        return GeoCoordinates(rscparams=rscparams)
        
    def __str__(self):
        s ='''Min lat: {0:.6f}
Max lat: {1:.6f}
Min lon: {2:.6f}
Max lon: {3:.6f}
Pixels in lat: {4:d}
Pixels in lon: {5:d}
Step in lat: {6:.6f}
Step in lon: {7:.6f}
Lat Pixel Spacing: {8:.6f}
Lon Pixel Spacing: {9:.6f}\n'''.format(
                     self.latmin, self.latmax, self.lonmin, self.lonmax,
                     self.nlat, self.nlon, self.dlat, self.dlon,
                     self.dlatspacing, self.dlonspacing)
        return s

def merge2geos(geo1,geo2):
    '''
        Calculate a new GeoCoordinates object that covers both 'geo1' and 'geo2'
    '''
    rscparams = {}
    lonmin = np.minimum(geo1.lonmin,geo2.lonmin)
    lonmax = np.maximum(geo1.lonmax,geo2.lonmax)
    latmin = np.minimum(geo1.latmin,geo2.latmin)
    latmax = np.maximum(geo1.latmax,geo2.latmax)
    dlon = np.maximum(geo1.dlon,geo2.dlon)
    dlat = np.minimum(geo1.dlat,geo2.dlat)
    nlon = int(np.floor((lonmax-lonmin)/dlon))+1
    nlat = int(np.floor((latmin-latmax)/dlat))+1
    rscparams['nlon'] = nlon
    rscparams['nlat'] = nlat
    rscparams['lonmin'] = lonmin
    rscparams['latmax'] = latmax
    # Adjust 'dlon' and 'dlat' to exactly match the maximum and minimum lat/lon
    rscparams['dlon'] = (lonmax-lonmin)/(nlon-1)
    rscparams['dlat'] = (latmin-latmax)/(nlat-1)
    geof = GeoCoordinates(rscparams = rscparams)
    return geof


def project(imgi,geoi,geof):
    nlat = imgi.shape[0]
    nlon = imgi.shape[1]
    assert nlon == geoi.nlon and nlat == geoi.nlat, "The input geocoordinate does not match with the image."
    lat,lon = geoi.grid()
    xf,yf = geof.ll2xy(lat,lon)
    validmask = (xf>=0) & (xf<geof.nlat) & (yf>0) & (yf<geof.nlon)
    if isinstance(imgi,np.ma.core.MaskedArray):
        validmask = validmask & np.logical_not(imgi.mask)
    xf = xf[validmask]
    yf = yf[validmask]
    d = imgi[validmask]
    imgf = np.ma.zeros(geof.nlat*geof.nlon,dtype=imgi.dtype)
    idx = np.ravel_multi_index(np.stack((xf,yf)),(geof.nlat,geof.nlon))
    imgf[idx] = d
    imgf.mask = imgf==0
    imgf = np.ma.reshape(imgf,(geof.nlat,geof.nlon))
    return imgf

