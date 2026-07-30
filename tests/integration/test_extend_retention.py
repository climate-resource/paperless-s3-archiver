"""
extend-retention against a real bucket: only ever later, and never in burn-in
"""

import json
from argparse import Namespace
from typing import Any

import pytest

from paperless_s3_archiver.commands.extend_retention import RefusedExtension, cmd_extend_retention
from paperless_s3_archiver.config import Config
from paperless_s3_archiver.retention import year_end
from paperless_s3_archiver.s3 import put_locked


def args(**overrides: Any) -> Namespace:
    base = {"apply": False, "reason": "", "retention_class": "", "grant": ""}
    base.update(overrides)
    return Namespace(**base)


def archive(cfg: Config, client: Any, *, key: str, sidecar: dict[str, Any], retain_year: int) -> None:
    """Put a document and its sidecar, both locked to the same date."""
    for target, body, content_type in (
        (key, b"pdf", "application/pdf"),
        (f"{key}.json", json.dumps(sidecar).encode(), "application/json"),
    ):
        put_locked(
            client=client,
            cfg=cfg,
            key=target,
            body=body,
            retain_until=year_end(retain_year),
            legal_hold=False,
            content_type=content_type,
        )


def retain_year_of(client: Any, cfg: Config, key: str) -> int:
    return client.head_object(Bucket=cfg.bucket, Key=key)["ObjectLockRetainUntilDate"].year


@pytest.fixture
def archived_grant_document(cfg: Config, locked_bucket):
    """One grant document, locked against the grant's period as it read then."""
    archive(
        cfg,
        locked_bucket,
        key="documents/2026/aaa",
        sidecar={"retention_class": "eu-grant", "tags": ["class:eu-grant", "grant:futura"]},
        retain_year=2035,
    )
    return locked_bucket


class TestDryRun:
    def test_writes_nothing_when_the_config_has_not_moved(self, cfg: Config, archived_grant_document, capsys):
        assert cmd_extend_retention(cfg, args()) == 0
        assert "nothing to extend" in capsys.readouterr().out
        assert retain_year_of(archived_grant_document, cfg, "documents/2026/aaa") == 2035

    def test_reports_what_it_would_do_after_an_amendment(self, cfg: Config, archived_grant_document, capsys):
        extended = cfg.model_copy(
            update={
                "grants": {"futura": cfg.grants["futura"].model_copy(update={"final_payment_year": 2031})}
            }
        )
        assert cmd_extend_retention(extended, args()) == 0

        out = capsys.readouterr().out
        assert "would extend" in out
        assert "2 object(s) would be extended" in out
        # And nothing actually moved.
        assert retain_year_of(archived_grant_document, cfg, "documents/2026/aaa") == 2035


class TestApply:
    def _extended(self, cfg: Config) -> Config:
        return cfg.model_copy(
            update={
                "grants": {"futura": cfg.grants["futura"].model_copy(update={"final_payment_year": 2031})}
            }
        )

    def test_moves_the_document_and_its_sidecar_together(self, cfg: Config, archived_grant_document):
        # A record whose metadata outlives it, or the reverse, is not a record.
        status = cmd_extend_retention(self._extended(cfg), args(apply=True, reason="FUTURA amendment 3"))
        assert status == 0
        assert retain_year_of(archived_grant_document, cfg, "documents/2026/aaa") == 2036
        assert retain_year_of(archived_grant_document, cfg, "documents/2026/aaa.json") == 2036

    def test_leaves_a_locked_record_of_the_act_in_the_bucket(self, cfg: Config, archived_grant_document):
        cmd_extend_retention(self._extended(cfg), args(apply=True, reason="FUTURA amendment 3"))

        listing = archived_grant_document.list_objects_v2(Bucket=cfg.bucket, Prefix="retention-extensions/")
        records = listing.get("Contents", [])
        assert len(records) == 1

        body = archived_grant_document.get_object(Bucket=cfg.bucket, Key=records[0]["Key"])["Body"].read()
        record = json.loads(body)
        assert record["reason"] == "FUTURA amendment 3"
        assert len(record["extensions"]) == 2
        assert record["extensions"][0]["retain_until_after"].startswith("2036")

    def test_the_record_is_itself_locked(self, cfg: Config, archived_grant_document):
        cmd_extend_retention(self._extended(cfg), args(apply=True, reason="x"))
        listing = archived_grant_document.list_objects_v2(Bucket=cfg.bucket, Prefix="retention-extensions/")
        key = listing["Contents"][0]["Key"]
        assert (
            archived_grant_document.head_object(Bucket=cfg.bucket, Key=key)["ObjectLockMode"] == "COMPLIANCE"
        )

    def test_is_safe_to_re_run(self, cfg: Config, archived_grant_document, capsys):
        extended = self._extended(cfg)
        cmd_extend_retention(extended, args(apply=True, reason="first"))
        capsys.readouterr()

        assert cmd_extend_retention(extended, args(apply=True, reason="second")) == 0
        assert "nothing to extend" in capsys.readouterr().out
        # No second record, because nothing moved.
        listing = archived_grant_document.list_objects_v2(Bucket=cfg.bucket, Prefix="retention-extensions/")
        assert len(listing["Contents"]) == 1

    def test_never_shortens(self, cfg: Config, archived_grant_document):
        # Shortening is not offered and is never attempted. COMPLIANCE would
        # refuse it anyway, and asking is not something this program should do.
        shortened = cfg.model_copy(
            update={
                "grants": {"futura": cfg.grants["futura"].model_copy(update={"final_payment_year": 2027})}
            }
        )
        assert cmd_extend_retention(shortened, args(apply=True, reason="wrong direction")) == 0
        assert retain_year_of(archived_grant_document, cfg, "documents/2026/aaa") == 2035


