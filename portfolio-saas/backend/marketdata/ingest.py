"""Payload -> model rows for the market-data warehouse.

Pure ingest layer: each function takes an already-fetched payload (see
marketdata.fetchers) and persists it. Field mappings mirror the BrsApi response
shapes exercised in tests/test_advanced_fetchers.py (e.g. `Buy_CountI` ->
`buy_count_i`, symbol metadata `z`/`bvol`/`mv` -> shares/base_volume/market_cap).

Conventions:
- Every function returns (created, skipped). `skipped` counts both duplicate
  rows (unique-constraint conflicts) and malformed records.
- Append-only tables use bulk_create(ignore_conflicts=True): re-running a
  backfill is free. StockSymbolMetadata is a mutable snapshot and uses
  update_or_create instead.
- One malformed record never aborts a batch: rows are built per-record inside
  try/except and logged at warning.
- Jalali dates are normalized to dash form ("1403/10/19" -> "1403-10-19") by
  `normalize_jalali` so exactly one format reaches the DB.
"""
import logging
from decimal import Decimal
from . import calendars, jalali, jalali as jalali_mod, validation
from .currency import canonical_symbol, gold_history_storage_unit, to_toman
from .models import (
    CodalAnnouncement,
    DailyStockHistory,
    DerivativeContract,
    DerivativeSnapshot,
    GoldCurrencyHistory,
    MarketCandle,
    MarketDailyBar,
    MarketIndexData,
    MarketSnapshot,
    RealLegalHistory,
    RejectedRecord,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)
from .workflows import current_correlation_id
from datetime import timedelta
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone


logger = logging.getLogger(__name__)

# candle_type param of fetch_candlesticks -> stored timeframe label
CANDLE_TIMEFRAMES = {1: "intraday", 2: "1d_unadj", 3: "1d_adj"}

#: Asset classes whose daily bars come from `DerivativeSnapshot` rather than
#: `MarketSnapshot`. Derived from the model's own choices so retiring a kind
#: (as `ime_future`/`ime_option` were on 2026-09-06) updates every branch at
#: once -- this tuple was previously spelled out at each call site, which is
#: how a removed kind can keep a dead branch alive.
DERIVATIVE_ASSET_CLASSES = frozenset(DerivativeContract.Kind.values)


# Codal is the one provider surface that answers in Persian/Arabic-Indic digits
# ("۱۴۰۵/۰۵/۰۳"). Stored raw they are unjoinable and unsortable against every
# other table, and the read-back verifier cannot see it because both sides of the
# comparison are equally Persian. Fold on the way in, once, for every field.
# Both helpers live in `jalali` so `validation` can screen dates with the same
# rules without importing this module (which imports it); re-exported here
# because every write path already calls them as `ingest.normalize_jalali`.
fold_digits = jalali.fold_digits
normalize_jalali = jalali.normalize_jalali


def screen(kind, records, endpoint, symbol="", date_key="date", default_date=""):
    """Validate `records`, persist what failed, return (accepted, rejected_count).

    The single choke point every ingest funnels through. Returning only the
    accepted records means a caller cannot forget to filter, and writing the
    rejections means a bad payload leaves evidence rather than a silent gap.

    The count comes back with them because every caller reports a skipped total:
    computed here once, a screened-out record can never be dropped silently, and
    the archive verifier still sees the endpoint as incomplete.
    """
    records, field_rejections = validation.salvage_ohlc_records(kind, records)
    accepted, rejections = validation.validate(kind, records)
    for rejection in [*field_rejections, *rejections]:
        day = rejection.record.get(date_key) or default_date
        day = normalize_jalali(day) if isinstance(day, str) else ""
        row, created = RejectedRecord.objects.get_or_create(
            endpoint=endpoint,
            symbol=str(symbol)[:64],
            date=str(day)[:10],
            reason=rejection.reason[:64],
            defaults={"payload": _jsonable(rejection.record)},
        )
        if not created:
            RejectedRecord.objects.filter(pk=row.pk).update(
                occurrences=row.occurrences + 1, payload=_jsonable(rejection.record)
            )
    if field_rejections or rejections:
        (logger.warning if rejections else logger.debug)(
            "%s(%s): rejected %d row(s), salvaged %d field(s) from %d records (%s)",
            endpoint, symbol, len(rejections), len(field_rejections), len(records),
            ", ".join(sorted({r.reason for r in [*field_rejections, *rejections]})),
        )
    return accepted, len(rejections)


def _jsonable(record):
    """JSONField refuses non-serialisable values; a sample is worth more than a crash."""
    return {key: (value if isinstance(value, (str, int, float, bool, type(None))) else repr(value))
            for key, value in record.items()}


def flatten_records(payload) -> list:
    """Yield the record dicts out of a list payload or a dict-of-lists payload.

    Commodity.php answers `{"metal_precious": [...], "metal_base": [...],
    "energy": [...]}`. Treating that dict as a single record found no `date` and
    no `symbol`, so every fetch parsed to zero rows -- and because the expected
    set was then empty, the archive state verified complete against an empty
    table for months.
    """
    if isinstance(payload, list):
        return [rec for rec in payload if isinstance(rec, dict)]
    if not isinstance(payload, dict):
        return []
    if any(isinstance(value, list) for value in payload.values()):
        return [
            rec
            for value in payload.values()
            if isinstance(value, list)
            for rec in value
            if isinstance(rec, dict)
        ]
    return [payload]


