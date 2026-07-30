"""
inventory: list the bucket and write a dated, immutable inventory back into it
"""

import argparse
import datetime as dt
import json
import logging

from botocore.exceptions import ClientError

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import Metric, journal, write_metrics
from paperless_s3_archiver.retention import year_end
from paperless_s3_archiver.s3 import list_bucket, put_locked, s3_client
from paperless_s3_archiver.state import State

LOG = logging.getLogger("paperless-archive")


def cmd_inventory(cfg: Config, args: argparse.Namespace) -> int:
    """
    Write today's inventory of the bucket into the bucket, under lock

    The inventory is therefore itself immutable and dated, which turns "what was
    in the archive on this date" into a question with a tamper-evident answer --
    without asking anyone to trust the application or us.

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments. Unused.

    Returns
    -------
    :
        1 if the inventory could not be produced or written, 0 otherwise.
    """
    del args
    state = State(cfg)
    status = 0
    count = total_bytes = 0
    try:
        # Two credentials, on purpose. The reader enumerates the bucket and
        # reads retention state; the writer only puts the finished inventory.
        # The writer holds no read capability at all, so it could not do the
        # first half even if this asked it to.
        reader = s3_client(cfg, role="reader")
        client = s3_client(cfg, role="writer")
        objects = list_bucket(client=reader, cfg=cfg)

        # Retention state is what an auditor actually checks, so it belongs in
        # the inventory rather than only in the bucket's live metadata.
        for obj in objects:
            if obj["key"].startswith("inventory/"):
                continue
            try:
                head = reader.head_object(Bucket=cfg.bucket, Key=obj["key"])
                obj["object_lock_mode"] = head.get("ObjectLockMode", "")
                retain = head.get("ObjectLockRetainUntilDate")
                obj["retain_until"] = retain.isoformat() if retain else ""
                obj["legal_hold"] = head.get("ObjectLockLegalHoldStatus", "")
            except ClientError as exc:
                obj["head_error"] = str(exc)

        count = len(objects)
        total_bytes = sum(o["size"] for o in objects)
        stamp = dt.date.today().isoformat()
        document = {
            "entity": cfg.entity,
            "bucket": cfg.bucket,
            "generated_at": dt.datetime.now(dt.UTC).isoformat(),
            "object_lock_mode": cfg.object_lock_mode,
            "object_count": count,
            "total_bytes": total_bytes,
            "objects": objects,
        }
        put_locked(
            client=client,
            cfg=cfg,
            key=f"inventory/{stamp}.json",
            body=json.dumps(document, indent=2, sort_keys=True).encode("utf-8"),
            retain_until=year_end(dt.date.today().year + cfg.export_retain_years),
            legal_hold=False,
            content_type="application/json",
        )
        state.set("inventory_last_success", dt.datetime.now(dt.UTC).timestamp())
        journal(cfg, {"event": "inventory", "objects": count, "bytes": total_bytes})
    except (ClientError, OSError) as exc:
        LOG.error("inventory failed: %s", exc)
        journal(cfg, {"event": "inventory_failed", "error": str(exc)})
        status = 1

    state.save()
    write_metrics(
        cfg,
        job="inventory",
        metrics=[
            Metric(
                name="paperless_inventory_last_success_timestamp_seconds",
                help_text="Unix time of the last successful bucket inventory",
                metric_type="gauge",
                value=float(state.get("inventory_last_success", 0)),
            ),
            Metric(
                name="paperless_inventory_last_run_status",
                help_text="1 if the last inventory ran cleanly",
                metric_type="gauge",
                value=0 if status else 1,
            ),
            Metric(
                name="paperless_inventory_objects",
                help_text="Objects in the archive bucket",
                metric_type="gauge",
                value=float(count),
            ),
            Metric(
                name="paperless_inventory_bytes",
                help_text="Bytes in the archive bucket",
                metric_type="gauge",
                value=float(total_bytes),
            ),
        ],
    )
    return status
