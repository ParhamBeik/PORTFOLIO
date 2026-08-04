"""Read-only extraction of stored financial data for the cross-source audit.

Runs INSIDE the backend container (which reaches the authoritative Docker
Postgres as host `db`). Pipe it in so the repo worktree is never mounted rw:

    docker exec -i portfolio-saas-backend-1 python - < audit_db.py > db_facts.json

Safety contract:
  * SELECT only. No .save(), .create(), .update(), .delete(), no migrations,
    no Celery task invocation anywhere in this file.
  * Refuses to run unless it is talking to the Docker Postgres service. The
    host machine also runs a Postgres on 5432; querying that one would produce
    confident numbers about the wrong database.
  * Emits JSON on stdout only. Nothing is written to disk from in here.
"""
import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app")
django.setup()

from django.conf import settings
from django.db import connection
from django.db.models import Sum

from marketdata.models import (
    ApiRequestQuota,
    CorporateAction,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    RejectedRecord,
)
from portfolio.models import Asset

# The sample. Every instrument and every date below was confirmed present in the
# authoritative database before being written here; `stored` coming back null in
# the output therefore means a real absence, not a typo.
#
# 1405-05-13 is the current Jalali day and is deliberately absent everywhere: an
# in-progress session is not a complete daily close.
TSE_SAMPLE = {
    "کاما": {
        "reason": (
            "The only Asset.tse_symbol in user-land, so it is the single TSE "
            "symbol that reaches live valuation. Deepest history (8,174 adjusted "
            "candles) and carries a visible adjustment step."
        ),
        "dates": [
            ("1405-05-10", "most recent complete stored adjusted close"),
            ("1405-04-29", "adjustment-sensitive: first date after adj/unadj reconverge"),
            ("1405-04-16", "adjustment-sensitive: last date where adj/unadj diverge"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
            ("1400-05-10", "deep historical date"),
        ],
    },
    "دجابر": {
        "reason": "Second-deepest TSE history (6,697 adjusted candles); independent comparator.",
        "dates": [
            ("1405-05-11", "most recent complete stored adjusted close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
        ],
    },
    "وغدیر": {
        "reason": (
            "Liquid holding company, most current stored close of the three "
            "(1405-05-12) — tests whether the freshest row matches the provider."
        ),
        "dates": [
            ("1405-05-12", "most recent complete stored adjusted close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
        ],
    },
}

BRS_SAMPLE = {
    "IR_COIN_EMAMI": {
        "reason": "Portfolio asset (emami_coin). Coin-unit instrument, 5,964 rows back to 1389.",
        "dates": [
            ("1405-05-12", "most recent complete stored close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
            ("1400-05-10", "deep historical date"),
        ],
    },
    "IR_GOLD_18K": {
        "reason": "Portfolio asset (gold_18k_gram). Gram-unit, so it fails loudly if coin/gram units are swapped.",
        "dates": [
            ("1405-05-12", "most recent complete stored close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
        ],
    },
    "USD": {
        "reason": (
            "Portfolio asset (usd_cash) AND the conversion rate every USD-quoted "
            "asset is multiplied by in returns._convert_usd_to_toman. If this is "
            "wrong, BTC and gold-ounce returns are wrong too."
        ),
        "dates": [
            ("1405-05-12", "most recent complete stored close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
            ("1400-05-10", "deep historical date"),
        ],
    },
    "USDT_IRT": {
        "reason": (
            "Tests whether USDT is priced independently in Toman or routed "
            "through USD. Stored series shows a 10x scale break at 1405-05-11, "
            "so both regimes are sampled."
        ),
        "dates": [
            ("1405-05-12", "most recent complete stored close (post-break regime)"),
            ("1405-05-11", "the break date itself"),
            ("1405-05-10", "last row of the pre-break regime"),
            ("1404-12-06", "ordinary historical date (pre-break regime)"),
            ("1403-06-15", "older historical date (pre-break regime)"),
        ],
    },
    "XAUUSD": {
        "reason": (
            "Stored with unit='دلار' (USD) inside a table whose other rows are "
            "Toman — the currency-conversion boundary case."
        ),
        "dates": [
            ("1405-05-12", "most recent complete stored close"),
            ("1404-12-06", "ordinary historical date"),
            ("1403-06-15", "older historical date"),
        ],
    },
    "SEK": {
        "reason": (
            "Known integrity concern: 1405-04-31 close is 96,150 against "
            "neighbours near 20,000. Sampled to test whether corrupt rows are "
            "isolated from valuation."
        ),
        "dates": [
            ("1405-05-12", "most recent complete stored close"),
            ("1405-04-31", "the suspect row"),
            ("1405-04-30", "the day before the suspect row"),
            ("1404-12-06", "ordinary historical date"),
        ],
    },
}


def assert_authoritative_db():
    """Refuse to audit anything but the Docker Postgres service."""
    db = settings.DATABASES["default"]
    host = db.get("HOST", "")
    if host not in ("db", "postgres"):
        sys.exit(
            f"REFUSING TO RUN: DB HOST is {host!r}, expected the Docker service "
            "'db'. The host machine's local Postgres must not be audited."
        )
    with connection.cursor() as cur:
        cur.execute(
            "SELECT current_database(), current_user, "
            "pg_size_pretty(pg_database_size(current_database())), version()"
        )
        name, user, size, version = cur.fetchone()
        cur.execute("SELECT inet_server_addr()::text")
        server_addr = cur.fetchone()[0]
    return {
        "database": name,
        "user": user,
        "size": size,
        "server_addr": server_addr,
        "postgres_version": version.split(" on ")[0],
        "django_db_host": host,
    }


def tse_stored(symbol, date):
    """Stored TSE facts for one symbol/day, across every table that holds a close."""
    # Both date spellings, because MarketCandle stores the same logical day as
    # either '1405-05-09' or '1405-05-09 00:00:00'.
    variants = [date, f"{date} 00:00:00"]
    out = {}
    for label, timeframe in (
        ("unadjusted", MarketCandle.UNADJUSTED),
        ("adjusted", MarketCandle.ADJUSTED),
        ("aggregate", MarketCandle.AGGREGATE),
    ):
        row = (
            MarketCandle.objects.filter(
                symbol=symbol, timeframe=timeframe, date_time__in=variants
            )
            .values("date_time", "open_price", "high_price", "low_price", "close_price", "volume")
            .first()
        )
        out[label] = (
            {
                "stored_date_literal": row["date_time"],
                "open": str(row["open_price"]),
                "high": str(row["high_price"]),
                "low": str(row["low_price"]),
                "close": str(row["close_price"]),
                "volume": row["volume"],
            }
            if row
            else None
        )

    hist = (
        DailyStockHistory.objects.filter(symbol=symbol, date=date, is_adjusted=False)
        .values("pc", "pl", "pmin", "pmax", "tvol", "tval")
        .first()
    )
    if hist:
        tvol, tval = hist["tvol"], hist["tval"]
        # tval/tvol is the provider's own value-weighted average price. It shares
        # whatever unit the provider quotes prices in, so agreement with pc
        # proves price and value are internally consistent -- it CANNOT
        # distinguish Rial from Toman, since both sides scale together.
        implied = (float(tval) / tvol) if tvol else None
        out["daily_history_unadjusted"] = {
            "pc_close": str(hist["pc"]),
            "pl_last": str(hist["pl"]),
            "pmin": str(hist["pmin"]),
            "pmax": str(hist["pmax"]),
            "tvol": tvol,
            "tval": tval,
            "implied_avg_price_from_value": round(implied, 4) if implied else None,
            "implied_over_pc": (
                round(implied / float(hist["pc"]), 6)
                if implied and float(hist["pc"])
                else None
            ),
        }
    else:
        out["daily_history_unadjusted"] = None

    adj, unadj = out["adjusted"], out["unadjusted"]
    if adj and unadj and float(unadj["close"]):
        out["adjustment_factor"] = round(float(adj["close"]) / float(unadj["close"]), 6)
    else:
        out["adjustment_factor"] = None

    out["rejections"] = list(
        RejectedRecord.objects.filter(symbol=symbol, date=date).values(
            "endpoint", "reason", "occurrences"
        )
    )
    return out


def brs_stored(symbol, date):
    rows = list(
        GoldCurrencyHistory.objects.filter(symbol=symbol, date=date).values(
            "name", "unit", "open_price", "high_price", "low_price", "close_price"
        )
    )
    return {
        "rows": [
            {
                "name": r["name"],
                "unit": r["unit"],
                "open": str(r["open_price"]),
                "high": str(r["high_price"]),
                "low": str(r["low_price"]),
                "close": str(r["close_price"]),
            }
            for r in rows
        ],
        "rejections": list(
            RejectedRecord.objects.filter(symbol=symbol, date=date).values(
                "endpoint", "reason", "occurrences"
            )
        ),
    }


def instrument_mapping():
    """How user-land assets bind to provider symbols."""
    return [
        {
            "asset_key": a.key,
            "name": a.name,
            "asset_class": a.asset_class,
            "declared_currency": a.currency,
            "tse_symbol": a.tse_symbol,
            "brs_symbol": a.brs_symbol,
            "is_active": a.is_active,
            "is_house": a.is_house,
        }
        for a in Asset.objects.all().order_by("asset_class", "key")
    ]


def integrity_observations():
    """Warehouse-wide observations the sample alone would not reveal."""
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM marketdata_marketcandle WHERE date_time LIKE '%% %%'"
        )
        suffixed = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM marketdata_marketcandle")
        candles_total = cur.fetchone()[0]
        # A day stored under BOTH spellings would mean genuinely duplicated data.
        cur.execute(
            "SELECT count(*) FROM (SELECT symbol, timeframe, split_part(date_time,' ',1) d "
            "FROM marketdata_marketcandle GROUP BY 1,2,3 HAVING count(*) > 1) x"
        )
        dup_days = cur.fetchone()[0]
        # Units present in a table documented as holding Tomans.
        cur.execute(
            "SELECT unit, count(*) FROM marketdata_goldcurrencyhistory "
            "GROUP BY unit ORDER BY 2 DESC"
        )
        units = [{"unit": u, "rows": n} for u, n in cur.fetchall()]
        # Currency symbols whose maximum close is a multiple of their typical
        # level -- the signature of the SEK 1405-04-31 defect.
        cur.execute(
            """
            SELECT symbol,
                   MAX(close_price) AS max_close,
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY close_price) AS median_close,
                   ROUND((MAX(close_price) / NULLIF(percentile_cont(0.5)
                       WITHIN GROUP (ORDER BY close_price), 0))::numeric, 2) AS max_over_median
            FROM marketdata_goldcurrencyhistory
            GROUP BY symbol
            HAVING MAX(close_price) / NULLIF(percentile_cont(0.5)
                       WITHIN GROUP (ORDER BY close_price), 0) > 3
            ORDER BY 4 DESC
            """
        )
        spikes = [
            {
                "symbol": s,
                "max_close": str(mx),
                "median_close": str(med),
                "max_over_median": str(ratio),
            }
            for s, mx, med, ratio in cur.fetchall()
        ]

    return {
        "market_candle_rows": candles_total,
        "market_candle_rows_with_time_suffix": suffixed,
        "days_stored_under_both_date_spellings": dup_days,
        "gold_currency_units": units,
        "gold_currency_symbols_max_over_3x_median": spikes,
        "corporate_action_rows": CorporateAction.objects.count(),
        "rejected_records_by_endpoint_reason": list(
            RejectedRecord.objects.values("endpoint", "reason")
            .annotate(occurrences=Sum("occurrences"))
            .order_by("-occurrences")
        ),
        "series_screen_rejections": RejectedRecord.objects.filter(
            endpoint__startswith="series:"
        ).count(),
        "quota_recent": list(
            ApiRequestQuota.objects.order_by("-day").values(
                "day", "limit", "used", "archive_used", "live_used", "other_used"
            )[:2]
        ),
    }


def main():
    payload = {
        "database_identity": assert_authoritative_db(),
        "instrument_mapping": instrument_mapping(),
        "integrity_observations": integrity_observations(),
        "tse": {},
        "brs": {},
        "sample_rationale": {
            **{s: v["reason"] for s, v in TSE_SAMPLE.items()},
            **{s: v["reason"] for s, v in BRS_SAMPLE.items()},
        },
    }
    for symbol, spec in TSE_SAMPLE.items():
        payload["tse"][symbol] = {
            date: {"why_this_date": why, **tse_stored(symbol, date)}
            for date, why in spec["dates"]
        }
    for symbol, spec in BRS_SAMPLE.items():
        payload["brs"][symbol] = {
            date: {"why_this_date": why, **brs_stored(symbol, date)}
            for date, why in spec["dates"]
        }
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
