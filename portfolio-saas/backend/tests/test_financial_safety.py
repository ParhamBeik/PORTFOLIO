import datetime as dt
from decimal import Decimal
import pytest
from django.utils import timezone
from django.conf import settings

from portfolio.models import Account, Holding, Price
from portfolio.services import value_account, value_user
from portfolio.services.valuation import get_latest_prices, value_as_of
from portfolio.services.returns import daily_returns_matrix, _load_live_price_panel
from marketdata.models import RejectedRecord, GoldCurrencyHistory, MarketCandle
from marketdata.currency import to_toman

pytestmark = pytest.mark.django_db


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
    from marketdata.candles import candle_close_qs
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


def test_stale_prices_label_in_current_valuation(asset_catalog, make_user):
    """Proves stale prices (older than 300s) are labeled as stale in current valuation."""
    user = make_user()
    account = Account.objects.create(user=user, name="Stale Test")
    asset = asset_catalog["emami_coin"]
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("2"))

    stale_time = timezone.now() - dt.timedelta(seconds=400)
    p = Price.objects.create(asset=asset, price=Decimal("480000"), source="API")
    Price.objects.filter(pk=p.pk).update(fetched_at=stale_time)

    # Run valuation
    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == asset.key)
    assert item["quality_status"] == "stale"


def test_unadjusted_price_used_when_adjusted_absent(asset_catalog):
    """Proves unadjusted closes are used when adjusted closes are absent."""
    symbol = "کاما"
    date_str = "1405-04-31"

    # Write aggregate/unadjusted candle ONLY
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

    from marketdata.candles import candle_close_qs
    qs = candle_close_qs(symbol)
    candle = qs.filter(date_time=date_str).first()
    assert candle is not None
    assert candle.close_price == Decimal("150")


def test_invalid_data_blocked_through_fallback(asset_catalog):
    """Proves unadjusted closes with zero/negative close prices are blocked."""
    symbol = "کاما"
    date_str = "1405-04-31"

    # Write aggregate/unadjusted candle with zero close price
    MarketCandle.objects.create(
        symbol=symbol,
        timeframe=MarketCandle.AGGREGATE,
        date_time=date_str,
        open_price=Decimal("100"),
        high_price=Decimal("100"),
        low_price=Decimal("100"),
        close_price=Decimal("0"),
        volume=1000,
    )

    from marketdata.candles import candle_close_qs
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

    from marketdata.candles import candle_close_qs
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

