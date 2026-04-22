#!/usr/bin/env python3
import numpy as np
import os
import shutil
from typing import Tuple, Sequence, Literal

from s1proc import get_bin_path
from s1proc import geocoordinates
from s1proc import sario
from s1proc.sario import CroppedImage, NHEAD
from s1proc._log import setup_logger, set_logging_level
logger = setup_logger(name=__name__,level='INFO')

def crossmul_strip(
        strip1: CroppedImage,
        strip2: CroppedImage,
        rowlook: int,
        collook: int
        )->Tuple[CroppedImage|None, int]:
    """
    Form an multilooked interferogram between two strips

    Parameters
    ----------
    strip1: CroppedImage
        First strip
    strip2: CroppedImage
        Second strip
    rowlook: int
        Number of looks in the row direction
    collook: int
        Number of looks in the column direction

    Returns
    -------
    strip: CroppedImage | None
        SLC trip, None if the two image strips do not overlap
    next_flag: int
        0: compare next strip1 with current strip2
        1: compare current strip1 with next strip2
        2: compare next strip2 with next strip2
        3: the returned strip is cropped from strip1
        4: the returned strip is cropped from strip2
    """
    if strip1.top >= strip2.bottom:
        return None, 0
    if strip1.bottom <= strip2.top:
        return None, 1
    # define the common grid to resample the two strips
    nrow0 = strip1.nrow0
    ncol0 = strip1.ncol0
    left = int(np.minimum(strip1.left, strip2.left))
    left = left // collook * collook
    right = int(np.maximum(strip1.right, strip2.right))
    right = right // collook * collook
    top = int(np.minimum(strip1.top, strip2.top))
    bottom = int(np.maximum(strip1.bottom, strip2.bottom))
    res1 = strip1.resample(left, top, right, bottom)
    res2 = strip2.resample(left, top, right, bottom)
    amp1 = np.abs(res1)
    amp2 = np.abs(res2)

    # create a mask representing the upper half of the strip
    first_nonzero_col, last_nonzero_col = \
            np.where(np.any(amp1>0,axis=0))[0][[0,-1]]
    #print(f'first nonzero column, {first_nonzero_col}')
    #print(f'last nonzero column, {last_nonzero_col}')
    first_mean_idx = np.mean(np.where(amp1[:,first_nonzero_col]>0)[0])
    last_mean_idx = np.mean(np.where(amp1[:,last_nonzero_col]>0)[0])
    #print(f'mean row idx of the first nonzero column, {first_mean_idx}')
    #print(f'mean row idx of the last nonzero column, {last_mean_idx}')
    rr = np.outer(np.arange(bottom - top),
            np.ones(right - left, dtype = int))
    if first_mean_idx > last_mean_idx:
        # ascending tracks
        upper_mask = rr < (np.arange(right-left)-first_nonzero_col)/ \
                (last_nonzero_col-first_nonzero_col)*(top-bottom) + bottom - top
    else:
        # descending tracks
        upper_mask = rr < (np.arange(right-left)-first_nonzero_col)/ \
                (last_nonzero_col-first_nonzero_col)*(bottom-top)

    # find areas where the first strip is nonzero and the second strip is zero
    mask1 = (amp1 > 0) & (amp2 == 0) & upper_mask
    nonoverlap_rows = np.sum(mask1,axis=0)
    mean_no_rows1 = np.median(
            nonoverlap_rows[first_nonzero_col:last_nonzero_col])
    res1[~mask1] = 0.
    nonzero_rows = np.where(np.any(mask1, axis=1))[0]
    if len(nonzero_rows) < np.maximum(2, rowlook):
        top1, bottom1 = None, None
    else:
        rstart1, rend1 = nonzero_rows[0], nonzero_rows[-1]
        top1 = int(np.ceil((top + rstart1) / rowlook)) * rowlook
        bottom1 = (top + rend1 + 1) // rowlook * rowlook

    # find areas where the first strip is zero and the second strip is nonzero
    mask2 = (amp1 == 0) & (amp2 > 0) & upper_mask
    nonoverlap_rows = np.sum(mask2,axis=0)
    mean_no_rows2 = np.median(
            nonoverlap_rows[first_nonzero_col:last_nonzero_col])
    # return None if nonoverlapped areas are too narrow for multilooking
    if np.maximum(mean_no_rows1, mean_no_rows2) <= rowlook:
        return None, 2
    res2[~mask2] = 0
    nonzero_rows = np.where(np.any(mask2, axis=1))[0]
    if len(nonzero_rows) < np.maximum(2, rowlook):
        top2, bottom2 = None, None
    else:
        rstart2, rend2 = nonzero_rows[0], nonzero_rows[-1]
        top2 = int(np.ceil((top + rstart2) / rowlook)) * rowlook
        bottom2 = (top + rend2 + 1) // rowlook * rowlook
    
    # --- case 1: the two strips overlap perferctly ---
    if top1 is None and top2 is None:
        return None, 2
    elif top2 is None:
        strip = CroppedImage(nrow0, ncol0, left, top1, right, bottom1,
                res1[top1 - top : bottom1 - top, :])
        return strip, 3
    elif top1 is None:
        strip = CroppedImage(nrow0, ncol0, left, top2, right, bottom2,
                res2[top2 - top : bottom2 - top, :])
        return strip, 4
    elif top1 < top2:
        strip = CroppedImage(nrow0, ncol0, left, top1, right, bottom1,
                res1[top1 - top : bottom1 - top, :])
        return strip, 3
    else:
        strip = CroppedImage(nrow0, ncol0, left, top2, right, bottom2,
                res2[top2 - top : bottom2 - top, :])
        return strip, 4

