"""Live valuation: holdings x prices, the bases it can be expressed in, house marks, price fallbacks, and the daily averages the charts read.

Merged from 8 files; each section keeps its original banner.
"""

from datetime import timedelta
import datetime as dt
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
import pytest
from rest_framework.test import APIClient

from config.settings import CpiUnavailable, cpi_for
from marketdata.models import GoldCurrencyHistory, MarketDailyBar
from portfolio.models import Account
from portfolio.models import Account, Asset, Holding, LedgerEntry
from portfolio.models import Account, Asset, Holding, LedgerEntry, Snapshot
from portfolio.models import Account, Holding
from portfolio.models import DailyPriceAverage, Price
from portfolio.models import Price
from portfolio.services import asset_value, value_account, value_user
from portfolio.services.insights import (
    _liquid_items,
    _total,
    allocation_breakdown,
    concentration_risk,
)
from portfolio.services.ledger import record_house_mark
from portfolio.services.timeline import (
    house_area_as_of,
    house_marks_as_of,
    holdings_as_of,
)
from portfolio.services.valuation import _house_value
from portfolio.services.valuation import get_latest_prices, invalidate_prices_cache
from portfolio.tasks import _persistable_prices
from portfolio.tasks import aggregate_daily_price_averages

pytestmark = pytest.mark.django_db


# ----------------------------------------------------------------------
# test_valuation.py
# Valuation engine: holdings x latest prices -> portfolio value.
# 
# Covers the two non-trivial pieces of portfolio.services: the real-estate house
# formula and the live aggregation across accounts.


def test_house_formula_is_gross_of_mortgage():
    """Since 0017 the mortgage is a Liability, netted off the account total.

    Subtracting it here too would double-count it: 50M Toman/sqm * 90.2 sqm.
    """
    assert _house_value(Decimal("50")) == Decimal("4510000000")


def test_house_formula_zero_price_is_zero():
    assert _house_value(Decimal("0")) == Decimal("0")


def test_asset_value_uses_house_formula_for_real_estate(asset_catalog):
    house = asset_catalog["house_asset"]
    holding = Holding(asset=house, quantity=Decimal("50"), mortgage_deduction_tomans=Decimal("400000000"))
    # The legacy holding column is deliberately ignored — Liability owns the debt.
    assert asset_value(holding, Decimal("0")) == _house_value(Decimal("50"))


def test_asset_value_multiplies_quantity_for_normal_asset(asset_catalog):
    emami = asset_catalog["emami_coin"]
    holding = Holding(asset=emami, quantity=Decimal("2"))
    assert asset_value(holding, Decimal("480000000")) == Decimal("960000000")


def test_value_account_multiplies_quantity_by_price(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("63200")})
    user = make_user()
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2"))

    result = value_account(account)
    assert result["total"] == Decimal("960000000")
    item = next(i for i in result["items"] if i["key"] == "emami_coin")
    assert item["unit_price"] == Decimal("480000000")
    assert item["value"] == Decimal("960000000")
    assert item["source"] == "TEST"
    assert item["priced_at"] is not None
    assert item["age_seconds"] >= 0
    assert item["quality_status"] == "live"
    assert result["priced_assets"] == result["total_assets"] == 1


def test_value_account_marks_missing_quote_unavailable(asset_catalog, make_user):
    user = make_user(email="missing-price@test.test")
    account = Account.objects.create(user=user, name="Missing")
    Holding.objects.create(
        account=account,
        asset=asset_catalog["bitcoin_usd"],
        quantity=Decimal("2"),
    )

    result = value_account(account, prices={})

    assert result["total"] == 0
    assert result["priced_assets"] == 0
    assert result["total_assets"] == 1
    assert result["quality_status"] == "unavailable"
    assert result["items"][0]["value"] is None
    assert result["items"][0]["quality_status"] == "unavailable"
    assert result["excluded"] == [
        {"asset_key": "bitcoin_usd", "reason": "missing_price"}
    ]


def test_value_account_applies_house_formula(asset_catalog, write_prices, make_user):
    write_prices({})
    user = make_user(email="house@test.test")
    account = Account.objects.create(user=user, name="Property")
    Holding.objects.create(
        account=account,
        asset=asset_catalog["house_asset"],
        quantity=Decimal("50"),
        mortgage_deduction_tomans=Decimal("400000000")
    )

    result = value_account(account)
    assert result["total"] == _house_value(Decimal("50"))


def test_value_user_aggregates_across_accounts(asset_catalog, write_prices, make_user):
    write_prices({"emami_coin": Decimal("480000000"), "kama_stock": Decimal("5230")})
    user = make_user(email="agg@test.test")
    brokerage = Account.objects.create(user=user, name="Brokerage")
    cash = Account.objects.create(user=user, name="Cash")
    Holding.objects.create(account=brokerage, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))
    Holding.objects.create(account=cash, asset=asset_catalog["kama_stock"], quantity=Decimal("100"))

    valuation = value_user(user)
    assert valuation["total"] == Decimal("480000000") + Decimal("5230") * Decimal("100")
    assert {a["name"] for a in valuation["accounts"]} == {"Brokerage", "Cash"}


