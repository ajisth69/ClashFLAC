import argparse
import asyncio
import sys
from pathlib import Path

from amzdl._version import VERSION
from amzdl.api import auth
from amzdl.cli import cli, config, prompts
from amzdl.download import links
from amzdl.download.download import download, download_batch
from amzdl.metadata.search import SEARCH_TYPES, normalize_type, search_catalog


def _add_download_args(parser):
    parser.add_argument(
        "--account",
        default=None,
        help="Which stored account to use: customer id, name, or country code "
             "(default: config default_account, else the only/selected account)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Directory to save the file (default: config default_output)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--quality",
        default=None,
        metavar="TIER",
        help="Quality tier: a linear ceiling (LD/SD/HD/UHD or a sub-tier like "
             "SD_HIGH, HD_44, UHD_48) or a spatial tier (SPATIAL_ATMOS[_LOW/_MEDIUM/"
             "_HIGH], SPATIAL_RA360[_L0..L3]). Default: config default_quality",
    )
    parser.add_argument(
        "--wvd-path",
        default=None,
        help="Path to the Widevine device file (default: config default_wvd_path)",
    )
    parser.add_argument(
        "--metadata-concurrency",
        type=int,
        default=None,
        metavar="N",
        help="How many album-metadata lookups to run at once when downloading an "
             "artist (default: config default_metadata_concurrency)",
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=cli.paint(f"amzdl v{VERSION}", cli.CYAN),
        epilog="Manage accounts: `amzdl accounts`",
    )

    parser.add_argument(
        "content_asin",
        metavar="INPUT",
        help="What to download: an ASIN, an Amazon Music link, or a path to a text "
             "file of ASINs/links (one per line). Resolves a track, album, artist, "
             "or playlist (catalog or user). A link's `trackAsin=` selects one track",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"amzdl v{VERSION}",
    )
    _add_download_args(parser)

    return parser.parse_args(argv)


def parse_search_args(argv):
    parser = argparse.ArgumentParser(
        prog="amzdl search",
        description=cli.paint(f"amzdl v{VERSION} — search", cli.CYAN),
    )
    parser.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Search text (prompted for if omitted)",
    )
    parser.add_argument(
        "--type",
        default=None,
        choices=tuple(SEARCH_TYPES),
        help="What to search for (prompted for if omitted)",
    )
    parser.add_argument(
        "--search-limit",
        type=int,
        default=None,
        metavar="N",
        help="Max results to show (default: config default_search_limit)",
    )
    _add_download_args(parser)
    return parser.parse_args(argv)


async def run_search(args):
    cli.setup_logging(args.verbose)

    settings = config.get_settings()
    quality = args.quality or settings["default_quality"]
    wvd_path = config.resolve_wvd_path(args.wvd_path)
    output_dir = Path(args.output or settings["default_output"]).expanduser()
    concurrency = settings["default_concurrency"]
    metadata_concurrency = (
        args.metadata_concurrency or settings["default_metadata_concurrency"]
    )
    limit = args.search_limit or settings["default_search_limit"]

    session = auth.get_session(account=args.account)

    search_type = normalize_type(args.type) if args.type else None
    query = args.query
    if query is None:
        query = prompts.prompt_search_query(search_type)
    if search_type is None:
        search_type = prompts.prompt_search_type(tuple(SEARCH_TYPES))

    results = await asyncio.to_thread(
        search_catalog, session, query, search_type, limit
    )
    if not results:
        cli.note(f"No {search_type}s found for '{query}'.")
        return

    choice = prompts.prompt_search_results(search_type, [r.fields for r in results])
    if choice is None:
        return

    if wvd_path is not None and not Path(wvd_path).exists():
        cli.print_error(f"Widevine device not found: {wvd_path}")
        sys.exit(1)

    type_hint = search_type if settings["use_link_hints"] else None
    await download(session, results[choice].asin, output_dir, quality, wvd_path,
                   plain=args.verbose, concurrency=concurrency,
                   metadata_concurrency=metadata_concurrency, type_hint=type_hint)


