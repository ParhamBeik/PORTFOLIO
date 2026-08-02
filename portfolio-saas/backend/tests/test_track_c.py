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

    def test_backtest_run_success(self):
        # Testing Protocol: We choose an integration test for the walk-forward simulation because it involves coordinating multiple models, optimization service states, and mock database price series across multiple years.
        from portfolio.models import BacktestRun, BacktestYear
        from portfolio.services.backtest import run_backtest

        Asset.objects.all().delete()
        BacktestRun.objects.all().delete()
        BacktestYear.objects.all().delete()

        # Seed assets
        kama = Asset.objects.create(key="kama_stock", name="Kama", tse_symbol="KAMA", is_active=True)
        fars = Asset.objects.create(key="fars_stock", name="Fars", tse_symbol="FARS", is_active=True)
        usd = Asset.objects.create(key="usd_cash", name="USD", brs_symbol="USD", asset_class="Cash", is_active=True)
        emami = Asset.objects.create(key="emami_coin", name="Emami", brs_symbol="Emami", asset_class="Gold", is_active=True)

        SymbolIntegrity.objects.create(symbol="KAMA", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="FARS", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="USD", passes_gate=True)
        SymbolIntegrity.objects.create(symbol="Emami", passes_gate=True)

        # Create history for these assets covering the 1400-01-01 to 1405-01-01 period
        # Let's seed simple daily prices
        # Jalali year 1399 to 1405
        # For simplicity, we seed candles and gold histories
        # 1399 to 1405 covers about 6 years. Let's seed monthly data to satisfy MIN_DAILY_RETURNS (which is 30)
        # Wait, if we need at least 30 returns in the window, we need 31 dates before Farvardin 1400.
        # Farvardin 1400 is approx 2021-03-21. Trailing window of 180 days is about 6 months before.
        # Let's seed daily prices for all assets for a range of dates: 2020-09-01 to 2026-01-01
        start_date = datetime.date(2020, 9, 1)
        end_date = datetime.date(2026, 1, 1)
        delta = datetime.timedelta(days=2) # Seed every 2 days to make it faster
        
        candles = []
        brs_hist = []
        curr_date = start_date
        i = 0
        while curr_date <= end_date:
            jday = jdatetime.date.fromgregorian(date=curr_date)
            dt_str = f"{jday.year:04d}-{jday.month:02d}-{jday.day:02d}"
            
            # KAMA and FARS (tse)
            candles.append(MarketCandle(
                symbol="KAMA", timeframe="1d_adj", date_time=dt_str,
                close_price=100.0 + i * 0.1, open_price=100.0, volume=1000
            ))
            candles.append(MarketCandle(
                symbol="FARS", timeframe="1d_adj", date_time=dt_str,
                close_price=200.0 - i * 0.1, open_price=200.0, volume=1000
            ))
            # USD (brs)
            brs_hist.append(GoldCurrencyHistory(
                symbol="USD", date=dt_str, close_price=30000.0 + i * 10
            ))
            # Emami (brs)
            brs_hist.append(GoldCurrencyHistory(
                symbol="Emami", date=dt_str, close_price=10000000.0 + i * 1000
            ))
            curr_date += delta
            i += 1

        MarketCandle.objects.bulk_create(candles)
        GoldCurrencyHistory.objects.bulk_create(brs_hist)

        # Clear cache
        cache.clear()

        # Create BacktestRun
        run = BacktestRun.objects.create(
            params_hash="test_hash",
            basis="nominal",
            universe=["kama_stock", "fars_stock", "usd_cash", "emami_coin"],
            universe_hash="test_univ_hash",
            status=BacktestRun.Status.QUEUED
        )

        run_backtest(run.id)

        # Refresh
        run.refresh_from_db()
        print("RUN ERROR:", run.error)
        years = BacktestYear.objects.filter(run=run)
        for y in years:
            print("YEAR:", y.cutoff_date, y.scenario, y.realized_metrics)
        assert run.status == BacktestRun.Status.READY
        assert run.progress == 100

        # Five completed years across optimizer scenarios and three baselines.
        from portfolio.services.optimization import SCENARIOS
        assert years.count() == 5 * (len(SCENARIOS) + 3)

        # The fixture intentionally has fewer than 252 pre-cutoff observations
        # for 1400, so inspect the first year that satisfies the approved data
        # threshold instead of assuming an under-covered cutoff must succeed.
        y1 = next(
            year
            for year in years.filter(scenario="equal_weight").order_by("cutoff_date")
            if "realized_return" in year.realized_metrics
        )
        assert "realized_return" in y1.realized_metrics
        assert "realized_volatility" in y1.realized_metrics
        assert "sharpe" in y1.realized_metrics
        assert "cost_drag" in y1.realized_metrics
        assert "max_drawdown" in y1.realized_metrics

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
        # Seed 40 days to exceed MIN_DAILY_RETURNS = 30
        today_j = jdatetime.date.today()
        dates = [(today_j - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(40, 0, -1)]

        for dt_str in dates:
            MarketCandle.objects.create(
                symbol="SHAFN", timeframe="1d_adj", date_time=dt_str,
                close_price=10000.0, open_price=10000.0, volume=10000
            )

        candidates, excluded = get_candidate_universe()
        assert "SHAFN" in candidates
        assert "ILIZ" not in candidates  # Eligible is False

    def test_watchlist_force_include_exclude(self):
        # Testing Protocol: We choose an integration test for watchlist modes and force include/exclude logic because it validates how user-curated overrides interact with candidate universe lists.
        from portfolio.models import Account, Watchlist, WatchlistItem
        from portfolio.services.returns import get_universe_by_mode
        from django.contrib.auth import get_user_model

        User = get_user_model()
        user = User.objects.create_user(email="wt@example.com", password="password")
        account = Account.objects.create(user=user, name="Watchlist Account")
        
        watchlist = Watchlist.objects.create(account=account)
        WatchlistItem.objects.create(watchlist=watchlist, symbol="SHAFN", force_include=True)
        WatchlistItem.objects.create(watchlist=watchlist, symbol="KAMA", force_exclude=True)

        # Resolve universe under watchlist mode
        # Force include should ensure SHAFN is present
        # Force exclude should ensure KAMA is absent
        universe = get_universe_by_mode("watchlist", user=user, account=account)
        assert "SHAFN" in universe
        assert "KAMA" not in universe

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
