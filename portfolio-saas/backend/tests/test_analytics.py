"""Performance and signals: TWR/XIRR honesty, which return series is chosen, and the quantitative claims the UI is allowed to make.

Merged from 7 files; each section keeps its original banner.
"""

import datetime
import datetime as dt
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
import jdatetime
import numpy as np
import pandas as pd
import pytest
from rest_framework import status
from rest_framework.test import APITestCase

from marketdata import ingest
from marketdata.integrity import compute_symbol_integrity
from marketdata.integrity import compute_symbol_integrity, update_all_symbols_integrity
from marketdata.models import (
    MarketCandle,
    MarketIndexData,
    MarketInstrument,
    RejectedRecord,
)
from marketdata.models import DailyStockHistory, MarketCandle, MarketDailyBar, MarketInstrument
from marketdata.models import MarketCandle, SymbolIntegrity, MarketIndexData, GoldCurrencyHistory, MarketInstrument
from marketdata.models import MarketInstrument, SymbolIntegrity, MarketIndexData, MarketCandle
from marketdata.models import SymbolIntegrity
from marketdata.tasks import nightly_data_integrity
from portfolio.models import Account, Asset
from portfolio.models import Account, LedgerEntry
from portfolio.models import Asset
from portfolio.models import Asset, Price
from portfolio.services import signals as sig
from portfolio.services.deflator import normalize_basis, to_basis
from portfolio.services.deflator import to_basis
from portfolio.services.diagnostics import _load_index_returns
from portfolio.services.diagnostics import portfolio_diagnostics
from portfolio.services.ledger import create_ledger_entry, reverse_ledger_entry
from portfolio.services.optimization import optimize
from portfolio.services.performance import _position_metrics, account_performance
from portfolio.services.returns import (
    MIN_DAILY_RETURNS,
    _load_price_panel,
    _price_version_fingerprint,
    daily_returns_matrix,
)
from portfolio.services.returns import _build_returns_matrix, _returns_cache_key
from portfolio.services.returns import daily_returns_matrix
from portfolio.services.returns import daily_returns_matrix, _returns_cache_key, _load_price_panel, normalize_as_of
from portfolio.services.timeline import xirr

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_performance_honesty.py
# Performance/TWR/XIRR honesty gates.
# 
# Integration tests at the service boundary (Account/LedgerEntry rows through
# account_performance and the ledger service), because the behaviour under test
# is exactly the interaction between ledger state and the performance gate --
# not something a pure-function unit test can exercise on its own.


def _cash_account(make_user, email, *, opened_days_ago):
    account = Account.objects.create(user=make_user(email=email), name="Test")
    opened_at = timezone.now() - dt.timedelta(days=opened_days_ago)
    create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="1000000",
        occurred_at=opened_at,
    )
    account.refresh_from_db()
    return account


def test_short_tracking_window_is_gated_as_insufficient_history(make_user):
    account = _cash_account(make_user, "short@test.test", opened_days_ago=10)

    result = account_performance(account)

    assert result["performance_available"] is False
    assert result["reason"] == "insufficient_history"
    assert "xirr" not in result
    assert result["days_tracked"] == 10


def test_buy_entry_with_null_price_never_reports_numeric_unrealized_pnl(
    make_user, asset_catalog
):
    # Written directly (bypassing create_ledger_entry, which validates a BUY
    # price) to reproduce how the real reconstructed ledger rows landed with
    # price_tomans NULL on a non-opening entry.
    account = Account.objects.create(user=make_user(email="nullprice@test.test"), name="Test")
    LedgerEntry.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("2"),
        price_tomans=None,
        amount_tomans=Decimal("0"),
        timestamp=timezone.now() - dt.timedelta(days=200),
    )

    metrics = _position_metrics(account)

    row = metrics["emami_coin"]
    assert row["cost_basis_known"] is False
    assert row["average_cost_tomans"] is None
    assert row["total_cost_basis_tomans"] is None
    assert row["unrealized_pnl_tomans"] is None


def test_average_cost_declares_rial_while_its_own_cost_basis_is_toman(
    make_user, asset_catalog
):
    """A TSE row's unit cost and its cost basis are ten apart, and must say so.

    `average_cost_tomans` is not Toman for a TSE share: it is the Rial quote the
    user typed, left alone because it is a unit price. `total_cost_basis_tomans`
    beside it is a product and has been divided. Rendering the pair with one
    Toman suffix printed the average cost ten times over and put two columns
    that cannot be multiplied out next to each other.
    """
    account = Account.objects.create(
        user=make_user(email="rialcost@test.test"), name="Test"
    )
    create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["kama_stock"],
        quantity=Decimal("100"),
        unit_price_tomans=Decimal("46348"),  # Rial, as TSE quotes it
        occurred_at=timezone.now() - dt.timedelta(days=200),
    )

    row = _position_metrics(account)["kama_stock"]

    assert row["average_cost_currency"] == "rial"
    assert Decimal(row["average_cost_tomans"]) == Decimal("46348")
    # The product converted; the unit price did not.
    assert Decimal(row["total_cost_basis_tomans"]) == Decimal("463480")

    gold = Account.objects.create(
        user=make_user(email="tomancost@test.test"), name="Gold"
    )
    create_ledger_entry(
        account=gold,
        kind=LedgerEntry.Kind.BUY,
        asset=asset_catalog["emami_coin"],
        quantity=Decimal("2"),
        unit_price_tomans=Decimal("500000000"),
        occurred_at=timezone.now() - dt.timedelta(days=200),
    )
    gold_row = _position_metrics(gold)["emami_coin"]
    assert gold_row["average_cost_currency"] == "toman"
    assert Decimal(gold_row["total_cost_basis_tomans"]) == Decimal("1000000000")


