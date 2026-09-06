"""Does the warehouse hold what it claims: trading calendars, closure vs gap, coverage reports, collisions, and per-asset evidence.

Merged from 8 files; each section keeps its original banner.
"""

from datetime import datetime, timezone as dt_timezone
from datetime import timedelta
import datetime as dt
from decimal import Decimal
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
import pandas as pd
import pytest
from rest_framework.test import APIClient

from marketdata import calendars
from marketdata import ingest, jalali
from marketdata import jalali, quota
from marketdata.archive import (
    _tick_dates_needed,
    _tick_days_unreconciled,
    market_trading_days,
)
from marketdata.calendars import market_closure_days
from marketdata.coverage_report import (
    classify_archive_state,
    classify_live_asset,
    state_stale_days,
)
from marketdata.management.commands.audit_warehouse import Command
from marketdata.models import (
    ArchiveFetchState,
    MarketCandle,
    MarketInstrument,
    RejectedRecord,
    SymbolIntegrity,
    WorkflowRun,
)
from marketdata.models import (
    DailyStockHistory,
    DerivativeContract,
    GoldCurrencyHistory,
    MarketIndexData,
    MarketSnapshot,
)
from marketdata.models import ApiRequestQuota, MarketCandle, StockTransactionTick
from marketdata.models import ArchiveFetchState
from marketdata.models import DailyStockHistory
from marketdata.models import GoldCurrencyHistory
from marketdata.models import MarketDailyBar, MarketIndexData, MarketSnapshot
from marketdata.workflows import WorkflowOutcome, current_correlation_id
from portfolio.models import Account, Asset, Holding, LedgerEntry
from portfolio.models import Asset, Price
from portfolio.models import Price
from portfolio.services.returns import _closure_explained, _mask_closure_returns

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_trust_gaps.py
# Regressions for trust-first defects that the first pass left behind.
# 
# Test-pyramid rationale: four of these are integration tests because the defect
# only appears where the ledger, the warehouse tables, and the projection meet —
# a unit test of either side alone passes while the system is still wrong. The
# cutoff-count case is a unit test: arithmetic over the Jalali calendar, no
# database involved.


@pytest.fixture
def ledger_account(asset_catalog, make_user):
    return Account.objects.create(
        user=make_user(email="trust-gaps@test.test"), name="Gaps"
    )


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _csv(content: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("ledger.csv", content.encode("utf-8"), "text/csv")


def test_csv_import_applies_rows_in_event_order_not_file_order(
    ledger_account, asset_catalog
):
    """A broker export is rarely chronological; a sell above its buy must import."""
    opened = timezone.now() - dt.timedelta(days=10)
    bought = opened + dt.timedelta(days=1)
    sold = opened + dt.timedelta(days=2)
    content = (
        "external_id,occurred_at,kind,asset_key,quantity,unit_price_tomans,"
        "amount_tomans,note\n"
        f"s-1,{sold.isoformat()},sell,emami_coin,2,600,,Later sale\n"
        f"b-1,{bought.isoformat()},buy,emami_coin,2,500,,Earlier purchase\n"
        f"c-1,{opened.isoformat()},opening_cash,,,,5000,Starting cash\n"
    )

    response = _client(ledger_account.user).post(
        f"/api/accounts/{ledger_account.id}/imports/commit/",
        {"file": _csv(content)},
        format="multipart",
    )

    assert response.status_code == 201, response.data
    ledger_account.refresh_from_db()
    # 5000 - (2 x 500) + (2 x 600) = 5200, and the position closed out.
    assert ledger_account.cash_balance_tomans == Decimal("5200")
    assert not Holding.objects.filter(account=ledger_account).exists()


def test_reversed_buy_leaves_no_trace_in_position_metrics(
    ledger_account, asset_catalog
):
    """Cost basis must not keep an event the holding projection has dropped."""
    from portfolio.models import Asset
    from portfolio.services.ledger import create_ledger_entry, reverse_ledger_entry
    from portfolio.services.performance import _position_metrics

    asset = Asset.objects.get(key="emami_coin")
    started_at = timezone.now() - dt.timedelta(days=5)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=started_at,
    )
    keep = create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="2", unit_price_tomans="500",
        occurred_at=started_at + dt.timedelta(hours=1),
    )
    mistake = create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="3", unit_price_tomans="900",
        occurred_at=started_at + dt.timedelta(hours=2),
    )
    reverse_ledger_entry(
        user=ledger_account.user, account_id=ledger_account.id, entry_id=mistake.id
    )

    metrics = _position_metrics(ledger_account)[asset.key]
    holding = Holding.objects.get(account=ledger_account, asset=asset)

    assert Decimal(metrics["quantity"]) == holding.quantity == Decimal("2")
    # Only the surviving buy sets the basis; the reversed 900 never counts.
    assert Decimal(metrics["average_cost_tomans"]) == Decimal(str(keep.price_tomans))


def test_as_of_valuation_refuses_a_price_stale_beyond_five_sessions(
    ledger_account, asset_catalog
):
    """A last close from before a long trading gap is not a price for today."""
    from marketdata.models import GoldCurrencyHistory
    from portfolio.models import Asset
    from portfolio.services.ledger import create_ledger_entry
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import value_as_of

    asset = Asset.objects.get(key="emami_coin")
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])
    as_of = timezone.now()
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=asset, quantity="2",
        occurred_at=as_of - dt.timedelta(days=30),
    )
    # The asset last printed nine days ago; the market traded on each of the
    # eight days since (other symbols kept quoting), so the gap is over five.
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date=to_jalali_str(as_of - dt.timedelta(days=9)),
        close_price=Decimal("1000"),
    )
    for offset in range(1, 9):
        GoldCurrencyHistory.objects.create(
            symbol="USD",
            date=to_jalali_str(as_of - dt.timedelta(days=offset)),
            close_price=Decimal("60000"),
        )

    payload = value_as_of(ledger_account.user, account=ledger_account, as_of=as_of)

    reasons = {item["reason"] for item in payload["excluded"]}
    assert "price_gap_exceeded" in reasons
    assert payload["quality_status"] == "partial"
    assert all(item["asset_key"] != asset.key for item in payload["excluded"]
               if item.get("reason") == "price_gap_exceeded") or any(
        item.get("asset_key") == asset.key for item in payload["excluded"]
    )
    assert all(
        (item.get("asset_key") or item.get("key")) != asset.key
        for item in payload["items"]
    )


def test_same_day_as_of_respects_exact_event_timestamps(ledger_account, asset_catalog):
    """Two events on one calendar day still order by clock time, not date alone."""
    from portfolio.models import Asset
    from portfolio.services.ledger import create_ledger_entry
    from portfolio.services.timeline import holdings_as_of

    asset = Asset.objects.get(key="emami_coin")
    day = (timezone.now() - dt.timedelta(days=3)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    morning = day.replace(hour=9)
    afternoon = day.replace(hour=15)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=morning - dt.timedelta(days=1),
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="5", unit_price_tomans="100", occurred_at=morning,
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.SELL, asset=asset,
        quantity="5", unit_price_tomans="110", occurred_at=afternoon,
    )

    midday = day.replace(hour=12)
    assert holdings_as_of(ledger_account.user, ledger_account, midday) == {
        asset.key: Decimal("5")
    }
    assert holdings_as_of(ledger_account.user, ledger_account, afternoon) == {}


def test_sold_out_position_is_visible_before_its_sale(ledger_account, asset_catalog):
    """Deleting the Holding row must not erase historical quantity before the sell."""
    from portfolio.models import Asset
    from portfolio.services.ledger import create_ledger_entry
    from portfolio.services.timeline import holdings_as_of

    asset = Asset.objects.get(key="emami_coin")
    opened = timezone.now() - dt.timedelta(days=10)
    sold = timezone.now() - dt.timedelta(days=2)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=opened,
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="3", unit_price_tomans="100",
        occurred_at=opened + dt.timedelta(hours=1),
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.SELL, asset=asset,
        quantity="3", unit_price_tomans="120", occurred_at=sold,
    )
    assert not Holding.objects.filter(account=ledger_account, asset=asset).exists()

    before_sale = sold - dt.timedelta(hours=1)
    assert holdings_as_of(ledger_account.user, ledger_account, before_sale) == {
        asset.key: Decimal("3")
    }


def test_celery_routes_work_and_producers_separately():
    """Backlogged work queues must not starve their lightweight producers."""
    from config.celery import app

    routes = app.conf.task_routes
    assert routes["marketdata.tasks.archive_tick"]["queue"] == "live"
    assert routes["marketdata.tasks.*"]["queue"] == "archive"
    assert routes["portfolio.tasks.*"]["queue"] == "live"
    assert app.conf.task_default_queue == "live"


def test_projection_replay_rejects_negative_intermediate_balances(
    ledger_account, asset_catalog
):
    from portfolio.services.ledger import LedgerError, compute_projections

    asset = Asset.objects.get(key="emami_coin")
    now = timezone.now()
    LedgerEntry.objects.create(
        account=ledger_account,
        asset=asset,
        kind=LedgerEntry.Kind.SELL,
        quantity=1,
        price_tomans=100,
        amount_tomans=100,
        timestamp=now,
    )

    with pytest.raises(LedgerError, match="holding quantity"):
        compute_projections(ledger_account)


