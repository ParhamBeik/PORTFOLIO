"""Symbol canonicalisation and unit-boundary helpers (Rial ↔ Toman).

Authoritative storage units:

| Table | Unit |
|---|---|
| marketdata_marketcandle, _dailystockhistory, _stocktransactiontick | Rial, provider-verbatim |
| marketdata_goldcurrencyhistory | Toman for IRR-denominated; provider-native for USD/Tether (XAUUSD=دلار, BTC=تتر) |
| marketdata_cryptohistory | close_price_usd USD, close_price_toman Toman |
| portfolio_price, Snapshot, LedgerEntry, Liability | Toman (price_unit=IRT) |
| marketdata_reallegalhistory (buy_*/sell_*_value) | Rial, provider-verbatim (same TSE feed) |
| marketdata_marketindexdata (market_value, trade_value) | Rial; index_* are points, not money |
| marketdata_stocksymbolmetadata (market_cap, eps) | Rial; pe/ps/g_pe are dimensionless |
| marketdata_etfnavhistory (nav_*, market_price) | Rial (TSE feed); table currently empty |
| marketdata_optioncontracthistory (strike, settlement, notional) | Rial (TSE feed); table currently empty |
| marketdata_commodityhistory (close_price) | provider-native; the sibling `unit` string is the only label |

Non-monetary conventions that bite just as hard:
  * Warehouse dates are Jalali STRINGS ("1403-10-19"); user-land time
    (LedgerEntry/Snapshot/Price) is tz-aware Gregorian. There is no single
    conversion helper -- `marketdata.jalali.normalize_jalali` is the closest.
  * `MarketCandle.date_time` is stored BOTH bare ("1405-05-09") and suffixed
    ("1405-05-09 00:00:00"). String-compared bounds must allow for both;
    `candle_close_qs` does, ad-hoc readers often do not.
  * `1d_agg` rows are tick-derived, NOT provider-verbatim, so "provider-verbatim"
    above holds only for `1d_adj`/`1d_unadj`.
  * A BRS symbol must never appear in marketdata_marketcandle: that table is
    Rial TSE data and BRS quotes are Toman. Guarded by a test in
    tests/test_raw_storage.py.

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

IRR_QUOTE_UNITS = frozenset({
    "ریال".casefold(), "rial", "irr", "تومان".casefold(), "toman",
})
FOREIGN_QUOTE_UNITS = frozenset({
    "دلار".casefold(), "dollar", "usd", "تتر".casefold(), "tether", "usdt",
})


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


def gold_history_storage_unit(raw_unit):
    """Canonical warehouse unit for one declared historical BRS quote."""
    folded = str(raw_unit or "").strip().casefold()
    if folded in IRR_QUOTE_UNITS:
        return "تومان"
    if folded in FOREIGN_QUOTE_UNITS:
        return str(raw_unit).strip()
    return None