def provider_symbol(record) -> str:
    """The symbol a BrsApi record is keyed on, across every payload family.

    `symbol` on Market/*, `l18`/`code` on Tsetmc/*, and `name_en` on
    Market/Cryptocurrency.php, which carries no symbol field at all -- only an
    English name and a numeric id. The snapshot ingest learned that the hard
    way (0 rows created, 100% skipped, every cycle) and grew the fallback; the
    catalog sync did not, so crypto had 1.1M snapshots and 32k daily bars in the
    warehouse and not one MarketInstrument row, which left the add-holding
    wizard's Crypto tab permanently empty. One reader now, so the next payload
    family cannot be learned in one place and missed in the other.
    """
    if not isinstance(record, dict):
        return ""
    return str(
        record.get("symbol") or record.get("l18") or record.get("code")
        or record.get("name_en") or ""
    ).strip()


def _lineage():

    return {
        "ingested_at": timezone.now(),
        "last_correlation_id": current_correlation_id(),
    }


def _bulk(
    model, rows, scope=None, *, update_fields=(), unique_fields=(),
    recent_field=None, recent_limit=10,
):
    """bulk_create with conflict-skip; returns (created, conflicts).

    Postgres does not return pks for rows under ignore_conflicts (verified on
    Django 5.0), so the only reliable created-count is a before/after count.
    `scope` narrows that count to the slice this batch can possibly touch --
    without it, every tick ingest ran two sequential scans of a 12.7M-row table
    at ~2.1s each, which dwarfed the fetch it was measuring.
    """
    if not rows:
        return 0, 0
    qs = model.objects.filter(**scope) if scope else model.objects.all()
    before = qs.count()
    model.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
    landed = qs.count() - before
    if update_fields:
        recent_values = sorted({getattr(row, recent_field) for row in rows})[-recent_limit:]
        recent_rows = {
            tuple(getattr(row, field) for field in unique_fields): row
            for row in rows
            if getattr(row, recent_field) in recent_values
        }.values()
        model.objects.bulk_create(
            recent_rows,
            batch_size=500,
            update_conflicts=True,
            update_fields=update_fields,
            unique_fields=unique_fields,
        )
    return landed, len(rows) - landed


def ingest_symbol_metadata(payload) -> tuple[int, int]:
    """Symbol.php payload -> StockSymbolMetadata (mutable snapshot, keyed on ins_code)."""
    if not isinstance(payload, dict) or payload.get("id") is None:
        return 0, 1
    try:
        _, created = StockSymbolMetadata.objects.update_or_create(
            ins_code=payload["id"],
            defaults={
                "l18": payload.get("l18", ""),
                "l30": payload.get("l30", ""),
                "l30_en": payload.get("l30_en", "") or "",
                "isin": payload.get("isin", "") or "",
                "code_12": payload.get("code_12", "") or "",
                "code_5": payload.get("code_5", "") or "",
                "code_4": payload.get("code_4", "") or "",
                "market": payload.get("m", "") or "",
                "market_board": payload.get("m_board", "") or "",
                "sector": payload.get("cs", "") or "",
                "sector_sub": payload.get("cs_sub", "") or "",
                "shares_count": payload.get("z") or 0,
                "base_volume": payload.get("bvol") or 0,
                "market_cap": payload.get("mv") or 0,
                "eps": payload.get("eps"),
                "pe": payload.get("pe"),
                "g_pe": payload.get("gpe"),
                "ps": payload.get("ps"),
                "free_float": payload.get("free_float"),
                "state": payload.get("state", "") or "",
            },
        )
        return (1, 0) if created else (0, 1)
    except Exception as exc:
        logger.warning("symbol metadata ingest failed for id=%s: %s", payload.get("id"), exc)
        return 0, 1