def test_reversed_deposit_is_fully_excluded_from_cashflows(make_user):
    account = _cash_account(make_user, "reversal@test.test", opened_days_ago=100)
    deposit = create_ledger_entry(
        account=account,
        kind=LedgerEntry.Kind.DEPOSIT,
        amount_tomans="500000",
        occurred_at=timezone.now() - dt.timedelta(days=50),
    )
    reverse_ledger_entry(user=account.user, account_id=account.id, entry_id=deposit.id)

    result = account_performance(account)

    assert result["performance_available"] is True
    # Both the deposit and its reversal must drop out, not just the reversal row.
    assert result["external_flow_count"] == 0
    assert abs(result["twr"]) < 1e-9


def test_xirr_non_convergence_returns_none_not_zero():
    d0 = dt.date(2020, 1, 1)
    # An outflow-only stream has no rate that zeroes the NPV: no root exists.
    cashflows = [(d0, Decimal("-1000")), (d0 + dt.timedelta(days=30), Decimal("-500"))]

    assert xirr(cashflows) is None


def test_usdt_denominated_current_value_matches_opening_denomination(make_user):
    from marketdata.models import GoldCurrencyHistory
    from portfolio.services.returns import to_jalali_str

    account = _cash_account(make_user, "usdt@test.test", opened_days_ago=100)
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT",
        date=to_jalali_str(timezone.now() - dt.timedelta(days=101)),
        close_price=Decimal("60000"),
    )

    result = account_performance(account, basis="usdt_denominated")

    assert result["performance_available"] is True
    # Regression guard for the ~10^5 bug: _current_value had no usdt_denominated
    # branch, so it returned raw Toman against a USDT-denominated opening value.
    # A flat cash balance in a consistent basis must show a near-zero TWR.
    assert abs(result["twr"]) < 10


# ----------------------------------------------------------------------
# test_returns_source_selection.py
# Returns source selection: warehouse daily series preferred, Price fallback.
# 
# The key regression test for the dual-source panel in
# portfolio/services/returns.py: an asset with >= MIN_DAILY_RETURNS warehouse
# rows must be priced from the warehouse; an asset without them must fall back
# to the live Price table; and warehouse writes must rotate the cache
# fingerprint.


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


