"""Direct TSETMC, for when an Iranian egress exists.

**Status: written, wired, and NOT verified against the live origin.** Every
other module in this package was built against payloads captured from the
production VPS. This one could not be: `cdn.tsetmc.com` drops the SYN from any
non-Iranian source address, so nothing here has ever seen a real response.

That is why `TSETMC_DIRECT_ENABLED` defaults to 0 and why
`manage.py check_egress --verify-tsetmc` exists. The command probes each
endpoint below and reports what actually came back, so the switch is flipped on
evidence rather than on the confidence of this docstring. Treat the response
shapes as hypotheses until that command passes.

What this buys once it works, none of which BrsApi sells at any price:

  * **The order book** (`BestLimits`). BrsApi exposes depth only on its live
    per-symbol call and never historically. The Q7 "walks from the lower limit
    to the upper limit" model needs queue sizes at each price level, and this
    is the only route to them.
  * **Unmetered intraday ticks.** The tick backfill is the single largest
    consumer of the ~10,000/day TSETMC wallet; at one metered request per
    symbol-day it was projected at roughly two years to complete. Unmetered,
    the bound becomes politeness and disk.
  * **Adjusted and unadjusted closes from one origin**, rather than two
    separate metered endpoints that have historically disagreed.

`insCode` is TSETMC's own instrument id, not the Persian symbol. It is stable
across renames -- which is precisely why symbol-keyed history breaks when a
company is renamed and this does not -- so `StockSymbolMetadata` should carry
it before this lane is switched on.
"""
import logging

from django.conf import settings

from .http import SourceResponseError, fetch, iran_egress_proxy

logger = logging.getLogger(__name__)

ORIGIN = "tsetmc"

# TSETMC's CDN refuses or misbehaves without a browser-shaped Referer on some
# paths. Harmless when unnecessary, so it is sent unconditionally.
HEADERS = {"Referer": "https://main.tsetmc.com/"}

#: path template -> what one call is expected to return. Kept as data so
#: `check_egress` can iterate it rather than duplicating the list.
ENDPOINTS = {
    "instrument_info": "/api/Instrument/GetInstrumentInfo/{ins_code}",
    "closing_price_info": "/api/ClosingPrice/GetClosingPriceInfo/{ins_code}",
    "daily_history": "/api/ClosingPrice/GetClosingPriceDailyList/{ins_code}/0",
    "best_limits": "/api/BestLimits/{ins_code}",
    "trade_last": "/api/Trade/GetTradeLast/{ins_code}/false",
    "market_watch": "/api/ClosingPrice/GetMarketWatch",
}


def _get(path, *, timeout=(5, 30)):
    proxy = iran_egress_proxy()
    if not proxy and not getattr(settings, "TSETMC_DIRECT_ENABLED", False):
        # Fail loudly and immediately rather than spending a connect timeout
        # discovering the network is wrong. The Codal subsystem learned this the
        # expensive way: ~583 doomed connects and ~20,000 no-op runs in one day.
        raise SourceResponseError(
            "Direct TSETMC is disabled and no IRAN_EGRESS_PROXY is set. "
            "Run `manage.py check_egress` for the current reachability picture.",
            origin=ORIGIN,
        )
    url = f"{settings.TSETMC_DIRECT_BASE_URL.rstrip('/')}{path}"
    return fetch(url, origin=ORIGIN, headers=HEADERS, proxy=proxy, timeout=timeout)


def fetch_endpoint(name, ins_code=""):
    """One named endpoint, for probing and for the callers below."""
    try:
        template = ENDPOINTS[name]
    except KeyError:
        raise ValueError(f"Unknown TSETMC endpoint {name!r}.") from None
    return _get(template.format(ins_code=ins_code))


def fetch_daily_history(ins_code):
    """Full unadjusted daily history for one instrument.

    Expected shape `{"closingPriceDailyList": [...]}` with per-row `dEven`
    (Gregorian yyyymmdd as an int), `pClosing`, `pDrCotVal`, `priceMin`,
    `priceMax`, `priceYesterday`, `zTotTran`, `qTotTran5J`, `qTotCap`.

    Prices are RIAL, matching what the warehouse already stores for TSE rows --
    so no conversion happens here, consistent with the rule that the warehouse
    is a verbatim copy of the origin and `holding_value_to_toman` is the single
    place the division occurs.
    """
    payload = fetch_endpoint("daily_history", ins_code)
    rows = payload.get("closingPriceDailyList") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceResponseError(
            f"TSETMC daily history for {ins_code} had no closingPriceDailyList.",
            origin=ORIGIN,
        )
    return rows


def fetch_best_limits(ins_code):
    """Order-book depth: the thing BrsApi cannot sell us historically.

    Expected `{"bestLimits": [{"number", "qTitMeDem", "zOrdMeDem", "pMeDem",
    "pMeOf", "zOrdMeOf", "qTitMeOf"}, ...]}` -- five bid and five ask levels
    with queue counts and volumes at each.
    """
    payload = fetch_endpoint("best_limits", ins_code)
    rows = payload.get("bestLimits") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceResponseError(
            f"TSETMC best limits for {ins_code} had no bestLimits.", origin=ORIGIN
        )
    return rows


def fetch_intraday_trades(ins_code):
    """Tick-by-tick trades for the current session.

    Expected `{"tradeTop": [{"nTran", "hEven", "qTitTran", "pTran"}, ...]}`,
    where `hEven` is HHMMSS as an int.
    """
    payload = fetch_endpoint("trade_last", ins_code)
    rows = payload.get("tradeTop") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceResponseError(
            f"TSETMC trades for {ins_code} had no tradeTop.", origin=ORIGIN
        )
    return rows
