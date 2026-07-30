"""
reconcile: compare paperless against the bucket, in both directions
"""

import argparse
import datetime as dt
import json
import logging
from typing import Any

import requests
from botocore.exceptions import ClientError

from paperless_b2_archiver.b2 import latest_inventory, s3_client
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.observability import Metric, journal, write_metrics
from paperless_b2_archiver.paperless import PaperlessAPI
from paperless_b2_archiver.retention import resolve_class
from paperless_b2_archiver.state import State

LOG = logging.getLogger("paperless-archive")


class NoInventory(Exception):
    """Nothing to reconcile against: the bucket holds no inventory yet."""


def _archived_document_ids(cfg: Config, *, client: Any) -> set[int]:
    """
    Every paperless document id the bucket claims to hold

    The bucket key is a sha256 the tap computed; paperless stores an md5. The
    sidecar is the only artefact that claims both identities, so the document id
    it records is what the two sides are joined on. Every sidecar is read once
    and indexed, rather than searching the bucket per document -- the second
    shape is quadratic and quietly stops finishing.

    Parameters
    ----------
    cfg
        The entity's config.
    client
        A reader-role S3 client.

    Returns
    -------
    :
        The ids named by the sidecars in the most recent inventory.

    Raises
    ------
    NoInventory
        When no inventory has been written yet, so there is nothing to compare
        paperless against.
    """
    inventory = latest_inventory(client=client, cfg=cfg)
    if inventory is None:
        raise NoInventory("no inventory in the bucket yet")

    archived_ids: set[int] = set()
    for obj in inventory["objects"]:
        key = obj["key"]
        if not (key.startswith("documents/") and key.endswith(".json")):
            continue
        body = client.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()
        doc_id = json.loads(body).get("paperless_document_id")
        if doc_id is not None:
            archived_ids.add(int(doc_id))
    return archived_ids


def cmd_reconcile(cfg: Config, args: argparse.Namespace) -> int:
    """
    Check paperless and the bucket against each other

    Both directions matter. A document in an archived class that is missing from
    the bucket is a failed ingest or a deletion. A document in a never-archived
    class that is *present* in the bucket is a leak of material that should never
    have been locked, and it cannot be taken back -- so it is the one that pages.

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments. Unused.

    Returns
    -------
    :
        1 if the reconciliation could not be completed, 0 otherwise. Gaps and
        leaks are reported through the metrics, not the exit status: they are
        findings about the archive rather than failures of this job.
    """
    del args
    state = State(cfg)
    status = 0
    gaps: list[dict[str, Any]] = []
    leaks: list[dict[str, Any]] = []

    try:
        client = s3_client(cfg, role="reader")
        archived_ids = _archived_document_ids(cfg, client=client)

        api = PaperlessAPI(api_base=cfg.api_base, entity=cfg.entity)
        tag_names = {t["id"]: t["name"] for t in api.all_pages("/tags/")}
        keep = cfg.archived_classes()

        for doc in api.all_pages("/documents/"):
            names = [tag_names.get(pk, "") for pk in doc.get("tags", [])]
            class_name = resolve_class(cfg, names)
            archived_here = doc["id"] in archived_ids

            if class_name in keep and not archived_here:
                gaps.append({"document_id": doc["id"], "class": class_name, "title": doc.get("title")})
            if class_name not in keep and archived_here:
                leaks.append({"document_id": doc["id"], "class": class_name, "title": doc.get("title")})

        if leaks:
            state.bump("worm_leak_total", len(leaks))
        state.set("reconcile_last_success", dt.datetime.now(dt.UTC).timestamp())
        journal(cfg, {"event": "reconcile", "gaps": gaps, "leaks": leaks})
    except (ClientError, requests.RequestException, OSError, NoInventory) as exc:
        LOG.error("reconcile failed: %s", exc)
        journal(cfg, {"event": "reconcile_failed", "error": str(exc)})
        status = 1

    state.save()
    write_metrics(
        cfg,
        job="reconcile",
        metrics=[
            Metric(
                name="paperless_reconcile_last_success_timestamp_seconds",
                help_text="Unix time of the last successful reconciliation",
                metric_type="gauge",
                value=float(state.get("reconcile_last_success", 0)),
            ),
            Metric(
                name="paperless_reconcile_last_run_status",
                help_text="1 if the last reconciliation ran cleanly",
                metric_type="gauge",
                value=0 if status else 1,
            ),
            Metric(
                name="paperless_reconcile_gap_count",
                help_text="Documents in an archived class that are missing from the bucket",
                metric_type="gauge",
                value=float(len(gaps)),
            ),
            Metric(
                name="paperless_worm_hr_leak_total",
                help_text=(
                    "Documents in a never-archived class found in the WORM bucket. Unfixable once locked"
                ),
                metric_type="counter",
                value=float(state.get("worm_leak_total", 0)),
            ),
        ],
    )
    return status
