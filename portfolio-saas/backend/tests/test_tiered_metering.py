"""Integration tests for metered API tiering: deep vs shallow tick growth and warehouse coverage honesty."""

from decimal import Decimal
from django.contrib.auth import get_user_model
from django.urls import reverse
import pytest
from rest_framework.test import APIClient

from marketdata.archive import grow_tick_windows, get_deep_tier_symbols
from marketdata.models import (
    ArchiveFetchState, MarketCandle, StockSymbolMetadata, SymbolIntegrity,
)
from portfolio.models import Account, Asset, Holding

pytestmark = pytest.mark.django_db


# Integration test: exercises the boundary between database models (holdings, symbol metadata, archive state)
# and the archive window progression logic to confirm tiered resource allocation without external network access.
def test_grow_tick_windows_gated_to_deep_tier(settings):
    User = get_user_model()
    user = User.objects.create_user(email="metering@example.com", password="password123")
    account = Account.objects.create(user=user, name="Metering Account")

    held_asset = Asset.objects.create(
        key="held_stock", name="Held Stock", asset_class=Asset.AssetClass.STOCK, tse_symbol="HELD_SYM", is_active=True
    )
    Holding.objects.create(account=account, asset=held_asset, quantity=Decimal("100"))

    # Liquidity is measured as median daily TURNOVER from candles, not from
    # StockSymbolMetadata.market_cap. That column is filled by the weekly
    # metadata sync and in production held a value for only 86 of 1,969 symbols,
    # so a "top N by market cap" tier was really "the ones the sync had reached".
    # Metadata rows are kept here precisely to prove they no longer decide it.
    StockSymbolMetadata.objects.create(
        ins_code=1001, l18="LIQUID_SYM", l30="Liquid Symbol", market_cap=0, free_float=Decimal("25.0")
    )
    StockSymbolMetadata.objects.create(
        ins_code=1002, l18="SHALLOW_SYM", l30="Shallow Symbol", market_cap=50_000_000_000, free_float=Decimal("1.0")
    )

    # LIQUID_SYM trades AND produces the reversal setup; SHALLOW_SYM barely
    # trades and never moves. Both conditions matter: ranking on turnover alone
    # admitted 118 of production's top 300 with zero positives ever -- fixed
    # income ETFs, heavily traded and by construction incapable of a 2% day.
    settings.MARKETDATA_REVERSAL_MIN_SESSIONS = 5
    settings.MARKETDATA_REVERSAL_MIN_POSITIVES = 3
    settings.MARKETDATA_DEEP_TIER_N = 1
    candles = []
    close = 1000.0
    for i in range(1, 11):
        day = f"1403-01-{i:02d}"
        previous, close = close, close * 1.03      # closes +3% on the prior close
        candles.append(MarketCandle(
            symbol="LIQUID_SYM", timeframe="1d_unadj", date_time=day,
            open_price=previous, high_price=close,
            low_price=previous * 0.95,             # ...after dipping 5% first
            close_price=close, volume=1_000_000,
        ))
        candles.append(MarketCandle(
            symbol="SHALLOW_SYM", timeframe="1d_unadj", date_time=day,
            open_price=1000, high_price=1000, low_price=1000,
            close_price=1000, volume=1,
        ))
    MarketCandle.objects.bulk_create(candles)

    tick_ep = ArchiveFetchState.Endpoint.STOCK_TRANSACTION_TICKS
    state_held = ArchiveFetchState.objects.create(
        endpoint=tick_ep, symbol="HELD_SYM", target_window_days=90, verified_complete=True
    )
    state_liquid = ArchiveFetchState.objects.create(
        endpoint=tick_ep, symbol="LIQUID_SYM", target_window_days=90, verified_complete=True
    )
    state_shallow = ArchiveFetchState.objects.create(
        endpoint=tick_ep, symbol="SHALLOW_SYM", target_window_days=90, verified_complete=True
    )

    deep_symbols = get_deep_tier_symbols()
    assert "HELD_SYM" in deep_symbols
    assert "LIQUID_SYM" in deep_symbols
    assert "SHALLOW_SYM" not in deep_symbols

    grown = grow_tick_windows(step_days=90)
    assert grown == 2

    state_held.refresh_from_db()
    state_liquid.refresh_from_db()
    state_shallow.refresh_from_db()

    assert state_held.target_window_days == 180
    assert state_held.verified_complete is False
    assert state_liquid.target_window_days == 180
    assert state_liquid.verified_complete is False
    assert state_shallow.target_window_days == 90
    assert state_shallow.verified_complete is True


def test_price_history_view_returns_caveats():
    User = get_user_model()
    user = User.objects.create_user(email="history_user@example.com", password="password123")
    asset = Asset.objects.create(
        key="gapped_asset", name="Gapped Asset", asset_class=Asset.AssetClass.STOCK, tse_symbol="GAPPED_SYM", is_active=True
    )
    SymbolIntegrity.objects.create(
        symbol="GAPPED_SYM",
        coverage_ratio=0.55,
        max_gap_days=20,
        passes_gate=False,
        reason="price_gap_exceeded",
    )

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("prices-history")
    response = client.get(url, {"asset": "gapped_asset"})
    assert response.status_code == 200
    assert "caveats" in response.data
    assert "price_gap_exceeded" in response.data["caveats"]
    assert "low_coverage" in response.data["caveats"]


def test_account_data_quality_hides_warehouse_coverage_from_members():
    User = get_user_model()
    user = User.objects.create_user(email="quality_user@example.com", password="password123")
    account = Account.objects.create(user=user, name="Quality Account")

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("account-data-quality", kwargs={"account_id": account.id})
    response = client.get(url)
    assert response.status_code == 200
    assert "warehouse_coverage" not in response.data
    assert "quality_status" in response.data


def test_account_data_quality_shows_warehouse_coverage_to_staff():
    User = get_user_model()
    user = User.objects.create_user(
        email="quality_staff@example.com",
        password="password123",
        is_staff=True,
    )
    account = Account.objects.create(user=user, name="Quality Account")

    client = APIClient()
    client.force_authenticate(user=user)

    url = reverse("account-data-quality", kwargs={"account_id": account.id})
    response = client.get(url)
    assert response.status_code == 200
    assert "warehouse_coverage" in response.data
    assert "counts" in response.data["warehouse_coverage"]
