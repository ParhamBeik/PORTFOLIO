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
from portfolio.models import Asset, Holding, LedgerEntry
from portfolio.models import Snapshot
from portfolio.models import DailyPriceAverage, Price
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
    # The gold coin is quoted in Toman, the stock in Rial: 100 shares at 5,230
    # Rial is 523,000 Rial, which is 52,300 Toman. Mixing the two without that
    # division is the bug `holding_value_to_toman` exists to prevent.
    assert valuation["total"] == (
        Decimal("480000000") + Decimal("5230") * Decimal("100") / Decimal("10")
    )
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
    cache.delete("prices:latest:verified-toman-v2")

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
    cache.delete("prices:latest:verified-toman-v2")

    assert get_latest_prices()["kama_stock"] == Decimal("800")


def test_crypto_keeps_its_live_price_overnight(asset_catalog, write_prices, monkeypatch):
    """Crypto desks never shut, so OVERNIGHT is not "closed" for them -- taking
    the archive close there would replace a current price with last night's."""
    asset = asset_catalog["bitcoin_usd"]
    asset.brs_symbol = "BTC"
    asset.save(update_fields=["brs_symbol"])
    # `bitcoin_usd` is quoted in dollars, so the price map converts it before
    # anything compares it to a Toman archive bar. A rate of 1 leaves it on its
    # own scale and keeps this test about market state rather than units.
    write_prices({"bitcoin_usd": Decimal("900"), "usd_cash": Decimal("1")})
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.CRYPTO,
        symbol="BTC",
        date="1405-05-30",
        close_price=Decimal("800"),
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")
    cache.delete("prices:latest:verified-toman-v2")

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
    cache.delete("prices:latest:verified-toman-v2")

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
    cache.delete("prices:latest:verified-toman-v2")

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
    cache.delete("prices:latest:verified-toman-v2")

    assert get_latest_prices()["kama_stock"] == Decimal("4910")


def test_quality_status_not_stale_when_tse_closed(asset_catalog, write_prices, make_user, monkeypatch):
    """A TSE stock's last price must not be flagged 'stale' just because the
    session closed hours ago -- the tsetmc live job only runs while state ==
    OPEN, so an old price during CLOSED_DAYTIME/OVERNIGHT is still correct.
    """
    from datetime import timedelta


    from portfolio.models import Price

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])

    from datetime import datetime, timezone as dt_timezone
    frozen = datetime(2026, 8, 26, 16, 30, tzinfo=dt_timezone.utc)
    _freeze_valuation_now(monkeypatch, frozen)
    write_prices({"kama_stock": Decimal("5230")})
    Price.objects.filter(asset__key="kama_stock").update(
        fetched_at=frozen - timedelta(hours=8)
    )

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")
    user = make_user(email="tse-closed@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "live"


def test_quality_status_follows_each_market_not_one_symbol(
    asset_catalog, write_prices, make_user, monkeypatch
):
    """Last print stays live after THAT asset's market closes. Crypto never
    closes, so an old quote is stale overnight. Unit test: this is a pure
    per-asset decision over a fixed clock, which is where the pyramid puts it.
    """
    from datetime import timedelta


    from portfolio.models import Asset, Price

    stock = Asset.objects.create(
        key="khodro_stock", name="Khodro", asset_class=Asset.AssetClass.STOCK,
        tse_symbol="خودرو", is_active=True,
    )
    gold = asset_catalog["emami_coin"]
    gold.brs_symbol = "EMAMI"
    gold.save(update_fields=["brs_symbol"])
    crypto = asset_catalog["bitcoin_usd"]
    # Production crypto carries a BRS join key; without it the old helper
    # already returned stale overnight, so the assert would not pin the fix.
    crypto.brs_symbol = "BTC"
    crypto.save(update_fields=["brs_symbol"])

    from datetime import datetime, timezone as dt_timezone
    # 23:15 Tehran Wednesday -- overnight, gold desks shut, last print still current.
    frozen = datetime(2026, 8, 26, 19, 45, tzinfo=dt_timezone.utc)
    _freeze_valuation_now(monkeypatch, frozen)
    write_prices({
        "khodro_stock": Decimal("2800"),
        "emami_coin": Decimal("480000000"),
        "bitcoin_usd": Decimal("900"),
        # Dollar-quoted, so the map converts it. A rate of 1 keeps this test
        # about which market is open rather than about units.
        "usd_cash": Decimal("1"),
    })
    Price.objects.filter(asset__key="khodro_stock").update(
        fetched_at=frozen - timedelta(hours=8),
    )
    Price.objects.filter(asset__key="emami_coin").update(
        fetched_at=frozen - timedelta(hours=8),
    )
    Price.objects.filter(asset__key="bitcoin_usd").update(
        fetched_at=frozen - timedelta(minutes=20),
    )

    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "overnight")
    user = make_user(email="any-market@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=stock, quantity=Decimal("1"))
    Holding.objects.create(account=account, asset=gold, quantity=Decimal("1"))
    Holding.objects.create(account=account, asset=crypto, quantity=Decimal("1"))

    by_key = {item["key"]: item["quality_status"] for item in value_account(account)["items"]}
    assert by_key["khodro_stock"] == "live"
    assert by_key["emami_coin"] == "live"
    assert by_key["bitcoin_usd"] == "stale"


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


def _freeze_valuation_now(monkeypatch, when):
    monkeypatch.setattr("portfolio.services.valuation.timezone.now", lambda: when)


def test_quality_status_quota_when_live_lane_blocked_and_session_was_missed(
    asset_catalog, write_prices, make_user, monkeypatch
):
    """Unit test: this is a pure badge decision over a frozen clock, which is
    where the pyramid puts it. A day-old TSE quote after today's session, with
    the live lane refused, must not stay green Live.
    """
    from datetime import datetime, timezone as dt_timezone

    from marketdata.quota import TSETMC, trip_plan_breaker
    from portfolio.models import Price

    # Wednesday 26 Aug 2026, 20:00 Tehran (16:30 UTC) -- TSE shut, session done.
    frozen = datetime(2026, 8, 26, 16, 30, tzinfo=dt_timezone.utc)
    _freeze_valuation_now(monkeypatch, frozen)

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("5230")})
    Price.objects.filter(asset__key="kama_stock").update(
        fetched_at=frozen - timedelta(days=1)
    )
    trip_plan_breaker(TSETMC, reason="http_429")
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    user = make_user(email="tse-quota@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(account=account, asset=kama, quantity=Decimal("1"))

    result = value_account(account)
    item = next(i for i in result["items"] if i["key"] == "kama_stock")
    assert item["quality_status"] == "quota"
    assert result["quality_status"] == "partial"


def test_quality_status_stale_when_session_missed_without_quota_block(
    asset_catalog, write_prices, make_user, monkeypatch
):
    """Same missed session, provider not refused: still not Live, just Stale."""
    from datetime import datetime, timezone as dt_timezone

    from portfolio.models import Price

    frozen = datetime(2026, 8, 26, 16, 30, tzinfo=dt_timezone.utc)
    _freeze_valuation_now(monkeypatch, frozen)

    kama = asset_catalog["kama_stock"]
    kama.tse_symbol = "کاما"
    kama.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("5230")})
    Price.objects.filter(asset__key="kama_stock").update(
        fetched_at=frozen - timedelta(days=1)
    )
    monkeypatch.setattr("marketdata.market_state.market_state", lambda: "closed_daytime")

    user = make_user(email="tse-missed@test.test")
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
    cache.delete("prices:latest:verified-toman-v2")

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
    cache.delete("prices:latest:verified-toman-v2")
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
    cache.delete("prices:latest:verified-toman-v2")

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
    yesterday = now - timedelta(days=1)
    Snapshot.objects.create(user=user, account=account, total_value_tomans=Decimal("105000"), timestamp=yesterday, is_session_close=True)

    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 2
    assert series[0]["date"] == yesterday.strftime("%Y-%m-%d")
    assert Decimal(series[0]["total"]) == Decimal("105000")
    assert series[0]["is_session_close"] is True
    assert series[1]["date"] == now.strftime("%Y-%m-%d")
    assert Snapshot.objects.filter(user=user, account=account).count() == 1


