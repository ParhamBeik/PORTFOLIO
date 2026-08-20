"""Returns source selection: warehouse daily series preferred, Price fallback.

The key regression test for the dual-source panel in
portfolio/services/returns.py: an asset with >= MIN_DAILY_RETURNS warehouse
rows must be priced from the warehouse; an asset without them must fall back
to the live Price table; and warehouse writes must rotate the cache
fingerprint.
"""
import datetime as dt

import jdatetime
import pytest

from marketdata.models import DailyStockHistory, MarketCandle, MarketDailyBar, MarketInstrument
from portfolio.models import Asset
from portfolio.services.returns import (
    MIN_DAILY_RETURNS,
    _load_price_panel,
    _price_version_fingerprint,
    daily_returns_matrix,
)

pytestmark = pytest.mark.django_db


def _seed_warehouse_days(symbol: str, n: int, start_price: float = 8000.0):
    """Write n consecutive daily closes ending today (Jalali-dated) to MarketCandle (1d_adj)."""
    today = dt.date.today()
    rows = []
    for i in range(n):
        day = today - dt.timedelta(days=n - i)
        jday = jdatetime.date.fromgregorian(date=day)
        date_str = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        price = start_price + i * 10
        rows.append(MarketCandle(
            symbol=symbol,
            timeframe="1d_adj",
            date_time=date_str,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1000,
        ))
    MarketCandle.objects.bulk_create(rows, ignore_conflicts=True)


def _seed_market_daily_bars(asset_class: str, symbol: str, n: int, start_price: float = 60000.0):
    """Write n consecutive daily closes ending today (Jalali-dated) to MarketDailyBar."""
    today = dt.date.today()
    rows = []
    for i in range(n):
        day = today - dt.timedelta(days=n - i)
        jday = jdatetime.date.fromgregorian(date=day)
        date_str = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        price = start_price + i * 10
        rows.append(MarketDailyBar(
            asset_class=asset_class,
            symbol=symbol,
            date=date_str,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            volume=1000,
            sample_count=5,
        ))
    MarketDailyBar.objects.bulk_create(rows, ignore_conflicts=True)


def test_market_daily_bar_used_for_crypto_before_live_fallback(asset_catalog, write_prices):
    """Crypto/commodity/ETF NAV must be priced from MarketDailyBar (real close,
    distilled from live snapshots -- open=first snapshot, close=last, not an
    average) rather than the live Price-tick fallback panel. Regression for the
    returns matrix silently mixing an intraday-mean series into the same
    covariance matrix as every close-based column.
    """
    btc = asset_catalog["bitcoin_usd"]
    btc.brs_symbol = "BTC"
    btc.save(update_fields=["brs_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="BTC",
        category=MarketInstrument.Category.CRYPTO,
        eligible=True,
    )
    _seed_market_daily_bars(MarketDailyBar.AssetClass.CRYPTO, "BTC", MIN_DAILY_RETURNS + 5)
    # A single live tick that would otherwise seed the mean-fallback panel --
    # must be ignored now that MarketDailyBar covers this symbol.
    write_prices({"bitcoin_usd": 1000})

    panel, excluded, warnings = _load_price_panel(history_days=90, universe=["bitcoin_usd"])
    series = panel["bitcoin_usd"].dropna()
    assert len(series) >= MIN_DAILY_RETURNS
    # Bar closes rise 10/day from 60000 -- evidence this is the warehouse
    # series, not the single 1000-priced live tick.
    assert series.iloc[-1] > 60000


def test_live_panel_uses_last_tick_not_mean(asset_catalog):
    """The live-tick fallback panel (for assets with no warehouse/MarketDailyBar
    coverage at all) must report the day's LAST price, not the mean --
    averaging silently swapped the return definition away from the
    close-to-close basis every other column in the panel uses.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price
    from portfolio.services.returns import _load_live_price_panel

    asset = asset_catalog["bitcoin_usd"]
    now = timezone.now()
    early = Price.objects.create(asset=asset, price=100, source="TEST")
    Price.objects.filter(id=early.id).update(fetched_at=now - timedelta(minutes=10))
    late = Price.objects.create(asset=asset, price=140, source="TEST")
    Price.objects.filter(id=late.id).update(fetched_at=now - timedelta(minutes=2))

    panel = _load_live_price_panel(now - timedelta(days=1), None, ["bitcoin_usd"])
    value = panel["bitcoin_usd"].dropna().iloc[-1]
    assert float(value) == 140.0  # last tick, not mean((100+140)/2 = 120)


def test_warehouse_series_used_when_deep_enough(asset_catalog, write_prices):
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    _seed_warehouse_days("کاما", MIN_DAILY_RETURNS + 10)
    # A couple of live Price rows for a different asset -> that one must fall back.
    write_prices({"emami_coin": 50_000_000})

    df, _ = daily_returns_matrix()
    assert "kama_stock" in df.columns
    # Warehouse closes rise 10 per day from 8000+, so daily returns are small
    # positive numbers — evidence the warehouse series (not the single live
    # Price row, which can't produce any return) fed this column.
    series = df["kama_stock"].dropna()
    assert len(series) >= MIN_DAILY_RETURNS - 1
    # (pct_change pad-fills days the warehouse lacks — e.g. today — to 0.0,
    # so assert non-negative everywhere and strictly positive on most days.)
    assert (series >= 0).all()
    assert (series > 0).sum() >= MIN_DAILY_RETURNS - 2
    # emami_coin has one live tick -> under MIN_DAILY_RETURNS -> excluded.
    assert "emami_coin" not in df.columns


def test_price_fallback_when_warehouse_too_shallow(asset_catalog, write_prices):
    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    # Fewer warehouse rows than the threshold -> must NOT use warehouse.
    _seed_warehouse_days("کاما", 5)
    df, excluded = daily_returns_matrix()
    # With only 5 warehouse days and no Price rows, kama has no eligible series.
    assert "kama_stock" not in df.columns


def test_fingerprint_rotates_on_warehouse_write(asset_catalog):
    before = _price_version_fingerprint()
    _seed_warehouse_days("کاما", 1)
    after = _price_version_fingerprint()
    assert before != after


def test_returns_cache_isolated_by_history_window(monkeypatch):
    import pandas as pd
    from django.core.cache import cache
    import portfolio.services.returns as returns

    calls = []

    def fake_panel(days, as_of=None, universe=None, held_keys=frozenset()):
        calls.append(days)
        return pd.DataFrame(), [], []

    cache.clear()
    monkeypatch.setattr(returns, "_load_price_panel", fake_panel)
    daily_returns_matrix(history_days=30)
    daily_returns_matrix(history_days=180)
    assert calls == [30, 180]
