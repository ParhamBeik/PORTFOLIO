"""How the risk breakdown finds a held asset's history in the warehouse.

Integration, not unit: every defect these cover lived at the ORM -> panel ->
matrix boundary (a SymbolIntegrity row, a leading run of NaNs, an Asset.proxy_key
lookup), not in a pure function. The pre-existing suite mocked
`daily_returns_matrix` wholesale, which is exactly why a 37%-weight holding could
vanish from the risk card without a single test going red. These seed real rows
and go through the real loader.
"""
import datetime

import jdatetime
import numpy as np
import pandas as pd
import pytest
from django.core.cache import cache

from marketdata.models import GoldCurrencyHistory, MarketCandle, MarketInstrument, SymbolIntegrity
from portfolio.models import Asset
from portfolio.services.diagnostics import _portfolio_returns, portfolio_diagnostics
from portfolio.services.returns import (
    _build_returns_matrix,
    _gap_profile,
    daily_returns_matrix,
    periods_per_year,
)

WINDOW_DAYS = 180


def _jalali_days(count, *, offset=0):
    """`count` consecutive Jalali date strings ending `offset` days before today."""
    today = jdatetime.date.today()
    return [
        (today - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(count + offset - 1, offset - 1, -1)
    ]


def _gold(symbol, days, *, start=1000.0, step=5.0):
    GoldCurrencyHistory.objects.bulk_create([
        GoldCurrencyHistory(
            symbol=symbol, date=day, close_price=start + i * step, unit="تومان"
        )
        for i, day in enumerate(days)
    ])


def _instrument(symbol, source=MarketInstrument.Source.BRS):
    return MarketInstrument.objects.create(
        symbol=symbol,
        name=symbol,
        source=source,
        category=MarketInstrument.Category.GOLD,
        eligible=True,
    )


def _asset(key, **kwargs):
    # Asset.save() runs full_clean, which requires an eligible MarketInstrument
    # for non-manual/non-house assets -- so seed the instrument first.
    kwargs.setdefault("name", key)
    kwargs.setdefault("is_active", True)
    return Asset.objects.create(key=key, **kwargs)


def _valuation(items):
    return {"items": [
        {
            "key": key,
            "asset": key,
            "class": cls,
            "value": value,
            "is_house": cls == "Real Estate",
            "is_manual": False,
        }
        for key, cls, value in items
    ]}


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


# --- the gap profile: short history is not corruption -------------------------

def test_leading_nans_are_not_counted_as_a_price_gap():
    # 10 missing days at the front, then a clean run: the asset simply started
    # late. The old single-scan reported this as a 10-session gap.
    observed = np.array([False] * 10 + [True] * 30)
    assert _gap_profile(observed) == (10, 0)


def test_interior_and_trailing_holes_still_count():
    observed = np.array([False] * 3 + [True] * 5 + [False] * 7 + [True] * 5)
    assert _gap_profile(observed) == (3, 7)
    # A series that stops mid-window is a hole, not a short history.
    assert _gap_profile(np.array([True] * 5 + [False] * 9)) == (0, 9)


def test_short_history_survives_for_a_held_asset_but_not_a_screened_one():
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    panel = pd.DataFrame(
        {
            "late": np.linspace(100.0, 160.0, len(index)),
            "complete": np.linspace(200.0, 260.0, len(index)),
        },
        index=index,
    )
    panel.loc[index[:40], "late"] = np.nan  # only 20 sessions of real history

    screened, excluded, _ = _build_returns_matrix(panel)
    assert "late" not in screened.columns
    assert {e["key"]: e["reason"] for e in excluded}["late"] == "insufficient_history"

    held, excluded_held, warnings = _build_returns_matrix(panel, frozenset({"late"}))
    assert "late" in held.columns, "a held asset keeps whatever history it has"
    assert not [e for e in excluded_held if e["key"] == "late"]
    assert {w["key"]: w["reason"] for w in warnings}["late"] == "short_history"


def test_a_real_interior_gap_excludes_even_a_held_asset():
    # Forward-filling past MAX_FORWARD_FILL_SESSIONS invents prices; a made-up
    # return is worse than a missing one, so this bar does not bend for holdings.
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    panel = pd.DataFrame({"holed": np.linspace(100.0, 160.0, len(index))}, index=index)
    panel.loc[index[20:32], "holed"] = np.nan

    _, excluded, _ = _build_returns_matrix(panel, frozenset({"holed"}))
    assert {e["key"]: e["reason"] for e in excluded}["holed"] == "price_gap_exceeded"


# --- annualization frequency --------------------------------------------------

def test_periods_per_year_reads_the_calendar_not_a_constant():
    daily = pd.date_range("2026-01-01", periods=200, freq="D", tz="UTC")
    assert periods_per_year(daily) == pytest.approx(365.25, abs=1.0)

    # Sat-Wed: five sessions a week, spacings of 1,1,1,1,3 -- whose MEDIAN is 1.
    sessions = daily[~daily.dayofweek.isin([3, 4])]
    assert periods_per_year(sessions) == pytest.approx(261, abs=8)

    # An exchange closure is not the cadence and must not drag the figure down.
    with_closure = sessions.delete(range(40, 100))
    assert periods_per_year(with_closure) == pytest.approx(
        periods_per_year(sessions), abs=8
    )


# --- portfolio aggregation ----------------------------------------------------

def test_portfolio_returns_renormalize_per_day_instead_of_dropping_assets():
    index = pd.date_range("2026-01-01", periods=40, tz="UTC")
    returns = pd.DataFrame(
        {"a": np.full(40, 0.01), "b": np.full(40, 0.02)}, index=index
    )
    returns.iloc[5, returns.columns.get_loc("b")] = np.nan

    series = _portfolio_returns(returns, {"a": 0.6, "b": 0.4})

    assert len(series.index) == 40, "no row lost because one asset missed a day"
    assert series.iloc[0] == pytest.approx(0.6 * 0.01 + 0.4 * 0.02)
    # On the thin day the weights renormalize onto 'a' alone.
    assert series.iloc[5] == pytest.approx(0.01)
    assert set(series.attrs["weights_used"]) == {"a", "b"}
    assert series.attrs["dropped_assets"] == []
    assert series.attrs["mean_weight_covered"] == pytest.approx(1 - 0.4 / 40, abs=1e-6)


def test_many_assets_on_a_short_window_are_not_truncated_to_three():
    # The old drop-loop required 10 shared observations per asset and stopped at
    # 3 survivors, so a 10-asset book on a 60-session window lost half itself.
    index = pd.date_range("2026-01-01", periods=60, tz="UTC")
    keys = [f"a{i}" for i in range(10)]
    returns = pd.DataFrame({k: np.full(60, 0.01) for k in keys}, index=index)

    series = _portfolio_returns(returns, {k: 0.1 for k in keys})

    assert len(series.attrs["weights_used"]) == 10
    assert series.attrs["weights_rescaled"] is False
    assert series.iloc[0] == pytest.approx(0.01)


# --- the full path, through the ORM ------------------------------------------

@pytest.mark.django_db
def test_held_asset_with_a_failed_integrity_gate_is_reported_with_a_warning():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _instrument("IR_COIN_EMAMI")
    _gold("IR_GOLD_18K", days)
    _gold("IR_COIN_EMAMI", days, start=5000.0, step=11.0)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset("emami_coin", asset_class="Gold", brs_symbol="IR_COIN_EMAMI")
    # The nightly screen fails it on a rejection ratio, despite full coverage.
    SymbolIntegrity.objects.create(
        symbol="IR_GOLD_18K", passes_gate=False, reason="excessive_rejections"
    )

    # Unheld, it is still screened out: the optimizer's universe is unchanged.
    screened, excluded = daily_returns_matrix(
        history_days=WINDOW_DAYS, universe=["gold_18k_gram", "emami_coin"]
    )
    assert "gold_18k_gram" not in screened.columns
    assert {e["key"]: e["reason"] for e in excluded}["gold_18k_gram"] == (
        "integrity_gate_failed"
    )

    valuation = _valuation([("gold_18k_gram", "Gold", 600), ("emami_coin", "Gold", 400)])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 0.6, "emami_coin": 0.4},
        1000,
        history_days=WINDOW_DAYS,
        valuation=valuation,
    )

    row = {r["key"]: r for r in payload["by_asset"]}["gold_18k_gram"]
    assert row["status"] == "ready"
    assert row["metrics"] is not None
    assert row["metrics"]["annualized_volatility"] > 0
    assert "integrity_gate_failed" in row["warnings"]
    assert payload["coverage"]["analyzed_weight_pct"] == pytest.approx(1.0, abs=1e-6)
    assert payload["coverage"]["health"] == "degraded", "a caveat must be visible"


