from datetime import datetime
import glob
from matplotlib import pyplot as plt
from matplotlib.colors import ListedColormap
import numpy as np
import os
import re

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

def readigram(filename,nrg):
    igram = readslc(filename,nrg)
    image1 = datetime.strptime(filename[0:8],'%Y%m%d')
    image2 = datetime.strptime(filename[9:17],'%Y%m%d')
    tempbl = (image2-image1).days
    tempbl = (image2-image1).days
    return {'dat':igram,'image1':image1,'image2':image2,'tempb':tempbl}

def plotigram(igram):
    phase = np.angle(igram['dat'])
    plt.imshow(phase,vmin=-np.pi,vmax=np.pi)
    plt.show()

def saveslc(slc,filename):
    naz,nrg = slc.shape
    tosave = np.zeros((naz,nrg*2),dtype='float32')
    tosave[:,0:2*nrg:2] = np.real(slc)
    tosave[:,1:2*nrg:2] = np.imag(slc)
    f = open(filename,'w')
    tosave.tofile(f)
    f.close()

def plotslc(slc):
    amp = abs(slc)
    cutmax = np.percentile(amp,99)
    cutmin = np.percentile(amp,1)
    amp = np.minimum(cutmax,np.maximum(cutmin,amp))
    plt.imshow(amp,cmap='jet')
    plt.show()

def readc(f,nrg):
    data = np.fromfile(f,dtype='float32')
    naz = len(data)//nrg//2
    data = np.reshape(data,(naz,2*nrg))
    c = data[:,0:nrg]+1j*data[:,nrg:]
    return c

def readcs(filepath,ext,nrg):
    cs = []
    filelist = glob.glob(os.path.join(filepath,'*.'+ext))
    for f in filelist:
        cs.append(readc(os.path.join(filepath,f),nrg))
    return cs

def readigrams(filepath,ext,nrg):
    igrams = []
    filelist = glob.glob(os.path.join(filepath,'*.'+ext))
    for f in filelist:
        igrams.append(readigram(os.path.join(filepath,f),nrg))
    return igrams

def savec(c,filename):
    naz,nrg = c.shape
    tosave = np.zeros((naz,nrg*2),dtype='float32')
    tosave[:,0:nrg] = np.real(c)
    tosave[:,nrg:] = np.imag(c)
    f = open(filename,'w')
    tosave.tofile(f)
    f.close()

def plotc(c):
    plotdata(np.imag(c))

def plotdata(img):
    plt.imshow(img,vmax=np.percentile(img,99),
            vmin=np.percentile(img,1),cmap='jet')
    plt.show()

def readpixel(unwpath,sbaslist,nx,ny,x,y):
    s = np.zeros(len(sbaslist))
    offset = (nx*(2*y+1)+x)*4
    for i in range(len(sbaslist)):
        if i%100==0:
            print(i)
        if sbaslist['deramp'][i]:
            filename = os.path.join(unwpath, \
                                    'unw_deramp',
                                    sbaslist['image1'][i]+'_'+ \
                                    sbaslist['image2'][i]+'.unw')
        else:
            filename = os.path.join(unwpath, \
                                    sbaslist['image1'][i]+'_'+ \
                                    sbaslist['image2'][i]+'.unw')
        with open(filename,'r') as f:
            f.seek(offset)
            s[i] = np.fromfile(f, dtype='float32', count=1)[0]
            f.close()
    return s

def readstack(unwpath,sbaslist,nx,ny,idx1,idx2):
    x1 = idx1%nx
    y1 = idx1//nx
    x2 = idx2%nx
    y2 = idx2//nx
    if y2 == ny:
        y2 = ny - 1
    offset = nx*2*y1*4
    count = (y2-y1+1)*nx*2
    s = np.zeros((len(sbaslist),idx2-idx1))
    for i in range(len(sbaslist)):
        if sbaslist['deramp'][i]:
            filename = os.path.join(unwpath, \
                                    sbaslist['image1'][i]+'_'+ \
                                    sbaslist['image2'][i]+'.unw.deramp')
        else:
            filename = os.path.join(unwpath, \
                                    sbaslist['image1'][i]+'_'+ \
                                    sbaslist['image2'][i]+'.unw')
        with open(filename,'r') as f:
            f.seek(offset)
            patch = np.fromfile(f,dtype='float32', count=count)
            patch = np.reshape(patch,(y2-y1+1,2*nx))
            patch = patch[:,nx:].ravel()
            if y2 == ny -1:
                s[i,:] = patch[x1:]
            else:
                s[i,:] = patch[x1:-(nx-x2)]
            f.close()
    return s
    
