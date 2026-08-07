"""Repair missing KAMA stock prices in snapshot history.

The daily pipeline calls this as a best-effort cleanup. It looks up historical
KAMA prices from TSETMC and rewrites only snapshots where KAMA was missing.
"""

import argparse
import json
import os
import sys
from datetime import date, datetime

from fetcher import (
    DEFAULT_TSETMC_HISTORY_URL,
    KAMA_SYMBOL,
    extract_price,
    fetch_tsetmc_history,
)
from utils import load_json, save_json, log_step

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
CACHE_PATH = os.path.join(DATA_DIR, "latest_prices_cache.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history_snapshots.jsonl")


# --- Date and price helpers --------------------------------------------------
def _gregorian_to_jalali(gregorian_date):
    """Convert a Gregorian date to Jalali YYYY-MM-DD without extra packages."""
    gregorian_days_in_month = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    jalali_days_in_month = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]

    gy = gregorian_date.year - 1600
    gm = gregorian_date.month - 1
    gd = gregorian_date.day - 1

    day_number = 365 * gy + (gy + 3) // 4 - (gy + 99) // 100 + (gy + 399) // 400
    day_number += sum(gregorian_days_in_month[:gm]) + gd
    if gm > 1 and ((gy + 1600) % 4 == 0 and ((gy + 1600) % 100 != 0 or (gy + 1600) % 400 == 0)):
        day_number += 1

    jalali_day_number = day_number - 79
    jalali_cycle = jalali_day_number // 12053
    jalali_day_number %= 12053

    jy = 979 + 33 * jalali_cycle + 4 * (jalali_day_number // 1461)
    jalali_day_number %= 1461

    if jalali_day_number >= 366:
        jy += (jalali_day_number - 1) // 365
        jalali_day_number = (jalali_day_number - 1) % 365

    jm = 0
    while jm < 11 and jalali_day_number >= jalali_days_in_month[jm]:
        jalali_day_number -= jalali_days_in_month[jm]
        jm += 1

    return f"{jy:04d}-{jm + 1:02d}-{jalali_day_number + 1:02d}"


def _parse_snapshot_date(timestamp):
    try:
        return datetime.fromisoformat(str(timestamp)).date()
    except (TypeError, ValueError):
        return None


# --- JSONL history helpers ---------------------------------------------------
def _history_rows(payload):
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        rows = []
        for value in payload.values():
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, dict))
        return rows or [payload]
    return []


def _load_jsonl(path):
    records = []
    with open(path, "r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                records.append((line_number, json.loads(text)))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc}") from exc
    return records


def _save_jsonl(path, numbered_records):
    with open(path, "w", encoding="utf-8") as file:
        for _, record in numbered_records:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


def _build_history_price_map(history_payload):
    prices = {}
    for record in _history_rows(history_payload):
        history_date = str(record.get("date") or "").strip()
        price = extract_price(record)
        if history_date and price > 0:
            prices[history_date] = price
    return prices


# --- Repair logic ------------------------------------------------------------
def _nearest_price_on_or_before(history_prices, jalali_date):
    if jalali_date in history_prices:
        return jalali_date, history_prices[jalali_date]
    earlier_dates = [history_date for history_date in history_prices if history_date <= jalali_date]
    if not earlier_dates:
        return None, 0
    nearest_date = max(earlier_dates)
    return nearest_date, history_prices[nearest_date]


def _repair_record(record, kama_price):
    snapshot = record.get("snapshot")
    if not isinstance(snapshot, dict):
        return False, 0

    prices = snapshot.setdefault("prices", {})
    old_price = extract_price({"pl": prices.get("kama_stock")})
    if old_price > 0:
        return False, 0

    prices["kama_stock"] = kama_price
    total_delta_by_owner = {}

    portfolios = snapshot.get("portfolios", {})
    if isinstance(portfolios, dict):
        for owner, assets in portfolios.items():
            if not isinstance(assets, dict):
                continue
            kama_asset = assets.get("kama_stock")
            if not isinstance(kama_asset, dict):
                continue
            quantity = kama_asset.get("quantity", 0)
            if not isinstance(quantity, (int, float)) or isinstance(quantity, bool):
                quantity = 0
            old_total = kama_asset.get("total_value_tomans", 0)
            if not isinstance(old_total, (int, float)) or isinstance(old_total, bool):
                old_total = 0
            new_total = quantity * kama_price
            kama_asset["unit_price"] = kama_price
            kama_asset["total_value_tomans"] = new_total
            total_delta_by_owner[owner] = total_delta_by_owner.get(owner, 0) + (new_total - old_total)

    totals = snapshot.get("total_values_tomans", {})
    if isinstance(totals, dict):
        for owner, delta in total_delta_by_owner.items():
            old_owner_total = totals.get(owner, 0)
            if isinstance(old_owner_total, (int, float)) and not isinstance(old_owner_total, bool):
                totals[owner] = old_owner_total + delta

    return True, sum(total_delta_by_owner.values())


