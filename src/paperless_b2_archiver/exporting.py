"""
Filtering a paperless export down to what may be locked away

The least obvious failure mode in the whole design lives here. `document_exporter`
has no tag filter and dumps everything it can see, so an unfiltered export
uploaded to the WORM bucket would lock away, for eleven years, exactly the
personnel material the tap deliberately kept deletable. The tap is careful about
it and the export would quietly have undone that.
"""

import json
from pathlib import Path
from typing import Any

from paperless_b2_archiver.config import Config
from paperless_b2_archiver.retention import resolve_class


class ExportFilterLeak(Exception):
    """
    A document outside the kept classes survived the filter

    Raised instead of uploading. An object written under Object Lock cannot be
    taken back, so the only safe response is to refuse the whole export.
    """


def filter_export(cfg: Config, *, root: Path, keep: set[str]) -> tuple[int, int]:
    """
    Strip every document outside `keep` from an export tree, in place

    Parameters
    ----------
    cfg
        The entity's config, which owns the class table and the tag prefix.
    root
        The export tree, modified in place. Give this a copy, never the tree
        that the backup chain reads.
    keep
        The classes whose documents survive.

    Returns
    -------
    :
        ``(kept, removed)`` document counts.

    Raises
    ------
    ExportFilterLeak
        If any document outside `keep` is still in the manifest afterwards.
        Asserted rather than trusted: this is the control whose failure is
        permanent.
    """
    manifest_path = root / "manifest.json"
    manifest: list[dict[str, Any]] = json.loads(manifest_path.read_text(encoding="utf-8"))

    tag_names = {rec["pk"]: rec["fields"]["name"] for rec in manifest if rec.get("model") == "documents.tag"}
    document_ct = next(
        (
            rec["pk"]
            for rec in manifest
            if rec.get("model") == "contenttypes.contenttype"
            and rec["fields"].get("app_label") == "documents"
            and rec["fields"].get("model") == "document"
        ),
        None,
    )

    dropped: set[int] = set()
    kept = 0
    for rec in manifest:
        if rec.get("model") != "documents.document":
            continue
        names = [tag_names.get(pk, "") for pk in rec["fields"].get("tags", [])]
        if resolve_class(cfg, names) in keep:
            kept += 1
            continue
        dropped.add(rec["pk"])
        for attr in (
            "__exported_file_name__",
            "__exported_archive_name__",
            "__exported_thumbnail_name__",
        ):
            name = rec.get(attr)
            if name:
                (root / name).unlink(missing_ok=True)
        # --split-manifest puts each document's own metadata beside its file.
        name = rec.get("__exported_file_name__")
        if name:
            split = (root / name).with_name(Path(name).stem + "-manifest.json")
            split.unlink(missing_ok=True)

    dropped_str = {str(pk) for pk in dropped}

    def survives(rec: dict[str, Any]) -> bool:
        if rec.get("model") == "documents.document":
            return rec["pk"] not in dropped
        fields = rec.get("fields", {})
        # Notes, custom field instances and share links hang off a document.
        if fields.get("document") in dropped:
            return False
        # Audit-log entries and object permissions reference it by content type
        # and a stringified pk instead.
        if document_ct is not None and fields.get("content_type") == document_ct:
            if str(fields.get("object_pk", "")) in dropped_str:
                return False
        return True

    filtered = [rec for rec in manifest if survives(rec)]
    manifest_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")

    leaked = [
        rec
        for rec in filtered
        if rec.get("model") == "documents.document"
        and resolve_class(cfg, [tag_names.get(pk, "") for pk in rec["fields"].get("tags", [])]) not in keep
    ]
    if leaked:
        raise ExportFilterLeak(
            f"{len(leaked)} document(s) outside {sorted(keep)} survived the export filter. "
            "Refusing to upload: an object written under Object Lock cannot be taken back."
        )
    return kept, len(dropped)