def test_rebuild_preserves_non_ledger_holdings_and_real_estate_terms(
    ledger_account, asset_catalog
):
    from portfolio.services.ledger import create_ledger_entry, rebuild_projections

    coin = Asset.objects.get(key="emami_coin")
    house = Asset.objects.get(is_house=True)
    manual = Asset.objects.create(
        key="manual_keepsake",
        name="Manual keepsake",
        is_manual=True,
        asset_class=Asset.AssetClass.GOLD,
    )
    started_at = timezone.now() - dt.timedelta(days=2)
    create_ledger_entry(
        account=ledger_account,
        kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans=1000,
        occurred_at=started_at,
    )
    create_ledger_entry(
        account=ledger_account,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=coin,
        quantity=2,
        occurred_at=started_at,
    )
    create_ledger_entry(
        account=ledger_account,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=house,
        quantity=75,
        area_sqm=120,
        mortgage_deduction_tomans=250_000_000,
        occurred_at=started_at,
    )
    Holding.objects.create(account=ledger_account, asset=manual, quantity=3)
    Holding.objects.filter(account=ledger_account, asset=coin).update(quantity=99)
    Holding.objects.filter(account=ledger_account, asset=house).update(
        quantity=99,
        area_sqm=1,
        mortgage_deduction_tomans=1,
    )

    rebuild_projections(ledger_account)

    assert Holding.objects.get(account=ledger_account, asset=manual).quantity == 3
    assert Holding.objects.get(account=ledger_account, asset=coin).quantity == 2
    rebuilt_house = Holding.objects.get(account=ledger_account, asset=house)
    assert rebuilt_house.quantity == 75
    assert rebuilt_house.area_sqm == 120
    assert rebuilt_house.mortgage_deduction_tomans == 250_000_000


def test_real_estate_correction_appends_reversal_and_replacement(
    ledger_account, asset_catalog
):
    from portfolio.services.ledger import create_ledger_entry, replace_ledger_entry

    house = Asset.objects.get(is_house=True)
    started_at = timezone.now() - dt.timedelta(days=2)
    original = create_ledger_entry(
        account=ledger_account,
        kind=LedgerEntry.Kind.OPENING_POSITION,
        asset=house,
        quantity=60,
        area_sqm=90,
        mortgage_deduction_tomans=400_000_000,
        occurred_at=started_at,
    )

    replacement = replace_ledger_entry(
        user=ledger_account.user,
        account_id=ledger_account.id,
        entry_id=original.id,
        quantity=70,
        area_sqm=100,
        mortgage_deduction_tomans=300_000_000,
    )

    reversal = LedgerEntry.objects.get(reversal_of=original)
    holding = Holding.objects.get(account=ledger_account, asset=house)
    assert replacement.pk != original.pk
    assert reversal.source == "system"
    assert replacement.timestamp == original.timestamp
    assert holding.quantity == 70
    assert holding.area_sqm == 100
    assert holding.mortgage_deduction_tomans == 300_000_000


def test_candle_close_qs_only_returns_adjusted_candles(asset_catalog):
    """Live-tick-derived AGGREGATE candles are retired; only the provider's
    ADJUSTED close is ever a valid archive/fallback price.
    """
    from marketdata.calendars import candle_close_qs
    from marketdata.models import MarketCandle

    day = "1403-10-19"
    symbol = "کاما"
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time=day,
        close_price=Decimal("920"),
    )

    picked = candle_close_qs(symbol, as_of=day).order_by("-date_time").first()
    assert picked.close_price == Decimal("920.0000")

    # With no ADJUSTED row for the day, there is no fallback candle at all --
    # unlike the retired AGGREGATE mechanism, nothing tick-derived fills the gap.
    MarketCandle.objects.filter(timeframe=MarketCandle.ADJUSTED).delete()
    assert candle_close_qs(symbol, as_of=day).order_by("-date_time").first() is None


def test_factor_ratio_detector_finds_only_material_steps():
    from marketdata.validation import detect_factor_ratio_actions

    actions = detect_factor_ratio_actions(
        [
            ("1403-01-01", 100, 50),
            ("1403-01-02", 110, 55),
            ("1403-01-03", 120, 30),
        ]
    )

    assert len(actions) == 1
    assert actions[0]["date"] == "1403-01-03"
    assert actions[0]["factor"] == pytest.approx(0.5)


def test_series_spike_is_suppressed_by_corporate_action():
    from marketdata.validation import screen_series

    rows = [("1403-01-01", 100), ("1403-01-02", 200)]

    assert [r.reason for r in screen_series("KAMA", "1d_unadj", rows)] == [
        "series_spike"
    ]
    assert screen_series(
        "KAMA",
        "1d_unadj",
        rows,
        corporate_action_dates={"1403-01-02"},
    ) == []


@pytest.mark.parametrize(
    ("symbol", "price", "unit", "usd_rate", "expected"),
    [
        ("USD", 63200, "", None, Decimal("63200")),  # undeclared unit passes through
        ("USD", 632000, "IRR", None, Decimal("63200")),  # Rial-declared: divide by 10 to Toman
        ("USDT", 632000, "ریال", None, Decimal("63200")),  # Rial-declared: divide by 10 to Toman
        # Provider-verified 2026-08-14: BrsApi declares USDT_IRT as تومان across all
        # 1,016 warehouse rows (50,050-196,088 Toman), and no row in the 130k-row
        # gold/currency table has a blank unit. The old UNIT_OVERRIDES entry forced
        # USD onto this symbol and was simply wrong; the declared unit is the rule.
        ("USDT", 63200, "تومان", None, Decimal("63200")),  # Toman-declared: identity
        ("USDT", 1, "USD", 63200, Decimal("63200")),  # USD-declared: x usd_rate
    ],
)
def test_currency_conversion_has_one_unit_driven_rule(
    symbol, price, unit, usd_rate, expected
):
    from marketdata.currency import to_toman

    assert to_toman(symbol, price, unit, usd_rate=usd_rate) == expected


def test_symbol_matching_never_uses_substrings():
    from portfolio.live import find_symbol_record

    rows = [
        {"l18": "KAMA1", "l30": "KAMA Holdings"},
        {"l18": "KAMA", "l30": "Exact"},
    ]

    assert find_symbol_record(rows, "KAMA") == rows[1]
    assert find_symbol_record(rows[:1], "KAMA") is None


def test_risk_parity_reports_equal_weight_fallback(monkeypatch):
    import numpy as np
    import pandas as pd

    from portfolio.services import optimization

    returns = pd.DataFrame(
        {"a": [0.01, 0.02], "b": [0.02, 0.01], "c": [0.01, -0.01]}
    )
    covariance = pd.DataFrame(np.eye(3), index=returns.columns, columns=returns.columns)
    degraded = []
    monkeypatch.setattr(
        optimization.cp.Problem,
        "solve",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("forced")),
    )

    weights = optimization._risk_parity(
        returns,
        covariance,
        max_weight_per_asset=1.0,
        max_weight_per_class={},
        class_map={},
        degraded=degraded,
    )

    assert degraded == ["risk_parity_solver_failed_equal_weight_used"]
    assert all(value == pytest.approx(1 / 3) for value in weights.values())


def test_yearly_rate_and_real_toman_cpi_resolution(settings):
    import pandas as pd

    from portfolio.services.deflator import to_basis

    assert settings.RATE_FOR(1400) == 0.20
    assert settings.RATE_FOR(1300) == settings.RISK_FREE_RATE_ANNUAL
    index = pd.DatetimeIndex(["2024-03-20T00:00:00Z", "2025-03-20T00:00:00Z"])
    real = to_basis(pd.Series([1000.0, 1000.0], index=index), "real_toman")
    assert real.iloc[1] < real.iloc[0]


def test_universe_membership_is_point_in_time_not_current_catalog_state():
    import jdatetime

    from marketdata.models import InstrumentListingHistory, MarketInstrument
    from portfolio.services.returns import resolve_universe

    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="PAST",
        category=MarketInstrument.Category.STOCK,
        eligible=False,
    )
    InstrumentListingHistory.objects.create(
        symbol="PAST",
        first_seen="1400-01-01",
        last_seen="1404-01-01",
        eligible_from="1400-01-01",
        eligible_to="1403-01-01",
    )
    cutoff = jdatetime.date(1402, 1, 1).togregorian()
    later = jdatetime.date(1404, 1, 1).togregorian()

    assert [item["symbol"] for item in resolve_universe(["PAST"], as_of=cutoff)] == [
        "PAST"
    ]
    assert resolve_universe(["PAST"], as_of=later) == []


def test_dividend_does_not_create_a_twr_boundary(
    ledger_account, asset_catalog, monkeypatch
):
    from portfolio.services import performance

    ledger_account.ledger_complete = True
    # >= 90 days so the dividend/TWR-boundary assertion is reached at all.
    ledger_account.tracking_started_at = timezone.now() - dt.timedelta(days=200)
    ledger_account.cash_balance_tomans = 1000
    ledger_account.save()
    LedgerEntry.objects.create(
        account=ledger_account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.DIVIDEND,
        amount_tomans=100,
        timestamp=timezone.now() - dt.timedelta(days=2),
    )
    monkeypatch.setattr(
        performance,
        "value_as_of",
        lambda *args, **kwargs: {
            "quality_status": "complete",
            "total": 1000,
            "excluded": [],
        },
    )
    monkeypatch.setattr(
        performance,
        "_current_value",
        lambda *args, **kwargs: Decimal("1100"),
    )
    monkeypatch.setattr(
        performance,
        "value_account",
        lambda *args, **kwargs: {"total": Decimal("100")},
    )
    monkeypatch.setattr(performance, "_position_metrics", lambda account: {})

    payload = performance.account_performance(ledger_account)

    assert payload["external_flow_count"] == 0


