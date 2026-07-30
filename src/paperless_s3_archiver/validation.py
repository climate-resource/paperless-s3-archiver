"""
Checks on a configuration that would be wrong in a way we cannot undo

Loading a config already refuses the structural mistakes -- a missing class
table, an archived class with no period, an open audit window with no expiry --
because :mod:`paperless_s3_archiver.config` will not build a `Config` without
them. What is left here needs either the current date or a second entity's
config to decide, so it cannot be a field validator.

These run at converge time, in CI and before every job, rather than only in the
deployment tool. The same check applied in three places is what stops a
configuration reaching the bucket through a path nobody validated.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from typing import Literal

from paperless_s3_archiver.config import Config

#: How far out an audit-mode window may be opened. An engagement that genuinely
#: needs longer is a deliberate reopening with a new date, not one long window.
DEFAULT_PUBLIC_MAX_DAYS = 90

#: A grant is only worth reporting on once a second entity also holds it.
SHARED_BY_AT_LEAST = 2

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    """One thing a validation run has to say about a config."""

    severity: Severity
    message: str

    def __str__(self) -> str:
        """
        Render for a terminal

        Returns
        -------
        :
            The severity and the message.
        """
        return f"{self.severity.upper()}: {self.message}"


def check_exposure_window(
    cfg: Config, *, max_days: int = DEFAULT_PUBLIC_MAX_DAYS, today: dt.date | None = None
) -> list[Finding]:
    """
    Audit mode is easy to open because it closes on its own

    Parameters
    ----------
    cfg
        The entity's config.
    max_days
        The longest window that may be opened in one go.
    today
        The date to measure from. Defaults to the current UTC date.

    Returns
    -------
    :
        Findings. An unparseable, past or over-long `public_until` is an error.
    """
    if not cfg.public_enabled:
        return []
    today = today or dt.datetime.now(dt.UTC).date()

    try:
        until = dt.date.fromisoformat(cfg.public_until)
    except ValueError:
        return [
            Finding(
                "error",
                f"{cfg.entity}: public_until '{cfg.public_until}' is not a YYYY-MM-DD date",
            )
        ]

    if until <= today:
        return [
            Finding(
                "error",
                f"{cfg.entity}: public_until {until} is in the past, so the window "
                "should already have closed. Reopen with a new date or turn audit mode off.",
            )
        ]
    if (until - today).days > max_days:
        return [
            Finding(
                "error",
                f"{cfg.entity}: public_until {until} is more than {max_days} days out. "
                "An engagement that genuinely needs longer is a deliberate reopening "
                "with a new date, not one long window.",
            )
        ]
    return [
        Finding(
            "warning",
            f"{cfg.entity}: AUDIT MODE IS ON until {until} "
            f"({(until - today).days} days). The archive is reachable from the internet.",
        )
    ]


def check_retention_table(cfg: Config) -> list[Finding]:
    """
    Consistency within one entity's class table and its registries

    Parameters
    ----------
    cfg
        The entity's config.

    Returns
    -------
    :
        Findings. Anything that would make the tap refuse a document at ingest
        time is reported here instead, where it costs an edit rather than a
        stalled spool.
    """
    findings: list[Finding] = []

    for name, spec in sorted(cfg.retention_classes.items()):
        if spec.clock == "none" and spec.archive:
            findings.append(
                Finding(
                    "error",
                    f"{cfg.entity}: class '{name}' is archived but has no clock, so no "
                    "retain-until date can be computed for it",
                )
            )
        if spec.clock == "grant" and spec.years is not None:
            findings.append(
                Finding(
                    "warning",
                    f"{cfg.entity}: class '{name}' is on the grant clock, where the period "
                    f"comes from each grant agreement, but also declares years={spec.years}. "
                    "That number is ignored; remove it so the table does not imply otherwise.",
                )
            )
        if not spec.archive and not spec.basis:
            findings.append(
                Finding(
                    "warning",
                    f"{cfg.entity}: class '{name}' is never archived but states no basis. "
                    "Say why it is out of scope, so the carve-out is a decision on the record.",
                )
            )

    for slug, grant in sorted(cfg.grants.items()):
        if not grant.complete:
            findings.append(
                Finding(
                    "error",
                    f"{cfg.entity}: grant '{slug}' is missing final_payment_year or years. "
                    "Every document tagged with it will be refused until both are set.",
                )
            )

    if any(spec.clock == "grant" and spec.archive for spec in cfg.retention_classes.values()):
        if not cfg.grants:
            findings.append(
                Finding(
                    "warning",
                    f"{cfg.entity}: a grant-clock class is archived but no grants are "
                    "registered, so every grant document will be refused.",
                )
            )

    return findings


def check_object_lock_mode(cfg: Config) -> list[Finding]:
    """
    Say which mode a run will write under, every time

    Parameters
    ----------
    cfg
        The entity's config.

    Returns
    -------
    :
        One informational finding. Not a problem either way, but the single most
        consequential setting in the file and worth stating on every run.
    """
    if cfg.burn_in:
        return [
            Finding(
                "info",
                f"{cfg.entity}: Object Lock mode is {cfg.object_lock_mode}. Burn-in: objects "
                f"are locked for {cfg.burn_in_retain_days} days and everything written now is "
                "disposable. Nothing counts as archived until it is re-ingested under COMPLIANCE.",
            )
        ]
    return [
        Finding(
            "info",
            f"{cfg.entity}: Object Lock mode is COMPLIANCE. Retention is irreversible from "
            "here. Every object carries the real retain-until date computed from its class.",
        )
    ]


def check_shared_grants(configs: list[Config]) -> list[Finding]:
    """
    Report the grants held by more than one entity, and whether the periods agree

    A joint grant -- both entities named as beneficiaries -- deliberately carries
    the same slug in both, because the grant registry is per instance and there
    is no shared namespace to collide in.

    That independence is the point, and it is also the risk: an amendment moves
    the final payment year, one entity's entry is updated and the other is
    forgotten, and the second archive quietly under-retains. Reported rather than
    asserted, because two entities are allowed to hold the same grant on
    different periods -- the agreement's clause is common, but the advice each
    entity gets on top of it need not be.

    Parameters
    ----------
    configs
        Every entity's config. A single-entity list produces nothing.

    Returns
    -------
    :
        One finding per shared slug.
    """
    holders: dict[str, list[Config]] = defaultdict(list)
    for cfg in configs:
        for slug in cfg.grants:
            holders[slug].append(cfg)

    findings: list[Finding] = []
    for slug, entities in sorted(holders.items()):
        if len(entities) < SHARED_BY_AT_LEAST:
            continue
        years = {cfg.entity: cfg.grants[slug].final_payment_year for cfg in entities}
        periods = {cfg.entity: cfg.grants[slug].years for cfg in entities}
        stated_years = ", ".join(f"{name}={value}" for name, value in sorted(years.items()))
        stated_periods = ", ".join(f"{name}={value}" for name, value in sorted(periods.items()))
        divergent = len(set(years.values())) > 1 or len(set(periods.values())) > 1

        held_by = " and ".join(sorted(cfg.entity for cfg in entities))
        detail = (
            f"Grant '{slug}' is held by {held_by}. "
            f"Final payment year {stated_years}; period {stated_periods} years."
        )
        if divergent:
            findings.append(
                Finding(
                    "warning",
                    f"{detail} DIVERGENT. Deliberate if each entity's advice on top of the "
                    "agreement differs; a one-sided edit if not. Correct the entity that fell "
                    "behind and bring its objects up with `paperless-archive extend-retention`.",
                )
            )
        else:
            findings.append(Finding("info", f"{detail} They agree."))
    return findings


def validate(
    cfg: Config,
    *,
    peers: list[Config] | None = None,
    max_days: int = DEFAULT_PUBLIC_MAX_DAYS,
    today: dt.date | None = None,
) -> list[Finding]:
    """
    Every check, for one entity

    Parameters
    ----------
    cfg
        The entity to validate.
    peers
        The other entities' configs, for the cross-entity grant report.
    max_days
        The longest audit-mode window that may be opened in one go.
    today
        The date to measure the exposure window from.

    Returns
    -------
    :
        Every finding, errors first.
    """
    findings = [
        *check_retention_table(cfg),
        *check_exposure_window(cfg, max_days=max_days, today=today),
        *check_shared_grants([cfg, *(peers or [])]),
        *check_object_lock_mode(cfg),
    ]
    order: dict[Severity, int] = {"error": 0, "warning": 1, "info": 2}
    return sorted(findings, key=lambda f: order[f.severity])
