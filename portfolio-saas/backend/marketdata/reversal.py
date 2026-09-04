"""Which symbol-days are worth spending a tick request on.

The intraday tick endpoint costs one request per symbol-day and the universe is
~2.1M symbol-days, so a blanket backfill is a ~210-day crawl against the whole
10,000/day meter. Until 2026-09-04 that is what ran: `claim_archive_batch`
ordered tick states by starvation ("least covered first"), which is effectively
random with respect to any downstream use. It took 67% of all provider requests
for seven days and produced 96,602 symbol-days, of which only 3,172 were days
anything would want to look at.

The downstream use is a supervised model over the TSE's daily price band. The
exchange caps a symbol's daily move (nominally +/-3%, revised over the years) and
there is no short side, so the tradeable setup is one-directional: a stock dips
toward the bottom of its band and recovers to close near the top. The decision
point is the dip -- that is when you would buy -- so the question the model has
to answer is "given this symbol just fell 2%, does it close up 2%?".

That framing decides everything here:

* **Labels are free.** They come from daily OHLC we already hold (4.4M candles,
  all 1,969 symbols, current to the last session). No request buys a label.
* **The scarce thing is intraday features on the days that matter.** Only the
  trigger days -- the ones that actually dipped -- are decision points. The other
  94% of symbol-days never present the setup and teach the model nothing about it.
* **The negatives must share the trigger.** A day picked at random is not a
  counter-example to "dipped and recovered"; it is a different question entirely.
  The useful negative is a day that dipped exactly the same and did *not* recover.

Measured on the top 300 symbols by turnover: 132,725 trigger days, 8,020 of them
positive (a 6.0% base rate) and 124,705 negative. Taking every positive and
sampling negatives at `MARKETDATA_REVERSAL_NEGATIVE_RATIO`:1 is ~24,000
symbol-days -- about two days of the meter, against 210 for the blanket crawl.

Prices here are read from the **unadjusted** series on purpose. The band is
enforced by the exchange against the actual traded price; an adjusted series
rescales history across splits and dividends, so a corporate action would read as
a limit move that never happened.
"""
import hashlib
import logging

from django.conf import settings
from django.db import connection

from .models import MarketCandle

logger = logging.getLogger(__name__)

#: How far the low must fall below the previous close to count as a trigger.
DEFAULT_DIP = 0.02
#: How far the close must finish above the previous close to count as a recovery.
DEFAULT_RECOVERY = 0.02

POSITIVE = "positive"
NEGATIVE = "negative"


def _threshold(name, fallback):
    return float(getattr(settings, name, fallback) or fallback)


def liquid_symbols(limit=None):
    """The most tradeable symbols, ranked by median daily turnover.

    Deliberately NOT `StockSymbolMetadata.market_cap`, which is what
    `archive.get_deep_tier_symbols` uses: that column is populated by the weekly
    metadata sync and on 2026-09-04 held a value for **86 of 1,969 symbols**. A
    "top 300 by liquidity" built on it silently means "the 86 we happen to know",
    which is not a liquidity filter at all.

    Turnover (volume x close) comes from candles we already own, covers 1,156
    symbols, costs no request, and is a better proxy anyway -- market cap counts
    shares that never trade, and this model can only act on symbols you can
    actually get filled in.

    Median rather than mean: a single block trade should not promote an otherwise
    illiquid name into the training universe.
    """
    limit = limit or int(getattr(settings, "MARKETDATA_REVERSAL_UNIVERSE_N", 300))
    since = getattr(settings, "MARKETDATA_REVERSAL_LIQUIDITY_SINCE", "1403-01-01")
    min_sessions = int(getattr(settings, "MARKETDATA_REVERSAL_MIN_SESSIONS", 100))
    min_positives = int(getattr(settings, "MARKETDATA_REVERSAL_MIN_POSITIVES", 5))
    dip = _threshold("MARKETDATA_REVERSAL_DIP", DEFAULT_DIP)
    recovery = _threshold("MARKETDATA_REVERSAL_RECOVERY", DEFAULT_RECOVERY)
    # Liquid is necessary but not sufficient. Ranking on turnover alone put 118
    # of the top 300 into the universe with ZERO positives ever -- they are
    # fixed-income ETFs (آسان, آرامش, سپر ...), which are heavily traded and by
    # construction never move 2% in a day. Buying intraday history for a symbol
    # that cannot produce the setup is the same waste as the untargeted crawl,
    # just better disguised. Require that the pattern actually occurs.
    with connection.cursor() as cur:
        cur.execute(
            """
            WITH turnover AS (
                SELECT symbol,
                       percentile_disc(0.5) WITHIN GROUP (
                           ORDER BY volume * close_price
                       ) AS med
                FROM marketdata_marketcandle
                WHERE timeframe = '1d_unadj' AND date_time >= %s AND volume > 0
                GROUP BY symbol
                HAVING count(*) >= %s
            ), moves AS (
                SELECT symbol, low_price AS lo, close_price AS cl,
                       lag(close_price) OVER (
                           PARTITION BY symbol ORDER BY date_time
                       ) AS prev
                FROM marketdata_marketcandle
                WHERE timeframe = '1d_unadj'
            ), positives AS (
                SELECT symbol, count(*) AS n
                FROM moves
                WHERE prev > 0
                  AND (lo / prev - 1) <= %s
                  AND (cl / prev - 1) >= %s
                GROUP BY symbol
            )
            SELECT t.symbol
            FROM turnover t
            JOIN positives p ON p.symbol = t.symbol
            WHERE p.n >= %s
            ORDER BY t.med DESC
            LIMIT %s
            """,
            [since, min_sessions, -dip, recovery, min_positives, limit],
        )
        return [row[0] for row in cur.fetchall()]