def has_missing_kama_prices(history_path=HISTORY_PATH):
    """Return True if any snapshot has missing or zero KAMA price data."""
    if not os.path.exists(history_path):
        return False

    records = _load_jsonl(history_path)
    for _, record in records:
        snapshot = record.get("snapshot", {})
        if not isinstance(snapshot, dict):
            continue
        prices = snapshot.get("prices", {})
        if extract_price({"pl": prices.get("kama_stock")}) <= 0:
            return True
    return False


# --- Public backfill command -------------------------------------------------
def backfill(dry_run=True, skip_if_complete=False):
    if skip_if_complete and not has_missing_kama_prices():
        log_step("KAMA history backfill skipped: no missing KAMA prices found.", "info")
        return 0

    settings = load_json(SETTINGS_PATH)
    api_settings = settings.get("api_settings", {})
    tsetmc_key = api_settings.get("tsetmc_api_key")
    if not tsetmc_key:
        raise ValueError("Missing api_settings.tsetmc_api_key in settings.json")

    history_payload = fetch_tsetmc_history(
        api_settings.get("tsetmc_history_url", DEFAULT_TSETMC_HISTORY_URL),
        tsetmc_key,
        KAMA_SYMBOL,
    )
    history_prices = _build_history_price_map(history_payload)
    if not history_prices:
        raise ValueError("TSETMC History API returned no usable KAMA prices")

    records = _load_jsonl(HISTORY_PATH)
    changed = []

    for line_number, record in records:
        snapshot_date = _parse_snapshot_date(record.get("timestamp"))
        if not snapshot_date:
            continue
        jalali_date = _gregorian_to_jalali(snapshot_date)
        priced_date, kama_price = _nearest_price_on_or_before(history_prices, jalali_date)
        if not kama_price:
            continue
        was_changed, delta = _repair_record(record, kama_price)
        if was_changed:
            changed.append((line_number, snapshot_date.isoformat(), jalali_date, priced_date, kama_price, delta))

    if not changed:
        log_step("No missing KAMA history records were repairable.", "info")
        return 0

    for line_number, gregorian_date, jalali_date, priced_date, kama_price, delta in changed:
        log_step(
            f"Line {line_number}: {gregorian_date} / {jalali_date} priced from {priced_date} -> KAMA {kama_price:,.0f}, total delta {delta:,.0f}",
            "info",
        )

    if dry_run:
        log_step(f"Dry run only: {len(changed)} records would be updated.", "warning")
        return len(changed)

    _save_jsonl(HISTORY_PATH, records)
    cache = load_json(CACHE_PATH)
    today_jalali = _gregorian_to_jalali(date.today())
    latest_history_date, latest_price = _nearest_price_on_or_before(history_prices, today_jalali)
    if latest_price:
        cache["kama_stock"] = latest_price
        save_json(CACHE_PATH, cache)
        log_step(f"Updated latest_prices_cache.json KAMA to {latest_price:,.0f} from {latest_history_date}.", "success")
    log_step(f"Updated {len(changed)} history snapshot records.", "success")
    return len(changed)


def main():
    parser = argparse.ArgumentParser(description="Backfill missing KAMA stock prices from BRS TSETMC History API.")
    parser.add_argument("--write", action="store_true", help="Write updates to history_snapshots.jsonl and cache.")
    args = parser.parse_args()

    try:
        backfill(dry_run=not args.write)
    except Exception as exc:
        log_step(str(exc), "error")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
