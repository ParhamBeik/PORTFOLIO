"""Celery tasks for the real-time price loop.

`fetch_and_publish` is the single heartbeat: fetch global prices, persist them,
snapshot every user's valuation, bust the cache, then broadcast the new price map
over Redis pub/sub. The fetch_prices management command calls the same body so
GitHub Actions (the dead-man's switch) and Celery beat stay in lockstep.
"""
import json
import logging
from decimal import Decimal

from celery import shared_task
from django.db import transaction

from accounts.models import User
from portfolio.models import Asset, Price, Snapshot
from portfolio.services import asset_value, invalidate_prices_cache
from portfolio.services.valuation import guard_price_map
from portfolio.live.extractor import extract_standard_prices
from portfolio.live.fetcher import api_settings_from_django, fetch_all_markets
from portfolio.live.pubsub import CHANNEL, get_redis

logger = logging.getLogger(__name__)


def run_price_fetch(*, dry_run=False, publish=True):
    """Fetch, persist, and (optionally) broadcast the latest price map.

    Network I/O and extraction stay OUTSIDE the transaction (C2 fix): only the
    writes are atomic, so a slow market API never holds an open DB connection.
    Returns {"priced": <float map>, "written": bool}.
    """
    raw = fetch_all_markets(api_settings_from_django())
    last = _last_price_by_asset_key()  # one DISTINCT ON query (H3), not N+1
    prices = extract_standard_prices(raw, last_prices=last)
    active_keys = set(Asset.objects.filter(is_active=True).values_list("key", flat=True))
    priced = guard_price_map({
        key: value
        for key, value in prices.items()
        if key in active_keys and float(value) > 0
    })
    public_priced = {key: float(value) for key, value in priced.items()}

    written = False
    if priced and not dry_run:
        with transaction.atomic():
            _write_prices(priced)
            _write_snapshots(priced)
        invalidate_prices_cache()
        # LAZY import: avoids a circular `portfolio.tasks -> portfolio.services.returns ->
        # portfolio.models` chain at module load. Outside the transaction on
        # purpose — cache deletes are not transactional.
        from portfolio.services.returns import invalidate_returns_cache
        invalidate_returns_cache()
        if publish:
            publish_prices(public_priced)
        written = True
    return {"priced": public_priced, "written": written}


def _last_price_by_asset_key() -> dict:
    """Newest stored price per asset in one query (H3: replaces a.prices.first() per asset)."""
    rows = (
        Price.objects.select_related("asset")
        .order_by("asset_id", "-fetched_at", "-id")
        .distinct("asset_id")
    )
    return {row.asset.key: row.price for row in rows}


def _write_prices(priced: dict) -> None:
    assets = {
        a.key: a
        for a in Asset.objects.filter(key__in=priced.keys(), is_active=True)
    }
    rows = [
        Price(asset=assets[key], price=value, source="API")
        for key, value in priced.items()
        if key in assets
    ]
    if rows:
        Price.objects.bulk_create(rows, batch_size=500)
        logger.info("Wrote %d price rows.", len(rows))


def _write_snapshots(priced: dict) -> None:
    """Snapshot each user's net worth in bulk.

    For every user we stamp TWO kinds of row so both the aggregate chart and each
    portfolio's own chart have history:
      * one account=None row = the whole-user total (mirrors the trade path);
      * one row per account = that portfolio's total.

    H4: prefetch users -> accounts -> holdings -> asset in two queries instead of
    a query per account/holding. Totals are computed in memory from `priced`
    (the prices just fetched/written), consistent with this fetch.
    """
    guarded_priced = guard_price_map(priced)
    prices = {k: Decimal(str(v)) for k, v in guarded_priced.items()}
    users = User.objects.prefetch_related("accounts__holdings__asset").iterator(chunk_size=1000)
    snapshots = []
    for user in users:
        user_total = Decimal("0")
        has_holdings = False
        for account in user.accounts.all():
            account_total = Decimal("0")
            for holding in account.holdings.all():
                has_holdings = True
                unit_price = prices.get(holding.asset.key)
                value = asset_value(holding, unit_price)
                account_total += value
            user_total += account_total
            if account_total > 0 or not has_holdings:
                snapshots.append(
                    Snapshot(user=user, account=account, total_value_tomans=account_total)
                )
        if user_total > 0 or not has_holdings:
            snapshots.append(Snapshot(user=user, account=None, total_value_tomans=user_total))
    if snapshots:
        Snapshot.objects.bulk_create(snapshots, batch_size=500)
        logger.info("Wrote %d net-worth snapshots.", len(snapshots))



def publish_prices(priced: dict) -> None:
    """Broadcast the price map to SSE subscribers. No-op without Redis."""
    client = get_redis()
    if client is None:
        return
    client.publish(CHANNEL, json.dumps(priced))
    logger.info("Published %d prices to %s.", len(priced), CHANNEL)


@shared_task(ignore_result=True)
def fetch_and_publish():
    """Celery entry point run by beat every 2 minutes."""
    result = run_price_fetch(publish=True)
    logger.info(
        "fetch_and_publish: %d prices, written=%s",
        len(result["priced"]), result["written"],
    )
    return result