def test_market_daily_bar_used_for_etf_nav_before_live_fallback(asset_catalog, write_prices):
    """ETF NAV must be priced from MarketDailyBar (real close, distilled from
    live snapshots -- open=first snapshot, close=last, not an average) rather
    than the live Price-tick fallback panel, and converted Rial->Toman like
    every other TSE-sourced column (ingest_etf_nav_snapshot stores it Rial;
    Tsetmc/Nav.php is a TSETMC-source endpoint using the same l18 convention
    as ordinary stocks, so it must be sourced from `MarketInstrument.Source.
    TSETMC`, not BRS -- an earlier version of this wiring queried BRS and
    silently never matched any ETF row at all).
    """
    etf = asset_catalog["kama_stock"]  # reuse any non-house asset as the ETF holding
    etf.tse_symbol = "اهرم"
    etf.save(update_fields=["tse_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="اهرم",
        category=MarketInstrument.Category.ETF,
        eligible=True,
    )
    _seed_market_daily_bars(MarketDailyBar.AssetClass.ETF_NAV, "اهرم", MIN_DAILY_RETURNS + 5, start_price=60000.0)
    # A single live tick that would otherwise seed the live-tick fallback --
    # must be ignored now that MarketDailyBar covers this symbol.
    write_prices({"kama_stock": 1000})

    panel, excluded, warnings = _load_price_panel(history_days=90, universe=["kama_stock"])
    series = panel["kama_stock"].dropna()
    assert len(series) >= MIN_DAILY_RETURNS
    # Bar closes rise 10/day from 60000 Rial -> Toman means the panel value
    # should track ~6000+, not the 60000+ raw Rial figure and not the single
    # 1000-priced live tick.
    assert 5900 < series.iloc[-1] < 6100


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


def test_scoped_fingerprint_tracks_only_panel_sources(asset_catalog):
    kama = asset_catalog["kama_stock"]
    emami = asset_catalog["emami_coin"]
    kama.tse_symbol, emami.tse_symbol = "کاما", "امامی"
    kama.save(update_fields=["tse_symbol"])
    emami.save(update_fields=["tse_symbol"])

    before = _price_version_fingerprint([kama.key])
    Price.objects.create(asset=emami, price=1, source="TEST")
    MarketCandle.objects.create(
        symbol="امامی", timeframe=MarketCandle.ADJUSTED,
        date_time="1403-01-01", close_price=1,
    )
    assert _price_version_fingerprint([kama.key]) == before

    Price.objects.create(asset=kama, price=1, source="TEST")
    assert _price_version_fingerprint([kama.key]) != before


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


# ----------------------------------------------------------------------
# test_quant_trust.py


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


def test_an_asset_with_no_observations_at_all_is_excluded_not_a_500():
    """An empty column must not reach the Jalali converter.

    `panel[key].dropna().index.max()` on a column with nothing in it returns
    `pandas.NaT`, which is not None, and whose .year/.month/.day are float nan.
    The guard used to be `is not None`, so NaT sailed through into
    `to_jalali_str` and jdatetime raised "TypeError: 'float' object cannot be
    interpreted as an integer" -- surfacing as a 500 on MyOptimal, which is how
    the e2e suite found it.

    The verdict must be `insufficient_history`, not `price_gap_exceeded`: an
    all-empty column is entirely leading gap, and `_gap_profile` defines a
    leading run as the absence of history rather than a hole in it. Calling it
    a gap is the data-corruption accusation that rule exists to avoid.
    """
    index = pd.date_range("2026-01-01", periods=45, tz="UTC")
    panel = pd.DataFrame(
        {
            "complete": np.linspace(100.0, 145.0, len(index)),
            "never_priced": np.full(len(index), np.nan),
        },
        index=index,
    )

    returns, excluded, _warnings = _build_returns_matrix(panel)

    assert "complete" in returns.columns
    assert "never_priced" not in returns.columns
    reasons = {item["key"]: item["reason"] for item in excluded}
    assert reasons["never_priced"] == "insufficient_history"


def test_an_empty_column_is_excluded_even_when_it_is_held():
    """The held-asset softening must not turn the crash case into a warning.

    A held asset with too little history is downgraded to a `short_history`
    warning and kept -- but only when it has at least 2 observations. With zero
    it has no returns to contribute at all, so it must still be excluded.
    """
    index = pd.date_range("2026-01-01", periods=45, tz="UTC")
    panel = pd.DataFrame(
        {
            "complete": np.linspace(100.0, 145.0, len(index)),
            "never_priced": np.full(len(index), np.nan),
        },
        index=index,
    )

    returns, excluded, _warnings = _build_returns_matrix(
        panel, held_keys=frozenset({"never_priced"})
    )

    assert "never_priced" not in returns.columns
    assert {item["key"] for item in excluded} == {"never_priced"}


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


# ----------------------------------------------------------------------
# The integrity gate excluded 79% of the tracked universe (1,119 of 1,410) on
# 2026-08-24, stripping those assets from the returns matrix, risk metrics and
# the optimizer. These four pin the causes, each measured against production.


def _seed_market(symbol, days, *, timeframe="1d_adj", volume=1):
    """Candles for `symbol` on `days`, plus the unadjusted market-wide calendar."""
    for day in days:
        jday = jdatetime.date.fromgregorian(date=day)
        stamp = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        MarketCandle.objects.create(
            symbol=symbol, timeframe=timeframe, date_time=stamp,
            open_price=100, high_price=100, low_price=100,
            close_price=100, volume=volume,
        )
        MarketCandle.objects.get_or_create(
            symbol="REFERENCE", timeframe="1d_unadj", date_time=stamp,
            defaults={"close_price": 100, "volume": 1},
        )


def _instrument(symbol):
    return MarketInstrument.objects.create(
        symbol=symbol, name=symbol,
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        eligible=True,
    )


@pytest.mark.django_db
def test_a_symbol_listed_midway_is_not_accused_of_missing_history():
    """The largest cause: 537 symbols failed purely for being newer than the window.

    A leading run of absent days is the absence of history, not a hole in it --
    the same rule `_build_returns_matrix._gap_profile` already applies. The two
    layers must agree, or the gate silently overrules the matrix.
    """
    _instrument("NEWLY_LISTED")
    start, end = dt.date(2026, 7, 20), dt.date(2026, 7, 29)
    # The market traded every day; this symbol only exists for the last four.
    _seed_market("OTHER", [start + dt.timedelta(days=n) for n in range(10)])
    listed = [end - dt.timedelta(days=n) for n in range(4)]
    _seed_market("NEWLY_LISTED", listed)

    result = compute_symbol_integrity("NEWLY_LISTED", start=start, end=end)

    assert result["passes_gate"] is True, result["reason_codes"]
    assert result["coverage_ratio"] == pytest.approx(1.0)
    assert result["history_start"] == min(listed).isoformat()
    assert result["leading_gap_sessions"] == 6


@pytest.mark.django_db
def test_a_couple_of_rejected_days_does_not_disqualify_a_symbol():
    """208 symbols were dropped over one or two bad rows in ~112 sessions.

    `MAX_REJECTION_RATIO` is 1%, which over a window this size means "at most one
    rejected day". Rejected rows are already excluded from the series, so a
    handful is a quality note, not grounds to refuse the asset entirely.
    """
    _instrument("SLIGHTLY_DIRTY")
    start, end = dt.date(2026, 7, 5), dt.date(2026, 7, 29)
    days = [start + dt.timedelta(days=n) for n in range(25)]
    _seed_market("SLIGHTLY_DIRTY", days)
    for day in days[:2]:
        jday = jdatetime.date.fromgregorian(date=day)
        RejectedRecord.objects.create(
            endpoint="stock_candle_adjusted", symbol="SLIGHTLY_DIRTY",
            date=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            reason="series_spike", payload={},
        )

    result = compute_symbol_integrity("SLIGHTLY_DIRTY", start=start, end=end)

    assert result["rejected_count"] == 2
    assert "excessive_rejections" not in result["reason_codes"]
    assert result["passes_gate"] is True, result["reason_codes"]


@pytest.mark.django_db
def test_a_halted_symbol_is_not_scored_as_a_data_gap():
    """A suspended stock has no candle to fetch, and never will.

    The provider pads a halted symbol exactly as it pads a market-wide closure:
    a DailyStockHistory row at the last price with zero volume and zero trades.
    Counting those as missing sessions is a permanent, unfixable failure for an
    asset whose data is fine.
    """
    _instrument("HALTED")
    start, end = dt.date(2026, 7, 15), dt.date(2026, 7, 29)
    days = [start + dt.timedelta(days=n) for n in range(15)]
    _seed_market("OTHER", days)
    traded = days[:4] + days[12:]
    _seed_market("HALTED", traded)

    for day in days:
        jday = jdatetime.date.fromgregorian(date=day)
        stamp = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
        halted = day not in traded
        DailyStockHistory.objects.create(
            symbol="HALTED", date=stamp, pc=100,
            tvol=0 if halted else 500, tno=0 if halted else 5,
        )
        # The rest of the market kept trading, so this is a halt, not a closure.
        DailyStockHistory.objects.create(
            symbol="OTHER", date=stamp, pc=100, tvol=900, tno=9,
        )

    result = compute_symbol_integrity("HALTED", start=start, end=end)

    assert result["halted_sessions"] == 8
    assert "price_gap_exceeded" not in result["reason_codes"]
    assert result["passes_gate"] is True, result["reason_codes"]


@pytest.mark.django_db
def test_a_genuine_interior_hole_still_fails_the_gate():
    """The guard on all of the above: forgiveness must not become blindness."""
    _instrument("BROKEN")
    start, end = dt.date(2026, 7, 15), dt.date(2026, 7, 29)
    days = [start + dt.timedelta(days=n) for n in range(15)]
    _seed_market("OTHER", days)
    _seed_market("BROKEN", days[:4] + days[12:])
    # The market traded on the missing days AND so did this symbol -- the rows
    # are simply not in the warehouse. Nothing forgives that.
    for day in days:
        jday = jdatetime.date.fromgregorian(date=day)
        DailyStockHistory.objects.create(
            symbol="BROKEN",
            date=f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}",
            pc=100, tvol=500, tno=5,
        )

    result = compute_symbol_integrity("BROKEN", start=start, end=end)

    assert result["halted_sessions"] == 0
    assert "price_gap_exceeded" in result["reason_codes"]
    assert result["passes_gate"] is False


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


