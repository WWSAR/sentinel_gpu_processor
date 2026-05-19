
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
    from s1proc.utils import create_slc_pair_list
    from s1proc.utils import mid_orbit, los
    from s1proc.stitch import main as stitch_ifg
    from s1proc.interfere import interfere
    from s1proc.sentinel_stack import stack as stack
    from s1proc.unwrap import batch_snaphu
    from s1proc.coherence import multilook_amp, coherence
    from s1proc.query import query_asf
    from s1proc.utils import check_integrity
    tyro.extras.subcommand_cli_from_dict(
        {
            "interfere": interfere,
            "slcpairs": create_slc_pair_list,
            "stitch": stitch_ifg,
            "midorb": mid_orbit,
            "stack": stack,
            "unwrap": batch_snaphu,
            "amp": multilook_amp,
            "coh": coherence,
            "los": los,
            "query": query_asf,
            "integrity": check_integrity
        })
    return os.EX_OK

if __name__ == "__main__":
    main()
