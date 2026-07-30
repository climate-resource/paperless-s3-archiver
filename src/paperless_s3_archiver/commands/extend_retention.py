"""
extend-retention: move retain-until dates out where the config now says longer
"""

import argparse
import datetime as dt
import json
import logging
from typing import Any

from botocore.exceptions import ClientError

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import Metric, journal, write_metrics
from paperless_s3_archiver.retention import Undecidable, recompute_retention, tag_suffix, year_end
from paperless_s3_archiver.s3 import list_bucket, put_locked, s3_client
from paperless_s3_archiver.state import State

LOG = logging.getLogger("paperless-archive")


class RefusedExtension(Exception):
    """The run was refused before anything was read or written."""


def _guard(*, cfg: Config, args: argparse.Namespace) -> None:
    """
    Refuse the two runs that must never start

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments.

    Raises
    ------
    RefusedExtension
        For an `--apply` with no reason, or an `--apply` during burn-in.
    """
    if args.apply and not args.reason.strip():
        raise RefusedExtension(
            "--apply needs --reason: the extension is permanent, and the reason for it "
            "is written into the bucket as the record of why the period changed."
        )
    if args.apply and cfg.burn_in:
        # Burn-in objects carry a seven-day GOVERNANCE date and are disposable by
        # design -- nothing counts as archived until it is re-ingested under
        # COMPLIANCE. Extending one to a real period would strand the burn-in
        # bucket for a decade, which is the same trap the tap sidesteps by
        # applying no legal holds while the bucket is in GOVERNANCE mode.
        raise RefusedExtension(
            f"{cfg.entity} is in {cfg.object_lock_mode} burn-in, where everything written "
            "is disposable. Extending retention now would lock disposable objects for real "
            "periods. Dry runs are fine; --apply waits for COMPLIANCE mode."
        )


