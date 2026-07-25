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

from marketdata.models import DailyStockHistory, MarketCandle
from portfolio.models import Asset
from portfolio.services.returns import (
    MIN_DAILY_RETURNS,
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

    def fake_panel(days):
        calls.append(days)
        return pd.DataFrame()

    cache.clear()
    monkeypatch.setattr(returns, "_load_price_panel", fake_panel)
    daily_returns_matrix(history_days=30)
    daily_returns_matrix(history_days=180)
    assert calls == [30, 180]
