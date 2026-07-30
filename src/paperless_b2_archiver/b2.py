"""
The archive of record: an S3-compatible bucket with Object Lock

Backblaze B2 in production, but nothing here is B2-specific beyond the endpoint
in the config. Object Lock is an S3 API, and keeping to it means the archive can
be read back with any S3 client by somebody who has never heard of this program.
"""

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from paperless_b2_archiver.config import Config


class MissingCredentials(Exception):
    """No credentials in the environment for the role a job asked for."""


def s3_client(cfg: Config, *, role: str) -> Any:
    """
    An S3 client holding the bucket-scoped key for `role`

    Bucket scoping is the storage-layer half of the separation between entities:
    a bug in one entity's tap cannot write into another's bucket, because the
    credential it holds has no reach there.

    The two roles are separate credentials, not one key used twice. The writer
    holds no read capability and the reader no write capability, so a job that
    only enumerates cannot put an object even if a bug asked it to.

    Parameters
    ----------
    cfg
        The entity's config, which names the bucket, endpoint and region.
    role
        ``writer`` or ``reader``. Read from
        ``PAPERLESS_ARCHIVE_<ROLE>_KEY_ID`` and ``PAPERLESS_ARCHIVE_<ROLE>_KEY``.

    Returns
    -------
    :
        A boto3 S3 client.

    Raises
    ------
    MissingCredentials
        When the environment carries no key for that role.
    """
    key_id = os.environ.get(f"PAPERLESS_ARCHIVE_{role.upper()}_KEY_ID", "")
    key = os.environ.get(f"PAPERLESS_ARCHIVE_{role.upper()}_KEY", "")
    if not key_id or not key:
        raise MissingCredentials(f"No B2 {role} credentials in the environment for {cfg.entity}")
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        region_name=cfg.region,
        aws_access_key_id=key_id,
        aws_secret_access_key=key,
        config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"}),
    )


def put_locked(  # noqa: PLR0913 -- every one of these decides what gets written, or for how long
    *,
    client: Any,
    cfg: Config,
    key: str,
    body: bytes,
    retain_until: dt.datetime,
    legal_hold: bool,
    content_type: str,
    metadata: dict[str, str] | None = None,
) -> None:
    """
    PUT one object with its own Object Lock retention

    The retention arguments are keyword-only on purpose. This is the one call in
    the package whose mistakes cannot be taken back, and `retain_until` and
    `legal_hold` next to each other are exactly the pair a reader would have to
    count commas to check.

    Bucket default retention is deliberately not set, so a mistake in one class
    cannot silently apply to everything. Every object carries the date its own
    class computed.

    Parameters
    ----------
    client
        A writer-role S3 client.
    cfg
        The entity's config, which decides GOVERNANCE against COMPLIANCE.
    key
        The object key.
    body
        The bytes to write.
    retain_until
        The computed retain-until date. Ignored during burn-in.
    legal_hold
        Whether to apply a legal hold. Ignored during burn-in, deliberately.
    content_type
        The object's content type.
    metadata
        Optional user metadata.
    """
    kwargs: dict[str, Any] = {
        "Bucket": cfg.bucket,
        "Key": key,
        "Body": body,
        "ContentType": content_type,
    }
    if metadata:
        kwargs["Metadata"] = metadata

    if cfg.burn_in:
        # Burn-in: short GOVERNANCE retention, and deliberately no legal hold.
        # Our writer key holds no capability to release one, so a hold applied
        # now would strand the burn-in bucket permanently -- and the whole point
        # of the burn-in is that everything written during it is disposable.
        kwargs["ObjectLockMode"] = "GOVERNANCE"
        kwargs["ObjectLockRetainUntilDate"] = dt.datetime.now(dt.UTC) + dt.timedelta(
            days=cfg.burn_in_retain_days
        )
    else:
        kwargs["ObjectLockMode"] = "COMPLIANCE"
        kwargs["ObjectLockRetainUntilDate"] = retain_until
        if legal_hold:
            kwargs["ObjectLockLegalHoldStatus"] = "ON"

    client.put_object(**kwargs)


def list_bucket(*, client: Any, cfg: Config) -> list[dict[str, Any]]:
    """
    Every object in the entity's bucket

    Parameters
    ----------
    client
        A reader-role S3 client.
    cfg
        The entity's config.

    Returns
    -------
    :
        One record per object, with key, size, etag and last-modified time.
    """
    objects: list[dict[str, Any]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=cfg.bucket):
        for obj in page.get("Contents", []):
            objects.append(
                {
                    "key": obj["Key"],
                    "size": obj["Size"],
                    "etag": obj["ETag"].strip('"'),
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )
    return objects


def sync_tree(*, cfg: Config, client: Any, root: Path, prefix: str, retain_until: dt.datetime) -> int:
    """
    Upload a directory tree under one flat retain-until date

    Parameters
    ----------
    cfg
        The entity's config.
    client
        A writer-role S3 client.
    root
        The directory to upload. Only files are uploaded; the tree shape is kept
        in the keys.
    prefix
        The key prefix to upload under.
    retain_until
        The date every object in the tree is locked until.

    Returns
    -------
    :
        How many objects were uploaded.
    """
    uploaded = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        key = f"{prefix}{path.relative_to(root).as_posix()}"
        put_locked(
            client=client,
            cfg=cfg,
            key=key,
            body=path.read_bytes(),
            retain_until=retain_until,
            legal_hold=False,
            content_type="application/octet-stream",
        )
        uploaded += 1
    return uploaded


def latest_inventory(*, client: Any, cfg: Config) -> dict[str, Any] | None:
    """
    The most recent inventory the bucket holds

    Parameters
    ----------
    client
        A reader-role S3 client.
    cfg
        The entity's config.

    Returns
    -------
    :
        The parsed inventory, or ``None`` when none has been written yet.
    """
    keys = [o["key"] for o in list_bucket(client=client, cfg=cfg) if o["key"].startswith("inventory/")]
    if not keys:
        return None
    body = client.get_object(Bucket=cfg.bucket, Key=max(keys))["Body"].read()
    parsed: dict[str, Any] = json.loads(body)
    return parsed
