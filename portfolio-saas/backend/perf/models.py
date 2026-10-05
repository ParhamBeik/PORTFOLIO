"""Hourly latency rollups for API requests and client-observed page loads.

One row per (hour, source, route, method, market state). Rows hold *totals*
for that hour, rewritten idempotently by `perf.tasks.flush_perf_rollups` from
the Redis counters the middleware and client-ingest view increment. Nothing is
stored per request: a per-request table would grow without bound, and the
storage policy forbids retention deletes. At ~100 routes this is a few hundred
small rows per hour of traffic -- megabytes per year.

Latency percentiles come from `hist`, a fixed-bucket histogram whose upper
bounds are `perf.recorder.BUCKETS_MS`. Changing those bounds changes what
every stored histogram means, so append a bucket rather than editing one.
"""
from django.db import models


class RequestPerfRollup(models.Model):
    class Source(models.TextChoices):
        API = "api", "API request (server-measured)"
        CLIENT_API = "client_api", "API call (browser-measured)"
        CLIENT_PAGE = "client_page", "Page ready (browser-measured)"
        CLIENT_BOOT = "client_boot", "App boot (browser-measured)"

    bucket = models.DateTimeField(help_text="UTC start of the hour.")
    source = models.CharField(max_length=16, choices=Source.choices)
    route = models.CharField(max_length=160)
    method = models.CharField(max_length=8, default="")
    market_state = models.CharField(max_length=16, default="")

    count = models.PositiveIntegerField(default=0)
    errors = models.PositiveIntegerField(default=0, help_text="5xx / failed")
    client_errors = models.PositiveIntegerField(default=0, help_text="4xx")
    sum_ms = models.FloatField(default=0)
    max_ms = models.FloatField(default=0)
    sum_db_queries = models.BigIntegerField(default=0)
    sum_db_ms = models.FloatField(default=0)
    sum_inflight = models.BigIntegerField(default=0)
    hist = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["bucket", "source", "route", "method", "market_state"],
                name="uniq_perf_rollup_dims",
            )
        ]
        indexes = [models.Index(fields=["source", "bucket"])]
        ordering = ["-bucket"]
