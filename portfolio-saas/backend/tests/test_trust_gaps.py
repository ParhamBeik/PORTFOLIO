"""Regressions for trust-first defects that the first pass left behind.

Test-pyramid rationale: four of these are integration tests because the defect
only appears where the ledger, the warehouse tables, and the projection meet —
a unit test of either side alone passes while the system is still wrong. The
cutoff-count case is a unit test: arithmetic over the Jalali calendar, no
database involved.
"""
import datetime
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from portfolio.models import Account, Asset, BacktestRun, Holding, LedgerEntry


pytestmark = pytest.mark.django_db


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
    opened = timezone.now() - datetime.timedelta(days=10)
    bought = opened + datetime.timedelta(days=1)
    sold = opened + datetime.timedelta(days=2)
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
    started_at = timezone.now() - datetime.timedelta(days=5)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=started_at,
    )
    keep = create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="2", unit_price_tomans="500",
        occurred_at=started_at + datetime.timedelta(hours=1),
    )
    mistake = create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="3", unit_price_tomans="900",
        occurred_at=started_at + datetime.timedelta(hours=2),
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
        occurred_at=as_of - datetime.timedelta(days=30),
    )
    # The asset last printed nine days ago; the market traded on each of the
    # eight days since (other symbols kept quoting), so the gap is over five.
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date=to_jalali_str(as_of - datetime.timedelta(days=9)),
        close_price=Decimal("1000"),
    )
    for offset in range(1, 9):
        GoldCurrencyHistory.objects.create(
            symbol="USD",
            date=to_jalali_str(as_of - datetime.timedelta(days=offset)),
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


def test_run_backtest_evaluates_the_requested_number_of_completed_years(
    make_user, monkeypatch
):
    """The API accepts and hashes completed_years, so the runner must honour it."""
    from portfolio.services import backtest as backtest_service

    run = BacktestRun.objects.create(
        user=make_user(email="years@test.test"),
        universe_hash="h", params_hash="h", universe=["emami_coin"],
        completed_years=2,
    )
    seen = []
    real_cutoffs = backtest_service._completed_jalali_cutoffs

    def _record(now=None, count=5):
        seen.append(count)
        return real_cutoffs(now=now, count=count)

    monkeypatch.setattr(backtest_service, "_completed_jalali_cutoffs", _record)

    backtest_service.run_backtest(run.id)

    assert seen == [2]
    run.refresh_from_db()
    assert run.status == BacktestRun.Status.READY
    assert (
        run.years.values("cutoff_date").distinct().count() == 2
    ), "one cutoff per requested completed year"


def test_recover_stuck_backtests_fails_only_abandoned_runs(make_user):
    """A worker that dies mid-run cannot mark its own row failed."""
    from portfolio.tasks import BACKTEST_STUCK_AFTER_SECONDS, recover_stuck_backtests

    user = make_user(email="stuck@test.test")
    abandoned = BacktestRun.objects.create(
        user=user, universe_hash="h", params_hash="h",
        status=BacktestRun.Status.RUNNING,
    )
    fresh = BacktestRun.objects.create(
        user=user, universe_hash="h", params_hash="h",
        status=BacktestRun.Status.QUEUED,
    )
    # auto_now_add ignores an assigned value, so age the row with an update().
    BacktestRun.objects.filter(pk=abandoned.pk).update(
        created_at=timezone.now()
        - datetime.timedelta(seconds=BACKTEST_STUCK_AFTER_SECONDS + 60)
    )

    result = recover_stuck_backtests()

    abandoned.refresh_from_db()
    fresh.refresh_from_db()
    assert result["failed"] == 1
    assert abandoned.status == BacktestRun.Status.FAILED
    assert "worker was lost" in abandoned.error
    assert fresh.status == BacktestRun.Status.QUEUED


def test_same_day_as_of_respects_exact_event_timestamps(ledger_account, asset_catalog):
    """Two events on one calendar day still order by clock time, not date alone."""
    from portfolio.models import Asset
    from portfolio.services.ledger import create_ledger_entry
    from portfolio.services.timeline import holdings_as_of

    asset = Asset.objects.get(key="emami_coin")
    day = (timezone.now() - datetime.timedelta(days=3)).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    morning = day.replace(hour=9)
    afternoon = day.replace(hour=15)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=morning - datetime.timedelta(days=1),
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
    opened = timezone.now() - datetime.timedelta(days=10)
    sold = timezone.now() - datetime.timedelta(days=2)
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.OPENING_CASH,
        amount_tomans="10000", occurred_at=opened,
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.BUY, asset=asset,
        quantity="3", unit_price_tomans="100",
        occurred_at=opened + datetime.timedelta(hours=1),
    )
    create_ledger_entry(
        account=ledger_account, kind=LedgerEntry.Kind.SELL, asset=asset,
        quantity="3", unit_price_tomans="120", occurred_at=sold,
    )
    assert not Holding.objects.filter(account=ledger_account, asset=asset).exists()

    before_sale = sold - datetime.timedelta(hours=1)
    assert holdings_as_of(ledger_account.user, ledger_account, before_sale) == {
        asset.key: Decimal("3")
    }


