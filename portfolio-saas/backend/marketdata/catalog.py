"""Provider catalog synchronization and eligibility classification."""
from django.conf import settings
from django.db.models import Max, Min

from .fetchers import fetch_all_symbols, fetch_gold_currency_free
from . import jalali
from .models import (
    GoldCurrencyHistory,
    InstrumentListingHistory,
    MarketCandle,
    MarketInstrument,
)


def is_ordinary_stock(record):
    """The live catalog identifies shares with IRO-prefixed ISINs.

    IRR rows are rights offerings; IRT rows are funds and other tradable
    instruments. Keeping only IRO matches the provider's ordinary-share rows.
    """
    return str((record or {}).get("isin", "")).startswith("IRO")


def sync_provider_catalog():
    previous = {
        (row.source, row.symbol): row.eligible
        for row in MarketInstrument.objects.all()
    }
    stock_payload = fetch_all_symbols(settings.TSETMC_API_KEY)
    gold_payload = fetch_gold_currency_free(settings.BRS_API_KEY)
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

    if rows:
        MarketInstrument.objects.bulk_create(
            rows,
            update_conflicts=True,
            unique_fields=["source", "symbol"],
            update_fields=[
                "name", "category", "provider_group", "isin", "eligible", "updated_at"
            ],
        )
        today = jalali.today()
        for row in rows:
            if row.source == MarketInstrument.Source.TSETMC:
                bounds = MarketCandle.objects.filter(symbol=row.symbol).aggregate(
                    first=Min("date_time"), last=Max("date_time")
                )
            else:
                bounds = GoldCurrencyHistory.objects.filter(
                    symbol=row.symbol
                ).aggregate(first=Min("date"), last=Max("date"))
            first_seen = (bounds["first"] or today)[:10]
            last_seen = (bounds["last"] or today)[:10]
            history, created = InstrumentListingHistory.objects.get_or_create(
                symbol=row.symbol,
                defaults={
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "eligible_from": first_seen if row.eligible else None,
                },
            )
            old_eligible = previous.get((row.source, row.symbol))
            history.first_seen = min(history.first_seen, first_seen)
            history.last_seen = max(history.last_seen, last_seen, today)
            if row.eligible and (created or old_eligible is False):
                history.eligible_from = today if old_eligible is False else first_seen
                history.eligible_to = None
            elif old_eligible is True and not row.eligible:
                history.eligible_to = today
            history.save()
    return {
        "seen": len(rows),
        "eligible": sum(row.eligible for row in rows),
    }
