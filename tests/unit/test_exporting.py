"""
The export filter, which is the control whose failure is permanent

`document_exporter` has no tag filter and dumps everything it can see. An
unfiltered export uploaded to the WORM bucket would lock away, for eleven years,
exactly the personnel material the tap deliberately kept deletable.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from paperless_b2_archiver import exporting
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.exporting import ExportFilterLeak, filter_export

DOCUMENT_CONTENT_TYPE = 7

TAGS = {1: "class:receipts", 2: "class:hr-file", 3: "class:books", 4: "correspondent:acme"}


def _document(pk: int, tags: list[int], stem: str) -> dict[str, Any]:
    return {
        "model": "documents.document",
        "pk": pk,
        "fields": {"title": f"document {pk}", "tags": tags},
        "__exported_file_name__": f"originals/{stem}.pdf",
        "__exported_archive_name__": f"archive/{stem}.pdf",
        "__exported_thumbnail_name__": f"thumbnails/{stem}.webp",
    }


@pytest.fixture
def export_tree(tmp_path: Path) -> Path:
    """
    An export as `document_exporter --split-manifest` leaves it

    Three documents: a receipt (archived), a personnel file (never archived) and
    a book (archived), each with its files, its split manifest, a note, an audit
    log entry and an object permission hanging off it.
    """
    root = tmp_path / "export"
    manifest: list[dict[str, Any]] = [
        {"model": "documents.tag", "pk": pk, "fields": {"name": name}} for pk, name in TAGS.items()
    ]
    manifest.append(
        {
            "model": "contenttypes.contenttype",
            "pk": DOCUMENT_CONTENT_TYPE,
            "fields": {"app_label": "documents", "model": "document"},
        }
    )
    manifest += [
        _document(10, [1, 4], "receipt"),
        _document(11, [2], "personnel"),
        _document(12, [3], "ledger"),
    ]
    for pk, doc in ((20, 10), (21, 11), (22, 12)):
        manifest.append({"model": "documents.note", "pk": pk, "fields": {"document": doc, "note": "hello"}})
        manifest.append(
            {
                "model": "auditlog.logentry",
                "pk": pk + 100,
                "fields": {"content_type": DOCUMENT_CONTENT_TYPE, "object_pk": str(doc)},
            }
        )
        manifest.append(
            {
                "model": "guardian.userobjectpermission",
                "pk": pk + 200,
                "fields": {"content_type": DOCUMENT_CONTENT_TYPE, "object_pk": str(doc)},
            }
        )

    for rec in manifest:
        if rec["model"] != "documents.document":
            continue
        for attr in (
            "__exported_file_name__",
            "__exported_archive_name__",
            "__exported_thumbnail_name__",
        ):
            path = root / rec[attr]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-1.4 pretend")
        split = (root / rec["__exported_file_name__"]).with_name(
            Path(rec["__exported_file_name__"]).stem + "-manifest.json"
        )
        split.write_text(json.dumps([rec]), encoding="utf-8")

    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def _surviving_documents(root: Path) -> set[int]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return {rec["pk"] for rec in manifest if rec.get("model") == "documents.document"}


class TestFilterExport:
    def test_keeps_the_archived_classes_and_drops_the_rest(self, cfg: Config, export_tree: Path):
        kept, removed = filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        assert (kept, removed) == (2, 1)
        assert _surviving_documents(export_tree) == {10, 12}

    def test_personnel_material_never_reaches_the_upload(self, cfg: Config, export_tree: Path):
        # This is the whole point. hr-file is indexed and searchable in
        # paperless and must stay deletable, so it must not be in the tree that
        # gets locked for eleven years.
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        assert 11 not in _surviving_documents(export_tree)

    def test_removes_the_dropped_documents_files_from_disk(self, cfg: Config, export_tree: Path):
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        assert not (export_tree / "originals/personnel.pdf").exists()
        assert not (export_tree / "archive/personnel.pdf").exists()
        assert not (export_tree / "thumbnails/personnel.webp").exists()
        # The kept documents' files are untouched.
        assert (export_tree / "originals/receipt.pdf").exists()
        assert (export_tree / "originals/ledger.pdf").exists()

    def test_removes_the_dropped_documents_split_manifest(self, cfg: Config, export_tree: Path):
        # --split-manifest writes each document's metadata beside its file. A
        # dropped document whose split manifest survives is the same leak in a
        # smaller file.
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        assert not (export_tree / "originals/personnel-manifest.json").exists()
        assert (export_tree / "originals/receipt-manifest.json").exists()

    def test_removes_notes_hanging_off_a_dropped_document(self, cfg: Config, export_tree: Path):
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        notes = [r for r in manifest if r["model"] == "documents.note"]
        assert {r["fields"]["document"] for r in notes} == {10, 12}

    def test_removes_audit_entries_referencing_a_dropped_document(self, cfg: Config, export_tree: Path):
        # These reference the document by content type and a *stringified* pk,
        # which is the shape most likely to be missed by a filter.
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        entries = [r for r in manifest if r["model"] == "auditlog.logentry"]
        assert {r["fields"]["object_pk"] for r in entries} == {"10", "12"}

    def test_removes_object_permissions_referencing_a_dropped_document(self, cfg: Config, export_tree: Path):
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        perms = [r for r in manifest if r["model"] == "guardian.userobjectpermission"]
        assert {r["fields"]["object_pk"] for r in perms} == {"10", "12"}

    def test_keeps_the_tag_and_content_type_records(self, cfg: Config, export_tree: Path):
        # Dropping these would leave a manifest that cannot be restored.
        filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        assert len([r for r in manifest if r["model"] == "documents.tag"]) == len(TAGS)
        assert any(r["model"] == "contenttypes.contenttype" for r in manifest)

    def test_an_unclassified_document_is_dropped_not_kept(self, cfg: Config, export_tree: Path):
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        manifest.append(_document(13, [4], "mystery"))  # only a correspondent tag
        (export_tree / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        kept, removed = filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
        assert (kept, removed) == (2, 2)
        assert 13 not in _surviving_documents(export_tree)


class TestAuditorScope:
    def test_the_z3_handover_withholds_personnel_material_too(self, cfg: Config, export_tree: Path):
        # A Betriebsprüfer's access covers tax records, not personnel files, so
        # the handover is filtered harder than the nightly export. Both hr
        # classes are restricted, and only `books` and `receipts` survive.
        kept, removed = filter_export(cfg, root=export_tree, keep=cfg.tax_and_grant_classes())
        assert (kept, removed) == (2, 1)
        assert _surviving_documents(export_tree) == {10, 12}

    def test_a_restricted_but_archived_class_is_out_of_scope_for_an_auditor(
        self, cfg: Config, export_tree: Path
    ):
        manifest = json.loads((export_tree / "manifest.json").read_text(encoding="utf-8"))
        manifest.append({"model": "documents.tag", "pk": 5, "fields": {"name": "class:hr-contract"}})
        manifest.append(_document(14, [5], "contract"))
        (export_tree / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        # The nightly export keeps it: it is archived, and it is the restore path.
        assert "hr-contract" in cfg.archived_classes()
        # The auditor handover does not.
        assert "hr-contract" not in cfg.tax_and_grant_classes()

        filter_export(cfg, root=export_tree, keep=cfg.tax_and_grant_classes())
        assert 14 not in _surviving_documents(export_tree)


class TestLeakAssertion:
    def test_refuses_to_return_when_a_document_survives_that_should_not(
        self, cfg: Config, export_tree: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Simulate the filter failing to drop something, and confirm the
        # assertion at the end catches it rather than letting the upload proceed.
        real_resolve = exporting.resolve_class
        calls = {"n": 0}

        def flaky(cfg_arg, tag_names):
            # Claim everything is a receipt on the first pass (so nothing is
            # dropped), then tell the truth on the verification pass.
            calls["n"] += 1
            if calls["n"] <= 3:
                return "receipts"
            return real_resolve(cfg_arg, tag_names)

        monkeypatch.setattr(exporting, "resolve_class", flaky)

        with pytest.raises(ExportFilterLeak, match="Refusing to upload"):
            filter_export(cfg, root=export_tree, keep=cfg.archived_classes())