@pytest.mark.django_db
def test_day_avg_prefers_live_over_estimated_gap_fills(make_user):
    """A completed close is retained; the live point is derived, never stored."""
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
    Snapshot.objects.create(
        user=user,
        account=account,
        total_value_tomans=Decimal("27000000000"),
        timestamp=now - timedelta(days=1),
        is_estimated=False,
    )

    series = client.get(f"/api/snapshots/?days=7&account={account.id}").json()["series"]
    assert len(series) == 2
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

    for offset_days, value in enumerate([100000, 200000, 300000], start=1):
        day = now - timedelta(days=offset_days)
        Snapshot.objects.create(
            user=user, account=account, total_value_tomans=Decimal(str(value)),
            timestamp=day,
        )

    client = APIClient()
    client.force_authenticate(user=user)
    res = client.get(f"/api/snapshots/?days=7&account={account.id}")
    assert res.status_code == 200
    series = res.json()["series"]

    assert len(series) == 4
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
    assert len(capped) == 2
    assert capped[0]["date"] == (timezone.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    full = client.get(f"/api/snapshots/?days=all&account={account.id}").json()["series"]
    assert len(full) == 3
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
    assert len(series) == 2


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
def test_close_from_before_the_window_still_primes_the_series(
    asset_catalog, write_prices, make_user
):
    """The priming read reaches past the window; the bulk read does not have to.

    `stock_closes` is filled by two queries -- one bounded to the window, one
    DISTINCT ON row per symbol for the newest close at or before it. This pins
    the second. The last close sits 40 days back with the market shut ever
    since, so no session has elapsed and that close still stands. If priming
    were dropped in favour of clipping the bulk query to the window, the asset
    would fall back to its LIVE price instead, which is a different number --
    that substitution is the regression this asserts against.
    """
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    from portfolio.services.returns import to_jalali_str

    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    # Deliberately unequal to the candle close, so the two sources are telling
    # the chart different things and the assertion can say which one it read --
    # but close enough that the spike guard has no opinion, or the test would
    # pass for the wrong reason.
    write_prices({"kama_stock": Decimal("3500"), "usd_cash": Decimal("60000")})

    now = timezone.now()
    _write_candles("کاما", [to_jalali_str(now - timedelta(days=40))], price="3000")

    user = make_user(email="primed@test.test")
    account = Account.objects.create(user=user, name="Primed")
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("100"))

    series = compute_dynamic_net_worth_series(user, account, days=10)
    totals = {Decimal(row["total"]) for row in series}

    # 100 shares x 3000 Rial, under the legacy one-tenth-share convention.
    assert totals == {Decimal("30000")}, (
        "the series should carry the primed warehouse close (30000), not the "
        f"live quote (35000); got {totals}"
    )


@pytest.mark.django_db
def test_close_from_before_the_window_is_still_aged_out_when_sessions_elapsed(
    asset_catalog, write_prices, make_user
):
    """Priming exists to feed the staleness guard, not to defeat it.

    Same shape as above, except the market kept trading throughout. The primed
    close is then 40 days and many sessions old, so the guard must drop the
    holding rather than carry it -- which is only possible because priming
    recorded a `last_priced_date` for it to measure.
    """
    from portfolio.services.valuation import compute_dynamic_net_worth_series
    from portfolio.services.returns import to_jalali_str

    asset = asset_catalog["kama_stock"]
    asset.tse_symbol = "کاما"
    asset.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("9999"), "usd_cash": Decimal("60000")})

    now = timezone.now()
    _write_candles("کاما", [to_jalali_str(now - timedelta(days=40))], price="3000")
    _write_candles(
        "فولاد", [to_jalali_str(now - timedelta(days=d)) for d in range(1, 12)]
    )

    user = make_user(email="agedout@test.test")
    account = Account.objects.create(user=user, name="AgedOut")
    Holding.objects.create(account=account, asset=asset, quantity=Decimal("100"))

    series = compute_dynamic_net_worth_series(user, account, days=10)

    assert float(series[-1]["total"]) == 0.0, (
        "a close 40 days and many sessions old must be aged out, not carried"
    )


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


def test_rejected_newest_close_falls_through_to_the_session_before_it(
    asset_catalog, write_prices, monkeypatch
):
    """A quarantined close is not the answer, and it is not the end of the search.

    `_newest_close_per_symbol` resolves the common case with one DISTINCT ON
    row per symbol, which by construction cannot see past a rejected top row.
    The symbol-scoped second pass is what keeps the old streaming behaviour's
    answer: skip the rejection, take the session before it. Without it a single
    bad print would read as "this symbol has no warehouse close at all" and the
    holding would silently lose its archive veto.
    """
    from datetime import timedelta

    from django.utils import timezone
    from marketdata.models import MarketCandle, RejectedRecord
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import _latest_archive_closes

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])

    newest_day = to_jalali_str(timezone.now() - timedelta(days=1))
    prior_day = to_jalali_str(timezone.now() - timedelta(days=2))
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time=newest_day, close_price=Decimal("99999"),
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time=prior_day, close_price=Decimal("5200"),
    )
    RejectedRecord.objects.create(
        endpoint="stock_candle_adjusted", symbol="کاما",
        date=newest_day, reason="series_spike",
    )

    prices, dates = _latest_archive_closes([stock])

    assert prices["kama_stock"] == Decimal("5200")
    assert dates["kama_stock"] == prior_day


def test_every_rejected_close_leaves_the_symbol_unresolved(
    asset_catalog, write_prices, monkeypatch
):
    """The second pass must exhaust, not loop: a symbol whose every stored close
    is quarantined has no archive answer, and saying so is what lets the caller
    report the gap rather than reach for a number that was thrown out."""
    from datetime import timedelta

    from django.utils import timezone
    from marketdata.models import MarketCandle, RejectedRecord
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import _latest_archive_closes

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])

    for offset in (1, 2):
        day = to_jalali_str(timezone.now() - timedelta(days=offset))
        MarketCandle.objects.create(
            symbol="کاما", timeframe=MarketCandle.ADJUSTED,
            date_time=day, close_price=Decimal("5200"),
        )
        RejectedRecord.objects.create(
            endpoint="stock_candle_adjusted", symbol="کاما",
            date=day, reason="series_spike",
        )

    prices, _dates = _latest_archive_closes([stock])

    assert "kama_stock" not in prices


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


def test_archive_is_behind_protection_holds_without_an_explicit_session_map(
    asset_catalog, write_prices, monkeypatch
):
    """A caller that supplies no session map still gets the protection.

    Opting IN to it is how the closed-market regression reached three separate
    branches; the resolver now falls back to the sessions already stored.
    """
    from datetime import timedelta

    from django.utils import timezone
    from marketdata.models import MarketCandle
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import _archive_replacements

    stock = asset_catalog["kama_stock"]
    stock.tse_symbol = "کاما"
    stock.save(update_fields=["tse_symbol"])
    write_prices({"kama_stock": Decimal("4890")})
    MarketCandle.objects.create(
        symbol="کاما",
        timeframe=MarketCandle.ADJUSTED,
        date_time=to_jalali_str(timezone.now() - timedelta(days=1)),
        close_price=Decimal("4750"),
    )

    # No live_fetched_at, and a market state that would otherwise hand the
    # closed-market branch a lagging close.
    replacements = _archive_replacements(
        {"kama_stock": Decimal("4890")}, market_state="closed_daytime"
    )

    assert "kama_stock" not in replacements


# ---------------------------------------------------------------------------
# Switched-off holdings, personal nicknames, and multiple properties
#
# A holding can be listed without being counted -- someone's primary residence
# belongs in the inventory but swamps every figure of a portfolio they actually
# trade. The exclusion has to be identical everywhere: today's total, the
# allocation, the risk weights, the performance metrics and the recorded
# history. These pin the parts of that which are cheap to assert.
# ---------------------------------------------------------------------------


