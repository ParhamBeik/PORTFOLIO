"""Server-Sent Events stream for the global price map.

Prices are global, so a single Redis pub/sub channel fans the same bytes out to
every connected client — the push path is O(1) in user count. Portfolio values
are per-user and are NOT pushed: the client recomputes them locally from the
holdings it already has on each tick.

Auth: EventSource cannot set headers, so the access JWT rides as ?token=. Refresh
tokens are rejected. Concurrent streams per user are capped so one account cannot
hold an unbounded number of long-lived connections.
"""
import json
import time

from django.http import HttpResponse, StreamingHttpResponse
from django.views import View
from rest_framework_simplejwt.authentication import JWTAuthentication

from portfolio.services import get_latest_prices
from .pubsub import CHANNEL, get_redis

MAX_STREAMS_PER_USER = 3
MAX_STREAMS_PER_IP = 10  # H5: cap concurrent streams per source address
HEARTBEAT_SECONDS = 15
CONN_TTL_SECONDS = 3600


def format_event(event_type: str, data) -> str:
    """Render one SSE frame. `data` may be a JSON string (pass-through) or a dict."""
    body = data if isinstance(data, str) else json.dumps(data)
    return f"event: {event_type}\ndata: {body}\n\n"


def _client_ip(request) -> str:
    """Best-effort client IP (H5 per-address throttle)."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR", "")


def user_from_token(token):
    """Resolve an access-token string to a User, or None if invalid/wrong type.

    Access tokens only: simplejwt's AccessToken validator rejects refresh tokens
    (different token_type claim), so a stolen refresh token cannot open a stream.
    """
    if not token:
        return None
    authenticator = JWTAuthentication()
    try:
        validated = authenticator.get_validated_token(token)
        return authenticator.get_user(validated)
    except Exception:
        return None


def _stream_events(user, ip_key=None):
    """Generator yielding SSE frames for one connected user.

    Emits `hello` immediately (hydration from the cache), then `price` ticks as
    they are published, with a `: ping` comment every HEARTBEAT_SECONDS to keep
    proxies from timing the idle connection out.
    """
    client = get_redis()
    yield format_event("hello", {k: float(v) for k, v in get_latest_prices().items()})

    if client is None:
        # No Redis: keep the stream alive with heartbeats; client polls /latest.
        while True:
            yield ": ping\n\n"
            time.sleep(HEARTBEAT_SECONDS)
        return

    pubsub = client.pubsub(ignore_subscribe_messages=True)
    pubsub.subscribe(CHANNEL)
    last_heartbeat = time.monotonic()
    try:
        while True:
            message = pubsub.get_message(timeout=1.0)
            if message and message.get("type") == "message":
                # Published payloads are already JSON strings — pass straight through.
                yield format_event("price", message["data"])
            if time.monotonic() - last_heartbeat >= HEARTBEAT_SECONDS:
                yield ": ping\n\n"
                last_heartbeat = time.monotonic()
    finally:
        pubsub.close()
        # Release the connection slots claimed in the view (per-user + per-IP).
        client.decr(f"sse:conn:{user.id}")
        if ip_key:
            client.decr(ip_key)


class PriceStreamView(View):
    """GET /api/prices/stream/?token=<access> — live SSE stream of the price map."""

    def get(self, request):
        user = user_from_token(request.GET.get("token", ""))
        if user is None:
            return HttpResponse("Unauthorized", status=401)

        client = get_redis()
        ip_key = None
        if client is not None:
            # Per-IP cap first (H5): one address cannot open unbounded streams.
            ip_key = f"sse:conn:ip:{_client_ip(request)}"
            if client.incr(ip_key) > MAX_STREAMS_PER_IP:
                client.decr(ip_key)
                return HttpResponse("Too many streams from this address", status=429)
            client.expire(ip_key, CONN_TTL_SECONDS)

            # Per-user cap: one account cannot hold unbounded long-lived connections.
            user_key = f"sse:conn:{user.id}"
            if client.incr(user_key) > MAX_STREAMS_PER_USER:
                client.decr(user_key)
                client.decr(ip_key)  # release the IP slot we just claimed
                return HttpResponse("Too many concurrent streams", status=429)
            client.expire(user_key, CONN_TTL_SECONDS)  # leak guard if a disconnect is missed

        response = StreamingHttpResponse(
            _stream_events(user, ip_key), content_type="text/event-stream"
        )
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # disable nginx buffering for SSE
        return response
