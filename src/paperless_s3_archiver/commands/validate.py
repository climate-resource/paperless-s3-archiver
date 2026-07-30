"""
validate: check a configuration before it can write anything permanent
"""

import argparse
from pathlib import Path

from paperless_s3_archiver.config import Config, load_config
from paperless_s3_archiver.validation import validate


def cmd_validate(cfg: Config, args: argparse.Namespace) -> int:
    """
    Print every finding for this config, and fail on the ones that matter

    Parameters
    ----------
    cfg
        The entity's config. Loading it has already refused the structural
        mistakes; what is left needs the date or a second entity to decide.
    args
        Parsed arguments, carrying `peer` config paths and `max_public_days`.

    Returns
    -------
    :
        1 if any finding is an error, 0 otherwise. Warnings are printed and do
        not fail the run: a divergent shared grant is allowed, it just has to be
        seen.
    """
    peers = [load_config(Path(p)) for p in args.peer]
    findings = validate(cfg, peers=peers, max_days=args.max_public_days)
    for finding in findings:
        print(finding)
    return 1 if any(f.severity == "error" for f in findings) else 0