def test_compute_dynamic_net_worth_series(asset_catalog, write_prices, make_user):
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic@test.test")
    account = Account.objects.create(user=user, name="Dynamic")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))

    series = compute_dynamic_net_worth_series(user, account, days=7)
    assert len(series) == 7
    assert "total" in series[0]
    assert "total_usd" in series[0]
    assert float(series[0]["total"]) > 0
    assert series[0]["is_estimated"] is True
    # Opening-only: constant qty → flat when only latest prices exist.
    assert series[0]["total"] == series[-1]["total"]


def test_compute_dynamic_caps_at_90_days(asset_catalog, write_prices, make_user):
    from portfolio.services.valuation import SYNTHETIC_HISTORY_MAX_DAYS, compute_dynamic_net_worth_series
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic-cap@test.test")
    account = Account.objects.create(user=user, name="Dynamic")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1"))
    series = compute_dynamic_net_worth_series(user, account, days=365)
    assert len(series) == SYNTHETIC_HISTORY_MAX_DAYS


def test_compute_dynamic_respects_buy_sell_timeline(asset_catalog, write_prices, make_user):
    """With a recent BUY, pre-buy days should not include that position."""
    from django.utils import timezone
    from portfolio.models import LedgerEntry
    from portfolio.services.valuation import compute_dynamic_net_worth_series

    write_prices({"emami_coin": Decimal("100"), "usd_cash": Decimal("60000")})
    user = make_user(email="dynamic-buy@test.test")
    account = Account.objects.create(user=user, name="Traded")
    Holding.objects.create(account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("5"))
    # BUY "now" — walking holdings_as_of zeros qty before this timestamp.
    LedgerEntry.objects.create(
        account=account,
        asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("5"),
        price_tomans=Decimal("100"),
        amount_tomans=Decimal("500"),
        timestamp=timezone.now(),
        source="manual",
    )

    series = compute_dynamic_net_worth_series(user, account, days=5)
    assert len(series) == 5
    assert float(series[0]["total"]) == 0.0
    assert float(series[-1]["total"]) > 0


def test_archive_replaces_a_live_price_the_live_loop_left_behind(asset_catalog, write_prices):
    """A live price stuck on yesterday's session must be replaced by a newer
    archive close even when it's not a magnitude spike -- regression for a
    41-hour live-loop outage that left kama_stock frozen at Tuesday's close
    (4490) while the separate archive backfill had already converged on
    Wednesday's real close (4620), which passed the spike-sanity band (not
    an outlier) and so was silently never surfacing on the dashboard.
    """
    from datetime import timedelta

    import jdatetime
    from django.utils import timezone

    from marketdata.models import MarketCandle
    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    write_prices({"kama_stock": Decimal("4490")})
    stale_at = timezone.now() - timedelta(days=1, hours=14)
    Price.objects.filter(asset__key="kama_stock").update(fetched_at=stale_at)
    stale_session = jdatetime.date.fromgregorian(date=stale_at.date())
    stale_date_str = f"{stale_session.year:04d}-{stale_session.month:02d}-{stale_session.day:02d}"

    newer_session = jdatetime.date.fromgregorian(date=timezone.now().date())
    newer_date_str = f"{newer_session.year:04d}-{newer_session.month:02d}-{newer_session.day:02d}"
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED, date_time=stale_date_str,
        open_price=4490, high_price=4490, low_price=4490, close_price=4490, volume=1000,
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED, date_time=newer_date_str,
        open_price=4620, high_price=4620, low_price=4620, close_price=4620, volume=1000,
    )

    from portfolio.services.valuation import get_latest_prices

    prices = get_latest_prices()
    assert prices["kama_stock"] == Decimal("4620")


def test_guard_price_map_accepts_legitimate_large_moves(asset_catalog, write_prices):
    from portfolio.services.valuation import guard_price_map
    write_prices({"emami_coin": Decimal("500000000")})
    guarded = guard_price_map({"emami_coin": Decimal("560000000")})
    assert guarded["emami_coin"] == Decimal("560000000")


def test_guard_price_map_forward_fills_missing_or_zero_prices(asset_catalog, write_prices):
    from portfolio.services.valuation import guard_price_map
    write_prices({"emami_coin": Decimal("500000000")})
    # Live price fails / returns 0
    guarded = guard_price_map({"emami_coin": Decimal("0")})
    assert guarded["emami_coin"] == Decimal("500000000")  # Forward-fills previous price (flat-line)


