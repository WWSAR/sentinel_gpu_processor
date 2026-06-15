import glob
import numpy as np
import os
import re

from datetime import datetime
from matplotlib import pyplot as plt
from matplotlib.colors import ListedColormap
from tqdm import tqdm
from typing import List
NHEAD = 64
BLOCK = 512

class CroppedImage:
    def __init__(self, nrow0:int, ncol0:int, left:int, top:int, right:int,
            bottom:int, data:np.ndarray|None|str, dtype:type = np.complex64):
        """
        Initialize a Cropped Image object

        Parameters
        ----------
        nrow0, ncol0: int
            Size of the original uncropped image
        left, top, right, bottom: int
            Bounds of the cropped image
        data: np.ndarray|str|None
            Data of the cropped image.
            np.ndarray: fully loaded cropped image
            str: filename of the cropped image
            None: placeholder
        dtype: type
            Type of the image data
        """
        self.nrow0 = nrow0
        self.ncol0 = ncol0
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom
        self.data = data
        self.nrow = bottom - top
        self.ncol = right - left
        self.dtype = dtype

    @classmethod
    def from_file(cls, filename: str, load_data: bool = False,
                  dtype: type = np.complex64):
        """
        Initialize from a file

        Parameters
        ----------
        filename: str
            File storing the cropped image
        load_data: bool
            If true, load all data to the data field. Otherwise, the data
            field is set to `filename`
        dtype: type
            Type of the image data

        Returns
        -------
        cls: CroppedImage
            A CroppedImage object initalized from filename
        """
        with open(filename, 'rb') as f:
            header = np.fromfile(f, count = NHEAD, dtype = np.int32)
            nrow0 = header[0]
            ncol0 = header[1]
            left, top, right, bottom = \
                    header[2], header[3], header[4], header[5]
            nrow = bottom - top
            ncol = right - left
            if load_data:
                data = np.fromfile(
                        f, count = nrow * ncol, dtype = dtype)
                data = np.reshape(data,(nrow, ncol))
            else:
                data = filename
            return cls(nrow0, ncol0, left, top, right, bottom, data, dtype)

    def resample(self, left:int, top:int, right:int, bottom:int):
        """
        Resample this cropped image to another (cropped) grid

        Parameters
        ----------
        left, top, right, bottom: int
            Bounds of the destination grid

        Returns
        -------
        res: np.ndarray
            Resampled image
        """
        if self.data is None or isinstance(self.data, str):
            raise RuntimeError('Data are not loaded for this CroppedImage')
        nrow_dst = bottom - top
        ncol_dst = right - left
        overlap_left = np.maximum(left, self.left)
        overlap_right = np.minimum(right, self.right)
        overlap_top = np.maximum(top, self.top)
        overlap_bottom = np.minimum(bottom, self.bottom)
        res = np.zeros((nrow_dst, ncol_dst), dtype = self.data.dtype)
        res[overlap_top - top: overlap_bottom - top,
            overlap_left - left: overlap_right - left] = \
            self.data[overlap_top - self.top: overlap_bottom - self.top,
                      overlap_left - self.left: overlap_right - self.left]
        return res

    def load_data(self, left:int = None, top:int = None, right:int = None, \
            bottom:int = None):
        """
        Load part of the cropped image

        Parameters
        ----------
        left, top, right, bottom: int
            Bounds of the image to read

        Returns
        -------
        res: np.ndarray|None
            Loaded image, None if its data are not loaded and data file is not
            specified
        """
        if left is None:
            left = 0
        if top is None:
            top = 0
        if right is None:
            right = self.ncol0
        if bottom is None:
            bottom = self.nrow0
        if isinstance(self.data, np.ndarray):
            if left == self.left and top == self.top and \
               right == self.right and bottom == self.bottom:
                return self.data
            else:
                return self.resample(left, top, right, bottom)
        elif isinstance(self.data, str):
            # load part of the image from file
            with open(self.data, 'rb') as f:
                overlap_top = np.maximum(top, self.top)
                overlap_bottom = np.minimum(bottom, self.bottom)
                f.seek(NHEAD*4 + (overlap_top - self.top) * self.ncol *
                                 np.dtype(self.dtype).itemsize)
                d = np.fromfile(
                        f, 
                        count = (overlap_bottom - overlap_top)*self.ncol,
                        dtype = self.dtype)
                d = np.reshape(d, (overlap_bottom - overlap_top, self.ncol))
            if left == self.left and top == self.top and \
               right == self.right and bottom == self.bottom:
                return d
            else: 
                # create a new CroppedImage for image resampling
                temp = CroppedImage(self.nrow0, self.ncol0, self.left,
                        overlap_top, self.right, overlap_bottom, d)
                return temp.resample(left, top, right, bottom)
        return None

    def __str__(self):
        s = f'nrow0: {self.nrow0}\n' + \
            f'ncol0: {self.ncol0}\n' + \
            f'left: {self.left}\n' + \
            f'top: {self.top}\n' + \
            f'right: {self.right}\n' + \
            f'bottom: {self.bottom}\n' + \
            f'nrow: {self.nrow}\n' + \
            f'ncol: {self.ncol}'
        return s

