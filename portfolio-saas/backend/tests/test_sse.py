"""SSE view: token auth + frame formatting.

These cover the security-sensitive pieces (a query-param token must be a valid
*access* token, never a refresh token) without a live Redis connection. The
end-to-end publish -> streamed-event path needs a running Redis and is exercised
manually via the verification steps.
"""
import json

import pytest
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from portfolio.live.sse import format_event, user_from_token

pytestmark = pytest.mark.django_db


def test_format_event_serializes_a_dict():
    frame = format_event("price", {"kama_stock": 5230})
    assert frame == 'event: price\ndata: {"kama_stock": 5230}\n\n'


def test_format_event_passes_a_json_string_through_unchanged():
    raw = json.dumps({"kama_stock": 5230})
    frame = format_event("price", raw)
    # Published payloads are already JSON; the view must not double-encode them.
    assert frame == f"event: price\ndata: {raw}\n\n"


def test_user_from_valid_access_token(make_user):
    user = make_user(email="sse@test.test")
    token = str(AccessToken.for_user(user))
    resolved = user_from_token(token)
    assert resolved is not None and resolved.id == user.id


def test_user_from_refresh_token_is_rejected(make_user):
    """A refresh token must NOT authenticate a stream (access tokens only)."""
    user = make_user(email="refresh@test.test")
    refresh = str(RefreshToken.for_user(user))
    assert user_from_token(refresh) is None


def test_user_from_garbage_is_rejected():
    assert user_from_token("not-a-real-token") is None


def test_user_from_missing_token_is_rejected():
    assert user_from_token("") is None
    assert user_from_token(None) is None


def test_stream_endpoint_rejects_anonymous(monkeypatch):
    """No/invalid token -> 401, before any streaming starts."""
    from django.test import Client

    client = Client()
    resp = client.get("/api/prices/stream/")  # no token
    assert resp.status_code == 401
    resp = client.get("/api/prices/stream/?token=garbage")
    assert resp.status_code == 401


def test_sse_rate_limit_constants():
    """Verify stream concurrency cap and connection TTL constants."""
    from portfolio.live.sse import CONN_TTL_SECONDS, MAX_STREAMS_PER_IP, MAX_STREAMS_PER_USER

    assert MAX_STREAMS_PER_USER == 10
    assert MAX_STREAMS_PER_IP == 30
    assert CONN_TTL_SECONDS == 60

