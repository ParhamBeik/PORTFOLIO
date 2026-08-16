"""Shared Redis client.

Used for distributed locks (the live-fetch lock, archive claim leases, market-state
probes) and nothing else. This was `pubsub.py` back when a Celery task broadcast the
price map to SSE subscribers; the frontend never subscribed, so the publish/subscribe
half was removed and only the client accessor remains.
"""
import os

import redis

_client = None


def get_redis():
    """Return a shared Redis client, or None when REDIS_URL is unset.

    Returning None (instead of raising) lets callers degrade gracefully in
    environments without Redis (tests, local dev).
    """
    global _client
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    if _client is None:
        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client