def interfere_subswath(
    main_img_file: str,
    sec_img_file: str,
    outfile: str,
    rowlook: int,
    collook: int,
    direction: Literal['asc','desc'],
    out_float: bool):
    """
    Form an interferogram from two subswath geo-coded SLC images

    Parameters
    ----------
    main_img_file: str
        Main compressed subswath image
    sec_img_file: str
        Secondary compressed subswath image
    outfile: str
        Output image
    rowlook: int
        Number of looks in the row direction
    collook: int
        Number of looks in the column direction
    direction: Literal['asc','desc']
        Flight direction
    out_float: bool
        Only output phase
    """
    crossmul = get_bin_path('crossmul')
    crossmul_sec = get_bin_path('crossmul_sec')
    out_float_flag = 1 if out_float else 0 
    command = f'{crossmul} {main_img_file} {sec_img_file} {outfile} {rowlook} {collook}' + \
              f' {out_float_flag}'
    logger.info(command)
    os.system(command)
    main_supp_img_file = main_img_file.replace('main','sec')
    sec_supp_img_file = sec_img_file.replace('main','sec')
    command = f'{crossmul_sec} {main_img_file} {main_supp_img_file} ' + \
              f'{sec_img_file} {sec_supp_img_file} {outfile} ' + \
              f'{rowlook} {collook} {direction} {out_float_flag}'
    logger.info(command)
    os.system(command)
    return

