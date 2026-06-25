def main() -> int:
    """
    Top-level command line interface
    """

    import os
    import sys

    from s1proc import __version__

    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(__version__)
        raise SystemExit(os.EX_OK)

    import tyro

    from s1proc._config import initialize_config
    from s1proc.coherence import run_coherence, run_multilook_amp
    from s1proc.interfere import run_interfere
    from s1proc.phase_correction import phase_correction
    from s1proc.preproc import preprocess
    from s1proc.sentinel_stack import run_stack
    from s1proc.unwrap import batch_unwrap
    from s1proc.utils import run_check_integrity, run_create_slc_pair_list

    tyro.extras.subcommand_cli_from_dict({
        "init": initialize_config,
        "integrity": run_check_integrity,
        "preproc": preprocess,
        "stack": run_stack,
        "slcpairs": run_create_slc_pair_list,
        "interfere": run_interfere,
        "unwrap": batch_unwrap,
        "amp": run_multilook_amp,
        "coh": run_coherence,
        "phasecorr": phase_correction,
    })
    return os.EX_OK


if __name__ == "__main__":
    main()