class Subswath:
    def __init__(self, burst_files:List[str]):
        self.bursts = np.array([CroppedImage.from_file(s) for s in burst_files])
        self.size = len(self.bursts)
        if self.size == 0:
            self.nrow0 = 0
            self.ncol0 = 0
        else:
            self.nrow0 = self.bursts[0].nrow0
            self.ncol0 = self.bursts[0].ncol0
        self.sort()

    def bounds(self):
        if self.is_empty():
            return None
        left = np.minimum(self.bursts[0].left, self.bursts[-1].left)
        top = np.minimum(self.bursts[0].top, self.bursts[-1].top)
        right = np.maximum(self.bursts[0].right, self.bursts[-1].right)
        bottom = np.maximum(self.bursts[0].bottom, self.bursts[-1].bottom)
        return [left, top, right, bottom]

    def sort(self):
        idx = np.zeros(self.size, dtype=int)
        for i in range(self.size):
            idx[i] = self.bursts[i].top+self.bursts[i].bottom
        sorted_idx = np.argsort(idx)
        self.bursts = np.array([self.bursts[i] for i in sorted_idx])
    
    def is_empty(self):
        return self.size == 0

    def __str__(self):
        s = ''
        for i, burst in enumerate(self.burst):
            s = s + f'Burst No. {i+1}\n============\n' + sec.__str__()
        return s

class BurstGroup:
    def __init__(self, burst_files:List[str]):
        self.subswaths = []
        for subswath in range(1,4):
            burst_list = [s for s in burst_files if f'iw{subswath}' in s]
            self.subswaths.append(Subswath(burst_list))

def sentinel_parser(filename):
    filename = os.path.split(filename)[-1]
    words = re.split(r'[_]+|\.',filename)
    sent = {}
    sent['filename'] = filename
    sent['mission'] = words[0]
    sent['mode'] = words[1]
    sent['product_type'] = words[2]
    sent['level'] = words[3][0]
    sent['product_class'] = words[3][1]
    sent['polarization'] = words[3][2:4]
    sent['start_time'] = words[4]
    sent['stop_time'] = words[5]
    sent['orbit_number'] = words[6]
    sent['mission_id'] = words[7]
    sent['unique_id'] = words[8]
    return sent

def sentinel_acq_time(filename):
    sent = sentinel_parser(filename)
    start_time = datetime.strptime(sent["start_time"],"%Y%m%dT%H%M%S")
    stop_time = datetime.strptime(sent["stop_time"],"%Y%m%dT%H%M%S")
    t = start_time + (stop_time-start_time)/2
    return t

def np2rgb(m,cmap='jet',vmin=None,vmax=None):
    mask = np.isnan(m)
    m_ = m[~mask]
    if vmin is None:
        vmin = np.percentile(m_,1)
    if vmax is None:
        vmax = np.percentile(m_,99)
    norm = plt.Normalize(vmin=vmin,vmax=vmax)
    if isinstance(cmap,str):
        colors = getattr(plt.cm,cmap)(norm(m))
    else:
        colors = cmap(norm(m))
    return colors

def savematrix(m,filename,fmt='png',cmap='jet',vmin=None,vmax=None):
    '''
    save a matrix as png file
    
    Args:
        m: the matrix to save
        filename: the name of the png file
        cmap: the color scheme used to represent the matrix
        vmin and vmax: the range of the value that can be represented by color
    '''
    colors = np2rgb(m,cmap,vmin,vmax)
    words = filename.split('.')
    if len(words) > 1 and fmt.lower() == words[-1].lower():
        plt.imsave(filename,colors)
    else:
        plt.imsave(filename+'.'+fmt,colors)

def readintslc(filename,nrg):
    slc = np.fromfile(filename,dtype=np.int16)
    naz = len(slc)//nrg//2
    slc = np.reshape(slc,(naz,2*nrg))
    slc = slc[:,0:2*nrg:2] + 1j * slc[:,1:2*nrg:2]
    return slc

def readslc(filename,nrg,rowstart=None,rowend=None,colstart=None,colend=None,
        isfloat=True):
    if isfloat:
        byte_per_iq = 4
    else:
        byte_per_iq = 2
    byte_per_line = nrg*byte_per_iq*2
    if rowstart is None:
        rowstart = 0
    if colstart is None:
        colstart = 0
    if rowend is None:
        f = open(filename,'r')
        f.seek(0,2)
        fsize = f.tell()
        naz = int(fsize/byte_per_line)
        f.close()
        rowend = naz
    if colend is None:
        colend = nrg
    nline = rowend-rowstart
    f = open(filename,'rb')
    f.seek(byte_per_line*rowstart,0)
    if isfloat:
        slc=np.fromfile(f,count=nrg*2*nline,dtype=np.float32)
    else:
        slc=np.fromfile(f,count=nrg*2*nline,dtype=np.int16)
    f.close()
    slc = np.reshape(slc,(nline,2*nrg))
    slc = slc[:,colstart*2:colend*2:2] + 1j * slc[:,colstart*2+1:colend*2:2]
    return slc

