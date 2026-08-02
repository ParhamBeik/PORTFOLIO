from decimal import Decimal


SYMBOL_ALIASES = {
    "USDT": "USDT_IRT",
    "USDTIRT": "USDT_IRT",
}
UNIT_OVERRIDES = {
    "USDT_IRT": "USD",
}


def canonical_symbol(symbol):
    value = str(symbol or "").strip()
    return SYMBOL_ALIASES.get(value.upper(), value)


def to_toman(symbol, price, unit="", *, usd_rate=None):
    """Convert a provider quote to Tomans using declared units, never magnitude."""
    value = Decimal(str(price or 0))
    if value <= 0:
        return Decimal("0")
    symbol = canonical_symbol(symbol)
    unit = str(unit or UNIT_OVERRIDES.get(symbol, "")).strip().casefold()
    if unit in {"ریال".casefold(), "rial", "irr"}:
        return value / Decimal("10")
    if unit in {"usd", "dollar"} and usd_rate:
        return value * Decimal(str(usd_rate))
    return value
