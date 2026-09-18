Initial release. Extracted from the `paperless` Ansible role in
`climate-resource/infrastructure`, where it was a single 1551-line script with
no tests, and split into modules with a suite covering the retention arithmetic,
the export filter and the Object Lock behaviour.

Three changes beyond the move: the config is validated on load rather than
trusted; the two places that shell out to `docker` sit behind a `Runtime`
interface so a second deployment shape is an implementation rather than a
rewrite; and uploads no longer carry AWS-chunked framing or a trailing CRC32,
which are an AWS wire format rather than an S3 API and are not accepted
uniformly by S3-compatible stores.
