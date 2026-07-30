"""
export and metrics, both of which go through the runtime seam
"""

import json
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest
import responses

from paperless_b2_archiver.commands.export import (
    NoExportAvailable,
    cmd_auditor_export,
    cmd_export,
    cmd_fetch_export,
)
from paperless_b2_archiver.commands.metrics import cmd_metrics
from paperless_b2_archiver.config import Config


class FakeRuntime:
    """A deployment that produces an export without a container in sight."""

    def __init__(self, cfg: Config, *, tunnel_up: bool = False, fail: bool = False) -> None:
        self.cfg = cfg
        self.tunnel_up = tunnel_up
        self.fail = fail
        self.calls: list[str] = []

    def run_document_exporter(self, destination: str) -> None:
        self.calls.append(destination)
        if self.fail:
            raise OSError("the exporter fell over")
        _write_export(self.cfg.export_dir / "full")

    def tunnel_running(self) -> bool:
        return self.tunnel_up


def _write_export(root: Path) -> None:
    """A two-document export: one archived class, one never-archived."""
    manifest: list[dict[str, Any]] = [
        {"model": "documents.tag", "pk": 1, "fields": {"name": "class:receipts"}},
        {"model": "documents.tag", "pk": 2, "fields": {"name": "class:hr-file"}},
        {
            "model": "documents.document",
            "pk": 10,
            "fields": {"title": "Invoice", "tags": [1]},
            "__exported_file_name__": "originals/invoice.pdf",
        },
        {
            "model": "documents.document",
            "pk": 11,
            "fields": {"title": "Personnel", "tags": [2]},
            "__exported_file_name__": "originals/personnel.pdf",
        },
    ]
    (root / "originals").mkdir(parents=True, exist_ok=True)
    for rec in manifest:
        name = rec.get("__exported_file_name__")
        if name:
            (root / name).write_bytes(b"pdf")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def fake_runtime(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> FakeRuntime:
    runtime = FakeRuntime(cfg)
    monkeypatch.setattr("paperless_b2_archiver.commands.export.get_runtime", lambda **_kwargs: runtime)
    return runtime


class TestExport:
    def test_uploads_the_filtered_tree(self, cfg: Config, locked_bucket, fake_runtime):
        assert cmd_export(cfg, Namespace()) == 0

        listing = locked_bucket.list_objects_v2(Bucket=cfg.bucket, Prefix="exports/")
        keys = {obj["Key"] for obj in listing["Contents"]}
        assert any(k.endswith("manifest.json") for k in keys)
        assert any(k.endswith("originals/invoice.pdf") for k in keys)

    def test_personnel_material_never_reaches_the_bucket(self, cfg: Config, locked_bucket, fake_runtime):
        # An unfiltered export would lock away for eleven years exactly the
        # material the tap deliberately kept deletable.
        cmd_export(cfg, Namespace())
        listing = locked_bucket.list_objects_v2(Bucket=cfg.bucket, Prefix="exports/")
        keys = {obj["Key"] for obj in listing["Contents"]}
        assert not any("personnel" in k for k in keys)

    def test_the_unfiltered_export_stays_on_disk_for_the_backup_chain(
        self, cfg: Config, locked_bucket, fake_runtime
    ):
        cmd_export(cfg, Namespace())
        assert (cfg.export_dir / "full" / "originals" / "personnel.pdf").exists()

    def test_the_uploaded_objects_are_locked(self, cfg: Config, locked_bucket, fake_runtime):
        cmd_export(cfg, Namespace())
        listing = locked_bucket.list_objects_v2(Bucket=cfg.bucket, Prefix="exports/")
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key=listing["Contents"][0]["Key"])
        assert head["ObjectLockMode"] == "COMPLIANCE"

    def test_counts_what_it_kept_and_dropped(self, cfg: Config, locked_bucket, fake_runtime):
        cmd_export(cfg, Namespace())
        text = (cfg.metrics_dir / "paperless_crs_export.prom").read_text(encoding="utf-8")
        assert 'paperless_export_documents{entity="crs"} 1.0' in text
        assert 'paperless_export_filtered_documents{entity="crs"} 1.0' in text

    def test_reports_a_failed_exporter_without_uploading(self, cfg: Config, locked_bucket, monkeypatch):
        runtime = FakeRuntime(cfg, fail=True)
        monkeypatch.setattr("paperless_b2_archiver.commands.export.get_runtime", lambda **_kwargs: runtime)
        assert cmd_export(cfg, Namespace()) == 1
        assert locked_bucket.list_objects_v2(Bucket=cfg.bucket).get("Contents", []) == []

        text = (cfg.metrics_dir / "paperless_crs_export.prom").read_text(encoding="utf-8")
        assert 'paperless_export_last_run_status{entity="crs"} 0' in text