def _plan(
    *, cfg: Config, args: argparse.Namespace, reader: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Work out which objects the config now says should be held longer

    Parameters
    ----------
    cfg
        The entity's config, as it reads today.
    args
        Parsed arguments, carrying the class and grant filters.
    reader
        A reader-role S3 client.

    Returns
    -------
    :
        ``(plan, problems)``. The plan holds one entry per object to move.
    """
    plan: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    for obj in list_bucket(client=reader, cfg=cfg):
        key = obj["key"]
        if not (key.startswith("documents/") and key.endswith(".json")):
            continue
        doc_key = key[: -len(".json")]

        body = reader.get_object(Bucket=cfg.bucket, Key=key)["Body"].read()
        sidecar = json.loads(body)
        tags = sidecar.get("tags") or []

        if args.retention_class and sidecar.get("retention_class") != args.retention_class:
            continue
        if args.grant and tag_suffix(tags, prefix=cfg.grant_tag_prefix) != args.grant:
            continue

        try:
            want, _legal_hold, why = recompute_retention(cfg, sidecar=sidecar, doc_key=doc_key)
        except Undecidable as exc:
            problems.append({"key": doc_key, "error": str(exc)})
            continue

        # The document and its sidecar were written with the same date and are
        # moved together. A record whose metadata outlives it, or the reverse,
        # is not a record.
        for target in (doc_key, key):
            try:
                head = reader.head_object(Bucket=cfg.bucket, Key=target)
            except ClientError as exc:
                problems.append({"key": target, "error": str(exc)})
                continue
            have = head.get("ObjectLockRetainUntilDate")
            if have is not None and have >= want:
                # Already held at least this long. Never attempt the other
                # direction: the store refuses it under COMPLIANCE, and asking
                # is not something this program should ever do.
                continue
            plan.append(
                {
                    "key": target,
                    "class": sidecar.get("retention_class"),
                    "retain_until_before": have.isoformat() if have else "",
                    "retain_until_after": want.isoformat(),
                    "explanation": why,
                }
            )
    return plan, problems


def _apply(
    *, cfg: Config, writer: Any, plan: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Write the planned dates, one object at a time

    A failure on one object is not allowed to hide the ones that already moved:
    a date this program has changed must end up in the trail whatever happens to
    the rest of the run.

    Parameters
    ----------
    cfg
        The entity's config.
    writer
        A writer-role S3 client.
    plan
        The objects to move, from :func:`_plan`.

    Returns
    -------
    :
        ``(moved, problems)``.
    """
    moved: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []
    for item in plan:
        try:
            writer.put_object_retention(
                Bucket=cfg.bucket,
                Key=item["key"],
                Retention={
                    # COMPLIANCE unconditionally: --apply refuses to run in
                    # burn-in, so there is no GOVERNANCE object to reach here.
                    "Mode": "COMPLIANCE",
                    "RetainUntilDate": dt.datetime.fromisoformat(item["retain_until_after"]),
                },
            )
        except ClientError as exc:
            problems.append({"key": item["key"], "error": str(exc)})
            continue
        moved.append(item)
    return moved, problems


def cmd_extend_retention(cfg: Config, args: argparse.Namespace) -> int:
    """
    Extend retain-until where the config now says the record must be held longer

    Retention under COMPLIANCE can be extended and never shortened, so extending
    is the only direction anything here can move. That is what makes this safe to
    run declaratively: the entity's config is the source of truth, and the bucket
    converges towards it one object at a time.

    It exists for a period that grew after the objects were written. An EU grant
    extension moves the final payment year and every retain-until derived from it;
    a change in the law does the same to a whole class -- BEG IV moved
    Buchungsbelege from ten years to eight, and the next change will arrive the
    same way. Nothing notifies us of either, which is why the annual review exists,
    and why this has to still work years later.

    Scoped to ``documents/``, deliberately. Exports and inventories carry a flat
    period of their own rather than one derived from a class, so nothing in the
    config can change what they should be holding.

    Parameters
    ----------
    cfg
        The entity's config.
    args
        Parsed arguments: ``apply``, ``reason``, ``retention_class``, ``grant``.

    Returns
    -------
    :
        1 if anything could not be explained or written, 0 otherwise.

    Raises
    ------
    RefusedExtension
        For an `--apply` with no reason, or during burn-in.
    """
    _guard(cfg=cfg, args=args)

    state = State(cfg)
    status = 0
    applied = 0
    plan: list[dict[str, Any]] = []
    moved: list[dict[str, Any]] = []
    problems: list[dict[str, Any]] = []

    try:
        # The reader enumerates and reads retention state. The writer holds no
        # read capability at all and is only asked for once there is something
        # to write, so a dry run never obtains a credential that could write.
        reader = s3_client(cfg, role="reader")
        plan, problems = _plan(cfg=cfg, args=args, reader=reader)

        verb = "extend" if args.apply else "would extend"
        for item in plan:
            print(
                f"{verb} {item['key']}: "
                f"{item['retain_until_before'] or 'no retention'} -> "
                f"{item['retain_until_after']}  ({item['explanation']})"
            )

        writer = None
        if args.apply:
            writer = s3_client(cfg, role="writer")
            moved, apply_problems = _apply(cfg=cfg, writer=writer, plan=plan)
            problems.extend(apply_problems)
            applied = len(moved)

        # Journalled before the bucket record is written, and never inside a
        # branch that a failure can skip. The journal is the trail on another
        # machine; if the record below cannot be written, what moved is still
        # known.
        journal(
            cfg,
            {
                "event": "extend_retention" if args.apply else "extend_retention_dry_run",
                "reason": args.reason,
                "filter_class": args.retention_class or None,
                "filter_grant": args.grant or None,
                "candidates": len(plan),
                "applied": applied,
                "extensions": moved,
                "problems": problems,
            },
        )

        if moved and writer is not None:
            # An extension is a deliberate, permanent act, so it also leaves a
            # record in the strongest of the three trails -- the bucket itself,
            # under lock, dated, naming every object it moved, both dates and the
            # reason. The daily inventory captures the resulting state; this
            # captures the act, and it is the one an auditor can check without
            # trusting us.
            stamp = dt.datetime.now(dt.UTC)
            try:
                put_locked(
                    client=writer,
                    cfg=cfg,
                    key=f"retention-extensions/{stamp.strftime('%Y-%m-%dT%H-%M-%SZ')}.json",
                    body=json.dumps(
                        {
                            "entity": cfg.entity,
                            "bucket": cfg.bucket,
                            "applied_at": stamp.isoformat(),
                            "reason": args.reason,
                            "filter_class": args.retention_class or None,
                            "filter_grant": args.grant or None,
                            "extensions": moved,
                        },
                        indent=2,
                        sort_keys=True,
                    ).encode("utf-8"),
                    retain_until=year_end(stamp.year + cfg.export_retain_years),
                    legal_hold=False,
                    content_type="application/json",
                )
            except ClientError as exc:
                # The dates moved but the bucket has no record of it. Loud, and
                # not fixable by re-running: a second run finds nothing to extend,
                # because the objects already carry the new dates.
                LOG.error(
                    "dates were extended but the record could not be written: %s. "
                    "The journal holds what moved; write the record by hand",
                    exc,
                )
                journal(cfg, {"event": "extend_retention_record_failed", "error": str(exc)})
                problems.append({"key": "retention-extensions/", "error": str(exc)})
            state.bump("retention_extended_total", applied)

        if problems:
            # Either the config no longer explains an object that is already in
            # the bucket, or a write did not land. Both are real, neither is
            # transient.
            status = 1
    except (ClientError, OSError) as exc:
        LOG.error("extend-retention failed: %s", exc)
        journal(cfg, {"event": "extend_retention_failed", "error": str(exc)})
        status = 1

    state.save()
    write_metrics(
        cfg,
        job="extend_retention",
        metrics=[
            Metric(
                name="paperless_retention_extended_total",
                help_text="Objects whose retain-until has been extended after the fact",
                metric_type="counter",
                value=float(state.get("retention_extended_total", 0)),
            ),
            Metric(
                name="paperless_retention_extend_last_run_status",
                help_text="1 if the last retention extension ran cleanly",
                metric_type="gauge",
                value=0 if status else 1,
            ),
        ],
    )

    for problem in problems:
        LOG.error("%s: %s", problem["key"], problem["error"])
    if plan and not args.apply:
        print(f"\n{len(plan)} object(s) would be extended. Re-run with --apply --reason '...' to write.")
    elif not plan and not problems:
        print("nothing to extend: every object is already held at least as long as the config says")
    return status
