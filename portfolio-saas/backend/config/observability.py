"""Request correlation, structured logging, Sentry and operator alerts.

One request ID is minted per HTTP request (or accepted from `X-Request-ID`),
parked in a ContextVar so every log line, Sentry event, Celery task and alert
raised while handling that request carries the same id, and echoed back on the
response. Celery re-seeds the same ContextVar from a task header (config/celery.py).
"""
import hashlib
import json
import logging
import re
import uuid
from contextvars import ContextVar

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

request_id_var = ContextVar("request_id", default="-")
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def get_request_id():
    return request_id_var.get()


class RequestIDFilter(logging.Filter):
    """Makes `%(request_id)s` available to every log record (see LOGGING)."""

    def filter(self, record):
        record.request_id = get_request_id()
        return True


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get("X-Request-ID", "")
        request_id = incoming if VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        request.request_id = request_id
        if settings.SENTRY_DSN:
            import sentry_sdk

            sentry_sdk.set_tag("request_id", request_id)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)


def init_sentry(dsn, *, environment="production"):
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.error("SENTRY_DSN is set but sentry-sdk is not installed.")
        return False
    sentry_sdk.init(dsn=dsn, environment=environment, send_default_pii=False, traces_sample_rate=0)
    return True


SENSITIVE_PARTS = ("password", "secret", "token", "authorization", "api_key")


def _redact(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(p in key.lower() for p in SENSITIVE_PARTS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def notify(event, details, *, dedupe_seconds=900):
    """Raise one operator alert: always to stdout, to the webhook when configured.

    The stdout line is unconditional because operational_health_check's
    detections (wedged archive states, stale prices, backlog) route only through
    here -- without it an unconfigured ALERT_WEBHOOK_URL made the whole
    mechanism produce zero observable output.
    """
    from django.core.cache import cache

    safe_details = _redact(details)
    logger.warning("alert:%s %s", event, json.dumps(safe_details, sort_keys=True, default=str)[:500])
    url = getattr(settings, "ALERT_WEBHOOK_URL", "")
    if not url:
        return False
    digest = hashlib.sha256(
        json.dumps([event, safe_details], sort_keys=True, default=str).encode()
    ).hexdigest()
    if not cache.add(f"alert:{digest}", True, timeout=dedupe_seconds):
        return False
    try:
        requests.post(
            url,
            json={"event": event, "request_id": get_request_id(), "details": safe_details},
            timeout=5,
        ).raise_for_status()
        return True
    except Exception:
        logger.exception("Alert webhook failed for %s", event)
        return False
