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

from .models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)

logger = logging.getLogger(__name__)

# candle_type param of fetch_candlesticks -> stored timeframe label
CANDLE_TIMEFRAMES = {1: "intraday", 2: "1d_unadj", 3: "1d_adj"}


def normalize_jalali(value) -> str:
    """Normalize a Jalali date string to dash-separated ("1403-10-19")."""
    return str(value or "").strip().replace("/", "-")


def _bulk(model, rows):
    """bulk_create with conflict-skip; returns (created, conflicts).

    Postgres does not return pks for rows under ignore_conflicts, so the only
    reliable created-count is a before/after table count. Two cheap COUNTs per
    batch is fine at this write volume (daily sync / manual backfill).
    """
    if not rows:
        return 0, 0
    before = model.objects.count()
    model.objects.bulk_create(rows, batch_size=500, ignore_conflicts=True)
    landed = model.objects.count() - before
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
    except Exception:
        logger.warning("symbol metadata ingest failed for id=%s", payload.get("id"), exc_info=True)
        return 0, 1


def ingest_daily_history(symbol: str, payload, is_adjusted: bool) -> tuple[int, int]:
    """History.php payload (list of day records) -> DailyStockHistory rows."""
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    rows, bad = [], 0
    for rec in payload:
        try:
            rows.append(DailyStockHistory(
                symbol=symbol,
                date=normalize_jalali(rec["date"]),
                time=rec.get("time", "") or "",
                tno=rec.get("tno") or 0,
                tvol=rec.get("tvol") or 0,
                tval=rec.get("tval") or 0,
                pmin=rec.get("pmin") or 0,
                pmax=rec.get("pmax") or 0,
                py=rec.get("py") or 0,
                pf=rec.get("pf") or 0,
                pl=rec.get("pl") or 0,
                plc=rec.get("plc") or 0,
                plp=rec.get("plp") or 0.0,
                pc=rec.get("pc") or 0,
                pcc=rec.get("pcc") or 0,
                pcp=rec.get("pcp") or 0.0,
                is_adjusted=is_adjusted,
                # Real/Legal breakdown only exists on unadjusted (type=0) payloads.
                buy_count_i=rec.get("Buy_CountI"),
                buy_count_n=rec.get("Buy_CountN"),
                sell_count_i=rec.get("Sell_CountI"),
                sell_count_n=rec.get("Sell_CountN"),
                buy_i_volume=rec.get("Buy_I_Volume"),
                buy_n_volume=rec.get("Buy_N_Volume"),
                sell_i_volume=rec.get("Sell_I_Volume"),
                sell_n_volume=rec.get("Sell_N_Volume"),
                buy_i_value=rec.get("Buy_I_Value"),
                buy_n_value=rec.get("Buy_N_Value"),
                sell_i_value=rec.get("Sell_I_Value"),
                sell_n_value=rec.get("Sell_N_Value"),
            ))
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed history record for %s: %r", symbol, rec)
    created, conflicts = _bulk(DailyStockHistory, rows)
    return created, conflicts + bad


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
    rows, bad = [], 0
    for rec in records:
        try:
            rows.append(MarketCandle(
                symbol=symbol,
                timeframe=timeframe,
                date_time=normalize_jalali(rec["date"]) if "date" in rec else str(rec["datetime"]),
                open_price=rec["open"],
                high_price=rec["high"],
                low_price=rec["low"],
                close_price=rec["close"],
                volume=rec.get("volume") or 0,
            ))
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed candle for %s: %r", symbol, rec)
    created, conflicts = _bulk(MarketCandle, rows)
    return created, conflicts + bad


def ingest_transactions(symbol: str, date: str, payload) -> tuple[int, int]:
    """Transaction.php payload -> StockTransactionTick rows for one day."""
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    day = normalize_jalali(date)
    rows, bad = [], 0
    for rec in payload:
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
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed tick for %s %s: %r", symbol, day, rec)
    created, conflicts = _bulk(StockTransactionTick, rows)
    return created, conflicts + bad


def ingest_shareholders(symbol: str, payload, date: str = "") -> tuple[int, int]:
    """Shareholder.php payload -> ShareholderRecord rows (unique per symbol+date+id)."""
    if not isinstance(payload, list):
        return 0, 0 if payload is None else 1
    day = normalize_jalali(date)
    rows, bad = [], 0
    for rec in payload:
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
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed shareholder for %s: %r", symbol, rec)
    created, conflicts = _bulk(ShareholderRecord, rows)
    return created, conflicts + bad


