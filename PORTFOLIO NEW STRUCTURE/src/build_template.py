"""Build the v2 `.xlsx` dashboard workbook.

This file is layout-heavy because it creates sheets, formulas, tables, and
charts. Business rules stay in `engine.py`. Asset-class config and history
analytics are colocated below because the workbook is their only consumer.
"""

import datetime as _dt
import json
import os
from collections import defaultdict
from datetime import datetime
from statistics import mean

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from utils import log_step


# ===== Asset classes =========================================================
# Raw holding keys like `emami_coin` are grouped into readable classes. Kept
# here so analytics and workbook code agree on the same categories.

# This order is also the display order in charts and tables.
GOLD = "Gold"
CASH = "Cash"
STOCK = "Stock"
REAL_ESTATE = "Real Estate"
CRYPTO = "Crypto"

CLASS_ORDER = [GOLD, CASH, STOCK, REAL_ESTATE, CRYPTO]

# Persian labels keep workbook sheets bilingual.
CLASS_LABELS_FA = {
    GOLD: "طلا",
    CASH: "نقد / دلار",
    STOCK: "سهام",
    REAL_ESTATE: "ملک",
    CRYPTO: "رمز ارز",
}

# Stable colors keep the same class recognizable across charts.
CLASS_COLORS = {
    GOLD: "E0A82E",         # gold
    CASH: "4C9A2A",         # green
    STOCK: "2E75B6",        # blue
    REAL_ESTATE: "8B5E3C",  # brown
    CRYPTO: "F2A900",       # bitcoin orange
}

# Keys come from `current_state.json` and `engine.build_snapshot`.
ASSET_KEY_TO_CLASS = {
    "emami_coin": GOLD,
    "half_coin": GOLD,
    "quarter_coin": GOLD,
    "quarter_coin_pre86": GOLD,
    "swiss_gold_bar_1g": GOLD,
    "swiss_gold_bar_2_5g": GOLD,
    "one_gram_coin": GOLD,
    "gold_18k_gram": GOLD,
    "usd_cash": CASH,
    "tether": CASH,
    "usdt_irt": CASH,
    "kama_stock": STOCK,
    "house_asset": REAL_ESTATE,
    "bitcoin_usd": CRYPTO,
    "bitcoin": CRYPTO,
}

# House value is tracked in totals but excluded from allocation percentage charts.
CLASSES_EXCLUDED_FROM_ALLOCATION = {REAL_ESTATE}

# Canonical mapping from internal price keys (engine output) to human labels.
PRICE_KEY_LABELS = {
    "bitcoin_usd": "Bitcoin",
    "usdt_irt": "Tether",
    "usd_cash": "US Dollar",
    "emami_coin": "Emami Coin",
    "half_coin": "Half Coin",
    "quarter_coin": "Quarter Coin",
    "quarter_coin_pre86": "Quarter Coin (Pre-86)",
    "swiss_gold_bar_1g": "Swiss Gold Bar (1g)",
    "swiss_gold_bar_2_5g": "Swiss Gold Bar (2.5g)",
    "one_gram_coin": "1g Coin",
    "gold_18k_gram": "Gold Gram (18K)",
    "kama_stock": "KAMA Stock",
    "euro_cash": "Euro",
    "gold_ounce_usd": "Gold Ounce (Global)",
}
PRICE_LABEL_KEYS = {label: key for key, label in PRICE_KEY_LABELS.items()}

# Stable row order so dashboard price tables stay familiar.
LIVE_DATA_ORDER = [
    "Bitcoin",
    "Tether",
    "US Dollar",
    "Emami Coin",
    "Half Coin",
    "Quarter Coin",
    "Quarter Coin (Pre-86)",
    "Swiss Gold Bar (1g)",
    "Swiss Gold Bar (2.5g)",
    "1g Coin",
    "Gold Gram (18K)",
    "KAMA Stock",
    "Euro",
    "Gold Ounce (Global)",
]

# Prices quoted in USD rather than Tomans.
USD_PRICE_KEYS = {"bitcoin_usd", "gold_ounce_usd"}

IRT = "IRT"
USD = "USD"
CURRENCIES = [IRT, USD]

CURRENCY_LABELS = {
    IRT: "Tomans",
    USD: "USDT",
}


def classify(asset_key):
    """Return the class for a raw asset key, or None if unmapped."""
    return ASSET_KEY_TO_CLASS.get(asset_key)