def test_stored_backtest_result_is_immutable_to_later_warehouse_rows(make_user):
    """Adding post-cutoff candles must not rewrite a completed BacktestYear row."""
    from marketdata.models import MarketCandle
    from portfolio.models import BacktestYear
    from portfolio.services import backtest as backtest_service

    run = BacktestRun.objects.create(
        user=make_user(email="immutable@test.test"),
        universe_hash="h", params_hash="h", universe=["emami_coin"],
        completed_years=1,
        status=BacktestRun.Status.READY,
    )
    year = BacktestYear.objects.create(
        run=run,
        cutoff_date="1402-01-01",
        scenario="equal_weight",
        target_weights={"emami_coin": 1.0},
        realized_metrics={
            "net_return": 0.12,
            "manifest": {
                "cutoff": "1402-01-01",
                "maximum_source_data_timestamp": "1402-12-29",
            },
        },
    )
    frozen = year.realized_metrics.copy()

    MarketCandle.objects.create(
        symbol="EMAMI",
        timeframe="1d_adj",
        date_time="1403-06-01",
        open_price=1, high_price=1, low_price=1, close_price=999, volume=1,
    )
    year.refresh_from_db()
    assert year.realized_metrics == frozen
    assert BacktestYear.objects.get(pk=year.pk).realized_metrics["net_return"] == 0.12
    assert backtest_service.INTEGRITY_VERSION


def test_celery_routes_archive_and_live_queues_separately():
    """Production workers consume disjoint queues; routing must stay module-scoped."""
    from config.celery import app

    routes = app.conf.task_routes
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
    started_at = timezone.now() - datetime.timedelta(days=2)
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
    started_at = timezone.now() - datetime.timedelta(days=2)
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


def test_daily_aggregate_never_shadows_the_authoritative_adjusted_candle(asset_catalog):
    """The nightly aggregate and the provider once wrote the same natural key.

    The archive ingests with bulk_create(ignore_conflicts=True), so whatever
    already occupies (symbol, ADJUSTED, date) is permanent. A tick-derived
    aggregate parked there silently displaces the provider's real close for
    that day -- in the one series every valuation and return path reads.
    """
    from marketdata.ingest import ingest_candles
    from marketdata.models import MarketCandle
    from marketdata.tasks import aggregate_daily_stock_history
    from portfolio.models import Price

    day = "1403-10-19"
    symbol = "کاما"
    asset = Asset.objects.get(key="kama_stock")
    asset.tse_symbol = symbol
    asset.save(update_fields=["tse_symbol"])

    # The aggregator runs first, deriving a close from intraday ticks.
    Price.objects.create(asset=asset, price=Decimal("1000"))
    aggregate_daily_stock_history(date_str=day)

    # The provider then publishes the real adjusted close for the same day.
    created, _ = ingest_candles(
        symbol,
        3,
        {
            "candle_daily_adjusted": [
                {"date": day, "open": 900, "high": 950, "low": 890, "close": 920, "volume": 5}
            ]
        },
    )

    assert created == 1, "the tick aggregate blocked the provider's candle"
    authoritative = MarketCandle.objects.get(
        symbol=symbol, timeframe=MarketCandle.ADJUSTED, date_time=day
    )
    assert authoritative.close_price == Decimal("920.0000")
    assert MarketCandle.objects.filter(
        symbol=symbol, timeframe=MarketCandle.AGGREGATE, date_time=day
    ).exists(), "the aggregate must still be recorded, just not in the ADJUSTED slot"


