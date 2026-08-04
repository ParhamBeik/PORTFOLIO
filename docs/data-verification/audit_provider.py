"""Bounded live fetch of ORIGINAL provider values for the cross-source audit.

Runs INSIDE the backend container so it reuses the project's own fetchers --
the point of the audit is to compare against what the provider actually says,
retrieved the same way ingestion retrieves it:

    docker exec -i portfolio-saas-backend-1 python - < audit_provider.py > provider_facts.json

Cost: one request per (symbol, endpoint) pair, ~25 total. Every endpoint used
here is HISTORICAL_FULL, i.e. one request returns the entire series, so no
date-walking is needed or performed.

Safety contract:
  * Read-only against the provider. No ingest_* function is called, so nothing
    this script fetches can reach the database.
  * The API key is never emitted. `params_redacted` replaces it with '***', and
    only the SHA-256 of each raw response body is recorded -- never the body.
  * No Celery task is invoked and no unrestricted backfill is triggered.
"""
import hashlib
import json
import os
import sys

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
sys.path.insert(0, "/app")
django.setup()

from django.conf import settings

from marketdata import endpoints
from marketdata.fetchers.base import fetch_json

# Mirrors audit_db.py. Kept as a literal rather than imported so this file can
# run standalone via stdin, where there is no module to import from.
TSE_SYMBOLS = ["کاما", "دجابر", "وغدیر"]
TSE_DATES = {
    "کاما": ["1405-05-10", "1405-04-29", "1405-04-16", "1404-12-06", "1403-06-15", "1400-05-10"],
    "دجابر": ["1405-05-11", "1404-12-06", "1403-06-15"],
    "وغدیر": ["1405-05-12", "1404-12-06", "1403-06-15"],
}
# USDT is requested as 'USDT' -- the provider canonicalizes it to USDT_IRT on
# the way back, which is exactly the mapping this audit needs to confirm.
BRS_SYMBOLS = ["IR_COIN_EMAMI", "IR_GOLD_18K", "USD", "USDT", "XAUUSD", "SEK"]
BRS_DATES = {
    "IR_COIN_EMAMI": ["1405-05-12", "1404-12-06", "1403-06-15", "1400-05-10"],
    "IR_GOLD_18K": ["1405-05-12", "1404-12-06", "1403-06-15"],
    "USD": ["1405-05-12", "1404-12-06", "1403-06-15", "1400-05-10"],
    "USDT": ["1405-05-12", "1405-05-11", "1405-05-10", "1404-12-06", "1403-06-15"],
    "XAUUSD": ["1405-05-12", "1404-12-06", "1403-06-15"],
    "SEK": ["1405-05-12", "1405-04-31", "1405-04-30", "1404-12-06"],
}

HASHES = []


def record(endpoint_key, params, payload):
    """Hash the payload and log the retrieval, with the key redacted."""
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    HASHES.append(
        {
            "endpoint_key": endpoint_key,
            "url": endpoints.get(endpoint_key).url,
            "params_redacted": {
                k: ("***" if k == "key" else v) for k, v in params.items()
            },
            "response_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
            "response_bytes": len(body.encode("utf-8")),
            "top_level_type": type(payload).__name__,
            "record_count": (
                len(payload)
                if isinstance(payload, list)
                else (
                    len(payload.get("history_daily", []))
                    if isinstance(payload, dict) and "history_daily" in payload
                    else None
                )
            ),
        }
    )


def norm(value):
    """Provider dates arrive as '1405/05/10' or '1405-05-10'; compare on one form."""
    return str(value or "").strip().replace("/", "-").split(" ")[0]


def fetch_candles(symbol, candle_type):
    endpoint = endpoints.get("stock_candles")
    params = {"key": settings.BRS_API_KEY, "type": str(candle_type), "l18": symbol}
    payload = fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)
    record("stock_candles", params, payload)
    records = {}
    if isinstance(payload, dict):
        for container in ("candle_daily", "candle_daily_adjusted", "candle_intraday"):
            rows = payload.get(container)
            if isinstance(rows, list):
                for rec in rows:
                    if isinstance(rec, dict) and rec.get("date"):
                        records[norm(rec["date"])] = rec
                break
    return records


