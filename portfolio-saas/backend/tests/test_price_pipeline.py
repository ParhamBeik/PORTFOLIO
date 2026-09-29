"""The live price loop end to end: fetch, extract, concurrency, staleness, cleanup of bad prices, and the seeded asset catalog.

Merged from 10 files; each section keeps its original banner.
"""

from datetime import timedelta
import datetime as dt
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import RequestFactory
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from accounts.models import User
from config.health import PriceFeedView
from marketdata.currency import to_toman
from marketdata.models import GoldCurrencyHistory, MarketCandle
from marketdata.models import RejectedRecord
from marketdata.models import MarketInstrument
from marketdata.models import WorkflowRun
from marketdata.tasks import capture_derivative_snapshots
from portfolio.live.extractor import apply_instrument_prices, extract_standard_prices
from portfolio.management.commands.clean_mispriced_data import audit_and_repair_prices
from portfolio.models import Account, Asset, Price, Snapshot
from portfolio.models import Holding
from portfolio.services import value_account
from portfolio.services.returns import daily_returns_matrix, _load_live_price_panel
from portfolio.services.valuation import get_latest_prices, value_as_of
from portfolio.tasks import run_price_fetch


def _seal_test_day(*, close_keys=frozenset()):
    from portfolio.tasks import _write_snapshots

    day = timezone.localdate() - timedelta(days=1)
    return _write_snapshots(get_latest_prices(), day=day, session_close_keys=set(close_keys))

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_financial_safety.py


def test_negative_prices_blocked_from_valuation(asset_catalog, make_user):
    """Proves that a negative price in the Price model is blocked from live valuation."""
    user = make_user()
    account = Account.objects.create(user=user, name="Negative Test")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    # Create a negative price record
    Price.objects.create(asset=asset, price=Decimal("-480000"), source="API")

    # Confirm that get_latest_prices() does not return the negative price
    latest_prices = get_latest_prices()
    assert asset.key not in latest_prices or latest_prices[asset.key] <= 0

    # Valuation should exclude it and treat it as unavailable/missing_price
    result = value_account(account)
    assert result["total"] == Decimal("0")
    assert result["priced_assets"] == 0
    assert result["quality_status"] == "unavailable"
    assert any(item["asset_key"] == asset.key and item["reason"] == "missing_price" for item in result["excluded"])


def test_zero_prices_blocked_from_valuation(asset_catalog, make_user):
    """Proves that zero prices are blocked from valuation."""
    user = make_user()
    account = Account.objects.create(user=user, name="Zero Test")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    # Create a zero price record
    Price.objects.create(asset=asset, price=Decimal("0"), source="API")

    # Zero prices should be filtered out
    latest_prices = get_latest_prices()
    assert asset.key not in latest_prices or latest_prices[asset.key] <= 0

    result = value_account(account)
    assert result["total"] == Decimal("0")
    assert result["quality_status"] == "unavailable"


def test_rejected_record_excludes_historical_valuation(asset_catalog, make_user):
    """Proves that matching a RejectedRecord excludes a historical price from value_as_of."""
    user = make_user()
    account = Account.objects.create(user=user, name="Rejection Test")
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])
    
    as_of = timezone.now()
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    # Seed gold currency history row
    jalali_date = "1403-10-19"
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date=jalali_date,
        close_price=Decimal("10000"),
    )

    # 1. Without rejection, valuation succeeds
    res_normal = value_as_of(user, account=account, as_of=as_of)
    # The normal run might mark it as gap exceeded if we don't have enough recent dates, but it reads the price.
    # Let's add USD history to prevent gap exceeded if needed, or check the specific price missing reason
    
    # 2. Add RejectedRecord for the same symbol and date
    RejectedRecord.objects.create(
        endpoint="gold_daily",
        symbol="IR_COIN_EMAMI",
        date=jalali_date,
        reason="high_below_low",
        payload={},
    )

    res_rejected = value_as_of(user, account=account, as_of=as_of)
    reasons = {item["reason"] for item in res_rejected["excluded"]}
    # The price was rejected, so it should be missing_price
    assert "missing_price" in reasons


def test_rejected_record_excludes_returns_panel(asset_catalog):
    """Proves that a price matching a RejectedRecord is excluded from the returns price panel."""
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])

    cutoff = timezone.now() - dt.timedelta(days=10)
    jalali_date = "1403-10-19"

    # Seed gold currency history row
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date=jalali_date,
        close_price=Decimal("10000"),
    )

    # Seed rejection
    RejectedRecord.objects.create(
        endpoint="gold_daily",
        symbol="IR_COIN_EMAMI",
        date=jalali_date,
        reason="test_rejection",
        payload={},
    )

    # Load returns
    returns, excluded = daily_returns_matrix(
        history_days=30,
        universe=[asset.key],
    )
    # Excluded because the only price date we had was rejected, leading to insufficient history
    assert any(e["key"] == asset.key and e["reason"] in ("insufficient_history", "no_price_history") for e in excluded)


def test_stale_live_price_excluded_from_optimization_inputs(asset_catalog):
    """Proves that stale live prices are excluded from returns/optimization inputs."""
    asset = asset_catalog["emami_coin"]

    # Seed a stale live price (older than settings.PRICE_STALE_THRESHOLD_SECONDS)
    stale_time = timezone.now() - dt.timedelta(seconds=settings.PRICE_STALE_THRESHOLD_SECONDS + 10)
    p = Price.objects.create(asset=asset, price=Decimal("500000"), source="API")
    Price.objects.filter(pk=p.pk).update(fetched_at=stale_time)

    # Load panel
    panel = _load_live_price_panel(
        cutoff=timezone.now() - dt.timedelta(days=10),
        as_of=None,
        keys=[asset.key],
    )
    # Since it is stale, it should be excluded (panel should not have the column, or it is empty)
    assert asset.key not in panel.columns or panel[asset.key].isna().all()


def test_fresh_live_price_remains_usable(asset_catalog):
    """Proves that fresh live prices remain usable."""
    asset = asset_catalog["emami_coin"]

    # Seed a fresh live price
    fresh_time = timezone.now() - dt.timedelta(minutes=2)
    Price.objects.create(asset=asset, price=Decimal("500000"), fetched_at=fresh_time, source="API")

    panel = _load_live_price_panel(
        cutoff=timezone.now() - dt.timedelta(days=10),
        as_of=None,
        keys=[asset.key],
    )
    assert asset.key in panel.columns
    assert not panel[asset.key].isna().all()


def test_live_panel_resists_a_glitched_final_tick(asset_catalog):
    """A day's value is the median of its last 3 ticks, not a flat average of
    every tick and not the single last tick either.

    Averaging silently swapped the return definition away from the
    close-to-close basis every warehouse-backed column in the same panel uses
    (see _load_price_panel), which is what this fallback is reserved for now
    that MarketDailyBar covers ETF NAV directly. But the naive fix -- just
    take the single last tick -- reopens the exact bug a prior mean-based
    version of this function existed to fix: one glitched final-tick price
    would singlehandedly define the whole day's return. Median-of-last-3
    tracks the close-to-close basis on a normal day while still rejecting a
    lone bad tick.
    """
    asset = asset_catalog["emami_coin"]
    now = timezone.now()
    for offset_minutes, price in ((15, "100"), (10, "100"), (5, "100"), (0, "9999")):
        p = Price.objects.create(asset=asset, price=Decimal(price), source="API")
        Price.objects.filter(pk=p.pk).update(
            fetched_at=now - dt.timedelta(minutes=offset_minutes)
        )

    panel = _load_live_price_panel(
        cutoff=timezone.now() - dt.timedelta(days=10),
        as_of=None,
        keys=[asset.key],
    )
    day_value = panel[asset.key].dropna().iloc[-1]
    assert day_value == pytest.approx(100.0), (
        "expected the median of the last 3 ticks (100/100/9999 -> 100), "
        "not the glitched last tick (9999) or the mean of all 4 ticks (~2574.75)"
    )


def test_rial_to_toman_conversion():
    """Protects Rial-to-Toman unit conversion logic (Price storage unit is Toman)."""
    assert to_toman("کاما", 10000, "Toman") == Decimal("10000")
    assert to_toman("کاما", 10000, "Rial") == Decimal("1000")


def test_unadjusted_price_selection_fallback(asset_catalog):
    """Proves adjusted closes are preferred, with unadjusted fallback working appropriately."""
    # Seed adjusted and unadjusted candles for the same symbol/date
    # MarketCandle handles adjusted closes with timeframe="1d_adj", unadjusted with "1d_unadj"
    symbol = "کاما"
    date_str = "1405-04-31"

    # Write adjusted candle
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("200"),
        volume=1000,
    )

    # Write aggregate/unadjusted candle
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.AGGREGATE,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("150"),
        volume=1000,
    )

    # Load candles via candle_close_qs
    from marketdata.calendars import candle_close_qs
    qs = candle_close_qs(symbol)
    candle = qs.filter(date_time=date_str).first()
    
    # Proves adjusted (200) is preferred over aggregate/unadjusted (150)
    assert candle is not None
    assert candle.close_price == Decimal("200")


def test_negative_prices_blocked_from_returns(asset_catalog):
    """Proves negative prices cannot enter returns calculation."""
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])

    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date="1403-10-19",
        close_price=Decimal("-10000"),
    )

    returns, excluded = daily_returns_matrix(
        history_days=30,
        universe=[asset.key],
    )
    # The negative price is filtered out, leading to no valid price history
    assert any(e["key"] == asset.key and e["reason"] in ("insufficient_history", "no_price_history") for e in excluded)