def test_guard_price_map_does_not_forward_fill_past_max_sessions(asset_catalog, write_prices):
    """A price older than MAX_FORWARD_FILL_SESSIONS days must not be carried
    forward forever -- it is never re-persisted (see _persistable_prices), so
    its `fetched_at` never advances on its own.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price
    from portfolio.services.valuation import MAX_FORWARD_FILL_SESSIONS, guard_price_map

    write_prices({"emami_coin": Decimal("500000000")})
    stale_at = timezone.now() - timedelta(days=MAX_FORWARD_FILL_SESSIONS + 1)
    Price.objects.filter(asset__key="emami_coin").update(fetched_at=stale_at)

    guarded = guard_price_map({"emami_coin": Decimal("0")})
    assert guarded["emami_coin"] == Decimal("0")


def test_closed_tse_valuation_uses_latest_archive_close(asset_catalog, write_prices, monkeypatch):
    from django.core.cache import cache
    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import get_latest_prices

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("100")})
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now()),
        close_price=Decimal("200"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    cache.delete("prices:latest")

    assert get_latest_prices()["kama_stock"] == Decimal("200")


def test_live_only_asset_uses_market_daily_bar_when_closed(asset_catalog, write_prices, monkeypatch):
    """An ETF has no candle history at all -- its only close is the daily bar
    distilled from the live NAV poll, and the TSE desk being shut is what makes
    that close authoritative over an older live row."""
    from marketdata.models import MarketInstrument

    etf = asset_catalog["kama_stock"]
    etf.tse_symbol = "اهرم"
    etf.save(update_fields=["tse_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        symbol="اهرم",
        category=MarketInstrument.Category.ETF,
    )
    from portfolio.services.returns import to_jalali_str

    write_prices({"kama_stock": Decimal("900")})
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.ETF_NAV,
        symbol="اهرم",
        date=to_jalali_str(timezone.now()),
        close_price=Decimal("800"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    cache.delete("prices:latest")

    assert get_latest_prices()["kama_stock"] == Decimal("800")


def test_crypto_keeps_its_live_price_overnight(asset_catalog, write_prices, monkeypatch):
    """Crypto desks never shut, so OVERNIGHT is not "closed" for them -- taking
    the archive close there would replace a current price with last night's."""
    asset = asset_catalog["bitcoin_usd"]
    asset.brs_symbol = "BTC"
    asset.save(update_fields=["brs_symbol"])
    write_prices({"bitcoin_usd": Decimal("900")})
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.CRYPTO,
        symbol="BTC",
        date="1405-05-30",
        close_price=Decimal("800"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")
    cache.delete("prices:latest")

    assert get_latest_prices()["bitcoin_usd"] == Decimal("900")


def test_live_bar_fallback_requires_matching_asset_class(asset_catalog, write_prices, monkeypatch):
    """Same symbol, wrong feed: an index bar named "BTC" must never price a coin."""
    asset = asset_catalog["bitcoin_usd"]
    asset.brs_symbol = "BTC"
    asset.save(update_fields=["brs_symbol"])
    write_prices({"bitcoin_usd": Decimal("0")})
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.INDEX,
        symbol="BTC",
        date="1405-05-30",
        close_price=Decimal("800"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")
    cache.delete("prices:latest")

    assert get_latest_prices().get("bitcoin_usd", Decimal("0")) == Decimal("0")


def test_closed_tse_keeps_todays_live_price_until_the_archive_catches_up(
    asset_catalog, write_prices, monkeypatch
):
    """Observed in production 2026-08-23: KAMA showed 4890 correctly while the
    session was open, then dropped to yesterday's close the moment it shut.

    The archive ingests a session's close hours after the bell, so between the
    two the newest live row IS today's close. Preferring an older archive row
    there throws away the real closing price every single day.
    """
    from datetime import timedelta

    from django.core.cache import cache
    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import get_latest_prices

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("4890")})
    yesterday = to_jalali_str(timezone.now() - timedelta(days=1))
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=yesterday,
        close_price=Decimal("5200"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    cache.delete("prices:latest")

    assert get_latest_prices()["kama_stock"] == Decimal("4890")


def test_closed_tse_prefers_the_archive_once_it_has_todays_close(
    asset_catalog, write_prices, monkeypatch
):
    """The other half of the same rule: once the warehouse holds today's close
    it is the settled figure and outranks the last intraday tick."""
    from django.core.cache import cache
    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import get_latest_prices

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("4890")})
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now()),
        close_price=Decimal("4910"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    cache.delete("prices:latest")

    assert get_latest_prices()["kama_stock"] == Decimal("4910")


def test_quality_status_not_stale_when_tse_closed(asset_catalog, write_prices, make_user, monkeypatch):
    """A TSE stock's last price must not be flagged 'stale' just because the
    session closed hours ago -- the tsetmc live job only runs while state ==
    OPEN, so an old price during CLOSED_DAYTIME/OVERNIGHT is still correct.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    write_prices({"kama_stock": Decimal("5230")})
    old_at = timezone.now() - timedelta(hours=8)
    Price.objects.filter(asset__key="kama_stock").update(fetched_at=old_at)

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    user = make_user(email="tse-closed@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "live"


def test_quality_status_stale_when_tse_open_and_price_did_not_refresh(
    asset_catalog, write_prices, make_user, monkeypatch
):
    """The same old price IS a real problem while the market is open -- the
    live job should have refreshed it, so this must still show 'stale'.
    """
    from datetime import timedelta

    from django.utils import timezone

    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    write_prices({"kama_stock": Decimal("5230")})
    old_at = timezone.now() - timedelta(minutes=20)
    Price.objects.filter(asset__key="kama_stock").update(fetched_at=old_at)

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "open")
    user = make_user(email="tse-open@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "stale"


# ----------------------------------------------------------------------
# test_services.py
# Cache + DISTINCT ON behaviour of get_latest_prices.
# 
# These are the scale levers: one query for the newest price per asset, cached so
# reads stay cheap. The DISTINCT ON clause is Postgres-only, which is why these
# tests require a real postgres (not sqlite).


def test_latest_price_is_newest_per_asset(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("400000000")})
    write_prices({"emami_coin": Decimal("480000000")})  # newer
    cache.delete("prices:latest")

    prices = get_latest_prices()
    assert prices["emami_coin"] == Decimal("480000000")


def test_latest_prices_is_cached(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("480000000")})
    first = get_latest_prices()

    # Add a newer row WITHOUT busting the cache; the cached read must not see it.
    Price.objects.create(asset=asset_catalog["emami_coin"], price=Decimal("1"), source="TEST")
    second = get_latest_prices()
    assert second == first
    assert second["emami_coin"] == Decimal("480000000")


def test_latest_prices_cache_is_invalidated_by_market_state_change(
    asset_catalog, write_prices, monkeypatch
):
    write_prices({"emami_coin": Decimal("480000000")})
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "open")
    get_latest_prices()

    Price.objects.create(
        asset=asset_catalog["emami_coin"],
        price=Decimal("500000000"),
        source="TEST",
    )
    monkeypatch.setattr(
        "marketdata.market_state.market_state",
        lambda: "closed_daytime",
    )

    assert get_latest_prices()["emami_coin"] == Decimal("500000000")


def test_invalidate_forces_refresh(asset_catalog, write_prices):
    write_prices({"emami_coin": Decimal("480000000")})
    get_latest_prices()  # populate cache
    write_prices({"emami_coin": Decimal("500000000")})

    invalidate_prices_cache()
    refreshed = get_latest_prices()
    assert refreshed["emami_coin"] == Decimal("500000000")


def test_inactive_assets_are_excluded(asset_catalog, write_prices):
    from portfolio.models import Asset

    write_prices({"emami_coin": Decimal("480000000")})
    Asset.objects.filter(key="emami_coin").update(is_active=False)
    cache.delete("prices:latest")
    prices = get_latest_prices()
    assert "emami_coin" not in prices


def test_latest_price_uses_archive_when_latest_fetch_sharply_drops(asset_catalog):
    gold = asset_catalog["emami_coin"]
    gold.brs_symbol = "IR_COIN_EMAMI"
    gold.save(update_fields=["brs_symbol"])
    Price.objects.create(asset=gold, price=Decimal("480000000"), source="SEED")
    Price.objects.create(asset=gold, price=Decimal("1"), source="BAD_FETCH")
    GoldCurrencyHistory.objects.create(
        symbol="IR_COIN_EMAMI",
        date="1404-01-02",
        close_price=Decimal("479000000"),
    )
    cache.delete("prices:latest")

    prices = get_latest_prices()
    assert prices["emami_coin"] == Decimal("479000000")


# ----------------------------------------------------------------------
# test_price_fallback.py


def test_archive_replacement_is_persisted_but_forward_fill_is_not():
    priced, sources = _persistable_prices(
        {"archive": 0, "forward_fill": 0, "live": 4300},
        {
            "archive": Decimal("4360"),
            "forward_fill": Decimal("4240"),
            "live": Decimal("4300"),
        },
        {"archive": Decimal("4360")},
    )

    assert priced == {"archive": Decimal("4360"), "live": Decimal("4300")}
    assert sources == {"archive": "ARCHIVE", "live": "API"}


# ----------------------------------------------------------------------
# test_unpriced_asset_does_not_crash.py
# An unpriced holding must not take down the analytics endpoints.
# 
# Unit tests: `_liquid_items` / `_total` are pure functions over the valuation
# payload dict, so the defect (a None value reaching sum()/max()) reproduces
# without a database. That keeps this fast and pins exactly one behaviour.


def _valuation_with_one_unpriced():
    """Shape produced by value_user(): one priced asset, one with no price."""
    return {
        "accounts": [
            {
                "items": [
                    {
                        "key": "emami_coin",
                        "asset": "Emami Coin",
                        "class": "Gold",
                        "value": Decimal("1000"),
                        "quality_status": "live",
                    },
                    {
                        # valuation.py sets value=None when the price is missing.
                        "key": "kama_stock",
                        "asset": "KAMA Stock",
                        "class": "Stock",
                        "value": None,
                        "quality_status": "unavailable",
                    },
                    {
                        "key": "house_asset",
                        "asset": "Real Estate",
                        "class": "Real Estate",
                        "value": Decimal("5000"),
                        "quality_status": "manual",
                    },
                ]
            }
        ],
        "excluded": [{"asset_key": "kama_stock", "reason": "missing_price"}],
    }


def test_unpriced_holding_is_dropped_from_liquid_items():
    items = _liquid_items(_valuation_with_one_unpriced())

    keys = {i["key"] for i in items}
    assert keys == {"emami_coin"}  # real estate and the unpriced stock both gone
    assert all(i["value"] is not None for i in items)


def test_total_does_not_raise_on_an_unpriced_holding():
    # Before the fix this raised TypeError: unsupported operand Decimal + None.
    assert _total(_liquid_items(_valuation_with_one_unpriced())) == Decimal("1000")


def test_allocation_and_concentration_survive_an_unpriced_holding():
    valuation = _valuation_with_one_unpriced()

    # concentration_risk did `max(items, key=lambda i: i["value"])`, which raised.
    breakdown = allocation_breakdown(valuation)
    risk = concentration_risk(valuation)

    # The one priced liquid asset is 100% of the liquid portfolio; the unpriced
    # stock contributes nothing rather than blowing up the calculation.
    assert breakdown == {"Gold": 100.0}
    assert risk["share"] == 100.0  # concentration_risk reports a percentage


def test_every_holding_unpriced_yields_empty_not_an_exception():
    valuation = {"accounts": [{"items": [
        {"key": "a", "asset": "A", "class": "Gold", "value": None,
         "quality_status": "unavailable"},
    ]}]}

    assert _liquid_items(valuation) == []
    assert _total(_liquid_items(valuation)) == Decimal("0")
    assert concentration_risk(valuation)["severity"] == "info"


# ----------------------------------------------------------------------
# test_house_valuation_marks.py
# A house must be worth what it was worth at the time, not what it is worth now.
# 
# Integration tests: the behaviour spans LedgerEntry rows, the holding projection
# and the as-of valuation, so it only reproduces against the database.
# 
# Before dated marks existed, revaluing a house REPLACED its single opening entry.
# The house therefore carried one price across all of history: every rial of
# appreciation was invisible to the net-worth chart, and today's price was baked
# into the opening balance, which understated TWR. Since real estate is a large
# share of this family's net worth, that distortion was material.


@pytest.fixture
def house_account(asset_catalog, make_user):
    user = make_user(email="house-marks@test.test")
    account = Account.objects.create(user=user, name="Home")
    return user, account, Asset.objects.get(key="house_asset")


def test_first_mark_is_the_opening_position(house_account):
    user, account, house = house_account

    entry = record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=400),
    )

    assert entry.kind == LedgerEntry.Kind.OPENING_POSITION


def test_later_marks_append_and_do_not_overwrite_history(house_account):
    user, account, house = house_account
    old = timezone.now() - dt.timedelta(days=400)
    recent = timezone.now() - dt.timedelta(days=10)

    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"), occurred_at=old,
    )
    second = record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("150"), area_sqm=Decimal("90.2"), occurred_at=recent,
    )

    assert second.kind == LedgerEntry.Kind.VALUATION_MARK
    # Both events survive; the revaluation did not rewrite the opening.
    assert LedgerEntry.objects.filter(account=account, asset=house).count() == 2
    # The old date still sees the old price -- this is the whole point.
    assert house_marks_as_of(account, old)["house_asset"] == Decimal("90")
    assert house_marks_as_of(account, recent)["house_asset"] == Decimal("150")
    # Before any mark exists the house simply is not there yet.
    assert house_marks_as_of(account, old - dt.timedelta(days=1)) == {}


def test_marks_replace_rather_than_accumulate(house_account):
    """90 then 150 is a revaluation to 150, never a holding of 240."""
    user, account, house = house_account
    record_house_mark(
        user=user, account_id=account.id, asset=house, quantity=Decimal("90"),
        area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=400),
    )
    record_house_mark(
        user=user, account_id=account.id, asset=house, quantity=Decimal("150"),
        area_sqm=Decimal("90.2"),
        occurred_at=timezone.now() - dt.timedelta(days=10),
    )

    holding = Holding.objects.get(account=account, asset=house)

    assert holding.quantity == Decimal("150")
    assert holdings_as_of(user, account, timezone.now())["house_asset"] == Decimal("150")


def test_area_travels_with_the_mark_in_force(house_account):
    user, account, house = house_account
    old = timezone.now() - dt.timedelta(days=400)
    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("90"), area_sqm=Decimal("90.2"), occurred_at=old,
    )
    record_house_mark(
        user=user, account_id=account.id, asset=house,
        quantity=Decimal("150"), area_sqm=Decimal("120.5"),
        occurred_at=timezone.now() - dt.timedelta(days=10),
    )

    # Pairing a historical price with today's area would mix two points in time.
    assert house_area_as_of(account, old)["house_asset"] == Decimal("90.2")
    assert house_area_as_of(account, timezone.now())["house_asset"] == Decimal("120.5")


def test_a_mark_cannot_be_dated_in_the_future(house_account):
    from portfolio.services.ledger import LedgerError

    user, account, house = house_account

    with pytest.raises(LedgerError):
        record_house_mark(
            user=user, account_id=account.id, asset=house,
            quantity=Decimal("90"), area_sqm=Decimal("90.2"),
            occurred_at=timezone.now() + dt.timedelta(days=1),
        )


# ----------------------------------------------------------------------
# test_chart_snapshot_groupby.py
# Net-worth history: one point per calendar day, preferring verified closes.


def _mark_traded(account, asset):
    """BUY/SELL presence opts the account out of holdings-only synthetic history."""
    LedgerEntry.objects.create(
        account=account,
        asset=asset,
        kind=LedgerEntry.Kind.BUY,
        quantity=Decimal("1"),
        price_tomans=Decimal("1"),
        amount_tomans=Decimal("1"),
        source="system",
        note="test marker",
    )


@pytest.mark.django_db
def test_same_day_snapshots_use_latest_live_point(make_user):
    user = make_user("chart_test_user@example.com")
    client = APIClient()
    client.force_authenticate(user=user)

    account = Account.objects.create(user=user, name="Test Account")
    asset = Asset.objects.create(key="test_gold", name="Gold Asset", asset_class=Asset.AssetClass.GOLD, is_active=True)
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("10"))
    _mark_traded(account, asset)

    now = timezone.now()
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("100000"), timestamp=now - timedelta(hours=3))
    Snapshot.objects.create(
        user=user,
        account=account,
        total_value_tomans=Decimal("105000"),
        timestamp=now - timedelta(hours=1),
        is_session_close=True,
    )
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("110000"), timestamp=now - timedelta(minutes=5))

    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 1
    assert series[0]["date"] == now.strftime("%Y-%m-%d")
    assert Decimal(series[0]["total"]) == Decimal("105000")
    assert series[0]["is_session_close"] is True


@pytest.mark.django_db
def test_day_avg_prefers_live_over_estimated_gap_fills(make_user):
    """Gap-fill estimates must not drag a day that also has live snaps."""
    user = make_user("chart_live_pref@example.com")
    client = APIClient()
    client.force_authenticate(user=user)

    account = Account.objects.create(user=user, name="Test Account")
    asset = Asset.objects.create(
        key="test_live_pref", name="Gold", asset_class=Asset.AssetClass.GOLD, is_active=True
    )
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("1"))
    _mark_traded(account, asset)

    now = timezone.now()
    for i in range(10):
        Snapshot.objects.create(
            user=user,
            account=account,
            total_value_tomans=Decimal("18000000000"),
            timestamp=now - timedelta(minutes=2 * i),
            is_estimated=True,
        )
    Snapshot.objects.create(
        user=user,
        account=account,
        total_value_tomans=Decimal("27000000000"),
        timestamp=now,
        is_estimated=False,
    )

    series = client.get(f"/api/snapshots/?days=7&account={account.id}").json()["series"]
    assert len(series) == 1
    assert Decimal(series[0]["total"]) == Decimal("27000000000")
    assert series[0]["is_estimated"] is False


@pytest.mark.django_db
def test_snapshots_across_multiple_days_yield_one_point_per_day(make_user):
    user = make_user("chart_multi_day@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    asset = Asset.objects.create(key="test_gold_multi", name="Gold", asset_class=Asset.AssetClass.GOLD, is_active=True)
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("1"))
    _mark_traded(account, asset)
    now = timezone.now()

    for offset_days, values in enumerate([[100000, 102000], [200000], [300000, 301000, 299000]]):
        day = now - timedelta(days=offset_days)
        for i, value in enumerate(values):
            Snapshot.objects.create(
                user=user, account=account, total_value_tomans=Decimal(str(value)),
                timestamp=day - timedelta(hours=i),
            )

    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 3
    # Oldest first.
    assert series[0]["date"] < series[1]["date"] < series[2]["date"]


@pytest.mark.django_db
def test_days_all_returns_history_beyond_one_year(make_user):
    user = make_user("chart_all_range@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    old_snapshot_time = timezone.now() - timedelta(days=800)
    Snapshot.objects.create(
        user=user, account=account, total_value_tomans=Decimal("50000"), timestamp=old_snapshot_time,
    )
    # Second recent point so 365/all keep real Snapshot history (≥2 days).
    Snapshot.objects.create(
        user=user, account=account, total_value_tomans=Decimal("51000"),
        timestamp=timezone.now() - timedelta(days=1),
    )

    client = APIClient()
    client.force_authenticate(user=user)

    capped = client.get(f"/api/snapshots/?days=365&account={account.id}").json()["series"]
    assert len(capped) == 1
    assert capped[0]["date"] == (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    full = client.get(f"/api/snapshots/?days=all&account={account.id}").json()["series"]
    assert len(full) == 2
    assert full[0]["date"] == old_snapshot_time.strftime("%Y-%m-%d")


@pytest.mark.django_db
def test_snapshot_series_does_not_fabricate_pre_history(make_user):
    user = make_user("chart_start@example.com")
    snapshot_time = timezone.now() - timedelta(days=1)
    Snapshot.objects.create(
        user=user,
        account=None,
        total_value_tomans=Decimal("100000"),
        timestamp=snapshot_time,
    )
    client = APIClient()
    client.force_authenticate(user=user)

    series = client.get("/api/snapshots/?days=30").json()["series"]

    assert series[0]["date"] == snapshot_time.strftime("%Y-%m-%d")
    assert len(series) == 1


@pytest.mark.django_db
def test_holdings_only_snapshots_use_warehouse_series(make_user, asset_catalog, write_prices):
    """Quantity-only accounts get multi-day synthetic history without BUY/SELL."""
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user("holdings_only_chart@example.com")
    account = Account.objects.create(user=user, name="Mother")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )

    client = APIClient()
    client.force_authenticate(user=user)
    series = client.get(f"/api/snapshots/?days=30&account={account.id}").json()["series"]

    assert len(series) == 30
    assert all(row["is_estimated"] is True for row in series)
    assert all(float(row["total"]) > 0 for row in series)


# --- Forward-fill is bounded in SESSIONS, never in calendar days ------------
# Regression cover for the 1405-04 incident: کاما last printed an adjusted close
# on 1405-04-09, the exchange then shut for five days (1405-04-11..15), and the
# chart dropped the position on 2026-07-06 and 07-07 -- a ~3% cliff in a
# portfolio that had not moved. Five *calendar* days had passed; one *session*
# had. See marketdata.calendars.sessions_between.


def _write_candles(symbol, dates, price="3000"):
    from marketdata.models import MarketCandle

    MarketCandle.objects.bulk_create(
        MarketCandle(
            symbol=symbol,
            timeframe=MarketCandle.ADJUSTED,
            date_time=date,
            close_price=Decimal(price),
        )
        for date in dates
    )


@pytest.mark.django_db
def test_market_closure_does_not_drop_a_held_stock(asset_catalog, write_prices, make_user):
    """A closure longer than five calendar days is not five stale sessions."""
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    from portfolio.services.returns import to_jalali_str

    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("3000"), "usd_cash": Decimal("60000")})

    now = timezone.now()
    # A close 9 days ago, then the exchange is shut every day since: no symbol
    # printed, so no session elapsed and the last close still stands.
    _write_candles("کاما", [to_jalali_str(now - timedelta(days=9))])

    user = make_user(email="closure@test.test")
    account = Account.objects.create(user=user, name="Closure")
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("100"))

    series = compute_dynamic_net_worth_series(user, account, days=10)
    totals = [float(row["total"]) for row in series]
    assert all(total > 0 for total in totals), "closure wrongly dropped the holding"
    assert len(set(totals)) == 1, "value should be flat across a closure, not cliffed"


@pytest.mark.django_db
def test_stock_silent_beyond_five_sessions_is_still_dropped(
    asset_catalog, write_prices, make_user
):
    """The bound must still fire when the market genuinely kept trading."""
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    from portfolio.services.returns import to_jalali_str

    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("3000"), "usd_cash": Decimal("60000")})

    now = timezone.now()
    _write_candles("کاما", [to_jalali_str(now - timedelta(days=9))])
    # Another symbol prints on each of the last 8 days: the market was open and
    # کاما simply stopped -- carrying its close further would invent a price.
    _write_candles(
        "فولاد", [to_jalali_str(now - timedelta(days=d)) for d in range(1, 9)]
    )

    user = make_user(email="silent@test.test")
    account = Account.objects.create(user=user, name="Silent")
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("100"))

    series = compute_dynamic_net_worth_series(user, account, days=10)
    assert float(series[-1]["total"]) == 0.0, "a genuinely stale price must be excluded"


@pytest.mark.django_db
def test_dynamic_series_query_count_is_flat_in_window_length(
    asset_catalog, write_prices, make_user
):
    """Staleness is asked per asset per day; it must not be a query per ask."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    from portfolio.services.returns import to_jalali_str

    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("3000"), "usd_cash": Decimal("60000")})
    now = timezone.now()
    _write_candles("کاما", [to_jalali_str(now - timedelta(days=40))])

    user = make_user(email="queries@test.test")
    account = Account.objects.create(user=user, name="Queries")
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("100"))

    cache.clear()
    with CaptureQueriesContext(connection) as short:
        compute_dynamic_net_worth_series(user, account, days=10)
    cache.clear()
    with CaptureQueriesContext(connection) as long:
        compute_dynamic_net_worth_series(user, account, days=90)

    assert len(long) == len(short), (
        f"query count grew with the window ({len(short)} -> {len(long)}): "
        "the session calendar is being refetched inside the day loop"
    )