# ----------------------------------------------------------------------
# test_signals.py
# Indicator arithmetic, the backtest's lookahead guard, and the integrity gate.
# 
# The arithmetic tests need no database: `portfolio.services.signals` is pure
# pandas over a price series, which is why it was written that way.


def _rising(n=300, step=0.002):
    return pd.Series(100 * (1.0 + pd.Series(np.full(n, step))).cumprod())


def test_rsi_saturates_at_the_extremes():
    """A series with no down days is RSI 100; no up days is RSI 0.

    This is the check that catches the common implementation error of using a
    rolling mean instead of Wilder's EWM smoothing, which never quite reaches
    either bound.
    """
    up = pd.Series(np.arange(1, 200, dtype=float))
    down = pd.Series(np.arange(200, 1, -1, dtype=float))
    assert float(sig.rsi(up).iloc[-1]) == pytest.approx(100.0, abs=0.01)
    assert float(sig.rsi(down).iloc[-1]) < 1.0


def test_rsi_of_a_flat_series_is_neutral_not_nan():
    """No gains AND no losses is 0/0. It must land on 50, not NaN, or every
    downstream comparison silently becomes False."""
    flat = pd.Series(np.full(120, 42.0))
    value = float(sig.rsi(flat).iloc[-1])
    assert not np.isnan(value)
    assert value == pytest.approx(50.0, abs=1e-6)


def test_macd_histogram_is_zero_for_a_flat_series():
    flat = pd.Series(np.full(150, 10.0))
    assert abs(float(sig.macd(flat)["histogram"].iloc[-1])) < 1e-9


def test_macd_histogram_turns_positive_on_an_upturn():
    """Fast EMA leads slow EMA out of a trough, so the histogram flips sign."""
    series = pd.concat([
        pd.Series(np.linspace(100, 60, 120)),   # decline
        pd.Series(np.linspace(60, 130, 120)),   # recovery
    ], ignore_index=True)
    hist = sig.macd(series)["histogram"]
    assert float(hist.iloc[-1]) > 0
    assert float(hist.iloc[110]) < 0


def test_price_index_reproduces_compounded_returns():
    r = pd.Series([0.10, -0.05, 0.02])
    got = float(sig.price_index_from_returns(r).iloc[-1])
    assert got == pytest.approx(100 * 1.10 * 0.95 * 1.02, abs=1e-9)


def test_describe_refuses_thin_history():
    """20 sessions of RSI is noise wearing a verdict; it must return nothing."""
    assert sig.describe(_rising(20)) is None
    assert sig.describe(_rising(sig.MIN_OBSERVATIONS + 5)) is not None


def test_describe_reports_which_trend_window_it_used():
    """`above_trend` means different things at 50 vs 200 sessions, so the window
    travels with the flag rather than being assumed by the reader."""
    short = sig.describe(_rising(120))
    long = sig.describe(_rising(400))
    assert short["trend_window"] == 50
    assert long["trend_window"] == 200


def test_a_steady_uptrend_reads_bullish():
    reading = sig.describe(_rising(400))
    assert reading["above_trend"] is True
    assert reading["macd_histogram"] > 0
    assert reading["stance"] == "bullish"


def test_a_steady_downtrend_reads_bearish():
    # A constant PERCENTAGE decline (pd.Series(...).cumprod()) is a pathological
    # fixture for MACD: the absolute daily drop shrinks as price shrinks, so
    # momentum-of-momentum (the histogram) legitimately turns positive well
    # before any reversal -- a real, narrow MACD property, not a bug. A steady
    # or worsening ABSOLUTE decline is what "downtrend" means here and is what
    # actually exercises the bearish path.
    drops = 0.05 + 0.001 * np.arange(400)
    falling = pd.Series(100 - np.cumsum(drops))
    reading = sig.describe(falling)
    assert reading["above_trend"] is False
    assert reading["macd_histogram"] < 0
    assert reading["stance"] == "bearish"


def test_backtest_does_not_peek_at_the_current_bar():
    """The position is decided from yesterday's closes.

    On a series that only ever rises, a crossover strategy MUST underperform
    buy-and-hold: it is flat until the fast average crosses up. A backtest that
    matched or beat it would be reading the bar it trades on -- the single most
    common way a strategy looks profitable and is not.
    """
    result = sig.backtest_crossover(_rising(500))
    assert result is not None
    assert result["strategy_return"] <= result["buy_and_hold_return"] + 1e-9
    assert 0.0 <= result["share_of_days_invested"] < 1.0


def test_backtest_refuses_a_window_it_cannot_fill():
    assert sig.backtest_crossover(_rising(50), fast=50, slow=200) is None


