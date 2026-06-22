import os
import subprocess
from typing import Sequence

from s1proc import get_bin_path
from s1proc._log import setup_logger

logger = setup_logger(name=__name__, level="INFO")


def _ps_selection(
    ifg_list: Sequence[str],
    nrow: int,
    ncol: int,
    ps_file: str,
    med_sim_outfile: str,
    max_sim_outfile: str,
    nneigh: int,
    rdmin: int,
    rdmax: int,
    med_sim_th: float,
    max_sim_th: float,
):
    """
    Run PS selection for an interferogram list

    Parameters
    ----------
    ifg_list: Sequence[str]
        List of interferograms to run phase similarity computation
    nrow: int
        Number of rows of each interferogram
    ncol: int
        Number of columns of each interferogram
    ps_file: str
        Int32 binary file with 1 representing PS candidates and 0 non-PS pixels
    med_sim_outfile: str
        Output file for median phase similarity
    max_sim_outfile: str
        Output file for maximum phase similarity
    nneigh: int
        Number of nearest neighbor pixels for phase similarity calculation
    rdmin: int
        Minimum radius for PS search
    rdmax: int
        Maximum radius for PS search
    med_sim_th: float
        Median phase similarity threshold for PS candidate selection
    max_sim_th: float
        Maximum phase similarity threshold for PS candidate selection
    """
    temp_dir = "temp"
    os.makedirs(temp_dir, exist_ok=True)
    basename1 = os.path.basename(ifg_list[0])
    basename2 = os.path.basename(ifg_list[1])
    infile = os.path.join(temp_dir, f"infile_{basename1[0:17]}_{basename2[0:17]}.txt")
    nifg = len(ifg_list)
    logger.info(f"number of interferograms: {nifg}")
    with open(infile, "w") as f:
        f.write(f"{nifg} {nrow} {ncol}\n")
        f.write("\n".join(ifg_list))
    phase_similarity = get_bin_path("phase_similarity")
    subprocess.run(
        [
            phase_similarity,
            infile,
            ps_file,
            med_sim_outfile,
            max_sim_outfile,
            str(nneigh),
            str(rdmin),
            str(rdmax),
            str(med_sim_th),
            str(max_sim_th),
        ],
        check=True,
    )
    return
