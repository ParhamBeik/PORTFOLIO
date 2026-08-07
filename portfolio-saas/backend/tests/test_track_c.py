import pytest
from decimal import Decimal
from django.utils import timezone
import jdatetime
import datetime
import pandas as pd
from django.core.cache import cache

from marketdata.models import MarketCandle, SymbolIntegrity, MarketIndexData, GoldCurrencyHistory, MarketInstrument
from portfolio.models import Asset, Price
from portfolio.services.returns import daily_returns_matrix, _returns_cache_key, _load_price_panel, normalize_as_of
from portfolio.services.diagnostics import portfolio_diagnostics
from portfolio.services.deflator import to_basis
from portfolio.services.optimization import optimize

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
        panel, excluded = _load_price_panel(history_days=180, as_of=timezone.now(), universe=["kama_stock", "fars_stock"])
        
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
