"""Standard price extraction from raw market payloads.

Ported from the legacy tracker engine `extract_standard_prices`.
Returns the canonical price map keyed by asset.key (emami_coin, kama_stock...).
"""
import logging
from decimal import Decimal

from django.conf import settings
from marketdata.currency import IRR_QUOTE_UNITS, FOREIGN_QUOTE_UNITS, canonical_symbol, to_toman
from portfolio.live import find_symbol_record

logger = logging.getLogger(__name__)

# Derived-coin constants (same values as the original project).
QUARTER_PRE86_FACTOR = Decimal("0.8694109297")
QUARTER_TO_1G_RATIO = Decimal("0.493733384")


def _build_lookup(brs_payload):
    """Index BRS rows by symbol/code/name so pricing rules stay simple."""
    lookup = {}
    if not isinstance(brs_payload, dict):
        return lookup
    for value in brs_payload.values():
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    for key_name in ("symbol", "code", "name"):
                        key = item.get(key_name)
                        if key:
                            lookup[str(key).strip().casefold()] = item
    return lookup


def _lookup_price(lookup, symbols):
    """Get the first positive price for the given symbol list."""
    for symbol in symbols:
        item = lookup.get(str(symbol).strip().casefold())
        if not isinstance(item, dict):
            continue
        try:
            price = Decimal(str(item.get("price") or 0))
        except (ArithmeticError, ValueError):
            continue
        if price > 0:
            return price
    return Decimal("0")


def _find_tsetmc_symbol(tsetmc_payload, name):
    if tsetmc_payload is None:
        # No payload at all means the price loop deliberately skipped the TSE
        # fetch because the exchange is shut -- not a failure to resolve the
        # symbol. Warning about it logged 210 alarms a day for a symbol that was
        # priced correctly all session and simply had no new quote to report.
        logger.debug("No TSETMC payload this cycle (market closed); %s unchanged.", name)
        return None
    record = find_symbol_record(tsetmc_payload, name)
    if record is None:
        logger.warning("No exact TSETMC match for %s; skipping.", name)
    return record


def _normalize_symbol_payload(symbol_payload):
    if isinstance(symbol_payload, dict):
        if symbol_payload.get("l18") or symbol_payload.get("pl") or symbol_payload.get("pc"):
            return symbol_payload
        for value in symbol_payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        return symbol_payload
    if isinstance(symbol_payload, list) and symbol_payload:
        first_item = symbol_payload[0]
        if isinstance(first_item, dict):
            return first_item
    return None


def _price_from_tsetmc_record(record):
    """Live TSE quote (`pl`/`pc`) kept in **Rial** for `portfolio_price`.

    Intentionally NOT converted to Toman. Stock holdings are entered at 1/10 of
    broker share count so `qty * rial_price` equals the true Toman market value
    (same product as `broker_qty * toman_price`). Warehouse candles stay Rial;
    gold/FX stay Toman — only TSE share quotes use this convention.

    ponytail: migrate legacy TSE quantities by 10 before accepting normal broker
    share counts; then restore the app-wide Toman boundary.
    """
    if not isinstance(record, dict):
        return Decimal("0")
    for field_name in ("pl", "pc"):
        try:
            price = Decimal(str(record.get(field_name) or 0))
        except (ArithmeticError, ValueError):
            continue
        if price > 0:
            return price
    return Decimal("0")


def _lookup_usdt_toman(lookup, usd_rate, history_payload=None):
    """Resolve USDT/IRT in Tomans.

    Prefer the provider's IRR/Toman quote (history or live row). Only when the
    feed quotes tether near 1 USD with no local unit do we scale by `usd_rate`.
    """
    if history_payload:
        from_history = _usdt_toman_from_history(history_payload, usd_rate)
        if from_history > 0:
            return from_history

    rate = Decimal(str(usd_rate or 0))
    for symbol in ["USDT_IRT", "USDTIRT", "USDT", "TETHER", "Tether", "تتر"]:
        item = lookup.get(str(symbol).strip().casefold())
        if not isinstance(item, dict):
            continue
        try:
            price = Decimal(str(item.get("price") or 0))
        except (ArithmeticError, ValueError):
            continue
        if price <= 0:
            continue
        unit = str(item.get("unit") or "").strip().casefold()
        if unit in IRR_QUOTE_UNITS or (not unit and price >= 10):
            value = to_toman(
                canonical_symbol(item.get("symbol") or symbol),
                price,
                unit or "تومان",
            )
            if value > 0:
                return value.quantize(Decimal("1"))
        if unit in FOREIGN_QUOTE_UNITS or price < 10:
            if rate > 0:
                return (price * rate).quantize(Decimal("1"))
        return price.quantize(Decimal("1"))
    return Decimal("0")


