"""
Archiving one consumed document

This is the authoritative path into the archive: a document becomes immutable
here, before anyone has had the chance to edit or delete it.
"""

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.observability import journal
from paperless_s3_archiver.paperless import PaperlessAPI
from paperless_s3_archiver.retention import (
    AMBIGUOUS,
    UNCLASSIFIED,
    Undecidable,
    resolve_class,
    retention_for,
)
from paperless_s3_archiver.s3 import put_locked
from paperless_s3_archiver.state import State

#: Read in chunks so a large scan does not have to fit in memory twice.
HASH_CHUNK_BYTES = 1024 * 1024


def sha256_of(path: Path) -> str:
    """
    The SHA-256 of a file

    Parameters
    ----------
    path
        The file to hash.

    Returns
    -------
    :
        The hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def host_path(cfg: Config, container_path: str) -> Path:
    """
    Translate a path the application reported into the path the job can read

    Parameters
    ----------
    cfg
        The entity's config, which names both views of the data directory.
    container_path
        The path as paperless reported it.

    Returns
    -------
    :
        The same file, as this process sees it.

    Raises
    ------
    Undecidable
        When the path is outside both known roots. Refusing beats reading
        whatever happens to be at a guessed location.
    """
    if container_path.startswith(cfg.container_media_root):
        return cfg.media_dir / container_path[len(cfg.container_media_root) :].lstrip("/")
    if container_path.startswith(cfg.container_export_root):
        return cfg.export_dir / container_path[len(cfg.container_export_root) :].lstrip("/")
    raise Undecidable(f"cannot map container path {container_path} onto the host")


@dataclass(frozen=True)
class TapContext:
    """
    What one tap run establishes once and every document in it then uses

    Built at the top of a run rather than per document, because the tag table
    is the same for every job in the spool and reading it is an API round trip.
    """

    client: Any
    """A writer-role S3 client."""

    api: PaperlessAPI
    """The paperless API, asked what each document currently is."""

    tag_names_by_id: dict[int, str]
    """Every tag id mapped to its name, read once per run."""

    @classmethod
    def build(cls, *, client: Any, api: PaperlessAPI) -> "TapContext":
        """
        Establish the run's context from the API

        Parameters
        ----------
        client
            A writer-role S3 client.
        api
            The paperless API.

        Returns
        -------
        :
            The context.
        """
        return cls(
            client=client,
            api=api,
            tag_names_by_id={t["id"]: t["name"] for t in api.all_pages("/tags/")},
        )


def archive_document(cfg: Config, *, state: State, ctx: TapContext, job: dict[str, Any]) -> str:
    """
    Archive one consumed document

    Parameters
    ----------
    cfg
        The entity's config.
    state
        The persistent counters, bumped per outcome.
    ctx
        What the run established once: the S3 client, the API, and the lookups.
    job
        The spool job the post-consume hook wrote.

    Returns
    -------
    :
        The outcome: ``archived``, ``rejected`` or ``indexed``.

    Raises
    ------
    Undecidable
        When the document's class needs a date we do not have. The job file
        stays in the spool so the document is never quietly dropped.
    """
    doc_id = int(job["document_id"])
    # The API, not the hook's environment, is the authority on what the document
    # is: a workflow or a human may have retagged it between consumption and now,
    # and the class decides a date we cannot revise afterwards.
    doc = ctx.api.get(f"/documents/{doc_id}/")

    tag_names = [ctx.tag_names_by_id.get(pk, "") for pk in doc.get("tags", [])]
    class_name = resolve_class(cfg, tag_names)

    created = doc.get("created") or doc.get("added") or ""
    doc_year = int(created[:4]) if created[:4].isdigit() else dt.date.today().year

    if class_name in (UNCLASSIFIED, AMBIGUOUS):
        # Left in paperless for a human. Refusing is the control: it is what
        # keeps Entgeltunterlagen out, since no class here accepts them.
        state.bump("rejected_total")
        journal(
            cfg,
            {
                "event": "reject",
                "document_id": doc_id,
                "reason": class_name,
                "tags": tag_names,
                "title": doc.get("title"),
            },
        )
        return "rejected"

    spec = cfg.retention_classes[class_name]

    if not spec.archive:
        # Indexed, searchable, backed up through the ordinary backup chain, and
        # deliberately never written to the WORM bucket -- so material we may be
        # obliged to erase under Art. 17 GDPR stays erasable. Recording it here
        # is what lets reconciliation check the bucket in the other direction.
        state.bump("hr_indexed_total")
        journal(
            cfg,
            {
                "event": "indexed_not_archived",
                "document_id": doc_id,
                "class": class_name,
                "title": doc.get("title"),
                "reason": spec.basis,
            },
        )
        return "indexed"

    retain_until, legal_hold, why = retention_for(
        cfg, class_name=class_name, doc_year=doc_year, tag_names=tag_names
    )

    source = host_path(cfg, job["document_source_path"])
    payload = source.read_bytes()
    checksum = hashlib.sha256(payload).hexdigest()
    key = f"documents/{doc_year:04d}/{checksum}"

    sidecar = {
        "entity": cfg.entity,
        "paperless_document_id": doc_id,
        "title": doc.get("title"),
        "correspondent": job.get("document_correspondent") or None,
        "document_type": job.get("document_type") or None,
        "tags": tag_names,
        "retention_class": class_name,
        "retention_basis": spec.basis,
        "created": doc.get("created"),
        "added": doc.get("added"),
        "original_filename": job.get("document_original_filename"),
        "sha256": checksum,
        "size_bytes": len(payload),
        "object_key": key,
        "retain_until": retain_until.isoformat(),
        "legal_hold": legal_hold,
        "object_lock_mode": "GOVERNANCE" if cfg.burn_in else "COMPLIANCE",
        "burn_in": cfg.burn_in,
        "explanation": why,
        "archived_at": dt.datetime.now(dt.UTC).isoformat(),
    }

    put_locked(
        client=ctx.client,
        cfg=cfg,
        key=key,
        body=payload,
        retain_until=retain_until,
        legal_hold=legal_hold,
        content_type="application/octet-stream",
        metadata={"sha256": checksum, "class": class_name},
    )
    put_locked(
        client=ctx.client,
        cfg=cfg,
        key=f"{key}.json",
        body=json.dumps(sidecar, indent=2, sort_keys=True).encode("utf-8"),
        retain_until=retain_until,
        legal_hold=legal_hold,
        content_type="application/json",
    )

    state.bump("archived_total")
    journal(cfg, {"event": "archived", **sidecar})
    return "archived"
