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
    def __init__(self, main:CroppedImage, sec:List[CroppedImage]):
        self.main = main
        self.sec = sec
        self.nrow0 = main.nrow0
        self.ncol0 = main.ncol
        self.left = main.left
        self.top = main.top
        self.right = main.right
        self.bottom = main.bottom

    @classmethod
    def from_file(cls, filename:str,
            load_main_data:bool = False):
        """
        Initialize a Subswath object from a file
        
        Parameters
        ----------
        filename: str
            Compressed subswath image
        load_main_data: bool
            If true, load the data of the main image, which can take a lot of
            memory. Otherwise, use a placeholder instead.

        Returns
        ------
        subswath: Subswath
            Loaded subswath object
        """
        f = open(filename, 'rb')
        header = np.fromfile(f, count = NHEAD, dtype = np.int32)
        nrow0 = header[0]
        ncol0 = header[1]
        left, main_top, right, main_bottom = \
                header[2], header[3], header[4], header[5]
        main_nrow = main_bottom - main_top
        ncol = right - left
        if load_main_data:
            main_data = np.fromfile(
                    f, count = main_nrow * ncol, dtype = np.complex64)
            main_data = np.reshape(main_data,(main_nrow, ncol))
        else:
            main_data = filename
            f.seek(main_nrow * ncol * 8, 1)
        main_img = CroppedImage(nrow0, ncol0, left, main_top, right,
                main_bottom, main_data)
        nstrip = header[6]
        sec_imgs = []
        for i in range(nstrip):
            sec_top = header[7 + 2*i]
            sec_bottom = header[8 + 2*i]
            sec_nrow = sec_bottom - sec_top
            sec_data = np.fromfile(f, count = sec_nrow * ncol, dtype = np.complex64)
            sec_data = np.reshape(sec_data, (sec_nrow, ncol))
            sec_img = CroppedImage(nrow0, ncol0, left, sec_top, right,
                    sec_bottom, sec_data)
            sec_imgs.append(sec_img)  
        f.close()
        subswath = cls(main_img, sec_imgs)
        return subswath
    
    def __str__(self):
        s = 'Main image:\n============\n' + self.main.__str__()
        s = s + '\n\nSecondary images:'
        for i, sec in enumerate(self.sec):
            s = s + f'\n\nStrip No. {i+1}\n============\n' + sec.__str__()
        return s

def compress(
        main_img_file: str,
        sec_img_file: str,
        outfile: str,
        nrow: int,
        ncol: int):
    """
    Compress a geocoded subswath by removing all-zero lines and columns

    Parameters
    ----------
    main_img_file: str
        Main image file
    sec_img_file: str
        Secondary image file only containing the radar image in areas where
        adjacent burst overlap
    outfile: str
        Output image
    nrow: int
        Number of rows
    ncol: int
        Number of columns
    """
    header = np.zeros(NHEAD, dtype = np.int32) 
    header[0] = nrow
    header[1] = ncol
    # compress the main image
    main_img = np.memmap(main_img_file, dtype=np.complex64, mode="r",
            shape=(nrow, ncol))
    
    # top
    for top in range(nrow):
        if np.any(np.abs(main_img[top,:]) != 0):
            break
    if top == nrow-1:
        return
    # bottom
    for bottom in range(nrow-1, -1, -1):
        if np.any(np.abs(main_img[bottom,:]) != 0):
            bottom += 1
            break
    # left
    for left in range(0, ncol, BLOCK):
        sub = np.abs(main_img[:, left:left+BLOCK])
        if np.any(sub != 0):
            left += np.where(np.any(sub != 0, axis=0))[0][0]
            break
    # right
    for right in range(ncol, 0, -BLOCK):
        left_ = np.maximum(0, right - BLOCK)
        sub = np.abs(main_img[:, left_:right])
        if np.any(sub != 0):
            right = left_ + np.where(np.any(sub != 0, axis = 0))[0][-1] + 1
            break
    header[2],header[3],header[4],header[5] = left, top, right, bottom

    # compress the secondary image
    sec_img = np.memmap(sec_img_file, dtype=np.complex64, mode="r",
            shape=(nrow, ncol))
    nonzero_rows = np.any(sec_img != 0, axis=1).astype(int)
    p = np.pad(nonzero_rows, (1,1))       # add 0 at both ends
    d = np.diff(p)
    if np.all(d == 0):
        nstrip = 0
    else:
        starts = np.where(d == 1)[0]
        ends = np.where(d == -1)[0] - 1
        nstrip = len(starts)
    header[6] = nstrip
    for i in range(nstrip):
        header[7+2*i] = starts[i]
        header[8+2*i] = ends[i] + 1
    with open(outfile, 'wb') as f:
        header.tofile(f)
        main_img[top:bottom, left:right].tofile(f)
        for i in range(nstrip):
            sec_img[starts[i]:ends[i]+1,left:right].tofile(f)

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

def cpxlooks(imgbg,nlookaz,nlookrg):
    naz,nrg = imgbg.shape
    newnaz = np.floor(naz/nlookaz).astype(int)
    newnrg = np.floor(nrg/nlookrg).astype(int)
    imgaz = np.zeros((newnaz,nrg),dtype=imgbg.dtype)
    imgsm = np.zeros((newnaz,newnrg),dtype=imgbg.dtype)
    if nlookaz>1:
        for i in np.arange(0,newnaz):
            imgaz[i,:] = np.sum(imgbg[i*nlookaz:(i+1)*nlookaz,:],axis=0)
    else:
        imgaz = imgbg
    if nlookrg>1:
        for i in np.arange(0,newnrg):
            imgsm[:,i] = np.sum(imgaz[:,i*nlookrg:(i+1)*nlookrg],axis=1)
    else:
        imgsm = imgaz
    return imgsm/nlookrg/nlookaz

def powlooks(imgbg,nlookaz,nlookrg):
    return np.abs(cpxlooks(np.abs(imgbg)**2,nlookrg,nlookaz))

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

def correlation(slc1,slc2,nlookaz,nlookrg,igram=None):
    if igram is None:
        igram = interferogram(slc1,slc2,nlookrg,nlookaz)
    ampslc1 = np.sqrt(powlooks(slc1,nlookrg,nlookaz))
    ampslc2 = np.sqrt(powlooks(slc2,nlookrg,nlookaz))
    amp = np.real(abs(igram))
    c = np.real(amp/(np.finfo(float).eps+ampslc1*ampslc2))
    return c,amp,igram

def bwr_cmap(n):
    x = np.array([-1,0,1])
    y = np.array([[1,0,0],[1,1,1],[0,0,1]])
    c = np.ones((n,4))
    xval = np.linspace(-1,1,n)
    for i in range(3):
        c[:,i] = np.interp(xval,x,y[:,i])
    return c

bwrcmap = ListedColormap(bwr_cmap(256))
