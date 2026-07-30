import datetime as dt
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from paperless_b2_archiver.config import Config

#: A table with one class per clock, so every branch of the retention
#: arithmetic has a fixture to exercise it. Deliberately not a copy of any real
#: entity's table: the periods here are round numbers chosen to make an
#: off-by-one visible, not legal advice.
RETENTION_CLASSES: dict[str, dict[str, Any]] = {
    "books": {
        "archive": True,
        "years": 11,
        "clock": "document_year",
        "restricted": False,
        "basis": "§ 147 (1) Nr. 1, (3) AO",
    },
    "receipts": {
        "archive": True,
        "years": 9,
        "clock": "document_year",
        "restricted": False,
        "basis": "§ 147 (1) Nr. 4, (3) AO",
    },
    "eu-grant": {
        "archive": True,
        "clock": "grant",
        "restricted": False,
        "basis": "Grant agreement",
    },
    "hr-contract": {
        "archive": True,
        "years": 11,
        "clock": "employment_end",
        "restricted": True,
        "basis": "§ 195 BGB",
    },
    "hr-pension": {
        "archive": True,
        "years": 30,
        "clock": "employment_end",
        "restricted": True,
        "basis": "§ 18a BetrAVG",
    },
    "hr-file": {
        "archive": False,
        "clock": "none",
        "restricted": True,
        "basis": "No statutory retention basis. Art. 17 GDPR applies",
    },
}


def config_dict(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """The rendered config an entity's deployment would produce."""
    raw: dict[str, Any] = {
        "entity": "crs",
        "display_name": "Example GmbH",
        "hostname": "archive-crs.example.com",
        "data_dir": str(tmp_path / "data"),
        "metrics_dir": str(tmp_path / "metrics"),
        "state_root": str(tmp_path / "state"),
        "compose_dir": str(tmp_path / "compose"),
        "container_media_root": "/usr/src/paperless/media",
        "container_export_root": "/usr/src/paperless/export",
        "api_base": "http://paperless.invalid/api",
        "bucket": "example-archive-crs",
        # moto only intercepts endpoints it recognises as S3, so the tests use
        # an AWS-shaped one. Production points at B2; nothing in this package is
        # B2-specific beyond the value of this field.
        "endpoint": "https://s3.us-east-1.amazonaws.com",
        "region": "us-east-1",
        "object_lock_mode": "COMPLIANCE",
        "burn_in_retain_days": 7,
        "export_retain_years": 11,
        "retention_classes": RETENTION_CLASSES,
        "grants": {"futura": {"final_payment_year": 2030, "years": 5}},
        "employments": {"alex": {"end_year": None}, "sam": {"end_year": 2029}},
        "public_enabled": False,
        "public_until": "",
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """An entity in COMPLIANCE mode, which is where the real dates apply."""
    return Config.model_validate(config_dict(tmp_path))


@pytest.fixture
def burn_in_cfg(tmp_path: Path) -> Config:
    """The same entity during the GOVERNANCE burn-in."""
    return Config.model_validate(config_dict(tmp_path, object_lock_mode="GOVERNANCE"))


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """A config written to disk, for the loader and the CLI."""
    path = tmp_path / "archive.json"
    path.write_text(json.dumps(config_dict(tmp_path)), encoding="utf-8")
    return path


@pytest.fixture
def b2_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both roles' credentials, as the units would supply them."""
    for role in ("WRITER", "READER"):
        monkeypatch.setenv(f"PAPERLESS_ARCHIVE_{role}_KEY_ID", f"test-{role.lower()}-id")
        monkeypatch.setenv(f"PAPERLESS_ARCHIVE_{role}_KEY", f"test-{role.lower()}-key")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def locked_bucket(cfg: Config, b2_credentials: None):
    """A moto S3 bucket with Object Lock enabled, as Terraform creates it."""
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=cfg.bucket, ObjectLockEnabledForBucket=True)
        yield client


def utc(year: int, month: int = 1, day: int = 1) -> dt.datetime:
    """A UTC datetime, for comparing against computed retain-until dates."""
    return dt.datetime(year, month, day, tzinfo=dt.UTC)