def allocation_classes():
    """Classes shown in the allocation-% charts (house excluded)."""
    return [c for c in CLASS_ORDER if c not in CLASSES_EXCLUDED_FROM_ALLOCATION]


def owners_from_state(current_state):
    """Derive the ordered owner list from current_state.json."""
    if not isinstance(current_state, dict):
        return []
    return [owner for owner, assets in current_state.items() if isinstance(assets, dict)]


# ===== History analytics =====================================================
# Reads `history_snapshots.jsonl` and turns raw snapshots into daily average
# tables that the workbook charts consume.

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

# --- Workbook paths ----------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
TEMPLATE_DIR = os.path.join(BASE_DIR, "template")
DEFAULT_OUTPUT = os.path.join(TEMPLATE_DIR, "Portfolio_Tracker_v2.xlsx")

CURRENT_STATE_PATH = os.path.join(DATA_DIR, "current_state.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history_snapshots.jsonl")

# --- Sheet names -------------------------------------------------------------
SH_DASH = "Dashboard"
SH_HISTDATA = "History_Data"
SH_ALLOC = "Allocation"
SH_TRENDS = "Trends"
SH_COMPARE = "Comparison"
SH_LIVE = "Live_Data"
SH_SETTINGS = "Settings"

# --- Styling -----------------------------------------------------------------
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SUBHEAD_FILL = PatternFill("solid", fgColor="D9E1F2")
DARK_FONT = Font(bold=True, color="1F3864")
WHITE_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=16, color="1F3864")
CARD_FILL = PatternFill("solid", fgColor="F3F6FA")
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
TABLE_STYLE = "TableStyleMedium2"


# --- Small value helpers -----------------------------------------------------
def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_mean(values):
    numeric = [_as_float(value) for value in values]
    numeric = [value for value in numeric if value is not None]
    return mean(numeric) if numeric else 0.0


def _latest_snapshot(history_path=HISTORY_PATH, snapshot=None):
    if snapshot:
        return snapshot
    records = load_history(history_path)
    if not records:
        return {}
    return sorted_snapshots(records, reverse=True)[0].get("snapshot", {}) or {}


def _snapshot_timestamp(history_path=HISTORY_PATH):
    records = load_history(history_path)
    if not records:
        return ""
    return sorted_snapshots(records, reverse=True)[0].get("timestamp", "")


def _currency_rate(prices):
    return _as_float(prices.get("usdt_irt")) or _as_float(prices.get("usd_cash")) or 0.0


# --- Snapshot-to-dashboard data helpers -------------------------------------
def _snapshot_class_values(snapshot):
    """Return current values by currency, owner, and asset class."""
    prices = snapshot.get("prices", {}) or {}
    usd_rate = _currency_rate(prices)
    owners = [
        owner for owner, assets in (snapshot.get("portfolios", {}) or {}).items()
        if isinstance(assets, dict)
    ]
    owners_with_total = owners + ["Total"]
    values = {
        (currency, owner, cls): 0.0
        for currency in (IRT, USD)
        for owner in owners_with_total
        for cls in CLASS_ORDER
    }

    for owner in owners:
        for asset_key, detail in (snapshot.get("portfolios", {}).get(owner, {}) or {}).items():
            if not isinstance(detail, dict):
                continue
            cls = classify(asset_key)
            if not cls:
                continue
            tomans = _as_float(detail.get("total_value_tomans")) or 0.0
            values[(IRT, owner, cls)] += tomans
            values[(IRT, "Total", cls)] += tomans
            if usd_rate:
                usd_value = tomans / usd_rate
                values[(USD, owner, cls)] += usd_value
                values[(USD, "Total", cls)] += usd_value

    return owners_with_total, values


def _snapshot_total(values, currency, owner):
    return sum(values.get((currency, owner, cls), 0.0) for cls in CLASS_ORDER)


# --- History_Data column helpers --------------------------------------------
def _owners_from_history_header(header):
    owners = []
    for col in header[1:]:
        parts = col.split("_", 2)
        if len(parts) != 3:
            continue
        if parts[0] not in (IRT, USD):
            continue
        owner = parts[1]
        if owner not in owners:
            owners.append(owner)
    return owners


def _history_col(header, currency, owner, cls):
    key = f"{currency}_{owner}_{cls.replace(' ', '')}"
    return header.index(key) + 1