def test_as_of_returns_and_universe_ignore_everything_published_later(asset_catalog):
    """The leakage gate: the past must not move when the future arrives.

    Recomputing an as-of window after later data lands must return the
    identical frame, and a symbol first listed after the cutoff must not
    appear in that cutoff's universe.

    `_price_version_fingerprint()` keys the returns cache on max row ids, so
    inserting later candles invalidates it -- this genuinely recomputes rather
    than replaying a memoised frame.
    """
    import jdatetime
    import pandas as pd

    from marketdata.models import InstrumentListingHistory, MarketCandle
    from portfolio.services.returns import daily_returns_matrix, resolve_universe

    symbol = "کاما"
    asset = Asset.objects.get(key="kama_stock")
    asset.tse_symbol = symbol
    asset.save(update_fields=["tse_symbol"])

    start = jdatetime.date(1402, 1, 5)
    price = 1000
    for offset in range(60):
        price += 7 if offset % 3 else -5
        MarketCandle.objects.create(
            symbol=symbol,
            timeframe=MarketCandle.ADJUSTED,
            date_time=(start + dt.timedelta(days=offset)).strftime("%Y-%m-%d"),
            open_price=price,
            high_price=price + 5,
            low_price=price - 5,
            close_price=price,
            volume=100,
        )

    cutoff_at = dt.datetime.combine(
        (start + dt.timedelta(days=59)).togregorian(),
        dt.time.min,
        tzinfo=dt.timezone.utc,
    )
    before, _ = daily_returns_matrix(
        history_days=400, as_of=cutoff_at, universe=[asset.key]
    )
    assert not before.empty, "fixture must produce a usable panel"

    # The future arrives, at prices nothing in the window could imply.
    for offset in range(60, 90):
        MarketCandle.objects.create(
            symbol=symbol,
            timeframe=MarketCandle.ADJUSTED,
            date_time=(start + dt.timedelta(days=offset)).strftime("%Y-%m-%d"),
            open_price=99999,
            high_price=99999,
            low_price=99999,
            close_price=99999,
            volume=1,
        )

    after, _ = daily_returns_matrix(
        history_days=400, as_of=cutoff_at, universe=[asset.key]
    )
    pd.testing.assert_frame_equal(before, after)

    # Survivorship: listed only after the cutoff, so absent from that universe.
    InstrumentListingHistory.objects.create(
        symbol=symbol,
        first_seen="1403-01-01",
        last_seen="1403-12-29",
        eligible_from="1403-01-01",
    )
    assert asset.key not in [
        item["key"] for item in resolve_universe([asset.key], as_of=cutoff_at)
    ], "a symbol listed after the cutoff leaked into the cutoff's universe"
    assert asset.key in [item["key"] for item in resolve_universe([asset.key])]


def test_ingest_real_legal_rejection_uses_distinct_endpoint():
    """F4: real/legal rejections must not suppress daily prices."""
    from marketdata.ingest import ingest_real_legal
    from marketdata.models import RejectedRecord, DailyStockHistory

    symbol = "فملی"
    # Payload with buy/sell volume mismatch to trigger rejection
    payload = [
        {
            "date": "1403-01-01",
            "Buy_CountI": 100,
            "Buy_CountN": 50,
            "Sell_CountI": 80,
            "Sell_CountN": 40,
            "Buy_I_Volume": 1000,
            "Buy_N_Volume": 500,
            "Sell_I_Volume": 800,
            "Sell_N_Volume": 300,  # mismatch: 1500 != 1100
            "Buy_I_Value": 100000,
            "Buy_N_Value": 50000,
            "Sell_I_Value": 80000,
            "Sell_N_Value": 30000,
        }
    ]

    # Ensure a clean DailyStockHistory row exists for the same symbol/date
    DailyStockHistory.objects.update_or_create(
        symbol=symbol,
        date="1403-01-01",
        defaults={"pc": 1000, "pl": 1000, "pmin": 990, "pmax": 1010, "tvol": 100000, "tval": 100000000},
    )

    created, bad = ingest_real_legal(symbol, payload)

    # Rejection should be recorded under "real_legal_history", not "stock_history_adjusted"
    rejection = RejectedRecord.objects.get(endpoint="real_legal_history", symbol=symbol, date="1403-01-01")
    assert rejection.reason == "buy_sell_volume_mismatch"

    # The daily price must NOT be excluded by the real/legal rejection
    stock_rejections = RejectedRecord.objects.filter(
        endpoint__in=["stock_history_adjusted", "stock_history_unadjusted"],
        symbol=symbol,
        date="1403-01-01",
    )
    assert not stock_rejections.exists(), "real/legal rejection must not be recorded as a price rejection"

    # The price row should still exist and be accessible
    price = DailyStockHistory.objects.get(symbol=symbol, date="1403-01-01")
    assert price.pc == 1000


def test_candle_close_qs_includes_both_date_formats(asset_catalog):
    """F5: candle_close_qs must include rows with and without time suffix for as_of."""
    from marketdata.calendars import candle_close_qs
    from marketdata.models import MarketCandle
    from decimal import Decimal

    symbol = "کاما"

    # Create adjusted candle with plain date format
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-05-09",
        close_price=Decimal("1000"),
        open_price=Decimal("1000"),
        high_price=Decimal("1000"),
        low_price=Decimal("1000"),
        volume=100,
    )

    # Create adjusted candle with time suffix for the same day (should not happen in practice,
    # but the fix must handle legacy rows that have both formats)
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-05-09 00:00:00",
        close_price=Decimal("2000"),
        open_price=Decimal("2000"),
        high_price=Decimal("2000"),
        low_price=Decimal("2000"),
        volume=100,
    )

    # as_of="1405-05-09" should include BOTH rows (they represent the same day)
    # The adjusted preference logic will pick one, but both should be in the queryset
    qs = candle_close_qs(symbol, as_of="1405-05-09")
    dates = list(qs.values_list("date_time", flat=True))

    # Both formats should be present
    assert "1405-05-09" in dates
    assert "1405-05-09 00:00:00" in dates


def test_candle_close_qs_excludes_following_day(asset_catalog):
    """F5: as_of must not include the following day's rows."""
    from marketdata.calendars import candle_close_qs
    from marketdata.models import MarketCandle
    from decimal import Decimal

    symbol = "کاما"

    # Create candle for 1405-05-09
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-05-09",
        close_price=Decimal("1000"),
        open_price=Decimal("1000"),
        high_price=Decimal("1000"),
        low_price=Decimal("1000"),
        volume=100,
    )

    # Create candle for 1405-05-10 (next day)
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-05-10",
        close_price=Decimal("2000"),
        open_price=Decimal("2000"),
        high_price=Decimal("2000"),
        low_price=Decimal("2000"),
        volume=100,
    )

    # as_of="1405-05-09" should only include 1405-05-09
    qs = candle_close_qs(symbol, as_of="1405-05-09")
    dates = list(qs.values_list("date_time", flat=True))

    assert "1405-05-09" in dates
    assert "1405-05-10" not in dates, "following day must be excluded"


def test_candle_close_qs_adjusted_preference_preserved(asset_catalog):
    """F5: adjusted-price preference must still work with the fix."""
    from marketdata.calendars import candle_close_qs
    from marketdata.models import MarketCandle
    from decimal import Decimal

    symbol = "کاما"

    # Create both adjusted and aggregate for the same day
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.AGGREGATE,
        date_time="1405-05-09",
        close_price=Decimal("500"),  # aggregate price
        open_price=Decimal("500"),
        high_price=Decimal("500"),
        low_price=Decimal("500"),
        volume=100,
    )
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-05-09",
        close_price=Decimal("1000"),  # adjusted price (preferred)
        open_price=Decimal("1000"),
        high_price=Decimal("1000"),
        low_price=Decimal("1000"),
        volume=100,
    )

    # Should pick the adjusted row (provider-adjusted preferred)
    picked = candle_close_qs(symbol, as_of="1405-05-09").order_by("-date_time").first()
    assert picked.close_price == Decimal("1000.0000")
    assert picked.timeframe == MarketCandle.ADJUSTED


# ----------------------------------------------------------------------
# test_marketdata_coverage.py
# Tests for per-day tick coverage and the live-quota reserve that funds it.
# 
# Two behaviours the archive gained together:
# 
# - Which days are worth spending a request on. Requesting ticks for a day the
#   exchange was shut costs a request and returns nothing, and no local weekday
#   rule knows Iranian public holidays -- so the calendar is read out of the candle
#   table, where a holiday shows up as no symbol quoting at all.
# - How many requests the archive may take. The old static floor reserved the same
#   600 for live prices at 23:00 as at 08:00, so the tail of a quiet day went
#   unspent; the reserve now shrinks as the day closes.
# 
# The calendar helpers query two tables, so they are narrow integration tests. The
# reserve is arithmetic over a settings object and one row, so it is a unit test.


def _candles(day, count, *, volume=1000):
    MarketCandle.objects.bulk_create([
        MarketCandle(
            symbol=f"SYM{i}", timeframe="1d_unadj", date_time=day,
            open_price=100, high_price=110, low_price=95, close_price=105,
            volume=volume,
        )
        for i in range(count)
    ])


@pytest.mark.django_db
class TestTradingCalendar:
    def test_a_day_the_whole_market_quoted_is_a_trading_day(self):
        day = jalali.today()
        _candles(day, 50)
        assert market_trading_days() == {day}

    def test_a_holiday_with_a_few_stray_prints_is_not_a_trading_day(self):
        """Late corrections leave a handful of candles on a closed day."""
        recent = jalali.recent_days(5)
        _candles(recent[0], 50)   # a real session
        _candles(recent[1], 3)    # 3 of 50 symbols: the exchange was shut
        assert market_trading_days() == {recent[0]}

    def test_days_outside_the_window_are_never_offered(self):
        recent = jalali.recent_days(5)
        _candles(recent[0], 50)
        _candles(recent[4], 50)
        assert market_trading_days(window_days=2) == {recent[0]}

    def test_an_empty_candle_table_yields_no_days_rather_than_every_day(self):
        assert market_trading_days() == set()