@pytest.mark.django_db
def test_recorded_snapshots_are_never_discarded_for_an_estimate(
    make_user, asset_catalog, write_prices
):
    """Weekends must not make real history look 'too thin' and trigger the estimate."""
    write_prices({"emami_coin": Decimal("480000000"), "usd_cash": Decimal("60000")})
    user = make_user("recorded_wins@example.com")
    account = Account.objects.create(user=user, name="Mother")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    now = timezone.now()
    # 20 recorded days inside a 30-day window -- exactly the weekend-shaped
    # coverage that used to be treated as "no history at all".
    for offset in range(20):
        Snapshot.objects.create(
            user=user,
            account=account,
            timestamp=now - timedelta(days=offset),
            total_value_tomans=Decimal("960000000"),
        )

    client = APIClient()
    client.force_authenticate(user=user)
    series = client.get(f"/api/snapshots/?days=30&account={account.id}").json()["series"]

    assert len(series) == 20, "recorded snapshots should be returned, not recomputed"
    assert all(row["is_estimated"] is False for row in series)


@pytest.mark.django_db
def test_prune_snapshots_disabled_by_default_deletes_nothing(make_user, settings):
    from portfolio.tasks import prune_snapshots

    settings.SNAPSHOT_PRUNE_ENABLED = False
    settings.SNAPSHOT_RETENTION_DAYS = 30
    user = make_user("prune_test@example.com")
    account = Account.objects.create(user=user, name="Test Account")
    old_time = timezone.now() - timedelta(days=90)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("10000"), timestamp=old_time)

    before = Snapshot.objects.count()
    result = prune_snapshots()
    after = Snapshot.objects.count()

    assert result["enabled"] is False
    assert after == before


