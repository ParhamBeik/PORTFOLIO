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


def _telegram_target():
    """(token, chat_id) when both are configured, else None."""
    token = getattr(settings, "ALERT_TELEGRAM_BOT_TOKEN", "")
    chat_id = getattr(settings, "ALERT_TELEGRAM_CHAT_ID", "")
    return (token, chat_id) if token and chat_id else None


def _telegram_api_base():
    """Telegram-compatible Bot API root.

    api.telegram.org is unreachable from the Iranian VPS, so every alert failed
    with ConnectionError and none was ever delivered. Bale serves the same
    sendMessage contract at https://tapi.bale.ai -- point this there.
    """
    return (getattr(settings, "ALERT_TELEGRAM_API_BASE", "") or "https://api.telegram.org").rstrip("/")


def _send_telegram(token, chat_id, event, safe_details):
    # `text` rather than a parse_mode: alert payloads carry Persian symbol names
    # and JSON punctuation, and Markdown/HTML parsing would make Telegram reject
    # the message for an unbalanced `_` in a symbol -- an alert that fails to
    # send because of the shape of its own contents is the worst failure mode
    # available here.
    body = json.dumps(safe_details, sort_keys=True, default=str, ensure_ascii=False)
    requests.post(
        f"{_telegram_api_base()}/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": f"[{event}] {get_request_id()}\n{body}"[:4096],
            "disable_web_page_preview": True,
        },
        timeout=5,
    ).raise_for_status()


def notify(event, details, *, dedupe_seconds=900):
    """Raise one operator alert: always to stdout, plus every configured channel.

    The stdout line is unconditional because operational_health_check's
    detections (wedged archive states, stale prices, backlog) route only through
    here -- without it an unconfigured channel made the whole mechanism produce
    zero observable output.

    Channels are independent and best-effort: one failing must not suppress the
    other, and neither may raise into the health check that called it.
    """
    from django.core.cache import cache

    safe_details = _redact(details)
    url = getattr(settings, "ALERT_WEBHOOK_URL", "")
    telegram = _telegram_target()
    payload = json.dumps(safe_details, sort_keys=True, default=str)[:500]
    if not (url or telegram):
        # On 2026-08-27 the archive was dead for 13h having raised
        # stale-archive-progress 49 times, and every line looked delivered --
        # no channel was configured. `undelivered=1` is the greppable difference.
        logger.warning("alert:%s undelivered=1 %s", event, payload)
        return False
    digest = hashlib.sha256(
        json.dumps([event, safe_details], sort_keys=True, default=str).encode()
    ).hexdigest()
    cache_key = f"alert:{digest}"
    try:
        reserved = cache.add(cache_key, True, timeout=dedupe_seconds)
    except Exception:
        reserved = True
    if not reserved:
        return False

    delivered = False
    if url:
        try:
            requests.post(
                url,
                json={
                    "event": event,
                    "request_id": get_request_id(),
                    "details": safe_details,
                },
                timeout=5,
            ).raise_for_status()
            delivered = True
        except Exception:
            logger.exception("Alert webhook failed for %s", event)
    if telegram:
        try:
            _send_telegram(*telegram, event, safe_details)
            delivered = True
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error(
                "Alert telegram failed for %s status=%s err=%s",
                event, status, type(exc).__name__,
            )
    if not delivered:
        try:
            cache.delete(cache_key)
        except Exception:
            logger.warning("could not release alert dedupe for %s", event)
    logger.warning("alert:%s undelivered=%d %s", event, 0 if delivered else 1, payload)
    return delivered
