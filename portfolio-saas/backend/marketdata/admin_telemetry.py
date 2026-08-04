import logging
import time
from django.conf import settings
from django.db import connection
from django.utils import timezone
from accounts.models import User
from portfolio.models import Account, Holding, Price, Snapshot, LedgerEntry
from marketdata.models import (
    ApiRequestQuota,
    ArchiveFetchState,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketInstrument,
    StockTransactionTick,
    CodalAnnouncement,
    ShareholderRecord,
)
from marketdata.quota import get_quota_status, quota_day

logger = logging.getLogger(__name__)

def get_cached_db_counts():
    from django.core.cache import cache
    cached = cache.get("db_counts_diagnostics")
    if cached:
        return cached

    models_map = {
        "accounts": Account,
        "holdings": Holding,
        "prices": Price,
        "snapshots": Snapshot,
        "ledger_entries": LedgerEntry,
        "stock_transaction_ticks": StockTransactionTick,
        "market_instruments": MarketInstrument,
        "stock_history_rows": DailyStockHistory,
        "gold_currency_rows": GoldCurrencyHistory,
        "candles": MarketCandle,
        "announcements": CodalAnnouncement,
        "shareholders": ShareholderRecord,
    }

    counts = {}
    with connection.cursor() as cursor:
        for key, model in models_map.items():
            table_name = model._meta.db_table
            try:
                cursor.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [table_name])
                row = cursor.fetchone()
                if row is not None and row[0] >= 0:
                    counts[key] = int(row[0])
                else:
                    counts[key] = model.objects.count()
            except Exception:
                counts[key] = model.objects.count()

    cache.set("db_counts_diagnostics", counts, 10)
    return counts

def get_admin_telemetry_context():
    """Generates the context for the custom admin dashboard, matching feature parity with AdminStatusView."""
    total_states = ArchiveFetchState.objects.count()
    complete_states = ArchiveFetchState.objects.filter(verified_complete=True).count()
    pending_states = max(0, total_states - complete_states)
    failures = ArchiveFetchState.objects.filter(consecutive_failures__gt=0).count()
    
    # User stats
    total_users = User.objects.count()
    staff_users = User.objects.filter(is_staff=True).count()
    pro_users = sum(1 for user in User.objects.all() if user.is_pro())
    
    # Celery workers status
    workers = {}
    try:
        from config.celery import app as celery_app
        inspect = celery_app.control.inspect(timeout=0.5)
        ping = inspect.ping()
        active = inspect.active()
        reserved = inspect.reserved()
        stats = inspect.stats()
        
        if ping:
            for worker_name in ping:
                workers[worker_name] = {
                    "status": "online",
                    "active_tasks": len(active.get(worker_name, []) or []) if active else 0,
                    "reserved_tasks": len(reserved.get(worker_name, []) or []) if reserved else 0,
                    "stats": stats.get(worker_name, {}) if stats else {},
                }
        else:
            workers = {"detail": "No active workers detected."}
    except Exception as e:
        workers = {"error": str(e)}

    # Category summaries
    category_summary = {}
    for ep_choice, ep_label in ArchiveFetchState.Endpoint.choices:
        states = ArchiveFetchState.objects.filter(endpoint=ep_choice)
        t_cnt = states.count()
        c_cnt = states.filter(verified_complete=True).count()
        f_cnt = states.filter(consecutive_failures__gt=0).count()
        category_summary[ep_choice] = {
            "label": ep_label,
            "total_states": t_cnt,
            "complete_states": c_cnt,
            "pending_states": max(0, t_cnt - c_cnt),
            "failed_states": f_cnt,
            "progress_pct": round((c_cnt / t_cnt * 100), 1) if t_cnt else 0,
        }

    # Quota status
    quota = quota_status = get_quota_status()
    
    # DB Counts
    db_counts = get_cached_db_counts()

    return {
        "archive": {
            "total_states": total_states,
            "complete_states": complete_states,
            "pending_states": pending_states,
            "failed_states": failures,
            "progress_pct": round((complete_states / total_states * 100), 1) if total_states else 0,
            "category_summary": category_summary,
        },
        "users": {
            "total": total_users,
            "staff": staff_users,
            "pro": pro_users,
        },
        "workers": workers,
        "quota": quota_status,
        "database": db_counts,
    }