# ----------------------------------------------------------------------
# test_daily_price_average.py


def test_aggregate_excludes_archive_rows_from_the_average(asset_catalog):
    """ARCHIVE-tagged Price rows are guard_price_map's forward-fill-to-warehouse
    writes, not a live observation -- they must not enter the daily average.
    """
    asset = asset_catalog["emami_coin"]
    Price.objects.create(asset=asset, price=Decimal("100"), source="API")
    Price.objects.create(asset=asset, price=Decimal("200"), source="API")
    Price.objects.create(asset=asset, price=Decimal("999999"), source="ARCHIVE")

    written = aggregate_daily_price_averages()

    assert written == 1
    row = DailyPriceAverage.objects.get(asset=asset)
    assert row.avg_price == Decimal("150.0000")
    assert row.sample_count == 2


def test_aggregate_skips_assets_with_no_ticks(asset_catalog):
    written = aggregate_daily_price_averages()

    assert written == 0
    assert not DailyPriceAverage.objects.exists()


def test_aggregate_is_idempotent_per_day(asset_catalog):
    asset = asset_catalog["emami_coin"]
    Price.objects.create(asset=asset, price=Decimal("100"), source="API")

    aggregate_daily_price_averages()
    aggregate_daily_price_averages()

    assert DailyPriceAverage.objects.filter(asset=asset).count() == 1