def test_backtest_counts_trades_and_bounds_drawdown():
    choppy = pd.Series(
        100 + 20 * np.sin(np.linspace(0, 12 * np.pi, 600))
    )
    result = sig.backtest_crossover(choppy, fast=20, slow=60)
    assert result is not None
    assert result["trades"] > 0
    assert -1.0 <= result["max_drawdown"] <= 0.0


@pytest.mark.django_db
def test_nightly_task_records_the_integrity_verdict_on_every_row():
    """A stance drawn from a gap-ridden series must carry that fact.

    1,072 of 1,346 symbols currently fail the gate while the archive backfills,
    so this flag is what stops the UI presenting those as actionable.
    """
    from unittest.mock import patch

    from marketdata import tasks
    from marketdata.models import AssetSignalSnapshot, MarketInstrument, SymbolIntegrity

    MarketInstrument.objects.create(
        source="tsetmc", symbol="GOOD", category="stock", eligible=True
    )
    MarketInstrument.objects.create(
        source="tsetmc", symbol="GAPPY", category="stock", eligible=True
    )
    SymbolIntegrity.objects.create(symbol="GOOD", passes_gate=True)
    SymbolIntegrity.objects.create(symbol="GAPPY", passes_gate=False)

    dates = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    returns = pd.DataFrame(
        {"GOOD": np.full(300, 0.002), "GAPPY": np.full(300, 0.002)}, index=dates
    )

    # The task imports daily_returns_matrix locally from portfolio.services.returns,
    # so patching it at the source is both necessary and sufficient.
    with patch("portfolio.services.returns.daily_returns_matrix",
               return_value=(returns, [])):
        tasks.nightly_asset_signals(window_days=365)

    rows = {r.symbol: r for r in AssetSignalSnapshot.objects.all()}
    assert set(rows) == {"GOOD", "GAPPY"}
    assert rows["GOOD"].passes_integrity is True
    assert rows["GAPPY"].passes_integrity is False
    # Both still get a stance; the gate labels it rather than hiding it.
    assert rows["GAPPY"].stance == "bullish"
    assert rows["GAPPY"].observations == 300


@pytest.mark.django_db
def test_nightly_task_is_idempotent_within_a_day():
    from unittest.mock import patch

    from marketdata import tasks
    from marketdata.models import AssetSignalSnapshot, MarketInstrument

    MarketInstrument.objects.create(
        source="tsetmc", symbol="ONE", category="stock", eligible=True
    )
    dates = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    returns = pd.DataFrame({"ONE": np.full(300, 0.001)}, index=dates)

    with patch("portfolio.services.returns.daily_returns_matrix",
               return_value=(returns, [])):
        tasks.nightly_asset_signals(window_days=365)
        tasks.nightly_asset_signals(window_days=365)

    assert AssetSignalSnapshot.objects.count() == 1


# ----------------------------------------------------------------------
# test_track_b.py


@pytest.mark.django_db
class TestTrackB:
    # We choose an integration test type here because validating data integrity, gate exclusion, and benchmark diagnostics requires component boundaries (models, database state, service functions, and celery tasks) to work in unison.

    def test_nightly_data_integrity_task(self, monkeypatch):
        """Verify the nightly Celery task computes and saves integrity metrics for eligible symbols."""
        calendar_calls = 0

        def market_calendar(**_kwargs):
            nonlocal calendar_calls
            calendar_calls += 1
            return {jdatetime.date.today().strftime("%Y-%m-%d")}

        monkeypatch.setattr("marketdata.integrity.actual_trading_days", market_calendar)

        # Create an eligible instrument
        instrument = MarketInstrument.objects.create(
            symbol="TEST_STOCK",
            name="Test Stock",
            source=MarketInstrument.Source.TSETMC,
            eligible=True
        )
        MarketInstrument.objects.create(
            symbol="TEST_STOCK_2",
            name="Second Test Stock",
            source=MarketInstrument.Source.TSETMC,
            eligible=True,
        )
        
        # Add some historical candles
        MarketCandle.objects.create(
            symbol="TEST_STOCK",
            timeframe="1d_adj",
            date_time=jdatetime.date.today().strftime("%Y-%m-%d"),
            open_price=100.0,
            high_price=110.0,
            low_price=90.0,
            close_price=105.0,
            volume=1000
        )
        
        # Run Celery task
        nightly_data_integrity()
        
        # Assert SymbolIntegrity row created
        integrity = SymbolIntegrity.objects.filter(symbol="TEST_STOCK").first()
        assert integrity is not None
        assert integrity.symbol == "TEST_STOCK"
        assert integrity.passes_gate is True
        # Once to build the shared session calendar for all symbols, once for
        # the market-wide outage sweep. The point of the guard is that neither
        # is per-symbol.
        assert calendar_calls == 2

    def test_integrity_gate_enforcement(self):
        """Verify that symbols failing the integrity gate are excluded from the daily returns matrix."""
        # Create active assets
        Asset.objects.all().delete()
        asset1 = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", is_active=True)
        asset2 = Asset.objects.create(key="fars_stock", name="Fars", tse_symbol="FARS", is_active=True)
        
        date_str = jdatetime.date.today().strftime("%Y-%m-%d")
        
        # Add candles
        MarketCandle.objects.create(
            symbol="KAMA", timeframe="1d_adj", date_time=date_str,
            open_price=100.0, high_price=100.0, low_price=100.0, close_price=100.0, volume=100
        )
        MarketCandle.objects.create(
            symbol="FARS", timeframe="1d_adj", date_time=date_str,
            open_price=200.0, high_price=200.0, low_price=200.0, close_price=200.0, volume=200
        )
        
        # Fail KAMA but pass FARS
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=False, reason="Low coverage ratio")
        SymbolIntegrity.objects.create(symbol="FARS", passes_gate=True, reason="")
        
        # Build daily returns matrix
        _, excluded = daily_returns_matrix()
        
        excluded_keys = {item["key"]: item for item in excluded}
        assert "kama_stock" in excluded_keys
        assert excluded_keys["kama_stock"]["reason"] == "integrity_gate_failed"
        assert excluded_keys["kama_stock"]["detail"] == "Low coverage ratio"

    @override_settings(HISTORICAL_BENCHMARK_ENABLED=True)
    def test_benchmark_diagnostics_calculation(self):
        """Verify that benchmark metrics are calculated when index data exists, and omitted when it is empty."""
        Asset.objects.all().delete()
        asset1 = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", is_active=True)

        weights = {"kama_stock": 1.0}
        
        # Without Index data, metrics should be omitted
        diag = portfolio_diagnostics(weights, Decimal("100000"))
        assert "beta" not in diag["metrics"]
        assert "alpha" not in diag["metrics"]
        
        # Add index data (Need at least 30 returns rows, so at least 31 price points)
        today_j = jdatetime.date.today()
        dates = [(today_j - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(40, 0, -1)]
        
        # Clean existing cache to force rebuild
        from django.core.cache import cache
        cache.clear()
        
        for i, dt_str in enumerate(dates):
            MarketIndexData.objects.create(
                date=dt_str,
                time="12:30:00",
                state="بسته",
                index_overall=1000000.0 + i * 10000.0,
                index_overall_change=10000.0,
            )
            # Create price for KAMA to have sufficient history
            MarketCandle.objects.create(
                symbol="KAMA",
                timeframe="1d_adj",
                date_time=dt_str,
                open_price=100.0 + i * 2.0,
                close_price=100.0 + i * 2.0,
                volume=1000
            )
            
        # Kama passes gate
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True, reason="")
        
        diag = portfolio_diagnostics(weights, Decimal("100000"))
        
        assert "beta" in diag["metrics"]
        assert "alpha" in diag["metrics"]
        assert "tracking_error" in diag["metrics"]
        assert "information_ratio" in diag["metrics"]


