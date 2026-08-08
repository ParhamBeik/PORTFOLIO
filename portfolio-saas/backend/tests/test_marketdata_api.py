"""API tests for the /api/market/ warehouse endpoints (FREE vs PRO gating)."""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    StockSymbolMetadata,
)
from portfolio.models import Asset

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
    assert {
        "date_time", "open", "high", "low", "close", "volume", "source", "unit"
    } == set(body[0])
    assert body[0]["unit"] == "IRR"
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


def test_admin_status_rejects_non_staff_user(make_user):
    from django.test import Client
    client = Client()
    # Anonymous or non-staff user gets redirected to admin login page (302)
    response = client.get("/admin/")
    assert response.status_code in (302, 403)
    
    user = make_user()
    client.force_login(user)
    response = client.get("/admin/")
    assert response.status_code in (302, 403)


def test_admin_status_allows_staff_user(make_user):
    user = make_user()
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    
    from django.test import Client
    client = Client()
    client.force_login(user)
    response = client.get("/admin/")
    assert response.status_code == 200
    assert b"API Quota Status" in response.content
    assert b"Archive Backfill Progress" in response.content
    assert b"Database Table Freshness" in response.content



def test_market_assets_and_gold_performance(make_user, asset_catalog):
    gold = asset_catalog["emami_coin"]
    gold.brs_symbol = "IR_COIN_EMAMI"
    gold.save(update_fields=["brs_symbol"])
    GoldCurrencyHistory.objects.bulk_create([
        GoldCurrencyHistory(
            symbol="IR_COIN_EMAMI",
            name="Emami Coin",
            date=f"1404-01-{day:02d}",
            open_price=100 + day,
            high_price=110 + day,
            low_price=90 + day,
            close_price=105 + day,
        )
        for day in (1, 2)
    ])
    client = _auth(make_user())
    assets = client.get("/api/market/assets/").json()
    row = next(row for row in assets if row["key"] == "emami_coin")
    assert row["records"] == 2
    assert row["asset_class"] == "Gold"
    assert row["source"] == "gold"
    response = client.get("/api/market/performance/?asset=emami_coin")
    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["records"] == 2
    assert body["series"][0]["date"] < body["series"][-1]["date"]


def test_performance_rejects_inactive_asset(make_user, asset_catalog):
    asset = Asset.objects.get(key="bitcoin_usd")
    asset.is_active = False
    asset.save(update_fields=["is_active"])
    response = _auth(make_user()).get("/api/market/performance/?asset=bitcoin_usd")
    assert response.status_code == 400


def test_market_assets_include_stock_industry_and_currency_groups(make_user, asset_catalog):
    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    usd = asset_catalog["usd_cash"]
    usd.brs_symbol = "USD"
    usd.save(update_fields=["brs_symbol"])
    StockSymbolMetadata.objects.create(
        ins_code=1,
        l18="کاما",
        l30="Bama",
        sector="Mining",
        sector_sub="Lead and zinc",
    )
    DailyStockHistory.objects.create(
        symbol="کاما",
        date="1404-01-02",
        pl=Decimal("7000"),
        pc=Decimal("7000"),
        is_adjusted=True,
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD",
        date="1404-01-02",
        close_price=Decimal("63200"),
    )

    rows = _auth(make_user()).get("/api/market/assets/").json()
    kama = next(row for row in rows if row["key"] == "kama_stock")
    dollar = next(row for row in rows if row["key"] == "usd_cash")
    assert kama["asset_class"] == "Stock"
    assert kama["source"] == "stock"
    assert kama["sector"] == "Mining"
    assert kama["sector_sub"] == "Lead and zinc"
    assert dollar["asset_class"] == "Cash"
    assert dollar["source"] == "currency"


def test_ticks_returns_series(make_user):
    from marketdata.models import StockTransactionTick
    StockTransactionTick.objects.create(
        symbol="کاما",
        date="1404-01-02",
        time="09:30:00",
        row=1,
        price=Decimal("7050"),
        volume=5000,
        canceled=False,
    )
    resp = _auth(make_user()).get("/api/market/ticks/?symbol=کاما")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["price"] == 7050.0
    assert body[0]["volume"] == 5000
    assert body[0]["unit"] == "IRR"


def test_ticks_requires_symbol(make_user):
    resp = _auth(make_user()).get("/api/market/ticks/")
    assert resp.status_code == 400


def test_market_assets_and_performance_strictly_use_adjusted(make_user, asset_catalog):
    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])

    # Create one adjusted MarketCandle and one unadjusted DailyStockHistory record for the same day
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe="1d_adj",
        date_time="1404-01-02",
        open_price=Decimal("7000"),
        high_price=Decimal("7000"),
        low_price=Decimal("7000"),
        close_price=Decimal("7000"),
        volume=1000,
    )
    DailyStockHistory.objects.create(
        symbol="کاما",
        date="1404-01-02",
        pl=Decimal("8000"),
        pc=Decimal("8000"),
        is_adjusted=False,
    )

    client = _auth(make_user())

    # 1. Assets list stats should only count the adjusted record
    rows = client.get("/api/market/assets/").json()
    kama = next(row for row in rows if row["key"] == "kama_stock")
    assert kama["records"] == 1

    # 2. Performance view should return the adjusted price (7000) instead of the unadjusted (8000)
    perf = client.get("/api/market/performance/?asset=kama_stock").json()
    assert perf["coverage"]["records"] == 1
    assert perf["series"][0]["close"] == 7000.0
