"""Compact terminal workflow outcomes backed by a queryable 30-day ledger."""

import json
import logging
import re
import threading
import time
import uuid
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field
from functools import partial
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from celery import current_task

logger = logging.getLogger("workflow")


class AttemptCounters:
    """Mutable, thread-safe attempt tallies belonging to one workflow.

    Mutable on purpose. `ThreadPoolExecutor` starts each worker in a *fresh*
    context, so the previous design -- two plain-int ContextVars rebound by
    `var.set()` -- threw away every bump made off the calling thread. The live
    price loop fans its provider calls out across a pool, so it recorded 5 HTTP
    attempts on a day it actually spent 488 requests. A shared object mutated in
    place is visible to the parent, provided the context reaches the child at all
    (see `submit_with_context`).
    """

    __slots__ = ("http", "quota", "_closed", "_lock")

    def __init__(self):
        self.http = 0
        self.quota = 0
        self._closed = False
        self._lock = threading.Lock()

    def record(self, *, quota=False):
        # `+= 1` is load/add/store, not atomic. Six pool threads billing the same
        # quota counter is exactly the race that would under-report spend.
        with self._lock:
            if self._closed:
                return
            self.http += 1
            if quota:
                self.quota += 1

    def close(self):
        with self._lock:
            self._closed = True
            return self.http, self.quota


# `None` means "no workflow owns this call". Counting into an ownerless default
# is what the old int vars did, and those numbers went nowhere.
counters_var = ContextVar("workflow_attempt_counters", default=None)
correlation_id_var = ContextVar("workflow_correlation_id", default="")


def submit_with_context(executor, fn, /, *args, **kwargs):
    """`executor.submit` that carries the caller's context into the worker thread.

    Without this the child sees default ContextVars: attempt bumps land in a
    throwaway context and log lines carry an empty correlation id.
    """
    return executor.submit(copy_context().run, partial(fn, *args, **kwargs))


def current_correlation_id() -> str:
    return correlation_id_var.get() or ""

_SECRET_KEY = re.compile(r"(api.?key|secret|password|credential|token|signature)", re.I)
_SIGNED_QUERY = re.compile(r"^(x-amz-|awsaccesskeyid|signature|expires$)", re.I)


def _redact_url(value):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return value
    if not parsed.scheme or not parsed.netloc:
        return value
    host = parsed.hostname or ""
    netloc = host
    if parsed.port:
        netloc += f":{parsed.port}"
    query = [
        (key, "***" if _SIGNED_QUERY.search(key) or _SECRET_KEY.search(key) else val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(query), ""))


def redact(value):
    if isinstance(value, dict):
        return {
            str(key): "***" if _SECRET_KEY.search(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _redact_url(value)
    return value


def _flatten(metadata, limit=500):
    """Render a metadata dict as one line of `key=value` pairs, bounded."""
    if not isinstance(metadata, dict) or not metadata:
        return json.dumps(metadata, sort_keys=True, default=str)[:limit] if metadata else ""
    pairs = []
    for key in sorted(metadata):
        value = metadata[key]
        if not isinstance(value, (str, int, float, bool)) and value is not None:
            value = json.dumps(value, sort_keys=True, default=str)
        pairs.append(f"{key}={' '.join(str(value).split())}")
    return " ".join(pairs)[:limit]


def _task_id():
    request = getattr(current_task, "request", None)
    return getattr(request, "id", "") or ""


def record_http_attempt(*, quota=False):
    counters = counters_var.get()
    if counters is not None:
        counters.record(quota=quota)


@dataclass
class WorkflowOutcome:
    workflow: str
    endpoint: str = ""
    symbol: str = ""
    source: str = ""
    destination_table: str = ""
    task_id: str = ""
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: float = field(default_factory=time.monotonic)
    _counters: AttemptCounters = field(init=False, repr=False)

    def __post_init__(self):
        # A fresh counter per workflow, so tallies are the workflow's own total
        # rather than a delta against whatever ran before it in this context.
        self._counters = AttemptCounters()
        self._counters_token = counters_var.set(self._counters)
        self._correlation_token = correlation_id_var.set(self.correlation_id)

    def finish(self, outcome, **values):
        from .models import WorkflowRun

        counted_http, counted_quota = self._counters.close()
        payload = {
            "workflow": self.workflow,
            "task_id": self.task_id or _task_id(),
            "correlation_id": self.correlation_id,
            "endpoint": self.endpoint,
            "symbol": self.symbol,
            "outcome": outcome,
            "source": self.source,
            "destination_table": self.destination_table,
            "rows_received": int(values.pop("rows_received", 0) or 0),
            "rows_accepted": int(values.pop("rows_accepted", 0) or 0),
            "rows_created": int(values.pop("rows_created", 0) or 0),
            "rows_updated": int(values.pop("rows_updated", 0) or 0),
            "rows_rejected": int(values.pop("rows_rejected", 0) or 0),
            "http_attempts": int(values.pop("http_attempts", counted_http) or 0),
            "quota_attempts": int(values.pop("quota_attempts", counted_quota) or 0),
            "duration_ms": int(
                values.pop("duration_ms", None)
                or round((time.monotonic() - self.started) * 1000)
            ),
            "error_code": str(values.pop("error_code", "") or "")[:80],
            "metadata": redact(values.pop("metadata", values)),
        }
        safe = redact(payload)
        # `source` and `dest` answer "where did this come from, where did it go"
        # without opening the database. Both were already recorded on the ledger
        # row and both were omitted from the line, so the one artifact an
        # operator actually reads -- `docker logs` -- could not answer either
        # question. `cid` ties the line back to its WorkflowRun and to every
        # other log line emitted under the same correlation id.
        summary = (
            f"workflow={safe['workflow']} outcome={safe['outcome']} "
            f"endpoint={safe['endpoint'] or '-'} symbol={safe['symbol'] or '-'} "
            f"source={safe['source'] or '-'} dest={safe['destination_table'] or '-'} "
            f"rows={safe['rows_received']}/{safe['rows_accepted']} "
            f"created={safe['rows_created']} updated={safe['rows_updated']} "
            f"rejected={safe['rows_rejected']} "
            f"attempts={safe['http_attempts']}/{safe['quota_attempts']} "
            f"duration_ms={safe['duration_ms']} error={safe['error_code'] or '-'} "
            f"cid={safe['correlation_id']}"
        )
        # Every metadata key is printed, not just "reason": Codal ingest failures
        # set error_code/artifacts/parse_errors and no reason at all, and used to
        # log no per-stage detail whatsoever -- it existed only in the WorkflowRun
        # row. Rendered as flat `key=value` pairs on the same line, with newlines
        # collapsed, so one workflow is one greppable log line rather than a JSON
        # blob a human has to unpick in `docker compose logs`.
        logger.info(" ".join(filter(None, (summary, _flatten(safe["metadata"])))))
        try:
            return WorkflowRun.objects.create(**safe)
        except Exception:
            logger.exception(
                "workflow_ledger_write_failed correlation_id=%s", self.correlation_id
            )
            return None
        finally:
            token = getattr(self, "_counters_token", None)
            if token is not None:
                counters_var.reset(token)
            token = getattr(self, "_correlation_token", None)
            if token is not None:
                correlation_id_var.reset(token)
