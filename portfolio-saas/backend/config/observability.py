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
    return (getattr(settings, "ALERT_TELEGRAM_API_BASE", "") or "https://tapi.bale.ai").rstrip("/")


def _gb(value):
    return f"{(value or 0) / 1e9:.1f} GB"


def _num(value):
    return f"{value:,}" if isinstance(value, int) else str(value)


def _minutes(seconds):
    if seconds is None:
        return "ever (no price stored)"
    minutes = int(seconds // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes // 60} h {minutes % 60} min"


def _causes(causes):
    return ", ".join(f"{count} {cause.rstrip('.')}" for cause, count in (causes or {}).items())


# One line a person can act on, per event. Raw details used to go out as a JSON
# blob (`{"codal_bytes": 22064086717, ...}`), which nobody reads on a phone.
# An event missing here still sends: title from its name, details as key: value.
ALERT_TEXT = {
    "stale-prices": ("Prices frozen", lambda d: f"No new price for {_minutes(d.get('age_seconds'))} while the market is open."),
    "disk-projection": ("Disk filling up", lambda d: (
        f"80% full in ~{d.get('days_to_80pct')} days. "
        f"{_gb(d.get('filesystem_free_bytes'))} free of {_gb(d.get('filesystem_total_bytes'))}, "
        f"growing {_gb(d.get('growth_bytes_per_day'))}/day.")),
    "quota-exhausted-early": ("Quota ran out mid-session", lambda d: (
        f"{d.get('plan')} wallet: {_num(d.get('used'))} of {_num(d.get('limit'))} used. "
        "Live prices on it stop until Tehran midnight.")),
    "queue-backlog": ("Task queues backing up", lambda d: " · ".join(f"{q} {_num(n)}" for q, n in d.items())),
    "wedged-archive-states": ("Archive jobs stuck", lambda d: (
        f"{_num(d.get('count'))} jobs keep failing: {_causes(d.get('causes'))}.")),
    "stale-archive-progress": ("Archive stalled", lambda d: (
        f"No progress in {_minutes(d.get('stale_seconds'))} with {_num(d.get('archive_capacity'))} requests of quota left.")),
    "failed-integrity-assessments": ("Data-quality gate", lambda d: (
        f"{_num(d.get('failed'))} of {_num(d.get('assessed'))} symbols fail ({round((d.get('rate') or 0) * 100)}%).")),
    "elevated-workflow-failure-rate": ("Background jobs failing", lambda d: (
        f"{_num(d.get('failed'))} of {_num(d.get('total'))} runs failed in the last hour ({round((d.get('rate') or 0) * 100)}%).")),
    "quota-ledger-drift": ("Quota counters disagree", lambda d: (
        f"{d.get('plan')}: {_num(d.get('local_attempts'))} requests counted, lanes add up to {_num(d.get('bucket_total'))}.")),
    "quota-provider-variance-rising": ("Provider counts more than we do", lambda d: (
        f"{d.get('plan')}: provider {_num(d.get('provider_used'))}, ours {_num(d.get('local_attempts'))} (gap {_num(d.get('variance'))}).")),
}


def format_alert(event, details):
    """(title, one-line summary) for a person, never raises."""
    title, render = ALERT_TEXT.get(event, (event.replace("-", " ").capitalize(), None))
    try:
        if render is not None:
            return title, render(details or {})
    except Exception:
        logger.warning("could not format alert %s", event)
    text = "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in (details or {}).items()) if isinstance(details, dict) else str(details)
    return title, text


def _send_text(token, chat_id, text):
    # Plain text, no parse_mode: payloads carry Persian symbol names, and an
    # unbalanced `_` under Markdown makes the bot API reject the whole message --
    # an alert that fails because of its own contents is the worst failure here.
    requests.post(
        f"{_telegram_api_base()}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4096], "disable_web_page_preview": True},
        timeout=5,
    ).raise_for_status()


def _send_telegram(token, chat_id, event, safe_details):
    title, text = format_alert(event, safe_details)
    _send_text(token, chat_id, f"🔴 {title}\n{text}")


# The console's "Recent alerts" list. A raw Redis list on the cache instance
# (REDIS_URL, docker-compose.prod.yml DB 1), not a Django cache key, so the host
# scripts can LPUSH to it with redis-cli when the app itself is down
# (scripts/host_alert.py writes the same record shape to the same key). Capped:
# it is a view of the last few alerts, never a log -- the stdout line is the log.
RECENT_ALERTS_KEY = "ops:recent_alerts"
RECENT_ALERTS_MAX = 50


def _alert_redis():
    from portfolio.live.redis_client import get_redis

    return get_redis()


def record_alert(event, safe_details, *, delivered, source="app", route="now"):
    """Keep one alert in the capped console list. Best-effort: never raises."""
    from django.utils import timezone

    try:
        client = _alert_redis()
        if client is None:
            return
        record = json.dumps(
            {
                "at": timezone.now().isoformat(),
                "event": event,
                "source": source,
                "delivered": bool(delivered),
                "route": route,
                "title": format_alert(event, safe_details)[0],
                "text": format_alert(event, safe_details)[1][:300],
                "details": json.dumps(safe_details, sort_keys=True, default=str, ensure_ascii=False)[:500],
            },
            ensure_ascii=False,
        )
        pipe = client.pipeline()
        pipe.lpush(RECENT_ALERTS_KEY, record)
        pipe.ltrim(RECENT_ALERTS_KEY, 0, RECENT_ALERTS_MAX - 1)
        pipe.execute()
    except Exception:
        logger.warning("could not record alert %s for the ops console", event)


def recent_alerts():
    """Newest first. A malformed entry is skipped rather than failing the page."""
    try:
        client = _alert_redis()
        raw = [] if client is None else client.lrange(RECENT_ALERTS_KEY, 0, RECENT_ALERTS_MAX - 1)
    except Exception:
        logger.warning("could not read recent alerts for the ops console")
        return []
    alerts = []
    for item in raw:
        try:
            row = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(row, dict) and row.get("event"):
            alerts.append(row)
    return alerts


def notify(event, details, *, dedupe_seconds=900, dedupe_key=None, urgent=True):
    """Raise one operator alert: always to stdout, plus every configured channel.

    `urgent=False` sends nothing now: the alert joins the once-a-day summary
    (`send_alert_digest`). Only something a person must act on within the hour
    should interrupt them; everything else firing every 15 minutes is how 583
    alerts in 72h trained everyone to ignore the channel.

    Dedupe keys on (event, details) unless `dedupe_key` is given. A standing
    condition whose details drift every run (disk days-to-80%, a wedged-state
    count) must pass one: keyed on its details it never repeats, so it re-sent
    every quarter hour -- 288 disk-projection and 288 wedged-archive-states
    alerts in the 72h to 2026-10-11.

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
    identity = [event, dedupe_key] if dedupe_key is not None else [event, safe_details]
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    # The summary and an interruption reserve separately: a condition queued for
    # the digest at 08:15 (market shut) must still interrupt at the 08:30 open.
    cache_key = f"alert:{digest}" if urgent else f"alert:digest:{digest}"
    try:
        reserved = cache.add(cache_key, True, timeout=dedupe_seconds)
    except Exception:
        reserved = True
    if not urgent:
        logger.warning("alert:%s digest=1 %s", event, payload)
        queue_for_digest(event, safe_details)
        if reserved:
            record_alert(event, safe_details, delivered=False, route="digest")
        return False
    if not (url or telegram):
        # On 2026-08-27 the archive was dead for 13h having raised
        # stale-archive-progress 49 times, and every line looked delivered --
        # no channel was configured. `undelivered=1` is the greppable difference.
        # The log line stays unconditional; the console list takes the deduped
        # view so one standing condition cannot push everything else off it.
        logger.warning("alert:%s undelivered=1 %s", event, payload)
        if reserved:
            record_alert(event, safe_details, delivered=False)
        return False
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
    record_alert(event, safe_details, delivered=delivered)
    return delivered


# Non-urgent alerts collect here until the daily summary. One hash field per
# event, so a condition that fired 96 times yesterday is one line with a count.
DIGEST_KEY = "ops:alert_digest"
DIGEST_SENDING_KEY = "ops:alert_digest:sending"


def queue_for_digest(event, safe_details):
    """Best-effort: never raises."""
    from django.utils import timezone

    try:
        client = _alert_redis()
        if client is None:
            return
        now = timezone.now().isoformat()
        previous = client.hget(DIGEST_KEY, event)
        entry = json.loads(previous) if previous else {"count": 0, "first": now}
        entry.update(count=entry["count"] + 1, last=now, details=safe_details)
        client.hset(DIGEST_KEY, event, json.dumps(entry, default=str, ensure_ascii=False))
    except Exception:
        logger.warning("could not queue alert %s for the daily summary", event)


def build_digest_text(entries):
    """The one daily message. `entries` maps event -> {count, details}."""
    lines = ["📋 Daily summary (nothing here needed you right away)"]
    for event, entry in sorted(entries.items(), key=lambda item: -item[1].get("count", 0)):
        title, text = format_alert(event, entry.get("details") or {})
        count = entry.get("count", 1)
        times = f" ×{count}" if count > 1 else ""
        lines.append(f"\n• {title}{times}\n  {text}")
    return "\n".join(lines)


def send_alert_digest():
    """Send and clear the summary. Returns True when something was delivered.

    Cleared only after a successful send, so a failed day rolls into the next
    one instead of disappearing.
    """
    # Swap the hash out before reading it: the health check runs on the same
    # queue at the same minute, and anything it queues after a plain read would
    # be erased by the delete without ever being sent.
    try:
        client = _alert_redis()
        if client is None:
            return False
        try:
            client.rename(DIGEST_KEY, DIGEST_SENDING_KEY)
        except Exception:
            return False  # nothing queued
        raw = client.hgetall(DIGEST_SENDING_KEY)
    except Exception:
        logger.warning("could not read the alert summary")
        return False
    entries = {}
    for key, value in (raw or {}).items():
        try:
            entries[key.decode() if isinstance(key, bytes) else key] = json.loads(value)
        except (TypeError, ValueError):
            continue
    if not entries:
        return False
    text = build_digest_text(entries)
    url = getattr(settings, "ALERT_WEBHOOK_URL", "")
    telegram = _telegram_target()
    delivered = False
    if url:
        try:
            requests.post(url, json={"event": "daily-digest", "details": entries}, timeout=5).raise_for_status()
            delivered = True
        except Exception:
            logger.exception("Alert webhook failed for daily-digest")
    if telegram:
        try:
            _send_text(*telegram, text)
            delivered = True
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error("Alert telegram failed for daily-digest status=%s err=%s", status, type(exc).__name__)
    logger.warning("alert:daily-digest undelivered=%d events=%d", 0 if delivered else 1, len(entries))
    try:
        if not delivered:
            _merge_back(client, entries)
        client.delete(DIGEST_SENDING_KEY)
    except Exception:
        logger.warning("could not clear the alert summary")
    return delivered


def _merge_back(client, entries):
    """A failed day rolls into the next one, counted with whatever arrived since."""
    for event, entry in entries.items():
        newer = client.hget(DIGEST_KEY, event)
        if newer:
            newer = json.loads(newer)
            newer.update(count=newer.get("count", 0) + entry.get("count", 0), first=entry.get("first", newer.get("first")))
            entry = newer
        client.hset(DIGEST_KEY, event, json.dumps(entry, default=str, ensure_ascii=False))