@pytest.mark.django_db
def test_manual_asset_borrows_its_proxy_series():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset(
        "swiss_gold_bar_1g",
        asset_class="Gold",
        is_manual=True,
        proxy_key="gold_18k_gram",
    )

    valuation = _valuation([
        ("gold_18k_gram", "Gold", 900),
        ("swiss_gold_bar_1g", "Gold", 100),
    ])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 0.9, "swiss_gold_bar_1g": 0.1},
        1000,
        history_days=WINDOW_DAYS,
        valuation=valuation,
    )

    rows = {r["key"]: r for r in payload["by_asset"]}
    bar, gold = rows["swiss_gold_bar_1g"], rows["gold_18k_gram"]
    assert bar["status"] == "ready"
    assert bar["proxied_from"] == "gold_18k_gram"
    assert "proxied" in bar["warnings"]
    assert bar["metrics"]["annualized_volatility"] == pytest.approx(
        gold["metrics"]["annualized_volatility"]
    )
    # Proxying is a modeling choice, not a data defect.
    assert payload["coverage"]["health"] == "healthy"


@pytest.mark.django_db
def test_proxies_stay_out_of_the_unheld_universe():
    # Two identical columns would give the optimizer a singular covariance and an
    # arbitrary choice between them, so proxy resolution is opt-in via held_keys.
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset(
        "swiss_gold_bar_1g",
        asset_class="Gold",
        is_manual=True,
        proxy_key="gold_18k_gram",
    )

    universe = ["gold_18k_gram", "swiss_gold_bar_1g"]
    screened, _ = daily_returns_matrix(history_days=WINDOW_DAYS, universe=universe)
    assert "swiss_gold_bar_1g" not in screened.columns

    held, _ = daily_returns_matrix(
        history_days=WINDOW_DAYS,
        universe=universe,
        held_keys=frozenset({"swiss_gold_bar_1g"}),
    )
    assert "swiss_gold_bar_1g" in held.columns


@pytest.mark.django_db
def test_real_estate_is_excluded_from_weights_but_stated_in_coverage():
    days = _jalali_days(120)
    _instrument("IR_GOLD_18K")
    _gold("IR_GOLD_18K", days)
    _asset("gold_18k_gram", asset_class="Gold", brs_symbol="IR_GOLD_18K")
    _asset("house_asset", asset_class="Real Estate", is_house=True)

    valuation = _valuation([
        ("gold_18k_gram", "Gold", 700),
        ("house_asset", "Real Estate", 300),
    ])
    payload = portfolio_diagnostics(
        {"gold_18k_gram": 1.0}, 700, history_days=WINDOW_DAYS, valuation=valuation
    )

    rows = {r["key"]: r for r in payload["by_asset"]}
    assert rows["house_asset"]["status"] == "not_applicable"
    # The metrics describe 70% of the book, and the payload says so out loud.
    assert payload["coverage"]["analyzed_weight_pct"] == pytest.approx(0.7, abs=1e-6)
