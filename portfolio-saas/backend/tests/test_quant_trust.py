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
from portfolio.models import BacktestRun, BacktestYear
from portfolio.services import backtest
from portfolio.services.deflator import normalize_basis, to_basis
from portfolio.services.diagnostics import _load_index_returns
from portfolio.services.optimization import SCENARIOS
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

    returns, excluded = _build_returns_matrix(panel)

    assert "complete" in returns.columns
    assert "long_gap" not in returns.columns
    assert {item["key"]: item["reason"] for item in excluded}["long_gap"] == "price_gap_exceeded"


def test_completed_cutoffs_roll_forward_with_the_jalali_calendar():
    first = backtest._completed_jalali_cutoffs(
        dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
    )
    second = backtest._completed_jalali_cutoffs(
        dt.datetime(2027, 8, 1, tzinfo=dt.timezone.utc)
    )

    assert first == [
        "1400-01-01",
        "1401-01-01",
        "1402-01-01",
        "1403-01-01",
        "1404-01-01",
    ]
    assert second == [
        "1401-01-01",
        "1402-01-01",
        "1403-01-01",
        "1404-01-01",
        "1405-01-01",
    ]


def test_buy_and_hold_deducts_costs_and_rejects_missing_evaluation_returns():
    index = pd.date_range("2026-01-01", periods=2, tz="UTC")
    returns = pd.DataFrame({"asset": [0.10, 0.0]}, index=index)

    simulation = backtest._simulate_buy_and_hold(
        returns, {"asset": 1.0}, cost_drag=0.01
    )

    assert simulation["gross_return"] == pytest.approx(0.10)
    assert simulation["net_return"] == pytest.approx(0.089)
    assert simulation["net_return"] < simulation["gross_return"]

    returns.iloc[1, 0] = np.nan
    with pytest.raises(ValueError, match="evaluation_missing_prices"):
        backtest._simulate_buy_and_hold(returns, {"asset": 1.0}, cost_drag=0.01)


@pytest.mark.django_db
def test_walk_forward_uses_three_year_training_equal_weight_and_manifest(monkeypatch):
    run = BacktestRun.objects.create(
        params_hash="quant-trust",
        basis="nominal",
        universe=["asset_a", "asset_b", "asset_c"],
        universe_hash="abc",
    )
    optimize_calls = []

    def fake_optimize(**kwargs):
        optimize_calls.append(kwargs)
        return {
            "target_weights": {
                "asset_a": 1 / 3,
                "asset_b": 1 / 3,
                "asset_c": 1 / 3,
            },
            "excluded_assets": [],
            "constraints_applied": {"long_only": True},
            "price_version": "fixture-v1",
        }

    def fake_returns(*, as_of, **kwargs):
        end = pd.Timestamp(as_of).normalize()
        index = pd.date_range(end=end - pd.Timedelta(days=1), periods=3, tz="UTC")
        return pd.DataFrame(
            {"asset_a": 0.01, "asset_b": 0.01, "asset_c": 0.01},
            index=index,
        ), []

    monkeypatch.setattr(backtest, "optimize", fake_optimize)
    monkeypatch.setattr(backtest, "daily_returns_matrix", fake_returns)

    backtest.run_backtest(run.id)

    run.refresh_from_db()
    years = BacktestYear.objects.filter(run=run)
    assert run.status == BacktestRun.Status.READY
    assert years.count() == 25
    assert "equal_weight" in SCENARIOS
    assert {year.scenario for year in years} == set(SCENARIOS)
    assert all(call["history_days"] >= 3 * 365 for call in optimize_calls)
    result = years.exclude(realized_metrics__has_key="error").first()
    assert result.realized_metrics["realized_return"] < result.realized_metrics["gross_return"]
    manifest = result.realized_metrics["manifest"]
    assert manifest["basis"] == "nominal_toman"
    assert manifest["training_window"]["years"] == 3
    assert manifest["evaluation_window"]["start"] == result.cutoff_date
    assert manifest["price_version"] == "fixture-v1"


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