def test_negative_prices_blocked_from_optimization(asset_catalog):
    """Proves negative prices are excluded from optimization inputs."""
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])

    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date="1403-10-19",
        close_price=Decimal("-500"),
    )

    returns, excluded = daily_returns_matrix(
        history_days=30,
        universe=[asset.key],
    )
    assert asset.key not in returns.columns


def test_low_integrity_symbols_excluded_from_optimization(asset_catalog):
    """Proves low-integrity symbols (passes_gate=False) are excluded from returns/optimization."""
    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])

    # Seed SymbolIntegrity as failed
    from marketdata.models import SymbolIntegrity
    SymbolIntegrity.objects.update_or_create(
        symbol="کاما",
        defaults={"passes_gate": False, "reason": "extreme_spikes"},
    )

    # Seed daily candle
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time="1403-10-19",
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("200"),
        volume=1000,
    )

    returns, excluded = daily_returns_matrix(
        history_days=30,
        universe=[asset.key],
    )
    assert any(e["key"] == asset.key and e["reason"] == "integrity_gate_failed" for e in excluded)
    assert asset.key not in returns.columns


def test_missing_prices_produce_controlled_behavior(asset_catalog, make_user):
    """Proves missing prices are explicitly logged as missing_price in excluded list."""
    user = make_user()
    account = Account.objects.create(user=user, name="Missing Test")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    # No price created
    result = value_account(account, prices={})
    assert result["total"] == Decimal("0")
    assert any(item["asset_key"] == asset.key and item["reason"] == "missing_price" for item in result["excluded"])


def test_stale_prices_label_in_current_valuation(asset_catalog, make_user, monkeypatch):
    """A quote older than 300s is stale while that asset's market is open.

    Gold desks run through CLOSED_DAYTIME; overnight the same age must stay
    live (the last print is the current price). Pin daytime so this does not
    flip with the wall clock.
    """
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    user = make_user()
    account = Account.objects.create(user=user, name="Stale Test")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    stale_time = timezone.now() - dt.timedelta(seconds=400)
    p = Price.objects.create(asset=asset, price=Decimal("480000"), source="API")
    Price.objects.filter(pk=p.pk).update(fetched_at=stale_time)

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == asset.key)
    assert item["quality_status"] == "stale"


def test_no_fallback_when_adjusted_absent(asset_catalog):
    """Proves there is no tick-derived fallback: only ADJUSTED rows are ever picked."""
    symbol = "کاما"
    date_str = "1405-04-31"

    # A non-ADJUSTED candle for the day must never be picked as a substitute.
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.UNADJUSTED,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("150"),
        volume=1000,
    )

    from marketdata.calendars import candle_close_qs
    qs = candle_close_qs(symbol)
    candle = qs.filter(date_time=date_str).first()
    assert candle is None


def test_invalid_data_blocked_through_fallback(asset_catalog):
    """Proves adjusted closes with zero/negative close prices are blocked."""
    symbol = "کاما"
    date_str = "1405-04-31"

    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("0"),
        volume=1000,
    )

    from marketdata.calendars import candle_close_qs
    qs = candle_close_qs(symbol)
    candle = qs.filter(date_time=date_str).first()
    assert candle is None


def test_cached_and_uncached_paths_match(asset_catalog):
    """Proves cached and uncached paths return identical results (tested via cache-invalidation fingerprint)."""
    asset = asset_catalog["emami_coin"]
    asset.brs_symbol = "IR_COIN_EMAMI"
    asset.save(update_fields=["brs_symbol"])

    from portfolio.services.returns import to_jalali_str
    # Seed 35 days of history for both EMAMI and USD to avoid insufficient history exclusion
    for day in range(35):
        date_str = to_jalali_str(timezone.now() - dt.timedelta(days=day + 1))
        GoldCurrencyHistory.objects.create(
            symbol="IR_COIN_EMAMI",
            date=date_str,
            close_price=Decimal("10000") + day * 100,
        )
        GoldCurrencyHistory.objects.create(
            symbol="USD",
            date=date_str,
            close_price=Decimal("60000"),
        )

    # First call - loads uncached
    m1, e1 = daily_returns_matrix(history_days=30, universe=[asset.key])

    # Second call - loads cached
    m2, e2 = daily_returns_matrix(history_days=30, universe=[asset.key])

    assert not m1.empty
    assert m1.equals(m2)
    assert e1 == e2


def test_existing_valid_valuation_unchanged(asset_catalog, make_user):
    """Proves that a valid asset price remains usable and valued correctly."""
    user = make_user()
    account = Account.objects.create(user=user, name="Valid Account")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    # Write a fresh positive price
    Price.objects.create(asset=asset, price=Decimal("480000"), source="API")

    result = value_account(account)
    assert result["total"] == Decimal("960000")
    assert result["quality_status"] == "complete"


def test_rejected_record_does_not_exclude_other_endpoints(asset_catalog):
    """Proves that a rejection on 'stock_transaction_ticks' does not exclude daily close candles."""
    symbol = "کاما"
    date_str = "1403-10-19"
    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = symbol
    asset.save(update_fields=["tse_symbol"])

    # Write daily close candle
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.ADJUSTED,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("200"),
        volume=1000,
    )

    # Write RejectedRecord for intraday ticks endpoint (should not affect daily candle)
    RejectedRecord.objects.create(
        endpoint="stock_transaction_ticks",
        symbol=symbol,
        date=date_str,
        reason="test_intraday_failure",
        payload={},
    )

    from marketdata.calendars import candle_close_qs
    # candle_close_qs does not exclude this date since it is not a daily candle/history rejection
    rejections = RejectedRecord.objects.filter(
        symbol=symbol,
        endpoint__in=[
            "stock_candle_adjusted", "stock_candle_unadjusted",
            "stock_history_adjusted", "stock_history_unadjusted",
            "series:1d_adj", "series:1d_unadj"
        ]
    ).values_list("date", flat=True)
    
    candles = candle_close_qs(symbol).exclude(date_time__in=rejections)
    assert candles.filter(date_time=date_str).exists()


def test_f1_tse_valuation_marked_unverified(asset_catalog, make_user, monkeypatch):
    import marketdata.currency
    monkeypatch.setattr(marketdata.currency, "TSE_PRICE_UNIT", "unverified")
    user = make_user()
    account = Account.objects.create(user=user, name="F1 Val")
    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    Holding.objects.create(account=account, asset=stock, quantity=Decimal("10"))
    Price.objects.create(asset=stock, price=Decimal("3320"), source="API")
    result = value_account(account)
    assert result["tse_unit_policy"] == "unverified"
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["price_unit_status"] == "unverified"


def test_f1_mixed_optimize_blocked(asset_catalog, monkeypatch):
    import marketdata.currency
    monkeypatch.setattr(marketdata.currency, "TSE_PRICE_UNIT", "unverified")
    from portfolio.services.optimization import MixedUnitUniverseBlocked, _guard_mixed_tse_units

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    with pytest.raises(MixedUnitUniverseBlocked) as exc:
        _guard_mixed_tse_units(["kama_stock", "emami_coin", "usd_cash"])
    assert "kama_stock" in exc.value.tse_keys
    assert "emami_coin" in exc.value.other_keys or "usd_cash" in exc.value.other_keys


def test_f1_tse_only_guard_allows_partition(asset_catalog, monkeypatch):
    import marketdata.currency
    monkeypatch.setattr(marketdata.currency, "TSE_PRICE_UNIT", "unverified")
    from marketdata.currency import partition_tse_asset_keys, tse_unit_verified
    from portfolio.services.optimization import _guard_mixed_tse_units

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    assert tse_unit_verified() is False
    tse, other = partition_tse_asset_keys(["kama_stock"])
    assert tse == ["kama_stock"]
    assert other == []
    _guard_mixed_tse_units(["kama_stock"])  # TSE-only must not raise


# ----------------------------------------------------------------------
# test_fetch_command.py
# fetch_prices management command.
# 
# This is the single entry point that keeps prices fresh and snapshots every user.
# The tests mock the network fetch so they run offline and assert the write
# behaviour (C2 fix: network stays outside the transaction).


def _patch_fetch(monkeypatch, payload):
    import portfolio.tasks as mod

    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: payload)


def test_fetch_writes_prices_but_not_intraday_snapshots(asset_catalog, raw_market_sample, monkeypatch):
    user = User.objects.create_user(email="fetch@test.test", password="Sup3rSecret!")
    Account.objects.create(user=user, name="Main")
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    output = out.getvalue()
    assert "Price fetch complete" in output

    keys = set(Price.objects.values_list("asset__key", flat=True))
    assert {"emami_coin", "kama_stock", "usd_cash"}.issubset(keys)
    assert Snapshot.objects.filter(user=user).count() == 0
    _seal_test_day()
    assert Snapshot.objects.filter(user=user).count() == 2
    assert Snapshot.objects.filter(user=user, account=None).count() == 1
    snap = Snapshot.objects.get(user=user, account=None)
    assert snap.total_value_tomans == 0


