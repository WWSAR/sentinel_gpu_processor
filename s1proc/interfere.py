#!/usr/bin/env python3
import numpy as np
import os
import shutil
from typing import Tuple, Sequence

from s1proc import geocoordinates
from s1proc import sario
from s1proc.sario import CroppedImage, Subswath, NHEAD
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
    return None, 2

def interfere_subswath(
    main_img_file: str,
    sec_img_file: str,
    outfile: str,
    rowlook: int,
    collook: int):
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
    """
    command = f'crossmul {main_img_file} {sec_img_file} {rowlook} {collook}' + \
              f' {outfile}'
    os.system(command)
    return
    f = open(outfile, 'r+b')
    ifg_header = np.fromfile(f, count = NHEAD, dtype = np.int32)
    ifg_left0, ifg_top0, ifg_right0, ifg_bottom0 = \
            ifg_header[2], ifg_header[3], ifg_header[4], ifg_header[5]
    ncol0 = ifg_right0 - ifg_left0
    subswath1 = Subswath.from_file(main_img_file)
    subswath2 = Subswath.from_file(sec_img_file)
    nstrip1 = len(subswath1.sec)
    nstrip2 = len(subswath2.sec)
    idx1 = idx2 = 0
    header_bytes = NHEAD * 4
    row_bytes = (ifg_right0 - ifg_left0)*8
    while idx1 < nstrip1 and idx2 < nstrip2:
        strip1 = subswath1.sec[idx1]
        strip2 = subswath2.sec[idx2]
        strip, next_flag = crossmul_strip(strip1, strip2, rowlook, collook)
        if strip is None:
            if next_flag == 0:
                idx1 += 1
                continue
            elif next_flag == 1:
                idx2 += 1
                continue
            elif next_flag == 2:
                idx1 += 1
                idx2 += 1
                continue
        else:
            idx1 += 1
            idx2 += 1

        # load corresponding data strip from the main image of the other
        # subswath
        curr_top = strip.top
        curr_bottom = strip.bottom
        curr_left = strip.left
        curr_right = strip.right
        if next_flag == 3:
            # strip is extracted from strip1, need to interfere it with
            # subswath2
            ref_data = strip.data
            sec_data = subswath2.main.load_data(
                    curr_left, curr_top, curr_right, curr_bottom)
        else:
            # strip is extracted from strip2, need to interfere it with
            # subswath1
            sec_data = strip.data
            ref_data = subswath1.main.load_data(
                    curr_left, curr_top, curr_right, curr_bottom)
        # form the multilooked interferogram
        ifg = sario.cpxlooks(np.conj(ref_data)*sec_data, rowlook, collook)
        ifg_top = curr_top // rowlook
        ifg_bottom = curr_bottom // rowlook
        ifg_left = curr_left // collook
        ifg_right = curr_right // collook
        mask = np.abs(ifg) > 0

        # move to the first requested line
        offset = header_bytes + (ifg_top-ifg_top0) * row_bytes
        f.seek(offset)
        # read rows
        nrow = ifg_bottom - ifg_top
        ncol = ifg_right - ifg_left
        data = np.fromfile(f, dtype = np.complex64, count=nrow*ncol0)
        data = data.reshape(nrow, ncol0)
        data_overlap = data[:,ifg_left - ifg_left0 : ifg_right - ifg_left0]
        data_overlap[mask] = ifg[mask]
        #data[:,ifg_left - ifg_left0 : ifg_right - ifg_left0] = data_overlap
        #fig, ax = plt.subplots(2,1)
        #ax[0].imshow(np.angle(data),cmap='jet')
        #ax[1].imshow(np.angle(ifg),cmap='jet')
        #plt.show()
        #plt.close()
        # go back to the same position
        f.seek(offset)
        # write back
        data.astype(np.complex64).tofile(f)
    f.close()

def stitch(
        ifg_files: Sequence[str],
        outfile: str):
    """
    Stitch coregistered interferograms

    Parameters
    ----------
    ifg_files: Sequence[str]
        List of interferograms to stitch
    outfile: str
        Output interferogram
    """
    nifg = len(ifg_files)
    if nifg == 0:
        return
    elif nifg == 1:
        os.rename(ifg_files[0], outfile)
        return
    ifg1 = np.fromfile(ifg_files[0],dtype=np.complex64)
    for i in range(1,nifg):
        ifg2 = sario.readslc(ifg_files[i],ncol)
        mask1 = np.abs(ifg1)>1e-3
        mask2 = np.abs(ifg2)>1e-3
        common_mask = mask1 & mask2
        ifg_diff = np.conj(ifg1)*ifg2
        phase_diff = np.angle(np.mean(ifg_diff[common_mask]))
        logger.debug(f'phase difference between {intfile1} and {intfile2} :'
                     f'{phase_diff} rad')
        ifg2 = ifg2 * np.exp(-1j*phase_diff)
        ifg1 = ifg1*mask1 + ifg2*mask2*(~mask1)
    ifg1.tofile(outfile)
    for ifg_file in ifg_files:
        os.remove(ifg_file)
    return

def interfere_single_scene(
        main_img_files: Sequence[str],
        sec_img_files: Sequence[str],
        ifg_dir: str,
        rowlook: int,
        collook: int) -> str:
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

    Returns
    -------
    ifg_file: str
        Filename of the generated interferogram
    """
    subifg_files = []
    nsubswath = len(main_img_files)
    outfile = None
    for i in range(nsubswath):
        main_img_file = main_img_files[i]
        sec_img_file = sec_img_files[i]
        main_date, main_id = parse_fname(main_img_file)
        sec_date, sec_id = parse_fname(sec_img_file)
        if outfile is None:
            outfile = os.path.join(ifg_dir,
                f'{main_date}_{sec_date}_{main_id}_{sec_id}.int')
        subifg_file = os.path.join(ifg_dir,
                f'{main_date}_{sec_date}_{main_id}_{sec_id}_{i}.int')
        subifg_files.append(subifg_file)
        interfere_subswath(main_img_file, sec_img_file, subifg_file,
                rowlook, collook)
    if nsubswath == 1:
        ci1 = CroppedImage.from_file(subifg_files[0], load_data = True)
        ci1.data.tofile(outfile)
    else:
        ci1 = CroppedImage.from_file(subifg_files[0])
        ifg1 = ci1.load_data()
        for i in range(1, nsubswath):
            ci2 = CroppedImage.from_file(subifg_files[i])
            ifg2 = ci2.load_data()
            mask = np.abs(ifg1) == 0
            ifg1[mask] = ifg2[mask]
        ifg1.tofile(outfile)
    for subifg_file in subifg_files:
        os.remove(subifg_file)
    return outfile

