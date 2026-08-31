"""Nobitex: the independent second opinion on live crypto prices.

Deliberately NOT the history source -- `/market/udf/history` capped BTCIRT at
466 daily candles where Wallex returned 2,820 in one request. Its job here is
disagreement detection: two exchanges that quote the same coin should agree to
within a spread, and when they do not, one of them is broken and we would
rather find out from a check than from a user's portfolio.

**The hostname is the trap.** The published documentation says
`api.nobitex.ir`; that name is NXDOMAIN. The API answers on `apiv2.nobitex.ir`.
An earlier survey of this box recorded Nobitex as network-blocked on that
basis, which was wrong -- it is reachable in about a second.

**Unit: RIAL.** Pairs are `<src>-<dst>` and the dst is `rls`, against Wallex's
Toman. The same coin therefore quotes 10x apart between the two sources, which
is exactly the kind of cross-source unit mismatch that produces a plausible
wrong number rather than an obvious one. `PAIR_UNITS` makes the distinction
explicit and `cross_check` normalises before comparing.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .http import SourceResponseError, fetch

logger = logging.getLogger(__name__)

ORIGIN = "nobitex"

#: Destination currency -> declared unit. `rls` is Rial; the exchange also
#: quotes some pairs in tether.
PAIR_UNITS = {"rls": "ریال", "usdt": "تتر"}

#: `srcCurrency=all` is accepted and returns zero pairs, so the source list has
#: to be explicit. These are the coins the app actually prices plus the two
#: gold tokens, which give an independent read on the gold price.
DEFAULT_SOURCES = ("btc", "usdt", "eth", "xrp", "ada", "doge", "ltc", "bnb",
                   "sol", "trx", "dot", "pmn", "paxg")


def _dec(raw):
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def fetch_stats(sources=None, destination="rls"):
    """Live stats for the given coins. One request covers all of them."""
    payload = fetch(
        f"{settings.NOBITEX_BASE_URL}/market/stats",
        origin=ORIGIN,
        params={
            "srcCurrency": ",".join(sources or DEFAULT_SOURCES),
            "dstCurrency": destination,
        },
        timeout=(5, 20),
    )
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise SourceResponseError("Nobitex stats call was unsuccessful.", origin=ORIGIN)
    stats = payload.get("stats")
    if not isinstance(stats, dict) or not stats:
        raise SourceResponseError("Nobitex returned no stats.", origin=ORIGIN)
    return stats


def live_rows(stats=None):
    """Stats reshaped to `{symbol, price, unit}` rows.

    Skips `isClosed` pairs. A closed market's `latest` is the last price before
    it shut, which is a real number describing a market that is not trading --
    forwarding it as live is how a stale quote enters a portfolio wearing a
    fresh timestamp.
    """
    stats = fetch_stats() if stats is None else stats
    rows = []
    for pair, entry in stats.items():
        if not isinstance(entry, dict) or entry.get("isClosed"):
            continue
        src, _, dst = pair.partition("-")
        unit = PAIR_UNITS.get(dst)
        if unit is None:
            continue
        price = _dec(entry.get("latest")) or _dec(entry.get("mark"))
        if price is None or price <= 0:
            continue
        rows.append({
            "symbol": src.upper(),
            "pair": pair,
            "price": str(price),
            "unit": unit,
            "day_high": str(_dec(entry.get("dayHigh")) or ""),
            "day_low": str(_dec(entry.get("dayLow")) or ""),
            "day_change_pct": entry.get("dayChange"),
            "source": ORIGIN,
        })
    return rows


def to_toman(price, unit):
    """Normalise one quote to Toman so two sources can be compared at all."""
    value = _dec(price)
    if value is None:
        return None
    if unit == "ریال":
        return value / Decimal("10")
    return value


def cross_check(nobitex_rows, wallex_rows, *, tolerance=Decimal("0.02")):
    """Compare the two exchanges coin by coin, in Toman.

    Returns `(agreements, disagreements)`, where a disagreement is a coin both
    exchanges quote whose Toman prices differ by more than `tolerance`. The
    default 2% is wide enough to absorb genuine spread between two venues and
    narrow enough that a unit error (10x) or a frozen feed cannot hide in it.

    This is the check that makes a single-source migration safe: it is what
    would have caught TGJU's dead `usdt-irr` slug automatically instead of by
    inspection.
    """
    wallex_by_coin = {}
    for row in wallex_rows:
        # Only the Toman book is comparable; the USDT book prices coins in
        # tether, which is a different question.
        if row.get("quote") != "TMN":
            continue
        base = str(row.get("base") or "").upper()
        if base:
            wallex_by_coin[base] = row

    agreements, disagreements = [], []
    for row in nobitex_rows:
        coin = str(row.get("symbol") or "").upper()
        peer = wallex_by_coin.get(coin)
        if peer is None:
            continue
        ours = to_toman(row["price"], row["unit"])
        theirs = to_toman(peer["price"], peer["unit"])
        if not ours or not theirs or ours <= 0 or theirs <= 0:
            continue
        spread = abs(ours - theirs) / max(ours, theirs)
        record = {"coin": coin, "nobitex_toman": ours, "wallex_toman": theirs,
                  "spread": spread}
        (disagreements if spread > tolerance else agreements).append(record)
    return agreements, disagreements