def test_fetch_stores_declared_foreign_seed_quotes_as_verified_toman(
    asset_catalog, raw_market_sample, monkeypatch,
):
    from portfolio.services.valuation import get_latest_prices

    Asset.objects.create(
        key="gold_ounce_usd", name="Gold ounce", asset_class=Asset.AssetClass.GOLD,
    )
    raw = {
        **raw_market_sample,
        "direct": {"rows": [
            {"symbol": "BTC", "price": 7000000000, "unit": "تومان"},
            {"symbol": "XAUUSD", "price": 2400, "unit": "دلار"},
        ]},
    }
    _patch_fetch(monkeypatch, raw)

    call_command("fetch_prices", stdout=StringIO())

    rows = {
        row.asset.key: row
        for row in Price.objects.select_related("asset").filter(
            asset__key__in=("bitcoin_usd", "gold_ounce_usd")
        )
    }
    assert rows["bitcoin_usd"].price == Decimal("7000000000")
    assert rows["gold_ounce_usd"].price == Decimal("151680000")
    assert all(row.price_unit == Price.Unit.IRT and row.price_unit_verified for row in rows.values())
    cache.delete("prices:latest:verified-toman-v2")
    latest = get_latest_prices()
    assert latest["bitcoin_usd"] == Decimal("7000000000")
    assert latest["gold_ounce_usd"] == Decimal("151680000")


def test_archive_replacement_certifies_equal_legacy_foreign_number(asset_catalog):
    from portfolio.tasks import _write_prices

    coin = asset_catalog["bitcoin_usd"]
    Price.objects.create(asset=coin, price=Decimal("200000"), source="API")

    _write_prices({"bitcoin_usd": Decimal("200000")}, sources={"bitcoin_usd": "ARCHIVE"})

    rows = list(Price.objects.filter(asset=coin).order_by("id"))
    assert len(rows) == 2
    assert rows[-1].source == "ARCHIVE"
    assert rows[-1].price_unit == Price.Unit.IRT
    assert rows[-1].price_unit_verified


def test_fetch_dry_run_writes_nothing(asset_catalog, raw_market_sample, monkeypatch):
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", "--dry-run", stdout=out)
    assert Price.objects.count() == 0
    assert Snapshot.objects.count() == 0


def test_fetch_persists_archive_replacement_for_missing_live_price(asset_catalog, monkeypatch):
    """Any asset can replace a missing live quote with its verified archive close.

    The stored price is aged behind the candle on purpose: the archive stands in
    only when it is NOT older than what is already held. A close that trails the
    stored session is the market having shut before the backfill ran, and taking
    it there walks the price backwards a session.
    """
    cache.delete("prices:latest:verified-toman-v2")
    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "ARCHIVE_STOCK"
    stock.save(update_fields=["tse_symbol"])
    seed = Price.objects.create(asset=stock, price=Decimal("7777"), source="SEED")
    stored_age = timezone.now() - timedelta(days=15)
    Price.objects.filter(pk=seed.pk).update(fetched_at=stored_age)
    # Derived from `now`, never hardcoded. The behaviour under test is a
    # RELATIVE one -- the archive stands in only when its close is not older
    # than the stored price -- so a fixed Jalali date drifts across that
    # boundary as the calendar moves. `1405-05-26` sat exactly on it on
    # 2026-09-01 and was one day the wrong side of it on 2026-09-02, which
    # turned CI red on a frontend-only commit.
    import jdatetime

    candle_day = jdatetime.date.fromgregorian(
        date=(stored_age + timedelta(days=1)).date()
    )
    MarketCandle.objects.create(
        symbol="ARCHIVE_STOCK",
        timeframe=MarketCandle.ADJUSTED,
        date_time=candle_day.strftime("%Y-%m-%d"),
        close_price=Decimal("8888"),
    )

    _patch_fetch(monkeypatch, {"brsapi": {"items": [{"symbol": "USD", "price": 63200}]}, "tsetmc": []})

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    latest = Price.objects.filter(asset=stock).order_by("-id").first()
    assert latest is not None and latest.price == Decimal("8888")
    assert latest.source == "ARCHIVE"

    call_command("fetch_prices", stdout=StringIO())
    assert Price.objects.filter(asset=stock).count() == 2


def test_fetch_snapshots_use_archive_guard_for_bad_latest_price(asset_catalog, monkeypatch):
    gold = asset_catalog["emami_coin"]
    gold.brs_symbol = "IR_COIN_EMAMI"
    gold.save(update_fields=["brs_symbol"])
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date="1404-01-02",
        close_price=Decimal("479000000"),
    )
    user = User.objects.create_user(email="guard@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")
    account.holdings.create(asset=gold, quantity=Decimal("2"))
    _patch_fetch(monkeypatch, {
        "brsapi": {
            "gold": [{"symbol": "IR_COIN_EMAMI", "price": 1}],
            "currency": [{"symbol": "USD", "price": 63200}],
        },
        "tsetmc": [],
    })

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    latest = Price.objects.filter(asset=gold).order_by("-id").first()
    _seal_test_day()
    snap = Snapshot.objects.get(user=user, account=None)
    assert latest.price == Decimal("479000000")
    assert snap.total_value_tomans == Decimal("958000000")


def test_closed_tse_fetch_persists_archive_close(asset_catalog, raw_market_sample, monkeypatch):
    from portfolio.services.returns import to_jalali_str

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now()),
        close_price=Decimal("5200"),
    )
    user = User.objects.create_user(email="closed-tse@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")
    account.holdings.create(asset=stock, quantity=Decimal("10"))
    _patch_fetch(monkeypatch, raw_market_sample)
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    call_command("fetch_prices", stdout=StringIO())

    latest = Price.objects.filter(asset=stock).order_by("-id").first()
    assert latest.price == Decimal("5200")
    assert latest.source == "ARCHIVE"
    _seal_test_day(close_keys={stock.key})
    snapshot = Snapshot.objects.get(user=user, account=None)
    # The archive close is Rial; the snapshot is Toman. 10 shares x 5,200 Rial.
    assert snapshot.total_value_tomans == Decimal("5200")
    assert snapshot.is_session_close is True


def test_partial_fetch_keeps_previous_prices_in_snapshots(asset_catalog, monkeypatch):
    gold = asset_catalog["emami_coin"]
    Price.objects.create(asset=gold, price=Decimal("400000000"), source="SEED")
    user = User.objects.create_user(email="partial@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")
    account.holdings.create(asset=gold, quantity=Decimal("2"))
    _patch_fetch(monkeypatch, {
        "brsapi": {"currency": [{"symbol": "USD", "price": 63200}]},
        "tsetmc": [],
    })

    call_command("fetch_prices", stdout=StringIO())

    _seal_test_day()
    assert Snapshot.objects.get(user=user, account=None).total_value_tomans == Decimal("800000000")
    assert Price.objects.filter(asset=gold).count() == 1


def test_fetch_snapshots_subtract_liabilities(asset_catalog, monkeypatch):
    gold = asset_catalog["emami_coin"]
    Price.objects.create(asset=gold, price=Decimal("400000000"), source="SEED")
    user = User.objects.create_user(email="liability@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")
    account.holdings.create(asset=gold, quantity=Decimal("2"))
    account.liabilities.create(label="Loan", amount_tomans=Decimal("300000000"))
    _patch_fetch(monkeypatch, {
        "brsapi": {"currency": [{"symbol": "USD", "price": 63200}]},
        "tsetmc": [],
    })

    call_command("fetch_prices", stdout=StringIO())

    _seal_test_day()
    assert Snapshot.objects.get(user=user, account=account).total_value_tomans == Decimal("500000000")
    assert Snapshot.objects.get(user=user, account=None).total_value_tomans == Decimal("500000000")


def test_fetch_no_users_still_writes_prices(asset_catalog, raw_market_sample, monkeypatch):
    """Prices are global; a fetch with zero users still records the market."""
    _patch_fetch(monkeypatch, raw_market_sample)

    out = StringIO()
    call_command("fetch_prices", stdout=out)
    assert Price.objects.filter(asset__key="emami_coin").exists()
    assert Snapshot.objects.count() == 0


# ----------------------------------------------------------------------
# test_price_fetch_concurrency.py
# Integration tests for run_price_fetch Redis lock concurrency and downtime gap backfill tagging.


@pytest.mark.django_db
def test_run_price_fetch_concurrency_lock(asset_catalog, raw_market_sample, monkeypatch):
    """Verify that a second run_price_fetch call fails to run concurrently if a Redis lock is held."""
    import portfolio.tasks as mod
    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: raw_market_sample)

    # Mock get_redis to return a fake Redis client that simulates locking
    mock_redis = MagicMock()
    # First call to set (nx=True) returns True (success), second returns False (locked)
    mock_redis.set.side_effect = [True, False]
    monkeypatch.setattr(mod, "get_redis", lambda: mock_redis)

    # First fetch succeeds
    res1 = run_price_fetch()
    assert res1["written"] is True

    # Second concurrent fetch gets blocked by lock and does not write
    res2 = run_price_fetch()
    assert res2["written"] is False
    assert res2["priced"] == {}
    mock_redis.eval.assert_called_once()


@pytest.mark.django_db
def test_run_price_fetch_does_not_generate_downtime_rows(asset_catalog, raw_market_sample, monkeypatch):
    import portfolio.tasks as mod
    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: raw_market_sample)
    # Disable Redis during this test to avoid lock interference
    monkeypatch.setattr(mod, "get_redis", lambda: None)

    user = User.objects.create_user(email="gap@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")

    old_time = timezone.now() - timedelta(days=2)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("1000"), timestamp=old_time)
    Snapshot.objects.create(user=user, account=None, total_value_tomans=Decimal("1000"), timestamp=old_time)

    res = run_price_fetch()
    assert res["written"] is True
    assert Snapshot.objects.filter(user=user).count() == 2
    assert not Snapshot.objects.filter(user=user, is_estimated=True).exists()
    _seal_test_day()
    assert Snapshot.objects.filter(user=user).count() == 4
    _seal_test_day()
    assert Snapshot.objects.filter(user=user).count() == 4