def _history_total_formula(header, row, currency, owner):
    cols = [_history_col(header, currency, owner, cls) for cls in CLASS_ORDER]
    refs = [f"{SH_HISTDATA}!{get_column_letter(col)}{row}" for col in cols]
    return "=" + "+".join(refs)


def _history_class_formula(header, row, currency, owner, cls):
    col = _history_col(header, currency, owner, cls)
    return f"={SH_HISTDATA}!{get_column_letter(col)}{row}"


# --- Workbook styling helpers ------------------------------------------------
def _style_header(row):
    for cell in row:
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.alignment = Alignment(horizontal="center")
        cell.border = BORDER


def _style_table(ws):
    for row in ws.iter_rows():
        for cell in row:
            cell.border = BORDER
            if cell.row == 1:
                continue
            cell.alignment = Alignment(horizontal="center")
            if isinstance(cell.value, (int, float)):
                cell.number_format = '#,##0'


def _set_widths(ws, widths):
    for column, width in widths.items():
        ws.column_dimensions[column].width = width


def _add_table(ws, name, ref):
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name=TABLE_STYLE,
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(table)


# --- History and price table builders ---------------------------------------
def _build_price_daily_rows(history_path):
    records = load_history(history_path)
    by_date = defaultdict(list)
    for record in records:
        timestamp = record.get("timestamp")
        if not timestamp:
            continue
        try:
            date_key = _dt.datetime.fromisoformat(timestamp).date().isoformat()
        except ValueError:
            continue
        by_date[date_key].append(record.get("snapshot", {}) or {})

    price_keys = []
    for record in records:
        prices = (record.get("snapshot", {}) or {}).get("prices", {}) or {}
        for key in prices:
            if key not in price_keys:
                price_keys.append(key)

    ordered_keys = [PRICE_LABEL_KEYS[name] for name in LIVE_DATA_ORDER if PRICE_LABEL_KEYS.get(name) in price_keys]
    ordered_keys.extend(sorted(key for key in price_keys if key not in ordered_keys))

    header = ["Date"] + ordered_keys
    rows = []
    for date_key in sorted(by_date):
        row = [date_key]
        for price_key in ordered_keys:
            values = [snapshot.get("prices", {}).get(price_key) for snapshot in by_date[date_key]]
            row.append(round(_safe_mean(values), 4))
        rows.append(row)
    return header, rows


# --- Sheet writers -----------------------------------------------------------
def _write_settings(ws):
    ws.title = SH_SETTINGS
    ws.append(["Setting", "Value", "Notes"])
    _style_header(ws[1])
    settings = [
        ("Rolling Window", 30, "Trend rows shown in charts; use All for full history"),
        ("Selected Currency", IRT, "IRT or USD"),
        ("Comparison Start Date", "=IFERROR(INDEX(History_Data!A:A,MAX(2,COUNTA(History_Data!A:A)-29)),TODAY())", "Editable"),
        ("Investment Amount", 100000000, "Editable lump-sum amount in asset currency"),
        ("Last Generated", _dt.datetime.now().replace(microsecond=0), "Local machine time"),
    ]
    for row in settings:
        ws.append(row)
    ws[4][1].number_format = "yyyy\\-mm\\-dd"
    ws[5][1].number_format = '#,##0'
    ws[6][1].number_format = "yyyy\\-mm\\-dd\\ h:mm:ss"
    _set_widths(ws, {"A": 18, "B": 18, "C": 39})
    _style_table(ws)


def _write_history_data(ws, history_path):
    """Write the normalized daily history table used by charts."""
    ws.title = SH_HISTDATA
    history_header, history_rows = matrix_to_rows(history_path)
    price_header, price_rows = _build_price_daily_rows(history_path)

    if not history_header:
        history_header = ["Date"]
    header = history_header + [f"Price_{key}" for key in price_header[1:]]
    ws.append(header)
    _style_header(ws[1])

    price_by_date = {row[0]: row[1:] for row in price_rows}
    for row in history_rows:
        date_value = _dt.date.fromisoformat(row[0])
        ws.append([date_value] + row[1:] + price_by_date.get(row[0], [0] * (len(price_header) - 1)))

    if ws.max_row == 1:
        ws.append([_dt.date.today().isoformat()] + [0] * (len(header) - 1))

    for cell in ws[1]:
        cell.alignment = Alignment(text_rotation=45, horizontal="center")
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[0].number_format = "yyyy\\-mm\\-dd"
        for cell in row[1:]:
            cell.number_format = '#,##0'
    _style_table(ws)
    _set_widths(ws, {"A": 10})
    for column in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(column)].width = 13
    _add_table(ws, "HistoryTable", f"A1:{get_column_letter(ws.max_column)}{ws.max_row}")
    ws.freeze_panes = "B2"
    return header, history_rows, price_header, price_rows


