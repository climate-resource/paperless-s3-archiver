"""
The command line

One entity per invocation. Everything that differs between entities lives in the
JSON config named by ``--config``, so no code here has to know which entity it is
running for -- which is what makes a second entity a configuration change rather
than a fork.
"""

import argparse
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from paperless_b2_archiver.b2 import MissingCredentials
from paperless_b2_archiver.commands.export import (
    NoExportAvailable,
    cmd_auditor_export,
    cmd_export,
    cmd_fetch_export,
)
from paperless_b2_archiver.commands.extend_retention import RefusedExtension, cmd_extend_retention
from paperless_b2_archiver.commands.inventory import cmd_inventory
from paperless_b2_archiver.commands.metrics import cmd_metrics
from paperless_b2_archiver.commands.reconcile import cmd_reconcile
from paperless_b2_archiver.commands.tap import cmd_tap
from paperless_b2_archiver.commands.validate import cmd_validate
from paperless_b2_archiver.config import Config, load_config
from paperless_b2_archiver.exporting import ExportFilterLeak
from paperless_b2_archiver.paperless import MissingToken
from paperless_b2_archiver.validation import DEFAULT_PUBLIC_MAX_DAYS

Command = Callable[[Config, argparse.Namespace], int]

COMMANDS: dict[str, Command] = {
    "tap": cmd_tap,
    "export": cmd_export,
    "auditor-export": cmd_auditor_export,
    "fetch-export": cmd_fetch_export,
    "inventory": cmd_inventory,
    "reconcile": cmd_reconcile,
    "extend-retention": cmd_extend_retention,
    "metrics": cmd_metrics,
    "validate": cmd_validate,
}

#: Refusals, as opposed to crashes. Each one is a decision this program made on
#: purpose, so it prints as a message and an exit status rather than a traceback.
REFUSALS = (
    ExportFilterLeak,
    MissingCredentials,
    MissingToken,
    NoExportAvailable,
    RefusedExtension,
)

DESCRIPTION = """\
Write paperless-ngx documents to a WORM object store under Object Lock.

Paperless-ngx is the index and the UI. It is not the archive of record: that is
the bucket this program writes to, where every object carries a server-side
retain-until date that nobody -- including the account owner holding the master
key -- can shorten.
"""


def build_parser() -> argparse.ArgumentParser:
    """
    The argument parser for every subcommand

    Returns
    -------
    :
        The parser.
    """
    parser = argparse.ArgumentParser(
        prog="paperless-archive",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to the entity's archive config. Defaults to $PAPERLESS_ARCHIVE_CONFIG",
    )
    parser.add_argument(
        "--runtime",
        default=None,
        choices=["docker"],
        help="How to reach the paperless deployment. Defaults to what the config says",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Log at DEBUG rather than INFO",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("tap", help="Archive every document waiting in the spool")
    sub.add_parser("export", help="Export, filter and upload the nightly snapshot")
    sub.add_parser("inventory", help="Write a dated inventory of the bucket into the bucket")
    sub.add_parser("reconcile", help="Compare paperless against the bucket, both directions")
    sub.add_parser("metrics", help="Exposure, break-glass logins and liveness")

    auditor = sub.add_parser("auditor-export", help="Build a Z3 handover for one engagement")
    auditor.add_argument("target", help="Directory to build the handover in")

    fetch = sub.add_parser("fetch-export", help="Download the most recent export from the bucket")
    fetch.add_argument("target", help="Directory to download the latest export into")

    extend = sub.add_parser(
        "extend-retention",
        help="Push retain-until out where the config now says longer",
    )
    extend.add_argument(
        "--apply",
        action="store_true",
        help="Write the new dates. Without this the run only reports what it would do",
    )
    extend.add_argument(
        "--reason",
        default="",
        help="Why the period changed. Required with --apply, and locked into the bucket",
    )
    extend.add_argument(
        "--class",
        dest="retention_class",
        default="",
        help="Narrow to one retention class",
    )
    extend.add_argument("--grant", default="", help="Narrow to documents tagged with one grant slug")

    check = sub.add_parser("validate", help="Check a config before it can write anything permanent")
    check.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="PATH",
        help="Another entity's config, for the shared-grant report. Repeatable",
    )
    check.add_argument(
        "--max-public-days",
        type=int,
        default=DEFAULT_PUBLIC_MAX_DAYS,
        help="The longest audit-mode window that may be opened in one go",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """
    Run one subcommand

    Parameters
    ----------
    argv
        The arguments, defaulting to `sys.argv`.

    Returns
    -------
    :
        The process exit status.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    config_path = args.config or os.environ.get("PAPERLESS_ARCHIVE_CONFIG", "")
    if not config_path:
        parser.error("--config or PAPERLESS_ARCHIVE_CONFIG is required")

    cfg = load_config(Path(config_path))
    if args.runtime:
        cfg = cfg.model_copy(update={"runtime": args.runtime})

    return COMMANDS[args.command](cfg, args)


def entry_point() -> None:
    """
    The console script

    Turns a deliberate refusal into a message and an exit status. A refusal is
    this program declining to do something permanent on incomplete information,
    and a traceback would suggest a bug instead.
    """
    try:
        sys.exit(main())
    except REFUSALS as exc:
        print(f"refused: {exc}", file=sys.stderr)
        sys.exit(2)