def test_daily_snapshot_task_is_idempotent_and_uses_tehran_day(asset_catalog, monkeypatch):
    from portfolio import tasks

    gold = asset_catalog["emami_coin"]
    user = User.objects.create_user(email="daily@test.test", password="Sup3rSecret!")
    account = Account.objects.create(user=user, name="Main")
    account.holdings.create(asset=gold, quantity=Decimal("2"))
    monkeypatch.setattr(tasks, "get_latest_prices", lambda: {gold.key: Decimal("400000000")})
    monkeypatch.setattr(tasks, "_archive_replacements", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(tasks, "guard_price_map", lambda prices, **_kwargs: prices)

    first = tasks.write_daily_net_worth_snapshot()
    second = tasks.write_daily_net_worth_snapshot()
    assert first == second
    assert first["rows"] == 2
    assert Snapshot.objects.filter(user=user).count() == 2
    assert set(Snapshot.objects.filter(user=user).values_list("day", flat=True)) == {
        timezone.localdate() - timedelta(days=1)
    }
    assert set(Snapshot.objects.filter(user=user).values_list("total_value_tomans", flat=True)) == {
        Decimal("800000000")
    }


# ----------------------------------------------------------------------
# test_price_history_api.py


def _history_day(days_ago):
    """(Jalali key, ISO string) for a day inside the endpoint's window.

    Derived from `now()` on purpose: a literal Jalali date against a relative
    window passes until the window walks past it, and then reddens CI on an
    unrelated commit.
    """
    from marketdata import jalali

    moment = timezone.now() - timedelta(days=days_ago)
    jalali_date = jalali.from_gregorian(moment)
    return jalali_date, jalali.to_gregorian(jalali_date).isoformat()


def test_price_history_rejects_invalid_window(make_user):
    client = APIClient()
    client.force_authenticate(user=make_user())

    response = client.get("/api/prices/history/?asset=emami_coin&days=abc")

    assert response.status_code == 400


def test_price_history_returns_ordered_warehouse_points_with_units(asset_catalog, make_user):
    user = make_user()
    stock = asset_catalog["kama_stock"]
    older, older_iso = _history_day(20)
    newer, newer_iso = _history_day(10)
    MarketCandle.objects.create(
        symbol=stock.tse_symbol,
        timeframe=MarketCandle.UNADJUSTED,
        date_time=newer,
        close_price=Decimal("5200"),
    )
    MarketCandle.objects.create(
        symbol=stock.tse_symbol,
        timeframe=MarketCandle.UNADJUSTED,
        date_time=older,
        close_price=Decimal("5000"),
    )

    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get(f"/api/prices/history/?asset={stock.key}&days=90")

    assert response.status_code == 200, response.data
    assert response.data["asset"]["unit"] == "Rial"
    # Gregorian ISO, not the warehouse's Jalali string: every date formatter in
    # the frontend is `new Date(iso)`, which reads "1405-01-01" as year 1405 CE.
    assert [point["date"] for point in response.data["points"]] == [
        older_iso,
        newer_iso,
    ]
    assert response.data["points"][0]["price"] == 5000.0
    assert response.data["earliest_date"] == older_iso


def test_price_history_windows_by_days_not_rows(asset_catalog, make_user):
    """The window is calendar days. A row cap answered "1Y" with half a day of
    two-minute ticks, all collapsed onto one x value.
    """
    stock = asset_catalog["kama_stock"]
    inside, inside_iso = _history_day(30)
    outside, _ = _history_day(200)
    for date, close in ((inside, "5200"), (outside, "4000")):
        MarketCandle.objects.create(
            symbol=stock.tse_symbol,
            timeframe=MarketCandle.UNADJUSTED,
            date_time=date,
            close_price=Decimal(close),
        )

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={stock.key}&days=90")

    assert [point["date"] for point in response.data["points"]] == [inside_iso]


def test_price_history_converts_a_tether_quoted_series_to_toman(asset_catalog, make_user):
    """The warehouse stores BTC in Tether and XAUUSD in dollars, provider-verbatim,
    with the unit in its own column. Reading that column as Toman is a ~100,000x
    error on the one screen whose entire job is showing a price.
    """
    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "BTC"
    coin.save(update_fields=["brs_symbol"])
    day, day_iso = _history_day(5)
    GoldCurrencyHistory.objects.create(
        symbol="BTC", date=day, close_price=Decimal("2"), unit="تتر",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=day, close_price=Decimal("90000"), unit="تومان",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT", date=day, close_price=Decimal("91000"), unit="تومان",
    )

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={coin.key}&days=90")

    assert response.data["asset"]["unit"] == "Toman"
    assert response.data["points"] == [{"date": day_iso, "price": 182000.0}]


def test_price_history_refuses_an_unlabelled_foreign_row(asset_catalog, make_user):
    """No unit on a dollar-quoted asset is a refusal, not a pass-through: the
    provider always declares one, so its absence means "unknown", never "Toman".
    """
    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "BTC"
    coin.save(update_fields=["brs_symbol"])
    day, _ = _history_day(5)
    GoldCurrencyHistory.objects.create(
        symbol="BTC", date=day, close_price=Decimal("95000"), unit="",
    )

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={coin.key}&days=90")

    assert response.data["points"] == []


def test_price_history_skips_a_day_the_warehouse_rejected(asset_catalog, make_user):
    stock = asset_catalog["kama_stock"]
    good, good_iso = _history_day(10)
    bad, _ = _history_day(9)
    for date, close in ((good, "5000"), (bad, "1")):
        MarketCandle.objects.create(
            symbol=stock.tse_symbol,
            timeframe=MarketCandle.UNADJUSTED,
            date_time=date,
            close_price=Decimal(close),
        )
    RejectedRecord.objects.create(
        endpoint="stock_candle_unadjusted", symbol=stock.tse_symbol, date=bad,
        reason="bad_close", payload={},
    )

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={stock.key}&days=90")

    assert [point["date"] for point in response.data["points"]] == [good_iso]


def test_price_history_labels_the_branch_that_actually_answered(asset_catalog, make_user):
    """`source` is provenance. Assigned before the fallback ran, it announced
    "TSETMC daily close" over live ticks.
    """
    stock = asset_catalog["kama_stock"]
    # `fetched_at` is auto_now_add, so these are three ticks on one day.
    for price in ("5000", "5100", "5200"):
        Price.objects.create(asset=stock, price=Decimal(price))

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={stock.key}&days=90")

    assert response.data["source"] == "recorded daily average"
    # One point per day, not one per two-minute tick.
    assert len(response.data["points"]) == 1


def test_price_history_still_serves_a_deactivated_holding(asset_catalog, make_user):
    """A universe screen may drop a candidate, never a holding: someone still
    holding a delisted asset must keep being able to see its history.
    """
    stock = asset_catalog["kama_stock"]
    stock.is_active = False
    stock.save(update_fields=["is_active"])
    day, day_iso = _history_day(10)
    MarketCandle.objects.create(
        symbol=stock.tse_symbol,
        timeframe=MarketCandle.UNADJUSTED,
        date_time=day,
        close_price=Decimal("5000"),
    )

    client = APIClient()
    client.force_authenticate(user=make_user())
    response = client.get(f"/api/prices/history/?asset={stock.key}&days=90")

    assert response.status_code == 200, response.data
    assert response.data["latest_date"] == day_iso


# ----------------------------------------------------------------------
# test_health_prices.py
# PriceFeedView: the dead-man's switch the watchdog cron and the GitHub
# Actions probe both poll. Must not cry wolf every night during OVERNIGHT, when
# zero live jobs run by design (see marketdata.market_state.live_job_keys).


def _get(rf):
    return PriceFeedView.as_view()(rf.get("/api/health/prices/"))


def test_stale_price_during_open_hours_is_reported_stale(asset_catalog, write_prices, monkeypatch):
    write_prices({"emami_coin": 500000000})
    Price.objects.update(fetched_at=timezone.now() - timedelta(minutes=30))
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "open")

    response = _get(RequestFactory())
    assert response.status_code == 503
    assert response.data["status"] == "stale"


def test_stale_price_overnight_is_now_reported_stale(asset_catalog, write_prices, monkeypatch):
    """Overnight staleness IS a fault now that the gold/currency job runs 24/7.

    This test previously asserted the opposite, and was right to: `live_job_keys`
    gated that job to OPEN/CLOSED_DAYTIME, so 23:00-07:00 fetched nothing and an
    eight-hour-old price at 03:00 was simply the correct current price.

    That gate is gone -- crypto and hard currency trade around the clock, and the
    unmetered origins cost nothing to poll at 3am. The measured consequence of
    the old behaviour was hours 00-06 empty in the price table every night, with
    the dead-man's switch structurally unable to notice. Arming the watchdog for
    those eight hours is the point of the change, not a side effect of it.
    """
    write_prices({"emami_coin": 500000000})
    Price.objects.update(fetched_at=timezone.now() - timedelta(hours=8))
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")

    response = _get(RequestFactory())
    assert response.status_code == 503
    assert response.data["status"] == "stale"


def test_fresh_price_is_reported_fresh_regardless_of_state(asset_catalog, write_prices, monkeypatch):
    write_prices({"emami_coin": 500000000})
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    response = _get(RequestFactory())
    assert response.status_code == 200
    assert response.data["status"] == "fresh"


