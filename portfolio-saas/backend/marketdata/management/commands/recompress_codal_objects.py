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

from django.conf import settings
from django.core.management.base import BaseCommand

from marketdata.codal_storage import _client
from marketdata.models import CodalArtifact

_BATCH = 500


class Command(BaseCommand):
    help = "Re-put stored Codal objects so MinIO compression applies to them."

    def add_arguments(self, parser):
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--limit", type=int, default=None)

    def handle(self, *args, after_id: int = 0, limit: int | None = None, **options):
        client = _client()
        counts = {"rewritten": 0, "missing": 0, "checksum_mismatch": 0, "last_id": after_id}
        done = 0
        while limit is None or done < limit:
            # Short queries, not one long cursor: S3 calls between rows can outlast
            # Postgres' idle_session_timeout and kill a held-open iterator.
            batch = list(
                CodalArtifact.objects.filter(pk__gt=counts["last_id"], fetch_status="stored")
                .exclude(s3_key="").order_by("pk")
                .values_list("pk", "s3_key", "checksum_sha256", "content_type")[:_BATCH])
            if not batch:
                break
            for pk, key, checksum, content_type in batch[: (limit - done) if limit else None]:
                done += 1
                counts["last_id"] = pk
                self._rewrite(client, key, checksum, content_type, counts)
            self.stdout.write(json.dumps(counts))
            self.stdout.flush()

    def _rewrite(self, client, key, checksum, content_type, counts):
        try:
            body = client.get_object(Bucket=settings.CODAL_S3_BUCKET, Key=key)["Body"].read()
        except client.exceptions.NoSuchKey:
            counts["missing"] += 1
            return
        if checksum and hashlib.sha256(body).hexdigest() != checksum:
            counts["checksum_mismatch"] += 1
            return
        client.put_object(Bucket=settings.CODAL_S3_BUCKET, Key=key, Body=body,
                          ContentType=content_type or "application/octet-stream")
        counts["rewritten"] += 1