def stitch(
    ifg_files: Sequence[str],
    outfile: str,
    out_float: bool,
    chunk_size_gb: float = 1.0):
    """
    Stitch coregistered interferograms with chunked processing

    Parameters
    ----------
    ifg_files: Sequence[str]
        List of interferograms to stitch
    outfile: str
        Output interferogram
    out_float: bool
        Output float rather than complex images
    chunk_size_gb: float
        Maximum chunk size in GB per read operation (default: 1.0)
    """
    nifg = len(ifg_files)
    if nifg == 0:
        return
    elif nifg == 1:
        if out_float:
            ifg = np.fromfile(ifg_files[0], dtype=np.complex64)
            np.angle(ifg).astype(np.float32).tofile(outfile)
        else:
            os.rename(ifg_files[0], outfile)
        return

    # Get file size and calculate chunk size in elements
    file_size = os.path.getsize(ifg_files[0])
    dtype = np.complex64
    elem_size = np.dtype(dtype).itemsize  # 8 bytes for complex64
    total_elements = file_size // elem_size

    # Calculate chunk size in elements (1GB = 1e9 bytes)
    max_chunk_bytes = chunk_size_gb * 1e9
    chunk_elements = max(1, int(max_chunk_bytes // elem_size))

    # Ensure we don't create too many chunks for small files
    num_chunks = int(max(1, np.ceil(total_elements / chunk_elements)))

    logger.info(f'Processing {nifg} files with {num_chunks} chunks '
    f'({chunk_elements} elements per chunk)')

    # Open output file for writing
    with open(outfile, 'wb') as out_f:
        # Process each chunk
        for chunk_idx in range(num_chunks):
            start_idx = chunk_idx * chunk_elements
            end_idx = min((chunk_idx + 1) * chunk_elements, total_elements)
            chunk_size = end_idx - start_idx

            # Read first interferogram chunk
            with open(ifg_files[0], 'rb') as f:
                f.seek(start_idx * elem_size)
                ifg1 = np.frombuffer(f.read(chunk_size * elem_size), dtype=dtype)

            # Process remaining interferograms for this chunk
            for i in range(1, nifg):
                # Read chunk from current file
                with open(ifg_files[i], 'rb') as f:
                    f.seek(start_idx * elem_size)
                    ifg2 = np.frombuffer(f.read(chunk_size * elem_size),
                            dtype=dtype)

                # Calculate masks for this chunk
                mask1 = np.abs(ifg1) > 1e-3
                mask2 = np.abs(ifg2) > 1e-3
                common_mask = mask1 & mask2

                # Calculate phase difference only if there are common points
                if np.any(common_mask):
                    ifg_diff = np.conj(ifg1[common_mask]) * ifg2[common_mask]
                    phase_diff = np.angle(np.mean(ifg_diff))
                    ifg_diff = ifg_diff*np.exp(-1j*phase_diff)
                    phase_diff = phase_diff + np.median(np.angle(ifg_diff))
                    logger.info(f'Chunk {chunk_idx+1}/{num_chunks}, ' + \
                                 'phase difference ' + \
                                 f'between file 0 and file {i}: {phase_diff} rad')
                    if np.abs(phase_diff) > 0.2:
                        ifg2 = ifg2 * np.exp(-1j * phase_diff)

                # Merge interferograms
                ifg1 = ifg1 * mask1 + ifg2 * mask2 * (~mask1)

            # Write processed chunk to output file
            if out_float:
                np.angle(ifg1).astype(np.float32).tofile(out_f)
            else:
                ifg1.tofile(out_f)

    # Clean up original files
    for ifg_file in ifg_files:
        os.remove(ifg_file)

    return

def stitch_patches(patch, ifg, left, right, out_float):
    temp = patch[:,left:right]
    if out_float:
        common_mask = (temp!=0) & (ifg!=0)
    else:
        common_mask = (temp.real!=0) & (ifg.real!=0)
    if np.any(common_mask):
        if out_float:
            phase_diff = np.exp(1j*(-temp[common_mask] + ifg[common_mask]))
        else:
            phase_diff = np.conj(temp[common_mask]) * ifg[common_mask]
        mean_phase_diff = np.angle(np.mean(phase_diff)) 
        phase_diff = phase_diff * np.exp(-1j*mean_phase_diff)
        med_phase_diff = np.median(np.angle(phase_diff))
        mean_phase_diff += med_phase_diff
        logger.info(f'mean phase offset: {mean_phase_diff} rad')
        if out_float:
            replace_mask = (temp == 0) & (ifg != 0)
            ifg = ifg - mean_phase_diff
            ifg[ifg > np.pi] -= 2*np.pi
            ifg[ifg < -np.pi] += 2*np.pi
        else:
            replace_mask = (temp.real == 0)
            ifg = ifg * np.exp(-1j*mean_phase_diff)
        temp[replace_mask] = ifg[replace_mask]
    else:
        temp[:] = ifg[:]
    return

def interfere_single_scene(
        main_img_files: Sequence[str],
        sec_img_files: Sequence[str],
        ifg_dir: str,
        rowlook: int,
        collook: int,
        direction: Literal['asc','desc'],
        outfile: str,
        out_float: bool = False):
    """
    Form an interferogram for a single scene

    Parameters
    ----------
    main_img_files: Sequence[str]
        Main subswath image files
    sec_img_files: Sequence[str]
        Secondary subswath image files
    ifg_dir: str
        Directory to save interferograms
    rowlook: int
        Number of looks in the row direction
    collook: int
        Number of looks in the column direction
    direction: Literal['asc','desc']
        flight direction
    outfile: str
        Output interferogram
    out_float: bool
        Only write phase to disk
    """
    subifg_files = []
    nsubswath = len(main_img_files)
    if out_float:
        element_size = 4
        dtype = np.float32
    else:
        element_size = 8
        dtype = np.complex64
    for i in range(nsubswath):
        main_img_file = main_img_files[i]
        sec_img_file = sec_img_files[i]
        main_date, main_id = parse_fname(main_img_file)
        sec_date, sec_id = parse_fname(sec_img_file)
        subifg_file = os.path.join(ifg_dir,
                f'{main_date}_{sec_date}_{main_id}_{sec_id}_{i}.int')
        subifg_files.append(subifg_file)
        if os.path.exists(subifg_file):
            continue
        interfere_subswath(main_img_file, sec_img_file, subifg_file,
                rowlook, collook, direction, out_float)
    tempfile = outfile+'.temp'
    if not os.path.exists(tempfile):
        if nsubswath == 1:
            ci1 = CroppedImage.from_file(subifg_files[0],dtype=dtype)
            ifg = ci1.load_data()
            ifg.tofile(tempfile)
        else:
            ci1 = CroppedImage.from_file(subifg_files[0],dtype=dtype)
            ifg1 = ci1.load_data()
            for i in range(1, nsubswath):
                ci2 = CroppedImage.from_file(
                    subifg_files[i],load_data=True,dtype=dtype)
                ifg2 = ci2.data
                ifg1_cropped = ifg1[ci2.top:ci2.bottom,ci2.left:ci2.right]
                if out_float:
                    mask = ifg1_cropped == 0
                else:
                    mask = ifg1_cropped.real == 0
                ifg1_cropped[mask] = ifg2[mask]
            ifg1.tofile(tempfile)
    else:
        if nsubswath == 1:
            ci1 = CroppedImage.from_file(
                subifg_files[0],load_data=True,dtype=dtype)
            with open(tempfile,'r+b') as f:
                f.seek(ci1.top*ci1.ncol0*element_size, 0)
                curr_data = np.fromfile(f,count=ci1.nrow*ci1.ncol0,dtype=dtype)
                curr_data = np.reshape(curr_data,(ci1.nrow,ci1.ncol0))
                stitch_patches(
                    curr_data, ci1.data, ci1.left, ci1.right, out_float)
                f.seek(ci1.top*ci1.ncol0*element_size, 0)
                f.write(curr_data)
        else:
            tops = np.zeros(nsubswath, dtype = int)
            bottoms = np.zeros(nsubswath, dtype = int)
            lefts = np.zeros(nsubswath, dtype = int)
            rights = np.zeros(nsubswath, dtype = int)
            for i,subifg_file in enumerate(subifg_files):
                ci = CroppedImage.from_file(subifg_file,
                                            load_data=False,dtype=dtype)
                tops[i] = ci.top
                bottoms[i] = ci.bottom
                lefts[i] = ci.left
                rights[i] = ci.right
            top = np.min(tops) 
            bottom = np.max(bottoms)
            left = np.min(lefts)
            right = np.max(rights)
            ci1 = CroppedImage.from_file(
                subifg_files[0],load_data=False,dtype=dtype)
            ifg = ci1.load_data(left, top, right, bottom)
            for i in range(1, nsubswath):
                ci2 = CroppedImage.from_file(
                    subifg_files[i],load_data=True,dtype=dtype)
                ifg2 = ci2.data
                ifg_cropped = ifg[ci2.top-top:ci2.bottom-top,
                                  ci2.left-left:ci2.right-left]
                if out_float:
                    mask = ifg_cropped == 0
                else:
                    mask = ifg_cropped.real == 0
                ifg_cropped[mask] = ifg2[mask]
            with open(tempfile,'r+b') as f:
                f.seek(top*ci1.ncol0*element_size, 0)
                curr_data = np.fromfile(f,count=(bottom-top)*ci1.ncol0,dtype=dtype)
                curr_data = np.reshape(curr_data,((bottom-top),ci1.ncol0))
                print(curr_data.shape)
                stitch_patches(curr_data, ifg, left, right, out_float)
                f.seek(top*ci1.ncol0*element_size, 0)
                f.write(curr_data)
    #for subifg_file in subifg_files:
    #    os.remove(subifg_file)
    return tempfile

def parse_fname(fn:str)->Tuple[str, str]:
    basename = os.path.basename(fn)
    date = basename[0:8]
    data_id = basename[9:20]
    return date, data_id

def interfere(
        img_pair_file: str,
        rscfile: str,
        direction: Literal['asc','desc'],
        /,
        ifg_dir: str = 'igrams',
        rowlook: int = 1,
        collook: int = 1,
        out_float: bool = False,
        verbose: bool = False):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    img_pair_file: str
        File containing pairs of subswath images
    rscfile: str
        rsc file
    direction: Literal['asc','desc']
        Flight direction
    ifg_dir: str
        Directory to save interferograms
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    out_float: bool
        Output float rather than cpx images
    verbose: bool
        Set the logging level to debug
    """
    if verbose:
        set_logging_level(logger, 'DEBUG')
    os.makedirs(ifg_dir, exist_ok = True)
    intlist_file = os.path.join(ifg_dir, 'intlist')
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook,collook)
    rsclook.save_as_rsc(os.path.join(ifg_dir,'dem.rsc'))

    fin = open(img_pair_file,'r')
    lines = fin.readlines()
    fin.close()
    main_list = []
    sec_list = []
    main_date_list = []
    sec_date_list = []
    main_id_list = []
    sec_id_list = []
    for line in lines:
        words = line.split()
        main_img_file = words[0]
        sec_img_file = words[1]
        main_date, main_id = parse_fname(main_img_file)
        sec_date, sec_id = parse_fname(sec_img_file)
        main_list.append(main_img_file)
        sec_list.append(sec_img_file)
        main_date_list.append(main_date)
        sec_date_list.append(sec_date)
        main_id_list.append(main_id)
        sec_id_list.append(sec_id)

    line_idx = 0
    nlines = len(main_list)
    intlist = []
    while line_idx < nlines:
        main_date = main_date_list[line_idx]
        sec_date = sec_date_list[line_idx]
        intfile = os.path.join(ifg_dir,f'{main_date}_{sec_date}.int')
        logger.info(f'processing interferogram {intfile}')
        if os.path.exists(intfile):
            if len(intlist) == 0 or intlist[-1] != intfile:
                intlist.append(intfile)
            line_idx += 1
            continue
        subsets = []
        curr_main_imgs = []
        curr_sec_imgs = []
        count = 0
        for i in range(line_idx, nlines):
            if main_date == main_date_list[i] and sec_date == sec_date_list[i]:
                count += 1
                if i > line_idx and \
                   (main_id_list[i] != main_id_list[i-1] or \
                    sec_id_list[i] != sec_id_list[i-1]):
                    # subswaths for a new interferogram
                    subsets.append((np.copy(curr_main_imgs),
                                    np.copy(curr_sec_imgs)))
                    curr_main_imgs = [main_list[i]]
                    curr_sec_imgs = [sec_list[i]]
                else:
                    # different subswaths of the same interferogram
                    curr_main_imgs.append(main_list[i])
                    curr_sec_imgs.append(sec_list[i])
            else:
                break
        if len(curr_main_imgs) > 0:
            subsets.append((np.copy(curr_main_imgs),
                            np.copy(curr_sec_imgs)))
        line_idx += count
        for main_img_files, sec_img_files in subsets:
            tempfile = interfere_single_scene(
                        main_img_files,
                        sec_img_files,
                        ifg_dir,
                        rowlook,
                        collook,
                        direction,
                        intfile,
                        out_float)
        os.rename(tempfile,intfile)
        #stitch(ifg_files, intfile, out_float)
    fout = open(intlist_file,'w')
    fout.write('\n'.join(intlist))
    fout.close()