def readtile(ifglist,geo,idx11,idx12,idx21,idx22,filepath=None):
    n = len(ifglist)
    #s = np.zeros((n,idx12-idx11,idx22-idx21),dtype=np.float32)
    s = np.zeros((n,idx12-idx11,idx22-idx21),dtype=np.complex64)
    if filepath is None:
        filepath = os.path.join(os.getcwd(),'ps_data')
    for i in range(n):
        filename = os.path.join(filepath, \
                                ifglist['image1'][i]+'_'+ \
                                ifglist['image2'][i]+'.rephase') 
        res = readslc(filename,geo.nlon)
        s[i,:,:] = res[idx11:idx12,idx21:idx22]
        #with open(filename,'r') as f:
        #    phase = np.fromfile(f,dtype=np.float32)
        #    phase = np.reshape(phase,(geo.nlat,geo.nlon))
        #    s[i,:,:] = phase[idx11:idx12,idx21:idx22]
        #    f.close()
    return s

def loadtile(tilename):
    tiledata = np.fromfile(tilename,dtype=np.float32)
    n = int(tiledata[0])
    n1 = int(tiledata[2]-tiledata[1])
    n2 = int(tiledata[4]-tiledata[3])
    header = tiledata[0:5].astype(int)
    return header,np.reshape(tiledata[5:],(n,n1,n2))

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

