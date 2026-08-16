import datetime as dt
from decimal import Decimal

import jdatetime
import numpy as np
import pandas as pd
import pytest
from django.test import override_settings

from marketdata import ingest
from marketdata.integrity import compute_symbol_integrity
from marketdata.models import (
    MarketCandle,
    MarketIndexData,
    MarketInstrument,
    RejectedRecord,
)
from portfolio.services.deflator import normalize_basis, to_basis
from portfolio.services.diagnostics import _load_index_returns
from portfolio.services.returns import _build_returns_matrix, _returns_cache_key


def test_basis_aliases_are_canonical_and_fx_never_backfills_from_the_future():
    index = pd.date_range("2026-01-01", periods=3, tz="UTC")
    prices = pd.Series([100.0, 110.0, 120.0], index=index)
    usd = pd.Series([10.0], index=index[1:2])

    assert normalize_basis("nominal") == "nominal_toman"
    assert normalize_basis("nominal_toman") == "nominal_toman"
    assert normalize_basis("usd_real") == "usd_denominated"
    assert normalize_basis("usd_denominated") == "usd_denominated"
    assert _returns_cache_key(30, None, None, "usd_real", "1") == _returns_cache_key(
        30, None, None, "usd_denominated", "1"
    )

    converted = to_basis(prices, "usd_denominated", usd_series=usd)
    assert pd.isna(converted.iloc[0])
    assert converted.iloc[1] == pytest.approx(11.0)
    assert converted.iloc[2] == pytest.approx(12.0)


def test_returns_exclude_prices_with_a_gap_longer_than_five_sessions():
    index = pd.date_range("2026-01-01", periods=45, tz="UTC")
    panel = pd.DataFrame(
        {
            "complete": np.linspace(100.0, 145.0, len(index)),
            "long_gap": np.linspace(200.0, 245.0, len(index)),
        },
        index=index,
    )
    panel.loc[index[12:18], "long_gap"] = np.nan

    returns, excluded, _warnings = _build_returns_matrix(panel)

    assert "complete" in returns.columns
    assert "long_gap" not in returns.columns
    assert {item["key"]: item["reason"] for item in excluded}["long_gap"] == "price_gap_exceeded"


@pytest.mark.django_db
def test_integrity_uses_an_explicit_window_and_real_expected_sessions():
    MarketInstrument.objects.create(
        symbol="WINDOWED",
        name="Windowed",
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        eligible=True,
    )
    start = dt.date(2026, 7, 25)
    end = dt.date(2026, 7, 29)
    for day in (start, start + dt.timedelta(days=1), end):
        jday = jdatetime.date.fromgregorian(date=day)
        MarketCandle.objects.create(
            symbol="WINDOWED",
            timeframe="1d_adj",
            date_time=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            open_price=100,
            high_price=100,
            low_price=100,
            close_price=100,
            volume=1,
        )
    # The market-wide calendar must know the two sessions WINDOWED missed.
    for day in (start + dt.timedelta(days=2), start + dt.timedelta(days=3)):
        jday = jdatetime.date.fromgregorian(date=day)
        MarketCandle.objects.create(
            symbol="REFERENCE",
            timeframe="1d_unadj",
            date_time=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            close_price=100,
            volume=1,
        )
    # And seed unadjusted reference rows for WINDOWED's observed sessions.
    for day in (start, start + dt.timedelta(days=1), end):
        jday = jdatetime.date.fromgregorian(date=day)
        MarketCandle.objects.create(
            symbol="REFERENCE",
            timeframe="1d_unadj",
            date_time=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            close_price=100,
            volume=1,
        )

    result = compute_symbol_integrity("WINDOWED", start=start, end=end)

    assert result["observed_sessions"] == 3
    assert result["expected_sessions"] == 5
    assert result["coverage_ratio"] == pytest.approx(0.6)
    assert result["last_valid_date"] == end.isoformat()
    assert "low_coverage" in result["reason_codes"]


@pytest.mark.django_db
def test_index_ingest_uses_the_shared_validation_screen():
    created, rejected = ingest.ingest_market_index(
        {
            "date": "1405-05-10",
            "time": "12:30:00",
            "index": -1,
            "index_change": 0,
        }
    )

    assert (created, rejected) == (0, 1)
    assert not MarketIndexData.objects.exists()
    rejection = RejectedRecord.objects.get(endpoint="market_index", symbol="TEDPIX")
    assert rejection.reason == "index_not_positive"


@pytest.mark.django_db
@override_settings(HISTORICAL_BENCHMARK_ENABLED=True)
def test_benchmark_alignment_never_backfills_missing_returns():
    dates = [dt.date(2026, 7, 25), dt.date(2026, 7, 27)]
    for value, day in zip((100.0, 110.0), dates):
        jday = jdatetime.date.fromgregorian(date=day)
        MarketIndexData.objects.create(
            date=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            time="12:30:00",
            index_overall=value,
        )
    target = pd.date_range("2026-07-25", periods=3, tz="UTC")

    aligned = _load_index_returns(target, as_of=dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc))

    assert pd.isna(aligned.loc[target[0]])
    assert pd.isna(aligned.loc[target[1]])
    assert aligned.loc[target[2]] == pytest.approx(0.10)
