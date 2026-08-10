"""Per-endpoint sanity checks applied between parsing a payload and storing it.

The archive verifier answers "is anything missing?" by comparing key sets. That
question cannot catch a payload that is complete and wrong, which is how
1.3M all-zero rows, 12k Persian-digit dates and a table of 547 coins collapsed
onto one symbol all passed as verified. This module answers the other question:
"does each record make sense on its own terms?"

Contract: `validate(kind, records)` -> (accepted, rejections). Callers store the
accepted records and hand the rejections to `RejectedRecord` so nothing is lost
and the provider's actual output stays inspectable.

Every rule here encodes something observed in the live payloads, not a guess:
- A stock that did not trade reports pl/pc but zeroes tvol/pmin/pmax. That is a
  real suspended day (4,778 of them line up with a missing candle), so it is
  accepted for price history and rejected for candles, which should never exist
  for a day with no trade.
- Transaction.php repeats a `row` number for a cancelled trade, once at the
  trade time and once at the cancellation time, flagged canceled=1.
- Announcement.php answers in Persian-Indic digits; ingest folds them, so
  anything still non-ASCII here means the folding was bypassed.
"""
from dataclasses import dataclass
import math

from django.conf import settings

from . import jalali

# A Jalali day the provider could plausibly report. TSE data starts in the
# 1340s (XAUUSD reaches 1348) and anything past the current year is a parse bug.
MIN_YEAR, MAX_YEAR = jalali.MIN_YEAR, jalali.MAX_YEAR

# Tehran Stock Exchange price bands are wide but finite; a quote outside this is
# a unit error (Rial/Toman confusion) rather than a real price.
MAX_PRICE = 10**12


@dataclass(frozen=True)
class Rejection:
    reason: str
    record: dict


def detect_factor_ratio_actions(ordered_rows, tolerance=0.01):
    """Return factor steps from ordered (date, unadjusted, adjusted) rows."""
    actions = []
    previous_factor = None
    for date, unadjusted, adjusted in ordered_rows:
        unadjusted = _num(unadjusted)
        adjusted = _num(adjusted)
        if not unadjusted or not adjusted:
            continue
        factor = adjusted / unadjusted
        if previous_factor:
            step = factor / previous_factor
            if abs(step - 1) > tolerance:
                actions.append({"date": date, "factor": step})
        previous_factor = factor
    return actions


def screen_series(
    symbol,
    kind,
    ordered_rows,
    *,
    corporate_action_dates=(),
    max_log_return=math.log(1.5),
):
    """Return cross-day spike rejections for ordered (date, close) rows."""
    action_dates = set(corporate_action_dates)
    rejections = []
    previous = None
    for date, close in ordered_rows:
        close = _num(close)
        if not close or close <= 0:
            continue
        if previous is not None and date not in action_dates:
            log_return = math.log(close / previous)
            if abs(log_return) > max_log_return:
                rejections.append(
                    Rejection(
                        "series_spike",
                        {
                            "symbol": symbol,
                            "kind": kind,
                            "date": date,
                            "previous_close": previous,
                            "close": close,
                            "log_return": log_return,
                        },
                    )
                )
        previous = close
    return rejections


