"""Standard price extraction from raw market payloads.

Ported from PORTFOLIO NEW STRUCTURE/src/engine.py `extract_standard_prices`.
Returns the canonical price map keyed by asset.key (emami_coin, kama_stock...).
"""
import logging
from decimal import Decimal

from django.conf import settings

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


def _convert_usd_quote_to_tomans(price, usd_rate):
    """Convert USD-denominated stablecoin quotes to Tomans when needed."""
    if not price:
        return Decimal("0")
    if price < 10 and usd_rate:
        # Match the legacy engine's round(): Toman prices are whole numbers.
        return (price * usd_rate).quantize(Decimal("1"))
    return price


def _find_tsetmc_symbol(tsetmc_payload, name):
    """Find one stock row by Persian symbol name (l18/l30, exact then partial)."""
    if not isinstance(tsetmc_payload, list) or not tsetmc_payload:
        return None
    for record in tsetmc_payload:
        if isinstance(record, dict) and record.get("l18") == name:
            return record
    for record in tsetmc_payload:
        if isinstance(record, dict) and record.get("l30") == name:
            return record
    name_lower = str(name).strip().casefold()
    for record in tsetmc_payload:
        if isinstance(record, dict):
            l18 = str(record.get("l18", "")).strip().casefold()
            if l18 and name_lower in l18:
                return record
    for record in tsetmc_payload:
        if isinstance(record, dict):
            l30 = str(record.get("l30", "")).strip().casefold()
            if l30 and name_lower in l30:
                return record
    return None


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


def extract_standard_prices(raw_data, last_prices=None):
    """Create the standard price map used by snapshots and valuation.

    `last_prices` is the previously-fetched price map, used as a fallback when
    a source (typically KAMA) returns nothing.
    """
    last_prices = last_prices or {}
    raw_data = raw_data if isinstance(raw_data, dict) else {}
    prices = {}

    lookup = _build_lookup(raw_data.get("brsapi"))
    prices["emami_coin"] = _lookup_price(lookup, ["IR_COIN_EMAMI"])
    prices["half_coin"] = _lookup_price(lookup, ["IR_COIN_HALF"])
    prices["quarter_coin"] = _lookup_price(lookup, ["IR_COIN_QUARTER"])
    prices["gold_18k_gram"] = _lookup_price(lookup, ["IR_GOLD_18K"])
    prices["usd_cash"] = _lookup_price(lookup, ["USD"])
    prices["bitcoin_usd"] = _lookup_price(
        lookup, ["BTC", "BTCUSDT", "BITCOIN", "Bitcoin", "بیتکوین", "بیت کوین"]
    )
    tether_price = _lookup_price(
        lookup, ["USDT_IRT", "USDTIRT", "USDT", "TETHER", "Tether", "تتر"]
    )
    prices["usdt_irt"] = _convert_usd_quote_to_tomans(
        tether_price, prices.get("usd_cash", Decimal("0"))
    )
    prices["euro_cash"] = _lookup_price(lookup, ["EUR", "EURO", "Euro", "یورو"])
    prices["gold_ounce_usd"] = _lookup_price(
        lookup, ["XAUUSD", "XAU", "GOLD_OUNCE", "Gold Ounce (Global)", "اونس طلا", "انس طلا"]
    )

    # Swiss bars are manual (no reliable API). Coerced to Decimal once in settings.
    manual = settings.MANUAL_PRICES
    prices["swiss_gold_bar_1g"] = manual.get("swiss_gold_bar_1g", Decimal("0"))
    prices["swiss_gold_bar_2_5g"] = manual.get("swiss_gold_bar_2_5g", Decimal("0"))

    quarter_price = prices.get("quarter_coin", Decimal("0"))
    prices["quarter_coin_pre86"] = (quarter_price * QUARTER_PRE86_FACTOR).quantize(Decimal("1"))

    coin_1g = _lookup_price(lookup, ["IR_COIN_1G", "IR_COIN_GRAM", "IR_GOLD_1G", "GOLD_1G"])
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
