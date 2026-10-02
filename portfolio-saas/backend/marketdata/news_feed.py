"""Curated price series for the News Intelligence app (catalog, series, snapshot).

A read-through layer, not a warehouse. Every series here is a public daily
close that its origin already keeps in full (TGJU back to 1979 for gold, FRED
to 1976 for the 2Y yield), so storing a copy would spend the shared disk on a
mirror of data that is one request away. Only the compact parsed result
`[(date, close)]` is cached, in Redis, for minutes to hours.

Contract keys are ours, not the provider's: the news app stores them next to
its own analyses, and a TGJU slug rename must not break that history.

Units are declared per entry, never inferred -- the same rule as
`sources.tgju.SLUG_UNITS`. Rial series are returned verbatim in rial;
converting is the reader's job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .sources import tgju
from .sources.http import SourceError, fetch

UTC = ZoneInfo("UTC")
# home.treasury.gov drops connections from the Tehran VPS; FRED republishes the
# same constant-maturity yield (DGS2) and is reachable (probed 2026-10-02).
FRED_GRAPH_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
ECB_FX_90D_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"

GROUPS = {
    "iran_fx_gold": ("ارز و طلای ایران", "Iran FX & gold"),
    "global_commodities": ("کالاهای جهانی", "Global commodities"),
    "global_fx_indices": ("ارز و شاخص‌های جهانی", "Global FX & indices"),
    "iran_markets_crypto": ("بازار سرمایه ایران و رمزارز", "Iran markets & crypto"),
}

#: When a market prints. `days` are Python weekdays (Mon=0). A quote read
#: outside these hours is labelled `market_closed`, not treated as stale.
HOURS = {
    "fx_24x5": {"label": "24/5", "tz": "America/New_York", "days": [0, 1, 2, 3, 4],
                "open": "00:00", "close": "23:59"},
    "commodity_24x5": {"label": "Futures, ~23/5", "tz": "America/New_York",
                       "days": [0, 1, 2, 3, 4], "open": "00:00", "close": "17:00"},
    "us_equity": {"label": "NYSE/Nasdaq", "tz": "America/New_York",
                  "days": [0, 1, 2, 3, 4], "open": "09:30", "close": "16:00"},
    "eu_equity": {"label": "European cash session", "tz": "Europe/Berlin",
                  "days": [0, 1, 2, 3, 4], "open": "09:00", "close": "17:30"},
    "jp_equity": {"label": "Tokyo Stock Exchange", "tz": "Asia/Tokyo",
                  "days": [0, 1, 2, 3, 4], "open": "09:00", "close": "15:30"},
    "us_rates": {"label": "US business days (daily fixing)", "tz": "America/New_York",
                 "days": [0, 1, 2, 3, 4], "open": "00:00", "close": "23:59"},
    "ecb_fixing": {"label": "ECB business days, fixed 14:15 CET", "tz": "Europe/Berlin",
                   "days": [0, 1, 2, 3, 4], "open": "00:00", "close": "23:59"},
    "tehran_exchange": {"label": "TSE, Sat-Wed", "tz": "Asia/Tehran",
                        "days": [5, 6, 0, 1, 2], "open": "09:00", "close": "12:30"},
    "tehran_open_market": {"label": "Tehran open market, Sat-Thu", "tz": "Asia/Tehran",
                           "days": [5, 6, 0, 1, 2, 3], "open": "10:00", "close": "19:00"},
    "crypto_24x7": {"label": "24/7", "tz": "UTC", "days": [0, 1, 2, 3, 4, 5, 6],
                    "open": "00:00", "close": "23:59"},
}


@dataclass(frozen=True)
class Instrument:
    key: str
    group: str
    asset_class: str
    name_fa: str
    name_en: str
    unit: str
    currency: str
    hours: str
    provider: str  # tgju | fred | ecb
    source_id: str
    cadence: str = "intraday"


def _i(*args, **kwargs):
    return Instrument(*args, **kwargs)


CATALOG = {item.key: item for item in (
    # Iran FX & gold -- TGJU quotes all of these in rial.
    _i("usd_irr", "iran_fx_gold", "fx", "دلار آزاد", "US dollar (Tehran open market)", "IRR", "IRR", "tehran_open_market", "tgju", "price_dollar_rl"),
    _i("eur_irr", "iran_fx_gold", "fx", "یورو آزاد", "Euro (Tehran open market)", "IRR", "IRR", "tehran_open_market", "tgju", "price_eur"),
    _i("usdt_irr", "iran_fx_gold", "fx", "تتر", "Tether (USDT/IRR)", "IRR", "IRR", "crypto_24x7", "tgju", "crypto-tether-irr"),
    _i("emami_coin", "iran_fx_gold", "gold", "سکه امامی", "Emami gold coin", "IRR", "IRR", "tehran_open_market", "tgju", "sekee"),
    _i("gold_18k", "iran_fx_gold", "gold", "طلای ۱۸ عیار (گرم)", "18k gold (gram)", "IRR", "IRR", "tehran_open_market", "tgju", "geram18"),
    _i("mesghal", "iran_fx_gold", "gold", "مثقال طلا", "Gold mesghal", "IRR", "IRR", "tehran_open_market", "tgju", "mesghal"),
    # Global commodities -- dollar quotes.
    _i("brent", "global_commodities", "commodity", "نفت برنت", "Brent crude", "USD/bbl", "USD", "commodity_24x5", "tgju", "oil_brent"),
    _i("wti", "global_commodities", "commodity", "نفت WTI", "WTI crude", "USD/bbl", "USD", "commodity_24x5", "tgju", "oil"),
    _i("opec_basket", "global_commodities", "commodity", "سبد نفتی اوپک", "OPEC basket", "USD/bbl", "USD", "commodity_24x5", "tgju", "oil_opec"),
    _i("gold_ounce", "global_commodities", "commodity", "انس طلا", "Gold (ounce)", "USD/oz", "USD", "commodity_24x5", "tgju", "ons"),
    _i("silver_ounce", "global_commodities", "commodity", "انس نقره", "Silver (ounce)", "USD/oz", "USD", "commodity_24x5", "tgju", "silver"),
    _i("platinum_ounce", "global_commodities", "commodity", "انس پلاتین", "Platinum (ounce)", "USD/oz", "USD", "commodity_24x5", "tgju", "platinum"),
    _i("natural_gas", "global_commodities", "commodity", "گاز طبیعی", "Natural gas", "USD/MMBtu", "USD", "commodity_24x5", "tgju", "energy_natural_gas"),
    _i("copper", "global_commodities", "commodity", "مس", "Copper", "USD/t", "USD", "commodity_24x5", "tgju", "copper"),
    _i("copper_global", "global_commodities", "commodity", "مس (بازار جهانی)", "Copper (global benchmark)", "USD/t", "USD", "commodity_24x5", "tgju", "base_global_copper"),
    # Global FX, indices and rates.
    _i("eur_usd", "global_fx_indices", "fx", "یورو به دلار", "EUR/USD", "USD per EUR", "USD", "fx_24x5", "tgju", "eur-usd-ask"),
    _i("ecb_eur_usd", "global_fx_indices", "fx", "نرخ مرجع یورو/دلار (ECB)", "EUR/USD ECB reference", "USD per EUR", "USD", "ecb_fixing", "ecb", "USD", "daily"),
    _i("us_treasury_2y", "global_fx_indices", "rate", "بازده اوراق ۲ ساله آمریکا", "US Treasury 2Y yield", "percent", "USD", "us_rates", "fred", "DGS2", "daily"),
    _i("nasdaq", "global_fx_indices", "index", "نزدک", "Nasdaq Composite", "index points", "USD", "us_equity", "tgju", "nasdaq_us"),
    _i("dow_global", "global_fx_indices", "index", "داوجونز جهانی", "Dow Jones Global", "index points", "USD", "us_equity", "tgju", "bourse_globaldow"),
    _i("nikkei_225", "global_fx_indices", "index", "نیکی ۲۲۵", "Nikkei 225", "index points", "JPY", "jp_equity", "tgju", "bourse_nikkei-225"),
    _i("stoxx_600", "global_fx_indices", "index", "استاکس ۶۰۰", "STOXX Europe 600", "index points", "EUR", "eu_equity", "tgju", "bourse_stoxx-600"),
    # Iran markets & crypto.
    _i("tedpix", "iran_markets_crypto", "index", "شاخص کل بورس", "TEDPIX", "index points", "IRR", "tehran_exchange", "tgju", "bourse"),
    _i("btc_usd", "iran_markets_crypto", "crypto", "بیت‌کوین", "Bitcoin", "USD", "USD", "crypto_24x7", "tgju", "crypto-bitcoin"),
    _i("eth_usd", "iran_markets_crypto", "crypto", "اتریوم", "Ethereum", "USD", "USD", "crypto_24x7", "tgju", "crypto-ethereum"),
)}

#: How old the newest point may be, relative to the window's end, before the
#: series is called stale. Generous enough for a long weekend plus a holiday.
STALE_AFTER = timedelta(days=5)
LIVE_TTL = 60
HISTORY_TTL = 60 * 60
#: TGJU's history endpoint takes a DataTables `length`; asking for rows we do
#: not need cost 1.3 MB per request for EUR/USD. Buckets keep the cache small.
ROW_BUCKETS = (64, 400, 1500, 4000)


class FeedUnavailable(Exception):
    """The origin could not be read; the caller reports it, never zero-fills."""


def catalog_rows():
    rows = []
    for item in CATALOG.values():
        hours = HOURS[item.hours]
        rows.append({
            "key": item.key, "group": item.group, "asset_class": item.asset_class,
            "name_fa": item.name_fa, "name_en": item.name_en, "unit": item.unit,
            "currency": item.currency, "cadence": item.cadence,
            "market_hours": hours, "provider": item.provider,
        })
    return rows


def market_open(item, now=None):
    hours = HOURS[item.hours]
    local = (now or timezone.now()).astimezone(ZoneInfo(hours["tz"]))
    if local.weekday() not in hours["days"]:
        return False
    opens = time.fromisoformat(hours["open"])
    closes = time.fromisoformat(hours["close"])
    return opens <= local.time().replace(tzinfo=None) <= closes


def _observed_at(day: date, item) -> datetime:
    # A daily close belongs at the END of its market day: anchoring it at the
    # start would put the close before the news that moved it.
    tz = ZoneInfo("Asia/Tehran") if item.provider == "tgju" else UTC
    return datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)


def _rows_for(days: int) -> int:
    for bucket in ROW_BUCKETS:
        if days + 10 <= bucket:
            return bucket
    return ROW_BUCKETS[-1]


def _tgju_history(slug: str, rows: int):
    url = f"{settings.TGJU_HISTORY_URL.rstrip('/')}/{slug}"
    payload = fetch(url, origin=tgju.ORIGIN, params={"length": rows, "start": 0},
                    timeout=(5, 10), retries=0)
    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raise FeedUnavailable(f"tgju history for {slug} carried no data")
    points = []
    for row in raw:
        parsed = tgju.parse_history_row(row)
        if parsed is None or parsed["close"] <= 0:
            continue
        try:
            day = date.fromisoformat(parsed["gregorian"])
        except ValueError:
            continue
        points.append((day, parsed["close"]))
    return points


def _fred_history(series_id: str, since: date):
    text = fetch(getattr(settings, "FRED_GRAPH_CSV_URL", FRED_GRAPH_CSV_URL), origin="fred",
                 params={"id": series_id, "cosd": since.isoformat()},
                 # FRED's edge stalls (no response, 30 s+) for the browser UA that
                 # `http.DEFAULT_HEADERS` sends AND for unknown UAs, yet answers
                 # requests' own default UA in <1 s (measured 2026-10-02).
                 headers={"User-Agent": requests.utils.default_user_agent(), "Accept": "*/*"},
                 timeout=(5, 10), retries=0, expect_json=False)
    points = []
    for line in text.splitlines()[1:]:
        day, _, value = line.partition(",")
        try:
            points.append((date.fromisoformat(day.strip()), Decimal(value.strip())))
        except (ValueError, ArithmeticError):
            continue  # FRED prints "." for a holiday; that is not a zero.
    return points


_ECB_DAY = re.compile(r'<Cube time="(\d{4}-\d{2}-\d{2})">(.*?)</Cube>', re.S)


def _ecb_history(currency: str):
    text = fetch(getattr(settings, "ECB_FX_90D_URL", ECB_FX_90D_URL), origin="ecb", timeout=(5, 10),
                 retries=0, expect_json=False)
    rate = re.compile(rf'currency="{re.escape(currency)}" rate="([0-9.]+)"')
    points = []
    for day, body in _ECB_DAY.findall(text):
        match = rate.search(body)
        if match:
            points.append((date.fromisoformat(day), Decimal(match.group(1))))
    return points


def history(item, since: date):
    """Ascending `[(date, Decimal close)]` from `since`, cached and compact."""
    today = timezone.now().date()
    days = max((today - since).days, 1)
    if item.provider == "tgju":
        rows = _rows_for(days)
        key, loader = f"newsfeed:tgju:{item.source_id}:{rows}", lambda: _tgju_history(item.source_id, rows)
    elif item.provider == "fred":
        start = today - timedelta(days=_rows_for(days) * 2)
        key, loader = f"newsfeed:fred:{item.source_id}:{start}", lambda: _fred_history(item.source_id, start)
    else:
        key, loader = f"newsfeed:ecb:{item.source_id}", lambda: _ecb_history(item.source_id)
    cached = cache.get(key)
    if cached is None:
        try:
            cached = [(d.isoformat(), str(v)) for d, v in loader()]
        except SourceError as exc:
            raise FeedUnavailable(str(exc)) from exc
        cache.set(key, cached, HISTORY_TTL)
    points = sorted((date.fromisoformat(d), Decimal(v)) for d, v in cached)
    return [(d, v) for d, v in points if d >= since]


def series(item, since: date, until: date, interval: str = "1d", now=None):
    points = [(d, v) for d, v in history(item, since) if d <= until]
    caveats = []
    if interval == "1w":
        weekly = {}
        for day, value in points:
            weekly[day.isocalendar()[:2]] = (day, value)
        points = list(weekly.values())
    if not points:
        caveats.append("no_points")
    elif until - points[-1][0] > STALE_AFTER:
        caveats.append("stale_series")
    elif item.provider == "ecb" and points[0][0] - since > STALE_AFTER:
        caveats.append("history_limited_to_90_days")
    if until >= (now or timezone.now()).date() and not market_open(item, now):
        caveats.append("market_closed")
    return {
        "key": item.key, "from": since.isoformat(), "to": until.isoformat(),
        "interval": interval, "unit": item.unit, "currency": item.currency,
        "provider": item.provider.upper(), "resolution": "daily close",
        "points": [
            {"date": day.isoformat(), "observed_at": _observed_at(day, item).isoformat(),
             "price": str(value), "quality": "provider_daily_close",
             "provider": item.provider.upper(), "time_precision": "date"}
            for day, value in points
        ],
        "caveats": caveats,
    }


def _live_board():
    board = cache.get("newsfeed:tgju:board")
    if board is None:
        try:
            board = tgju.fetch_live()
        except SourceError as exc:
            raise FeedUnavailable(str(exc)) from exc
        # Keep only the slugs we serve; the full board is ~180 KB.
        slugs = {i.source_id for i in CATALOG.values() if i.provider == "tgju"}
        board = {k: v for k, v in board.items() if k in slugs}
        cache.set("newsfeed:tgju:board", board, LIVE_TTL)
    return board


def snapshot(keys, now=None):
    now = now or timezone.now()
    results, board, board_error = [], None, None
    for key in keys:
        item = CATALOG[key]
        entry = {"key": key, "unit": item.unit, "currency": item.currency,
                 "price": None, "observed_at": None, "change_pct": None, "caveats": []}
        try:
            if item.provider == "tgju":
                if board is None and board_error is None:
                    try:
                        board = _live_board()
                    except FeedUnavailable as exc:
                        board_error = exc
                if board_error is not None:
                    raise board_error
                row = board.get(item.source_id) or {}
                price = tgju._to_decimal(row.get("p"))
                stamped = tgju._parse_ts(row.get("ts"))
                if price is None or price <= 0 or stamped is None:
                    entry["caveats"].append("no_quote")
                else:
                    change = Decimal(str(row.get("dp") or 0))
                    entry.update(price=str(price), observed_at=stamped.isoformat(),
                                 change_pct=str(-change if row.get("dt") == "low" else change))
                    if now - stamped > timedelta(days=3):
                        entry["caveats"].append("stale_quote")
            else:
                points = history(item, now.date() - timedelta(days=14))
                if not points:
                    entry["caveats"].append("no_quote")
                else:
                    day, value = points[-1]
                    entry.update(price=str(value), observed_at=_observed_at(day, item).isoformat())
                    if len(points) > 1 and points[-2][1]:
                        prev = points[-2][1]
                        entry["change_pct"] = str(((value - prev) / prev * 100).quantize(Decimal("0.01")))
                    if now.date() - day > STALE_AFTER:
                        entry["caveats"].append("stale_quote")
        except FeedUnavailable:
            entry["caveats"].append("source_unavailable")
        if not market_open(item, now):
            entry["caveats"].append("market_closed")
        results.append(entry)
    return results