def ingest_daily_history(symbol: str, payload) -> tuple[int, int]:
    """History.php?type=0 payload (list of day records) -> DailyStockHistory rows.

    type=1 (misleadingly named "adjusted" in the endpoint registry) is the
    Real/Legal participant breakdown, not price data -- see `ingest_real_legal`.
    """
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    accepted, bad = screen("daily_history", payload, "stock_history_unadjusted", symbol)

    rows = []
    malformed = 0
    malformed_reasons = set()
    for rec in accepted:
        try:
            pmin_val = rec.get("pmin") if "pmin" in rec else 0
            pmax_val = rec.get("pmax") if "pmax" in rec else 0
            py_val = rec.get("py") or 0
            pf_val = rec.get("pf") if "pf" in rec else 0
            pl_val = rec.get("pl") or 0
            plc_val = rec.get("plc") or 0
            pc_val = rec.get("pc") or 0
            pcc_val = rec.get("pcc") or 0
            tval_val = rec.get("tval") or 0

            buy_i_val = rec.get("Buy_I_Value")
            buy_n_val = rec.get("Buy_N_Value")
            sell_i_val = rec.get("Sell_I_Value")
            sell_n_val = rec.get("Sell_N_Value")

            rows.append(DailyStockHistory(
                symbol=symbol,
                date=normalize_jalali(rec["date"]),
                time=rec.get("time", "") or "",
                tno=rec.get("tno") or 0,
                tvol=rec.get("tvol") or 0,
                tval=int(float(tval_val)),
                pmin=Decimal(str(pmin_val)) if pmin_val is not None else None,
                pmax=Decimal(str(pmax_val)) if pmax_val is not None else None,
                py=Decimal(str(py_val)),
                pf=Decimal(str(pf_val)) if pf_val is not None else None,
                pl=Decimal(str(pl_val)),
                plc=Decimal(str(plc_val)),
                plp=rec.get("plp") or 0.0,
                pc=Decimal(str(pc_val)),
                pcc=Decimal(str(pcc_val)),
                pcp=rec.get("pcp") or 0.0,
                # Real/Legal breakdown only exists on unadjusted (type=0) payloads.
                buy_count_i=rec.get("Buy_CountI"),
                buy_count_n=rec.get("Buy_CountN"),
                sell_count_i=rec.get("Sell_CountI"),
                sell_count_n=rec.get("Sell_CountN"),
                buy_i_volume=rec.get("Buy_I_Volume"),
                buy_n_volume=rec.get("Buy_N_Volume"),
                sell_i_volume=rec.get("Sell_I_Volume"),
                sell_n_volume=rec.get("Sell_N_Volume"),
                buy_i_value=int(float(buy_i_val)) if buy_i_val is not None else None,
                buy_n_value=int(float(buy_n_val)) if buy_n_val is not None else None,
                sell_i_value=int(float(sell_i_val)) if sell_i_val is not None else None,
                sell_n_value=int(float(sell_n_val)) if sell_n_val is not None else None,
                **_lineage(),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if malformed:
        logger.warning(
            "ingest_daily_history(%s): skipped %d malformed record(s) (%s)",
            symbol, malformed, ", ".join(sorted(malformed_reasons)),
        )
    created, conflicts = _bulk(
        DailyStockHistory, rows, scope={"symbol": symbol},
        update_fields=(
            "time", "tno", "tvol", "tval", "pmin", "pmax", "py", "pf",
            "pl", "plc", "plp", "pc", "pcc", "pcp", "buy_count_i",
            "buy_count_n", "sell_count_i", "sell_count_n", "buy_i_volume",
            "buy_n_volume", "sell_i_volume", "sell_n_volume", "buy_i_value",
            "buy_n_value", "sell_i_value", "sell_n_value",
            "ingested_at", "last_correlation_id",
        ),
        unique_fields=("symbol", "date"),
        recent_field="date",
    )
    return created, conflicts + bad


# History.php?type=1 returns the Real/Legal (حقیقی/حقوقی) participant breakdown,
# NOT adjusted prices -- it carries no price field at all. Feeding it through
# ingest_daily_history wrote 1.3M rows whose every price column defaulted to 0,
# and the date-set verifier passed them because the dates were present. Adjusted
# prices come from Candlestick.php type=3 (MarketCandle "1d_adj"), not from here.
_REAL_LEGAL_FIELDS = {
    "buy_count_i": "Buy_CountI",
    "buy_count_n": "Buy_CountN",
    "sell_count_i": "Sell_CountI",
    "sell_count_n": "Sell_CountN",
    "buy_i_volume": "Buy_I_Volume",
    "buy_n_volume": "Buy_N_Volume",
    "sell_i_volume": "Sell_I_Volume",
    "sell_n_volume": "Sell_N_Volume",
    "buy_i_value": "Buy_I_Value",
    "buy_n_value": "Buy_N_Value",
    "sell_i_value": "Sell_I_Value",
    "sell_n_value": "Sell_N_Value",
}


def ingest_real_legal(symbol: str, payload) -> tuple[int, int]:
    """History.php?type=1 payload -> independent real/legal daily rows.

    The `.update()` below writes real/legal value columns onto the matching
    `DailyStockHistory` row. Both this function's values and
    `ingest_daily_history`'s are now stored undivided (Rial), so this no
    longer overwrites a divided (Toman) column with an undivided one -- keep
    it that way if either write path's conversion ever changes.
    """
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    accepted, bad = screen("real_legal", payload, "real_legal_history", symbol)
    rows = []
    updated_daily = 0
    skipped_daily = 0
    for rec in accepted:
        date_val = normalize_jalali(rec["date"])
        fields_to_update = {field: rec.get(key) for field, key in _REAL_LEGAL_FIELDS.items()}

        updated_rows = DailyStockHistory.objects.filter(
            symbol=symbol, date=date_val,
        ).update(**fields_to_update)
        updated_daily += int(updated_rows > 0)
        skipped_daily += int(updated_rows == 0)

        rows.append(RealLegalHistory(
            symbol=symbol,
            date=date_val,
            **fields_to_update,
        ))
    _bulk(RealLegalHistory, rows, scope={"symbol": symbol})
    return updated_daily, skipped_daily + bad


def ingest_candles(symbol: str, candle_type: int, payload) -> tuple[int, int]:
    """Candlestick.php payload -> MarketCandle rows for one timeframe."""
    timeframe = CANDLE_TIMEFRAMES.get(candle_type, str(candle_type))
    records = (payload or {}).get("candle_daily") if isinstance(payload, dict) else None
    if records is None and isinstance(payload, dict):
        records = payload.get("candle_daily_adjusted")
    if records is None and isinstance(payload, dict):
        records = payload.get("candle_intraday")
    if not isinstance(records, list):
        return 0, 0 if payload is None else 1
    endpoint = f"stock_candle_{'adjusted' if candle_type == 3 else 'unadjusted'}"
    accepted, bad = screen("candle", records, endpoint, symbol)

    rows = []
    malformed = 0
    malformed_reasons = set()
    for rec in accepted:
        try:
            dt_str = normalize_jalali(rec["date"]) if "date" in rec else str(rec["datetime"])
            # Split on whitespace to keep only the date portion (e.g. "1405-05-09")
            if dt_str:
                dt_str = dt_str.split()[0]
            rows.append(MarketCandle(
                symbol=symbol,
                timeframe=timeframe,
                date_time=dt_str,
                open_price=rec.get("open") if rec.get("open") is not None else None,
                high_price=rec.get("high") if rec.get("high") is not None else None,
                low_price=rec.get("low") if rec.get("low") is not None else None,
                close_price=rec.get("close") or 0,
                volume=rec.get("volume") or 0,
                **_lineage(),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if malformed:
        logger.warning(
            "ingest_candles(%s): skipped %d malformed record(s) (%s)",
            symbol, malformed, ", ".join(sorted(malformed_reasons)),
        )
    created, conflicts = _bulk(
        MarketCandle, rows, scope={"symbol": symbol, "timeframe": timeframe},
        update_fields=("open_price", "high_price", "low_price", "close_price", "volume", "ingested_at", "last_correlation_id"),
        unique_fields=("symbol", "timeframe", "date_time"),
        recent_field="date_time",
    )
    return created, conflicts + bad


def ingest_transactions(
    symbol: str, date: str, payload, *, replace=False, require_all_valid=False,
) -> tuple[int, int]:
    """Transaction.php payload -> StockTransactionTick rows for one day."""
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    day = normalize_jalali(date)
    rows = []
    # The tick payload has no date of its own, so the rejection rows are stamped
    # with the day we asked for rather than a field that does not exist.
    accepted, bad = screen(
        "tick", payload, "stock_transaction_ticks", symbol, default_date=day
    )
    if require_all_valid and bad:
        return 0, bad
    malformed = 0
    malformed_reasons = set()
    for rec in accepted:
        try:
            rows.append(StockTransactionTick(
                symbol=symbol,
                date=day,
                time=rec.get("time", "") or "",
                row=rec["row"],
                price=rec["price"],
                volume=rec.get("volume") or 0,
                canceled=bool(rec.get("canceled")),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if malformed:
        logger.warning(
            "ingest_transactions(%s, %s): skipped %d malformed record(s) (%s)",
            symbol, day, malformed, ", ".join(sorted(malformed_reasons)),
        )

    with transaction.atomic():
        if replace:
            # Bound the partition column too, or this deletes by scanning all
            # 1,240 chunks of the hypertable -- see `jalali.ts_window`. Falls
            # back to the unpruned delete when the day will not parse, because
            # deleting nothing here would leave the old rows in place.
            stale = StockTransactionTick.objects.filter(symbol=symbol, date=day)
            window = jalali.ts_window(day)
            if window:
                stale = stale.filter(ts__gte=window[0], ts__lt=window[1])
            stale.delete()
        created, conflicts = _bulk(
            StockTransactionTick, rows, scope={"symbol": symbol, "date": day}
        )
    return created, conflicts + bad


def ingest_shareholders(symbol: str, payload, date: str = "") -> tuple[int, int]:
    """Shareholder.php payload -> ShareholderRecord rows (unique per symbol+date+id).

    The provider's roster carries no date of its own -- it is "who holds this
    today". Callers that omit `date` used to store every row under "", so all
    625 symbols collapsed to a single dateless snapshot that each re-fetch
    silently overwrote. Stamping the observation day makes it a real series.
    """
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    day = normalize_jalali(date) or jalali.today()
    rows = []
    accepted, bad = screen(
        "shareholder", payload, "shareholder_records", symbol, default_date=day
    )
    malformed = 0
    malformed_reasons = set()
    for rec in accepted:
        try:
            rows.append(ShareholderRecord(
                symbol=symbol,
                date=day,
                shareholder_id=rec["id"],
                name=rec.get("name", "") or "",
                volume=rec.get("volume") or 0,
                percent=rec.get("percent") or 0.0,
                change=rec.get("change") or 0,
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if malformed:
        logger.warning(
            "ingest_shareholders(%s): skipped %d malformed record(s) (%s)",
            symbol, malformed, ", ".join(sorted(malformed_reasons)),
        )
    created, conflicts = _bulk(
        ShareholderRecord, rows, scope={"symbol": symbol, "date": day}
    )
    return created, conflicts + bad


def ingest_codal(payload) -> tuple[int, int]:
    """Announcement.php payload -> CodalAnnouncement rows."""
    from .codal_classification import classify

    records = (payload or {}).get("announcement") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return 0, 0 if payload is None else 1
    # Fold first, then validate: this endpoint answers in Persian-Indic digits,
    # so validating the raw record would reject every announcement for a defect
    # the very next line repairs.
    folded = [
        {
            **rec,
            "code": fold_digits(rec.get("code", "")),
            "date_title": normalize_jalali(rec.get("date_title", "")),
            "date_send": normalize_jalali(rec.get("date_send", "")),
            "time_send": fold_digits(rec.get("time_send", "")),
            "date_publish": normalize_jalali(rec.get("date_publish", "")),
            "time_publish": fold_digits(rec.get("time_publish", "")),
        }
        for rec in records
        if isinstance(rec, dict)
    ]
    accepted, rejected = screen(
        "codal", folded, "codal_announcements", date_key="date_publish"
    )
    rows, bad = [], len(records) - len(folded) + rejected
    malformed = 0
    malformed_reasons = set()
    for rec in accepted:
        try:
            cat_val = rec.get("category")
            source_category = int(cat_val) if cat_val is not None else None
            source_category_title = rec.get("category_title", "") or ""
            source_is_audited = rec.get("is_audited") if isinstance(rec.get("is_audited"), bool) else None
            classification = classify(rec["title"], source_category, source_category_title)
            rows.append(CodalAnnouncement(
                # Strip: the provider pads some symbols with a trailing space
                # ("زقیام " vs "زقیام"). Storing it verbatim silently broke every
                # join to MarketInstrument/Asset for those symbols, so they read
                # as having no disclosures at all while 100 rows sat in the table.
                symbol=(rec.get("l18") or "").strip(),
                company_name=(rec.get("l30") or "").strip(),
                title=rec["title"],
                # Already ASCII-folded above, before validation.
                code=rec["code"],
                source_category=source_category,
                source_category_title=source_category_title,
                source_is_audited=source_is_audited,
                category=source_category,
                category_title=source_category_title,
                doc_type=classification.doc_type,
                tier=classification.tier,
                classified_by=classification.classified_by,
                is_audited=source_is_audited,
                date_title=rec["date_title"],
                date_send=rec["date_send"],
                time_send=rec["time_send"],
                date_publish=rec["date_publish"],
                time_publish=rec["time_publish"],
                link=rec.get("link", "") or "",
                link_pdf=rec.get("link_pdf", "") or "",
                link_excel=rec.get("link_excel", "") or "",
                link_attachment=rec.get("link_attachment", "") or "",
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if malformed:
        logger.warning(
            "ingest_codal: skipped %d malformed record(s) (%s)",
            malformed, ", ".join(sorted(malformed_reasons)),
        )
    # One payload spans several issuers, so the count is scoped to the symbols
    # this batch actually touches rather than the whole (growing) table.
    created, conflicts = _bulk(
        CodalAnnouncement, rows, scope={"symbol__in": sorted({r.symbol for r in rows})}
    )
    if created:
        _enqueue_codal_extractions(rows)
    return created, conflicts + bad


def _enqueue_codal_extractions(rows):
    """Start document extraction for announcements this batch just introduced.

    The extractor used to be a batch job on a six-hourly cron, which against a
    ~74,000-document backlog meant a filing waited hours to be read even though
    downloading it costs no provider quota at all. Queueing at write time is the
    "as soon as it appears" behaviour; `queue_codal_extractions` stays on as the
    sweeper for anything that fails or is stranded.

    Re-querying rather than using the bulk_create return value: Postgres does not
    hand back pks under ignore_conflicts (see `_bulk`). Filtering on
    `report__isnull=True` is what makes this idempotent -- an announcement already
    extracted is simply not selected, so a repeated payload enqueues nothing.
    """

    from .codal_storage import origin_unreachable
    from .tasks import _dispatch_codal_ids

    if not settings.CODAL_ENABLED or origin_unreachable():
        return 0
    pairs = sorted({(row.symbol, row.code) for row in rows if row.symbol and row.code})
    if not pairs:
        return 0
    has_artifact = (
        Q(link_excel__gt="") | Q(link_pdf__gt="") | Q(link__gt="") | Q(link_attachment__gt="")
    )
    pair_filter = Q()
    for symbol, code in pairs:
        pair_filter |= Q(symbol=symbol, code=code)
    ids = list(
        CodalAnnouncement.objects.filter(
            has_artifact, pair_filter, report__isnull=True
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    return _dispatch_codal_ids(ids)


def _number(value):
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except (ArithmeticError, TypeError, ValueError):
        return None


def ingest_derivative_snapshots(kind, payload) -> tuple[int, int]:
    """Persist provider snapshots without pretending they are historical data."""

    rows = flatten_records(payload)
    observed_at = timezone.now()
    created = skipped = 0
    for row in rows:
        code = str(
            row.get("contract_code") or row.get("code") or row.get("l18") or row.get("symbol") or ""
        ).strip()
        if not code:
            skipped += 1
            continue
        contract, _ = DerivativeContract.objects.update_or_create(
            kind=kind,
            contract_code=code,
            defaults={
                "underlying_code": str(
                    row.get("underlying_code") or row.get("underlying") or row.get("symbol_underlying") or ""
                ).strip(),
                "expiry_date": normalize_jalali(
                    row.get("date_end") or row.get("expiry_date") or ""
                ),
                "contract_size": _number(row.get("contract_size") or row.get("size")),
                "active": True,
                "provider_payload": row,
            },
        )
        DerivativeSnapshot.objects.create(
            contract=contract,
            observed_at=observed_at,
            last_price=_number(row.get("last_price") or row.get("price") or row.get("pl")),
            bid_price=_number(row.get("bid_price") or row.get("best_demand_price")),
            ask_price=_number(row.get("ask_price") or row.get("best_supply_price")),
            volume=row.get("volume") or row.get("tvol") or None,
            open_interest=row.get("open_interest") or row.get("openinterest") or None,
            provider_payload=row,
        )
        created += 1
    return created, skipped
# The provider answers a `USDT` request with rows that belong under `USDT_IRT`
# (Tether quoted in Rial). Callers that verify a fetch must read back on the
# symbol the ingest wrote, not the one they asked for, or the state can never
# converge and re-fetches forever.
def canonical_gold_symbol(payload, fallback: str = "") -> str:
    raw = (payload.get("symbol") if isinstance(payload, dict) else "") or fallback or ""
    return canonical_symbol(raw)


def ingest_gold_currency_history(payload, *, origin=GoldCurrencyHistory.Origin.BRSAPI) -> tuple[int, int]:
    """Gold_Currency_Pro.php history=2 payload -> GoldCurrencyHistory rows.

    The payload carries symbol/name/unit at the top level and the day records
    under `history_daily`.

    Unit policy: this table is Toman-denominated for the IRR-quoted symbols and
    provider-native for the rest (`XAUUSD` in دلار, `BTC` in تتر). The provider
    answers Rial for fiat pairs and Toman for gold/coins, so `to_toman()`
    normalises the Rial ones and everything lands on one scale. This is the one
    deliberate exception to storing verbatim: `portfolio/services/valuation.py`
    reads `close_price` straight as Toman, so a Rial row here would value a
    holding 10x high.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("history_daily"), list):
        return 0, 0 if payload is None else 1
    if origin not in (GoldCurrencyHistory.Origin.BRSAPI, GoldCurrencyHistory.Origin.TGJU):
        raise ValueError("gold history origin must identify the source")
    symbol = canonical_gold_symbol(payload)
    name = payload.get("name", "") or ""
    raw_unit = payload.get("unit", "") or ""

    # Rial- and Toman-quoted input both end up Toman, so both get the Toman
    # label; a USD/Tether-quoted payload is never converted and keeps its own.
    unit = gold_history_storage_unit(raw_unit)
    is_irr_quoted = unit == "تومان"
    # Fail closed on anything else. valuation.py reads close_price straight as
    # Toman, so storing a quote whose scale we cannot name is a silent 10x
    # waiting to happen -- refusing the batch is the safe default.
    if unit is None:
        RejectedRecord.objects.get_or_create(
            endpoint="gold_daily",
            symbol=symbol,
            date="",
            reason="unit_unrecognised",
            defaults={"payload": {"unit": raw_unit, "name": name}},
        )
        logger.warning(
            "ingest_gold_currency_history: refusing %s -- unrecognised unit %r",
            symbol, raw_unit,
        )
        return 0, len(payload["history_daily"])

    def to_storage(value):
        return float(to_toman(symbol, value, raw_unit)) if is_irr_quoted else float(value)

    # This screen is what stops the 54 high-below-low rows found in the audit
    # from coming back on the next fetch.
    accepted, bad = screen("gold", payload["history_daily"], "gold_daily", symbol)

    # Sort by date chronological order so rolling screening works cleanly
    accepted_sorted = sorted(accepted, key=lambda r: normalize_jalali(r.get("date", "")))

    # Outlier screening compares each close against the median of its
    # *neighbours in this batch*, never against the current price level. The
    # previous version seeded this from the 5 newest stored rows and then
    # walked the payload oldest-first, so a full-history fetch measured
    # 1348-era prices against today's -- every early record read as a >50%
    # deviation and was blacklisted, 105k rows across all 38 symbols, while
    # `archive.py` subtracted those dates from `missing` and called the symbol
    # complete. Seeded from the batch, the median tracks the era being ingested.
    rolling_closes = []

    rows = []
    outliers = 0
    malformed = 0
    malformed_reasons = set()
    for rec in accepted_sorted:
        try:
            c = to_storage(float(rec["close"]))
            salvaged_fields = set(rec.get("_salvaged_ohlc", ()))
            o = (None if "open" in salvaged_fields else
                 to_storage(float(rec.get("open"))) if rec.get("open") is not None else c)
            h = (None if "high" in salvaged_fields else
                 to_storage(float(rec.get("high"))) if rec.get("high") is not None else c)
            l = (None if "low" in salvaged_fields else
                 to_storage(float(rec.get("low"))) if rec.get("low") is not None else c)

            # Cross-day outlier screening (both sides are on the storage scale).
            if len(rolling_closes) >= 3:
                import statistics
                median = statistics.median(rolling_closes)
                if median > 0:
                    deviation = abs(c - median) / median
                    if deviation > 0.50:
                        bad += 1
                        day_str = normalize_jalali(rec["date"])
                        row_rej, created_rej = RejectedRecord.objects.get_or_create(
                            endpoint="gold_daily",
                            symbol=str(symbol)[:64],
                            date=str(day_str)[:10],
                            reason="outlier_deviation",
                            defaults={"payload": {"close": c, "median": median, "record": rec}},
                        )
                        if not created_rej:
                            RejectedRecord.objects.filter(pk=row_rej.pk).update(
                                occurrences=row_rej.occurrences + 1,
                                payload={"close": c, "median": median, "record": rec}
                            )
                        outliers += 1
                        continue

            # Update rolling closes with verified price
            rolling_closes.append(c)
            if len(rolling_closes) > 5:
                rolling_closes.pop(0)

            rows.append(GoldCurrencyHistory(
                symbol=symbol,
                name=name,
                unit=unit,
                date=normalize_jalali(rec["date"]),
                open_price=Decimal(str(o)) if o is not None else None,
                high_price=Decimal(str(h)) if h is not None else None,
                low_price=Decimal(str(l)) if l is not None else None,
                close_price=Decimal(str(c)),
                source=GoldCurrencyHistory.Source.PROVIDER,
                origin=origin,
                **_lineage(),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            bad += 1
            malformed += 1
            malformed_reasons.add(type(exc).__name__)
    if outliers:
        logger.warning("gold_daily(%s): rejected %d outlier close(s)", symbol, outliers)
    if malformed:
        logger.warning(
            "ingest_gold_currency_history(%s): skipped %d malformed record(s) (%s)",
            symbol, malformed, ", ".join(sorted(malformed_reasons)),
        )
    created, conflicts = _bulk(
        GoldCurrencyHistory, rows, scope={"symbol": symbol},
        update_fields=(
            "name", "unit", "open_price", "high_price", "low_price",
            "close_price", "source", "origin", "ingested_at", "last_correlation_id",
        ),
        unique_fields=("symbol", "date"),
        recent_field="date",
    )
    return created, conflicts + bad


def ingest_market_index(payload) -> tuple[int, int]:
    """Index.php payload (single snapshot dict) -> one MarketIndexData row."""
    if not isinstance(payload, dict) or "date" not in payload:
        return 0, 0 if payload is None else 1
    accepted, rejected = screen("index", [payload], "market_index", "TEDPIX")
    rows = [
        MarketIndexData(
            date=normalize_jalali(record["date"]),
            time=record.get("time", "") or "",
            state=record.get("state", "") or "",
            index_overall=record["index"],
            index_overall_change=record.get("index_change") or 0.0,
            index_equal_weight=record.get("index_equalWeight") or 0.0,
            index_equal_weight_change=record.get("index_equalWeight_change") or 0.0,
            market_value=record.get("mv") or 0,
            trade_number=record.get("tno") or 0,
            trade_value=record.get("tval") or 0,
            trade_volume=record.get("tvol") or 0,
        )
        for record in accepted
    ]
    created, conflicts = _bulk(MarketIndexData, rows)
    return created, conflicts + rejected


def ingest_tedpix_history(records) -> tuple[int, int]:
    """TGJU daily TEDPIX rows -> one close observation per trading day."""
    rows = []
    rejected = 0
    for record in records or ():
        date = normalize_jalali(record.get("jalali", ""))
        close = record.get("close")
        if not jalali.is_jalali(date) or close is None or close <= 0:
            rejected += 1
            continue
        rows.append(
            MarketIndexData(
                date=date,
                time="00:00:00",
                # State stays EMPTY on purpose. `calendars.is_closure_day`
                # reads the newest non-empty state for a date to decide whether
                # the exchange was shut; a historical close carries no such
                # claim, and any sentinel string here would be silently read as
                # "not closed" for 2,751 days. Empty is the one value that
                # branch already excludes.
                state="",
                index_overall=float(close),
            )
        )
    created, conflicts = _bulk(MarketIndexData, rows)
    return created, conflicts + rejected


def ingest_market_snapshots(asset_class, payload) -> tuple[int, int]:
    """Persist a live poll of crypto/commodity as raw snapshots.

    Index is deliberately not one of these: `MarketIndexData` already
    captures every index live tick via the existing `ingest_market_index`
    path, and `aggregate_market_daily_bars` reads that table directly for
    `asset_class="index"` instead of a second raw-capture table.

    Same shape as `ingest_derivative_snapshots`: append-only, no pretense of
    being historical data on its own -- `aggregate_market_daily_bars` is what
    turns repeated snapshots into a proper daily series. Field names are
    defensive across both BrsApi payload families seen elsewhere in this
    module: `symbol`/`price` (Market/* endpoints, e.g. gold/currency/crypto)
    and `l18`/`pl`/`pc` (Tsetmc/* endpoints).
    """

    rows = flatten_records(payload)
    observed_at = timezone.now()
    created = skipped = 0
    for row in rows:
        symbol = provider_symbol(row)
        if not symbol:
            skipped += 1
            logger.warning(
                "ingest_market_snapshots(%s): skipped row with no usable symbol "
                "field, keys present=%s",
                asset_class, sorted(row.keys()),
            )
            continue
        price = _number(
            row.get("price") or row.get("pl") or row.get("pc") or row.get("last_price")
        )
        rows_out = MarketSnapshot.objects.create(
            asset_class=asset_class,
            symbol=canonical_symbol(symbol) if asset_class in ("crypto", "commodity") else symbol[:64],
            observed_at=observed_at,
            last_price=price,
            bid_price=_number(row.get("bid_price") or row.get("pd") or row.get("best_demand_price")),
            ask_price=_number(row.get("ask_price") or row.get("po") or row.get("best_supply_price")),
            volume=row.get("volume") or row.get("tvol") or None,
            provider_payload=row,
        )
        created += 1 if rows_out else 0
    logger.info(
        "ingest_market_snapshots(%s): created %d row(s), skipped %d of %d fetched",
        asset_class, created, skipped, len(rows),
    )
    return created, skipped


def aggregate_market_daily_bars(asset_class, jalali_date, *, symbols=None) -> tuple[int, int]:
    """Distill one Jalali day's `MarketSnapshot`/`DerivativeSnapshot` rows into
    `MarketDailyBar` OHLC rows, for one asset class.

    Derivative kinds (`DerivativeContract.Kind`) read from the existing
    `DerivativeSnapshot` table (kept as-is, see MarketSnapshot's docstring);
    the newly-wired classes read from `MarketSnapshot`.
    Skips symbols where `calendars.is_closure_day`/`is_contract_expired` says
    there was nothing to fetch, so a legitimate no-data day never shows up as
    a gap in `MarketDailyBar`'s own row-existence completeness signal.
    """


    start = jalali_mod.to_datetime(jalali_date)
    end = start + timedelta(days=1)

    if asset_class in DERIVATIVE_ASSET_CLASSES:
        snapshot_qs = DerivativeSnapshot.objects.filter(
            contract__kind=asset_class, observed_at__gte=start, observed_at__lt=end
        ).select_related("contract").order_by("contract__contract_code", "observed_at")
        grouped: dict[str, list] = {}
        for snap in snapshot_qs:
            grouped.setdefault(snap.contract.contract_code, []).append(snap)
    elif asset_class == "index":
        # No dedicated MarketSnapshot rows: MarketIndexData already captures
        # every live tick (existing ingest_market_index path), so read that
        # directly instead of dual-writing the same signal into two tables.
        from types import SimpleNamespace

        index_rows = MarketIndexData.objects.filter(date=jalali_date).order_by("time")
        grouped = {"overall": [], "equal_weight": []}
        for row in index_rows:
            grouped["overall"].append(
                SimpleNamespace(last_price=row.index_overall, volume=row.trade_volume)
            )
            grouped["equal_weight"].append(
                SimpleNamespace(last_price=row.index_equal_weight, volume=row.trade_volume)
            )
    else:
        snapshot_qs = MarketSnapshot.objects.filter(
            asset_class=asset_class, observed_at__gte=start, observed_at__lt=end
        ).order_by("symbol", "observed_at")
        grouped = {}
        for snap in snapshot_qs:
            grouped.setdefault(snap.symbol, []).append(snap)

    target_symbols = set(symbols) if symbols else set(grouped)
    rows = []
    skipped_closed = 0
    for symbol in target_symbols:
        snaps = grouped.get(symbol, [])
        if not snaps:
            if asset_class in DERIVATIVE_ASSET_CLASSES:
                expired = calendars.is_contract_expired(asset_class, symbol, jalali_date)
            else:
                expired = False
            if expired or calendars.is_closure_day(asset_class, symbol, jalali_date):
                skipped_closed += 1
            continue
        prices = [s.last_price for s in snaps if s.last_price is not None]
        if not prices:
            continue
        volumes = [s.volume for s in snaps if s.volume is not None]
        rows.append(MarketDailyBar(
            asset_class=asset_class,
            symbol=symbol,
            date=jalali_date,
            open_price=prices[0],
            high_price=max(prices),
            low_price=min(prices),
            close_price=prices[-1],
            volume=sum(volumes) if volumes else None,
            open_interest=next(
                (s.open_interest for s in reversed(snaps) if getattr(s, "open_interest", None) is not None),
                None,
            ),
            sample_count=len(snaps),
        ))
    created, conflicts = _bulk(
        MarketDailyBar, rows,
        scope={"asset_class": asset_class, "date": jalali_date},
        update_fields=(
            "open_price", "high_price", "low_price", "close_price",
            "volume", "open_interest", "sample_count",
        ),
        unique_fields=("asset_class", "symbol", "date"),
        recent_field="date",
    )
    return created, conflicts + skipped_closed


def ingest_direct_crypto_history(symbol, unit, candles, *, repair_zero=False) -> tuple[int, int]:
    """Wallex UDF candles -> GoldCurrencyHistory rows.

    Deliberately never overwrites an existing row, and the reason is the
    validation that justified this backfill at all. Wallex and the incumbent
    provider quote the same coins at different venues: across the 1,037
    overlapping BTC days they agreed to a median of 0.43% (p90 1.42%, one day
    over 5%), and on USDT/Toman to a median of 0.26%. Close enough to trust the
    days we are MISSING, not close enough to be worth rewriting days we already
    have -- rewriting them would stitch two venues into one series and put a
    small discontinuity at every join, in a table the returns matrix reads.

    So this fills holes and leaves valid history alone. An explicit repair pass
    may replace a persisted zero for the same symbol, quote unit, and provider:
    zero is not a valid price, and four-decimal storage truncated the entire
    SHIB/USDT history to zero. The ordinary backfill remains insert-only.

    Unit is passed in rather than inferred, per the rule that a quote's currency
    is declared by whoever produced it. Wallex says `quoteAsset`, and the
    warehouse's existing convention is followed exactly: a Toman series is
    `{BASE}_IRT`/تومان (as `USDT_IRT` already is) and a tether series is
    `{BASE}`/تتر (as `BTC` already is). Writing a Toman price under the bare
    symbol would put two units in one column keyed by one symbol -- the failure
    this warehouse has paid for more than once.
    """

    if not candles:
        return 0, 0

    rows, rejected = [], 0
    for candle in candles:
        date = jalali.from_epoch(candle.get("ts"))
        close = candle.get("close")
        if not date or close is None or close <= 0:
            rejected += 1
            continue
        rows.append(
            GoldCurrencyHistory(
                symbol=symbol,
                unit=unit,
                date=date,
                open_price=candle.get("open"),
                high_price=candle.get("high"),
                low_price=candle.get("low"),
                close_price=close,
                source=GoldCurrencyHistory.Source.PROVIDER,
                origin=GoldCurrencyHistory.Origin.WALLEX,
                ingested_at=timezone.now(),
            )
        )

    created, conflicts = _bulk(
        GoldCurrencyHistory, rows, scope={"symbol": symbol}
    )
    repaired = 0
    if repair_zero and rows:
        by_date = {row.date: row for row in rows}
        damaged = GoldCurrencyHistory.objects.filter(
            symbol=symbol, unit=unit, source=GoldCurrencyHistory.Source.PROVIDER,
            date__in=by_date, close_price=0,
        )
        fixes = []
        for stored in damaged:
            incoming = by_date[stored.date]
            for field in ("open_price", "high_price", "low_price", "close_price"):
                setattr(stored, field, getattr(incoming, field))
            stored.origin = GoldCurrencyHistory.Origin.WALLEX
            fixes.append(stored)
        if fixes:
            GoldCurrencyHistory.objects.bulk_update(
                fixes, ["open_price", "high_price", "low_price", "close_price", "origin"],
                batch_size=200,
            )
            repaired = len(fixes)
    return created, conflicts + rejected - repaired
