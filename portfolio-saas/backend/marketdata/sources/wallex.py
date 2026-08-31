"""Wallex: the crypto HISTORY source, and the deepest one reachable.

Why this is the history source and Nobitex is not, measured 2026-08-31:

    Wallex   /v1/udf/history  BTCTMN   resolution=D    2,820 candles, one request
                                                       2018-11-27 -> today
                              USDTTMN  resolution=60  24,488 candles, one request
    Nobitex  /market/udf/history BTCIRT resolution=D     466 candles (~500 cap)

That is the difference between "the whole series arrives in one call" and
"page it 40 times". It also ends, for crypto, the 30/90-day archive ceiling
this warehouse has been working around: there is no ceiling to work around.

**Unit: TMN, declared.** `quoteAsset` is an explicit field, and Wallex splits
193 TMN pairs from 192 USDT pairs, so the Toman/Tether distinction is read off
the payload rather than guessed from magnitude. This matters more than usual
here -- Nobitex quotes the same coins in RIAL, so a source swap that lost track
of the unit would be wrong by exactly 10x, which is both the most likely error
in this codebase and the hardest to spot on a chart.

**Pacing.** 60 requests in 17.1s drew no throttling and no rate-limit headers.
An origin that will not tell us to stop is one we have to stop ourselves; see
`DIRECT_SOURCE_MIN_INTERVAL`.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .http import SourceResponseError, fetch

logger = logging.getLogger(__name__)

ORIGIN = "wallex"

#: Resolutions the origin actually honours. `5`, `180`, `W` and `M` error out
#: despite looking plausible, so this is a whitelist rather than a passthrough:
#: an unsupported value returns an error body, and an error body parsed as
#: candles is an empty ingest recorded as a quiet day.
SUPPORTED_RESOLUTIONS = frozenset({"1", "15", "60", "240", "D"})

#: Quote assets, as the origin declares them, mapped to the unit vocabulary
#: `marketdata.currency` already understands.
QUOTE_UNITS = {"TMN": "تومان", "USDT": "تتر"}


def _dec(raw):
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def fetch_markets():
    """All 385 symbols with live stats, in one request.

    Returns the `result.symbols` mapping. One call covers every crypto quote
    the app needs plus the USDT/TMN rate that every dollar-denominated holding
    is multiplied by.
    """
    payload = fetch(f"{settings.WALLEX_BASE_URL}/v1/markets", origin=ORIGIN,
                    timeout=(5, 25))
    if not isinstance(payload, dict) or not payload.get("success", True):
        raise SourceResponseError("Wallex markets call was unsuccessful.", origin=ORIGIN)
    symbols = (payload.get("result") or {}).get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise SourceResponseError("Wallex returned no symbols.", origin=ORIGIN)
    return symbols


def live_rows(symbols=None):
    """Market stats reshaped to `{symbol, price, unit}` rows.

    `lastPrice` is preferred over the bid/ask midpoint: it is a price something
    actually traded at, whereas a midpoint on a thin book is a number no one
    ever paid. Falls back to the midpoint only when there is no last trade.
    """
    symbols = fetch_markets() if symbols is None else symbols
    rows = []
    for name, entry in symbols.items():
        if not isinstance(entry, dict):
            continue
        unit = QUOTE_UNITS.get(entry.get("quoteAsset"))
        if unit is None:
            # An undeclared quote asset is exactly the case where guessing is
            # a 10x error waiting to happen. Skip it.
            continue
        stats = entry.get("stats") or {}
        price = _dec(stats.get("lastPrice"))
        if price is None or price <= 0:
            bid, ask = _dec(stats.get("bidPrice")), _dec(stats.get("askPrice"))
            if bid and ask and bid > 0 and ask > 0:
                price = (bid + ask) / 2
        if price is None or price <= 0:
            continue
        rows.append({
            "symbol": name,
            "base": entry.get("baseAsset"),
            "quote": entry.get("quoteAsset"),
            "name_fa": entry.get("faBaseAsset") or "",
            "name_en": entry.get("enBaseAsset") or "",
            "price": str(price),
            "unit": unit,
            "day_high": str(_dec(stats.get("24h_highPrice")) or ""),
            "day_low": str(_dec(stats.get("24h_lowPrice")) or ""),
            "day_change_pct": stats.get("24h_ch"),
            "source": ORIGIN,
        })
    return rows


def fetch_ohlc(symbol, *, resolution="D", start=None, end=None):
    """TradingView-UDF candles for one symbol.

    The response is COLUMN-major (`{s, t, o, h, l, c, v}` as parallel arrays),
    not a list of candle objects -- transposing it is `zip`, and forgetting to
    is a silent misalignment rather than an error, so `candles()` below is the
    only supported way to read it.

    `start`/`end` are epoch seconds. Defaulting `start` to 0 is deliberate: the
    origin honours it and returns the entire series, which is the cheapest
    possible backfill.
    """
    resolution = str(resolution)
    if resolution not in SUPPORTED_RESOLUTIONS:
        raise ValueError(
            f"Wallex rejects resolution {resolution!r}; "
            f"supported: {sorted(SUPPORTED_RESOLUTIONS)}."
        )
    payload = fetch(
        f"{settings.WALLEX_BASE_URL}/v1/udf/history",
        origin=ORIGIN,
        params={
            "symbol": symbol,
            "resolution": resolution,
            "from": int(start or 0),
            "to": int(end or 2_000_000_000),
        },
        timeout=(5, 45),
    )
    if not isinstance(payload, dict):
        raise SourceResponseError(f"Wallex history for {symbol} was not an object.",
                                  origin=ORIGIN)
    status = payload.get("s")
    if status == "no_data":
        return {"s": "no_data", "t": []}
    if status != "ok":
        raise SourceResponseError(
            f"Wallex history for {symbol} returned s={status!r}.", origin=ORIGIN
        )
    return payload


def candles(payload):
    """Column-major UDF payload -> row-major dicts, newest last.

    Rows whose OHLC cannot be read are dropped rather than raising: these
    series run to tens of thousands of candles and one bad entry must not
    discard a backfill that is otherwise complete.
    """
    if not isinstance(payload, dict) or payload.get("s") != "ok":
        return []
    times = payload.get("t") or []
    columns = [payload.get(k) or [] for k in ("o", "h", "l", "c", "v")]
    # A short column would zip silently against the wrong timestamps, mislabelling
    # every candle after the truncation point. Refuse instead.
    if any(len(col) != len(times) for col in columns):
        raise SourceResponseError(
            "Wallex history columns disagree in length; refusing to transpose.",
            origin=ORIGIN,
        )
    out = []
    for ts, o, h, low, c, v in zip(times, *columns):
        values = [_dec(o), _dec(h), _dec(low), _dec(c)]
        if any(x is None or x <= 0 for x in values):
            continue
        out.append({
            "ts": int(ts),
            "open": values[0], "high": values[1],
            "low": values[2], "close": values[3],
            "volume": _dec(v) or Decimal("0"),
        })
    return out
