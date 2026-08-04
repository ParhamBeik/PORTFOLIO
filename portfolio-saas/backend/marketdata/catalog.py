"""Provider catalog synchronization and eligibility classification."""
import logging
from django.conf import settings
from django.db.models import Max, Min
from django.utils import timezone

from .fetchers import fetch_all_symbols, fetch_gold_currency_free
from . import jalali
from .models import (
    GoldCurrencyHistory,
    InstrumentListingHistory,
    MarketCandle,
    MarketInstrument,
)

logger = logging.getLogger(__name__)


def is_ordinary_stock(record):
    """The live catalog identifies shares with IRO-prefixed ISINs.

    IRR rows are rights offerings; IRT rows are funds and other tradable
    instruments. Keeping only IRO matches the provider's ordinary-share rows.
    """
    return str((record or {}).get("isin", "")).startswith("IRO")


def sync_provider_catalog(limit=None):
    """Sync provider catalog into MarketInstrument and InstrumentListingHistory.

    Optimized to query all date ranges and existing listings in batch to avoid N+1 queries.
    """
    logger.info("Starting provider catalog sync (limit=%s)...", limit)

    previous = {
        (row.source, row.symbol): row.eligible
        for row in MarketInstrument.objects.all()
    }
    
    # Fetch payloads
    stock_payload = fetch_all_symbols(settings.TSETMC_API_KEY)
    gold_payload = fetch_gold_currency_free(settings.BRS_API_KEY)
    
    # Bounded test mode slicing
    if limit is not None:
        if isinstance(stock_payload, list):
            stock_payload = stock_payload[:limit]
            logger.info("Bounded test mode: sliced stock_payload to %d items", len(stock_payload))
            
    rows = []

    for record in stock_payload if isinstance(stock_payload, list) else []:
        symbol = str(record.get("l18", "")).strip()
        if not symbol:
            continue
        eligible = is_ordinary_stock(record)
        rows.append(MarketInstrument(
            source=MarketInstrument.Source.TSETMC,
            symbol=symbol,
            name=str(record.get("l30", "") or ""),
            category=(
                MarketInstrument.Category.STOCK
                if eligible else MarketInstrument.Category.EXCLUDED
            ),
            provider_group=str(record.get("cs", "") or ""),
            isin=str(record.get("isin", "") or ""),
            eligible=eligible,
        ))

    gold_items = []
    if isinstance(gold_payload, dict):
        for provider_group, instruments in gold_payload.items():
            if not isinstance(instruments, list):
                continue
            for record in instruments:
                if not isinstance(record, dict):
                    continue
                symbol = str(record.get("symbol", "")).strip()
                if not symbol:
                    continue
                eligible = provider_group in ("gold", "currency") or symbol in ("USDT", "BTC")
                gold_items.append((symbol, record, provider_group, eligible))

    if limit is not None:
        gold_items = gold_items[:limit]
        logger.info("Bounded test mode: sliced gold_items to %d items", len(gold_items))

    for symbol, record, provider_group, eligible in gold_items:
        rows.append(MarketInstrument(
            source=MarketInstrument.Source.BRS,
            symbol=symbol,
            name=str(record.get("name", "") or ""),
            category=(
                MarketInstrument.Category.GOLD
                if eligible else MarketInstrument.Category.EXCLUDED
            ),
            provider_group=provider_group,
            eligible=eligible,
        ))

    if not rows:
        logger.info("No instruments found to sync.")
        return {"seen": 0, "eligible": 0}

    # Bulk create or update MarketInstrument
    logger.info("Bulk creating/updating %d MarketInstrument records...", len(rows))
    MarketInstrument.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["source", "symbol"],
        update_fields=[
            "name", "category", "provider_group", "isin", "eligible", "updated_at"
        ],
    )

    # Batch fetch bounds to avoid N+1 queries
    logger.info("Fetching stock candle date bounds...")
    stock_bounds = {
        row["symbol"]: (row["first"], row["last"])
        for row in MarketCandle.objects.filter(
            timeframe__in=(MarketCandle.ADJUSTED, MarketCandle.AGGREGATE),
            close_price__gt=0,
        )
        .values("symbol")
        .annotate(first=Min("date_time"), last=Max("date_time"))
    }

    logger.info("Fetching gold/currency date bounds...")
    gold_bounds = {
        row["symbol"]: (row["first"], row["last"])
        for row in GoldCurrencyHistory.objects.filter(close_price__gt=0)
        .values("symbol")
        .annotate(first=Min("date"), last=Max("date"))
    }

    # Batch fetch existing InstrumentListingHistory
    logger.info("Fetching existing InstrumentListingHistory mapping...")
    existing_histories = {
        h.symbol: h for h in InstrumentListingHistory.objects.all()
    }

    today = jalali.today()
    histories_to_update = []
    histories_to_create = []

    logger.info("Calculating listing history updates...")
    for idx, row in enumerate(rows):
        # Progress logging
        if (idx + 1) % 200 == 0 or (idx + 1) == len(rows):
            logger.info("Calculating bounds progress: %d/%d instruments", idx + 1, len(rows))

        if row.source == MarketInstrument.Source.TSETMC:
            first, last = stock_bounds.get(row.symbol, (None, None))
        else:
            first, last = gold_bounds.get(row.symbol, (None, None))

        first_seen = (first or today)[:10]
        last_seen = (last or today)[:10]

        history = existing_histories.get(row.symbol)
        created = False
        if not history:
            history = InstrumentListingHistory(
                symbol=row.symbol,
                first_seen=first_seen,
                last_seen=last_seen,
                eligible_from=first_seen if row.eligible else None,
            )
            created = True
        
        old_eligible = previous.get((row.source, row.symbol))
        history.first_seen = min(history.first_seen, first_seen)
        history.last_seen = max(history.last_seen, last_seen, today)
        
        if row.eligible and (created or old_eligible is False):
            history.eligible_from = today if old_eligible is False else first_seen
            history.eligible_to = None
        elif old_eligible is True and not row.eligible:
            history.eligible_to = today

        if created:
            histories_to_create.append(history)
        else:
            histories_to_update.append(history)

    # Bulk create new histories
    if histories_to_create:
        logger.info("Bulk creating %d new InstrumentListingHistory records...", len(histories_to_create))
        InstrumentListingHistory.objects.bulk_create(histories_to_create)

    # Bulk update existing histories
    if histories_to_update:
        logger.info("Bulk updating %d existing InstrumentListingHistory records...", len(histories_to_update))
        InstrumentListingHistory.objects.bulk_update(
            histories_to_update,
            fields=["first_seen", "last_seen", "eligible_from", "eligible_to"]
        )

    logger.info("Catalog sync completed successfully.")
    return {
        "seen": len(rows),
        "eligible": sum(row.eligible for row in rows),
    }