def _house_holding(account, asset, *, price_per_sqm_million, area_sqm, hidden=False):
    """Holding plus a mark dated well before typical chart windows.

    History reads marks, not current qty. Without a past opening, hide cannot
    Y-shift recorded snapshots (the house did not exist on those days).
    """
    record_house_mark(
        user=account.user,
        account_id=account.id,
        asset=asset,
        quantity=Decimal(str(price_per_sqm_million)),
        area_sqm=Decimal(str(area_sqm)),
        occurred_at=timezone.now() - timedelta(days=400),
    )
    holding = Holding.objects.get(account=account, asset=asset)
    if hidden:
        holding.is_hidden = True
        holding.save(update_fields=["is_hidden"])
    return holding


def test_house_value_is_area_times_price_per_sqm_in_millions(asset_catalog, make_user):
    """91 m2 at 100 million a meter is 9.1 billion Toman.

    Unit test: one arithmetic convention, no I/O, and the cheapest place to pin
    the number the whole real-estate feature rests on.
    """
    from portfolio.services.valuation import _house_value

    assert _house_value(Decimal("100"), area_sqm=Decimal("91")) == Decimal("9100000000")


def test_hidden_holding_is_listed_but_not_counted(
    asset_catalog, make_user, write_prices
):
    """Integration: the split between `items` and `hidden_items` is what makes
    every downstream consumer -- donut, weights, diagnostics, optimizer -- honour
    the tick without code of its own, so it is asserted at that boundary."""
    write_prices({"emami_coin": Decimal("100")})
    account = Account.objects.create(user=make_user(email="hidden@test.test"), name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    _house_holding(
        account, asset_catalog["house_asset"],
        price_per_sqm_million=100, area_sqm=91, hidden=True,
    )

    result = value_account(account)

    assert [i["key"] for i in result["items"]] == ["emami_coin"]
    assert [i["key"] for i in result["hidden_items"]] == ["house_asset"]
    # Valued, so the UI can show what is being left out -- just not added in.
    assert Decimal(result["hidden_items"][0]["value"]) == Decimal("9100000000")
    assert result["total"] == Decimal("200")
    assert result["total_assets"] == 1


def test_snapshot_writers_still_record_everything_owned(
    asset_catalog, make_user, write_prices
):
    """`include_hidden=True` keeps the stored series meaning one thing.

    If the writer started omitting whatever was switched off, the chart would
    show a cliff on the day the box was unticked rather than a continuous line.
    """
    write_prices({"emami_coin": Decimal("100")})
    account = Account.objects.create(user=make_user(email="snap@test.test"), name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    _house_holding(
        account, asset_catalog["house_asset"],
        price_per_sqm_million=100, area_sqm=91, hidden=True,
    )

    assert value_account(account, include_hidden=True)["total"] == Decimal("9100000200")
    assert value_account(account)["total"] == Decimal("200")


def test_hiding_a_mortgaged_house_takes_its_mortgage_with_it(
    asset_catalog, make_user, write_prices
):
    """Otherwise net worth drops by the loan alone."""
    from portfolio.models import Liability

    write_prices({"emami_coin": Decimal("100")})
    account = Account.objects.create(user=make_user(email="mortgage@test.test"), name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    house = asset_catalog["house_asset"]
    _house_holding(account, house, price_per_sqm_million=100, area_sqm=91, hidden=True)
    Liability.objects.create(
        account=account, asset=house, label="Mortgage", amount_tomans=Decimal("500000000")
    )

    result = value_account(account)

    assert result["total"] == Decimal("200")
    assert result["total_liabilities"] == 0.0


def test_a_portfolio_can_hold_more_than_one_property(asset_catalog, make_user):
    """Properties are minted per holding, so three homes are three catalog rows
    owned by that user -- the shared catalog is untouched."""
    user = make_user(email="landlord@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)

    for name, sqm, price in (("Home", "91", "100"), ("Shop", "40", "250")):
        resp = client.post(
            f"/api/accounts/{account.id}/holdings/",
            {"new_property_name": name, "area_sqm": sqm, "price_per_sqm_million": price},
            format="json",
        )
        assert resp.status_code == 201, resp.data

    owned = Asset.objects.filter(owner=user, is_house=True)
    assert owned.count() == 2
    assert account.holdings.filter(asset__is_house=True).count() == 2
    # 91 x 100M + 40 x 250M
    assert value_account(account)["total"] == Decimal("19100000000")


def test_seed_assets_leaves_user_properties_active(asset_catalog, make_user):
    """The seeder deactivates anything outside its list, and it runs on every
    container start -- unscoped, it switched off every property ever created."""
    from django.core.management import call_command

    user = make_user(email="seed@test.test")
    mine = Asset.objects.create(
        key="re-abc123", name="Home", asset_class="Real Estate",
        is_house=True, owner=user,
    )

    call_command("seed_assets")

    mine.refresh_from_db()
    assert mine.is_active is True


def test_a_stock_holding_is_named_by_its_ticker_not_the_company(
    asset_catalog, make_user, write_prices
):
    """`name_fa` on a TSE row is the REGISTERED COMPANY name.

    خگستر is seeded with name_fa="گسترش‌سرمایه‌گذاری‌ایران‌خودرو" and
    tse_symbol="خگستر". Reading `name_fa` first put the company name in the
    holdings table, the ledger and every chart tooltip -- long enough to break
    the row, and not what anyone calls the stock. A nickname the owner typed
    still outranks both.
    """
    stock = asset_catalog["kama_stock"]
    stock.name_fa = "سهام کاما"
    stock.save()
    write_prices({"kama_stock": Decimal("50000")})
    user = make_user(email="ticker@test.test")
    account = Account.objects.create(user=user, name="Main")
    holding = Holding.objects.create(
        account=account, asset=stock, quantity=Decimal("100")
    )

    assert holding.label == "کاما"

    item = next(i for i in value_account(account)["items"] if i["key"] == "kama_stock")
    assert item["label"] == "کاما"
    # The company name is still carried, for the tooltip beside the ticker.
    assert item["name_fa"] == "سهام کاما"
    assert item["symbol"] == "کاما"

    holding.display_name = "کاما - بلندمدت"
    holding.save()
    assert holding.label == "کاما - بلندمدت"

    # A gold row has no ticker and must keep reading as its Persian name.
    gold = asset_catalog["emami_coin"]
    gold.name_fa = "سکه امامی"
    gold.save()
    coin = Holding.objects.create(
        account=account, asset=gold, quantity=Decimal("1")
    )
    assert coin.label == "سکه امامی"


def test_a_counted_asset_steps_by_one_and_a_divisible_one_does_not(asset_catalog):
    """Unit test: a pure model property, no I/O and no request to make.

    Almost everything in this catalog is COUNTED -- a share, a coin, a bar, a
    banknote -- and the holdings editor offered every one of them a step of
    "any", so its spinner walked a stock position in fractions of a share. The
    three units that genuinely divide have to keep their decimals; getting that
    backwards would round somebody's gold down to the nearest gram.
    """
    counted = ["kama_stock", "emami_coin", "swiss_gold_bar_1g", "usd_cash", "euro_cash"]
    for key in counted:
        assert asset_catalog[key].quantity_step == "1", key

    for key in ["bitcoin_usd", "gold_18k_gram", "usdt_irt"]:
        assert asset_catalog[key].quantity_step == "0.000001", key

    # A property's `quantity` column is not a count at all: it holds the price of
    # a square meter in millions of Toman, so it steps freely.
    assert asset_catalog["house_asset"].quantity_step == "any"


def test_the_valuation_row_says_what_one_of_the_asset_is(
    asset_catalog, make_user, write_prices
):
    """Integration: the client cannot infer this, so the payload must carry it.

    One Gold row is sold by the gram and the rest are coins you count -- the
    asset class does not separate them, and neither does the price. Declared by
    the server for the same reason `unit_price_currency` is.
    """
    write_prices({"kama_stock": Decimal("5000"), "gold_18k_gram": Decimal("90000")})
    user = make_user(email="steps@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["kama_stock"], quantity=Decimal("120")
    )
    Holding.objects.create(
        account=account, asset=asset_catalog["gold_18k_gram"], quantity=Decimal("12.5")
    )

    rows = {i["key"]: i for i in value_account(account)["items"]}
    assert rows["kama_stock"]["quantity_step"] == "1"
    assert rows["gold_18k_gram"]["quantity_step"] == "0.000001"


def test_projected_inflation_stays_in_the_band_the_published_series_lives_in():
    """Unit test: configuration arithmetic, no I/O.

    The projection for the unpublished year shipped at 6.5%/MONTH, which
    compounds to +112%/year -- roughly triple every published year in the table
    (31-46%) and triple the CBI deposit rate this same file carries for the same
    year. At that pace the "vs inflation" line halves a portfolio's real value
    over twelve months regardless of how it performed, and nothing on the page
    said the rate was a guess. The band below is deliberately wide: it is a
    tripwire against another order-of-magnitude slip, not a second opinion on
    the operator's number.
    """
    from django.conf import settings

    if not settings.CPI_ESTIMATED_YEARS:
        pytest.skip("estimation disabled via CPI_ESTIMATED_ANNUAL_RATE=0")

    annual = settings.CPI_ESTIMATED_ANNUAL_RATE_EFFECTIVE
    assert 0.15 <= annual <= 0.60, (
        f"projected inflation is {annual:.0%}/year; every published year in "
        f"CPI_BY_JALALI_YEAR sits between 31% and 46%"
    )
    # And it must still be legible as an estimate wherever it is printed.
    assert "ESTIMATE" in settings.CPI_SOURCE
    assert f"{annual:.0%}/year" in settings.CPI_SOURCE


def test_the_inflation_chart_reports_the_rate_it_divided_by(make_user, asset_catalog):
    """Integration: the claim is about a rate, so the payload has to carry it.

    Two diverging lines and no number is unfalsifiable -- a projection running
    at triple the published pace draws the same picture as a correct one. The
    endpoint reports the pace implied by the CPI at the two ends of the drawn
    window, which is what the line is actually made of.
    """
    user = make_user(email="cpi-window@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1")
    )
    now = timezone.now()
    for days_ago, total in ((300, "1000000"), (0, "1500000")):
        Snapshot.objects.create(
            user=user,
            account=account,
            total_value_tomans=Decimal(total),
            timestamp=now - timedelta(days=days_ago),
        )

    res = _client(user).get(
        f"/api/snapshots/?days=365&basis=real_toman&account={account.id}"
    )
    # A window this long can outrun the CPI table, and refusing is the correct
    # answer when it does -- but it must never answer with a rate it cannot back.
    if res.status_code == 503:
        assert res.json()["reason"] == "cpi_unavailable"
        return

    assert res.status_code == 200
    body = res.json()
    assert body["basis"] == "real_toman"
    cpi = body["cpi"]
    assert cpi["verified_through_jalali_year"] >= 1404
    assert cpi["source"]
    # Same tripwire as the settings test, now on the number the chart prints.
    assert 0.05 <= cpi["applied_annual_rate"] <= 0.75


def test_nickname_and_visibility_save_without_touching_the_ledger(
    asset_catalog, make_user, write_prices
):
    """A rename or a tick is how a holding is shown, not something that happened
    to it. Routing them through the ledger appended a revaluation mark every
    time someone renamed a property."""
    write_prices({"swiss_gold_bar_1g": Decimal("5000000")})
    user = make_user(email="rename@test.test")
    account = Account.objects.create(user=user, name="Main")
    holding = Holding.objects.create(
        account=account, asset=asset_catalog["swiss_gold_bar_1g"], quantity=Decimal("2")
    )
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.patch(
        f"/api/accounts/{account.id}/holdings/{holding.id}/",
        {"display_name": "Dad's bar", "is_hidden": True},
        format="json",
    )

    assert resp.status_code == 200, resp.data
    holding.refresh_from_db()
    assert holding.display_name == "Dad's bar"
    assert holding.is_hidden is True
    assert holding.quantity == Decimal("2")
    assert not LedgerEntry.objects.filter(account=account).exists()


def test_recorded_history_is_netted_of_switched_off_holdings(
    asset_catalog, make_user, write_prices
):
    """Integration, at the endpoint: the subtraction spans the whole window.

    Snapshots record everything owned, so the tick has to be applied at read
    time -- and to every point, not from today forward. Applying it only to new
    rows would put a cliff in the line on the day the user changed their mind.
    A property is the clean case to assert: its worth on a date is the mark in
    force, so no warehouse price is involved.
    """
    write_prices({"emami_coin": Decimal("100")})
    user = make_user(email="history@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    _house_holding(
        account, asset_catalog["house_asset"],
        price_per_sqm_million=100, area_sqm=91, hidden=True,
    )
    # Two days of recorded totals, both counting the house (9.1B + 200).
    for days_ago in (2, 1):
        Snapshot.objects.create(
            user=user,
            account=account,
            total_value_tomans=Decimal("9100000200"),
            timestamp=timezone.now() - timedelta(days=days_ago),
        )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(f"/api/snapshots/?days=30&account={account.id}")

    assert resp.status_code == 200, resp.data
    series = resp.data["series"]
    assert len(series) >= 2
    # Every point, including the oldest, is net of the switched-off property.
    for point in series:
        assert Decimal(str(point["total"])) == Decimal("200")


def test_hidden_house_not_in_the_photograph_does_not_zero_the_line(
    asset_catalog, make_user, write_prices
):
    """Integration: hide must not subtract a house the snapshot never recorded.

    A property typed in today with a purchase date last week is absent from
    last week's photographs. Subtracting its value anyway floors those days
    at zero and puts a cliff on the day it was entered.
    """
    write_prices({"emami_coin": Decimal("100")})
    user = make_user(email="unbacked@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    _house_holding(
        account, asset_catalog["house_asset"],
        price_per_sqm_million=100, area_sqm=91, hidden=True,
    )
    for days_ago in (2, 1):
        Snapshot.objects.create(
            user=user,
            account=account,
            total_value_tomans=Decimal("200"),
            timestamp=timezone.now() - timedelta(days=days_ago),
        )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(f"/api/snapshots/?days=30&account={account.id}")

    assert resp.status_code == 200, resp.data
    for point in resp.data["series"]:
        assert Decimal(str(point["total"])) == Decimal("200")


def test_history_older_than_the_recompute_bound_is_still_netted(
    asset_catalog, make_user, write_prices, monkeypatch
):
    """Beyond the recompute ceiling the adjustment is carried back, not dropped.

    Leaving those points unadjusted would put the switched-off asset back into
    the far end of the line -- the same cliff the read-time subtraction exists to
    avoid, just relocated to the bound. They are netted from the oldest computed
    day and flagged `approximated`.
    """
    # The bound is read by `_subtract_hidden_holdings`, which lives in the
    # valuation view module.
    from portfolio.views import valuation as portfolio_views

    # Squeeze the ceiling so the test does not need years of snapshots.
    monkeypatch.setattr(portfolio_views, "HIDDEN_ADJUSTMENT_MAX_DAYS", 2)

    write_prices({"emami_coin": Decimal("100")})
    user = make_user(email="bound@test.test")
    account = Account.objects.create(user=user, name="Main")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("2")
    )
    _house_holding(
        account, asset_catalog["house_asset"],
        price_per_sqm_million=100, area_sqm=91, hidden=True,
    )
    for days_ago in (10, 1):
        Snapshot.objects.create(
            user=user,
            account=account,
            total_value_tomans=Decimal("9100000200"),
            timestamp=timezone.now() - timedelta(days=days_ago),
        )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(f"/api/snapshots/?days=30&account={account.id}")

    assert resp.status_code == 200, resp.data
    series = resp.data["series"]
    assert len(series) == 3
    # Both stored points and today's derived point are netted.
    assert all(Decimal(str(p["total"])) == Decimal("200") for p in series), series
    # And the out-of-reach one is honest about being an estimate.
    assert series[0]["approximated"] is True


def test_deleting_a_user_who_owns_a_property_succeeds(asset_catalog, make_user):
    """Account deletion must survive the new `Asset.owner` cascade.

    Before properties were user-owned, deleting a user never touched the asset
    catalog. Now it does -- and `Holding.asset` / `LedgerEntry.asset` are
    PROTECT, so the cascade walks straight into them. If Django resolves that as
    a ProtectedError, the delete-my-account endpoint breaks for every user who
    ever added a property.
    """
    user = make_user(email="closing@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        f"/api/accounts/{account.id}/holdings/",
        {"new_property_name": "Home", "area_sqm": "91", "price_per_sqm_million": "100"},
        format="json",
    )
    assert created.status_code == 201, created.data
    key = created.data["asset_key"]

    user.delete()

    assert not Asset.objects.filter(key=key).exists()
    assert not Account.objects.filter(pk=account.pk).exists()


def test_deleting_a_user_who_ran_a_csv_import_succeeds(asset_catalog, make_user):
    """The other PROTECT deadlock in the same cascade.

    `LedgerEntry.import_batch` PROTECTs `ImportBatch`, which is CASCADEd from
    `Account` -- so closing an account that ever imported a CSV raised
    ProtectedError. Pre-existing, and it fails for exactly the same reason the
    owned-property case did, which is why `User.delete` resolves both by
    ordering rather than special-casing either.
    """
    from portfolio.models import ImportBatch

    user = make_user(email="importer@test.test")
    account = Account.objects.create(user=user, name="Main")
    batch = ImportBatch.objects.create(account=account, file_hash="abc", row_count=1)
    LedgerEntry.objects.create(
        account=account, asset=asset_catalog["emami_coin"],
        kind=LedgerEntry.Kind.OPENING_POSITION, quantity=Decimal("1"),
        source="csv", import_batch=batch,
    )

    user.delete()

    assert not Account.objects.filter(pk=account.pk).exists()
    assert not ImportBatch.objects.filter(pk=batch.pk).exists()


def test_revaluing_a_property_replaces_its_price_and_keeps_its_size(
    asset_catalog, make_user
):
    """"Its value changed" in the add dialog, end to end.

    The client sends `price_per_sqm_million`; the serializer folds it onto the
    column a house actually stores it in. Two marks must REPLACE each other, not
    accumulate -- a revaluation from 100 to 150 a meter is a 150 house, not a
    250 one -- and the size must survive a price-only update.
    """
    user = make_user(email="revalue@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)
    created = client.post(
        f"/api/accounts/{account.id}/holdings/",
        {"new_property_name": "Home", "area_sqm": "91", "price_per_sqm_million": "100"},
        format="json",
    )
    assert created.status_code == 201, created.data
    holding_id = created.data["id"]

    bumped = client.patch(
        f"/api/accounts/{account.id}/holdings/{holding_id}/",
        {"price_per_sqm_million": "150"},
        format="json",
    )

    assert bumped.status_code == 200, bumped.data
    holding = Holding.objects.get(pk=holding_id)
    assert holding.quantity == Decimal("150")          # replaced, not 250
    assert holding.area_sqm == Decimal("91.00")        # size survived
    assert value_account(account)["total"] == Decimal("13650000000")  # 91 x 150M
    # The revaluation is its own dated event on top of the opening position.
    kinds = list(
        LedgerEntry.objects.filter(account=account).values_list("kind", flat=True)
    )
    assert sorted(kinds) == ["opening_position", "valuation_mark"], kinds


def test_selling_part_of_a_holding_through_the_trade_endpoint(
    asset_catalog, make_user, write_prices
):
    """The dialog's "I sold it" path on a portfolio that tracks no cash."""
    write_prices({"emami_coin": Decimal("176000000")})
    user = make_user(email="seller@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)
    bought = client.post(
        f"/api/accounts/{account.id}/trades/",
        {"asset_key": "emami_coin", "side": "buy", "quantity": "3",
         "price_tomans": "176000000"},
        format="json",
    )
    assert bought.status_code == 201, bought.data

    sold = client.post(
        f"/api/accounts/{account.id}/trades/",
        {"asset_key": "emami_coin", "side": "sell", "quantity": "1",
         "price_tomans": "180000000"},
        format="json",
    )

    assert sold.status_code == 201, sold.data
    assert account.holdings.get(asset__key="emami_coin").quantity == Decimal("2")
    account.refresh_from_db()
    # No cash was ever declared, so neither leg moved a balance.
    assert account.cash_balance_tomans == Decimal("0")


def test_recording_a_manual_asset_you_already_own_gives_it_a_value(
    asset_catalog, make_user
):
    """A stated price on a manual asset has to reach the price map.

    Manual assets (a Swiss bar, a pre-86 quarter coin) have no feed, so the only
    price they will ever have is one the user states. Storing it on the ledger
    row alone left the asset with no Price row: it valued as "unavailable" and
    added nothing, so recording something you own made it disappear from your
    net worth instead of increasing it.
    """
    user = make_user(email="manualprice@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.post(
        f"/api/accounts/{account.id}/ledger/",
        {
            "kind": "opening_position",
            "asset_key": "swiss_gold_bar_1g",
            "quantity": "2",
            "unit_price_tomans": "5000000",
        },
        format="json",
    )

    assert resp.status_code == 201, resp.data
    result = value_account(account)
    assert result["total"] == Decimal("10000000")
    assert result["items"][0]["quality_status"] == "manual"


def test_a_backdated_entry_never_overwrites_a_newer_manual_price(
    asset_catalog, make_user, write_prices
):
    """The guard on the fix above: only fill a price that is missing.

    Recording "I bought this bar in 2020 for 2 million" must not reprice today's
    holding at 2 million.
    """
    write_prices({"swiss_gold_bar_1g": Decimal("5000000")})
    user = make_user(email="backdated@test.test")
    account = Account.objects.create(user=user, name="Main")
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.post(
        f"/api/accounts/{account.id}/ledger/",
        {
            "kind": "opening_position",
            "asset_key": "swiss_gold_bar_1g",
            "quantity": "1",
            "unit_price_tomans": "2000000",
            "occurred_at": (timezone.now() - timedelta(days=1800)).isoformat(),
        },
        format="json",
    )

    assert resp.status_code == 201, resp.data
    # Still valued at the current 5,000,000 mark, not the 2020 purchase price.
    assert value_account(account)["total"] == Decimal("5000000")

@pytest.mark.django_db
def test_house_series_is_zero_before_the_purchase_mark(asset_catalog, make_user):
    """Unit: chart days before the mark must not carry today's house value."""
    from portfolio.services.valuation import compute_dynamic_net_worth_series

    user = make_user(email="househist@test.test")
    account = Account.objects.create(user=user, name="Property")
    house = asset_catalog["house_asset"]
    purchased = timezone.now() - timedelta(days=5)
    record_house_mark(
        user=user,
        account_id=account.id,
        asset=house,
        quantity=Decimal("100"),
        area_sqm=Decimal("91"),
        occurred_at=purchased,
    )
    series = compute_dynamic_net_worth_series(user, account, days=10)
    valued = Decimal("9100000000")
    before = [row for row in series if row["date"] < purchased.date().isoformat()]
    after = [row for row in series if row["date"] >= purchased.date().isoformat()]
    assert before and after
    assert all(Decimal(row["total"]) == 0 for row in before)
    assert all(Decimal(row["total"]) == valued for row in after)



# ----------------------------------------------------------------------
# The TSE unit boundary: quantities are true broker share counts, prices are
# Rial, and the PRODUCT is divided by ten exactly once.
#
# Unit tests for the helper (pure arithmetic), then integration tests for the
# three price-resolution paths, because the bug this replaced was precisely that
# those three disagreed with each other.


def test_holding_value_divides_the_product_not_the_price():
    from marketdata.currency import holding_value_to_toman, is_tse_priced

    class _A:
        def __init__(self, tse_symbol=""):
            self.tse_symbol = tse_symbol

    tse, gold = _A("کاما"), _A()
    assert is_tse_priced(tse) and not is_tse_priced(gold)
    # 26,000,000 shares x 5,330 Rial = 138,580,000,000 Rial = 13,858,000,000 Toman.
    assert holding_value_to_toman(
        tse, Decimal("26000000") * Decimal("5330")
    ) == Decimal("13858000000")
    # A Toman-quoted asset is returned untouched.
    assert holding_value_to_toman(gold, Decimal("480000000")) == Decimal("480000000")


def test_a_manual_stock_is_not_divided():
    """`is_tse_priced` keys on the symbol, not the asset class. A manual stock
    has no TSE feed and is priced by an operator in Toman; dividing it would
    report a tenth of its worth.
    """
    from marketdata.currency import holding_value_to_toman

    class _A:
        tse_symbol = ""
        asset_class = "Stock"

    assert holding_value_to_toman(_A(), Decimal("1000")) == Decimal("1000")


def test_every_valuation_path_agrees_on_a_tse_holding(
    asset_catalog, write_prices, make_user
):
    """value_account, value_as_of and the net-worth series must return the same
    Toman figure for the same holding on the same day. They each own a separate
    TSE branch, so a divisor added to one and missed by another is invisible
    until two screens disagree.
    """
    from portfolio.services.valuation import (
        compute_dynamic_net_worth_series,
        value_account,
        value_as_of,
    )

    import jdatetime
    from marketdata.models import MarketCandle

    write_prices({"kama_stock": Decimal("5330")})
    user = make_user(email="tse-agree@test.test")
    account = Account.objects.create(user=user, name="Broker")
    Holding.objects.create(
        account=account, asset=asset_catalog["kama_stock"], quantity=Decimal("26000000")
    )
    # value_as_of reads the warehouse, not the live price map, so it needs a
    # close of its own -- same Rial number, so all three must land on one figure.
    today_jalali = jdatetime.date.fromgregorian(date=timezone.now().date()).strftime(
        "%Y-%m-%d"
    )
    MarketCandle.objects.create(
        symbol="کاما", timeframe=MarketCandle.ADJUSTED,
        date_time=today_jalali, close_price=Decimal("5330"),
        open_price=Decimal("5330"), high_price=Decimal("5330"),
        low_price=Decimal("5330"), volume=1,
    )
    expected = Decimal("13858000000")

    assert Decimal(value_account(account)["total"]) == expected
    series = compute_dynamic_net_worth_series(user, account, days=2)
    assert Decimal(series[-1]["total"]) == expected
    assert Decimal(value_as_of(user, account, as_of=timezone.now())["total"]) == expected


def test_a_live_only_class_is_priced_by_every_valuation_path(
    asset_catalog, write_prices, make_user
):
    """Crypto has no provider history endpoint, so the candle and gold-history
    branches can never find it a close. It used to be `missing_price` in every
    as-of valuation -- and every as-of is a TWR cash-flow boundary, so one coin
    made performance permanently unavailable for the whole account.
    """
    import jdatetime
    from marketdata.models import (
        GoldCurrencyHistory, MarketDailyBar, MarketInstrument, MarketSnapshot,
    )
    from portfolio.services.valuation import (
        compute_dynamic_net_worth_series,
        value_account,
        value_as_of,
    )

    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "BTC"
    coin.save(update_fields=["brs_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="BTC", name="Bitcoin",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )
    today_jalali = jdatetime.date.fromgregorian(date=timezone.now().date()).strftime(
        "%Y-%m-%d"
    )
    # The provider quotes crypto in Tether and the bar table has no unit column,
    # so the unit comes off the snapshot payload and uses USDT/Toman.
    MarketSnapshot.objects.create(
        asset_class="crypto", symbol="BTC", observed_at=timezone.now(),
        last_price=Decimal("2"), provider_payload={"unit": "تتر"},
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=today_jalali, close_price=Decimal("100000"),
        unit="تومان",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT", date=today_jalali, close_price=Decimal("100000"),
        unit="تومان",
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.CRYPTO, symbol="BTC",
        date=today_jalali, open_price=Decimal("2"), high_price=Decimal("2"),
        low_price=Decimal("2"), close_price=Decimal("2"),
    )
    write_prices({"bitcoin_usd": Decimal("200000")})

    user = make_user(email="live-only-class@test.test")
    account = Account.objects.create(user=user, name="Wallet")
    Holding.objects.create(
        account=account, asset=coin, quantity=Decimal("3")
    )

    # 3 coins x 2 Tether x 100,000 Toman/Tether.
    expected = Decimal("600000")
    as_of = value_as_of(user, account, as_of=timezone.now())
    assert as_of["excluded"] == []
    assert Decimal(as_of["total"]) == expected
    assert Decimal(value_account(account)["total"]) == expected
    series = compute_dynamic_net_worth_series(user, account, days=2)
    assert Decimal(series[-1]["total"]) == expected


def test_a_dollar_quoted_bar_is_never_read_as_toman(asset_catalog, make_user):
    """With no dollar rate to convert it, a Tether-quoted close must yield no
    price at all. Returning the foreign number is how one Bitcoin came to be
    worth ~64,500 Toman.
    """
    import jdatetime
    from marketdata.models import MarketDailyBar, MarketInstrument, MarketSnapshot
    from portfolio.services.valuation import value_as_of

    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "BTC"
    coin.save(update_fields=["brs_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="BTC", name="Bitcoin",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )
    today_jalali = jdatetime.date.fromgregorian(date=timezone.now().date()).strftime(
        "%Y-%m-%d"
    )
    MarketSnapshot.objects.create(
        asset_class="crypto", symbol="BTC", observed_at=timezone.now(),
        last_price=Decimal("64500"), provider_payload={"unit": "تتر"},
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.CRYPTO, symbol="BTC",
        date=today_jalali, open_price=Decimal("64500"), high_price=Decimal("64500"),
        low_price=Decimal("64500"), close_price=Decimal("64500"),
    )

    user = make_user(email="no-rate@test.test")
    account = Account.objects.create(user=user, name="Wallet")
    Holding.objects.create(account=account, asset=coin, quantity=Decimal("1"))

    result = value_as_of(user, account, as_of=timezone.now())
    assert Decimal(result["total"]) == Decimal("0")
    assert [e["reason"] for e in result["excluded"]] == ["missing_price"]


def test_point_in_time_brs_history_converts_foreign_quotes_to_toman(make_user):
    import jdatetime
    from portfolio.services.valuation import value_as_of

    today = jdatetime.date.fromgregorian(date=timezone.now().date()).strftime("%Y-%m-%d")
    coin = Asset.objects.create(
        key="bitcoin_usd", name="Bitcoin", is_active=True,
        asset_class=Asset.AssetClass.CRYPTO, brs_symbol="BTC",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=today, close_price=Decimal("100000"), unit="تومان",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT", date=today, close_price=Decimal("100000"), unit="تومان",
    )
    GoldCurrencyHistory.objects.create(
        symbol="BTC", date=today, close_price=Decimal("2"), unit="تتر",
    )
    user = make_user(email="historical-units@test.test")
    account = Account.objects.create(user=user, name="Wallet")
    Holding.objects.create(account=account, asset=coin, quantity=Decimal("3"))

    result = value_as_of(user, account, as_of=timezone.now())

    assert Decimal(result["total"]) == Decimal("600000")


def test_a_pre_open_quote_does_not_stay_live_through_its_own_session(
    asset_catalog, monkeypatch
):
    """The session walk began at quote_date + 1, so a print taken BEFORE the
    open was never measured against its own day's session: it stayed green all
    day and, under closed-market grace, into the evening too.
    """
    from datetime import datetime, timezone as dt_timezone

    from portfolio.services.valuation import _clock_sessions_elapsed

    kama = asset_catalog["kama_stock"]
    # Wednesday 26 Aug 2026 is a TSE trading day. 07:00 Tehran = 03:30 UTC,
    # ninety minutes before the 08:30 open; read back at 20:00 Tehran.
    quote = datetime(2026, 8, 26, 3, 30, tzinfo=dt_timezone.utc)
    evening = datetime(2026, 8, 26, 16, 30, tzinfo=dt_timezone.utc)

    assert _clock_sessions_elapsed(kama, quote, evening) == 1

    # Read back before the open, nothing has been missed yet.
    pre_open = datetime(2026, 8, 26, 4, 0, tzinfo=dt_timezone.utc)
    assert _clock_sessions_elapsed(kama, quote, pre_open) == 0


def test_a_market_wide_closure_is_not_counted_as_a_missed_session(
    asset_catalog, monkeypatch
):
    """Wall-clock counting alone flips every holding to Stale on day one of a
    Nowruz shutdown, because a shut exchange looks exactly like a missed session.
    """
    from datetime import datetime, timezone as dt_timezone

    from portfolio.services import valuation as val

    kama = asset_catalog["kama_stock"]
    # Two TSE trading weekdays after the quote, both of which the exchange shut.
    quote = datetime(2026, 8, 23, 16, 30, tzinfo=dt_timezone.utc)
    later = datetime(2026, 8, 26, 16, 30, tzinfo=dt_timezone.utc)

    monkeypatch.setattr(val, "_known_closure_days", lambda *a, **k: frozenset())
    without_calendar = val._clock_sessions_elapsed(kama, quote, later)
    assert without_calendar == 3, "24th, 25th and 26th all look like sessions"

    # 1405-06-02 and -03 are 2026-08-24 and -25. The current day is deliberately
    # NOT in the closure set -- a missing candle for today is the quota-starvation
    # case, not evidence the exchange was shut -- so today's session still counts.
    monkeypatch.setattr(
        val, "_known_closure_days",
        lambda *a, **k: frozenset({"1405-06-02", "1405-06-03"}),
    )
    with_calendar = val._clock_sessions_elapsed(kama, quote, later)

    assert with_calendar == 1, "only the un-vouched-for current day is counted"


def test_a_crypto_quote_is_dollars_even_with_no_unit_label(asset_catalog, make_user):
    """Cryptocurrency.php sends no unit string. It sends the quote twice --
    `price` in dollars and `price_toman` converted -- and the snapshot ingest
    stores the first. Without reading that pair, Bitcoin valued at 79,606 Toman
    instead of 15.9 billion.
    """
    import jdatetime
    from marketdata.models import (
        GoldCurrencyHistory, MarketDailyBar, MarketInstrument, MarketSnapshot,
    )
    from portfolio.services.valuation import value_as_of

    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "Bitcoin"
    coin.save(update_fields=["brs_symbol"])
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.BRS, symbol="Bitcoin", name="Bitcoin",
        category=MarketInstrument.Category.CRYPTO, eligible=True,
    )
    today_jalali = jdatetime.date.fromgregorian(date=timezone.now().date()).strftime(
        "%Y-%m-%d"
    )
    MarketSnapshot.objects.create(
        asset_class="crypto", symbol="Bitcoin", observed_at=timezone.now(),
        last_price=Decimal("79606"),
        provider_payload={"price": "79606", "price_toman": "15961436042"},
    )
    GoldCurrencyHistory.objects.create(
        symbol="USD", date=today_jalali, close_price=Decimal("200000"), unit="تومان",
    )
    MarketDailyBar.objects.create(
        asset_class=MarketDailyBar.AssetClass.CRYPTO, symbol="Bitcoin",
        date=today_jalali, open_price=Decimal("79606"), high_price=Decimal("79606"),
        low_price=Decimal("79606"), close_price=Decimal("79606"),
    )

    user = make_user(email="btc-unit@test.test")
    account = Account.objects.create(user=user, name="Wallet")
    Holding.objects.create(account=account, asset=coin, quantity=Decimal("1"))

    result = value_as_of(user, account, as_of=timezone.now())
    assert result["excluded"] == []
    # 79,606 dollars x 200,000 Toman, not 79,606 Toman.
    assert Decimal(result["total"]) == Decimal("15921200000")


# ----------------------------------------------------------------------
# Foreign-seed Price rows reach valuation only after provider-unit normalization.


def test_verified_toman_foreign_price_is_not_converted_twice(asset_catalog):
    from portfolio.tasks import _write_prices

    _write_prices(
        {"bitcoin_usd": Decimal("5700000000"), "usd_cash": Decimal("60000")},
        normalized_foreign_keys={"bitcoin_usd"},
    )
    cache.delete("prices:latest:verified-toman-v2")

    assert get_latest_prices()["bitcoin_usd"] == Decimal("5700000000")


def test_old_unlabelled_foreign_price_is_unavailable(asset_catalog):
    Price.objects.create(
        asset=asset_catalog["bitcoin_usd"], price=Decimal("95000"), source="API",
    )
    Price.objects.create(
        asset=asset_catalog["usd_cash"], price=Decimal("60000"), source="API",
    )
    cache.delete("prices:latest:verified-toman-v2")

    assert get_latest_prices()["bitcoin_usd"] == Decimal("0")


def test_old_unlabelled_foreign_price_does_not_hide_a_declared_archive_close(
    asset_catalog, make_user,
):
    from portfolio.services.returns import to_jalali_str
    from portfolio.services.valuation import compute_dynamic_net_worth_series, value_as_of

    coin = asset_catalog["bitcoin_usd"]
    coin.brs_symbol = "BTC"
    coin.save(update_fields=["brs_symbol"])
    day = to_jalali_str(timezone.now())
    GoldCurrencyHistory.objects.create(
        symbol="BTC", date=day, close_price=Decimal("2"), unit="تتر",
    )
    GoldCurrencyHistory.objects.create(
        symbol="USDT_IRT", date=day, close_price=Decimal("100000"), unit="تومان",
    )
    Price.objects.create(asset=coin, price=Decimal("2"), source="API")
    cache.delete("prices:latest:verified-toman-v2")

    assert get_latest_prices()["bitcoin_usd"] == Decimal("200000")
    user = make_user(email="archived-btc@test.test")
    account = Account.objects.create(user=user, name="Crypto")
    Holding.objects.create(account=account, asset=coin, quantity=Decimal("3"))
    assert Decimal(value_as_of(user, account, as_of=timezone.now())["total"]) == Decimal("600000")
    series = compute_dynamic_net_worth_series(user, account, days=2)
    assert Decimal(series[-1]["total"]) == Decimal("600000")


def test_a_bitcoin_holding_is_worth_billions_not_thousands(asset_catalog, make_user):
    user = make_user(email="btc-scale@test.test")
    account = Account.objects.create(user=user, name="BTC")
    Holding.objects.create(
        account=account, asset=asset_catalog["bitcoin_usd"], quantity=Decimal("2"),
    )

    # The map the valuation receives has already been normalized to Toman.
    result = value_account(account, prices={
        "bitcoin_usd": Decimal("5700000000"), "usd_cash": Decimal("60000"),
    })

    assert result["total"] == Decimal("11400000000")


def test_an_unsecured_loan_is_spread_over_the_book_not_charged_to_the_pair(
    asset_catalog, write_prices, make_user
):
    """A loan secured on nothing is charged against the WHOLE book.

    The matched pair is only ever part of it -- an asset bought today, or one
    dropped by the forward-fill guard, is in neither side. Charging the whole
    loan to that smaller base levers the day up, and `_twr_index` is a running
    product that never gives it back. Pro-rated to the share of yesterday's book
    the pair represents, so the leverage stays proportionate to what is measured.
    """
    from portfolio.models import Liability
    from portfolio.services.valuation import compute_dynamic_net_worth_series

    write_prices({
        "emami_coin": Decimal("1000000"),
        "half_coin": Decimal("500000"),
        "usd_cash": Decimal("60000"),
    })
    user = make_user(email="unsecured-loan@test.test")
    account = Account.objects.create(user=user, name="Levered")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1"),
    )
    Holding.objects.create(
        account=account, asset=asset_catalog["half_coin"], quantity=Decimal("1"),
    )
    Liability.objects.create(
        account=account, label="Personal loan", amount_tomans=Decimal("800000"),
    )

    series = compute_dynamic_net_worth_series(user, account, days=4)

    # Net worth is assets minus the loan, on every day of the window.
    assert Decimal(series[-1]["total"]) == Decimal("700000")
    for point in series[1:]:
        base = Decimal(point["total_ex_flows_base"])
        paired = Decimal(point["total_ex_flows"])
        # The loan never exceeds the book it is spread over, so neither side of
        # the pair can go negative and flip the index through zero.
        assert base >= 0 and paired >= 0


# ----------------------------------------------------------------------
# A loan is a schedule, not a number someone re-types every month
# ----------------------------------------------------------------------


def _loan(account, **terms):
    from portfolio.models import Liability

    defaults = {
        "label": "Bank loan",
        "kind": Liability.Kind.BANK_LOAN,
        "lender": "Bank Melli",
        "amount_tomans": Decimal("0"),
    }
    return Liability.objects.create(account=account, **{**defaults, **terms})


def test_an_amortizing_loan_owes_less_every_month(db, make_user):
    """The balance is derived from the terms, not read off a stale column.

    Straight-line "principal minus payments made" is the tempting shortcut and
    it is wrong for the whole life of the loan: early installments are mostly
    interest, so at the midpoint of a 20%/5-year loan it understates the debt
    by roughly a third of the balance.
    """
    import datetime as dt
    from django.utils import timezone

    user = make_user(email="amortize@test.test")
    account = Account.objects.create(user=user, name="Levered")
    loan = _loan(
        account,
        principal_tomans=Decimal("600000000"),
        annual_rate_pct=Decimal("20"),
        term_months=60,
        started_on=dt.date(2024, 1, 15),
    )

    assert loan.balance_basis == "amortized"
    # Nothing paid on day one: the full principal is owed.
    at_start = loan.outstanding_tomans(
        timezone.make_aware(dt.datetime(2024, 1, 15, 12, 0))
    )
    assert at_start == Decimal("600000000.0000")

    # Halfway through the term, still owing well over half -- the point of the
    # formula. 29 of 60 installments paid (Jalali 1402-10-25 to 1405-04-24),
    # and the balance is 382m against a straight-line 300m.
    halfway = loan.outstanding_tomans(
        timezone.make_aware(dt.datetime(2026, 7, 15, 12, 0))
    )
    assert loan.installments_paid(
        timezone.make_aware(dt.datetime(2026, 7, 15, 12, 0))
    ) == 29
    assert Decimal("380000000") < halfway < Decimal("385000000")
    straight_line = Decimal("600000000") * Decimal(60 - 29) / 60
    assert halfway > straight_line

    # Past maturity it is settled, never negative -- a matured loan read as an
    # asset would add its whole balance to net worth.
    assert loan.outstanding_tomans(
        timezone.make_aware(dt.datetime(2031, 1, 15, 12, 0))
    ) == Decimal("0.0000")


def test_the_payoff_date_is_the_month_the_balance_reaches_zero(db, make_user):
    """`payoff_on` and `installments_paid` must name the same month.

    They read `started_on` independently -- one adds the term to it, the other
    counts months elapsed since it -- so a disagreement about whether that date
    is origination or the first installment shows up as a payoff date a month
    away from the month the balance actually hits zero. The date is built on
    the Jalali calendar, so the assertion is made there rather than against a
    Gregorian literal that would drift.
    """
    import datetime as dt

    import jdatetime
    from django.utils import timezone

    user = make_user(email="payoff@test.test")
    account = Account.objects.create(user=user, name="Levered")
    loan = _loan(
        account,
        principal_tomans=Decimal("600000000"),
        annual_rate_pct=Decimal("20"),
        term_months=60,
        started_on=dt.date(2024, 1, 15),
    )

    start = jdatetime.date.fromgregorian(date=loan.started_on)
    payoff = jdatetime.date.fromgregorian(date=loan.payoff_on())
    assert (payoff.year - start.year) * 12 + (payoff.month - start.month) == 60

    # The same day, from the other direction: every installment is accounted
    # for and nothing is left owing.
    at_payoff = timezone.make_aware(
        dt.datetime.combine(loan.payoff_on(), dt.time(12, 0))
    )
    assert loan.installments_paid(at_payoff) == 60
    assert loan.outstanding_tomans(at_payoff) == Decimal("0.0000")

    # And one month earlier it is not yet settled, so the date is not merely
    # somewhere past maturity.
    a_month_before = timezone.make_aware(
        dt.datetime.combine(
            jdatetime.date(payoff.year, payoff.month - 1, payoff.day).togregorian(),
            dt.time(12, 0),
        )
    )
    assert loan.installments_paid(a_month_before) == 59
    assert loan.outstanding_tomans(a_month_before) > Decimal("0")


def test_a_loan_quoted_in_installments_owes_what_is_left_to_pay(db, make_user):
    """Iranian banks quote a loan as "N installments of X", not as a rate.

    With no rate there is no amortization to do: the interest is already inside
    the installment, so what is owed is what is left to hand over.
    """
    import datetime as dt
    from django.utils import timezone

    user = make_user(email="installments@test.test")
    account = Account.objects.create(user=user, name="Levered")
    loan = _loan(
        account,
        monthly_installment_tomans=Decimal("5000000"),
        term_months=36,
        started_on=dt.date(2025, 1, 1),
    )

    assert loan.balance_basis == "installments"
    # Twelve months in, 24 installments left. Counted on the JALALI calendar,
    # which is why the anniversary is 1404-10-12 (2026-01-02) and not the
    # Gregorian 2026-01-01 -- one day earlier is still only eleven payments in.
    owed = loan.outstanding_tomans(
        timezone.make_aware(dt.datetime(2026, 1, 2, 12, 0))
    )
    assert owed == Decimal("120000000.0000")
    assert loan.outstanding_tomans(
        timezone.make_aware(dt.datetime(2026, 1, 1, 12, 0))
    ) == Decimal("125000000.0000")


def test_a_liability_with_no_terms_keeps_the_figure_it_was_given(db, make_user):
    user = make_user(email="declared@test.test")
    account = Account.objects.create(user=user, name="Simple")
    debt = account.liabilities.create(
        label="Owed to a friend", amount_tomans=Decimal("50000000")
    )

    assert debt.balance_basis == "declared"
    assert debt.outstanding_tomans() == Decimal("50000000")


def test_valuation_nets_what_is_owed_today_not_what_was_borrowed(
    db, make_user, asset_catalog, write_prices
):
    """`value_account` must read the schedule, not the stored column.

    The stored `amount_tomans` is only the figure last written down. Netting it
    while the loan is being repaid holds net worth flat for the life of the
    loan and then jumps it the day someone remembers to edit the row.
    """
    import datetime as dt

    write_prices({"emami_coin": Decimal("1000000000")})
    user = make_user(email="netting@test.test")
    account = Account.objects.create(user=user, name="Levered")
    Holding.objects.create(
        account=account, asset=asset_catalog["emami_coin"], quantity=Decimal("1")
    )
    loan = _loan(
        account,
        # Deliberately stale: what the row says, versus what the schedule says.
        amount_tomans=Decimal("600000000"),
        principal_tomans=Decimal("600000000"),
        annual_rate_pct=Decimal("20"),
        term_months=60,
        started_on=dt.date(2024, 1, 15),
    )

    result = value_account(account)

    owed = loan.outstanding_tomans()
    assert owed < Decimal("600000000")
    assert result["total_liabilities"] == pytest.approx(float(owed))
    assert result["total"] == Decimal("1000000000") - owed
    # The itemised row reports the netted figure and the stored one separately,
    # so the two can be compared where they are supposed to agree.
    row = result["liabilities"][0]
    assert row["amount_tomans"] == pytest.approx(float(owed))
    assert row["declared_amount_tomans"] == 600000000.0
    assert row["balance_basis"] == "amortized"
