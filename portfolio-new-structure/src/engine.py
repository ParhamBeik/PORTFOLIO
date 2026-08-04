"""Portfolio pricing and valuation rules.

This module receives raw market data and owner holdings, then returns the clean
snapshot shape used by history files and Excel exports.
"""

from utils import log_step, load_json
from fetcher import extract_price, find_tsetmc_symbol
import logging
import os

logger = logging.getLogger(__name__)

CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "latest_prices_cache.json",
)


# --- BRS price lookup helpers ------------------------------------------------
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
            price = float(item.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            return price
    return 0


def _convert_usd_quote_to_tomans(price, usd_rate):
    """Convert USD-denominated stablecoin quotes to Tomans when needed."""
    if not price:
        return 0
    if price < 10 and usd_rate:
        return round(price * usd_rate)
    return price


# --- TSETMC/KAMA helpers -----------------------------------------------------
def _normalize_symbol_payload(symbol_payload):
    """Return a symbol record from the Symbol.php response shape."""
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


# --- Public pricing API ------------------------------------------------------
def extract_standard_prices(raw_data, constants):
    """Create the standard price map used by snapshots and exports."""
    prices = {}

    if not isinstance(raw_data, dict):
        raw_data = {}

    # BRS gives most coin, gold, currency, and crypto prices.
    log_step("Pricing mode: BRS API values where available, else derived/manual fallback.", "info")
    lookup = _build_lookup(raw_data.get("brsapi"))
    prices["emami_coin"] = _lookup_price(lookup, ["IR_COIN_EMAMI"])
    prices["half_coin"] = _lookup_price(lookup, ["IR_COIN_HALF"])
    prices["quarter_coin"] = _lookup_price(lookup, ["IR_COIN_QUARTER"])
    prices["gold_18k_gram"] = _lookup_price(lookup, ["IR_GOLD_18K"])
    prices["usd_cash"] = _lookup_price(lookup, ["USD"])
    prices["bitcoin_usd"] = _lookup_price(
        lookup,
        ["BTC", "BTCUSDT", "BITCOIN", "Bitcoin", "بیتکوین", "بیت کوین"],
    )
    tether_price = _lookup_price(
        lookup,
        ["USDT_IRT", "USDTIRT", "USDT", "TETHER", "Tether", "تتر"],
    )
    prices["usdt_irt"] = _convert_usd_quote_to_tomans(
        tether_price, prices.get("usd_cash", 0)
    )
    prices["euro_cash"] = _lookup_price(lookup, ["EUR", "EURO", "Euro", "یورو"])
    prices["gold_ounce_usd"] = _lookup_price(
        lookup,
        ["XAUUSD", "XAU", "GOLD_OUNCE", "Gold Ounce (Global)", "اونس طلا", "انس طلا"],
    )

    # Swiss bars are manual because they are not reliably available from APIs.
    prices["swiss_gold_bar_1g"] = constants.get("swiss_gold_bar_1g_price", 0)
    prices["swiss_gold_bar_2_5g"] = constants.get("swiss_gold_bar_2_5g_price", 0)

    # Some assets are derived from the quarter coin when no direct API price exists.
    quarter_price = prices.get("quarter_coin", 0)

    # Pre-86 quarter coin = quarter coin price x factor.
    prices["quarter_coin_pre86"] = round(
        quarter_price * constants.get("quarter_pre86_factor", 0.8694109297)
    )

    # 1-gram coin: API value if exists; otherwise use ratio from quarter coin.
    coin_1g = _lookup_price(lookup, ["IR_COIN_1G", "IR_COIN_GRAM", "IR_GOLD_1G", "GOLD_1G"])
    if not coin_1g:
        prices["one_gram_coin"] = round(
            quarter_price * constants.get("quarter_to_1g_ratio", 0.493733384)
        )
        if quarter_price:
            logger.info("1g Coin calculated from Quarter Coin.")
    else:
        prices["one_gram_coin"] = coin_1g

    # KAMA comes from TSETMC, with cache fallback because it is important for Father.
    kama = find_tsetmc_symbol(raw_data.get("tsetmc"), "کاما")
    prices["kama_stock"] = 0
    prices["kama_stock"] = extract_price(kama)
    kama_source = "AllSymbols"

    if prices["kama_stock"] <= 0:
        kama = _normalize_symbol_payload(raw_data.get("tsetmc_symbol_kama"))
        prices["kama_stock"] = extract_price(kama)
        kama_source = "Symbol.php"

    if prices["kama_stock"] <= 0:
        cached = load_json(CACHE_PATH)
        cached_kama = cached.get("kama_stock", 0)
        if cached_kama and cached_kama > 0:
            prices["kama_stock"] = cached_kama
            log_step(f"KAMA stock: using cached price {cached_kama:,.0f} Tomans (TSETMC lookup failed).", "warning")
        else:
            log_step("KAMA stock: price is 0 and no cache available. Father's portfolio will be undervalued!", "error")
    else:
        log_step(f"KAMA stock: {prices['kama_stock']:,.0f} Tomans (from TSETMC {kama_source}).", "success")

    log_step(
        f"Price map complete. Keys ready: {', '.join(sorted(prices))}",
        "success",
    )

    return prices


# --- Snapshot valuation ------------------------------------------------------
def _as_number(value):
    """Convert config values to numeric, safe fallback is zero."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def calculate_house_value(price_per_sqm_million, constants):
    """Compute house value as area * price_per_m2 - mortgage."""
    sqm_price_tomans = _as_number(price_per_sqm_million) * 1_000_000
    area = _as_number(constants.get("house_area_sqm", 90.2)) or 90.2
    mortgage = _as_number(constants.get("house_mortgage_deduction", 400_000_000))
    return (sqm_price_tomans * area) - mortgage


def build_snapshot(current_state, settings, latest_prices):
    """Calculate per-owner asset values and total portfolio values."""
    snapshot = {
        "portfolios": {},
        "total_values_tomans": {},
        "prices": latest_prices,
    }

    constants = settings.get("constants", {})

    for owner, assets in current_state.items():
        owner_total = 0
        snapshot["portfolios"][owner] = {}

        # Ignore invalid owner entries and keep the run alive.
        if not isinstance(assets, dict):
            logger.warning("Ignoring non-dict assets for owner '%s'.", owner)
            snapshot["total_values_tomans"][owner] = owner_total
            continue

        for asset_key, quantity in assets.items():
            value_tomans = 0

            if asset_key == "house_price_per_sqm_million":
                # Houses are priced with a custom formula.
                value_tomans = calculate_house_value(quantity, constants)
                snapshot["portfolios"][owner]["house_asset"] = {
                    "price_per_sqm_million": quantity,
                    "total_value_tomans": value_tomans,
                }
            else:
                # Other assets are simple quantity x unit price.
                unit_price = latest_prices.get(asset_key, 0)
                value_tomans = unit_price * _as_number(quantity)
                snapshot["portfolios"][owner][asset_key] = {
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "total_value_tomans": value_tomans,
                }

            owner_total += value_tomans

        snapshot["total_values_tomans"][owner] = owner_total

    return snapshot
