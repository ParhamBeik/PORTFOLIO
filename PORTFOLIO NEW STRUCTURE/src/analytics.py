#!/usr/bin/env python3
"""History analytics for Excel dashboards.

This module reads `history_snapshots.jsonl` and turns many raw snapshots into
daily average tables. The workbook builder consumes these tables.
"""

import json
import os
from datetime import datetime
from collections import defaultdict
from statistics import mean

from asset_classes import (
    CLASS_ORDER,
    IRT,
    USD,
    classify,
)


# --- Load and group history --------------------------------------------------
def load_history(history_path):
    """Load all snapshots from the JSONL history file."""
    snapshots = []
    if not os.path.exists(history_path):
        return snapshots
    with open(history_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snapshots.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return snapshots


def group_snapshots_by_date(snapshots):
    """Group snapshots by date (YYYY-MM-DD) from timestamp."""
    by_date = defaultdict(list)
    for entry in snapshots:
        ts = entry.get("timestamp")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        date_key = dt.date().isoformat()
        by_date[date_key].append(entry)
    return by_date


def sorted_snapshots(snapshots, reverse=True):
    """Return snapshots sorted by timestamp, newest first by default."""
    def sort_key(entry):
        ts = entry.get("timestamp")
        if not ts:
            return datetime.min
        try:
            return datetime.fromisoformat(ts)
        except ValueError:
            return datetime.min

    return sorted(snapshots, key=sort_key, reverse=reverse)


# --- V2 chart matrix ---------------------------------------------------------
# The v2 dashboard uses one wide table: one row per day, then columns for each
# currency + owner + asset class. This makes Excel charts simple and stable.


def _snapshot_class_values(snapshot):
    """Group one snapshot into IRT/USD values by owner and asset class."""
    prices = snapshot.get("prices", {}) or {}
    usd_rate = prices.get("usdt_irt") or prices.get("usd_cash") or 0

    result = {IRT: defaultdict(lambda: defaultdict(float)),
              USD: defaultdict(lambda: defaultdict(float))}

    for owner, assets in snapshot.get("portfolios", {}).items():
        if not isinstance(assets, dict):
            continue
        for asset_key, detail in assets.items():
            if not isinstance(detail, dict):
                continue
            cls = classify(asset_key)
            if cls is None:
                continue
            tomans = detail.get("total_value_tomans") or 0
            result[IRT][owner][cls] += tomans
            if usd_rate:
                result[USD][owner][cls] += tomans / usd_rate

    return result


def build_history_matrix(history_path):
    """Build the daily history matrix that feeds every v2 chart.

    Returns (dates, owners, matrix) where:
      * dates  -- sorted list of 'YYYY-MM-DD' strings (one per day)
      * owners -- ordered owner list seen in history, plus the synthetic 'Total'
      * matrix -- dict keyed (currency, owner, class) -> list aligned with dates,
                  each entry the day's average value (0.0 when no data).
                  'Total' owner is the sum across real owners for that class.
    """
    snapshots = load_history(history_path)
    if not snapshots:
        return [], [], {}

    by_date = group_snapshots_by_date(snapshots)

    # Keep owner columns stable even though the history file is newest-first.
    owners = []
    for entry in sorted_snapshots(snapshots, reverse=False):
        for owner in entry.get("snapshot", {}).get("portfolios", {}):
            if owner not in owners:
                owners.append(owner)
    owners_with_total = owners + ["Total"]

    dates = sorted(by_date.keys())
    # Each matrix cell stores one time-series list aligned with `dates`.
    matrix = {
        (cur, owner, cls): []
        for cur in (IRT, USD)
        for owner in owners_with_total
        for cls in CLASS_ORDER
    }

    for date_key in dates:
        # Average all runs from the same date into one daily value.
        day_acc = {
            (cur, owner, cls): []
            for cur in (IRT, USD)
            for owner in owners
            for cls in CLASS_ORDER
        }
        for entry in by_date[date_key]:
            per = _snapshot_class_values(entry.get("snapshot", {}))
            for cur in (IRT, USD):
                for owner in owners:
                    for cls in CLASS_ORDER:
                        day_acc[(cur, owner, cls)].append(
                            per[cur].get(owner, {}).get(cls, 0.0)
                        )

        for cur in (IRT, USD):
            for cls in CLASS_ORDER:
                total_for_class = 0.0
                for owner in owners:
                    vals = day_acc[(cur, owner, cls)]
                    avg = mean(vals) if vals else 0.0
                    matrix[(cur, owner, cls)].append(avg)
                    total_for_class += avg
                matrix[(cur, "Total", cls)].append(total_for_class)

    return dates, owners_with_total, matrix


def matrix_to_rows(history_path):
    """Flatten build_history_matrix into a header + rows table for Excel.

    Example columns: Date, IRT_Mother_Gold, USD_Total_Cash.
    """
    dates, owners, matrix = build_history_matrix(history_path)
    if not dates:
        return [], []

    columns = []  # ordered list of (currency, owner, class)
    for cur in (IRT, USD):
        for owner in owners:
            for cls in CLASS_ORDER:
                columns.append((cur, owner, cls))

    header = ["Date"] + [f"{cur}_{owner}_{cls.replace(' ', '')}"
                         for (cur, owner, cls) in columns]

    rows = []
    for i, date_key in enumerate(dates):
        row = [date_key]
        for col in columns:
            row.append(round(matrix[col][i], 4))
        rows.append(row)

    return header, rows
