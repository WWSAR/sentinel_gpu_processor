def main()->int:
    """
    Top-level command line interface
    """

    import os
    import sys
    from s1proc import __version__

    if len(sys.argv) > 1 and sys.argv[1] == '--version':
        print(__version__)
        raise SystemExit(os.EX_OK)

    import tyro
    from s1proc._config import initialize_config
    from s1proc.utils import run_create_slc_pair_list
    from s1proc.interfere import run_interfere
    from s1proc.sentinel_stack import run_stack
    from s1proc.unwrap import run_batch_snaphu
    from s1proc.coherence import run_multilook_amp, run_coherence
    from s1proc.query import query_asf
    from s1proc.utils import run_check_integrity
    from s1proc.phase_correction import phase_correction
    tyro.extras.subcommand_cli_from_dict(
        {
            "init": initialize_config,
            "query": query_asf,
            "integrity": run_check_integrity,
            "stack": run_stack,
            "slcpairs": run_create_slc_pair_list,
            "interfere": run_interfere,
            "unwrap": run_batch_snaphu,
            "amp": run_multilook_amp,
            "coh": run_coherence,
            "phasecorr": phase_correction
        })
    return os.EX_OK

if __name__ == "__main__":
    main()
