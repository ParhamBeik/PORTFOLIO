from decimal import Decimal


SYMBOL_ALIASES = {
    "USDT": "USDT_IRT",
    "USDTIRT": "USDT_IRT",
}
UNIT_OVERRIDES = {
    "USDT_IRT": "USD",
}

# F1: flip to "rial" or "toman" only with documented exchange evidence.
# See docs/data-verification/F1_POLICY.md — do not infer from magnitude.
TSE_PRICE_UNIT = "rial"


def tse_unit_verified() -> bool:
    return TSE_PRICE_UNIT in {"rial", "toman"}


def partition_tse_asset_keys(keys) -> tuple[list[str], list[str]]:
    """Split asset keys into (tse_keys, other_keys) using Asset.tse_symbol."""
    from portfolio.models import Asset

    key_list = [k for k in keys if k]
    if not key_list:
        return [], []
    tse = set(
        Asset.objects.filter(key__in=key_list)
        .exclude(tse_symbol="")
        .values_list("key", flat=True)
    )
    others = [k for k in key_list if k not in tse]
    return sorted(tse), sorted(others)


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