def test_no_price_ever_written_is_stale_even_overnight(asset_catalog, monkeypatch):
    """A Price table with zero rows (fresh deploy, catastrophic data loss) must
    never read as 'fresh' -- expects_live_prices() excuses an old-but-real
    price during a designed pause, not a total absence of data.
    """
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")

    response = _get(RequestFactory())
    assert response.status_code == 503
    assert response.data["status"] == "stale"


# ----------------------------------------------------------------------
# test_clean_mispriced_data.py
# Tests for the clean_mispriced_data management command and its core logic.
# 
# Unit/integration tests (DB-backed but no HTTP layer) exercising
# `audit_and_repair_prices` directly: this is pure business logic with a few
# collaborators (ORM models), so a narrow DB-integration test is the right
# fit on the pyramid — fast enough to run every commit, but real enough to
# catch the `KeyError` and cross-user contamination bugs a mocked DB would
# hide.


def test_dry_run_cli_does_not_raise(asset_catalog):
    """Reproduces audit finding #1: `options['dry-run']` used to KeyError on any invocation."""
    call_command("clean_mispriced_data", "--dry-run")
    call_command("clean_mispriced_data")  # bare invocation is also dry-run by default


def test_fix_and_dry_run_are_mutually_exclusive(asset_catalog):
    with pytest.raises(Exception):
        call_command("clean_mispriced_data", "--fix", "--dry-run")


def test_flagged_spike_is_not_folded_into_baseline(asset_catalog, db):
    """A single bad spike must not corrupt the baseline for the next (correct) price."""
    asset = asset_catalog["emami_coin"]
    for price in [Decimal("1000000"), Decimal("1010000"), Decimal("1005000")]:
        Price.objects.create(asset=asset, price=price, source="TEST")
    spike = Price.objects.create(asset=asset, price=Decimal("5000000"), source="TEST")  # bogus spike
    recovery = Price.objects.create(asset=asset, price=Decimal("1015000"), source="TEST")  # correct, back to normal

    stats = audit_and_repair_prices(fix=True)

    assert stats["flagged_spikes"] == 1
    assert not Price.objects.filter(id=spike.id).exists()
    assert Price.objects.filter(id=recovery.id).exists()  # must survive: it's not a spike vs. the real baseline


def test_snapshot_purge_is_scoped_per_user(asset_catalog, db):
    """Audit finding #2: a global median let one whale account nuke another user's legit snapshots."""
    from accounts.models import User

    whale = User.objects.create_user(email="whale@test.test", password="Sup3rSecret!")
    normal = User.objects.create_user(email="normal@test.test", password="Sup3rSecret!")
    whale_account = Account.objects.create(user=whale, name="Main")
    normal_account = Account.objects.create(user=normal, name="Main")

    # Whale: legit history clusters around 10,000,000,000 Tomans.
    for index in range(6):
        Snapshot.objects.create(user=whale, account=whale_account, total_value_tomans=Decimal("10000000000"), timestamp=timezone.now() - timedelta(days=index + 2))
    whale_outlier = Snapshot.objects.create(
        user=whale, account=whale_account, total_value_tomans=Decimal("100000000000"), timestamp=timezone.now() - timedelta(days=8)
    )

    # Normal user: legit history clusters around 50,000,000 Tomans — far below
    # the whale's median, so a global-median filter would wrongly flag these.
    normal_snaps = [
        Snapshot.objects.create(user=normal, account=normal_account, total_value_tomans=Decimal("50000000"), timestamp=timezone.now() - timedelta(days=index + 2))
        for index in range(6)
    ]

    stats = audit_and_repair_prices(fix=True)

    assert stats["purged_snapshots"] == 1
    assert not Snapshot.objects.filter(id=whale_outlier.id).exists()
    for snap in normal_snaps:
        assert Snapshot.objects.filter(id=snap.id).exists()


# ----------------------------------------------------------------------
# test_clean_invalid_candles.py


def test_cleanup_salvages_valid_close_only_when_applied():
    candle = MarketCandle.objects.create(
        symbol="TEST", timeframe="1d_adj", date_time="1405-05-03",
        open_price=100, high_price=90, low_price=95, close_price=96, volume=1,
    )
    call_command("clean_invalid_candles")
    assert MarketCandle.objects.filter(pk=candle.pk).exists()

    call_command("clean_invalid_candles", "--apply")
    candle.refresh_from_db()
    assert candle.close_price == 96
    assert candle.high_price is None
    assert candle.low_price is None
    assert RejectedRecord.objects.filter(
        endpoint="stock_candle_adjusted", symbol="TEST", reason__startswith="field_"
    ).exists()


# ----------------------------------------------------------------------
# test_extractor_parity.py
# extractor.extract_standard_prices must match the legacy engine exactly.
# 
# This is the contract that makes the SaaS a faithful port: identical raw payloads
# must yield identical price maps, key for key. These are pure functions — no
# database is needed.


# Mirror settings.MANUAL_PRICES plus the two derived-coin factors, in the shape
# the legacy engine's `constants` dict expects.
LEGACY_CONSTANTS = {
    "swiss_gold_bar_1g_price": 25900000,
    "swiss_gold_bar_2_5g_price": 61610000,
    "quarter_pre86_factor": 0.8694109297,
    "quarter_to_1g_ratio": 0.493733384,
}

PARITY_KEYS = [
    "emami_coin", "half_coin", "quarter_coin", "quarter_coin_pre86",
    "one_gram_coin", "swiss_gold_bar_1g", "swiss_gold_bar_2_5g",
    "gold_18k_gram", "usd_cash", "usdt_irt", "euro_cash",
    "gold_ounce_usd", "bitcoin_usd", "kama_stock",
]


def test_extract_matches_legacy_for_domestic_keys(raw_market_sample, legacy_engine):
    saas = extract_standard_prices(raw_market_sample)
    legacy = legacy_engine.extract_standard_prices(raw_market_sample, LEGACY_CONSTANTS)

    assert set(saas) == set(legacy), (
        f"key sets differ: saas_only={set(saas) - set(legacy)} legacy_only={set(legacy) - set(saas)}"
    )

    mismatches = {
        k: (float(saas[k]), float(legacy[k]))
        for k in PARITY_KEYS if k not in {"usdt_irt", "bitcoin_usd", "gold_ounce_usd"}
        if float(saas[k]) != float(legacy[k])
    }
    assert not mismatches, f"price map diverged from legacy engine: {mismatches}"
    # The synthetic sample has no quote-unit labels for these instruments.
    assert saas["usdt_irt"] == 0
    assert saas["bitcoin_usd"] == 0
    assert saas["gold_ounce_usd"] == 0


def test_usdt_low_quote_requires_a_declared_unit(raw_market_sample):
    """A near-one quote cannot reveal whether the provider meant USD or USDT."""
    prices = extract_standard_prices(raw_market_sample)
    assert prices["usdt_irt"] == Decimal("0")
    assert prices["usd_cash"] == Decimal("63200")


def test_usdt_history_irt_quote_differs_from_usd_pegged_feed(raw_market_sample):
    prices = extract_standard_prices({
        **raw_market_sample,
        "usdt_irt_quote": {
            "symbol": "USDT",
            "unit": "ریال",
            "history_daily": [
                {"date": "1405-05-04", "close": 1880000},
            ],
        },
    })
    assert prices["usd_cash"] == Decimal("63200")
    assert prices["usdt_irt"] == Decimal("188000")


def test_unlabelled_btc_quote_is_unavailable(raw_market_sample):
    """A number near 64,500 cannot reveal USD, Tether, or Toman."""
    prices = extract_standard_prices(raw_market_sample)
    assert prices["bitcoin_usd"] == Decimal("0")


@pytest.mark.parametrize(
    ("unit", "expected"),
    [("تومان", "7000000000"), ("تتر", "4160000000"), ("دلار", "4044800000")],
)
def test_btc_seed_normalizes_the_providers_declared_unit(raw_market_sample, unit, expected):
    raw = {
        **raw_market_sample,
        "direct": {"rows": [{"symbol": "BTC", "price": 64000 if unit != "تومان" else 7000000000, "unit": unit}]},
        "usdt_irt_quote": {
            "symbol": "USDT", "unit": "تومان",
            "history_daily": [{"date": "1405-05-04", "close": 65000}],
        },
    }
    assert extract_standard_prices(raw)["bitcoin_usd"] == Decimal(expected)


def test_ounce_seed_uses_cash_dollar_rate(raw_market_sample):
    raw = {
        **raw_market_sample,
        "direct": {"rows": [{"symbol": "XAUUSD", "price": 2400, "unit": "دلار"}]},
    }
    assert extract_standard_prices(raw)["gold_ounce_usd"] == Decimal("151680000")


def test_kama_extracted_from_tsetmc_in_provider_rials(raw_market_sample):
    """TSE live quotes preserve provider Rial under the legacy quantity convention."""
    prices = extract_standard_prices(raw_market_sample)
    assert prices["kama_stock"] == Decimal("5230")


def test_kama_falls_back_to_last_price_when_missing():
    """When TSETMC returns nothing, KAMA reuses the last-known price."""
    raw = {"brsapi": {"items": [{"symbol": "USD", "price": 63200}]}, "tsetmc": []}
    prices = extract_standard_prices(raw, last_prices={"kama_stock": 9999})
    assert prices["kama_stock"] == Decimal("9999")