# ----------------------------------------------------------------------
# test_track_c.py


# Testing Protocol: We choose a mix of unit tests for pure currency deflation logic and integration tests for Django-Postgres query boundaries and cache validation to protect against database look-ahead leaks.

def test_to_basis_nominal_and_usd_real():
    # Unit test for checking Toman-nominal vs USD-real return conversions.
    series = pd.Series([1000.0, 1200.0, 1500.0])
    usd_series = pd.Series([50.0, 60.0, 75.0])
    
    # Nominal should be identical
    res_nom = to_basis(series, "nominal", usd_series=usd_series)
    pd.testing.assert_series_equal(res_nom, series)
    
    # USD real should be divided by exchange rate (1000/50=20, 1200/60=20, 1500/75=20)
    res_real = to_basis(series, "usd_real", usd_series=usd_series)
    expected = pd.Series([20.0, 20.0, 20.0])
    pd.testing.assert_series_equal(res_real, expected)


@pytest.mark.django_db
class TestTrackC:
    def test_look_ahead_guard(self):
        # Integration test verifying that a price spike inserted after as_of date is completely invisible in returns matrix.
        Asset.objects.all().delete()
        asset = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", is_active=True)
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True)
        
        # 60 days history to ensure > 30 returns before as_of (which is at index 45)
        today_j = jdatetime.date.today()
        dates = [(today_j - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(60, 0, -1)]
        
        for i, dt_str in enumerate(dates):
            MarketCandle.objects.create(
                symbol="KAMA",
                timeframe="1d_adj",
                date_time=dt_str,
                open_price=100.0 + i * 2.0,
                close_price=100.0 + i * 2.0,
                volume=1000
            )
            
        # Clear cache
        cache.clear()
        
        # Select an as_of date in the middle of history (index 45 leaves 46 price points = 45 returns before as_of)
        as_of_jalali = dates[45]
        parts = [int(p) for p in as_of_jalali.split("-")]
        greg_date = jdatetime.date(parts[0], parts[1], parts[2]).togregorian()
        as_of_dt = datetime.datetime(greg_date.year, greg_date.month, greg_date.day, 23, 59, 59, tzinfo=datetime.timezone.utc)
        
        # Get baseline returns matrix as of as_of_dt
        df_base, _ = daily_returns_matrix(as_of=as_of_dt)
        assert "kama_stock" in df_base.columns
        
        # Now introduce a massive price spike in the database AFTER as_of_dt
        post_spike_date = dates[55]
        candle = MarketCandle.objects.get(symbol="KAMA", timeframe="1d_adj", date_time=post_spike_date)
        candle.close_price = 999999.0
        candle.save()
        
        # Request returns matrix again with same as_of
        cache.clear()
        df_new, _ = daily_returns_matrix(as_of=as_of_dt)
        
        # The return values should be EXACTLY identical (the post-as_of spike is completely ignored)
        pd.testing.assert_frame_equal(df_base, df_new)

    def test_bulk_loader_and_survivorship(self):
        # Integration test verifying that _load_price_panel loads the requested universe, filtering by integrity gate, and applying survivorship guard.
        Asset.objects.all().delete()
        asset1 = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", is_active=True)
        asset2 = Asset.objects.create(key="fars_stock", name="Fars", tse_symbol="FARS", is_active=True)
        
        today_j = jdatetime.date.today()
        dates = [(today_j - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(40, 0, -1)]
        
        # Kama is healthy
        for i, dt_str in enumerate(dates):
            MarketCandle.objects.create(
                symbol="KAMA", timeframe="1d_adj", date_time=dt_str,
                open_price=100.0 + i * 2.0, close_price=100.0 + i * 2.0, volume=1000
            )
            
        # Fars is dead (no price updates in last 35 days, violates survivorship guard near as_of)
        for i, dt_str in enumerate(dates[:5]):
            MarketCandle.objects.create(
                symbol="FARS", timeframe="1d_adj", date_time=dt_str,
                open_price=200.0, close_price=200.0, volume=1000
            )
            
        # Both pass integrity gate
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="FARS", passes_gate=True)
        
        # Clear cache
        cache.clear()
        
        # Load panel as of today
        panel, excluded, _warnings = _load_price_panel(history_days=180, as_of=timezone.now(), universe=["kama_stock", "fars_stock"])
        
        assert "kama_stock" in panel.columns
        assert "fars_stock" not in panel.columns  # Fars failed survivorship guard
        
        excluded_keys = {item["key"]: item for item in excluded}
        assert "fars_stock" in excluded_keys
        assert excluded_keys["fars_stock"]["reason"] == "survivorship_guard_failed"

    def test_returns_and_opt_cache_key(self):
        # Integration test verifying that cache keys are isolated by as_of, universe, and basis.
        today_j = jdatetime.date.today()
        as_of_1 = (today_j - datetime.timedelta(days=5)).strftime("%Y-%m-%d")
        as_of_2 = (today_j - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        
        version = "test_version"
        
        # Cache keys should be completely different for different as_of
        key1 = _returns_cache_key(180, normalize_as_of(as_of_1), None, "nominal", version)
        key2 = _returns_cache_key(180, normalize_as_of(as_of_2), None, "nominal", version)
        assert key1 != key2
        
        # Cache keys should be different for different universe
        key3 = _returns_cache_key(180, None, ["kama_stock"], "nominal", version)
        key4 = _returns_cache_key(180, None, ["fars_stock"], "nominal", version)
        assert key3 != key4
        
        # Cache keys should be different for different basis
        key5 = _returns_cache_key(180, None, None, "nominal", version)
        key6 = _returns_cache_key(180, None, None, "usd_real", version)
        assert key5 != key6

    def test_candidate_universe(self):
        # Testing Protocol: We choose an integration test for the candidate universe resolver because it queries multiple DB tables (MarketInstrument, SymbolIntegrity, MarketCandle) and calculates statistical metrics (median daily volume/turnover) to filters assets.
        from marketdata.universe import get_candidate_universe

        MarketInstrument.objects.all().delete()
        SymbolIntegrity.objects.all().delete()
        MarketCandle.objects.all().delete()

        # Create eligible and non-eligible instruments
        mi1 = MarketInstrument.objects.create(source="tsetmc", symbol="SHAFN", name="Shafn", category="stock", eligible=True)
        mi2 = MarketInstrument.objects.create(source="tsetmc", symbol="ILIZ", name="Iliz", category="stock", eligible=False)

        SymbolIntegrity.objects.create(symbol="SHAFN", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="ILIZ", passes_gate=True)

        # Seed candles for SHAFN to satisfy liquidity (MIN_MEDIAN_DAILY_VOLUME = 1000, MIN_MEDIAN_DAILY_TURNOVER_TOMANS = 50000000)
        # close_price is a warehouse value, i.e. raw Rial: 100_000 Rial =
        # 10_000 Toman, so turnover is 10_000 x 10_000 = 100M Toman, clearing
        # the 50M threshold the resolver screens against.
        # Seed 40 days to exceed MIN_DAILY_RETURNS = 30
        today_j = jdatetime.date.today()
        dates = [(today_j - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(40, 0, -1)]

        for dt_str in dates:
            MarketCandle.objects.create(
                symbol="SHAFN", timeframe="1d_adj", date_time=dt_str,
                close_price=100000.0, open_price=100000.0, volume=10000
            )

        candidates, excluded = get_candidate_universe()
        assert "SHAFN" in candidates
        assert "ILIZ" not in candidates  # Eligible is False

    def test_auto_provision_asset(self):
        # Testing Protocol: We choose an integration test for asset auto-provisioning because it validates that our on-demand Asset creation pipeline cleanly runs Asset.full_clean() and maps properties from the verified MarketInstrument model.
        from portfolio.services.trades import execute_trade, provision_asset
        from portfolio.models import Account
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(email="pv@example.com", password="password")
        account = Account.objects.create(user=user, name="Main")

        MarketInstrument.objects.all().delete()
        mi = MarketInstrument.objects.create(source="tsetmc", symbol="KAMA", name="Kama Stock", category="stock", eligible=True)

        # Provision asset should succeed and save it in catalog
        asset = provision_asset("KAMA")
        assert asset.key == "kama_stock"
        assert asset.name == "Kama Stock"
        assert asset.tse_symbol == "KAMA"
        assert Asset.objects.filter(key="kama_stock").exists()


# ----------------------------------------------------------------------
# test_track_d.py


User = get_user_model()

class TestTrackD(APITestCase):

    def setUp(self):
        # Create users
        self.pro_group, _ = Group.objects.get_or_create(name="Pro")
        
        self.pro_user = User.objects.create_user(email="pro@example.com", password="password")
        self.pro_user.groups.add(self.pro_group)
        
        self.free_user = User.objects.create_user(email="free@example.com", password="password")
        
        # Setup Account
        self.pro_account = Account.objects.create(user=self.pro_user, name="Pro Main")
        self.free_account = Account.objects.create(user=self.free_user, name="Free Main")
        
        # Seed simple active assets
        self.kama = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", asset_class="Stock", is_active=True)
        self.usd = Asset.objects.create(key="usd_cash", name="USD", brs_symbol="USD", asset_class="Cash", is_active=True)
        self.emami = Asset.objects.create(key="emami_coin", name="Emami", brs_symbol="Emami", asset_class="Gold", is_active=True)
        
        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="USD", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="Emami", passes_gate=True)

    # `/api/performance/` is now account-scoped and ledger-derived; its contract
    # lives in tests/test_ledger_api.py (opening baseline, external cash flows,
    # account scoping). The old snapshot-delta assertions were removed with it.

    def test_data_integrity_endpoint(self):
        # Testing Protocol: We choose an integration test for the integrity API endpoint to verify that only authenticated staff users can access data quality reports.
        self.client.force_authenticate(user=self.pro_user)
        res = self.client.get(reverse("integrity"))
        assert res.status_code == status.HTTP_403_FORBIDDEN
        
        # Authenticate as staff
        self.pro_user.is_staff = True
        self.pro_user.save()
        
        res = self.client.get(reverse("integrity"))
        assert res.status_code == status.HTTP_200_OK
        # We seeded 3 SymbolIntegrity rows in setUp
        assert len(res.data["integrity"]) == 3


def test_a_tether_quoted_bar_converts_at_the_rate_of_its_own_day(db):
    """Crypto and commodity closes are the provider's foreign numbers. Converting
    the whole series at today's dollar rate would be a scalar multiple -- correct
    for magnitude and wrong for returns, because it flattens out every move the
    rial itself made. The holder lived through the Toman series.
    """
    from decimal import Decimal

    from marketdata.models import GoldCurrencyHistory, MarketDailyBar, MarketSnapshot
    from marketdata.provenance import daily_bar_price

    from marketdata.models import MarketInstrument
    from portfolio.models import Asset

    coin = Asset.objects.create(
        key="btc-panel", name="Bitcoin", asset_class=Asset.AssetClass.CRYPTO,
        brs_symbol="BTC", is_active=True,
    )
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="BTC", name="Bitcoin",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )
    MarketSnapshot.objects.create(
        asset_class="crypto", symbol="BTC", observed_at=timezone.now(),
        last_price=Decimal("1"), provider_payload={"unit": "تتر"},
    )
    for date, close in (("1404-01-01", "10"), ("1404-01-02", "10")):
        MarketDailyBar.objects.create(
            asset_class=MarketDailyBar.AssetClass.CRYPTO, symbol="BTC", date=date,
            open_price=Decimal(close), high_price=Decimal(close),
            low_price=Decimal(close), close_price=Decimal(close),
        )
    # The coin did not move; the dollar did.
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1404-01-01", close_price=Decimal("50000"), unit="تومان",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1404-01-02", close_price=Decimal("60000"), unit="تومان",
    )

    rows = dict(
        (date, price) for _symbol, date, price in daily_bar_price([coin])
    )
    assert rows == {
        "1404-01-01": Decimal("500000"),
        "1404-01-02": Decimal("600000"),
    }


