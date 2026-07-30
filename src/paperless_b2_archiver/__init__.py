"""
Write paperless-ngx documents to a WORM object store under Object Lock

Paperless-ngx is the index and the UI. It is not the archive of record: that is
the object store this package writes to. If paperless is lost, corrupted, or a
document is deleted from it, the record still exists, byte-identical, with a
server-side retain-until date that nobody -- including the account owner holding
the master key -- can shorten.

The retention period is computed per document from the entity's own class table,
so the archive holds each record exactly as long as the law or the agreement it
falls under requires, and no longer.
"""

import importlib.metadata

__version__ = importlib.metadata.version("paperless-b2-archiver")