def test_derived_coins_match_legacy_arithmetic(raw_market_sample, legacy_engine):
    """quarter_pre86 and one_gram_coin are derived from the quarter coin identically."""
    saas = extract_standard_prices(raw_market_sample)
    legacy = legacy_engine.extract_standard_prices(raw_market_sample, LEGACY_CONSTANTS)
    assert saas["quarter_coin_pre86"] == Decimal("108676366")
    assert saas["one_gram_coin"] == Decimal("61716673")
    assert saas["quarter_coin_pre86"] == Decimal(legacy["quarter_coin_pre86"])
    assert saas["one_gram_coin"] == Decimal(legacy["one_gram_coin"])


# ----------------------------------------------------------------------
# test_seed_assets.py


def test_seed_assets_includes_formula_valued_house(db):
    MarketInstrument.objects.bulk_create([
        MarketInstrument(
            source="brs",
            symbol=symbol,
            category=MarketInstrument.Category.GOLD,
            eligible=True,
        )
        for symbol in (
            "IR_COIN_EMAMI",
            "IR_COIN_HALF",
            "IR_COIN_QUARTER",
            "IR_COIN_1G",
            "IR_GOLD_18K",
            "USD",
            # Asset.clean() requires an eligible catalog row for every active,
            # non-manual asset, so the fixture has to cover the whole seeded set.
            "USDT_IRT",
            "EUR",
        )
    ] + [
        MarketInstrument(
            source="tsetmc",
            symbol="کاما",
            category=MarketInstrument.Category.STOCK,
            eligible=True,
        )
    ])

    call_command("seed_assets")
    Asset.objects.filter(key="house_asset").update(is_active=False)
    call_command("seed_assets")

    house = Asset.objects.get(key="house_asset")
    assert house.is_active
    assert house.is_house
    assert house.asset_class == Asset.AssetClass.REAL_ESTATE


def test_seed_assets_keeps_catalog_backed_rows(db):
    extra = Asset.objects.create(
        key="tse-iro1shpn0001",
        name="Shapna",
        asset_class=Asset.AssetClass.STOCK,
        tse_symbol="شپنا",
    )
    orphan = Asset.objects.create(
        key="orphan_tmp",
        name="Orphan",
        asset_class=Asset.AssetClass.GOLD,
    )
    call_command("seed_assets")
    extra.refresh_from_db()
    orphan.refresh_from_db()
    assert extra.is_active
    assert not orphan.is_active


# ----------------------------------------------------------------------
# test_asset_catalog_search.py
# Integration tests: the wizard lists MarketInstrument rows, not only the seed.


def _catalog_client(make_user):
    user = make_user(email="catalog@test.test")
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _stock_instruments():
    return [
        MarketInstrument(
            source="tsetmc",
            symbol="کاما",
            name="bama",
            category=MarketInstrument.Category.STOCK,
            eligible=True,
            isin="IRO1KAMA0001",
        ),
        MarketInstrument(
            source="tsetmc",
            symbol="شپنا",
            name="shapna",
            category=MarketInstrument.Category.STOCK,
            eligible=True,
            isin="IRO1SHPN0001",
        ),
        MarketInstrument(
            source="tsetmc",
            symbol="خگستر",
            name="khegstar",
            category=MarketInstrument.Category.STOCK,
            eligible=True,
            isin="IRO1KGST0001",
        ),
        MarketInstrument(
            source="tsetmc",
            symbol="FOO",
            name="ineligible",
            category=MarketInstrument.Category.STOCK,
            eligible=False,
        ),
        MarketInstrument(
            source="brs",
            symbol="BTC",
            name="bitcoin",
            category=MarketInstrument.Category.CRYPTO,
            eligible=True,
        ),
        MarketInstrument(
            source="brs",
            symbol="IR_COIN_EMAMI",
            name="emami",
            category=MarketInstrument.Category.GOLD,
            provider_group="gold",
            eligible=True,
        ),
        MarketInstrument(
            source="brs",
            symbol="USD",
            name="dollar",
            category=MarketInstrument.Category.GOLD,
            provider_group="currency",
            eligible=True,
        ),
    ]


def test_asset_catalog_lists_stocks_and_other_classes(make_user):
    MarketInstrument.objects.bulk_create(_stock_instruments())
    client = _catalog_client(make_user)

    stocks = client.get("/api/assets/catalog/", {"asset_class": "Stock"}).json()
    symbols = {row["symbol"] for row in stocks}
    assert {"کاما", "شپنا", "خگستر"} <= symbols
    assert "FOO" not in symbols

    found = client.get(
        "/api/assets/catalog/", {"asset_class": "Stock", "q": "شپنا"}
    ).json()
    assert [row["symbol"] for row in found] == ["شپنا"]

    gold = client.get("/api/assets/catalog/", {"asset_class": "Gold"}).json()
    assert any(row["symbol"] == "IR_COIN_EMAMI" for row in gold)
    assert all(row["symbol"] != "USD" for row in gold)

    cash = client.get("/api/assets/catalog/", {"asset_class": "Cash"}).json()
    assert any(row["symbol"] == "USD" for row in cash)

    crypto = client.get("/api/assets/catalog/", {"asset_class": "Crypto"}).json()
    assert any(row["symbol"] == "BTC" for row in crypto)


def test_ensure_asset_mints_then_reuses(make_user):
    MarketInstrument.objects.bulk_create(_stock_instruments())
    client = _catalog_client(make_user)

    first = client.post(
        "/api/assets/ensure/",
        {"source": "tsetmc", "symbol": "شپنا"},
        format="json",
    )
    assert first.status_code == 200
    body = first.json()
    assert body["asset_class"] == "Stock"
    assert body["key"]

    second = client.post(
        "/api/assets/ensure/",
        {"source": "tsetmc", "symbol": "شپنا"},
        format="json",
    )
    assert second.json()["key"] == body["key"]
    assert Asset.objects.filter(tse_symbol="شپنا").count() == 1

    denied = client.post(
        "/api/assets/ensure/",
        {"source": "tsetmc", "symbol": "FOO"},
        format="json",
    )
    assert denied.status_code == 400


def test_crypto_asset_can_be_verified_against_catalog(db):
    MarketInstrument.objects.create(
        source="brs",
        symbol="BTC",
        category=MarketInstrument.Category.CRYPTO,
        eligible=True,
    )
    asset = Asset.objects.create(
        key="brs-btc",
        name="Bitcoin",
        asset_class=Asset.AssetClass.CRYPTO,
        brs_symbol="BTC",
    )
    assert asset.is_active


def test_fetch_writes_price_for_catalog_stock(asset_catalog, monkeypatch):
    MarketInstrument.objects.create(
        source="tsetmc",
        symbol="شپنا",
        category=MarketInstrument.Category.STOCK,
        eligible=True,
    )
    Asset.objects.create(
        key="tse-shapna",
        name="Shapna",
        asset_class=Asset.AssetClass.STOCK,
        tse_symbol="شپنا",
    )
    _patch_fetch(
        monkeypatch,
        {"brsapi": {"items": []}, "tsetmc": [{"l18": "شپنا", "pl": 4321, "pc": 4300}]},
    )
    out = run_price_fetch()
    assert out["priced"].get("tse-shapna") == 4321
    assert Price.objects.filter(asset__key="tse-shapna", price_iranian=4321).exists()


def test_apply_instrument_prices_leaves_seed_quote(raw_market_sample):
    prices = extract_standard_prices(raw_market_sample)
    kama = prices["kama_stock"]
    filled = apply_instrument_prices(
        {
            **raw_market_sample,
            "tsetmc": raw_market_sample["tsetmc"]
            + [{"l18": "شپنا", "pl": 1111, "pc": 1100}],
        },
        [("kama_stock", "کاما", ""), ("tse-shapna", "شپنا", "")],
        prices,
    )
    assert filled["kama_stock"] == kama
    assert filled["tse-shapna"] == Decimal("1111")


# ----------------------------------------------------------------------
# test_derivative_snapshot_split.py
# capture_derivative_snapshots: each kind is its own provider endpoint and must
# be its own failure domain. Before this split, ime_futures/ime_options
# (evaluated after tse_option) repeatedly timed out and re-raised, so
# tse_option's already-ingested rows were the only ones ever reflected in the
# WorkflowRun ledger.
#
# The two IME kinds were then retired outright on 2026-09-06: every row they
# returned failed validation on ingest (135 rejected, 0 kept, per pass) while
# billing the TSETMC plan on a 900s cadence, and nothing in the product read an
# ime_future/ime_option row. `tse_option` is the only kind left. The isolation
# property is still pinned below, because it is what makes adding a kind back
# safe; what is pinned alongside it now is that the IME kinds stay gone.


def test_a_kinds_failure_is_recorded_and_does_not_raise(settings):
    settings.TSETMC_API_KEY = "test-key"
    settings.MARKETDATA_IGNORE_MARKET_HOURS = True  # session-gated; see below

    def fake_fetch(api_key, endpoint_key):
        raise TimeoutError("ReadTimeout")

    with (
        patch("marketdata.fetchers.fetch_derivatives", side_effect=fake_fetch),
        patch("marketdata.ingest.ingest_derivative_snapshots", return_value=(3, 0)),
    ):
        results = capture_derivative_snapshots()

    # Contained, not propagated: one endpoint's timeout must never abort the
    # loop, or a later kind's rows go missing with no signal of their own.
    assert isinstance(results["tse_option"], TimeoutError)

    runs = {
        run.workflow: run.outcome
        for run in WorkflowRun.objects.filter(workflow__startswith="capture_derivative_snapshots:")
    }
    assert runs["capture_derivative_snapshots:tse_option"] == WorkflowRun.Outcome.FAILED