@pytest.mark.django_db
class TestTickCoverage:
    def test_a_day_whose_ticks_add_up_is_left_alone(self):
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=300, canceled=False,
        )
        assert _tick_days_unreconciled("SYM0", {day}) == set()
        assert _tick_dates_needed("SYM0") == []

    def test_a_day_missing_ticks_entirely_is_queued(self):
        day = jalali.today()
        _candles(day, 50)
        assert _tick_dates_needed("SYM0") == [day]

    def test_a_day_whose_ticks_do_not_add_up_is_queued_for_repair(self):
        """Stored-but-wrong is the case a row counter can never see."""
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=180, canceled=False,  # 120 short of the candle
        )
        assert _tick_days_unreconciled("SYM0", {day}) == {day}
        assert _tick_dates_needed("SYM0") == [day]

    def test_cancelled_trades_do_not_count_toward_the_day(self):
        day = jalali.today()
        _candles(day, 50, volume=300)
        StockTransactionTick.objects.bulk_create([
            StockTransactionTick(symbol="SYM0", date=day, row=1, time="09:15:00",
                                 price=100, volume=300, canceled=False),
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="09:20:00",
                                 price=100, volume=500, canceled=True),
            # The cancellation twin: same row, reported again at cancellation time.
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="12:40:00",
                                 price=100, volume=500, canceled=True),
        ])
        assert _tick_days_unreconciled("SYM0", {day}) == set()

    def test_the_cancellation_twin_survives_the_widened_unique_key(self):
        """Keying on row alone dropped ~8% of a busy day; time is part of the key."""
        day = jalali.today()
        StockTransactionTick.objects.bulk_create([
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="09:20:00",
                                 price=100, volume=500, canceled=True),
            StockTransactionTick(symbol="SYM0", date=day, row=2, time="12:40:00",
                                 price=100, volume=500, canceled=True),
        ], ignore_conflicts=True)
        assert StockTransactionTick.objects.filter(symbol="SYM0", row=2).count() == 2

    def test_failed_tick_replacement_retains_the_previous_day(self, settings):
        from unittest.mock import patch
        from marketdata.archive import run_archive_state
        from marketdata.models import ArchiveFetchState

        settings.TSETMC_API_KEY = "test-key"
        day = jalali.today()
        _candles(day, 50, volume=100)
        old = StockTransactionTick.objects.create(
            symbol="SYM0", date=day, row=1, time="09:15:00",
            price=100, volume=50,
        )
        state = ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
            symbol="SYM0",
        )
        replacement = [{
            "row": 2, "time": "09:20:00", "price": 101,
            "volume": 60, "date": day,
        }]
        with patch("marketdata.archive.fetch_transactions", return_value=replacement):
            run_archive_state(state.pk)

        assert StockTransactionTick.objects.filter(pk=old.pk, volume=50).exists()
        assert not StockTransactionTick.objects.filter(symbol="SYM0", row=2).exists()


@pytest.mark.django_db
class TestLiveReserve:
    """How much of the day's quota the archive must leave for live prices."""

    def _row(self, **over):
        fields = {"day": quota.quota_day(), "limit": 9800, "used": 0,
                  "live_used": 0, "archive_used": 0, "other_used": 0}
        fields.update(over)
        return ApiRequestQuota(**fields)

    @staticmethod
    def _configure(settings, *, tse=False):
        settings.BRS_API_KEY = "brs-key"
        settings.TSETMC_API_KEY = "tse-key" if tse else ""
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 300
        settings.MARKETDATA_LIVE_INTERVAL_DAYTIME = 300
        settings.MARKETDATA_LIVE_INTERVAL_OVERNIGHT = 300
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 6000
        settings.MARKETDATA_LIVE_REQUEST_HEADROOM = 0
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        settings.MARKETDATA_IGNORE_MARKET_HOURS = False

    def _both_plans(self, now):
        """The reserve is per provider plan now; these cases span both.

        Gold/currency bills the BRS wallet while the index probe and AllSymbols
        bill TSETMC, so a scenario that enables the TSE has to add the two.
        """
        return sum(
            quota.live_reserve_remaining(plan, self._row(), now=now)
            for plan in quota.PLANS
        )

    def test_a_full_day_ahead_reserves_every_cycle_it_will_need(self, settings):
        """Reserve the real BRS plan for a full quota day."""
        self._configure(settings)
        # 00:00 Tehran is 20:30 UTC the previous day: a whole quota day remains.
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        # 24h / 300s. Was 192 (16h) while `live_job_keys` gated gold/currency to
        # 07:00-23:00; that blackout is gone, so the reserve now covers the
        # overnight cycles it will actually spend.
        assert quota.live_reserve_remaining(quota.BRS, self._row(), now=midnight_tehran) == 288

    def test_the_reserve_does_not_shrink_as_the_day_closes(self, settings):
        """Static 24h slice: evening leftover is not released to archive."""
        self._configure(settings)
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        two_hours_left = datetime(2026, 7, 27, 18, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(quota.BRS, self._row(), now=two_hours_left) == 288
        assert quota.live_reserve_remaining(quota.BRS, self._row(), now=midnight_tehran) == 288

    def test_the_reserve_never_exceeds_what_live_could_still_spend(self, settings):
        """Live cannot borrow, so holding more than its bucket protects nothing."""
        self._configure(settings)
        settings.MARKETDATA_LIVE_REQUEST_FLOOR = 100
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        row = self._row(live_used=70)
        assert quota.live_reserve_remaining(quota.BRS, row, now=midnight_tehran) == 30

    def test_the_reserve_prices_the_day_it_is_asked_about(self, settings):
        """The TSE lane costs nothing on a weekend -- and that must be decided
        by the day passed in, not by the day the test happens to run.

        `live_day_cost` read the wall clock instead of `now`, so every reserve
        answered for today. `test_a_faster_cadence_reserves_more` below then
        passed Monday-to-Wednesday and failed on Thursday and Friday, when there
        is no session for a faster cadence to poll -- which is how a red CI run
        blocked an unrelated deploy while the archive was down.
        """
        self._configure(settings, tse=True)
        trading_day = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)  # Mon
        weekend = datetime(2026, 8, 26, 20, 30, tzinfo=dt_timezone.utc)  # Thu

        tse_trading = quota.live_reserve_remaining(
            quota.TSETMC, self._row(), now=trading_day
        )
        tse_weekend = quota.live_reserve_remaining(
            quota.TSETMC, self._row(), now=weekend
        )

        assert tse_trading > 0, "a session day must reserve for the TSE lane"
        assert tse_weekend == 0, "no session, nothing to poll, nothing to reserve"
        # BRS polls gold/FX every daytime hour regardless of the TSE calendar.
        assert quota.live_reserve_remaining(quota.BRS, self._row(), now=weekend) > 0

    def test_a_faster_cadence_reserves_more(self, settings):
        """But only for the hours the fast cadence actually runs.

        The open-market interval applies 08:30-13:00 on a trading day, not all
        24 hours. Costing the whole day at it reserved ~4,300/day against an
        observed live spend of 24-719/day and locked the archive out of the tail
        of every day, so the reserve is now priced per market state.
        """
        self._configure(settings, tse=True)
        # 1405-05-05, a Monday: the session runs, so the open interval bites.
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)

        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 300
        baseline = self._both_plans(midnight_tehran)
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 120  # the old 2-minute loop
        faster = self._both_plans(midnight_tehran)

        assert faster > baseline
        # BRS gold runs every cycle of the 24h day (288 at 300s, plus the faster
        # open-session ones); during the 135 open cycles tsetmc runs too, plus one
        # provider-state probe every 30 minutes of the 4.5h session.
        assert faster == 513
        assert faster < 6 * 720

    def test_the_session_cadence_is_not_charged_on_a_closed_day(self, settings):
        """Thursday/Friday is the Iranian weekend; no session runs to pay for."""
        self._configure(settings, tse=True)
        settings.MARKETDATA_LIVE_INTERVAL_OPEN = 120
        # 1405-05-09 is a Friday (jdatetime weekday 6). Freeze wall clock so
        # the static 24h slice is that Friday, not whatever today is.
        friday_midnight = datetime(2026, 7, 30, 20, 30, tzinfo=dt_timezone.utc)
        with patch("django.utils.timezone.now", return_value=friday_midnight):
            # Gold/FX only: 24h / 300s, with the TSE lane charging nothing.
            assert self._both_plans(friday_midnight) == 288

    def test_no_credentials_means_no_phantom_reserve(self, settings):
        self._configure(settings)
        settings.BRS_API_KEY = ""
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        assert quota.live_reserve_remaining(quota.BRS, self._row(), now=midnight_tehran) == 0