def ingest_codal(payload) -> tuple[int, int]:
    """Announcement.php payload -> CodalAnnouncement rows."""
    records = (payload or {}).get("announcement") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return 0, 0 if payload is None else 1
    rows, bad = [], 0
    for rec in records:
        try:
            cat_val = rec.get("category")
            rows.append(CodalAnnouncement(
                symbol=rec.get("l18", "") or "",
                company_name=rec.get("l30", "") or "",
                title=rec["title"],
                code=rec.get("code", "") or "",
                category=int(cat_val) if cat_val is not None else None,
                category_title=rec.get("category_title", "") or "",
                is_audited=rec.get("is_audited") if isinstance(rec.get("is_audited"), bool) else None,
                date_title=normalize_jalali(rec.get("date_title", "")),
                date_send=normalize_jalali(rec.get("date_send", "")),
                time_send=rec.get("time_send", "") or "",
                date_publish=normalize_jalali(rec.get("date_publish", "")),
                time_publish=rec.get("time_publish", "") or "",
                link=rec.get("link", "") or "",
                link_pdf=rec.get("link_pdf", "") or "",
                link_excel=rec.get("link_excel", "") or "",
                link_attachment=rec.get("link_attachment", "") or "",
            ))
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed codal record: %r", rec)
    created, conflicts = _bulk(CodalAnnouncement, rows)
    return created, conflicts + bad


def ingest_gold_currency_history(payload) -> tuple[int, int]:
    """Gold_Currency_Pro.php history=2 payload -> GoldCurrencyHistory rows.

    The payload carries symbol/name/unit at the top level and the day records
    under `history_daily`. Converts raw provider Rial quotes into Tomans.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("history_daily"), list):
        return 0, 0 if payload is None else 1
    symbol = payload.get("symbol", "") or ""
    name = payload.get("name", "") or ""
    raw_unit = payload.get("unit", "") or ""

    is_rial = (
        raw_unit == "ریال"
        or symbol in ("USD", "EUR", "GBP", "AED", "CNY", "CAD", "AUD", "CHF")
    )
    unit = "تومان" if is_rial else raw_unit

    rows, bad = [], 0
    for rec in payload["history_daily"]:
        try:
            c = float(rec["close"])
            o = float(rec.get("open")) if rec.get("open") is not None else c
            h = float(rec.get("high")) if rec.get("high") is not None else c
            l = float(rec.get("low")) if rec.get("low") is not None else c

            if is_rial and c > 500000:
                c /= 10.0
                o /= 10.0
                h /= 10.0
                l /= 10.0

            rows.append(GoldCurrencyHistory(
                symbol=symbol,
                name=name,
                unit=unit,
                date=normalize_jalali(rec["date"]),
                open_price=round(o, 4),
                high_price=round(h, 4),
                low_price=round(l, 4),
                close_price=round(c, 4),
            ))
        except (KeyError, TypeError, ValueError):
            bad += 1
            logger.warning("skipping malformed gold/currency record for %s: %r", symbol, rec)
    created, conflicts = _bulk(GoldCurrencyHistory, rows)
    return created, conflicts + bad


def ingest_market_index(payload) -> tuple[int, int]:
    """Index.php payload (single snapshot dict) -> one MarketIndexData row."""
    if not isinstance(payload, dict) or "date" not in payload:
        return 0, 0 if payload is None else 1
    rows = [MarketIndexData(
        date=normalize_jalali(payload["date"]),
        time=payload.get("time", "") or "",
        state=payload.get("state", "") or "",
        index_overall=payload.get("index") or 0.0,
        index_overall_change=payload.get("index_change") or 0.0,
        index_equal_weight=payload.get("index_equalWeight") or 0.0,
        index_equal_weight_change=payload.get("index_equalWeight_change") or 0.0,
        market_value=payload.get("mv") or 0,
        trade_number=payload.get("tno") or 0,
        trade_value=payload.get("tval") or 0,
        trade_volume=payload.get("tvol") or 0,
    )]
    return _bulk(MarketIndexData, rows)