class TestAuditorExport:
    def test_builds_a_handover_scoped_to_the_tax_classes(
        self, cfg: Config, locked_bucket, fake_runtime, tmp_path, capsys
    ):
        cmd_export(cfg, Namespace())
        target = tmp_path / "handover"

        assert cmd_auditor_export(cfg, Namespace(target=str(target))) == 0
        assert not (target / "originals" / "personnel.pdf").exists()
        assert (target / "originals" / "invoice.pdf").exists()
        assert "1 documents" in capsys.readouterr().out

    def test_refuses_when_there_is_no_export_to_build_from(self, cfg: Config, tmp_path):
        with pytest.raises(NoExportAvailable, match="Run `paperless-archive export` first"):
            cmd_auditor_export(cfg, Namespace(target=str(tmp_path / "handover")))


class TestFetchExport:
    def test_downloads_the_most_recent_export(
        self, cfg: Config, locked_bucket, fake_runtime, tmp_path, capsys
    ):
        cmd_export(cfg, Namespace())
        target = tmp_path / "restored"

        assert cmd_fetch_export(cfg, Namespace(target=str(target))) == 0
        assert (target / "manifest.json").exists()
        # It prints the date it found, which is what the restore drill reads.
        assert capsys.readouterr().out.strip().startswith("20")

    def test_refuses_when_the_bucket_holds_no_export(self, cfg: Config, locked_bucket, tmp_path):
        with pytest.raises(NoExportAvailable, match="no export under exports/"):
            cmd_fetch_export(cfg, Namespace(target=str(tmp_path / "restored")))


class TestMetrics:
    @pytest.fixture
    def paperless_api(self, cfg: Config, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("PAPERLESS_ARCHIVE_API_TOKEN", "test-token")
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            yield mocked

    def _users(self, mocked, cfg: Config, users: list[dict[str, Any]]) -> None:
        mocked.add(
            responses.GET,
            f"{cfg.api_base}/users/",
            json={"count": len(users), "next": None, "results": users},
        )

    def test_reports_the_instance_as_up(self, cfg: Config, paperless_api):
        self._users(paperless_api, cfg, [])
        assert cmd_metrics(cfg, Namespace()) == 0
        text = (cfg.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        assert 'paperless_instance_up{entity="crs"} 1.0' in text

    def test_reports_the_instance_as_down_without_failing_the_job(self, cfg: Config, paperless_api):
        # This job's own failure and the instance being down are different
        # alerts, so an unreachable paperless is reported, not raised.
        paperless_api.add(responses.GET, f"{cfg.api_base}/users/", status=500)
        assert cmd_metrics(cfg, Namespace()) == 0
        text = (cfg.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        assert 'paperless_instance_up{entity="crs"} 0.0' in text

    def test_reports_a_break_glass_login(self, cfg: Config, paperless_api):
        # Any increase in this is worth noticing.
        self._users(
            paperless_api,
            cfg,
            [{"id": 1, "username": "breakglass", "last_login": "2026-07-30T09:00:00Z"}],
        )
        cmd_metrics(cfg, Namespace())
        text = (cfg.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        line = next(
            entry for entry in text.splitlines() if entry.startswith("paperless_breakglass_last_login")
        )
        assert float(line.rsplit(" ", 1)[1]) > 0

    def test_reports_no_exposure_when_audit_mode_is_off(self, cfg: Config, paperless_api):
        self._users(paperless_api, cfg, [])
        cmd_metrics(cfg, Namespace())
        text = (cfg.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        assert 'paperless_public_exposure_active{entity="crs"} 0.0' in text

    def test_reports_the_exposure_window_while_audit_mode_is_on(
        self, cfg: Config, paperless_api, monkeypatch
    ):
        open_cfg = cfg.model_copy(update={"public_enabled": True, "public_until": "2099-01-01"})
        monkeypatch.setattr(
            "paperless_b2_archiver.commands.metrics.get_runtime",
            lambda **_kwargs: FakeRuntime(open_cfg, tunnel_up=True),
        )
        self._users(paperless_api, open_cfg, [])
        cmd_metrics(open_cfg, Namespace())

        text = (open_cfg.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        assert 'paperless_public_exposure_active{entity="crs"} 1.0' in text
        remaining = next(
            entry
            for entry in text.splitlines()
            if entry.startswith("paperless_public_exposure_seconds_remaining")
        )
        assert float(remaining.rsplit(" ", 1)[1]) > 0

    def test_a_window_past_its_date_reports_negative_seconds(self, cfg: Config, paperless_api, monkeypatch):
        # Negative means the window should already have closed, which is what
        # the alert rule keys off.
        stale = cfg.model_copy(update={"public_enabled": True, "public_until": "2020-01-01"})
        monkeypatch.setattr(
            "paperless_b2_archiver.commands.metrics.get_runtime",
            lambda **_kwargs: FakeRuntime(stale, tunnel_up=True),
        )
        self._users(paperless_api, stale, [])
        cmd_metrics(stale, Namespace())

        text = (stale.metrics_dir / "paperless_crs_status.prom").read_text(encoding="utf-8")
        remaining = next(
            entry
            for entry in text.splitlines()
            if entry.startswith("paperless_public_exposure_seconds_remaining")
        )
        assert float(remaining.rsplit(" ", 1)[1]) < 0