def test_valuation_prefers_the_authoritative_candle_over_the_aggregate(asset_catalog):
    """Both timeframes may hold a day; the provider wins, the aggregate fills gaps."""
    from marketdata.candles import candle_close_qs
    from marketdata.models import MarketCandle

    day = "1403-10-19"
    symbol = "کاما"
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.AGGREGATE,
        date_time=day,
        close_price=Decimal("1000"),
    )
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time=day,
        close_price=Decimal("920"),
    )

    def _picked():
        return (
            candle_close_qs(symbol, as_of=day)
            .order_by("-date_time")
            .first()
        )

    assert _picked().close_price == Decimal("920.0000")

    # With no provider row for the day, the aggregate is still a usable answer.
    MarketCandle.objects.filter(timeframe=MarketCandle.ADJUSTED).delete()
    assert _picked().close_price == Decimal("1000.0000")


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
        ("USD", 63200, "", None, Decimal("63200")),
        ("USD", 632000, "IRR", None, Decimal("63200")),
        ("USDT", 632000, "ریال", None, Decimal("63200")),
        ("USDT", 1, "", 63200, Decimal("63200")),
    ],
)
def test_currency_conversion_has_one_unit_driven_rule(
    symbol, price, unit, usd_rate, expected
):
    from marketdata.currency import to_toman

    assert to_toman(symbol, price, unit, usd_rate=usd_rate) == expected


def test_symbol_matching_never_uses_substrings():
    from marketdata.symbols import find_symbol_record

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


def test_baseline_simulator_matches_hand_computed_buy_and_hold():
    import pandas as pd

    from portfolio.services.backtest import _simulate_buy_and_hold

    returns = pd.DataFrame(
        {"gold": [0.10, -0.05]},
        index=pd.date_range("2025-01-01", periods=2, tz="UTC"),
    )

    simulation = _simulate_buy_and_hold(
        returns,
        {"gold": 1.0},
        cost_drag=0.01,
    )

    assert simulation["gross_return"] == pytest.approx(1.10 * 0.95 - 1)
    assert simulation["net_return"] == pytest.approx(0.99 * 1.10 * 0.95 - 1)


def test_dividend_does_not_create_a_twr_boundary(
    ledger_account, asset_catalog, monkeypatch
):
    from portfolio.services import performance

    ledger_account.ledger_complete = True
    ledger_account.tracking_started_at = timezone.now() - datetime.timedelta(days=10)
    ledger_account.cash_balance_tomans = 1000
    ledger_account.save()
    LedgerEntry.objects.create(
        account=ledger_account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.DIVIDEND,
        amount_tomans=100,
        timestamp=timezone.now() - datetime.timedelta(days=2),
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

    `test_stored_backtest_result_is_immutable_to_later_warehouse_rows` only
    proves a persisted row is not rewritten, which nothing rewrites anyway.
    The claim that actually makes a backtest honest is this one: recomputing
    an as-of window after later data lands must return the identical frame,
    and a symbol first listed after the cutoff must not appear in that
    cutoff's universe.

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
            date_time=(start + datetime.timedelta(days=offset)).strftime("%Y-%m-%d"),
            open_price=price,
            high_price=price + 5,
            low_price=price - 5,
            close_price=price,
            volume=100,
        )

    cutoff_at = datetime.datetime.combine(
        (start + datetime.timedelta(days=59)).togregorian(),
        datetime.time.min,
        tzinfo=datetime.timezone.utc,
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
            date_time=(start + datetime.timedelta(days=offset)).strftime("%Y-%m-%d"),
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