def saveslc(slc,filename):
    naz,nrg = slc.shape
    tosave = np.zeros((naz,nrg*2),dtype='float32')
    tosave[:,0:2*nrg:2] = np.real(slc)
    tosave[:,1:2*nrg:2] = np.imag(slc)
    f = open(filename,'w')
    tosave.tofile(f)
    f.close()

def readc(f,nrg):
    data = np.fromfile(f,dtype='float32')
    naz = len(data)//nrg//2
    data = np.reshape(data,(naz,2*nrg))
    c = data[:,0:nrg]+1j*data[:,nrg:]
    return c

def savec(c,filename):
    naz,nrg = c.shape
    tosave = np.zeros((naz,nrg*2),dtype='float32')
    tosave[:,0:nrg] = np.real(c)
    tosave[:,nrg:] = np.imag(c)
    f = open(filename,'w')
    tosave.tofile(f)
    f.close()

def cpxlooks(imgbg,rowlook,collook):
    naz,nrg = imgbg.shape
    newnaz = np.floor(naz/rowlook).astype(int)
    newnrg = np.floor(nrg/collook).astype(int)
    imgaz = np.zeros((newnaz,nrg),dtype=imgbg.dtype)
    imgsm = np.zeros((newnaz,newnrg),dtype=imgbg.dtype)
    if rowlook>1:
        for i in np.arange(0,newnaz):
            imgaz[i,:] = np.sum(imgbg[i*rowlook:(i+1)*rowlook,:],axis=0)
    else:
        imgaz = imgbg
    if collook>1:
        for i in np.arange(0,newnrg):
            imgsm[:,i] = np.sum(imgaz[:,i*collook:(i+1)*collook],axis=1)
    else:
        imgsm = imgaz
    return imgsm/collook/rowlook

def powlooks(imgbg,rowlook,collook):
    return np.abs(cpxlooks(np.abs(imgbg)**2,rowlook,collook))

def multilooks(imgfile: str, outfile: str, dtype:type,
               nr: int, nc: int, nrlook:int, nclook:int,
               chuncksize:float=1e9):
    """
    Take multilook of a large image (>10 G)

    Parameters
    ----------
    imgfile : string
        slcfile to read
    outfile : string
        output amplitude file to write
    dtype: type
        type of the element in the image to multilook
    nr : int
        number of rows (nlat or naz)
    nc : int
        number of columns (nlon or nrg)
    nrlook : int
        number of looks to take in the row direction
    nclook : int
        number of looks to take in the column direction
    chunckisize : int
        number of bytes to read each time
    """
    try:
        byte_per_element = np.dtype(dtype).itemsize
    except TypeError:
        raise TypeError(f"Unrecognized type: {dtype}")
    nrpatch = int(int(chuncksize/(nc*byte_per_element))//nrlook*nrlook)
    npatch = int(np.ceil(nr/nrpatch))
    with open(outfile, 'wb') as fout:
        with open(imgfile, 'rb') as f:
            for i in tqdm(range(npatch), desc='multilook dem'):
                line_start = nrpatch*i
                line_end = line_start + nrpatch
                line_end = np.minimum(line_end,nr)
                nlines = line_end-line_start
                patch = np.fromfile(f, dtype=dtype, count=nlines*nc)
                patch = np.reshape(patch, (nlines, nc))
                patch = cpxlooks(patch*1., nrlook, nclook).astype(dtype)
                patch.tofile(fout)

def read_orbit(orbfile:str)->np.ndarray:
    """
    read an orbitiming file

    Parameters
    ----------
    orbfile: str
        orbit file

    Returns
    -------
    orb: np.ndarray
        orbit vector
    """
    with open(orbfile,"r") as f:
        line = f.readline()
        line = line.strip()
        nstatvec = int(line.strip())
        orb = np.zeros((nstatvec,7))
        for i in range(nstatvec):
            line = f.readline()
            words = line.split()
            for j in range(7):
                orb[i,j] = float(words[j])
    return orb

def bwr_cmap(n):
    x = np.array([-1,0,1])
    y = np.array([[1,0,0],[1,1,1],[0,0,1]])
    c = np.ones((n,4))
    xval = np.linspace(-1,1,n)
    for i in range(3):
        c[:,i] = np.interp(xval,x,y[:,i])
    return c

bwrcmap = ListedColormap(bwr_cmap(256))
