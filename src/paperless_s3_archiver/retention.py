"""
Retention arithmetic: which class a document is in, and how long it is held

Everything in this module is a pure function of the config and the document's
tags. That is deliberate. A retain-until date written under COMPLIANCE cannot be
corrected downwards afterwards -- not by us, not by the account owner holding the
master key -- so the code that computes one has no I/O to mock and no ordering to
get wrong, and can be tested exhaustively against the statute it implements.
"""

import datetime as dt
from typing import Any

from paperless_s3_archiver.config import Config

#: Returned by :func:`resolve_class` when a document carries no class tag.
UNCLASSIFIED = "__unclassified__"

#: Returned by :func:`resolve_class` when a document carries more than one.
AMBIGUOUS = "__ambiguous__"

#: ``documents/<yyyy>/<sha256>`` -- the shape the tap writes, and the only shape
#: a document year can be read back off.
DOCUMENT_KEY_PARTS = 3


class Undecidable(Exception):
    """
    The document cannot be given a retain-until date from what we know

    Always fatal for that document: it stays in paperless, unarchived, and a
    human classifies it. Guessing a retention period would write a date that
    cannot be corrected downwards afterwards.
    """


def year_end(year: int) -> dt.datetime:
    """
    31 December of `year`, in UTC

    § 147 (4) AO starts the clock at the end of the calendar year of the last
    entry or receipt, so every retain-until date is a 31 December, never "now
    plus N".

    Parameters
    ----------
    year
        The calendar year the period runs out in.

    Returns
    -------
    :
        The last second of that year, in UTC.
    """
    return dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.UTC)


def resolve_class(cfg: Config, tag_names: list[str]) -> str:
    """
    Map a document's tags to exactly one retention class

    Zero classes or two is a refusal, not a default. The refusal is what enforces
    the Entgeltunterlagen carve-out and the GDPR boundary: a document type nobody
    has classified must not fall into whichever class happens to be first in the
    table.

    Parameters
    ----------
    cfg
        The entity's config, which owns the tag prefix and the class table.
    tag_names
        Every tag on the document, class tags and others alike.

    Returns
    -------
    :
        The class name, or :data:`UNCLASSIFIED` / :data:`AMBIGUOUS`.
    """
    found = [t[len(cfg.class_tag_prefix) :] for t in tag_names if t.startswith(cfg.class_tag_prefix)]
    known = [c for c in found if c in cfg.retention_classes]
    if len(known) == 1 and len(found) == 1:
        return known[0]
    if len(found) > 1:
        return AMBIGUOUS
    return UNCLASSIFIED


def tag_suffix(tag_names: list[str], *, prefix: str) -> str | None:
    """
    The first tag carrying `prefix`, with the prefix removed

    Parameters
    ----------
    tag_names
        Every tag on the document.
    prefix
        The prefix to look for, for example ``grant:``.

    Returns
    -------
    :
        The suffix, or ``None`` when no tag carries the prefix.
    """
    for t in tag_names:
        if t.startswith(prefix):
            return t[len(prefix) :]
    return None