def _write_live_data(ws, snapshot, history_path):
    """Write the latest price table shown on the dashboard."""
    ws.title = SH_LIVE
    prices = snapshot.get("prices", {}) or {}
    timestamp = _snapshot_timestamp(history_path)
    keys = []
    for name in LIVE_DATA_ORDER:
        key = PRICE_LABEL_KEYS.get(name)
        if key and key in prices:
            keys.append(key)
    keys.extend(sorted(key for key in prices if key not in keys))

    ws.append(["Asset", "Price", "Unit", "Price Key", "Last Snapshot"])
    _style_header(ws[1])
    for key in keys:
        asset = PRICE_KEY_LABELS.get(key, key)
        unit = "USD" if key in USD_PRICE_KEYS else "IRT"
        ws.append([asset, prices.get(key), unit, key, timestamp])
    if ws.max_row == 1:
        ws.append(["No prices", 0, "", "", timestamp])

    _style_table(ws)
    _set_widths(ws, {"A": 17, "B": 11, "C": 10, "D": 17, "E": 25})
    _add_table(ws, "LivePriceTable", f"A1:E{ws.max_row}")


def _write_allocation(ws, snapshot):
    """Write current asset-class allocation values and chart."""
    ws.title = SH_ALLOC
    owners, values = _snapshot_class_values(snapshot)

    ws.append(["Currency", "Owner", "Asset Class", "Value", "Share"])
    _style_header(ws[1])
    for currency in (IRT, USD):
        for owner in owners:
            if owner == "Total":
                continue
            for cls in CLASS_ORDER:
                row = ws.max_row + 1
                ws.append([
                    currency,
                    owner,
                    cls,
                    values.get((currency, owner, cls), 0.0),
                    f"=IFERROR(D{row}/SUMIFS(D:D,A:A,A{row},B:B,B{row}),0)",
                ])
        for cls in CLASS_ORDER:
            row = ws.max_row + 1
            ws.append([
                currency,
                "Total",
                cls,
                values.get((currency, "Total", cls), 0.0),
                f"=IFERROR(D{row}/SUMIFS(D:D,A:A,A{row},B:B,B{row}),0)",
            ])

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[3].number_format = '#,##0'
        row[4].number_format = '0.0%'
    _style_table(ws)
    _set_widths(ws, {"A": 13, "B": 11, "C": 14, "D": 13, "E": 11})
    _add_table(ws, "AllocationTable", f"A1:E{ws.max_row}")

    chart = PieChart()
    chart.title = "Total Allocation - IRT"
    labels = Reference(ws, min_col=3, min_row=2, max_row=1 + len(CLASS_ORDER))
    data = Reference(ws, min_col=4, min_row=1, max_row=1 + len(CLASS_ORDER))
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(labels)
    chart.height = 8
    chart.width = 11
    ws.add_chart(chart, "G2")