def test_a_bar_with_no_dollar_rate_yet_yields_no_row(db):
    """A gap the coverage gate can see beats a day priced in the wrong currency."""
    from decimal import Decimal

    from marketdata.models import GoldCurrencyHistory, MarketDailyBar, MarketSnapshot
    from marketdata.provenance import daily_bar_price

    from marketdata.models import MarketInstrument
    from portfolio.models import Asset

    coin = Asset.objects.create(
        key="btc-panel", name="Bitcoin", asset_class=Asset.AssetClass.CRYPTO,
        brs_symbol="BTC", is_active=True,
    )
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="BTC", name="Bitcoin",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )
    MarketSnapshot.objects.create(
        asset_class="crypto", symbol="BTC", observed_at=timezone.now(),
        last_price=Decimal("1"), provider_payload={"unit": "تتر"},
    )
    for date in ("1404-01-01", "1404-01-05"):
        MarketDailyBar.objects.create(
            asset_class=MarketDailyBar.AssetClass.CRYPTO, symbol="BTC", date=date,
            open_price=Decimal("10"), high_price=Decimal("10"),
            low_price=Decimal("10"), close_price=Decimal("10"),
        )
    # The rate series starts after the first bar.
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1404-01-03", close_price=Decimal("50000"), unit="تومان",
    )

    rows = daily_bar_price([coin])
    assert [date for _symbol, date, _price in rows] == ["1404-01-05"]


