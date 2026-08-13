"""Compact terminal workflow outcomes backed by a queryable 30-day ledger."""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from celery import current_task

logger = logging.getLogger("workflow")
http_attempt_var = ContextVar("workflow_http_attempts", default=0)
quota_attempt_var = ContextVar("workflow_quota_attempts", default=0)
correlation_id_var = ContextVar("workflow_correlation_id", default="")


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


def _task_id():
    request = getattr(current_task, "request", None)
    return getattr(request, "id", "") or ""


def record_http_attempt(*, quota=False):
    http_attempt_var.set(http_attempt_var.get() + 1)
    if quota:
        quota_attempt_var.set(quota_attempt_var.get() + 1)


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
    _http_start: int = field(init=False, repr=False)
    _quota_start: int = field(init=False, repr=False)

    def __post_init__(self):
        self._http_start = http_attempt_var.get()
        self._quota_start = quota_attempt_var.get()
        self._correlation_token = correlation_id_var.set(self.correlation_id)

    def finish(self, outcome, **values):
        from .models import WorkflowRun

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
            "http_attempts": int(values.pop("http_attempts", http_attempt_var.get() - self._http_start) or 0),
            "quota_attempts": int(values.pop("quota_attempts", quota_attempt_var.get() - self._quota_start) or 0),
            "duration_ms": round((time.monotonic() - self.started) * 1000),
            "error_code": str(values.pop("error_code", "") or "")[:80],
            "metadata": redact(values.pop("metadata", values)),
        }
        safe = redact(payload)
        logger.info(json.dumps(safe, ensure_ascii=False, separators=(",", ":"), default=str))
        try:
            return WorkflowRun.objects.create(**safe)
        except Exception:
            logger.exception(
                "workflow_ledger_write_failed correlation_id=%s", self.correlation_id
            )
            return None
        finally:
            token = getattr(self, "_correlation_token", None)
            if token is not None:
                correlation_id_var.reset(token)
