"""
Object Lock: the difference between the burn-in and the real thing
"""

import datetime as dt

import pytest

from paperless_b2_archiver.b2 import MissingCredentials, put_locked, s3_client
from paperless_b2_archiver.config import Config
from paperless_b2_archiver.retention import year_end


class TestS3Client:
    def test_reads_the_role_specific_credentials(
        self, cfg: Config, b2_credentials: None, monkeypatch: pytest.MonkeyPatch
    ):
        # Bucket scoping is the storage-layer half of the entity separation, and
        # it only works if each role really uses its own key.
        seen = {}

        def fake_client(_service, **kwargs):
            seen.update(kwargs)
            return object()

        monkeypatch.setattr("paperless_b2_archiver.b2.boto3.client", fake_client)
        s3_client(cfg, role="reader")
        assert seen["aws_access_key_id"] == "test-reader-id"
        assert seen["endpoint_url"] == cfg.endpoint

    def test_refuses_when_the_role_has_no_credentials(self, cfg: Config):
        with pytest.raises(MissingCredentials, match="writer"):
            s3_client(cfg, role="writer")


class TestPutLockedUnderCompliance:
    def test_writes_the_computed_retain_until(self, cfg: Config, locked_bucket):
        retain_until = year_end(2035)
        put_locked(
            client=locked_bucket,
            cfg=cfg,
            key="documents/2026/abc",
            body=b"pdf",
            retain_until=retain_until,
            legal_hold=False,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key="documents/2026/abc")
        assert head["ObjectLockMode"] == "COMPLIANCE"
        assert head["ObjectLockRetainUntilDate"] == retain_until

    def test_applies_a_legal_hold_when_asked(self, cfg: Config, locked_bucket):
        put_locked(
            client=locked_bucket,
            cfg=cfg,
            key="documents/2027/hr",
            body=b"pdf",
            retain_until=year_end(2038),
            legal_hold=True,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key="documents/2027/hr")
        assert head["ObjectLockLegalHoldStatus"] == "ON"

    def test_omits_the_hold_when_not_asked(self, cfg: Config, locked_bucket):
        put_locked(
            client=locked_bucket,
            cfg=cfg,
            key="documents/2026/plain",
            body=b"pdf",
            retain_until=year_end(2035),
            legal_hold=False,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key="documents/2026/plain")
        assert head.get("ObjectLockLegalHoldStatus", "OFF") == "OFF"

    def test_carries_the_checksum_and_class_as_metadata(self, cfg: Config, locked_bucket):
        put_locked(
            client=locked_bucket,
            cfg=cfg,
            key="documents/2026/meta",
            body=b"pdf",
            retain_until=year_end(2035),
            legal_hold=False,
            content_type="application/pdf",
            metadata={"sha256": "deadbeef", "class": "receipts"},
        )
        head = locked_bucket.head_object(Bucket=cfg.bucket, Key="documents/2026/meta")
        assert head["Metadata"]["sha256"] == "deadbeef"


class TestPutLockedDuringBurnIn:
    def test_writes_governance_not_compliance(self, burn_in_cfg: Config, locked_bucket):
        put_locked(
            client=locked_bucket,
            cfg=burn_in_cfg,
            key="documents/2026/abc",
            body=b"pdf",
            retain_until=year_end(2035),
            legal_hold=False,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=burn_in_cfg.bucket, Key="documents/2026/abc")
        assert head["ObjectLockMode"] == "GOVERNANCE"

    def test_ignores_the_computed_date_in_favour_of_a_short_one(self, burn_in_cfg: Config, locked_bucket):
        # Everything written during burn-in is disposable. A 2035 date on a
        # burn-in object would strand the bucket for a decade.
        put_locked(
            client=locked_bucket,
            cfg=burn_in_cfg,
            key="documents/2026/abc",
            body=b"pdf",
            retain_until=year_end(2035),
            legal_hold=False,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=burn_in_cfg.bucket, Key="documents/2026/abc")
        horizon = dt.datetime.now(dt.UTC) + dt.timedelta(days=burn_in_cfg.burn_in_retain_days + 1)
        assert head["ObjectLockRetainUntilDate"] < horizon

    def test_never_applies_a_legal_hold(self, burn_in_cfg: Config, locked_bucket):
        # The writer key holds no capability to release a hold, so one applied
        # during burn-in would strand the bucket permanently.
        put_locked(
            client=locked_bucket,
            cfg=burn_in_cfg,
            key="documents/2027/hr",
            body=b"pdf",
            retain_until=year_end(2038),
            legal_hold=True,
            content_type="application/pdf",
        )
        head = locked_bucket.head_object(Bucket=burn_in_cfg.bucket, Key="documents/2027/hr")
        assert head.get("ObjectLockLegalHoldStatus", "OFF") == "OFF"
