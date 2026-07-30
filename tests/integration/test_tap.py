"""
A whole tap run: spool file in, locked object and sidecar out
"""

import hashlib
import json
from argparse import Namespace
from typing import Any

import pytest
import responses

from paperless_b2_archiver.commands.tap import cmd_tap
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.retention import year_end

TAGS = [
    {"id": 1, "name": "class:receipts"},
    {"id": 2, "name": "class:hr-file"},
    {"id": 3, "name": "class:eu-grant"},
    {"id": 4, "name": "grant:futura"},
    {"id": 5, "name": "class:books"},
]


def _page(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(results), "next": None, "results": results}


@pytest.fixture
def paperless_api(cfg: Config, monkeypatch: pytest.MonkeyPatch):
    """A stubbed paperless instance holding four documents."""
    monkeypatch.setenv("PAPERLESS_ARCHIVE_API_TOKEN", "test-token")
    documents = {
        10: {"id": 10, "title": "Invoice 1", "tags": [1], "created": "2026-03-04", "added": "2026-03-05"},
        11: {"id": 11, "title": "Personnel note", "tags": [2], "created": "2026-03-04"},
        12: {"id": 12, "title": "Grant report", "tags": [3, 4], "created": "2026-03-04"},
        13: {"id": 13, "title": "Mystery", "tags": [], "created": "2026-03-04"},
    }
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, f"{cfg.api_base}/groups/", json=_page([{"id": 7, "name": "hr"}]))
        mocked.add(
            responses.GET,
            f"{cfg.api_base}/users/",
            json=_page([{"id": 3, "username": "archive-tap"}]),
        )
        mocked.add(responses.GET, f"{cfg.api_base}/tags/", json=_page(TAGS))
        for doc_id, doc in documents.items():
            mocked.add(responses.GET, f"{cfg.api_base}/documents/{doc_id}/", json=doc)
            mocked.add(responses.PATCH, f"{cfg.api_base}/documents/{doc_id}/", json=doc)
        yield mocked


def _spool(cfg: Config, doc_id: int, filename: str) -> None:
    """Write a spool job and the media file it points at."""
    media = cfg.media_dir / "documents" / "originals" / filename
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(f"pretend pdf {doc_id}".encode())

    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    (cfg.spool_dir / f"{doc_id}.json").write_text(
        json.dumps(
            {
                "document_id": doc_id,
                "document_source_path": f"{cfg.container_media_root}/documents/originals/{filename}",
                "document_original_filename": filename,
            }
        ),
        encoding="utf-8",
    )


def _keys(client: Any, cfg: Config) -> set[str]:
    listing = client.list_objects_v2(Bucket=cfg.bucket)
    return {obj["Key"] for obj in listing.get("Contents", [])}