async def run_download(args):
    cli.setup_logging(args.verbose)

    settings = config.get_settings()
    quality = args.quality or settings["default_quality"]
    wvd_path = config.resolve_wvd_path(args.wvd_path)
    output_dir = Path(args.output or settings["default_output"]).expanduser()
    concurrency = settings["default_concurrency"]
    metadata_concurrency = (
        args.metadata_concurrency or settings["default_metadata_concurrency"]
    )

    try:
        asins = links.resolve_inputs(args.content_asin)
    except (ValueError, OSError) as exc:
        cli.print_error(f"Could not parse input: {exc}")
        sys.exit(1)
    if not asins:
        cli.print_error("No ASINs or links found in input")
        sys.exit(1)

    if wvd_path is not None and not Path(wvd_path).exists():
        cli.print_error(f"Widevine device not found: {wvd_path}")
        sys.exit(1)

    hint = (
        links.hint(args.content_asin)
        if settings["use_link_hints"]
        else links.LinkHint()
    )

    session = auth.get_session(account=args.account, country_hint=hint.country)

    source = links.input_file_label(args.content_asin)
    if source is not None:
        await download_batch(session, source, asins, output_dir, quality, wvd_path,
                             plain=args.verbose, concurrency=concurrency,
                             metadata_concurrency=metadata_concurrency)
    else:
        type_hint = hint.type
        await download(session, asins[0], output_dir, quality, wvd_path,
                       plain=args.verbose, concurrency=concurrency,
                       metadata_concurrency=metadata_concurrency,
                       type_hint=type_hint)


def _account_options():
    accounts = config.load_accounts()
    ids = list(accounts.keys())
    options = [auth._name_region(info) for info in accounts.values()]
    return ids, options


def _add_account(country=None):
    country = country or prompts.prompt_region()
    try:
        auth.login(country)
    except (ValueError, TypeError) as exc:
        cli.print_error(f"Add account failed: {exc}")
        sys.exit(1)
    _, options = _account_options()
    prompts.print_account_summary("Account added", options)


def run_accounts(argv):
    if argv:
        parser = argparse.ArgumentParser(
            prog="amzdl accounts",
            description="Add or remove an Amazon Music account.",
        )
        parser.add_argument(
            "--add",
            nargs="?",
            const="",
            default=None,
            metavar="COUNTRY",
            help="Sign in and add an account (optionally for a given 2-letter region).",
        )
        parser.add_argument(
            "--delete",
            default=None,
            metavar="ACCOUNT_ID",
            help="Remove a stored account by customer id, name, or country code.",
        )
        args = parser.parse_args(argv)

        if args.delete is not None:
            try:
                info = auth.delete_account(args.delete)
            except KeyError:
                cli.print_error(f"No stored account matching '{args.delete}'.")
                sys.exit(1)
            name, region = auth._name_region(info)
            cli.note(f"Removed account '{name}' ({region}).")
        elif args.add is not None:
            _add_account(args.add or None)
        else:
            parser.error("specify --add or --delete")
        return

    while True:
        ids, options = _account_options()
        if not ids:
            cli.note("No accounts stored yet.")
            _add_account()
            continue

        choice = prompts.prompt_manage_account(options)
        if choice == "quit":
            return
        if choice == "add":
            _add_account()
            continue

        account_id = ids[choice]
        name, region = options[choice]
        if prompts.confirm_delete(name, region):
            auth.delete_account(account_id)
            cli.note(f"Removed account '{name}' ({region}).")


def main():
    argv = sys.argv[1:]
    verbose = "-v" in argv or "--verbose" in argv
    try:
        if argv and argv[0] in ("accounts", "account"):
            run_accounts(argv[1:])
        elif argv and argv[0] == "search":
            asyncio.run(run_search(parse_search_args(argv[1:])))
        else:
            asyncio.run(run_download(parse_args(argv)))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        if verbose:
            raise
        cli.print_error(str(exc) or type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