class TestFilters:
    def test_narrows_to_one_grant(self, cfg: Config, locked_bucket):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/aaa",
            sidecar={"retention_class": "eu-grant", "tags": ["grant:futura"]},
            retain_year=2035,
        )
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/bbb",
            sidecar={"retention_class": "receipts", "tags": ["class:receipts"]},
            retain_year=2035,
        )
        extended = cfg.model_copy(
            update={
                "grants": {"futura": cfg.grants["futura"].model_copy(update={"final_payment_year": 2031})}
            }
        )
        cmd_extend_retention(extended, args(apply=True, reason="x", grant="futura"))

        assert retain_year_of(locked_bucket, cfg, "documents/2026/aaa") == 2036
        assert retain_year_of(locked_bucket, cfg, "documents/2026/bbb") == 2035

    def test_narrows_to_one_class(self, cfg: Config, locked_bucket):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/bbb",
            sidecar={"retention_class": "receipts", "tags": ["class:receipts"]},
            retain_year=2030,
        )
        cmd_extend_retention(cfg, args(apply=True, reason="x", retention_class="receipts"))
        # receipts is 9 years on a 2026 document.
        assert retain_year_of(locked_bucket, cfg, "documents/2026/bbb") == 2035

    def test_ignores_exports_and_inventories(self, cfg: Config, locked_bucket):
        # Those carry a flat period of their own rather than one derived from a
        # class, so nothing in the config can change what they hold.
        put_locked(
            client=locked_bucket,
            cfg=cfg,
            key="exports/2026-07-30/manifest.json",
            body=b"[]",
            retain_until=year_end(2037),
            legal_hold=False,
            content_type="application/json",
        )
        assert cmd_extend_retention(cfg, args()) == 0
        assert retain_year_of(locked_bucket, cfg, "exports/2026-07-30/manifest.json") == 2037


class TestRefusals:
    def test_refuses_apply_with_no_reason(self, cfg: Config, locked_bucket):
        with pytest.raises(RefusedExtension, match="needs --reason"):
            cmd_extend_retention(cfg, args(apply=True, reason="   "))

    def test_refuses_apply_during_burn_in(self, burn_in_cfg: Config, locked_bucket):
        # Burn-in objects are disposable by design. Giving one a real retention
        # date would strand the burn-in bucket for a decade.
        with pytest.raises(RefusedExtension, match="burn-in"):
            cmd_extend_retention(burn_in_cfg, args(apply=True, reason="a good one"))

    def test_allows_a_dry_run_during_burn_in(self, burn_in_cfg: Config, locked_bucket):
        assert cmd_extend_retention(burn_in_cfg, args()) == 0


class TestUnexplainedObjects:
    def test_fails_on_a_class_the_table_no_longer_has(self, cfg: Config, locked_bucket):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/ccc",
            sidecar={"retention_class": "retired", "tags": []},
            retain_year=2035,
        )
        # Reported and non-zero rather than guessing a date.
        assert cmd_extend_retention(cfg, args()) == 1

    def test_fails_on_an_object_in_a_never_archived_class(self, cfg: Config, locked_bucket):
        # That is a leak to investigate, not a retention question.
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/ddd",
            sidecar={"retention_class": "hr-file", "tags": []},
            retain_year=2035,
        )
        assert cmd_extend_retention(cfg, args()) == 1

    def test_writes_its_metrics_on_the_failure_path(self, cfg: Config, locked_bucket):
        archive(
            cfg,
            locked_bucket,
            key="documents/2026/ccc",
            sidecar={"retention_class": "retired", "tags": []},
            retain_year=2035,
        )
        cmd_extend_retention(cfg, args())
        text = (cfg.metrics_dir / "paperless_crs_extend_retention.prom").read_text(encoding="utf-8")
        assert 'paperless_retention_extend_last_run_status{entity="crs"} 0' in text