@pytest.mark.django_db
def test_resolve_universe_does_not_query_per_symbol(django_assert_num_queries):
    """The universe branch must batch its MarketInstrument lookups.

    `resolve_universe` used to issue one query per symbol that is not already a
    catalog asset, and a second `iexact` query for each one that missed. The
    market-wide universe is ~1,900 symbols and `daily_returns_matrix` -- which
    every analytics surface routes through -- calls this, so a single request
    could issue thousands of queries.

    The bound below is deliberately a small constant rather than an exact
    number: the point is that it does not scale with the size of the universe.
    """
    from portfolio.services.returns import resolve_universe

    symbols = [f"SYM{i:03d}" for i in range(40)]
    MarketInstrument.objects.bulk_create([
        MarketInstrument(symbol=s, source=MarketInstrument.Source.TSETMC)
        for s in symbols
    ])

    with django_assert_num_queries(2):
        resolved = resolve_universe(symbols)

    assert len(resolved) == len(symbols)
    assert {item["symbol"] for item in resolved} == set(symbols)
    assert all(item["source"] == "tse" for item in resolved)


@pytest.mark.django_db
def test_asset_class_map_does_not_query_per_symbol(django_assert_num_queries):
    """`asset_class_map` batches too -- it is the twin of the resolve_universe N+1.

    It runs on the optimizer path over the same market-wide universe, and had
    the same shape: one MarketInstrument query per resolved entry.
    """
    from portfolio.services.classification import asset_class_map

    symbols = [f"CLS{i:03d}" for i in range(40)]
    MarketInstrument.objects.bulk_create([
        MarketInstrument(symbol=s, source=MarketInstrument.Source.TSETMC)
        for s in symbols
    ])

    with django_assert_num_queries(3):
        mapping = asset_class_map(symbols)

    assert set(mapping) == set(symbols)