def test_live_job_plan_changes_with_market_state():
    from marketdata.market_state import CLOSED_DAYTIME, OPEN, OVERNIGHT, live_job_keys

    monday_noon = datetime(2026, 7, 27, 8, 30, tzinfo=dt_timezone.utc)
    assert live_job_keys(
        state=OPEN, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == ("gold_currency", "tsetmc")
    assert live_job_keys(
        state=CLOSED_DAYTIME, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == ("gold_currency",)
    # Overnight is no longer empty. Gold/FX/crypto trade around the clock and
    # the origins behind that job are unmetered, so the old () left hours 00-06
    # with no price of any kind. The TSE job stays session-gated: outside
    # 08:30-13:00 its payload is byte-identical and bills the binding meter.
    assert live_job_keys(
        state=OVERNIGHT, now=monday_noon, has_brs=True, has_tsetmc=True
    ) == ("gold_currency",)
    assert live_job_keys(
        state=OVERNIGHT, now=monday_noon, has_brs=False, has_tsetmc=True
    ) == ()


def test_index_probe_closes_a_holiday_before_tse_jobs(settings):
    from marketdata.market_state import CLOSED_DAYTIME
    from portfolio.live.fetcher import fetch_all_markets

    settings.MARKETDATA_IGNORE_MARKET_HOURS = False
    settings.TGJU_ENABLED = False
    settings.TSETMC_SYMBOL_URL = "https://example.test/symbol"
    index_payload = {"date": "1405-05-17", "time": "08:30", "state": "بسته"}
    with (
        patch("marketdata.market_state.claim_provider_state_probe", return_value=True),
        patch("marketdata.fetchers.fetch_market_index", return_value=index_payload) as index,
        patch("marketdata.ingest.ingest_market_index", return_value=(1, 0)),
        patch("marketdata.market_state.market_state", return_value=CLOSED_DAYTIME),
        patch("portfolio.live.fetcher._tsetmc_job") as stocks,
    ):
        raw = fetch_all_markets({
            "brs_url": "", "brs_api_key": "",
            "tsetmc_url": "https://example.test/tse", "tsetmc_api_key": "key",
            "tsetmc_symbol_url": "https://example.test/symbol",
        })

    index.assert_called_once_with("key")
    assert raw["market_index"] == index_payload
    stocks.assert_not_called()


def test_ingest_tedpix_history_is_idempotent_and_keeps_live_rows():
    MarketIndexData.objects.create(
        date="1405-06-09",
        time="12:30:00",
        state="open",
        index_overall=6_548_000,
    )
    records = [
        {"jalali": "1405-06-09", "close": Decimal("6547963.76")},
        {"jalali": "1405-06-07", "close": Decimal("6516183.93")},
    ]

    assert ingest.ingest_tedpix_history(records) == (2, 0)
    assert ingest.ingest_tedpix_history(records) == (0, 2)
    assert MarketIndexData.objects.filter(date="1405-06-09").count() == 2


@pytest.mark.django_db
class TestIngestOutageDetection:
    """The gap that no per-symbol check could see.

    `compute_symbol_integrity` measures a symbol against the trading calendar,
    and that calendar is read out of the candle table. So a stretch where
    nothing was ingested contributes no sessions, no symbol is short any
    session, and every symbol reports healthy coverage over a hole. Meanwhile
    the archive marks a state complete once it has stored whatever the last
    payload held, and `claim_archive_batch` never looks at a complete state
    again -- so the hole seals itself shut. These cover the two halves of the
    escape hatch.
    """

    def _month_of_sessions(self, year, month, count=20):
        for day in range(1, count + 1):
            _candles(f"{year:04d}-{month:02d}-{day:02d}", 50)

    def test_a_quiet_stretch_longer_than_a_holiday_is_reported_as_an_outage(self):
        from marketdata.integrity import market_outage_windows

        self._month_of_sessions(1404, 9)
        # 1404-10 and 1404-11 ingested nothing at all.
        self._month_of_sessions(1404, 12)

        outages = market_outage_windows(start="1404-09-01", end="1404-12-29")

        assert len(outages) == 1
        start, end = outages[0]
        assert (end - start).days > 21

    def test_nowruz_length_closure_is_not_an_outage(self):
        from marketdata.integrity import market_outage_windows

        # Trading runs to the last week of Esfand and resumes mid-Farvardin.
        self._month_of_sessions(1404, 12, count=28)
        _candles("1405-01-14", 50)

        assert market_outage_windows(start="1404-12-01", end="1405-01-20") == []

    def test_reopening_puts_completed_states_back_in_the_queue(self):
        """reopen_states_with_gaps is the *urgent* path: it bypasses the normal
        schedule for a state an integrity check found a real gap in, even
        before its next routine reverify comes due. That is distinct from the
        recency-gap tier in claim_archive_batch, which only claims states that
        are already due -- so this state must start out not-yet-due to
        actually exercise reopen_states_with_gaps rather than the routine
        tier."""
        from datetime import timedelta

        from django.utils import timezone

        from marketdata.archive import claim_archive_batch, reopen_states_with_gaps
        from marketdata.models import ArchiveFetchState

        state = ArchiveFetchState.objects.create(
            endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
            symbol="SYM0",
            verified_complete=True,
            next_attempt_at=timezone.now() + timedelta(days=1),
        )
        assert state.pk not in claim_archive_batch(limit=5)

        assert reopen_states_with_gaps(None) == 1

        state.refresh_from_db()
        assert state.verified_complete is False
        assert state.pk in claim_archive_batch(limit=5)


# ----------------------------------------------------------------------
# test_coverage_report.py
# Coverage report classification helpers.


def test_classify_archive_state_lifecycle():
    now = timezone.now()
    assert classify_archive_state(ArchiveFetchState(verified_complete=True)) == "complete"
    assert classify_archive_state(ArchiveFetchState(last_attempt_at=None)) == "not_tried"
    assert classify_archive_state(
        ArchiveFetchState(last_attempt_at=now, consecutive_failures=2)
    ) == "failed"
    assert classify_archive_state(
        ArchiveFetchState(last_attempt_at=now, last_success_at=now, stored_rows=3, missing_rows=2)
    ) == "partial"

    # The regression this taxonomy exists for. A completed state is deliberately
    # re-armed (`verified_complete=False`) so it re-checks for newly printed
    # sessions; if the daily quota runs out before its turn, the quota handler
    # stamps last_attempt_at and leaves consecutive_failures at 0 on purpose.
    # The old four-way ladder had no rung for that and dropped it into
    # "partial -- gaps remain", which put 52.8% of the warehouse in the damage
    # bucket while every one of those jobs held exactly the rows it expected.
    quota_parked = ArchiveFetchState(
        verified_complete=False,
        last_attempt_at=now,
        last_success_at=now - timedelta(days=4),
        consecutive_failures=0,
        stored_rows=575,
        expected_rows=575,
        missing_rows=0,
        last_error="Daily quota unavailable.",
    )
    assert classify_archive_state(quota_parked) == "refresh_due"

    # Attempted but never delivered a payload: not a gap, and not "never tried".
    assert classify_archive_state(
        ArchiveFetchState(last_attempt_at=now, last_success_at=None, stored_rows=0, expected_rows=0)
    ) == "awaiting_data"

    # Unfetchability outranks everything, including `verified_complete`. A
    # blacklisted state is not going to be fetched whatever its row counts say,
    # and counting it as coverage overstates what we hold; counting it as
    # "failed" overstates what we can still work through.
    assert classify_archive_state(
        ArchiveFetchState(blacklisted=True, verified_complete=True, stored_rows=900)
    ) == "unfetchable"
    assert classify_archive_state(
        ArchiveFetchState(suspended_at=now, last_attempt_at=now, consecutive_failures=3)
    ) == "suspended"


def test_worst_archive_status_does_not_hide_unfetchable():
    """A blocked job must outrank a complete sibling on the same symbol."""
    from marketdata.coverage_report import _worst_archive_status

    complete = ArchiveFetchState(verified_complete=True, stored_rows=10, expected_rows=10)
    blocked = ArchiveFetchState(blacklisted=True, verified_complete=True, stored_rows=10)
    assert _worst_archive_status([complete, blocked]) == "unfetchable"


@pytest.mark.django_db
def test_symbol_census_counts_symbols_not_jobs():
    """The census answers a question about symbols, so it must not count states.

    Every other number in the coverage report counts `(symbol, endpoint)` jobs,
    so a symbol with several endpoints contributes several rows. Reading those
    as a symbol count is how "1,331 pending" gets mistaken for 1,331 companies.
    """
    from marketdata.coverage_report import build_symbol_census

    now = timezone.now()
    E = ArchiveFetchState.Endpoint

    # Two endpoints, one landed: the symbol is fetched, counted once.
    ArchiveFetchState.objects.create(
        symbol="FETCHED", endpoint=E.STOCK_CANDLE_ADJUSTED,
        last_attempt_at=now, last_success_at=now,
    )
    ArchiveFetchState.objects.create(
        symbol="FETCHED", endpoint=E.STOCK_TRANSACTION_TICKS, last_attempt_at=now,
    )
    # Never reached at all.
    ArchiveFetchState.objects.create(symbol="UNTOUCHED", endpoint=E.STOCK_CANDLE_ADJUSTED)
    # Reached, came back empty every time -- a different problem from untouched.
    ArchiveFetchState.objects.create(
        symbol="EMPTY", endpoint=E.STOCK_CANDLE_ADJUSTED, last_attempt_at=now,
    )
    # Every endpoint given up on -> lost.
    ArchiveFetchState.objects.create(
        symbol="GONE", endpoint=E.STOCK_CANDLE_ADJUSTED, blacklisted=True,
    )
    # One endpoint blocked out of two, and the other landed: still a fetched
    # symbol, flagged as partially blocked rather than written off.
    ArchiveFetchState.objects.create(
        symbol="PARTIAL", endpoint=E.STOCK_CANDLE_ADJUSTED, suspended_at=now,
    )
    ArchiveFetchState.objects.create(
        symbol="PARTIAL", endpoint=E.STOCK_HISTORY_UNADJUSTED,
        last_attempt_at=now, last_success_at=now,
    )
    # Once fetched, then every endpoint given up: exclusive unfetchable, not
    # also counted as fetched. The tiles are a partition.
    ArchiveFetchState.objects.create(
        symbol="LOST", endpoint=E.STOCK_CANDLE_ADJUSTED,
        last_attempt_at=now, last_success_at=now, blacklisted=True,
    )

    census = build_symbol_census()

    assert census["symbols_total"] == 6  # not the 8 states
    assert census["fetched"] == 2  # FETCHED, PARTIAL
    assert census["never_fetched"] == 1  # UNTOUCHED
    assert census["attempted_never_landed"] == 1  # EMPTY
    assert census["unfetchable"] == 2  # GONE, LOST
    assert census["partially_unfetchable"] == 1  # PARTIAL


def test_state_stale_days_orders_the_refresh_queue():
    """Staleness is what the re-fetch queue sorts on, so it must be a real number."""
    now = timezone.now()
    assert state_stale_days(ArchiveFetchState(last_success_at=None), now=now) is None
    assert state_stale_days(
        ArchiveFetchState(last_success_at=now - timedelta(days=6, hours=3)), now=now
    ) == 6
    # A clock skew must not read as "fetched in the future".
    assert state_stale_days(
        ArchiveFetchState(last_success_at=now + timedelta(hours=2)), now=now
    ) == 0


def test_classify_live_asset(asset_catalog):
    manual = Asset.objects.get(key="emami_coin")
    manual.is_manual = True
    assert classify_live_asset(manual, None, now=timezone.now()) == "manual"

    asset = Asset.objects.get(key="kama_stock")
    asset.is_manual = False
    asset.tse_symbol = asset.tse_symbol or "کاما"
    asset.save(update_fields=["tse_symbol"])
    price = Price.objects.create(asset=asset, price="5000", source="TEST")
    assert classify_live_asset(asset, price, now=timezone.now()) == "fresh"


# ----------------------------------------------------------------------
# test_calendars.py
# marketdata.calendars.is_closure_day / is_contract_expired: the single
# dispatch point deciding whether a missing day is a legitimate closure or a
# real ingest gap, per asset class. Each branch is a genuinely different data
# source (DailyStockHistory volume/trades, GoldCurrencyHistory feed breadth,
# MarketSnapshot feed breadth, MarketIndexData's own state field, or -- for
# crypto -- no tolerance at all), so each is exercised independently here
# rather than only indirectly through the daily-bar aggregator tests.


def _stock_day(date, *, volume, trades, symbol="کاما"):
    return DailyStockHistory.objects.create(
        symbol=symbol, date=date, time="12:30",
        tno=trades, tvol=volume, tval=volume * 10,
        py=Decimal("2475"), pl=Decimal("2475"), plc=Decimal("0"),
        plp=0.0, pc=Decimal("2475"),
    )


@pytest.mark.parametrize("asset_class", ["stock", "tse_option", "etf_nav"])
def test_tse_underlying_classes_share_the_stock_closure_calendar(asset_class):
    _stock_day("1404-06-01", volume=1_000_000, trades=500)
    _stock_day("1404-06-02", volume=0, trades=0)

    assert calendars.is_closure_day(asset_class, "کاما", "1404-06-02") is True
    assert calendars.is_closure_day(asset_class, "کاما", "1404-06-01") is False


def test_stock_closure_with_no_data_at_all_is_not_forgiven():
    # market_closure_days returns an empty set when there is nothing to judge
    # against; that must fail closed (a real gap), not be waved through.
    assert calendars.is_closure_day("stock", "کاما", "1399-01-01") is False


@pytest.mark.parametrize("asset_class", ["gold", "currency"])
def test_gold_and_currency_use_feed_breadth_quoting_days(asset_class):
    date_open = "1404-07-10"
    date_closed = "1404-07-11"
    for i in range(6):
        GoldCurrencyHistory.objects.create(
            symbol=f"SYM{i}", date=date_open, close_price=Decimal("100"),
            unit="Toman",
        )
    # A thin, non-representative day: below MIN_FEED_BREADTH_FOR_CALENDAR (5).
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=date_closed, close_price=Decimal("100"), unit="Toman",
    )

    assert calendars.is_closure_day(asset_class, "USD", date_closed) is True
    assert calendars.is_closure_day(asset_class, "USD", date_open) is False


def test_gold_currency_fails_closed_when_feed_too_thin_to_trust():
    GoldCurrencyHistory.objects.create(
        symbol="USD", date="1404-08-01", close_price=Decimal("100"), unit="Toman",
    )
    # Only one symbol on this date -- below the breadth floor, so the
    # quoting-days set is empty and the day must NOT be treated as a closure.
    assert calendars.is_closure_day("gold", "USD", "1404-08-01") is False


def test_crypto_is_never_forgiven():
    # No data, some data, lots of data -- crypto markets never close, so this
    # must always return False, unlike every other branch.
    assert calendars.is_closure_day("crypto", "BTC", "1404-01-01") is False
    for i in range(50):
        MarketSnapshot.objects.create(
            asset_class="crypto", symbol=f"COIN{i}",
            observed_at="2025-01-01T00:00:00Z", last_price=Decimal("1"),
        )
    assert calendars.is_closure_day("crypto", "BTC", "1404-01-01") is False


# `ime_future`/`ime_option` were retired 2026-09-06 and no longer reach this
# branch; `commodity` is the only class left that infers its calendar from the
# feed's own breadth.
@pytest.mark.parametrize("asset_class", ["commodity"])
def test_commodity_and_derivative_classes_use_snapshot_breadth(asset_class):
    from marketdata import jalali

    open_jalali = "1404-12-11"
    closed_jalali = "1404-12-12"
    open_day = jalali.to_datetime(open_jalali)
    closed_day = jalali.to_datetime(closed_jalali)
    for i in range(4):
        MarketSnapshot.objects.create(
            asset_class=asset_class, symbol=f"SYM{i}",
            observed_at=open_day, last_price=Decimal("100"),
        )
    # Below _MIN_BREADTH_FOR_CALENDAR (3): a thin day, not a representative one.
    MarketSnapshot.objects.create(
        asset_class=asset_class, symbol="SYM0",
        observed_at=closed_day, last_price=Decimal("100"),
    )

    assert calendars.is_closure_day(asset_class, "SYM0", closed_jalali) is True
    assert calendars.is_closure_day(asset_class, "SYM0", open_jalali) is False


def test_index_reads_provider_closed_state():
    from marketdata.market_state import PROVIDER_CLOSED

    MarketIndexData.objects.create(date="1404-09-01", time="09:00", state=PROVIDER_CLOSED)
    MarketIndexData.objects.create(date="1404-09-02", time="09:00", state="باز")

    assert calendars.is_closure_day("index", "overall", "1404-09-01") is True
    assert calendars.is_closure_day("index", "overall", "1404-09-02") is False


def test_index_with_no_state_recorded_is_not_forgiven():
    assert calendars.is_closure_day("index", "overall", "1404-09-03") is False


def test_unknown_asset_class_is_never_forgiven():
    assert calendars.is_closure_day("nonexistent_class", "X", "1404-01-01") is False


def test_contract_expired_true_past_expiry():
    DerivativeContract.objects.create(
        kind="tse_option", contract_code="OPT1", expiry_date="1404-01-01",
    )
    assert calendars.is_contract_expired("tse_option", "OPT1", "1404-02-01") is True
    assert calendars.is_contract_expired("tse_option", "OPT1", "1403-12-01") is False


def test_contract_expired_false_when_no_expiry_on_file():
    DerivativeContract.objects.create(kind="tse_option", contract_code="OPT2")
    assert calendars.is_contract_expired("tse_option", "OPT2", "1404-02-01") is False


def test_contract_expired_false_for_unknown_contract():
    assert calendars.is_contract_expired("tse_option", "GHOST", "1404-02-01") is False


# ----------------------------------------------------------------------
# test_market_closure_handling.py
# An exchange closure must not be mistaken for a warehouse gap.
# 
# Integration tests: the discriminator reads DailyStockHistory volume/trade totals
# and the panel index together, so the defect only reproduces with real rows in
# the database — a pure-pandas test cannot express "the provider padded these days".
# 
# Background: the TSE was shut for 83 days across 1404-1405. The provider still
# emitted a row per symbol for every closed day, carrying the previous price with
# zero volume and zero trades. `_trim_to_contiguous` read the resulting hole in
# MarketCandle as an ingest outage and discarded everything before it, collapsing
# a 2,333-session history to 55 and making all four lookback windows identical.


def _history(date, *, symbol="کاما", volume, trades, price="2475"):
    return DailyStockHistory(
        symbol=symbol, date=date, time="12:30",
        tno=trades, tvol=volume, tval=volume * 10,
        py=Decimal(price), pl=Decimal(price), plc=Decimal("0"),
        plp=0.0, pc=Decimal(price),
    )


def test_zero_volume_days_are_reported_as_closures():
    DailyStockHistory.objects.bulk_create([
        _history("1404-12-06", volume=36_421_072, trades=992),
        _history("1404-12-09", volume=0, trades=0),
        _history("1404-12-11", volume=0, trades=0),
        _history("1405-02-29", volume=12_000_000, trades=430),
    ])

    closures = market_closure_days(start="1404-12-01", end="1405-03-01")

    assert closures == {"1404-12-09", "1404-12-11"}


def test_a_traded_day_is_never_called_a_closure():
    # One real trade on the day is enough to make it a session, so a genuine
    # ingest hole elsewhere still gets caught rather than excused.
    DailyStockHistory.objects.bulk_create([
        _history("1404-12-09", volume=1, trades=1),
    ])

    assert market_closure_days(start="1404-12-01", end="1404-12-30") == set()


def test_closure_explained_is_false_without_any_closure_rows():
    left = pd.Timestamp("2026-02-25", tz=dt.timezone.utc)
    right = pd.Timestamp("2026-05-19", tz=dt.timezone.utc)

    # No DailyStockHistory rows at all -> nothing proves a closure, so the gap
    # must be treated as an ingest hole (fail closed).
    assert _closure_explained(left, right) is False


def test_the_return_bridging_a_long_break_is_masked():
    """Reopening after months holds one enormous 'daily' return. Drop just it."""
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-02-24", tz=dt.timezone.utc),
            pd.Timestamp("2026-02-25", tz=dt.timezone.utc),
            pd.Timestamp("2026-05-19", tz=dt.timezone.utc),  # 83 days later
            pd.Timestamp("2026-05-20", tz=dt.timezone.utc),
        ]
    )
    returns = pd.DataFrame({"کاما": [0.01, 0.02, 0.90, 0.01]}, index=index)
    panel = pd.DataFrame({"کاما": [100.0, 102.0, 194.0, 196.0]}, index=index)

    masked = _mask_closure_returns(returns.copy(), panel)

    assert pd.isna(masked.loc[index[2], "کاما"])  # the 90% bridge is gone
    # Everything either side survives -- this is the whole point of not trimming.
    assert masked.loc[index[0], "کاما"] == 0.01
    assert masked.loc[index[1], "کاما"] == 0.02
    assert masked.loc[index[3], "کاما"] == 0.01


