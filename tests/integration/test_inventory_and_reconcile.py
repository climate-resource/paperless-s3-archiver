"""
inventory and reconcile: the archive checking itself, in both directions
"""

import datetime as dt
import json
from argparse import Namespace
from typing import Any

import pytest
import responses

from paperless_b2_archiver.b2 import put_locked
from paperless_b2_archiver.commands.inventory import cmd_inventory
from paperless_b2_archiver.commands.reconcile import cmd_reconcile
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.retention import year_end

TAGS = [
    {"id": 1, "name": "class:receipts"},
    {"id": 2, "name": "class:hr-file"},
]


def _page(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(results), "next": None, "results": results}


def archive(cfg: Config, client: Any, *, key: str, sidecar: dict[str, Any]) -> None:
    put_locked(
        client=client,
        cfg=cfg,
        key=key,
        body=b"pdf",
        retain_until=year_end(2035),
        legal_hold=False,
        content_type="application/pdf",
    )
    put_locked(
        client=client,
        cfg=cfg,
        key=f"{key}.json",
        body=json.dumps(sidecar).encode(),
        retain_until=year_end(2035),
        legal_hold=False,
        content_type="application/json",
    )


class TestInventory:
    def test_writes_a_dated_inventory_into_the_bucket(self, cfg: Config, locked_bucket):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/aaa",
            sidecar={"retention_class": "receipts", "paperless_document_id": 10},
        )
        assert cmd_inventory(cfg, Namespace()) == 0

        stamp = dt.date.today().isoformat()
        body = locked_bucket.get_object(Bucket=cfg.bucket, Key=f"inventory/{stamp}.json")["Body"].read()
        inventory = json.loads(body)
        assert inventory["entity"] == "crs"
        assert inventory["object_count"] == 2

    def test_the_inventory_is_itself_locked(self, cfg: Config, locked_bucket):
        # That is what turns "what was in the archive on this date" into a
        # question with a tamper-evident answer.
        cmd_inventory(cfg, Namespace())
        stamp = dt.date.today().isoformat()
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key=f"inventory/{stamp}.json")
        assert head["ObjectLockMode"] == "COMPLIANCE"

    def test_records_each_objects_retention_state(self, cfg: Config, locked_bucket):
        # Retention state is what an auditor actually checks, so it belongs in
        # the inventory rather than only in the bucket's live metadata.
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/aaa",
            sidecar={"retention_class": "receipts", "paperless_document_id": 10},
        )
        cmd_inventory(cfg, Namespace())

        stamp = dt.date.today().isoformat()
        body = locked_bucket.get_object(Bucket=cfg.bucket, Key=f"inventory/{stamp}.json")["Body"].read()
        entry = next(o for o in json.loads(body)["objects"] if o["key"] == "documents/2026/aaa")
        assert entry["object_lock_mode"] == "COMPLIANCE"
        assert entry["retain_until"].startswith("2035")

    def test_writes_its_metrics(self, cfg: Config, locked_bucket):
        cmd_inventory(cfg, Namespace())
        text = (cfg.metrics_dir / "paperless_crs_inventory.prom").read_text(encoding="utf-8")
        assert 'paperless_inventory_last_run_status{entity="crs"} 1' in text


class TestReconcile:
    @pytest.fixture
    def paperless_api(self, cfg: Config, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PAPERLESS_ARCHIVE_API_TOKEN", "test-token")
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            yield mocked

    def _serve(self, mocked, cfg: Config, documents: list[dict[str, Any]]) -> None:
        mocked.add(responses.GET, f"{cfg.api_base}/tags/", json=_page(TAGS))
        mocked.add(responses.GET, f"{cfg.api_base}/documents/", json=_page(documents))

    def test_is_quiet_when_both_sides_agree(self, cfg: Config, locked_bucket, paperless_api):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/aaa",
            sidecar={"retention_class": "receipts", "paperless_document_id": 10},
        )
        cmd_inventory(cfg, Namespace())
        self._serve(paperless_api, cfg, [{"id": 10, "tags": [1], "title": "Invoice"}])

        assert cmd_reconcile(cfg, Namespace()) == 0
        text = (cfg.metrics_dir / "paperless_crs_reconcile.prom").read_text(encoding="utf-8")
        assert 'paperless_reconcile_gap_count{entity="crs"} 0.0' in text
        assert 'paperless_worm_hr_leak_total{entity="crs"} 0.0' in text

    def test_reports_a_document_missing_from_the_bucket(self, cfg: Config, locked_bucket, paperless_api):
        # A failed ingest, or a deletion.
        cmd_inventory(cfg, Namespace())
        self._serve(paperless_api, cfg, [{"id": 10, "tags": [1], "title": "Invoice"}])

        assert cmd_reconcile(cfg, Namespace()) == 0
        text = (cfg.metrics_dir / "paperless_crs_reconcile.prom").read_text(encoding="utf-8")
        assert 'paperless_reconcile_gap_count{entity="crs"} 1.0' in text

    def test_reports_never_archived_material_found_in_the_bucket(
        self, cfg: Config, locked_bucket, paperless_api
    ):
        # The one that pages: material that should never have been locked, and
        # cannot be taken back.
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/leak",
            sidecar={"retention_class": "hr-file", "paperless_document_id": 11},
        )
        cmd_inventory(cfg, Namespace())
        self._serve(paperless_api, cfg, [{"id": 11, "tags": [2], "title": "Personnel"}])

        assert cmd_reconcile(cfg, Namespace()) == 0
        text = (cfg.metrics_dir / "paperless_crs_reconcile.prom").read_text(encoding="utf-8")
        assert 'paperless_worm_hr_leak_total{entity="crs"} 1.0' in text

    def test_the_leak_counter_only_ever_goes_up(self, cfg: Config, locked_bucket, paperless_api):
        # Alerts fire on increases, and an unfixable finding must not disappear
        # from the metric just because a later run saw a different bucket.
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/leak",
            sidecar={"retention_class": "hr-file", "paperless_document_id": 11},
        )
        cmd_inventory(cfg, Namespace())
        self._serve(paperless_api, cfg, [{"id": 11, "tags": [2], "title": "Personnel"}])
        cmd_reconcile(cfg, Namespace())
        cmd_reconcile(cfg, Namespace())

        text = (cfg.metrics_dir / "paperless_crs_reconcile.prom").read_text(encoding="utf-8")
        assert 'paperless_worm_hr_leak_total{entity="crs"} 2.0' in text

    def test_fails_when_there_is_no_inventory_to_compare_against(
        self, cfg: Config, locked_bucket, paperless_api
    ):
        assert cmd_reconcile(cfg, Namespace()) == 1
        text = (cfg.metrics_dir / "paperless_crs_reconcile.prom").read_text(encoding="utf-8")
        assert 'paperless_reconcile_last_run_status{entity="crs"} 0' in text