# ----------------------------------------------------------------------
# test_cpi_unavailable_response.py
# Requesting real_toman without CPI coverage must be honest, not a 500.
# 
# Integration test: the behaviour under test is the DRF exception handler wired
# into the request/response cycle, so it only reproduces through the API layer —
# a unit test of `cpi_for()` alone proves the raise, not the response.


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_cpi_for_raises_instead_of_clamping_past_the_table():
    # The old behaviour silently returned the newest known value for ANY future
    # year, which made real_toman identical to nominal_toman for 17 months.
    with pytest.raises(CpiUnavailable) as exc:
        cpi_for(9999)
    assert exc.value.jalali_year == 9999
    assert exc.value.last_verified_year < 9999


def test_projected_cpi_years_are_never_counted_as_published():
    """Unit test: pure configuration arithmetic, no I/O.

    The estimate exists so real_toman works in the current year; it must not
    acquire the authority of an SCI release on the way. Anything that reports
    provenance has to keep the two apart.
    """
    from django.conf import settings

    estimated = settings.CPI_ESTIMATED_YEARS
    if not estimated:
        pytest.skip("estimation disabled via CPI_ESTIMATED_MONTHLY_RATE=0")

    assert settings.CPI_VERIFIED_THROUGH_YEAR < min(estimated), (
        "an estimated year was folded into the verified range"
    )
    assert "ESTIMATE" in settings.CPI_SOURCE, (
        "CPI_SOURCE is the provenance string shipped in the valuation payload; "
        "it must disclose that a projected index was used"
    )
    # Each projected anchor compounds the configured monthly rate over 12 months
    # from the last published one -- not from the previous projection's rounding.
    base = settings.CPI_BY_JALALI_YEAR[settings.CPI_VERIFIED_THROUGH_YEAR]
    for year in sorted(estimated):
        ahead = year - settings.CPI_VERIFIED_THROUGH_YEAR
        expected = base * (1 + settings.CPI_ESTIMATED_MONTHLY_RATE) ** (12 * ahead)
        assert settings.CPI_BY_JALALI_YEAR[year] == pytest.approx(expected)


