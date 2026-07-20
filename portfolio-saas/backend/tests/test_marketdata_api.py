"""API tests for the /api/market/ warehouse endpoints (FREE vs PRO gating)."""
import pytest
from rest_framework.test import APIClient

from marketdata.models import CodalAnnouncement, MarketCandle

pytestmark = pytest.mark.django_db


def _auth(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _seed_candles(n=3):
    MarketCandle.objects.bulk_create([
        MarketCandle(symbol="کاما", timeframe="1d_adj", date_time=f"1404-02-{20 + i:02d}",
                     open_price=7000 + i, high_price=7100 + i, low_price=6900 + i,
                     close_price=7050 + i, volume=1000 * (i + 1))
        for i in range(n)
    ])


def _seed_announcements(n=2):
    CodalAnnouncement.objects.bulk_create([
        CodalAnnouncement(symbol="کاما", title=f"گزارش {i}", code=f"c{i}",
                          date_publish=f"1404-01-{10 + i:02d}", time_publish="10:00:00")
        for i in range(n)
    ])


def test_candles_returns_series_for_free_user(make_user):
    _seed_candles()
    resp = _auth(make_user()).get("/api/market/candles/?symbol=کاما")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 3
    assert {"date_time", "open", "high", "low", "close", "volume"} == set(body[0])
    # Oldest-first for charting.
    assert body[0]["date_time"] < body[-1]["date_time"]


def test_candles_requires_symbol(make_user):
    resp = _auth(make_user()).get("/api/market/candles/")
    assert resp.status_code == 400


def test_candles_limit_is_capped(make_user):
    _seed_candles()
    resp = _auth(make_user()).get("/api/market/candles/?symbol=کاما&limit=99999")
    assert resp.status_code == 200  # capped internally, not an error


def test_announcements_blocked_for_free_user(make_user):
    _seed_announcements()
    resp = _auth(make_user(tier="FREE")).get("/api/market/announcements/?symbol=کاما")
    assert resp.status_code == 403


def test_announcements_returned_for_pro_user(make_user):
    _seed_announcements()
    resp = _auth(make_user(tier="PRO")).get("/api/market/announcements/?symbol=کاما")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    assert body[0]["symbol"] == "کاما"


def test_anonymous_rejected():
    assert APIClient().get("/api/market/candles/?symbol=x").status_code in (401, 403)
