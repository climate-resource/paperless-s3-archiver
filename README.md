# paperless-s3-archiver

Write [paperless-ngx](https://docs.paperless-ngx.com/) documents to a WORM object
store under Object Lock, with a statutory retention period computed for each
document from the class it belongs to.

Using this project, you can set up paperless-ngx as the index and the UI only.
It does not need to be the archive of record: that
is the bucket this program writes to. If paperless is lost, corrupted, or a
document is deleted from it, the record still exists, with a
server-side retain-until date that nobody — including the account owner holding
the master key — can shorten.

Built for a German *revisionssichere Dokumentenablage* (GoBD, § 147 AO), but
nothing here is Germany-specific: the retention table is configuration, and the
periods, their legal basis and the clock each one runs on come from the
deployment rather than from this code.

## What the object store has to support

Any S3-compatible store with Object Lock. It is developed against Backblaze B2
and tested against an AWS S3 emulation, and it names no provider anywhere in the
code. Six S3 calls are made — `put_object`, `head_object`, `get_object`,
`put_object_retention`, `list_objects_v2` and a managed download — so what your
store needs is:

- **Object Lock, enabled at bucket creation.** It cannot be retrofitted on any
  implementation, and on AWS S3 it also requires bucket versioning.
- **Per-object retention**, set on `PUT`. This program never sets a bucket
  default, so a mistake in one class cannot silently apply to everything.
- **`COMPLIANCE` mode**, if you want the guarantee that is the point of the
  exercise. `GOVERNANCE` is for the burn-in.
- **Legal holds**, if you use the `employment_end` clock. On B2 that needs the
  `writeFileLegalHolds` capability on the writer key.
- **Two credentials scoped to the one bucket**, one write-only and one
  read-only. Neither should be able to delete or to bypass governance.

Uploads are deliberately plain: AWS-chunked framing and trailing CRC32
checksums are turned off, because they are an AWS wire format rather than an S3
API and implementations differ on whether they accept the first and store the
second as object metadata. Integrity is established instead by a SHA-256 this
program computes, writes into the object's metadata and repeats in the sidecar
— verifiable by anyone holding the bytes, with no knowledge of this program.

Note this is not audited or certified in any way. It is your responsibility to
ensure that your use of this software meets your legal requirements, if you
decide to use it.

## The shape of it

```
upload ──▶ paperless-ngx (index, OCR, search, UI)      MUTABLE, restorable
               │
               │ post-consume hook writes a job file to the spool
               ▼
         paperless-archive tap  (holds the object-store keys)
               │
               ▼
         bucket, Object Lock                           ARCHIVE OF RECORD
```

The in-container hook holds no credentials, opens no network connection and
makes no retention decision; it records that a document was consumed and stops.
Everything that can write to the bucket runs outside paperless, so a flaw in the
application has no path to the archive.

## Install

```bash
uv tool install paperless-s3-archiver
```

Or run it without installing:

```bash
uvx paperless-s3-archiver --config /etc/paperless/crs/archive.json validate
```

A container image is published alongside each release:

```bash
docker run --rm ghcr.io/climate-resource/paperless-s3-archiver:latest --help
```

## Configure

One JSON file describes one legal entity. Everything that differs between
entities lives there, so a second entity is a configuration change rather than a
fork. Point at it with `--config` or `PAPERLESS_ARCHIVE_CONFIG`.

```json
{
  "entity": "example",
  "display_name": "Example GmbH",
  "hostname": "archive.example.com",
  "data_dir": "/data/paperless/example",
  "metrics_dir": "/var/lib/node_exporter/textfile",
  "container_media_root": "/usr/src/paperless/media",
  "container_export_root": "/usr/src/paperless/export",
  "api_base": "http://127.0.0.1:8101/api",
  "bucket": "example-archive",
  "endpoint": "https://s3.eu-central-003.backblazeb2.com",
  "region": "eu-central-003",
  "object_lock_mode": "GOVERNANCE",
  "burn_in_retain_days": 7,
  "export_retain_years": 11,
  "retention_classes": {
    "receipts": {
      "archive": true,
      "years": 9,
      "clock": "document_year",
      "basis": "§ 147 (1) Nr. 4, (3) AO — 8 years since BEG IV, + 1 margin"
    },
    "eu-grant": {
      "archive": true,
      "clock": "grant",
      "basis": "Grant agreement, typically N years after final payment"
    },
    "hr-file": {
      "archive": false,
      "clock": "none",
      "restricted": true,
      "basis": "No statutory retention basis. Art. 5 (1) (e), Art. 17 GDPR apply"
    }
  },
  "grants": {
    "futura": { "final_payment_year": 2030, "years": 5 }
  },
  "employments": {},
  "public_enabled": false,
  "public_until": ""
}
```

Credentials never appear in the config. They come from the environment:

| Variable | What it is |
| --- | --- |
| `PAPERLESS_ARCHIVE_WRITER_KEY_ID` / `_KEY` | Bucket-scoped key that may write and lock, but not delete |
| `PAPERLESS_ARCHIVE_READER_KEY_ID` / `_KEY` | Bucket-scoped key that may only read |
| `PAPERLESS_ARCHIVE_API_TOKEN` | Paperless DRF token for the archive jobs |

The two object-store roles are separate credentials, not one key used twice.
Neither holds `deleteFiles` or `bypassGovernance`.

## Retention classes

A document carries exactly one `class:<name>` tag. The class decides the
retain-until date **and whether the document is archived at all**. No class, or
two, is a refusal: the document stays in paperless until a human classifies it.

Each class names a `clock`, which is the year the period is measured from:

| `clock` | The period runs from | Also needs |
| --- | --- | --- |
| `document_year` | the document's own year | `years` |
| `grant` | the grant's final payment year | a `grant:<slug>` tag, and that slug in `grants` |
| `employment_end` | the year an employment ended | an `employment:<person>` tag, and that slug in `employments` |
| `none` | nothing; the class is never archived | — |

The retain-until date is always 31 December of `(clock year + years)`, never
"now plus N" — § 147 (4) AO starts the clock at the end of the calendar year of
the last entry.

A `grant:<slug>` tag is also a **floor** under every other clock. A subcontractor
invoice charged to a project is filed as `class:receipts` and tagged
`grant:futura`, and is held until the later of the two dates. Nobody has to work
out which period runs longer, and the sidecar's explanation names both bases. The
slug must be in `grants` whatever the class: an unknown slug is a refusal, so a
typo costs a re-tag rather than the grant period. `class:eu-grant` remains the
class for a document whose only basis is the agreement.

An `employment_end` document whose employment has not ended is archived under a
**legal hold**, with a floor retain-until computed from the document year. That
is what stops a contract signed in 2027 for an employment ending in 2045 from
unlocking in 2038. Setting `end_year` later replaces the hold with a real date.

`archive: false` means indexed and searchable but never written to the bucket,
which is what keeps material you may be obliged to erase under Art. 17 GDPR
erasable.

## Commands

```bash
paperless-archive validate      # check the config before it can write anything permanent
paperless-archive tap           # archive every document waiting in the spool
paperless-archive export        # export, filter and upload the nightly snapshot
paperless-archive inventory     # write a dated inventory of the bucket into the bucket
paperless-archive reconcile     # compare paperless against the bucket, both directions
paperless-archive metrics       # exposure, break-glass logins, liveness
paperless-archive fetch-export DIR    # download the latest export, for a restore drill
paperless-archive auditor-export DIR  # build a Z3 handover for one engagement
paperless-archive extend-retention    # move retain-until out where the config says longer
```

Every job writes Prometheus metrics to `metrics_dir` in node_exporter textfile
format, including on its failure path — a job that fails silently and a job that
never ran look identical to an alert rule otherwise. Every metric carries an
`entity` label, so a healthy instance cannot mask a broken one.

### Burn-in, then COMPLIANCE

`object_lock_mode` starts at `GOVERNANCE`. Everything written during burn-in is
**disposable**: objects carry a short retention, no legal holds are applied, and
`extend-retention --apply` refuses to run. Nothing counts as archived until it is
re-ingested under `COMPLIANCE` with real dates.

### When a period grows

A period can change after the objects were written — a grant extension moves the
final payment year, or the law changes, as BEG IV did to Buchungsbelege. Fix the
config first, then let the bucket catch up:

```bash
paperless-archive extend-retention                       # dry run, writes nothing
paperless-archive extend-retention --grant futura        # narrow it
paperless-archive extend-retention --apply --reason "FUTURA amendment 3: end moved to 2031"
```

It is declarative and safe to re-run: it recomputes every archived document's
date from the config, compares it with what the object carries, and only ever
moves a date **later**. Shortening is not offered and is never attempted. It
reads the sidecars rather than paperless, so it still works years later on a host
where the application no longer runs. Every applied run leaves a locked, dated
record in the bucket under `retention-extensions/`.

## Development

```bash
make virtual-environment   # uv sync + pre-commit install
make test                  # pytest
make checks                # ruff, ty, pylic, pre-commit
```

Changes go through a `changelog/<pr>.<type>.md` fragment; `towncrier` assembles
`CHANGELOG.md` at release time. A change to how a retain-until date is computed
is always `breaking`, however small the diff: dates written under the old rule
cannot be corrected downwards afterwards.

## Licence

Apache-2.0. See [LICENCE](LICENCE).