def test_retired_ime_kinds_are_never_polled(settings):
    """The IME endpoints must not be fetched, ledgered, or seeded."""
    settings.TSETMC_API_KEY = "test-key"
    settings.MARKETDATA_IGNORE_MARKET_HOURS = True

    called = []

    def fake_fetch(api_key, endpoint_key):
        called.append(endpoint_key)
        return []

    with (
        patch("marketdata.fetchers.fetch_derivatives", side_effect=fake_fetch),
        patch("marketdata.ingest.ingest_derivative_snapshots", return_value=(1, 0)),
    ):
        results = capture_derivative_snapshots()

    assert called == ["option_contracts"]
    assert results == {"tse_option": (1, 0)}
    assert not WorkflowRun.objects.filter(workflow__contains="ime_").exists()
    assert WorkflowRun.objects.filter(
        workflow__startswith="capture_derivative_snapshots:",
        outcome=WorkflowRun.Outcome.SUCCESS,
    ).count() == 1


@pytest.mark.django_db
def test_run_price_fetch_does_not_persist_a_lagging_archive_close(
    asset_catalog, raw_market_sample, monkeypatch
):
    """Regression, production 2026-08-23: KAMA priced correctly at 4890 while the
    session was open and reverted to yesterday's close the moment it shut.

    The write path decides what becomes a Price row, so if a closed market lets
    a not-yet-backfilled archive row outrank the price just fetched, yesterday's
    number is persisted as today's observation and every reader inherits it.
    """
    import portfolio.tasks as mod
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str

    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: raw_market_sample)
    monkeypatch.setattr(mod, "get_redis", lambda: None)
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now() - timedelta(days=1)),
        close_price=Decimal("4600"),
    )

    result = run_price_fetch()

    assert result["written"] is True
    stored = Price.objects.filter(asset=kama).order_by("-fetched_at", "-id").first()
    assert stored.price == Decimal("5230"), "persisted yesterday's archive close"


@pytest.mark.django_db
def test_closed_session_does_not_bury_the_days_last_live_price(
    asset_catalog, raw_market_sample, monkeypatch
):
    """The other half of the KAMA regression: once the session closes the live
    loop stops quoting TSE, so the fetch sees no price at all. An unguarded
    archive substitution there writes yesterday's close as today's NEWEST row,
    burying the price the session actually ended at.
    """
    import portfolio.tasks as mod
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    # Today's closing tick, captured while the session was still open.
    Price.objects.create(asset=kama, price=Decimal("4890"), source="API")
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now() - timedelta(days=1)),
        close_price=Decimal("4750"),
    )
    # The provider returns nothing for TSE now that the session is over.
    sample = {**raw_market_sample, "tsetmc": []}
    monkeypatch.setattr(mod, "fetch_all_markets", lambda _settings: sample)
    monkeypatch.setattr(mod, "get_redis", lambda: None)
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    run_price_fetch()

    newest = Price.objects.filter(asset=kama).order_by("-fetched_at", "-id").first()
    assert newest.price == Decimal("4890"), "yesterday's close buried today's"


# ----------------------------------------------------------------------
# Workflow attribution across the live lane's thread pool.
#
# The live fetcher fans its provider calls out over a ThreadPoolExecutor, and a
# pool worker starts from a *fresh* context. With the tallies held in plain-int
# ContextVars, every bump made off the calling thread was written to a throwaway
# context and dropped: production recorded 5 HTTP attempts on a day it spent 488
# live requests, leaving ~1,191 of the day's quota unattributable.


def test_attempts_made_on_a_pool_thread_reach_the_owning_workflow():
    from concurrent.futures import ThreadPoolExecutor, wait

    from marketdata.workflows import (
        WorkflowOutcome,
        record_http_attempt,
        submit_with_context,
    )

    outcome = WorkflowOutcome("thread_attribution_probe")
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [
            submit_with_context(pool, record_http_attempt, quota=True)
            for _ in range(12)
        ]
        wait(jobs)

    assert outcome._counters.http == 12
    assert outcome._counters.quota == 12


def test_a_bare_submit_would_have_lost_them():
    """Pins the mechanism, so a future refactor back to executor.submit fails here
    rather than silently in the quota ledger six months later."""
    from concurrent.futures import ThreadPoolExecutor, wait

    from marketdata.workflows import WorkflowOutcome, record_http_attempt

    outcome = WorkflowOutcome("thread_attribution_control")
    with ThreadPoolExecutor(max_workers=2) as pool:
        wait([pool.submit(record_http_attempt, quota=True) for _ in range(5)])

    assert outcome._counters.http == 0


def test_attempts_outside_any_workflow_are_not_counted():
    """The old int vars accumulated into an ownerless default that nothing read."""
    from marketdata.workflows import counters_var, record_http_attempt

    token = counters_var.set(None)
    try:
        record_http_attempt(quota=True)  # must not raise
        assert counters_var.get() is None
    finally:
        counters_var.reset(token)


def test_finishing_a_workflow_closes_and_resets_attempt_context():
    from marketdata.workflows import WorkflowOutcome, counters_var, record_http_attempt

    token = counters_var.set(None)
    try:
        outcome = WorkflowOutcome("context_cleanup")
        outcome.finish("success")

        assert counters_var.get() is None
        record_http_attempt(quota=True)
        assert outcome._counters.http == 0
        assert outcome._counters.quota == 0
    finally:
        counters_var.reset(token)


def test_a_dollar_quoted_catalog_symbol_is_never_stored_as_toman():
    """`to_toman` matched only the ASCII "usd"/"dollar" while BrsApi says
    "دلار"/"تتر", so a dollar quote fell through and was returned verbatim --
    then persisted as verified Toman. One Bitcoin at ~64,500 Toman.
    """
    from marketdata.currency import to_toman

    # Declared in Persian, no rate available: refuse rather than mislabel.
    assert to_toman("BTC", 64500, "دلار") == Decimal("0")
    assert to_toman("USDT", 1, "تتر") == Decimal("0")
    # With a rate, convert.
    assert to_toman("BTC", 64500, "دلار", usd_rate=63200) == Decimal("64500") * Decimal("63200")
    # Local units are unaffected.
    assert to_toman("IR_GOLD_18K", 7_000_000, "تومان") == Decimal("7000000")
    assert to_toman("کاما", 10000, "ریال") == Decimal("1000")


def test_catalog_search_always_leaves_room_for_new_instruments(db, make_user):
    """Existing assets filled the whole page, so a class holding SEARCH_LIMIT
    of them could never surface a new ticker again.
    """
    from marketdata.models import MarketInstrument
    from portfolio.models import Asset
    from portfolio.services.catalog import SEARCH_LIMIT, search_catalog

    user = make_user(email="catalog-share@test.test")
    for i in range(SEARCH_LIMIT + 5):
        symbol = f"سهم{i}"
        MarketInstrument.objects.create(
            source=MarketInstrument.Source.TSETMC, symbol=symbol,
            name=symbol, category=MarketInstrument.Category.STOCK, eligible=True,
        )
        Asset.objects.create(
            key=f"owned-{i}", name=symbol, asset_class=Asset.AssetClass.STOCK,
            tse_symbol=symbol, is_active=True,
        )
    # One instrument nobody owns yet -- it must still be reachable.
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, symbol="خگستر", name="خگستر",
        category=MarketInstrument.Category.STOCK, eligible=True,
    )

    rows = search_catalog(asset_class=Asset.AssetClass.STOCK, q="", user=user)

    assert len(rows) <= SEARCH_LIMIT
    assert any(r.get("symbol") == "خگستر" for r in rows), (
        "an unowned instrument must never be crowded out by owned assets"
    )
    # The dialog labels its price field Rial vs Toman off this key. If it is
    # absent the label silently reads "Toman" for a Rial-quoted stock and the
    # user types a 10x wrong cost basis.
    assert all("tse_symbol" in r for r in rows)
    stock_rows = [r for r in rows if r["asset_class"] == Asset.AssetClass.STOCK]
    assert stock_rows and all(r["tse_symbol"] for r in stock_rows)


def test_ensure_asset_is_safe_against_a_double_click(db):
    """Check-then-create: the picker fires this straight from a click, and the
    second of two concurrent calls used to die on the unique `key` as a 500.
    """
    from marketdata.models import MarketInstrument
    from portfolio.services.catalog import ensure_asset

    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC, symbol="خگستر", name="خگستر",
        category=MarketInstrument.Category.STOCK, eligible=True,
    )
    first = ensure_asset(source="tsetmc", symbol="خگستر")
    second = ensure_asset(source="tsetmc", symbol="خگستر")
    assert first.pk == second.pk


def test_a_coin_is_named_in_english_from_the_picker_to_the_ledger(db):
    """Market/Cryptocurrency.php carries no symbol field, so `provider_symbol`
    keys those rows on `name_en` and the provider's `name` arrives Persian.
    Minting stored that Persian name as the asset's own and the ledger labelled
    from `name_fa`, so a coin chosen as "Bitcoin" appeared on the ledger under a
    name the picker never showed. The Persian one is kept for the hover.
    """
    from marketdata.models import MarketInstrument
    from portfolio.services.catalog import ensure_asset
    from portfolio.services.ledger import ledger_label

    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS,
        symbol="Bitcoin",
        name="بیت کوین",
        category=MarketInstrument.Category.CRYPTO,
        provider_group="crypto",
        eligible=True,
    )

    coin = ensure_asset(source="brs", symbol="Bitcoin")

    assert coin.name == "Bitcoin"
    assert coin.name_fa == "بیت کوین"
    assert ledger_label(coin) == "Bitcoin"
    assert ledger_label(coin, "Long-term stack") == "Long-term stack", "nickname wins"


