"""
The archive of record: an S3-compatible bucket with Object Lock

No provider is named anywhere in this module. Six calls are made -- `put_object`,
`head_object`, `get_object`, `put_object_retention`, `list_objects_v2` and a
managed download -- and all six are the S3 API rather than any vendor's own.
Integrity comes from a SHA-256 this program computes and stores, never from
`ETag`, so nothing depends on how a particular implementation derives one.

Keeping to that is not tidiness. The retain-until dates written here run for a
decade, which is longer than any assumption about who is storing the bytes, so
the archive has to be readable with an ordinary S3 client by somebody who has
never heard of this program.
"""

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from paperless_s3_archiver.config import Config


class MissingCredentials(Exception):
    """No credentials in the environment for the role a job asked for."""


def content_md5(body: bytes) -> str:
    """
    The base64 MD5 of a payload, for the S3 `Content-MD5` header

    Parameters
    ----------
    body
        The bytes about to be uploaded.

    Returns
    -------
    :
        The digest, base64-encoded as the header wants it.
    """
    # Not a security property, and not this archive's integrity claim. S3
    # requires an integrity header on a locked PUT and this is the form every
    # implementation accepts; the claim that matters is the SHA-256 stored
    # alongside the object.
    digest = hashlib.md5(body, usedforsecurity=False).digest()
    return base64.b64encode(digest).decode("ascii")


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
        raise MissingCredentials(f"No {role} credentials in the environment for {cfg.entity}")
    return boto3.client(
        "s3",
        endpoint_url=cfg.endpoint,
        region_name=cfg.region,
        aws_access_key_id=key_id,
        aws_secret_access_key=key,
        config=BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            # Plain PUTs, with no AWS-chunked framing and no trailing CRC32.
            #
            # botocore >= 1.36 defaults these to "when_supported", which adds
            # `Content-Encoding: aws-chunked` and `x-amz-sdk-checksum-algorithm`
            # to every upload. That is an AWS wire format, not an S3 API, and
            # implementations vary in whether they accept it -- and in whether
            # they store the encoding header as object metadata, which would
            # follow the object for as long as it is locked.
            #
            # Nothing is lost by turning it off. Every object's integrity is
            # established by the SHA-256 this program computes, records in the
            # object's metadata, and repeats in the sidecar, which is verifiable
            # by anyone with the bytes and no knowledge of this program at all.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
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
        # S3 refuses a PUT that carries Object Lock retention unless it also
        # carries an integrity header, which is the right rule for an object
        # that cannot be corrected once written.
        #
        # Content-MD5 rather than the SDK's newer checksum trailer: it is part
        # of the original S3 API, every implementation accepts it, and it does
        # not pull in the AWS-chunked framing that `request_checksum_calculation`
        # is turned down to avoid. It is a transport check and nothing more --
        # this archive's integrity claim is the SHA-256 in the object's metadata
        # and its sidecar, so do not "upgrade" this expecting it to be load
        # bearing.
        "ContentMD5": content_md5(body),
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