def _num(value):
    """Coerce to float, or None when the value is not a usable number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bad_date(value):
    """Return a reason string when `value` is not a storable Jalali date.

    Dates arrive in three shapes -- "1405-05-03", "1405/05/03", and Codal's
    "۱۴۰۵/۰۵/۰۳" -- and every write path normalizes before storing. Screening has
    to judge what will actually be stored, so it normalizes too; judging the raw
    string rejected every slash-form and Persian-digit date the ingest handles.
    """
    if not value:
        return "date_missing"
    text = jalali.normalize_jalali(value)
    if not text.isascii():
        return "date_not_ascii"
    if not jalali.is_jalali(text):
        return "date_not_jalali"
    if not MIN_YEAR <= int(text[:4]) <= MAX_YEAR:
        return "date_out_of_range"
    return None


def _bad_ohlc(o, h, l, c, *, allow_all_zero):
    """Shared OHLC ordering check. Returns a reason or None."""
    values = [o, h, l, c]
    if any(v is None for v in values):
        return "price_not_numeric"
    if any(v < 0 for v in values):
        return "price_negative"
    if all(v == 0 for v in values):
        return None if allow_all_zero else "price_all_zero"
    if any(v > MAX_PRICE for v in values):
        return "price_absurd"
    # A zero leg alongside non-zero ones is a missing field, not a real quote,
    # so ordering is only meaningful once every leg is populated.
    if any(v == 0 for v in values):
        return "price_partially_zero"
    if h < l:
        return "high_below_low"
    if not (l <= o <= h):
        return "open_outside_range"
    if not (l <= c <= h):
        return "close_outside_range"
    return None


def _bad_time(value):
    if value in (None, ""):
        return None  # optional on most endpoints
    text = jalali.fold_digits(value)  # Codal answers "۱۳:۴۸:۵۴"
    if not text.isascii():
        return "time_not_ascii"
    parts = text.split(":")
    if len(parts) not in (2, 3) or not all(p.isdigit() for p in parts):
        return "time_malformed"
    hh, mm = int(parts[0]), int(parts[1])
    ss = int(parts[2]) if len(parts) == 3 else 0
    if hh > 23 or mm > 59 or ss > 59:
        return "time_out_of_range"
    return None


_OHLC_FIELDS = {
    "candle": ("open", "high", "low", "close"),
    "daily_history": ("pf", "pmax", "pmin", "pl"),
    "gold": ("open", "high", "low", "close"),
}


def salvage_ohlc_records(kind, records):
    """Null bad ancillary OHLC fields while preserving a trustworthy close."""
    fields = _OHLC_FIELDS.get(kind)
    if not fields:
        return records, []
    salvaged, issues = [], []
    open_key, high_key, low_key, close_key = fields
    for original in records:
        if not isinstance(original, dict):
            salvaged.append(original)
            continue
        if kind == "daily_history" and (_num(original.get("tvol")) or 0) == 0:
            salvaged.append(original)
            continue
        close = _num(original.get(close_key))
        if close is None or close <= 0 or close > MAX_PRICE:
            salvaged.append(original)
            continue

        record = dict(original)
        changed = []

        def clear(field, reason):
            if field not in changed:
                changed.append(field)
            record[field] = None
            issues.append(Rejection(
                f"field_{field}_{reason}",
                {**original, "field": field},
            ))

        high, low = _num(record.get(high_key)), _num(record.get(low_key))
        missing_is_bad = kind != "gold"
        if high is None or high <= 0 or high > MAX_PRICE:
            if missing_is_bad or record.get(high_key) is not None:
                clear(high_key, "invalid")
            high = None
        if low is None or low <= 0 or low > MAX_PRICE:
            if missing_is_bad or record.get(low_key) is not None:
                clear(low_key, "invalid")
            low = None
        if high is not None and low is not None and high < low:
            clear(high_key, "below_low")
            clear(low_key, "above_high")
            high = low = None
        if high is not None and low is not None and not low <= close <= high:
            clear(high_key, "excludes_close")
            clear(low_key, "excludes_close")
            high = low = None

        opened = _num(record.get(open_key))
        if opened is None or opened <= 0 or opened > MAX_PRICE:
            if missing_is_bad or record.get(open_key) is not None:
                clear(open_key, "invalid")
        elif high is not None and low is not None and not low <= opened <= high:
            clear(open_key, "outside_range")
        elif not 0.5 <= opened / close <= 2.0:
            clear(open_key, "implausible_vs_close")

        if changed:
            record["_salvaged_ohlc"] = changed
        salvaged.append(record)
    return salvaged, issues


def _check_candle(rec):
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    # A candle exists only because the stock traded, so an all-zero candle is a
    # provider artefact -- the matching "no trade" day is expressed by History.php
    # instead, and by the candle simply being absent.
    if rec.get("_salvaged_ohlc"):
        return None if (_num(rec.get("volume")) or 0) >= 0 else "volume_negative"
    reason = _bad_ohlc(
        _num(rec.get("open")), _num(rec.get("high")),
        _num(rec.get("low")), _num(rec.get("close")),
        allow_all_zero=False,
    )
    if reason:
        return reason
    return None if (_num(rec.get("volume")) or 0) >= 0 else "volume_negative"


def _check_daily_history(rec):
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    close = _num(rec.get("pc"))
    last = _num(rec.get("pl"))
    if close is None or last is None:
        return "price_not_numeric"
    if close < 0 or last < 0:
        return "price_negative"
    if max(close, last) > MAX_PRICE:
        return "price_absurd"
    if (_num(rec.get("tvol")) or 0) == 0:
        # Suspended day: the provider reports the carried-over close and zeroes
        # the rest. Legitimate and common (~22% of rows), so accept it as-is
        # rather than pretending an OHLC exists.
        return None
    if rec.get("_salvaged_ohlc"):
        return None
    return _bad_ohlc(
        _num(rec.get("pf")), _num(rec.get("pmax")),
        _num(rec.get("pmin")), _num(rec.get("pl")),
        allow_all_zero=False,
    )


def _check_real_legal(rec):
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    counts = [
        _num(rec.get(key))
        for key in ("Buy_CountI", "Buy_CountN", "Sell_CountI", "Sell_CountN")
    ]
    if all(v is None for v in counts):
        return "real_legal_fields_absent"
    if any(v is not None and v < 0 for v in counts):
        return "count_negative"
    bought = (_num(rec.get("Buy_I_Volume")) or 0) + (_num(rec.get("Buy_N_Volume")) or 0)
    sold = (_num(rec.get("Sell_I_Volume")) or 0) + (_num(rec.get("Sell_N_Volume")) or 0)
    if bought < 0 or sold < 0:
        return "volume_negative"
    # Every share bought is a share sold; the two legs must agree exactly.
    if bought and sold and bought != sold:
        return "buy_sell_volume_mismatch"
    return None


def _check_gold(rec):
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    close = _num(rec.get("close"))
    if close is None:
        return "price_not_numeric"
    if rec.get("_salvaged_ohlc"):
        return None
    # open/high/low fall back to close upstream when absent, so mirror that here
    # instead of rejecting a close-only quote.
    return _bad_ohlc(
        _num(rec.get("open")) if rec.get("open") is not None else close,
        _num(rec.get("high")) if rec.get("high") is not None else close,
        _num(rec.get("low")) if rec.get("low") is not None else close,
        close,
        allow_all_zero=False,
    )


def _check_tick(rec):
    if rec.get("row") is None:
        return "row_missing"
    reason = _bad_time(rec.get("time"))
    if reason:
        return reason
    price, volume = _num(rec.get("price")), _num(rec.get("volume"))
    if price is None or volume is None:
        return "price_not_numeric"
    if price <= 0:
        return "price_not_positive"
    if volume <= 0:
        return "volume_not_positive"
    if price > MAX_PRICE:
        return "price_absurd"
    return None


def _check_codal(rec):
    if not rec.get("title"):
        return "title_missing"
    if not rec.get("l18"):
        return "symbol_missing"
    reason = _bad_date(rec.get("date_publish"))
    if reason:
        return reason
    return _bad_time(rec.get("time_publish"))


def _check_shareholder(rec):
    if rec.get("id") is None:
        return "shareholder_id_missing"
    volume, percent = _num(rec.get("volume")), _num(rec.get("percent"))
    if volume is None or volume < 0:
        return "volume_invalid"
    if percent is None or not 0 <= percent <= 100:
        return "percent_out_of_range"
    return None


def _check_snapshot(rec):
    """Crypto and commodity: a dated quote that must identify its own symbol."""
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    name = rec.get("symbol") or rec.get("name_en") or rec.get("id")
    if not name:
        return "symbol_missing"
    if str(name).strip().upper() in ("CRYPTO", "COMMODITIES"):
        # The caller's placeholder leaking in as a record identity is exactly the
        # bug that collapsed 547 coins onto one row.
        return "symbol_is_placeholder"
    price = _num(rec.get("price"))
    toman = _num(rec.get("price_toman"))
    if price is None and toman is None:
        return "price_not_numeric"
    if (price or 0) < 0 or (toman or 0) < 0:
        return "price_negative"
    if (price or 0) <= 0 and (toman or 0) <= 0:
        return "price_not_positive"
    return None


def _check_index(rec):
    """Validate a live TEDPIX observation before it becomes benchmark data."""
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    reason = _bad_time(rec.get("time"))
    if reason:
        return reason
    overall = _num(rec.get("index"))
    if overall is None:
        return "index_not_numeric"
    if overall <= 0:
        return "index_not_positive"
    for field in ("mv", "tno", "tval", "tvol"):
        value = _num(rec.get(field))
        if value is not None and value < 0:
            return f"{field}_negative"
    return None


CHECKS = {
    "candle": _check_candle,
    "daily_history": _check_daily_history,
    "real_legal": _check_real_legal,
    "gold": _check_gold,
    "tick": _check_tick,
    "codal": _check_codal,
    "shareholder": _check_shareholder,
    "snapshot": _check_snapshot,
    "index": _check_index,
}


def validate(kind, records):
    """Split `records` into (accepted, rejections) using the rules for `kind`."""
    check = CHECKS[kind]
    accepted, rejections = [], []
    for rec in records:
        if not isinstance(rec, dict):
            rejections.append(Rejection("not_a_record", {"value": repr(rec)[:200]}))
            continue
        reason = check(rec)
        if reason:
            rejections.append(Rejection(reason, rec))
        else:
            accepted.append(rec)
    return accepted, rejections


def reconcile_tick_volume(tick_records, candle_volume, *, tolerance=None):
    """Does a day of ticks add up to the day's reported volume?

    Two independently fetched endpoints should agree. Cancelled trades carry the
    same `row` as the original and must be excluded. Exact equality was too
    brittle against provider noise (~75% of quarantines were under 1% relative),
    so a small relative tolerance (default MARKETDATA_TICK_VOLUME_TOLERANCE) is
    allowed. A zero-vs-nonzero split still fails.

    Returns None when it reconciles (or cannot be judged), else a reason.
    """
    if candle_volume is None:
        return None
    traded = int(sum(
        _num(rec.get("volume")) or 0
        for rec in tick_records
        if isinstance(rec, dict) and not rec.get("canceled")
    ))
    candle = int(candle_volume)
    if traded == candle:
        return None
    if traded == 0 or candle == 0:
        return f"tick_volume_mismatch:{traded}!={candle}"
    tol = (
        settings.MARKETDATA_TICK_VOLUME_TOLERANCE
        if tolerance is None
        else float(tolerance)
    )
    if abs(traded - candle) / max(traded, candle) <= tol:
        return None
    return f"tick_volume_mismatch:{traded}!={candle}"