def liquid_symbol_set(limit=None):
    """`liquid_symbols` as a set, cached, for per-claim membership tests.

    The scheduler asks this on every archive claim; the underlying query is a
    percentile over ~2.1M candle rows and must not run per claim. Six hours is
    far shorter than liquidity actually moves, and a stale answer only misroutes
    tick spending for one cycle -- it can never lose data.
    """
    from django.core.cache import cache

    limit = limit or int(getattr(settings, "MARKETDATA_REVERSAL_UNIVERSE_N", 300))
    key = f"marketdata:liquid_universe:{limit}"
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached
    except Exception:
        pass
    symbols = set(liquid_symbols(limit))
    try:
        cache.set(key, symbols, timeout=6 * 3600)
    except Exception:
        pass
    return symbols


def labelled_days(symbol):
    """`{jalali_date: POSITIVE|NEGATIVE}` for every trigger day this symbol had.

    A trigger day is one whose low fell at least `dip` below the previous close --
    the moment the strategy would buy. It is POSITIVE when the close finished at
    least `recovery` above that same previous close.

    Days with no prior close (the symbol's first bar) are skipped: without a
    reference price there is no band to measure against.
    """
    dip = _threshold("MARKETDATA_REVERSAL_DIP", DEFAULT_DIP)
    recovery = _threshold("MARKETDATA_REVERSAL_RECOVERY", DEFAULT_RECOVERY)
    rows = list(
        MarketCandle.objects.filter(symbol=symbol, timeframe="1d_unadj")
        .order_by("date_time")
        .values_list("date_time", "low_price", "close_price")
    )
    labels = {}
    previous_close = None
    for date_time, low, close in rows:
        if previous_close and previous_close > 0 and low is not None and close is not None:
            if (low / previous_close - 1) <= -dip:
                recovered = (close / previous_close - 1) >= recovery
                labels[date_time] = POSITIVE if recovered else NEGATIVE
        if close and close > 0:
            previous_close = close
    return labels


def _sampled(symbol, date, ratio):
    """Deterministically keep roughly 1 in `ratio` negatives.

    Hash-based rather than random so the selection is stable across processes and
    across restarts: the archive re-derives this on every tick claim, and a set
    that reshuffled each time would leave every negative permanently half-fetched.
    """
    if ratio <= 1:
        return True
    digest = hashlib.blake2b(f"{symbol}|{date}".encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big") % ratio == 0


def priority_tick_days(symbol):
    """Trigger days worth a tick request, most valuable first.

    Every POSITIVE is taken: at a 6% base rate they are the scarce class, and
    dropping one costs far more than the request saves. NEGATIVEs are sampled
    down to `MARKETDATA_REVERSAL_NEGATIVE_RATIO`:1 -- there are 15x more of them
    than positives, and buying all of them would spend ten days of meter to make
    the training set more imbalanced, not more informative.

    Newest first inside each class. Recent sessions sit under the current band
    rule and the current liquidity regime, so they are the observations most
    likely to transfer.
    """
    ratio = int(getattr(settings, "MARKETDATA_REVERSAL_NEGATIVE_RATIO", 2) or 1)
    labels = labelled_days(symbol)
    positives = sorted(
        (day for day, label in labels.items() if label == POSITIVE), reverse=True
    )
    negatives = sorted(
        (
            day for day, label in labels.items()
            if label == NEGATIVE and _sampled(symbol, day, ratio)
        ),
        reverse=True,
    )
    return positives + negatives
