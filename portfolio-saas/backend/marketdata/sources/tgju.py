"""TGJU: gold, coin, FX and commodity quotes -- the origin BrsApi resells.

This is not a substitute feed, it is the SAME feed. Compared live on
2026-08-31 against what production was serving from
`Market/Gold_Currency.php`, five of eight assets matched to the rial:

    emami_coin    223,510,000  ==  sekee/10            exact
    half_coin     114,000,000  ==  nim/10              exact
    quarter_coin   61,500,000  ==  rob/10              exact
    one_gram_coin  32,000,000  ==  gerami/10           exact
    euro_cash         243,980  ==  price_eur/10        exact
    usd_cash          209,300  vs  price_dollar_rl/10  -0.002%
    gold_18k_gram  22,220,100  vs  geram18/10          -0.16%  (later tick)
    usdt_irt          209,053  vs  crypto-tether-irr/10 +0.28%

The three inexact rows differ by one refresh interval, not by content. So
moving here costs no accuracy and removes the entire ~1,500/day BRS wallet from
the critical path.

Two things about this payload are dangerous enough to encode rather than
document:

**It declares no unit.** Every other provider we use sends a unit string, and
`marketdata.currency.to_toman` is built to trust it -- the project rule is that
currency is declared, never inferred from magnitude. TGJU breaks that, so
`SLUG_UNITS` below is a hand-verified map and `to_toman` is still handed an
explicit unit. Guessing from magnitude would work today and break the first
time the rial redenominates or a slug is repriced.

**It serves dead slugs alongside live ones.** `usdt-irr` still returns a
number, and that number is 273,000 from 2020-11-11 -- picking it by name (the
obvious choice for tether) would have silently valued every USDT holding at a
sixth of its worth. The live tether is `crypto-tether-irr`. Nothing in the
payload marks one as retired, so `MAX_QUOTE_AGE` is not optional hygiene here;
it is the only thing standing between us and a six-year-old price.
"""
import logging
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings

from .http import SourceResponseError, fetch

logger = logging.getLogger(__name__)

ORIGIN = "tgju"
TEHRAN = ZoneInfo("Asia/Tehran")

#: Quotes older than this are refused. TGJU keeps retired instruments in the
#: payload indefinitely (see the module docstring), and the coin slugs legitimately
#: go quiet for hours once the physical market shuts, so this has to be generous
#: enough to survive a normal overnight gap and tight enough to catch a feed
#: that died years ago. Three days clears the Thu/Fri weekend with room to spare.
MAX_QUOTE_AGE = timedelta(days=3)

#: Unit declared per slug, because the payload declares none. Verified against
#: the production comparison in the module docstring: every IRR-denominated TGJU
#: instrument quotes RIAL (the `_rl` suffix on the dollar slug says so outright,
#: and the coin/gold slugs divide by ten onto the exact numbers BrsApi reports
#: as Toman). `ons` and `crypto-bitcoin` are dollar-quoted global prices.
RIAL = "ریال"
DOLLAR = "دلار"

SLUG_UNITS = {
    "sekee": RIAL,
    "nim": RIAL,
    "rob": RIAL,
    "gerami": RIAL,
    "geram18": RIAL,
    "geram24": RIAL,
    "mesghal": RIAL,
    "silver_999": RIAL,
    "price_dollar_rl": RIAL,
    "price_eur": RIAL,
    "crypto-tether-irr": RIAL,
    "ons": DOLLAR,
    "crypto-bitcoin": DOLLAR,
}

#: BrsApi symbol -> TGJU slug. Keyed on the BRS symbol deliberately: `Asset.brs_symbol`
#: already carries it, the extractor already looks assets up by it, and the seed
#: catalog is written in it. Mapping here instead of renaming the catalog means
#: the source swap touches no user-visible identity and is reversible by config.
BRS_TO_SLUG = {
    "IR_COIN_EMAMI": "sekee",
    "IR_COIN_HALF": "nim",
    "IR_COIN_QUARTER": "rob",
    "IR_COIN_1G": "gerami",
    "IR_GOLD_18K": "geram18",
    "USD": "price_dollar_rl",
    "EUR": "price_eur",
    "USDT_IRT": "crypto-tether-irr",
    "XAUUSD": "ons",
    "BTC": "crypto-bitcoin",
}

#: Daily-history slugs, for the archive lane. Measured row counts and earliest
#: dates, 2026-08-31: price_dollar_rl 3,938 rows to 1390/09/05 (2011-11-26);
#: sekee and geram18 reach back comparably far. BrsApi's own gold history tops
#: out around 4,792 rows but costs a metered request; these cost nothing.
HISTORY_SLUGS = ("price_dollar_rl", "price_eur", "sekee", "nim", "rob", "gerami",
                 "geram18", "ons")