class TestTap:
    def test_archives_a_classified_document_and_its_sidecar(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        assert cmd_tap(cfg, Namespace()) == 0

        keys = _keys(locked_bucket, cfg)
        assert len(keys) == 2
        document = next(k for k in keys if not k.endswith(".json"))
        assert document.startswith("documents/2026/")
        assert f"{document}.json" in keys

    def test_the_object_is_locked_until_the_class_says(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        cmd_tap(cfg, Namespace())

        document = next(k for k in _keys(locked_bucket, cfg) if not k.endswith(".json"))
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key=document)
        # receipts is 9 years on the document-year clock, and the document is 2026.
        assert head["ObjectLockRetainUntilDate"] == year_end(2035)
        assert head["ObjectLockMode"] == "COMPLIANCE"

    def test_the_key_is_the_content_hash(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        cmd_tap(cfg, Namespace())

        document = next(k for k in _keys(locked_bucket, cfg) if not k.endswith(".json"))
        expected = hashlib.sha256(b"pretend pdf 10").hexdigest()
        assert document == f"documents/2026/{expected}"

    def test_the_sidecar_explains_the_date(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        cmd_tap(cfg, Namespace())

        sidecar_key = next(k for k in _keys(locked_bucket, cfg) if k.endswith(".json"))
        sidecar = json.loads(locked_bucket.get_object(Bucket=cfg.bucket, Key=sidecar_key)["Body"].read())
        assert sidecar["retention_class"] == "receipts"
        assert sidecar["retention_basis"].startswith("§ 147")
        assert sidecar["paperless_document_id"] == 10
        assert sidecar["retain_until"] == year_end(2035).isoformat()
        assert "document year 2026 + 9" in sidecar["explanation"]

    def test_a_never_archived_class_is_indexed_and_not_written(
        self, cfg: Config, locked_bucket, paperless_api
    ):
        # The boundary the whole design exists to hold: personnel material is
        # searchable in paperless and never reaches the bucket.
        _spool(cfg, 11, "personnel.pdf")
        assert cmd_tap(cfg, Namespace()) == 0
        assert _keys(locked_bucket, cfg) == set()

    def test_an_unclassified_document_is_refused_and_not_written(
        self, cfg: Config, locked_bucket, paperless_api
    ):
        _spool(cfg, 13, "mystery.pdf")
        assert cmd_tap(cfg, Namespace()) == 0
        assert _keys(locked_bucket, cfg) == set()

    def test_a_grant_document_uses_the_grants_period(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 12, "grant.pdf")
        cmd_tap(cfg, Namespace())

        document = next(k for k in _keys(locked_bucket, cfg) if not k.endswith(".json"))
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key=document)
        # futura: final payment 2030 + 5, and the 2026 document year is ignored.
        assert head["ObjectLockRetainUntilDate"] == year_end(2035)

    def test_a_processed_job_leaves_the_spool(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        cmd_tap(cfg, Namespace())
        assert list(cfg.spool_dir.glob("*.json")) == []

    def test_a_failed_job_stays_in_the_spool_for_the_next_run(
        self, cfg: Config, locked_bucket, paperless_api
    ):
        # Completeness is the point: a document must never be quietly dropped.
        cfg.spool_dir.mkdir(parents=True, exist_ok=True)
        (cfg.spool_dir / "10.json").write_text(
            json.dumps(
                {
                    "document_id": 10,
                    "document_source_path": f"{cfg.container_media_root}/documents/originals/gone.pdf",
                }
            ),
            encoding="utf-8",
        )
        assert cmd_tap(cfg, Namespace()) == 1
        assert (cfg.spool_dir / "10.json").exists()

    def test_writes_its_metrics_even_when_a_job_failed(self, cfg: Config, locked_bucket, paperless_api):
        # A job that fails silently and a job that never ran look identical to
        # an alert rule otherwise.
        cfg.spool_dir.mkdir(parents=True, exist_ok=True)
        (cfg.spool_dir / "10.json").write_text("not json at all", encoding="utf-8")
        assert cmd_tap(cfg, Namespace()) == 1

        text = (cfg.metrics_dir / "paperless_crs_ingest.prom").read_text(encoding="utf-8")
        assert 'paperless_ingest_last_run_status{entity="crs"} 0' in text

    def test_counts_each_outcome(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        _spool(cfg, 11, "personnel.pdf")
        _spool(cfg, 13, "mystery.pdf")
        assert cmd_tap(cfg, Namespace()) == 0

        text = (cfg.metrics_dir / "paperless_crs_ingest.prom").read_text(encoding="utf-8")
        assert 'paperless_ingest_archived_total{entity="crs"} 1.0' in text
        assert 'paperless_ingest_rejected_total{entity="crs"} 1.0' in text
        assert 'paperless_ingest_indexed_not_archived_total{entity="crs"} 1.0' in text

    def test_journals_every_decision_including_the_refusals(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        _spool(cfg, 11, "personnel.pdf")
        _spool(cfg, 13, "mystery.pdf")
        cmd_tap(cfg, Namespace())

        events = [
            json.loads(line)["event"]
            for line in (cfg.journal_dir / "tap.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert set(events) == {"archived", "indexed_not_archived", "reject"}


class TestTapDuringBurnIn:
    def test_writes_governance_and_a_disposable_date(
        self, burn_in_cfg: Config, locked_bucket, paperless_api, monkeypatch
    ):
        monkeypatch.setenv("PAPERLESS_ARCHIVE_API_TOKEN", "test-token")
        _spool(burn_in_cfg, 10, "invoice.pdf")
        assert cmd_tap(burn_in_cfg, Namespace()) == 0

        document = next(k for k in _keys(locked_bucket, burn_in_cfg) if not k.endswith(".json"))
        head = locked_bucket.head_object(Bucket=burn_in_cfg.bucket, Key=document)
        assert head["ObjectLockMode"] == "GOVERNANCE"
        assert head["ObjectLockRetainUntilDate"] < year_end(2035)

    def test_the_sidecar_records_that_it_was_burn_in_output(
        self, burn_in_cfg: Config, locked_bucket, paperless_api, monkeypatch
    ):
        # Nothing counts as archived until it is re-ingested under COMPLIANCE,
        # and the object itself has to say so.
        monkeypatch.setenv("PAPERLESS_ARCHIVE_API_TOKEN", "test-token")
        _spool(burn_in_cfg, 10, "invoice.pdf")
        cmd_tap(burn_in_cfg, Namespace())

        sidecar_key = next(k for k in _keys(locked_bucket, burn_in_cfg) if k.endswith(".json"))
        sidecar = json.loads(
            locked_bucket.get_object(Bucket=burn_in_cfg.bucket, Key=sidecar_key)["Body"].read()
        )
        assert sidecar["burn_in"] is True
        assert sidecar["object_lock_mode"] == "GOVERNANCE"


class TestRestrictedDocuments:
    def test_personnel_material_is_granted_to_the_hr_group(self, cfg: Config, locked_bucket, paperless_api):
        # In paperless an ownerless document is visible to everyone who can view
        # documents, including an auditor account.
        _spool(cfg, 11, "personnel.pdf")
        cmd_tap(cfg, Namespace())

        patches = [c for c in paperless_api.calls if c.request.method == "PATCH"]
        assert len(patches) == 1
        payload = json.loads(patches[0].request.body)
        assert payload["owner"] == 3
        assert payload["set_permissions"]["view"]["groups"] == [7]

    def test_an_ordinary_document_is_left_alone(self, cfg: Config, locked_bucket, paperless_api):
        _spool(cfg, 10, "invoice.pdf")
        cmd_tap(cfg, Namespace())
        assert [c for c in paperless_api.calls if c.request.method == "PATCH"] == []