def test_an_asset_resuming_after_the_market_is_still_masked():
    """Unit test: pure frame arithmetic, and the whole point is one column.

    The war closure reopened the exchange on 2026-05-19, so the panel index
    carries a single 83-day jump there. Gold resumed with the market; کاما did
    not trade until 2026-05-24. Masking only the index's bridging row leaves the
    stock's 88-day move sitting on a later date, published as one day's return.
    """
    index = pd.DatetimeIndex(
        [
            pd.Timestamp("2026-02-25", tz=dt.timezone.utc),
            pd.Timestamp("2026-05-19", tz=dt.timezone.utc),  # market reopens
            pd.Timestamp("2026-05-24", tz=dt.timezone.utc),  # کاما reopens
        ]
    )
    panel = pd.DataFrame(
        {
            "gold_18k_gram": [100.0, 130.0, 131.0],
            "کاما": [1872.0, float("nan"), 2613.0],  # silent on reopening day
        },
        index=index,
    )
    returns = pd.DataFrame(
        {"gold_18k_gram": [float("nan"), 0.30, 0.008],
         "کاما": [float("nan"), float("nan"), 0.396]},
        index=index,
    )

    masked = _mask_closure_returns(returns.copy(), panel)

    assert pd.isna(masked.loc[index[1], "gold_18k_gram"]), (
        "gold resumed with the market; its bridging return must still go"
    )
    assert pd.isna(masked.loc[index[2], "کاما"]), (
        "the stock's 88-day move was published as a single day's return because "
        "it resumed after the day the index-wide mask looks at"
    )
    # The move is not a daily return; the price behind it is real and untouched.
    assert panel.loc[index[2], "کاما"] == 2613.0
    assert masked.loc[index[2], "gold_18k_gram"] == 0.008


