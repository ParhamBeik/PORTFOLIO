"""Rewrite stored Codal objects so MinIO compresses them (docs/STORAGE-POLICY.md).

MinIO compresses on write only, so objects stored before compression was enabled
stay full size until rewritten. Each object is read, checked against the
`checksum_sha256` on its row, and written back under the same key: identical
bytes to every reader, smaller on disk. A checksum mismatch is left untouched.

    manage.py recompress_codal_objects --after-id 0 --limit 1000
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.management.base import BaseCommand

from marketdata.codal_storage import _client
from marketdata.models import CodalArtifact

_BATCH = 500
_WORKERS = 8  # each object is mostly waiting on MinIO, not CPU


class Command(BaseCommand):
    help = "Re-put stored Codal objects so MinIO compression applies to them."

    def add_arguments(self, parser):
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, after_id: int = 0, limit: int | None = None, **options):
        client = _client()
        counts = Counter(rewritten=0, missing=0, checksum_mismatch=0)
        last_id, done = after_id, 0
        with ThreadPoolExecutor(_WORKERS) as pool:
            while limit is None or done < limit:
                # Short queries, not one long cursor: S3 calls between rows can outlast
                # Postgres' idle_session_timeout and kill a held-open iterator.
                batch = list(
                    CodalArtifact.objects.filter(pk__gt=last_id, fetch_status="stored")
                    .exclude(s3_key="").order_by("pk")
                    .values_list("pk", "s3_key", "checksum_sha256", "content_type")[:_BATCH])
                if limit is not None:
                    batch = batch[: limit - done]
                if not batch:
                    break
                counts.update(pool.map(lambda row: _rewrite(client, *row[1:]), batch))
                done += len(batch)
                last_id = batch[-1][0]
                self.stdout.write(json.dumps({**counts, "last_id": last_id}))
                self.stdout.flush()


def _rewrite(client, key, checksum, content_type) -> str:
    try:
        body = client.get_object(Bucket=settings.CODAL_S3_BUCKET, Key=key)["Body"].read()
    except client.exceptions.NoSuchKey:
        return "missing"
    if checksum and hashlib.sha256(body).hexdigest() != checksum:
        return "checksum_mismatch"
    client.put_object(Bucket=settings.CODAL_S3_BUCKET, Key=key, Body=body,
                      ContentType=content_type or "application/octet-stream")
    return "rewritten"
