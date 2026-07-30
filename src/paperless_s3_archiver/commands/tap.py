"""
tap: drain the spool the post-consume hook writes to, and archive each document

The authoritative path into the archive. A document becomes immutable here,
before anyone has had the chance to edit or delete it.
"""

import argparse
import datetime as dt
import json
import logging

import requests
from botocore.exceptions import ClientError

from paperless_s3_archiver.archiving import TapContext, archive_document
from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import Metric, journal, write_metrics
from paperless_s3_archiver.paperless import PaperlessAPI
from paperless_s3_archiver.retention import Undecidable
from paperless_s3_archiver.s3 import s3_client
from paperless_s3_archiver.state import State

LOG = logging.getLogger("paperless-archive")


def cmd_tap(cfg: Config, args: argparse.Namespace) -> int:
    """
    Archive every document waiting in the spool

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments. Unused; the tap takes no options.

    Returns
    -------
    :
        1 if any document could not be archived, 0 otherwise.
    """
    del args
    state = State(cfg)
    ctx = TapContext.build(
        cfg,
        client=s3_client(cfg, role="writer"),
        api=PaperlessAPI(api_base=cfg.api_base, entity=cfg.entity),
    )

    jobs = sorted(p for p in cfg.spool_dir.glob("*.json"))
    failures = 0
    for path in jobs:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("unreadable spool job %s: %s", path, exc)
            failures += 1
            continue
        try:
            outcome = archive_document(cfg, state=state, ctx=ctx, job=job)
        except (Undecidable, ClientError, requests.RequestException, OSError) as exc:
            # The job file stays in the spool, so the next run retries it and
            # the document is never quietly dropped. Completeness is the point.
            LOG.error("document %s failed: %s", job.get("document_id"), exc)
            journal(
                cfg,
                {"event": "error", "document_id": job.get("document_id"), "error": str(exc)},
            )
            failures += 1
            continue
        LOG.info("document %s: %s", job.get("document_id"), outcome)
        path.unlink()

    now = dt.datetime.now(dt.UTC).timestamp()
    if failures == 0:
        state.set("last_success", now)
    state.save()

    write_metrics(
        cfg,
        job="ingest",
        metrics=[
            Metric(
                name="paperless_ingest_last_success_timestamp_seconds",
                help_text="Unix time of the last tap run that archived everything it was given",
                metric_type="gauge",
                value=float(state.get("last_success", 0)),
            ),
            Metric(
                name="paperless_ingest_last_run_status",
                help_text="1 if the last tap run had no failures",
                metric_type="gauge",
                value=0 if failures else 1,
            ),
            Metric(
                name="paperless_ingest_archived_total",
                help_text="Documents written to the immutable bucket",
                metric_type="counter",
                value=float(state.get("archived_total", 0)),
            ),
            Metric(
                name="paperless_ingest_rejected_total",
                help_text="Documents refused because they carry no single valid retention class",
                metric_type="counter",
                value=float(state.get("rejected_total", 0)),
            ),
            Metric(
                name="paperless_ingest_indexed_not_archived_total",
                help_text="Documents in a class that is indexed but deliberately never archived",
                metric_type="counter",
                value=float(state.get("hr_indexed_total", 0)),
            ),
            Metric(
                name="paperless_ingest_spool_depth",
                help_text="Spool jobs still waiting, including ones a previous run could not complete",
                metric_type="gauge",
                value=float(len(list(cfg.spool_dir.glob("*.json")))),
            ),
        ],
    )
    return 1 if failures else 0