def _write_trends(ws, header, history_rows):
    """Write historical trend tables and line charts."""
    ws.title = SH_TRENDS
    owners = _owners_from_history_header(header)
    real_owners = [owner for owner in owners if owner != "Total"]

    trend_header = ["Date", "Total IRT", "Total USD"]
    trend_header.extend([f"{owner} IRT" for owner in real_owners])
    trend_header.extend([f"{cls} IRT" for cls in CLASS_ORDER])
    ws.append(trend_header)
    _style_header(ws[1])

    for idx, _row in enumerate(history_rows, start=2):
        out = [f"={SH_HISTDATA}!A{idx}"]
        out.append(_history_total_formula(header, idx, IRT, "Total"))
        out.append(_history_total_formula(header, idx, USD, "Total"))
        for owner in real_owners:
            out.append(_history_total_formula(header, idx, IRT, owner))
        for cls in CLASS_ORDER:
            out.append(_history_class_formula(header, idx, IRT, "Total", cls))
        ws.append(out)

    if ws.max_row == 1:
        ws.append([_dt.date.today().isoformat()] + [0] * (len(trend_header) - 1))

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row):
        row[0].number_format = "yyyy\\-mm\\-dd"
        for cell in row[1:]:
            cell.number_format = '#,##0'
    _style_table(ws)
    _set_widths(ws, {"A": 10})
    for column in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(column)].width = 13
    _add_table(ws, "TrendTable", f"A1:{get_column_letter(ws.max_column)}{ws.max_row}")

    total_chart = LineChart()
    total_chart.title = "Net Worth Trend"
    total_chart.y_axis.title = "Value"
    total_chart.x_axis.title = "Date"
    total_chart.add_data(Reference(ws, min_col=2, max_col=3, min_row=1, max_row=ws.max_row), titles_from_data=True)
    total_chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
    total_chart.height = 8
    total_chart.width = 18
    ws.add_chart(total_chart, "A" + str(ws.max_row + 3))

    owner_chart = LineChart()
    owner_chart.title = "Owner Values - IRT"
    owner_start = 4
    owner_end = owner_start + len(real_owners) - 1
    if real_owners:
        owner_chart.add_data(Reference(ws, min_col=owner_start, max_col=owner_end, min_row=1, max_row=ws.max_row), titles_from_data=True)
        owner_chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
    owner_chart.height = 8
    owner_chart.width = 18
    ws.add_chart(owner_chart, "J" + str(ws.max_row + 3))

    class_chart = LineChart()
    class_chart.title = "Asset-Class Values - IRT"
    class_start = 4 + len(real_owners)
    class_end = class_start + len(CLASS_ORDER) - 1
    class_chart.add_data(Reference(ws, min_col=class_start, max_col=class_end, min_row=1, max_row=ws.max_row), titles_from_data=True)
    class_chart.set_categories(Reference(ws, min_col=1, min_row=2, max_row=ws.max_row))
    class_chart.height = 8
    class_chart.width = 18
    ws.add_chart(class_chart, "A" + str(ws.max_row + 20))


def _write_comparison(ws, snapshot, price_header, price_rows, history_row_count):
    """Write the what-if comparison sheet for lump-sum investments."""
    ws.title = SH_COMPARE
    latest_prices = snapshot.get("prices", {}) or {}
    price_keys = [key for key in price_header[1:] if key in latest_prices]
    price_keys.extend(sorted(key for key in latest_prices if key not in price_keys))

    ws["A1"] = "What-If Lump-Sum Comparison"
    ws["A1"].font = TITLE_FONT
    ws["A3"] = "Start Date"
    ws["B3"] = f"={SH_SETTINGS}!B3"
    ws["A4"] = "Investment Amount"
    ws["B4"] = f"={SH_SETTINGS}!B4"
    ws["A5"] = "Actual Portfolio Total (IRT)"
    ws["B5"] = f"={SH_DASH}!B4"
    for cell in ("A3", "A4", "A5"):
        ws[cell].font = DARK_FONT
    ws["B3"].number_format = "yyyy\\-mm\\-dd"
    ws["B4"].number_format = '#,##0'
    ws["B5"].number_format = '#,##0'

    start_row = 8
    headers = ["Asset", "Price Key", "Start Price", "Latest Price", "Units Bought", "Current Value", "Return %"]
    for col, value in enumerate(headers, start=1):
        cell = ws.cell(start_row, col, value)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
        cell.alignment = Alignment(horizontal="center")

    for offset, key in enumerate(price_keys, start=1):
        row = start_row + offset
        label = PRICE_KEY_LABELS.get(key, key)
        latest = latest_prices.get(key)
        price_col = None
        if key in price_header:
            price_col = price_header.index(key) + 1
        ws.cell(row, 1, label)
        ws.cell(row, 2, key)
        if price_col:
            letter = get_column_letter(price_col)
            ws.cell(
                row,
                3,
                f'=IFERROR(XLOOKUP($B$3,History_Data!$A$2:$A${history_row_count + 1},'
                f'History_Data!${letter}$2:${letter}${history_row_count + 1},"",1),"")',
            )
        else:
            ws.cell(row, 3, "")
        ws.cell(row, 4, latest)
        ws.cell(row, 5, f"=IFERROR($B$4/C{row},0)")
        ws.cell(row, 6, f"=IFERROR(E{row}*D{row},0)")
        ws.cell(row, 7, f"=IFERROR(F{row}/$B$4-1,0)")

    if not price_keys:
        ws.append(["No priced assets found", "", 0, 0, 0, 0, 0])

    last_row = start_row + max(1, len(price_keys))
    for row in ws.iter_rows(min_row=start_row + 1, max_row=last_row):
        for cell in row:
            cell.border = BORDER
        for idx in (2, 3, 4, 5):
            row[idx].number_format = '#,##0'
        row[6].number_format = '0.0%'
    _set_widths(ws, {"A": 28, "B": 24, "C": 18, "D": 18, "E": 18, "F": 20, "G": 12})
    _add_table(ws, "ComparisonTable", f"A{start_row}:G{last_row}")

    chart = BarChart()
    chart.title = "What-If Current Value"
    chart.y_axis.title = "Value"
    chart.add_data(Reference(ws, min_col=6, min_row=start_row, max_row=last_row), titles_from_data=True)
    chart.set_categories(Reference(ws, min_col=1, min_row=start_row + 1, max_row=last_row))
    chart.height = 10
    chart.width = 18
    ws.add_chart(chart, "I3")


