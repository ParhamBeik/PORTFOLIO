import pytest
from decimal import Decimal
from django.utils import timezone
import jdatetime
import datetime
import pandas as pd
from django.test import override_settings

from marketdata.models import MarketInstrument, SymbolIntegrity, MarketIndexData, MarketCandle
from marketdata.integrity import compute_symbol_integrity, update_all_symbols_integrity
from marketdata.tasks import nightly_data_integrity
from portfolio.models import Asset
from portfolio.services.returns import daily_returns_matrix
from portfolio.services.diagnostics import portfolio_diagnostics

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
        assert calendar_calls == 1

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
