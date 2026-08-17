"""Symbol canonicalisation and unit-boundary helpers (Rial ↔ Toman).

Authoritative storage units:

| Table | Unit |
|---|---|
| marketdata_marketcandle, _dailystockhistory, _stocktransactiontick | Rial, provider-verbatim |
| marketdata_goldcurrencyhistory | Toman for IRR-denominated; provider-native for USD/Tether (XAUUSD=دلار, BTC=تتر) |
| marketdata_cryptohistory | close_price_usd USD, close_price_toman Toman |
| portfolio_price (TSE stocks / `tse_symbol`) | **Rial** (deliberate: holdings qty is 1/10 of broker shares so qty×rial ≈ Toman value) |
| portfolio_price (gold/FX/manual), Snapshot, LedgerEntry amounts | Toman for non-TSE; TSE ledger unit prices follow portfolio_price (Rial) |
| marketdata_reallegalhistory (buy_*/sell_*_value) | Rial, provider-verbatim (same TSE feed) |
| marketdata_marketindexdata (market_value, trade_value) | Rial; index_* are points, not money |
| marketdata_stocksymbolmetadata (market_cap, eps) | Rial; pe/ps/g_pe are dimensionless |
| marketdata_etfnavhistory (nav_*, market_price) | Rial (TSE feed); table currently empty |
| marketdata_optioncontracthistory (strike, settlement, notional) | Rial (TSE feed); table currently empty |
| marketdata_commodityhistory (close_price) | provider-native; the sibling `unit` string is the only label |

`tse_close_to_toman()` remains for **analytics** readers that need a pure-Toman
panel (returns matrix, universe) when combining TSE closes with gold Toman.
Portfolio live valuation / `portfolio_price` for stocks must NOT convert — keep
Rial so the share-count hack stays consistent.

Non-monetary conventions that bite just as hard:
  * Warehouse dates are Jalali STRINGS ("1403-10-19"); user-land time
    (LedgerEntry/Snapshot/Price) is tz-aware Gregorian. There is no single
    conversion helper -- `marketdata.jalali.normalize_jalali` is the closest.
  * `MarketCandle.date_time` is stored BOTH bare ("1405-05-09") and suffixed
    ("1405-05-09 00:00:00"). String-compared bounds must allow for both;
    `candle_close_qs` does, ad-hoc readers often do not.
  * `1d_agg` (tick-derived, NOT provider-verbatim) is retired -- nothing writes
    it any more; see `portfolio.models.DailyPriceAverage` for the live rollup.
  * A BRS symbol must never appear in marketdata_marketcandle: that table is
    Rial TSE data and BRS quotes are Toman. Guarded by a test in
    tests/test_raw_storage.py.

Gold ingest is the one deliberate write-path exception: `ingest_gold_currency_history`
calls `to_toman()` for IRR-quoted symbols and stores unit="تومان" so the table
stays uniform with existing rows.

`to_toman()` normalizes non-TSE live provider values where their declared quote
unit requires it (`portfolio.live.extractor` → `portfolio_price`).
"""
from decimal import Decimal


SYMBOL_ALIASES = {
    "USDT": "USDT_IRT",
    "USDTIRT": "USDT_IRT",
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
    """Warehouse TSE price (raw Rial) -> Toman for mixed-unit analytics.

    Returns/universe readers combine provider-verbatim TSE rows with Toman gold
    data and therefore convert here. Portfolio valuation deliberately does not;
    see the legacy quantity convention in docs/F1_POLICY.md.
    """
    if value in (None, ""):
        return None
    return Decimal(str(value)) / Decimal("10")


def to_toman(symbol, price, unit="", *, usd_rate=None):
    """Convert a provider quote to Tomans using declared units, never magnitude."""
    value = Decimal(str(price or 0))
    if value <= 0:
        return Decimal("0")
    symbol = canonical_symbol(symbol)
    unit = str(unit or "").strip().casefold()
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
