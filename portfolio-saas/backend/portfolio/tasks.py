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
    redis_client = get_redis()
    lock_key = "lock:price_fetch"
    lock_acquired = False
    if redis_client and not dry_run:
        # SETNX to acquire the lock with a 90-second TTL
        lock_acquired = redis_client.set(lock_key, "1", ex=90, nx=True)
        if not lock_acquired:
            logger.warning("Another price fetch is already running (failed to acquire Redis lock). Skipping.")
            return {"priced": {}, "written": False}

    try:
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
    finally:
        if lock_acquired and redis_client:
            redis_client.delete(lock_key)


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


from datetime import timedelta
from django.utils import timezone


def _write_snapshots(priced: dict) -> None:
    """Snapshot each active user's net worth in bulk and fill downtime gaps safely."""
    guarded_priced = guard_price_map(priced)
    prices = {k: Decimal(str(v)) for k, v in guarded_priced.items()}
    now = timezone.now()

    # 1. Detect downtime gaps (> 4 minutes since last snapshot), capped to 60 intervals (2 hours) per run
    last_snap_time = (
        Snapshot.objects.order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    gap_timestamps = []
    if last_snap_time and (now - last_snap_time).total_seconds() > 240:
        gap_start = max(last_snap_time + timedelta(minutes=2), now - timedelta(hours=2))
        slot = gap_start
        while slot < now - timedelta(seconds=90) and len(gap_timestamps) < 60:
            gap_timestamps.append(slot)
            slot += timedelta(minutes=2)
        if gap_timestamps:
            logger.info(
                "[DOWNTIME_GAP_FILLED] Backfilling %d missing 2-minute interval snapshots from %s to %s",
                len(gap_timestamps), gap_timestamps[0].isoformat(), gap_timestamps[-1].isoformat()
            )

    # 2. Only snapshot users who actually have accounts
    active_users_qs = (
        User.objects.filter(accounts__isnull=False)
        .distinct()
        .prefetch_related("accounts__holdings__asset")
    )

    total_written = 0
    chunk_size = 200
    user_batch = []

    for user in active_users_qs.iterator(chunk_size=chunk_size):
        user_batch.append(user)
        if len(user_batch) >= chunk_size:
            total_written += _flush_user_snapshots(user_batch, prices, gap_timestamps)
            user_batch = []

    if user_batch:
        total_written += _flush_user_snapshots(user_batch, prices, gap_timestamps)

    logger.info("Wrote %d net-worth snapshots total.", total_written)


def _flush_user_snapshots(users: list, prices: dict, gap_timestamps: list) -> int:
    """Generate and bulk_create snapshots for a small batch of users."""
    snapshots = []

    def _build_batch_snapshots(ts=None):
        batch_snaps = []
        is_est = True if ts is not None else False
        for user in users:
            user_total = Decimal("0")
            for account in user.accounts.all():
                account_total = Decimal("0")
                for holding in account.holdings.all():
                    unit_price = prices.get(holding.asset.key)
                    value = asset_value(holding, unit_price)
                    account_total += value
                user_total += account_total
                snap = Snapshot(user=user, account=account, total_value_tomans=account_total, is_estimated=is_est)
                if ts:
                    snap.timestamp = ts
                batch_snaps.append(snap)
            snap = Snapshot(user=user, account=None, total_value_tomans=user_total, is_estimated=is_est)
            if ts:
                snap.timestamp = ts
            batch_snaps.append(snap)
        return batch_snaps

    for gap_ts in gap_timestamps:
        snapshots.extend(_build_batch_snapshots(ts=gap_ts))

    snapshots.extend(_build_batch_snapshots(ts=None))

    if snapshots:
        Snapshot.objects.bulk_create(snapshots, batch_size=500)
    return len(snapshots)




def publish_prices(priced: dict) -> None:
    """Broadcast the price map to SSE subscribers. No-op without Redis."""
    client = get_redis()
    if client is None:
        return
    client.publish(CHANNEL, json.dumps(priced))
    logger.info("Published %d prices to %s.", len(priced), CHANNEL)


@shared_task(
    ignore_result=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def fetch_and_publish():
    """Celery entry point run by beat every 2 minutes."""
    result = run_price_fetch(publish=True)
    logger.info(
        "fetch_and_publish: %d prices, written=%s",
        len(result["priced"]), result["written"],
    )
    return result