def _write_dashboard(ws, header, history_rows, snapshot):
    """Write the front dashboard sheet with cards, tables, and charts."""
    ws.title = SH_DASH
    latest_row = len(history_rows) + 1
    owners, current_values = _snapshot_class_values(snapshot)
    real_owners = [owner for owner in owners if owner != "Total"]

    ws["A1"] = "Portfolio Dashboard V2"
    ws["A1"].font = TITLE_FONT
    ws["A2"] = "Pure .xlsx dashboard generated from daily-average history"
    ws["A2"].font = Font(italic=True, color="666666")

    cards = [
        ("A4", "Total Net Worth (IRT)", _snapshot_total(current_values, IRT, "Total")),
        ("D4", "Total Net Worth (USD)", _snapshot_total(current_values, USD, "Total")),
        ("H4", "Latest Date", f"={SH_HISTDATA}!A{latest_row}"),
        ("K4", "USD Rate", f"={SH_LIVE}!B4"),
    ]
    for cell_ref, title, formula in cards:
        col = ws[cell_ref].column
        row = ws[cell_ref].row
        ws.cell(row, col, title)
        ws.cell(row, col).font = DARK_FONT
        ws.cell(row, col).fill = CARD_FILL
        ws.cell(row + 1, col, formula)
        ws.cell(row + 1, col).font = Font(bold=True, size=13)
        ws.cell(row + 1, col).fill = CARD_FILL
        ws.cell(row + 1, col).number_format = "yyyy\\-mm\\-dd" if title == "Latest Date" else '#,##0'

    ws["A8"] = "Owner Split"
    ws["A8"].font = DARK_FONT
    ws.append([])
    owner_start = 9
    owner_headers = ["Owner", "IRT", "USD", "Share"]
    for col, value in enumerate(owner_headers, start=1):
        ws.cell(owner_start, col, value)
    _style_header(ws[owner_start])
    for offset, owner in enumerate(real_owners, start=1):
        row = owner_start + offset
        ws.cell(row, 1, owner)
        ws.cell(row, 2, _snapshot_total(current_values, IRT, owner))
        ws.cell(row, 3, _snapshot_total(current_values, USD, owner))
        ws.cell(row, 4, f"=IFERROR(B{row}/A5,0)")
    owner_end = owner_start + max(1, len(real_owners))

    alloc_start = owner_end + 3
    ws.cell(alloc_start, 1, "Asset-Class Allocation")
    ws.cell(alloc_start, 1).font = DARK_FONT
    alloc_header_row = alloc_start + 1
    for col, value in enumerate(["Class", "IRT", "USD", "IRT Share", "USD Share"], start=1):
        ws.cell(alloc_header_row, col, value)
    _style_header(ws[alloc_header_row])
    for offset, cls in enumerate(CLASS_ORDER, start=1):
        row = alloc_header_row + offset
        ws.cell(row, 1, cls)
        ws.cell(row, 2, current_values.get((IRT, "Total", cls), 0.0))
        ws.cell(row, 3, current_values.get((USD, "Total", cls), 0.0))
        ws.cell(row, 4, f"=IFERROR(B{row}/A5,0)")
        ws.cell(row, 5, f"=IFERROR(C{row}/D5,0)")
    alloc_end = alloc_header_row + len(CLASS_ORDER)

    price_start = 8
    price_col = 8
    ws.cell(price_start, price_col, "Latest Prices")
    ws.cell(price_start, price_col).font = DARK_FONT
    for col, value in enumerate(["Asset", "Price", "Unit"], start=price_col):
        ws.cell(price_start + 1, col, value)
    for col in range(price_col, price_col + 3):
        cell = ws.cell(price_start + 1, col)
        cell.fill = HEADER_FILL
        cell.font = WHITE_FONT
        cell.border = BORDER
    for row in range(2, min(15, len(snapshot.get("prices", {})) + 2)):
        out_row = price_start + row
        ws.cell(out_row, price_col, f"={SH_LIVE}!A{row}")
        ws.cell(out_row, price_col + 1, f"={SH_LIVE}!B{row}")
        ws.cell(out_row, price_col + 2, f"={SH_LIVE}!C{row}")

    for row in ws.iter_rows(min_row=1, max_row=max(alloc_end, price_start + 15), max_col=12):
        for cell in row:
            if cell.value is not None:
                cell.border = BORDER
                cell.alignment = Alignment(horizontal="center")
    for row in range(owner_start + 1, owner_end + 1):
        ws.cell(row, 2).number_format = '#,##0'
        ws.cell(row, 3).number_format = '#,##0'
        ws.cell(row, 4).number_format = '0.0%'
    for row in range(alloc_header_row + 1, alloc_end + 1):
        ws.cell(row, 2).number_format = '#,##0'
        ws.cell(row, 3).number_format = '#,##0'
        ws.cell(row, 4).number_format = '0.0%'
        ws.cell(row, 5).number_format = '0.0%'

    _set_widths(ws, {"A": 47, "B": 14, "C": 7, "D": 18, "E": 9, "F": 9, "G": 17, "I": 10, "J": 5, "K": 9, "L": 9})

    owner_chart = PieChart()
    owner_chart.title = "Owner Split - IRT"
    owner_chart.add_data(Reference(ws, min_col=2, min_row=owner_start, max_row=owner_end), titles_from_data=True)
    owner_chart.set_categories(Reference(ws, min_col=1, min_row=owner_start + 1, max_row=owner_end))
    owner_chart.height = 8
    owner_chart.width = 10
    ws.add_chart(owner_chart, "G24")

    alloc_chart = PieChart()
    alloc_chart.title = "Asset-Class Allocation - IRT"
    alloc_chart.add_data(Reference(ws, min_col=2, min_row=alloc_header_row, max_row=alloc_end), titles_from_data=True)
    alloc_chart.set_categories(Reference(ws, min_col=1, min_row=alloc_header_row + 1, max_row=alloc_end))
    alloc_chart.height = 8
    alloc_chart.width = 10
    ws.add_chart(alloc_chart, "A" + str(alloc_end + 3))

    trend_chart = LineChart()
    trend_chart.title = "Recent Net Worth Trend"
    trend_chart.add_data(Reference(ws.parent[SH_TRENDS], min_col=2, max_col=3, min_row=1, max_row=ws.parent[SH_TRENDS].max_row), titles_from_data=True)
    trend_chart.set_categories(Reference(ws.parent[SH_TRENDS], min_col=1, min_row=2, max_row=ws.parent[SH_TRENDS].max_row))
    trend_chart.height = 8
    trend_chart.width = 18
    ws.add_chart(trend_chart, "G39")


def build_workbook(output_path=DEFAULT_OUTPUT, history_path=HISTORY_PATH, snapshot=None):
    """Build and save the complete v2 workbook."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    snapshot = _latest_snapshot(history_path, snapshot)

    wb = Workbook()
    ws_settings = wb.active
    _write_settings(ws_settings)
    ws_history = wb.create_sheet(SH_HISTDATA)
    header, history_rows, price_header, price_rows = _write_history_data(ws_history, history_path)
    _write_live_data(wb.create_sheet(SH_LIVE), snapshot, history_path)
    _write_allocation(wb.create_sheet(SH_ALLOC), snapshot)
    _write_trends(wb.create_sheet(SH_TRENDS), header, history_rows)
    _write_comparison(
        wb.create_sheet(SH_COMPARE),
        snapshot,
        price_header,
        price_rows,
        len(history_rows),
    )
    _write_dashboard(wb.create_sheet(SH_DASH, 0), header, history_rows, snapshot)

    for ws in wb.worksheets:
        ws.sheet_view.showGridLines = False

    wb.save(output_path)
    return output_path


def main():
    path = build_workbook()
    log_step(f"V2 .xlsx template generated: {path}", "success")


if __name__ == "__main__":
    main()
