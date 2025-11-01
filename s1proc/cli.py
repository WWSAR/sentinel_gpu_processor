
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
    from s1proc.slc_pairs import create_slc_pair_list
    from s1proc.stitch import main as stitch_ifg
    tyro.extras.subcommand_cli_from_dict(
        {
            "slcpairs": create_slc_pair_list,
            "stitch": stitch_ifg
        })
    return os.EX_OK

if __name__ == "__main__":
    main()
