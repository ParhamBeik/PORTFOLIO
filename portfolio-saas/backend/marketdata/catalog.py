"""Provider catalog synchronization and eligibility classification."""
import logging
from django.conf import settings
from django.db.models import Max, Min
from django.utils import timezone

from .currency import canonical_symbol
from .fetchers import fetch_all_symbols, fetch_derivatives, fetch_gold_currency_free
from .ingest import flatten_records
from . import jalali
from .models import (
    GoldCurrencyHistory,
    InstrumentListingHistory,
    MarketCandle,
    MarketDailyBar,
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

    rows = []

    for record in stock_payload if isinstance(stock_payload, list) else []:
        symbol = str(record.get("l18", "")).strip()
        if not symbol:
            continue
        isin = str(record.get("isin", "") or "")
        # IRT-prefixed ISINs are exchange-traded funds. They used to fall
        # through to EXCLUDED here, which left MarketInstrument with zero ETF
        # rows. Nav.php is not a discovery source (and is no longer polled).
        is_etf = isin.startswith("IRT")
        ordinary = is_ordinary_stock(record)
        eligible = ordinary or is_etf
        category = (
            MarketInstrument.Category.STOCK if ordinary
            else MarketInstrument.Category.ETF if is_etf
            else MarketInstrument.Category.EXCLUDED
        )
        rows.append(MarketInstrument(
            source=MarketInstrument.Source.TSETMC,
            symbol=symbol,
            name=str(record.get("l30", "") or ""),
            category=category,
            provider_group=str(record.get("cs", "") or ""),
            isin=isin,
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

    # Crypto/commodity: dead-until-recently endpoints, now the live-poll source
    # for MarketSnapshot too. Each returns its whole universe in one call, so
    # catalog discovery and the live capture task share a payload shape (see
    # flatten_records) -- no separate discovery endpoint needed.
    #
    # Funds are discovered above from IRT-ISIN rows in all_symbols, not here.
    for asset_class, endpoint_key, api_key, category in (
        ("crypto", "crypto", settings.BRS_API_KEY, MarketInstrument.Category.CRYPTO),
        ("commodity", "commodity", settings.BRS_API_KEY, MarketInstrument.Category.COMMODITY),
    ):
        payload = fetch_derivatives(api_key, endpoint_key)
        records = flatten_records(payload)
        if limit is not None:
            records = records[:limit]
        for record in records:
            symbol = str(
                record.get("symbol") or record.get("l18") or record.get("code") or ""
            ).strip()
            if not symbol:
                continue
            if asset_class in ("crypto", "commodity"):
                symbol = canonical_symbol(symbol)
            rows.append(MarketInstrument(
                source=MarketInstrument.Source.BRS,
                symbol=symbol[:64],
                name=str(record.get("name") or record.get("l30") or ""),
                category=category,
                provider_group=asset_class,
                eligible=True,
            ))

    if not rows:
        return {"seen": 0, "eligible": 0}

    # Bulk create or update MarketInstrument
    MarketInstrument.objects.bulk_create(
        rows,
        update_conflicts=True,
        unique_fields=["source", "symbol"],
        update_fields=[
            "name", "category", "provider_group", "isin", "eligible", "updated_at"
        ],
    )

    # Batch fetch bounds to avoid N+1 queries
    stock_bounds = {
        row["symbol"]: (row["first"], row["last"])
        for row in MarketCandle.objects.filter(
            timeframe=MarketCandle.ADJUSTED,
            close_price__gt=0,
        )
        .values("symbol")
        .annotate(first=Min("date_time"), last=Max("date_time"))
    }

    gold_bounds = {
        row["symbol"]: (row["first"], row["last"])
        for row in GoldCurrencyHistory.objects.filter(close_price__gt=0)
        .values("symbol")
        .annotate(first=Min("date"), last=Max("date"))
    }

    # Crypto/commodity/ETF NAV have no dedicated history table -- MarketDailyBar
    # (built from MarketSnapshot by aggregate_market_daily_bars) is their only
    # source of a first/last-seen date range.
    daily_bar_bounds = {
        (row["asset_class"], row["symbol"]): (row["first"], row["last"])
        for row in MarketDailyBar.objects.filter(
            asset_class__in=("crypto", "commodity", "etf_nav")
        )
        .values("asset_class", "symbol")
        .annotate(first=Min("date"), last=Max("date"))
    }

    # Batch fetch existing InstrumentListingHistory
    existing_histories = {
        h.symbol: h for h in InstrumentListingHistory.objects.all()
    }

    today = jalali.today()
    histories_to_update = []
    histories_to_create = []

    for row in rows:
        if row.source == MarketInstrument.Source.TSETMC:
            first, last = stock_bounds.get(row.symbol, (None, None))
        elif row.provider_group in ("crypto", "commodity", "etf_nav"):
            first, last = daily_bar_bounds.get((row.provider_group, row.symbol), (None, None))
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

    if histories_to_create:
        InstrumentListingHistory.objects.bulk_create(histories_to_create)

    if histories_to_update:
        InstrumentListingHistory.objects.bulk_update(
            histories_to_update,
            fields=["first_seen", "last_seen", "eligible_from", "eligible_to"]
        )

    return {
        "seen": len(rows),
        "eligible": sum(row.eligible for row in rows),
    }
