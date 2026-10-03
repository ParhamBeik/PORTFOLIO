"""The News Intelligence price contract: gated, curated, and honest about gaps."""
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from marketdata import news_feed

KEY = "test-service-key"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=ZoneInfo("UTC"))  # a Friday


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _client(key=KEY):
    client = APIClient()
    if key:
        client.credentials(HTTP_X_NEWS_SERVICE_KEY=key)
    return client


@pytest.mark.parametrize("path", ["catalog", "series", "snapshot"])
def test_every_route_is_closed_without_the_right_key(path):
    url = f"/api/marketdata/news/{path}/"
    with override_settings(NEWS_MARKET_SERVICE_KEY=""):
        assert _client().get(url).status_code == 403  # unset key disables the API
    with override_settings(NEWS_MARKET_SERVICE_KEY=KEY):
        assert _client(None).get(url).status_code == 403
        assert _client("wrong").get(url).status_code == 403


@override_settings(NEWS_MARKET_SERVICE_KEY=KEY)
def test_catalog_shape_covers_the_four_groups():
    response = _client().get("/api/marketdata/news/catalog/")
    assert response.status_code == 200
    rows = response.json()["results"]
    assert {r["group"] for r in rows} == set(news_feed.GROUPS)
    brent = next(r for r in rows if r["key"] == "brent")
    for field in ("asset_class", "name_fa", "name_en", "unit", "currency", "cadence", "market_hours"):
        assert brent[field]
    assert brent["market_hours"]["tz"] == "America/New_York"


TGJU_ROWS = {"data": [
    # newest first, positional, as the origin serves it
    ["100", "99", "103", "102.27", "", "", "2026/10/01", "1405/07/09"],
    ["101", "98", "102", "98.02", "", "", "2026/09/30", "1405/07/08"],
    ["bad", "", "", "", "", "", "2026/09/29", "1405/07/07"],
]}


@override_settings(NEWS_MARKET_SERVICE_KEY=KEY)
def test_series_returns_ascending_daily_closes_stamped_after_the_day():
    with patch("marketdata.news_feed.fetch", return_value=TGJU_ROWS) as fetch, \
            patch("django.utils.timezone.now", return_value=NOW):
        response = _client().get("/api/marketdata/news/series/",
                                 {"key": "brent", "from": "2026-09-01", "to": "2026-10-02"})
    assert response.status_code == 200
    body = response.json()
    assert fetch.call_args.kwargs["params"]["length"] == 64  # bounded, not the full history
    assert [p["date"] for p in body["points"]] == ["2026-09-30", "2026-10-01"]
    assert body["points"][-1]["price"] == "102.27"
    assert body["points"][-1]["observed_at"].startswith("2026-10-02T00:00:00")
    assert body["unit"] == "USD/bbl" and body["caveats"] == []


@override_settings(NEWS_MARKET_SERVICE_KEY=KEY)
def test_series_rejects_unknown_keys_and_bad_windows():
    client = _client()
    assert client.get("/api/marketdata/news/series/", {"key": "portfolio_holdings"}).status_code == 404
    assert client.get("/api/marketdata/news/series/", {"key": "brent", "from": "x"}).status_code == 400
    assert client.get("/api/marketdata/news/series/",
                      {"key": "brent", "from": "2026-10-02", "to": "2026-01-01"}).status_code == 400


def test_series_flags_a_dead_feed_and_a_closed_market():
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=ZoneInfo("UTC"))
    with patch("marketdata.news_feed.fetch", return_value=TGJU_ROWS), \
            patch("django.utils.timezone.now", return_value=saturday):
        body = news_feed.series(news_feed.CATALOG["brent"],
                                datetime(2026, 9, 1).date(), datetime(2026, 10, 20).date())
    assert "stale_series" in body["caveats"]
    assert "market_closed" in body["caveats"]


def test_fred_holiday_dot_is_skipped_not_zero():
    csv = "observation_date,DGS2\n2026-09-29,3.61\n2026-09-30,.\n2026-10-01,3.58\n"
    with patch("marketdata.news_feed.fetch", return_value=csv):
        points = news_feed._fred_history("DGS2", datetime(2026, 9, 1).date())
    assert [str(v) for _, v in points] == ["3.61", "3.58"]


def test_ecb_reference_rate_is_read_per_currency():
    xml = ('<Cube><Cube time="2026-10-01"><Cube currency="USD" rate="1.1298"/>'
           '<Cube currency="JPY" rate="178.49"/></Cube></Cube>')
    with patch("marketdata.news_feed.fetch", return_value=xml):
        assert news_feed._ecb_history("USD")[0][1] == news_feed.Decimal("1.1298")


@override_settings(NEWS_MARKET_SERVICE_KEY=KEY)
def test_snapshot_signs_the_change_and_flags_stale_quotes():
    board = {
        "oil_brent": {"p": "99.839", "dp": 2.44, "dt": "low", "ts": "2026-10-02 14:15:37"},
        "bourse_globaldow": {"p": "2,434", "dp": 0, "dt": "", "ts": "2023-10-15 17:00:00"},
    }
    with patch("marketdata.sources.tgju.fetch_live", return_value=board), \
            patch("django.utils.timezone.now", return_value=NOW):
        response = _client().get("/api/marketdata/news/snapshot/", {"keys": "brent,dow_global"})
    assert response.status_code == 200
    brent, dow = response.json()["results"]
    assert brent["price"] == "99.839" and brent["change_pct"] == "-2.44"
    assert "stale_quote" not in brent["caveats"]
    assert dow["price"] == "2434" and "stale_quote" in dow["caveats"]
    assert _client().get("/api/marketdata/news/snapshot/", {"keys": "nope"}).status_code == 404


def test_snapshot_reports_an_unreachable_origin_instead_of_zero():
    with patch("marketdata.sources.tgju.fetch_live",
               side_effect=news_feed.SourceError("down", origin="tgju")):
        [entry] = news_feed.snapshot(["brent"], now=NOW)
    assert entry["price"] is None and "source_unavailable" in entry["caveats"]