def retention_for(
    cfg: Config,
    *,
    class_name: str,
    doc_year: int,
    tag_names: list[str],
) -> tuple[dt.datetime, bool, str]:
    """
    Work out how long one document must be held

    Parameters
    ----------
    cfg
        The entity's config.
    class_name
        The class the document resolved to. Must be an archived class.
    doc_year
        The calendar year of the document itself.
    tag_names
        Every tag on the document. Two clocks need a second tag to resolve
        against: ``grant:<slug>`` and ``employment:<person>``.

    Returns
    -------
    :
        ``(retain_until, legal_hold, explanation)``. The explanation is carried
        into the sidecar, so the reason for a date is archived beside it.

    Raises
    ------
    Undecidable
        When the class needs a date we do not have. Never substituted with a
        guess: a wrong date here is permanent.
    """
    spec = cfg.retention_classes[class_name]
    clock = spec.clock

    if clock == "document_year":
        years = int(spec.years or 0)
        return (
            year_end(doc_year + years),
            False,
            f"{class_name}: document year {doc_year} + {years}",
        )

    if clock == "grant":
        slug = tag_suffix(tag_names, prefix=cfg.grant_tag_prefix)
        if slug is None:
            raise Undecidable(f"class {class_name} needs a {cfg.grant_tag_prefix}<slug> tag naming the grant")
        grant = cfg.grants.get(slug)
        if grant is None or not grant.complete:
            raise Undecidable(
                f"grant '{slug}' has no final_payment_year and years in the entity config. "
                "The period comes from the grant agreement and is never assumed."
            )
        base = int(grant.final_payment_year or 0)
        years = int(grant.years or 0)
        return (
            year_end(base + years),
            False,
            f"{class_name}: grant {slug}, final payment {base} + {years}",
        )

    if clock == "employment_end":
        years = int(spec.years or 0)
        slug = tag_suffix(tag_names, prefix=cfg.employment_tag_prefix)
        if slug is None:
            raise Undecidable(f"class {class_name} needs an {cfg.employment_tag_prefix}<person> tag")
        employment = cfg.employments.get(slug)
        end_year = employment.end_year if employment is not None else None
        if end_year is None:
            # Employment continues, so the clock has not started. A contract
            # signed in 2027 for an employment ending in 2045 must not unlock in
            # 2038, which is exactly what a legal hold is for.
            #
            # The retain-until below is a floor, not the real date: it is
            # computed from the document year so that the object is still locked
            # for the full period even if nobody ever comes back to set the
            # employment end. Retention can be extended and never shortened, so
            # a floor costs nothing and removes a silent failure mode.
            return (
                year_end(doc_year + years),
                True,
                f"{class_name}: employment '{slug}' has not ended. Legal hold, "
                f"floor retain-until from document year {doc_year} + {years}",
            )
        return (
            year_end(int(end_year) + years),
            False,
            f"{class_name}: employment '{slug}' ended {end_year} + {years}",
        )

    raise Undecidable(f"class {class_name} has an unknown clock '{clock}'")


def year_from_key(key: str) -> int:
    """
    The document year an object was filed under

    The tap writes ``documents/<yyyy>/<sha256>``, and that ``<yyyy>`` is the year
    it fed into the retention arithmetic. Reading it back off the key keeps
    :func:`recompute_retention`'s answer identical to the one the tap reached,
    with no second implementation of "which year is this document from" to drift
    from the first.

    Parameters
    ----------
    key
        The object key, without the ``.json`` sidecar suffix.

    Returns
    -------
    :
        The document year.

    Raises
    ------
    Undecidable
        When the key does not carry a year where the tap would have put one.
    """
    parts = key.split("/")
    if len(parts) < DOCUMENT_KEY_PARTS or not parts[1].isdigit():
        raise Undecidable(f"cannot read a document year off the key {key}")
    return int(parts[1])


def recompute_retention(
    cfg: Config, *, sidecar: dict[str, Any], doc_key: str
) -> tuple[dt.datetime, bool, str]:
    """
    What the config says this object's retain-until should be *now*

    Computed from the sidecar alone -- its class and its tags -- so this works off
    the bucket with no paperless to ask. That matters: an extension may be needed
    years after the fact, possibly on a host where the application no longer runs.

    Parameters
    ----------
    cfg
        The entity's config, as it reads today.
    sidecar
        The archived sidecar, which names the class and carries the tags.
    doc_key
        The document's own object key, which carries the year.

    Returns
    -------
    :
        ``(retain_until, legal_hold, explanation)``, as :func:`retention_for`.

    Raises
    ------
    Undecidable
        When the config no longer explains an object that is already archived.
        Reported rather than guessed: both cases below are real problems.
    """
    class_name = sidecar.get("retention_class") or ""
    if class_name not in cfg.retention_classes:
        raise Undecidable(
            f"sidecar names class '{class_name}', which this entity's retention "
            "table no longer has. Restore the class before extending anything"
        )
    if not cfg.retention_classes[class_name].archive:
        raise Undecidable(
            f"sidecar names class '{class_name}', which is never archived. An object "
            "in a never-archived class is a leak to investigate, not a retention question"
        )
    return retention_for(
        cfg,
        class_name=class_name,
        doc_year=year_from_key(doc_key),
        tag_names=sidecar.get("tags") or [],
    )
