"""
The two local audit trails: Prometheus metrics and the decision journal

Three independent trails record what this program did, and only two of them run
through here. The third is the bucket itself, which is the one an auditor can
verify without trusting us.
"""

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from paperless_b2_archiver.config import Config

LOG = logging.getLogger("paperless-archive")


@dataclass(frozen=True, kw_only=True)
class Metric:
    """One Prometheus series, with the help text an alert author will read."""

    name: str
    help_text: str
    metric_type: str
    value: float


def write_metrics(cfg: Config, *, job: str, metrics: list[Metric]) -> None:
    """
    Write one job's metrics where the collector will find them

    One file per job per entity, with disjoint metric names, so two jobs never
    clobber each other's series and the collector never sees a duplicate.

    Every metric carries an ``entity`` label, and every alert rule is per entity.
    A single unlabelled series would let a healthy instance mask a broken one,
    which is the classic way a second tenant goes unmonitored.

    Parameters
    ----------
    cfg
        The entity's config, which names the metrics directory.
    job
        The job's short name, used in the filename.
    metrics
        The series to write. Replaces whatever this job wrote last time.
    """
    cfg.metrics_dir.mkdir(parents=True, exist_ok=True)
    target = cfg.metrics_dir / f"paperless_{cfg.entity}_{job}.prom"
    lines: list[str] = []
    for metric in metrics:
        lines.append(f"# HELP {metric.name} {metric.help_text}")
        lines.append(f"# TYPE {metric.name} {metric.metric_type}")
        lines.append(f'{metric.name}{{entity="{cfg.entity}"}} {metric.value}')
    tmp = target.with_suffix(f".prom.{os.getpid()}")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(target)


def journal(cfg: Config, record: dict[str, Any]) -> None:
    """
    Append one decision to the local journal and to the log

    Two of the three independent audit trails run through here: the journal file
    on disk, and stdout/journald, which the log shipper carries to the sink on a
    different machine under different administration.

    Parameters
    ----------
    cfg
        The entity's config, which names the journal directory.
    record
        The decision. Timestamped and labelled with the entity here, so no
        caller has to remember to.
    """
    cfg.journal_dir.mkdir(parents=True, exist_ok=True)
    record = {"ts": dt.datetime.now(dt.UTC).isoformat(), "entity": cfg.entity, **record}
    line = json.dumps(record, sort_keys=True, default=str)
    with (cfg.journal_dir / "tap.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    LOG.info("%s", line)