def _to_decimal(raw):
    """TGJU prints prices as display strings: '2,092,950' and '4,425.19'."""
    if raw in (None, ""):
        return None
    text = str(raw).replace(",", "").replace("٬", "").strip()
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _parse_ts(raw):
    """`ts` is Tehran-local 'YYYY-MM-DD HH:MM:SS' with no offset."""
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=TEHRAN
        )
    except (ValueError, TypeError):
        return None


def fetch_live():
    """The whole board: 962 instruments in one ~180KB request.

    One request for every gold, coin, FX and commodity price the app needs. The
    BrsApi equivalent was two metered calls (`Gold_Currency.php` plus a separate
    USDT history call) against a 1,500/day wallet.
    """
    payload = fetch(settings.TGJU_LIVE_URL, origin=ORIGIN)
    if not isinstance(payload, dict):
        raise SourceResponseError("TGJU live payload was not an object.", origin=ORIGIN)
    current = payload.get("current")
    if not isinstance(current, dict) or not current:
        raise SourceResponseError(
            "TGJU live payload carried no `current` block.", origin=ORIGIN
        )
    return current


def quote(current, slug, *, now=None):
    """One slug as `(Decimal price, unit)`, or `(None, None)` if unusable.

    Refuses a quote whose own timestamp is older than `MAX_QUOTE_AGE` rather
    than returning it with a caveat. A caller that receives a number will price
    a holding with it; there is no "probably fine" tier for money.
    """
    row = current.get(slug)
    if not isinstance(row, dict):
        return None, None
    price = _to_decimal(row.get("p"))
    if price is None or price <= 0:
        return None, None

    stamped = _parse_ts(row.get("ts"))
    if stamped is None:
        logger.warning("tgju_quote_undated slug=%s; refusing.", slug)
        return None, None
    age = (now or datetime.now(TEHRAN)) - stamped
    if age > MAX_QUOTE_AGE:
        logger.warning(
            "tgju_quote_stale slug=%s age_days=%.1f ts=%s; refusing.",
            slug, age.total_seconds() / 86400, row.get("ts"),
        )
        return None, None

    unit = SLUG_UNITS.get(slug)
    if unit is None:
        # An unmapped slug has no declared unit, and inferring one from the
        # number is the exact mistake this module exists to avoid.
        logger.warning("tgju_quote_unmapped slug=%s; no declared unit.", slug)
        return None, None
    return price, unit


def live_rows(current=None, *, now=None):
    """The board reshaped into BrsApi's row shape: `{symbol, price, unit}`.

    Emitting the incumbent's shape is what lets `portfolio.live.extractor`
    consume this with no changes -- its `_build_lookup`/`to_toman` path already
    handles a declared Rial unit correctly. The alternative, teaching the
    extractor a second row format, would have duplicated the unit logic that
    has historically been the most expensive thing in this codebase to get
    wrong.
    """
    current = fetch_live() if current is None else current
    rows = []
    for brs_symbol, slug in BRS_TO_SLUG.items():
        price, unit = quote(current, slug, now=now)
        if price is None:
            continue
        rows.append({"symbol": brs_symbol, "price": str(price), "unit": unit,
                     "source": ORIGIN, "slug": slug})
    return rows


def fetch_daily_history(slug):
    """Full daily OHLC for one slug, newest-first, as the origin returns it.

    Columns are positional and undocumented; verified against the dollar series
    on 2026-08-31 (`[open, low, high, close, change_html, pct_html, gregorian,
    jalali]`) -- note LOW precedes HIGH, which is the reverse of every other
    feed here and would silently invert any range calculation.
    """
    url = f"{settings.TGJU_HISTORY_URL.rstrip('/')}/{slug}"
    payload = fetch(url, origin=ORIGIN, timeout=(5, 30))
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceResponseError(
            f"TGJU history for {slug} carried no `data` array.", origin=ORIGIN
        )
    return rows


def parse_history_row(row):
    """One raw history row -> `{jalali, gregorian, open, low, high, close}`.

    Returns None for a row that cannot be read rather than raising: these
    series are thousands of rows deep and a single malformed historical entry
    must not abort a backfill that is otherwise sound.
    """
    if not isinstance(row, list) or len(row) < 8:
        return None
    o, l, h, c = (_to_decimal(row[i]) for i in range(4))
    if None in (o, l, h, c):
        return None
    jalali = str(row[7]).strip().replace("/", "-")
    gregorian = str(row[6]).strip().replace("/", "-")
    if not jalali:
        return None
    return {"jalali": jalali, "gregorian": gregorian,
            "open": o, "low": l, "high": h, "close": c}
