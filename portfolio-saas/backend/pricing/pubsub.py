"""Redis pub/sub channel for the SSE price fan-out.

The channel name lives here so the publisher (Celery task) and the subscribers
(SSE streams) cannot drift apart. A single channel is enough because the price
map is global — every client wants the same bytes.
"""
import os

import redis

CHANNEL = "prices:update"

_client = None


def get_redis():
    """Return a shared Redis client, or None when REDIS_URL is unset.

    Returning None (instead of raising) lets the publish path and the SSE
    heartbeat degrade gracefully in environments without Redis (tests, local dev).
    """
    global _client
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    if _client is None:
        _client = redis.Redis.from_url(url, decode_responses=True)
    return _client