def _usdt_toman_from_history(payload, usd_rate):
    """Latest USDT close from Gold_Currency_Pro history=1/2 payload."""
    if not isinstance(payload, dict):
        return Decimal("0")
    rows = payload.get("history_daily") or payload.get("history") or []
    if not isinstance(rows, list) or not rows:
        return Decimal("0")
    latest = rows[-1]
    if not isinstance(latest, dict):
        return Decimal("0")
    close = latest.get("close")
    if close is None:
        close = latest.get("price")
    if close is None:
        return Decimal("0")
    unit = str(payload.get("unit") or "").strip()
    return to_toman(
        canonical_symbol(payload.get("symbol") or "USDT"),
        close,
        unit,
        usd_rate=usd_rate,
    ).quantize(Decimal("1"))


def _lookup_toman(lookup, symbols, *, usd_rate=None):
    for symbol in symbols:
        item = lookup.get(str(symbol).strip().casefold())
        if not isinstance(item, dict):
            continue
        value = to_toman(
            canonical_symbol(item.get("symbol") or symbol),
            item.get("price"),
            item.get("unit", ""),
            usd_rate=usd_rate,
        )
        if value > 0:
            return value.quantize(Decimal("1"))
    return Decimal("0")


def extract_standard_prices(raw_data, last_prices=None):
    """Create the standard price map used by snapshots and valuation.

    `last_prices` is the previously-fetched price map, used as a fallback when
    a source (typically KAMA) returns nothing.
    """
    last_prices = last_prices or {}
    raw_data = raw_data if isinstance(raw_data, dict) else {}
    prices = {}

    lookup = _build_lookup(raw_data.get("brsapi"))
    prices["emami_coin"] = _lookup_toman(lookup, ["IR_COIN_EMAMI"])
    prices["half_coin"] = _lookup_toman(lookup, ["IR_COIN_HALF"])
    prices["quarter_coin"] = _lookup_toman(lookup, ["IR_COIN_QUARTER"])
    prices["gold_18k_gram"] = _lookup_toman(lookup, ["IR_GOLD_18K"])
    prices["usd_cash"] = _lookup_toman(lookup, ["USD"])
    prices["bitcoin_usd"] = _lookup_price(
        lookup, ["BTC", "BTCUSDT", "BITCOIN", "Bitcoin", "بیتکوین", "بیت کوین"]
    )
    prices["usdt_irt"] = _lookup_usdt_toman(
        lookup,
        prices.get("usd_cash"),
        history_payload=raw_data.get("usdt_irt_quote"),
    )
    prices["euro_cash"] = _lookup_toman(lookup, ["EUR", "EURO", "Euro", "یورو"])
    prices["gold_ounce_usd"] = _lookup_price(
        lookup, ["XAUUSD", "XAU", "GOLD_OUNCE", "Gold Ounce (Global)", "اونس طلا", "انس طلا"]
    )

    # Swiss bars are manual (no reliable API). Coerced to Decimal once in settings.
    manual = settings.MANUAL_PRICES
    prices["swiss_gold_bar_1g"] = manual.get("swiss_gold_bar_1g", Decimal("0"))
    prices["swiss_gold_bar_2_5g"] = manual.get("swiss_gold_bar_2_5g", Decimal("0"))

    quarter_price = prices.get("quarter_coin", Decimal("0"))
    prices["quarter_coin_pre86"] = (quarter_price * QUARTER_PRE86_FACTOR).quantize(Decimal("1"))

    coin_1g = _lookup_toman(
        lookup, ["IR_COIN_1G", "IR_COIN_GRAM", "IR_GOLD_1G", "GOLD_1G"]
    )
    if not coin_1g:
        prices["one_gram_coin"] = (quarter_price * QUARTER_TO_1G_RATIO).quantize(Decimal("1"))
    else:
        prices["one_gram_coin"] = coin_1g

    # KAMA from TSETMC, with last-price fallback.
    kama = _find_tsetmc_symbol(raw_data.get("tsetmc"), "کاما")
    prices["kama_stock"] = _price_from_tsetmc_record(kama)
    if prices["kama_stock"] <= 0:
        kama = _normalize_symbol_payload(raw_data.get("tsetmc_symbol_kama"))
        prices["kama_stock"] = _price_from_tsetmc_record(kama)
    if prices["kama_stock"] <= 0:
        fallback = Decimal(str(last_prices.get("kama_stock", 0)))
        if fallback > 0:
            prices["kama_stock"] = fallback
            logger.info("KAMA using last-known price %s", fallback)

    return prices
