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

from . import jalali

# A Jalali day the provider could plausibly report. TSE data starts in the
# 1340s (XAUUSD reaches 1348) and anything past the current year is a parse bug.
MIN_YEAR, MAX_YEAR = 1340, 1500

# Tehran Stock Exchange price bands are wide but finite; a quote outside this is
# a unit error (Rial/Toman confusion) rather than a real price.
MAX_PRICE = 10**12


@dataclass(frozen=True)
class Rejection:
    reason: str
    record: dict


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


def _check_candle(rec):
    reason = _bad_date(rec.get("date"))
    if reason:
        return reason
    # A candle exists only because the stock traded, so an all-zero candle is a
    # provider artefact -- the matching "no trade" day is expressed by History.php
    # instead, and by the candle simply being absent.
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


def reconcile_tick_volume(tick_records, candle_volume):
    """Does a day of ticks add up to the day's reported volume?

    The strongest check available anywhere in this system: two independently
    fetched endpoints must agree. Cancelled trades carry the same `row` as the
    original and must be excluded -- doing so reproduced the candle volume
    exactly on every sampled day that previously disagreed.

    Returns None when it reconciles (or cannot be judged), else a reason.
    """
    if candle_volume is None:
        return None
    traded = sum(
        _num(rec.get("volume")) or 0
        for rec in tick_records
        if isinstance(rec, dict) and not rec.get("canceled")
    )
    if int(traded) != int(candle_volume):
        return f"tick_volume_mismatch:{int(traded)}!={int(candle_volume)}"
    return None