def fetch_history(symbol):
    endpoint = endpoints.get("stock_history")
    params = {"key": settings.BRS_API_KEY, "type": "0", "l18": symbol}
    payload = fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)
    record("stock_history", params, payload)
    rows = {}
    if isinstance(payload, list):
        for rec in payload:
            if isinstance(rec, dict) and rec.get("date"):
                rows[norm(rec["date"])] = rec
    return rows


def fetch_gold(symbol):
    endpoint = endpoints.get("gold_currency_history")
    params = {"key": settings.BRS_API_KEY, "history": "2", "symbol": symbol}
    payload = fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)
    record("gold_currency_history", params, payload)
    meta = {
        "provider_symbol": (payload or {}).get("symbol") if isinstance(payload, dict) else None,
        "provider_name": (payload or {}).get("name") if isinstance(payload, dict) else None,
        "provider_unit": (payload or {}).get("unit") if isinstance(payload, dict) else None,
    }
    rows = {}
    if isinstance(payload, dict) and isinstance(payload.get("history_daily"), list):
        for rec in payload["history_daily"]:
            if isinstance(rec, dict) and rec.get("date"):
                rows[norm(rec["date"])] = rec
    return meta, rows


def fetch_live_snapshot():
    """The live free endpoint, to read each symbol's declared unit as sent today."""
    endpoint = endpoints.get("gold_currency_free")
    params = {"key": settings.BRS_API_KEY}
    payload = fetch_json(endpoint.url, params=params, quota_bucket=endpoint.bucket)
    record("gold_currency_free", params, payload)
    out = {}
    if isinstance(payload, dict):
        for group, items in payload.items():
            if not isinstance(items, list):
                continue
            for rec in items:
                if isinstance(rec, dict) and rec.get("symbol"):
                    out[str(rec["symbol"])] = {
                        "provider_group": group,
                        "name": rec.get("name"),
                        "unit": rec.get("unit"),
                        "price": rec.get("price"),
                        "date": rec.get("date"),
                        "time": rec.get("time"),
                    }
    return out


def main():
    if not settings.BRS_API_KEY:
        sys.exit("REFUSING TO RUN: BRS_API_KEY is empty; no provider comparison possible.")

    result = {"tse": {}, "brs": {}, "live_snapshot": {}, "provider_hashes": []}

    for symbol in TSE_SYMBOLS:
        unadj = fetch_candles(symbol, 2)
        adj = fetch_candles(symbol, 3)
        hist = fetch_history(symbol)
        per_date = {}
        for date in TSE_DATES[symbol]:
            u, a, h = unadj.get(date), adj.get(date), hist.get(date)
            per_date[date] = {
                "candle_unadjusted": u,
                "candle_adjusted": a,
                "daily_history_type0": h,
                "provider_date_literal_unadj": (u or {}).get("date"),
                "provider_date_literal_adj": (a or {}).get("date"),
                "provider_date_literal_hist": (h or {}).get("date"),
            }
        result["tse"][symbol] = {
            "series_counts": {
                "candle_unadjusted_rows": len(unadj),
                "candle_adjusted_rows": len(adj),
                "daily_history_rows": len(hist),
            },
            "dates": per_date,
        }

    for symbol in BRS_SYMBOLS:
        meta, rows = fetch_gold(symbol)
        result["brs"][symbol] = {
            "provider_metadata": meta,
            "series_rows": len(rows),
            "dates": {d: rows.get(d) for d in BRS_DATES[symbol]},
        }

    result["live_snapshot"] = fetch_live_snapshot()
    result["provider_hashes"] = HASHES
    result["request_count"] = len(HASHES)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    main()