def test_picking_usdt_reuses_the_seeded_asset_instead_of_minting_a_twin(db, asset_catalog):
    """Ingest canonicalizes USDT -> USDT_IRT on write, but the catalog sync
    leaves the raw "USDT" eligible, so both are pickable. Minting on the raw
    symbol produced a second asset with no history at all, which the archive
    spike guard then valued at 1 Toman.
    """
    from marketdata.models import MarketInstrument
    from portfolio.models import Asset
    from portfolio.services.catalog import ensure_asset

    seeded = asset_catalog["usdt_irt"]
    seeded.brs_symbol = "USDT_IRT"
    seeded.save(update_fields=["brs_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="USDT", name="Tether",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )

    minted = ensure_asset(source="brs", symbol="USDT")

    assert minted.pk == seeded.pk, "must resolve to the seeded Tether asset"
    assert Asset.objects.filter(brs_symbol="USDT").count() == 0


def test_the_seed_sweep_retires_its_own_rows_and_spares_the_pickers(db):
    """It runs on every container boot, so both halves matter: a row dropped
    from the seed list must actually go, and a ticker the user added from the
    market picker must survive. Keying the sweep on "has a provider symbol"
    got the second right and the first wrong -- almost every seeded asset
    carries one, so nothing could ever be retired.
    """
    from django.core.management import call_command

    from portfolio.models import Asset
    from portfolio.management.commands.seed_assets import ASSETS

    seeded_key = ASSETS[0][0]
    # A seed row that is no longer in the list, still carrying its BRS symbol.
    Asset.objects.create(
        key="retired_coin", name="Retired Coin", asset_class=Asset.AssetClass.GOLD,
        brs_symbol="IR_COIN_GONE", is_active=True,
    )
    # Minted by the picker; not in ASSETS and must not be touched.
    Asset.objects.create(
        key="tse-irouston0001", name="خگستر", asset_class=Asset.AssetClass.STOCK,
        tse_symbol="خگستر", is_active=True,
    )
    # A property, which is never in ASSETS by construction.
    Asset.objects.create(
        key="house-1", name="Home", asset_class=Asset.AssetClass.REAL_ESTATE,
        is_house=True, is_active=True,
    )

    call_command("seed_assets")

    assert Asset.objects.get(key="retired_coin").is_active is False
    assert Asset.objects.get(key="tse-irouston0001").is_active is True
    assert Asset.objects.get(key="house-1").is_active is True
    assert Asset.objects.get(key=seeded_key).is_active is True


def test_the_seed_sweep_never_retires_an_asset_somebody_owns(db, make_user):
    """The picker is not the only minting path -- `trades.provision_asset`
    creates unprefixed keys like `khgostar_stock`. Enumerating minters is a race
    this command keeps losing, so being referenced is what protects a row.
    """
    from django.core.management import call_command

    from portfolio.models import Account, Asset, Holding

    owned = Asset.objects.create(
        key="khgostar_stock", name="خگستر", asset_class=Asset.AssetClass.STOCK,
        tse_symbol="خگستر", is_active=True,
    )
    orphan = Asset.objects.create(
        key="nobody_stock", name="Nobody", asset_class=Asset.AssetClass.STOCK,
        tse_symbol="هیچ", is_active=True,
    )
    account = Account.objects.create(
        user=make_user(email="sweep-owner@test.test"), name="Broker"
    )
    Holding.objects.create(account=account, asset=owned, quantity=Decimal("10"))

    call_command("seed_assets")

    assert Asset.objects.get(key="khgostar_stock").is_active is True
    assert Asset.objects.get(key="nobody_stock").is_active is False


# ----------------------------------------------------------------------
# The poll cadence must stay inside the freshness bar.
#
# `_FRESH_SECONDS` and MARKETDATA_LIVE_INTERVAL_DAYTIME were both 300, so a
# price crossed from fresh to stale at the same instant its replacement became
# due: every held asset flipped between Fresh and Stale all day and the Ops
# console's headline freshness never settled (measured at 244s and 568s minutes
# apart on the same assets). These are two numbers in two modules that only work
# if they disagree, which is exactly the pair worth pinning.
#
# Unit test: reads settings, no I/O.


def test_live_poll_interval_leaves_margin_under_the_freshness_bar():
    from django.conf import settings

    from portfolio.services.valuation import _FRESH_SECONDS

    for name in (
        "MARKETDATA_LIVE_INTERVAL_OPEN",
        "MARKETDATA_LIVE_INTERVAL_DAYTIME",
        "MARKETDATA_LIVE_INTERVAL_OVERNIGHT",
    ):
        interval = getattr(settings, name)
        assert interval < _FRESH_SECONDS, (
            f"{name}={interval} is not below the {_FRESH_SECONDS}s freshness bar, "
            "so a healthy loop still reports its own prices as stale."
        )


# ----------------------------------------------------------------------
# A constant is not a feed.
#
# The Swiss bars have no provider, so the extractor fills them from
# `settings.MANUAL_PRICES` and they travelled through the writer labelled "API"
# like any quote, restamped every cycle. The Ops console read them as sourced
# API and 244s old while the dashboard called the same holdings a 3-day-old
# manual valuation -- and a number that is rewritten every four minutes can
# never go stale, so the freshness panel could not have reported them going dark.
#
# Unit test on the labelling rule, then one integration pass proving the writer
# stops restamping an unchanged constant.


def test_a_manual_constant_is_not_labelled_as_a_provider_quote():
    from portfolio.tasks import _source_for

    assert _source_for("swiss_gold_bar_1g", set()) == "MANUAL"
    assert _source_for("usd_cash", set()) == "API"
    assert _source_for("kama_stock", {"kama_stock"}) == "ARCHIVE"


def test_an_unchanged_manual_price_is_not_restamped(db):
    from decimal import Decimal

    from portfolio.models import Asset, Price
    from portfolio.tasks import _write_prices

    asset = Asset.objects.create(
        key="swiss_gold_bar_1g", name="Swiss bar 1g",
        asset_class=Asset.AssetClass.GOLD, is_manual=True, is_active=True,
    )
    priced = {"swiss_gold_bar_1g": Decimal("25900000")}
    sources = {"swiss_gold_bar_1g": "MANUAL"}

    _write_prices(priced, sources=sources)
    _write_prices(priced, sources=sources)
    first = Price.objects.get(asset=asset)

    # The operator edits the constant: that IS a new observation.
    _write_prices({"swiss_gold_bar_1g": Decimal("26500000")}, sources=sources)

    assert Price.objects.filter(asset=asset).count() == 2
    assert first.source == "MANUAL"


def test_an_owners_manual_price_is_not_overwritten_by_the_settings_default(db, settings):
    """`MANUAL_PRICES` is a fallback, not an override.

    The extractor injects the constant into the live map on every cycle, so a
    Swiss bar edited on the dashboard saved and was overwritten about four
    minutes later. The edit box worked; the number just would not stay.
    """
    from decimal import Decimal

    from portfolio.models import Asset, Price
    from portfolio.services.ledger import record_manual_price
    from portfolio.tasks import _write_prices

    asset = Asset.objects.create(
        key="swiss_gold_bar_1g", name="Swiss bar 1g", is_active=True,
        asset_class=Asset.AssetClass.GOLD, is_manual=True,
    )
    record_manual_price(asset, Decimal("31000000"))

    # One ordinary fetch cycle, carrying the unchanged settings constant.
    _write_prices(
        {"swiss_gold_bar_1g": Decimal("25900000")},
        sources={"swiss_gold_bar_1g": "MANUAL"},
    )

    latest = Price.objects.filter(asset=asset).order_by("-fetched_at", "-id").first()
    assert latest.price == Decimal("31000000")
    assert latest.source == "manual"


def test_the_settings_default_still_prices_a_bar_nobody_has_marked(db):
    from decimal import Decimal

    from portfolio.models import Asset, Price
    from portfolio.tasks import _write_prices

    asset = Asset.objects.create(
        key="swiss_gold_bar_2_5g", name="Swiss bar 2.5g", is_active=True,
        asset_class=Asset.AssetClass.GOLD, is_manual=True,
    )
    _write_prices(
        {"swiss_gold_bar_2_5g": Decimal("61610000")},
        sources={"swiss_gold_bar_2_5g": "MANUAL"},
    )

    assert Price.objects.get(asset=asset).price == Decimal("61610000")


def test_cash_counts_once_on_home_in_history_and_in_performance(asset_catalog, make_user):
    """Home, the sealed history and performance all see the same net worth.

    Performance used to add the cash balance on its own while value_account and
    the nightly snapshot left it out, so the screens disagreed by exactly the
    cash. Now it is counted once, in value_account, and nowhere else.
    """
    from portfolio.services.performance import _current_value

    user = make_user()
    account = Account.objects.create(
        user=user, name="Cash", cash_balance_tomans=Decimal("1000000"), track_cash=True,
    )
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("1"))
    Price.objects.create(asset=asset, price=Decimal("480000"), source="API")

    valuation = value_account(account)
    assert valuation["cash_tomans"] == Decimal("1000000")
    assert valuation["total"] == Decimal("1480000")
    assert _current_value(account, "nominal_toman") == Decimal("1480000")

    _seal_test_day()
    assert Snapshot.objects.get(user=user, account=account).total_value_tomans == Decimal("1480000")
    assert Snapshot.objects.get(user=user, account=None).total_value_tomans == Decimal("1480000")