def test_cpi_extrapolates_below_the_base_year_on_purpose():
    # Flat before the base year is defined behaviour, not a missing value.
    assert cpi_for(1000) == cpi_for(1398)


@override_settings(CPI_BY_JALALI_YEAR_EXTRA_APPLIED=True)
def test_real_toman_reports_unavailable_rather_than_returning_nominal(
    asset_catalog, make_user
):
    user = make_user(email="cpi-gap@test.test")
    Account.objects.create(user=user, name="Main")

    response = _client(user).get("/api/valuation/?basis=real_toman")

    # Either the CPI covers the period (200) or it honestly refuses (503).
    # What must never happen is a 500, or a 200 carrying nominal numbers
    # mislabelled as real.
    assert response.status_code in (200, 503), response.status_code
    if response.status_code == 503:
        assert response.data["reason"] == "cpi_unavailable"
        assert response.data["basis"] == "real_toman"
        assert "last_verified_jalali_year" in response.data


def test_archive_close_past_the_forward_fill_bound_is_not_offered(
    asset_catalog, write_prices, monkeypatch
):
    """A close from beyond the forward-fill bound is the last thing a delisted
    asset ever printed, not a current price. Offering nothing lets the valuation
    layer report the gap instead of dressing an ancient number up as today's."""
    from datetime import timedelta

    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import _archive_replacements

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    # A market-wide calendar with plenty of recent sessions, and this symbol's
    # own last close sitting well behind them.
    for offset in range(0, 10):
        MarketCandle.objects.create(
            symbol="دیگر",
            timeframe=MarketCandle.ADJUSTED,
            date_time=to_jalali_str(timezone.now() - timedelta(days=offset)),
            close_price=Decimal("100"),
        )
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now() - timedelta(days=30)),
        close_price=Decimal("5200"),
    )

    replacements = _archive_replacements({"kama_stock": Decimal("0")})

    assert "kama_stock" not in replacements


def test_stale_archive_close_still_vetoes_a_corrupt_live_quote(
    asset_catalog, write_prices, monkeypatch
):
    """Staleness stops a close STANDING IN for a price; it must not disable the
    corruption guard. The archive backfill is quota-driven and does fall behind,
    and a 10x live quote persisted unchecked is worse than an old real one."""
    from datetime import timedelta

    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import _archive_replacements

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    for offset in range(0, 10):
        MarketCandle.objects.create(
            symbol="دیگر",
            timeframe=MarketCandle.ADJUSTED,
            date_time=to_jalali_str(timezone.now() - timedelta(days=offset)),
            close_price=Decimal("100"),
        )
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now() - timedelta(days=30)),
        close_price=Decimal("5200"),
    )

    # A plausible quote is left alone even though the close is stale...
    assert "kama_stock" not in _archive_replacements({"kama_stock": Decimal("5100")})
    # ...but a corrupt one is still vetoed by it.
    assert _archive_replacements({"kama_stock": Decimal("52000")}) == {
        "kama_stock": Decimal("5200")
    }
