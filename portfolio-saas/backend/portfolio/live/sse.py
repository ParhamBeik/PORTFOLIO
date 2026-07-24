"""Server-Sent Events stream for the global price map.

Prices are global, so a single Redis pub/sub channel fans the same bytes out to
every connected client — the push path is O(1) in user count. Portfolio values
are per-user and are NOT pushed: the client recomputes them locally from the
holdings it already has on each tick.

Auth: EventSource cannot set headers, so the access JWT rides as ?token=. Refresh
tokens are rejected. Concurrent streams per user are capped so one account cannot
hold an unbounded number of long-lived connections.
"""
import asyncio
import json

from asgiref.sync import sync_to_async
from django.http import HttpResponse, StreamingHttpResponse
from django.views import View
from rest_framework_simplejwt.authentication import JWTAuthentication

from portfolio.services import get_latest_prices
from .pubsub import CHANNEL, get_async_redis, get_redis

MAX_STREAMS_PER_USER = 10
MAX_STREAMS_PER_IP = 30  # H5: cap concurrent streams per source address
HEARTBEAT_SECONDS = 15
CONN_TTL_SECONDS = 60


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


async def _stream_events(user, ip_key=None):
    """Generator yielding SSE frames for one connected user.

    Emits `hello` immediately (hydration from the cache), then `price` ticks as
    they are published, with a `: ping` comment every HEARTBEAT_SECONDS to keep
    proxies from timing the idle connection out.
    """
    prices = await sync_to_async(get_latest_prices, thread_sensitive=True)()
    yield format_event("hello", {k: float(v) for k, v in prices.items()})

    client = get_async_redis()
    if client is None:
        # No Redis: keep the stream alive with heartbeats; client polls /latest.
        while True:
            yield ": ping\n\n"
            await asyncio.sleep(HEARTBEAT_SECONDS)
        return

    pubsub = client.pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(CHANNEL)
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    try:
        while True:
            message = await pubsub.get_message(timeout=1.0)
            if message and message.get("type") == "message":
                # Published payloads are already JSON strings — pass straight through.
                yield format_event("price", message["data"])
            if loop.time() - last_heartbeat >= HEARTBEAT_SECONDS:
                await client.expire(f"sse:conn:{user.id}", CONN_TTL_SECONDS)
                if ip_key:
                    await client.expire(ip_key, CONN_TTL_SECONDS)
                yield ": ping\n\n"
                last_heartbeat = loop.time()
    finally:
        await pubsub.aclose()
        # Release the connection slots claimed in the view (per-user + per-IP).
        user_key = f"sse:conn:{user.id}"
        if await client.decr(user_key) < 0:
            await client.delete(user_key)
        if ip_key:
            if await client.decr(ip_key) < 0:
                await client.delete(ip_key)


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