def multilooks(imgfile,outfile,nr,nc,nrlook,nclook,chuncksize=1e9):
    """
    Take multilook of a large slc image (>10 G)

    Parameters
    ----------
    imgfile : string
        slcfile to read
    outfile : string
        output amplitude file to write
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

    nrsmall = nr//nrlook
    nrpatch = int(int(chuncksize/(nc*4))//nrlook*nrlook)
    nr_read = 0
    with open(outfile,'w') as fout:
        with open(imgfile,'r') as f:
            while nr_read < nr-nrlook+1:
                rcount = int(np.minimum(nrpatch,nr-nr_read))
                nr_read = nr_read + rcount
                patch = np.fromfile(f, dtype=np.float32, count=rcount*nc*2)
                patch = np.reshape(patch,(rcount,2*nc))
                patch = patch[:,0:2*nc:2] + 1j * patch[:,1:2*nc:2]
                patch = np.sqrt(powlooks(patch,nrlook,nclook)).astype(np.float32)
                fout.write(patch)

def interferogram(slc1,slc2,nlookaz,nlookrg):
    return cpxlooks(slc1*np.conj(slc2),nlookrg,nlookaz)

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

def ifg_cmap(n):
    J = [0.9922,0.6914,0.6797,
	0.9922,0.7031,0.6719,
	0.9922,0.7070,0.6680,
	0.9922,0.7148,0.6562,
	0.9922,0.7227,0.6523,
	0.9922,0.7266,0.6445,
	0.9922,0.7383,0.6367,
	0.9922,0.7422,0.6289,
	0.9922,0.7539,0.6211,
	0.9922,0.7578,0.6172,
	0.9922,0.7617,0.6094,
	0.9922,0.7734,0.6016,
	0.9922,0.7773,0.5938,
	0.9922,0.7891,0.5859,
	0.9922,0.7930,0.5820,
	0.9922,0.7969,0.5742,
	0.9922,0.8086,0.5664,
	0.9922,0.8125,0.5586,
	0.9922,0.8242,0.5508,
	0.9922,0.8281,0.5430,
	0.9922,0.8398,0.5352,
	0.9922,0.8438,0.5312,
	0.9922,0.8477,0.5234,
	0.9922,0.8594,0.5156,
	0.9922,0.8633,0.5078,
	0.9922,0.8750,0.5000,
	0.9922,0.8789,0.4961,
	0.9922,0.8828,0.4883,
	0.9922,0.8945,0.4805,
	0.9922,0.8984,0.4727,
	0.9922,0.9102,0.4648,
	0.9922,0.9141,0.4609,
	0.9922,0.9180,0.4531,
	0.9922,0.9297,0.4453,
	0.9922,0.9336,0.4375,
	0.9922,0.9453,0.4297,
	0.9922,0.9492,0.4219,
	0.9922,0.9531,0.4180,
	0.9922,0.9648,0.4102,
	0.9922,0.9688,0.4023,
	0.9922,0.9805,0.3945,
	0.9922,0.9844,0.3867,
	0.9922,0.9883,0.3828,
	0.9805,0.9922,0.3945,
	0.9727,0.9922,0.3984,
	0.9648,0.9922,0.4102,
	0.9609,0.9922,0.4141,
	0.9492,0.9922,0.4219,
	0.9453,0.9922,0.4297,
	0.9375,0.9922,0.4336,
	0.9297,0.9922,0.4453,
	0.9258,0.9922,0.4492,
	0.9141,0.9922,0.4609,
	0.9102,0.9922,0.4648,
	0.9023,0.9922,0.4688,
	0.8945,0.9922,0.4805,
	0.8867,0.9922,0.4844,
	0.8789,0.9922,0.4961,
	0.8750,0.9922,0.5000,
	0.8672,0.9922,0.5039,
	0.8594,0.9922,0.5156,
	0.8516,0.9922,0.5195,
	0.8438,0.9922,0.5312,
	0.8398,0.9922,0.5352,
	0.8320,0.9922,0.5391,
	0.8242,0.9922,0.5508,
	0.8164,0.9922,0.5547,
	0.8086,0.9922,0.5664,
	0.8008,0.9922,0.5703,
	0.7969,0.9922,0.5742,
	0.7891,0.9922,0.5859,
	0.7812,0.9922,0.5898,
	0.7734,0.9922,0.6016,
	0.7656,0.9922,0.6055,
	0.7617,0.9922,0.6094,
	0.7539,0.9922,0.6211,
	0.7461,0.9922,0.6250,
	0.7383,0.9922,0.6367,
	0.7305,0.9922,0.6406,
	0.7266,0.9922,0.6445,
	0.7148,0.9922,0.6562,
	0.7109,0.9922,0.6602,
	0.7031,0.9922,0.6719,
	0.6953,0.9922,0.6758,
	0.6875,0.9922,0.6875,
	0.6797,0.9922,0.6914,
	0.6758,0.9922,0.6953,
	0.6680,0.9922,0.7070,
	0.6602,0.9922,0.7109,
	0.6523,0.9922,0.7227,
	0.6445,0.9922,0.7266,
	0.6406,0.9922,0.7305,
	0.6289,0.9922,0.7422,
	0.6250,0.9922,0.7461,
	0.6172,0.9922,0.7578,
	0.6094,0.9922,0.7617,
	0.6055,0.9922,0.7656,
	0.5938,0.9922,0.7773,
	0.5898,0.9922,0.7812,
	0.5820,0.9922,0.7930,
	0.5742,0.9922,0.7969,
	0.5703,0.9922,0.8008,
	0.5586,0.9922,0.8125,
	0.5547,0.9922,0.8164,
	0.5430,0.9922,0.8281,
	0.5391,0.9922,0.8320,
	0.5352,0.9922,0.8398,
	0.5234,0.9922,0.8477,
	0.5195,0.9922,0.8516,
	0.5078,0.9922,0.8633,
	0.5039,0.9922,0.8672,
	0.5000,0.9922,0.8750,
	0.4883,0.9922,0.8828,
	0.4844,0.9922,0.8867,
	0.4727,0.9922,0.8984,
	0.4688,0.9922,0.9023,
	0.4609,0.9922,0.9141,
	0.4531,0.9922,0.9180,
	0.4492,0.9922,0.9258,
	0.4375,0.9922,0.9336,
	0.4336,0.9922,0.9375,
	0.4219,0.9922,0.9492,
	0.4180,0.9922,0.9531,
	0.4141,0.9922,0.9609,
	0.4023,0.9922,0.9688,
	0.3984,0.9922,0.9727,
	0.3867,0.9922,0.9844,
	0.3828,0.9922,0.9883,
	0.3945,0.9883,0.9922,
	0.4023,0.9805,0.9922,
	0.4102,0.9727,0.9922,
	0.4180,0.9648,0.9922,
	0.4219,0.9609,0.9922,
	0.4297,0.9531,0.9922,
	0.4375,0.9453,0.9922,
	0.4453,0.9375,0.9922,
	0.4531,0.9297,0.9922,
	0.4609,0.9258,0.9922,
	0.4648,0.9180,0.9922,
	0.4727,0.9102,0.9922,
	0.4805,0.9023,0.9922,
	0.4883,0.8945,0.9922,
	0.4961,0.8867,0.9922,
	0.5039,0.8789,0.9922,
	0.5078,0.8750,0.9922,
	0.5156,0.8672,0.9922,
	0.5234,0.8594,0.9922,
	0.5312,0.8516,0.9922,
	0.5391,0.8438,0.9922,
	0.5430,0.8398,0.9922,
	0.5508,0.8320,0.9922,
	0.5586,0.8242,0.9922,
	0.5664,0.8164,0.9922,
	0.5742,0.8086,0.9922,
	0.5820,0.8008,0.9922,
	0.5859,0.7969,0.9922,
	0.5938,0.7891,0.9922,
	0.6016,0.7812,0.9922,
	0.6094,0.7734,0.9922,
	0.6172,0.7656,0.9922,
	0.6211,0.7617,0.9922,
	0.6289,0.7539,0.9922,
	0.6367,0.7461,0.9922,
	0.6445,0.7383,0.9922,
	0.6523,0.7305,0.9922,
	0.6562,0.7266,0.9922,
	0.6680,0.7148,0.9922,
	0.6719,0.7109,0.9922,
	0.6797,0.7031,0.9922,
	0.6875,0.6953,0.9922,
	0.6914,0.6914,0.9922,
	0.7031,0.6797,0.9922,
	0.7070,0.6758,0.9922,
	0.7148,0.6680,0.9922,
	0.7227,0.6602,0.9922,
	0.7305,0.6523,0.9922,
	0.7383,0.6445,0.9922,
	0.7422,0.6406,0.9922,
	0.7539,0.6289,0.9922,
	0.7578,0.6250,0.9922,
	0.7656,0.6172,0.9922,
	0.7734,0.6094,0.9922,
	0.7773,0.6055,0.9922,
	0.7891,0.5938,0.9922,
	0.7930,0.5898,0.9922,
	0.8008,0.5820,0.9922,
	0.8086,0.5742,0.9922,
	0.8125,0.5703,0.9922,
	0.8242,0.5586,0.9922,
	0.8281,0.5547,0.9922,
	0.8398,0.5430,0.9922,
	0.8438,0.5391,0.9922,
	0.8477,0.5352,0.9922,
	0.8594,0.5234,0.9922,
	0.8633,0.5195,0.9922,
	0.8750,0.5078,0.9922,
	0.8789,0.5039,0.9922,
	0.8828,0.5000,0.9922,
	0.8945,0.4883,0.9922,
	0.8984,0.4844,0.9922,
	0.9102,0.4727,0.9922,
	0.9141,0.4688,0.9922,
	0.9180,0.4648,0.9922,
	0.9297,0.4531,0.9922,
	0.9336,0.4492,0.9922,
	0.9453,0.4375,0.9922,
	0.9492,0.4336,0.9922,
	0.9531,0.4297,0.9922,
	0.9648,0.4180,0.9922,
	0.9688,0.4141,0.9922,
	0.9805,0.4023,0.9922,
	0.9844,0.3984,0.9922,
	0.9961,0.3867,0.9922,
	0.9922,0.3867,0.9844,
	0.9922,0.3945,0.9805,
	0.9922,0.4023,0.9688,
	0.9922,0.4102,0.9648,
	0.9922,0.4180,0.9531,
	0.9922,0.4219,0.9492,
	0.9922,0.4297,0.9453,
	0.9922,0.4375,0.9336,
	0.9922,0.4453,0.9297,
	0.9922,0.4531,0.9180,
	0.9922,0.4609,0.9141,
	0.9922,0.4648,0.9102,
	0.9922,0.4727,0.8984,
	0.9922,0.4805,0.8945,
	0.9922,0.4883,0.8828,
	0.9922,0.4961,0.8789,
	0.9922,0.5000,0.8750,
	0.9922,0.5078,0.8633,
	0.9922,0.5156,0.8594,
	0.9922,0.5234,0.8477,
	0.9922,0.5312,0.8438,
	0.9922,0.5352,0.8398,
	0.9922,0.5430,0.8281,
	0.9922,0.5508,0.8242,
	0.9922,0.5586,0.8125,
	0.9922,0.5664,0.8086,
	0.9922,0.5742,0.7969,
	0.9922,0.5820,0.7930,
	0.9922,0.5859,0.7891,
	0.9922,0.5938,0.7773,
	0.9922,0.6016,0.7734,
	0.9922,0.6094,0.7617,
	0.9922,0.6172,0.7578,
	0.9922,0.6211,0.7539,
	0.9922,0.6289,0.7422,
	0.9922,0.6367,0.7383,
	0.9922,0.6445,0.7266,
	0.9922,0.6523,0.7227,
	0.9922,0.6562,0.7148,
	0.9922,0.6680,0.7070,
	0.9922,0.6719,0.7031,
	0.9922,0.6797,0.6914,
	0.9922,0.6914,0.6797]
    J = np.array(J)
    J = np.reshape(J,(256,3))
    c = np.ones((n,4))
    for i in range(3):
        c[:,i] = np.interp(np.linspace(0,1,n),np.linspace(0,1,256),J[:,i])
    return c

ifgcmap = ListedColormap(ifg_cmap(256))
bwrcmap = ListedColormap(bwr_cmap(256))
