"""
export, auditor-export and fetch-export

Three different artefacts, deliberately not one. The nightly export is the
restore path and carries every archived class. The Z3 handover is filtered
harder, because a Betriebsprüfer's access covers tax records and not personnel
files. Fetching is the way back in on a host that holds nothing but this program
and the reader key.
"""

import argparse
import datetime as dt
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from botocore.exceptions import ClientError

from paperless_b2_archiver.b2 import list_bucket, s3_client, sync_tree
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.exporting import ExportFilterLeak, filter_export
from paperless_b2_archiver.observability import Metric, journal, write_metrics
from paperless_b2_archiver.retention import year_end
from paperless_b2_archiver.runtime import get_runtime
from paperless_b2_archiver.state import State

LOG = logging.getLogger("paperless-archive")

#: ``exports/<date>/<path>`` -- anything shallower is not a file inside an export.
EXPORT_KEY_SEPARATORS = 2


class NoExportAvailable(Exception):
    """There is no export to work from, locally or in the bucket."""


def cmd_export(cfg: Config, args: argparse.Namespace) -> int:
    """
    Run `document_exporter`, filter the result, and upload it under lock

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments. Unused; the nightly export takes no options.

    Returns
    -------
    :
        1 if the export or the upload failed, 0 otherwise.
    """
    del args
    state = State(cfg)
    status = 0
    kept = removed = uploaded = 0
    try:
        full = cfg.export_dir / "full"
        full.mkdir(parents=True, exist_ok=True)

        # The full export, personnel material included, goes only to the ordinary
        # backup chain: it lives in the data tree, ages out with the ordinary
        # backup retention, and stays deletable.
        runtime = get_runtime(name=cfg.runtime, entity=cfg.entity, compose_dir=cfg.compose_dir)
        runtime.run_document_exporter(f"{cfg.container_export_root}/full")

        # The copy that goes to the bucket is filtered first, and only then
        # uploaded. Filtering after upload is not a thing that exists.
        with tempfile.TemporaryDirectory(dir=str(cfg.export_dir)) as tmp:
            worm = Path(tmp) / "worm"
            shutil.copytree(full, worm)
            kept, removed = filter_export(cfg, root=worm, keep=cfg.archived_classes())
            stamp = dt.date.today().isoformat()
            uploaded = sync_tree(
                cfg=cfg,
                client=s3_client(cfg, role="writer"),
                root=worm,
                prefix=f"exports/{stamp}/",
                retain_until=year_end(dt.date.today().year + cfg.export_retain_years),
            )

        state.set("export_last_success", dt.datetime.now(dt.UTC).timestamp())
        journal(cfg, {"event": "export", "kept": kept, "removed": removed, "objects": uploaded})
    except (
        subprocess.SubprocessError,
        ClientError,
        ExportFilterLeak,
        OSError,
        ValueError,
    ) as exc:
        LOG.error("export failed: %s", exc)
        journal(cfg, {"event": "export_failed", "error": str(exc)})
        status = 1

    state.save()
    write_metrics(
        cfg,
        job="export",
        metrics=[
            Metric(
                name="paperless_export_last_success_timestamp_seconds",
                help_text="Unix time of the last successful nightly export and upload",
                metric_type="gauge",
                value=float(state.get("export_last_success", 0)),
            ),
            Metric(
                name="paperless_export_last_run_status",
                help_text="1 if the last export ran and uploaded cleanly",
                metric_type="gauge",
                value=0 if status else 1,
            ),
            Metric(
                name="paperless_export_documents",
                help_text="Documents in the filtered export that was uploaded",
                metric_type="gauge",
                value=float(kept),
            ),
            Metric(
                name="paperless_export_filtered_documents",
                help_text="Documents the filter removed before upload because their class is never archived",
                metric_type="gauge",
                value=float(removed),
            ),
        ],
    )
    return status


def cmd_auditor_export(cfg: Config, args: argparse.Namespace) -> int:
    """
    Build a Z3 handover: machine-evaluable, and scoped to what an auditor may see

    Deliberately separate from the nightly export. The nightly one carries every
    archived class, including the contract and pension material, because it is
    the restore path. A Betriebsprüfer's access covers tax records, not personnel
    files, so the handover is filtered harder and is produced for a named
    engagement rather than on a timer.

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments, carrying the target directory.

    Returns
    -------
    :
        0. Any problem raises instead, because this runs interactively.

    Raises
    ------
    NoExportAvailable
        When no nightly export has been produced to build the handover from.
    """
    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    full = cfg.export_dir / "full"
    if not (full / "manifest.json").exists():
        raise NoExportAvailable(f"No export at {full}. Run `paperless-archive export` first.")
    shutil.copytree(full, target, dirs_exist_ok=True)
    kept, removed = filter_export(cfg, root=target, keep=cfg.tax_and_grant_classes())
    journal(
        cfg,
        {"event": "auditor_export", "target": str(target), "kept": kept, "removed": removed},
    )
    print(f"Z3 handover at {target}: {kept} documents, {removed} withheld as out of scope")
    return 0


def cmd_fetch_export(cfg: Config, args: argparse.Namespace) -> int:
    """
    Download the most recent export from the bucket, and print the date it found

    Used by the restore drill, and by a human rebuilding the archive on a host
    that has nothing but this program and the reader key. The whole point of the
    archive being plain S3 objects with a manifest is that this step needs no
    application and no knowledge of paperless.

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments, carrying the target directory.

    Returns
    -------
    :
        0.

    Raises
    ------
    NoExportAvailable
        When the bucket holds no export yet.
    """
    client = s3_client(cfg, role="reader")
    prefixes = {
        obj["key"].split("/")[1]
        for obj in list_bucket(client=client, cfg=cfg)
        if obj["key"].startswith("exports/") and obj["key"].count("/") >= EXPORT_KEY_SEPARATORS
    }
    if not prefixes:
        raise NoExportAvailable(f"no export under exports/ in {cfg.bucket} to restore from")
    latest = max(prefixes)  # ISO dates sort lexically

    target = Path(args.target)
    target.mkdir(parents=True, exist_ok=True)
    prefix = f"exports/{latest}/"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            dest = target / obj["Key"][len(prefix) :]
            dest.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(cfg.bucket, obj["Key"], str(dest))
    print(latest)
    return 0
