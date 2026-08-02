import hashlib
import json
import logging

import requests
from django.conf import settings
from django.core.cache import cache

from .request_context import get_request_id


logger = logging.getLogger(__name__)
SENSITIVE_PARTS = ("password", "secret", "token", "authorization", "api_key")


def _redact(value):
    if isinstance(value, dict):
        return {
            key: (
                "[redacted]"
                if any(part in key.lower() for part in SENSITIVE_PARTS)
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def notify(event: str, details: dict, *, dedupe_seconds=900) -> bool:
    url = getattr(settings, "ALERT_WEBHOOK_URL", "")
    if not url:
        return False
    safe_details = _redact(details)
    digest = hashlib.sha256(
        json.dumps([event, safe_details], sort_keys=True, default=str).encode()
    ).hexdigest()
    if not cache.add(f"alert:{digest}", True, timeout=dedupe_seconds):
        return False
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
        return True
    except Exception:
        logger.exception("Alert webhook failed for %s", event)
        return False