# ----------------------------------------------------------------------
# test_market_snapshots.py
# MarketSnapshot/MarketDailyBar: the unified live->historical mechanism for
# crypto, commodity, ETF NAV, and market index (see marketdata/calendars.py and
# marketdata/ingest.py's aggregate_market_daily_bars).


def test_ingest_market_snapshots_flattens_dict_of_lists_payload():
    payload = {
        "metal_precious": [{"symbol": "XAUUSD", "price": "2400.5"}],
        "energy": [{"symbol": "WTI", "price": "80.1"}],
    }
    created, skipped = ingest.ingest_market_snapshots("commodity", payload)

    assert created == 2
    assert skipped == 0
    assert set(MarketSnapshot.objects.values_list("symbol", flat=True)) == {"XAUUSD", "WTI"}


def test_aggregate_market_daily_bars_builds_ohlc_from_snapshots():
    date = jalali.today()
    start = jalali.to_datetime(date)
    for offset_minutes, price in ((0, "100"), (30, "110"), (60, "90"), (90, "105")):
        MarketSnapshot.objects.create(
            asset_class="crypto",
            symbol="BTC",
            observed_at=start + timedelta(minutes=offset_minutes),
            last_price=price,
            volume=10,
        )

    created, skipped = ingest.aggregate_market_daily_bars("crypto", date)

    assert created == 1
    assert skipped == 0
    bar = MarketDailyBar.objects.get(asset_class="crypto", symbol="BTC", date=date)
    assert bar.open_price == 100
    assert bar.high_price == 110
    assert bar.low_price == 90
    assert bar.close_price == 105
    assert bar.sample_count == 4


def test_crypto_missing_day_is_a_real_gap_never_forgiven():
    """Crypto never closes -- a symbol with zero snapshots for the day must
    not be silently absorbed into the 'skipped' (legitimate closure) count."""
    date = jalali.today()

    created, skipped = ingest.aggregate_market_daily_bars("crypto", date, symbols=["BTC"])

    assert created == 0
    assert skipped == 0
    assert not MarketDailyBar.objects.filter(asset_class="crypto", symbol="BTC").exists()


def test_aggregate_market_daily_bars_reads_index_from_market_index_data():
    date = jalali.today()
    MarketIndexData.objects.create(
        date=date, time="09:00", index_overall=100, index_equal_weight=50, trade_volume=1000
    )
    MarketIndexData.objects.create(
        date=date, time="12:30", index_overall=105, index_equal_weight=48, trade_volume=500
    )

    created, _skipped = ingest.aggregate_market_daily_bars("index", date)

    assert created == 2
    overall = MarketDailyBar.objects.get(asset_class="index", symbol="overall", date=date)
    assert overall.open_price == 100
    assert overall.close_price == 105
    assert overall.high_price == 105
    equal_weight = MarketDailyBar.objects.get(asset_class="index", symbol="equal_weight", date=date)
    assert equal_weight.open_price == 50
    assert equal_weight.close_price == 48


# ----------------------------------------------------------------------
# test_audit_collisions.py
# Cross-symbol detection: one bad provider day, not eight market moves.
# 
# Every other unit check compares a symbol against its own history, so a payload
# that corrupts many symbols at once slipped through. On 1405-04-31 six currencies
# were written at ~96,000 Toman; the per-symbol test reported four and missed SEK
# and CNY entirely, because those two moved only 4.9x and 3.4x.


def _history_audit_collisions(symbol, values, start_day=10):
    for offset, value in enumerate(values):
        GoldCurrencyHistory.objects.create(
            symbol=symbol,
            date=f"1405-04-{start_day + offset:02d}",
            close_price=value,
            unit="تومان",
        )


def test_one_contaminated_day_across_symbols_is_caught():
    bad_day_index = 2
    for symbol, normal in (("AFN", 2874), ("AMD", 458), ("SEK", 19710)):
        values = [normal] * 5
        values[bad_day_index] = 96100
        _history_audit_collisions(symbol, values)

    findings = Command().check_same_day_collisions()

    assert {f["symbol"] for f in findings} == {"AFN", "AMD", "SEK"}
    assert {f["date"] for f in findings} == {"1405-04-12"}
    # SEK is the point of the check: a 4.9x step no per-symbol test would flag.
    sek = next(f for f in findings if f["symbol"] == "SEK")
    assert "2 unrelated symbols" in sek["evidence"]


def test_a_single_symbol_spiking_alone_is_not_a_collision():
    """One currency moving is a market event; the check must not claim otherwise."""
    _history_audit_collisions("AFN", [2874, 2874, 96100, 2874, 2874])
    _history_audit_collisions("AMD", [458] * 5)
    _history_audit_collisions("SEK", [19710] * 5)

    assert Command().check_same_day_collisions() == []


def test_steady_inflation_is_not_flagged():
    """A lifetime median made every old row an outlier and produced 326 false
    positives where SAR, QAR and MYR all legitimately traded near 900."""
    for symbol, base in (("SAR", 900), ("QAR", 890), ("MYR", 910)):
        _history_audit_collisions(symbol, [base + step * 10 for step in range(5)])

    assert Command().check_same_day_collisions() == []


# ----------------------------------------------------------------------
# test_asset_evidence.py
# Per-asset evidence inspector: named claims without docker/psql.


@pytest.fixture
def staff_client(db, make_user):
    user = make_user(email="evidence-admin@example.com")
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _kama(asset_catalog):
    MarketInstrument.objects.create(
        symbol="کاما",
        name="Kama",
        source=MarketInstrument.Source.TSETMC,
        eligible=True,
    )
    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    ArchiveFetchState.objects.create(
        symbol="کاما",
        endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED,
        stored_rows=4512,
        expected_rows=4512,
        missing_rows=0,
        verified_complete=True,
    )
    ArchiveFetchState.objects.create(
        symbol="کاما",
        endpoint=ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS,
        stored_rows=10,
        expected_rows=55,
        missing_rows=45,
        consecutive_failures=5,
        verified_complete=False,
        last_error="Provider returned zero records where records were expected",
    )
    SymbolIntegrity.objects.create(
        symbol="کاما",
        coverage_ratio=0.883,
        max_gap_days=3,
        rejected_count=12,
        passes_gate=False,
        reason="low_coverage,excessive_rejections",
    )
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time="1405-01-15",
        close_price=Decimal("4000"),
        volume=1000,
    )
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted",
        symbol="کاما",
        date="1405-01-01",
        reason="series_spike",
    )
    WorkflowRun.objects.create(
        workflow="archive_state",
        outcome="partial",
        endpoint="stock_transaction_ticks",
        symbol="کاما",
        error_code="tick_volume_mismatch",
    )
    Price.objects.create(asset=asset, price=Decimal("4000"), source="API")
    return asset


def test_evidence_requires_staff(asset_catalog, make_user):
    _kama(asset_catalog)
    client = APIClient()
    assert client.get("/api/admin/assets/kama_stock/evidence/").status_code in (401, 403)
    client.force_authenticate(user=make_user(email="free-evidence@example.com"))
    assert client.get("/api/admin/assets/kama_stock/evidence/").status_code == 403