def parse_fname(fn:str)->Tuple[str, str]:
    basename = os.path.basename(fn)
    date = basename[0:8]
    data_id = basename[9:20]
    return date, data_id

def interfere(
        img_pair_file: str,
        rscfile: str,
        /,
        ifg_dir: str = 'igrams',
        rowlook: int = 1,
        collook: int = 1):
    """
    Form interferograms from a subswath list

    Parameters
    ----------
    img_pair_file: str
        File containing pairs of subswath images
    rscfile: str
        rsc file
    ifg_dir: str
        Directory to save interferograms
    rowlook: int
        Number of look in row direction
    collook: int
        Number of look in column direction
    """
    os.makedirs(ifg_dir, exist_ok = True)
    intlist_file = os.path.join(ifg_dir, 'intlist')
    rsc = geocoordinates.GeoCoordinates(rscfile)
    rsclook = rsc.take_look(rowlook,collook)
    rsclook.save_as_rsc(os.path.join(ifg_dir,'dem.rsc'))

    img_pairs = []
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
                    subsets.append((np.copy(curr_main_imgs),
                                    np.copy(curr_sec_imgs)))
                    curr_main_imgs = []
                    curr_sec_imgs = []
                else:
                    curr_main_imgs.append(main_list[i])
                    curr_sec_imgs.append(sec_list[i])
            else:
                break
        subsets.append((np.copy(curr_main_imgs),
                        np.copy(curr_sec_imgs)))
        line_idx += count
        ifg_files = []
        for main_img_files, sec_img_files in subsets:
            ifg_files.append(
                    interfere_single_scene(
                        main_img_files,
                        sec_img_files,
                        ifg_dir,
                        rowlook,
                        collook))
        stitch(ifg_files, intfile)
    fout = open(intlist_file,'w')
    fout.write('\n'.join(intlist))
    fout.close()
