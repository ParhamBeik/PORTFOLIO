"""Symbol canonicalisation and unit-boundary helpers (Rial ↔ Toman).

Authoritative storage units:

| Table | Unit |
|---|---|
| marketdata_marketcandle, _dailystockhistory, _stocktransactiontick | Rial, provider-verbatim |
| marketdata_goldcurrencyhistory | Toman for IRR-denominated; provider-native for USD/Tether (XAUUSD=دلار, BTC=تتر) |
| marketdata_cryptohistory | close_price_usd USD, close_price_toman Toman |
| portfolio_price, Snapshot, LedgerEntry, Liability | Toman (price_unit=IRT) |

`tse_close_to_toman()` is THE single Rial→Toman read boundary for TSE warehouse
rows. Read paths that combine those closes with app Toman must route through it.

Gold ingest is the one deliberate write-path exception: `ingest_gold_currency_history`
calls `to_toman()` for IRR-quoted symbols and stores unit="تومان" so the table
stays uniform with existing rows.

`to_toman()` is also the live-price blender for mixed providers
(`portfolio.live.extractor` → `portfolio_price`).
"""
from decimal import Decimal


SYMBOL_ALIASES = {
    "USDT": "USDT_IRT",
    "USDTIRT": "USDT_IRT",
}
UNIT_OVERRIDES = {
    "USDT_IRT": "USD",
}

# VERIFIED 2026-08-06 against the provider: Candlestick.php returned کاما
# close=3610 for 1405-05-14, matching the operator-confirmed TSETMC screen
# price of 3610 Rial. Every TSE endpoint (History.php, Candlestick.php,
# Transaction.php) quotes Rial, and ingest stores that number verbatim.
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


def tse_close_to_toman(value):
    """Warehouse TSE price (raw Rial) -> Toman, the app's unit.

    THE unit boundary. `marketdata_marketcandle` / `marketdata_dailystockhistory`
    / `marketdata_stocktransactiontick` store BrsApi's TSE numbers verbatim, and
    those endpoints quote Rial (see TSE_PRICE_UNIT). Every app-level consumer --
    valuation, the returns panel, trade-price resolution -- works in Toman,
    because that is the unit the user's own ledger is entered in.

    Read paths that compare or combine a warehouse close with a
    `portfolio_price` value MUST route through here; skipping it silently
    compares a Rial against a Toman and lands 10x off. Gold/currency rows need
    no conversion: that endpoint already answers in Toman.
    """
    if value in (None, ""):
        return None
    return Decimal(str(value)) / Decimal("10")


def toman_to_tse_close(value):
    """Toman -> raw Rial, the inverse of `tse_close_to_toman()`.

    THE write-side boundary. Anything derived from `portfolio_price` (already
    Toman) that lands in a Rial table -- `marketdata_marketcandle` and friends --
    must route through here, or the read side divides by 10 a second time and the
    value shows up at a tenth of the truth. `aggregate_daily_stock_history`'s
    tickless fallback is the one live caller.
    """
    if value in (None, ""):
        return None
    return Decimal(str(value)) * Decimal("10")


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