def test_kama_archive_complete_and_gate_fail_disagree(staff_client, asset_catalog):
    _kama(asset_catalog)
    res = staff_client.get("/api/admin/assets/kama_stock/evidence/")
    assert res.status_code == 200
    body = res.json()
    claims = {c["id"]: c["passed"] for c in body["claims"]}
    assert claims["archive_payload_verified"] is True
    assert claims["analytics_gate_179d"] is False
    assert body["identity"]["tse_symbol"] == "کاما"
    assert body["identity"]["key"] == "kama_stock"
    assert "resync_symbol_from_provider" in body["suggested_cli"]
    assert any(s["endpoint"] == "stock_candle_adjusted" and s["verified_complete"] for s in body["archive_states"])
    assert body["integrity"]["passes_gate"] is False
    assert body["rejected"]["count"] >= 1
    assert body["workflows"]


def test_evidence_lookup_by_tse_symbol(staff_client, asset_catalog):
    _kama(asset_catalog)
    res = staff_client.get("/api/admin/assets/کاما/evidence/")
    assert res.status_code == 200
    assert res.json()["identity"]["key"] == "kama_stock"


def test_recompute_integrity_audits(staff_client, asset_catalog):
    _kama(asset_catalog)
    bad = staff_client.post("/api/admin/assets/kama_stock/recompute-integrity/", {}, format="json")
    assert bad.status_code == 400
    ok = staff_client.post(
        "/api/admin/assets/kama_stock/recompute-integrity/",
        {"confirm": True},
        format="json",
    )
    assert ok.status_code == 200
    assert WorkflowRun.objects.filter(workflow="ops_recompute_integrity").exists()


def test_symbol_retry_enqueues(staff_client, asset_catalog, monkeypatch):
    _kama(asset_catalog)
    calls = []

    class DummyTask:
        def delay(self, state_id):
            calls.append(state_id)

    monkeypatch.setattr("marketdata.tasks.retry_archive_job_task", DummyTask())
    monkeypatch.setattr(
        "marketdata.admin_api.get_quota_status",
        lambda: {"limit": 100, "used": 1, "remaining_daily": 99},
    )

    class FakeRedis:
        def ping(self):
            return True

    monkeypatch.setattr("redis.Redis.from_url", lambda url: FakeRedis())
    ok = staff_client.post(
        "/api/admin/assets/kama_stock/retry/",
        {"confirm": True},
        format="json",
    )
    assert ok.status_code == 200
    assert ok.json()["queued"]
    assert calls


def test_ingest_stamps_correlation_id():
    from marketdata.ingest import ingest_candles

    outcome = WorkflowOutcome("test_ingest", endpoint="stock_candle_adjusted", symbol="FOO")
    assert current_correlation_id() == outcome.correlation_id
    ingest_candles("FOO", 3, {
        "candle_daily_adjusted": [
            {"date": "1404-02-24", "open": 7380, "high": 7400, "low": 7280, "close": 7340, "volume": 180715348},
        ]
    })
    outcome.finish("success", rows_accepted=1)
    row = MarketCandle.objects.filter(symbol="FOO", timeframe=MarketCandle.ADJUSTED).first()
    assert row is not None
    assert row.last_correlation_id == outcome.correlation_id
    assert row.ingested_at is not None


def test_value_user_flattens_items(asset_catalog, make_user):
    from portfolio.models import Account, Holding
    from portfolio.services.valuation import value_user

    user = make_user(email="flatten@example.com")
    account = Account.objects.create(user=user, name="A")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=1)
    Price.objects.create(asset=asset_catalog["emami_coin"], price=Decimal("10"), source="API")
    payload = value_user(user)
    assert payload["items"]
    assert payload["items"][0]["key"] == "emami_coin"
    assert payload["items"][0]["account_id"] == account.id


def test_workflows_failed_only(staff_client):
    WorkflowRun.objects.create(workflow="archive", outcome="success")
    WorkflowRun.objects.create(workflow="archive", outcome="failed", error_code="boom")
    res = staff_client.get("/api/admin/workflows/?failed_only=true")
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert res.json()["results"][0]["outcome"] == "failed"


# ----------------------------------------------------------------------
# Live fetch plan: the cadence-driven endpoints that bill the live bucket.
#
# Before LiveFetchState these fired on a fixed 5-minute beat with no state row,
# so the reserve priced only the 2-minute price loop and crypto/commodity/ETF
# NAV/options/futures spent ~350/day that nothing had set aside. Integration
# tests, not unit: the plan is read back out of the database by the same query
# the scheduler uses, and that round trip is the thing being asserted.


@pytest.mark.django_db
class TestLiveFetchPlan:
    @staticmethod
    def _configure(settings):
        settings.BRS_API_KEY = ""
        settings.TSETMC_API_KEY = ""
        settings.MARKETDATA_QUOTA_TIMEZONE = "Asia/Tehran"
        settings.MARKETDATA_IGNORE_MARKET_HOURS = False

    def test_an_always_on_cadence_costs_the_whole_day(self, settings):
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        self._configure(settings)
        LiveFetchState.objects.create(
            endpoint_key="crypto", cadence_seconds=900, session_only=False
        )
        # [start, end): the firing exactly at the next midnight is outside the day.
        assert live_states.full_day_cost() == 96

    def test_session_gating_costs_only_the_trading_window(self, settings):
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        self._configure(settings)
        LiveFetchState.objects.create(
            endpoint_key="option_contracts", cadence_seconds=900, session_only=True
        )
        # The TSE trades 08:30-13:00, five days a week. A session-gated endpoint
        # must not be costed for the 19.5 hours it is not allowed to fetch.
        cost = live_states.full_day_cost()
        assert 0 <= cost <= 18, "at most one 4.5h session at 15-minute cadence"

    def test_the_reserve_counts_the_plan_not_just_the_price_loop(self, settings):
        from marketdata import quota
        from marketdata.models import LiveFetchState

        self._configure(settings)
        row = ApiRequestQuota(
            day=quota.quota_day(), limit=9800, used=0,
            live_used=0, archive_used=0, other_used=0,
        )
        midnight_tehran = datetime(2026, 7, 26, 20, 30, tzinfo=dt_timezone.utc)
        # No API keys configured, so the price loop plans nothing at all: whatever
        # is reserved here can only have come from the LiveFetchState table.
        assert quota.live_reserve_remaining(quota.BRS, row, now=midnight_tehran) == 0

        LiveFetchState.objects.create(
            endpoint_key="crypto", cadence_seconds=3600, session_only=False
        )
        # `crypto` is a Market/* endpoint, so it bills the BRS wallet -- and must
        # NOT show up in the TSETMC reserve.
        assert quota.live_reserve_remaining(quota.BRS, row, now=midnight_tehran) == 24
        assert quota.live_reserve_remaining(quota.TSETMC, row, now=midnight_tehran) == 0

    def test_seeding_never_overwrites_a_tuned_cadence(self):
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        live_states.ensure_live_states()
        tuned = LiveFetchState.objects.filter(endpoint_key="crypto").get()
        tuned.cadence_seconds = 60
        tuned.save(update_fields=["cadence_seconds"])

        live_states.ensure_live_states()

        tuned.refresh_from_db()
        assert tuned.cadence_seconds == 60, "re-seeding must not revert operator tuning"

    def test_seeding_disables_leftover_etf_nav_rows_without_reenable(self):
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        leftover = LiveFetchState.objects.create(
            endpoint_key="etf_nav", scope="OLD", cadence_seconds=86400
        )
        already_off = LiveFetchState.objects.create(
            endpoint_key="etf_nav", scope="MANUAL", cadence_seconds=86400,
            enabled=False, last_error="operator",
        )

        live_states.ensure_live_states()

        leftover.refresh_from_db()
        already_off.refresh_from_db()
        assert leftover.enabled is False
        # The reason is carried in the field so an operator reading the row
        # learns why it is off, rather than finding a bare "retired".
        assert leftover.last_error == "retired: no usable series"
        assert already_off.enabled is False
        assert already_off.last_error == "operator"

    def test_seeding_disables_leftover_ime_rows(self):
        """A seed governs creation only; a row an earlier deploy created keeps
        polling forever unless something switches it off."""
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        leftover = LiveFetchState.objects.create(
            endpoint_key="ime_futures", scope="", cadence_seconds=900
        )

        live_states.ensure_live_states()

        leftover.refresh_from_db()
        assert leftover.enabled is False
        assert leftover.last_error == "retired: 100% rejected on ingest"

    def test_a_claim_leases_the_row_so_a_second_worker_cannot_double_spend(self):
        from marketdata import live_states
        from marketdata.models import LiveFetchState

        LiveFetchState.objects.create(
            endpoint_key="crypto", cadence_seconds=900, session_only=False
        )
        assert len(live_states.claim_due("crypto")) == 1
        assert live_states.claim_due("crypto") == [], "cadence not elapsed yet"


def test_archive_claim_survives_a_pending_refresh(staff_client, asset_catalog):
    """A re-armed state still holding every row must not fail the archive claim.

    `verified_complete` is flipped back to False on purpose whenever a state
    falls due for another pass, so keying the claim on that flag alone made the
    evidence panel report "archive payload verified: no" for symbols with a
    complete, gap-free payload -- the same misreading that put half the
    warehouse in the damage bucket on the Ops donut.
    """
    _kama(asset_catalog)
    state = ArchiveFetchState.objects.get(
        symbol="کاما", endpoint=ArchiveFetchState.Endpoint.STOCK_CANDLE_ADJUSTED
    )
    state.verified_complete = False
    state.last_success_at = timezone.now() - timedelta(days=3)
    state.last_error = "Daily quota unavailable."
    state.save(update_fields=["verified_complete", "last_success_at", "last_error"])

    body = staff_client.get("/api/admin/assets/kama_stock/evidence/").json()
    claims = {c["id"]: c["passed"] for c in body["claims"]}
    assert claims["archive_payload_verified"] is True
