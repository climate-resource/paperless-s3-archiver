"""
Object Lock: the difference between the burn-in and the real thing
"""

import contextlib
import datetime as dt

import pytest

from paperless_s3_archiver.config import Config
from paperless_s3_archiver.retention import year_end
from paperless_s3_archiver.s3 import (
    MissingCredentials,
    content_md5,
    put_locked,
    s3_client,
)


class TestS3Client:
    def test_reads_the_role_specific_credentials(
        self, cfg: Config, store_credentials: None, monkeypatch: pytest.MonkeyPatch
    ):
        # Bucket scoping is the storage-layer half of the entity separation, and
        # it only works if each role really uses its own key.
        seen = {}

        def fake_client(_service, **kwargs):
            seen.update(kwargs)
            return object()

        monkeypatch.setattr("paperless_s3_archiver.s3.boto3.client", fake_client)
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


class TestPortability:
    """
    The wire format, which decides whether a store other than ours accepts a PUT

    These pin behaviour that no functional test would notice, because moto
    accepts far more than a real S3-compatible store does.
    """

    def _headers(self, cfg: Config, **overrides: object) -> dict[str, str]:
        """Capture the headers one locked PUT would actually send."""
        seen: dict[str, str] = {}
        client = s3_client(cfg, role="writer")
        client.meta.events.register(
            "before-send.s3.PutObject",
            lambda request, **_kw: (seen.update(dict(request.headers)), None)[1],
        )
        with contextlib.suppress(Exception):
            put_locked(
                client=client,
                cfg=cfg,
                key="documents/2026/abc",
                body=b"hello",
                retain_until=year_end(2035),
                legal_hold=False,
                content_type="application/pdf",
                **overrides,  # ty: ignore[invalid-argument-type]
            )
        return {k.lower(): (v.decode() if isinstance(v, bytes) else v) for k, v in seen.items()}

    def test_a_locked_put_carries_an_integrity_header(self, cfg: Config, store_credentials: None):
        # S3 refuses a PUT that carries Object Lock retention without one, which
        # is the right rule for an object that cannot be corrected once written.
        headers = self._headers(cfg)
        assert "content-md5" in headers

    def test_the_integrity_header_is_the_md5_of_the_body(self, cfg: Config, store_credentials: None):
        headers = self._headers(cfg)
        assert headers["content-md5"] == content_md5(b"hello")

    def test_uploads_carry_no_aws_chunked_framing(self, cfg: Config, store_credentials: None):
        # botocore >= 1.36 adds this by default. It is an AWS wire format rather
        # than an S3 API, and some implementations store the encoding header as
        # object metadata, where it would follow the object for as long as it is
        # locked.
        headers = self._headers(cfg)
        assert "aws-chunked" not in headers.get("content-encoding", "")
        assert "x-amz-sdk-checksum-algorithm" not in headers

    def test_requests_go_to_the_configured_endpoint_in_path_style(self, cfg: Config, store_credentials: None):
        # Path style is what a store without wildcard DNS needs, and it is what
        # boto3 resolves to for a custom endpoint. Pinned because a change in
        # that default would break every non-AWS deployment silently.
        seen: dict[str, str] = {}
        client = s3_client(cfg, role="writer")
        client.meta.events.register(
            "before-send.s3.PutObject",
            lambda request, **_kw: (seen.update(url=request.url), None)[1],
        )
        with contextlib.suppress(Exception):
            put_locked(
                client=client,
                cfg=cfg,
                key="documents/2026/abc",
                body=b"hello",
                retain_until=year_end(2035),
                legal_hold=False,
                content_type="application/pdf",
            )
        assert seen["url"].startswith(f"{cfg.endpoint}/{cfg.bucket}/")
