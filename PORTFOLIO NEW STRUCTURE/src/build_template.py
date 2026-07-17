"""Build the v2 `.xlsx` dashboard workbook.

This file is layout-heavy because it creates sheets, formulas, tables, and
charts. Business rules should stay in `engine.py`, `analytics.py`, and
`asset_classes.py`; this module should mostly decide how workbook data is shown.
"""

import datetime as _dt
import os
from collections import defaultdict
from statistics import mean

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.utils import get_column_letter

from analytics import load_history, matrix_to_rows, sorted_snapshots
from asset_classes import (
    CLASS_ORDER,
    IRT,
    USD,
    LIVE_DATA_ORDER,
    PRICE_KEY_LABELS,
    PRICE_LABEL_KEYS,
    USD_PRICE_KEYS,
    classify,
)
from utils import log_step

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
    ws[4][1].number_format = "yyyy-mm-dd"
    ws[5][1].number_format = '#,##0'
    _set_widths(ws, {"A": 24, "B": 24, "C": 62})
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
        row[0].number_format = "yyyy-mm-dd"
        for cell in row[1:]:
            cell.number_format = '#,##0'
    _style_table(ws)
    _set_widths(ws, {"A": 14})
    for column in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(column)].width = 16
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
    _set_widths(ws, {"A": 28, "B": 18, "C": 12, "D": 24, "E": 24})
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
    _set_widths(ws, {"A": 12, "B": 18, "C": 18, "D": 20, "E": 12})
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
        row[0].number_format = "yyyy-mm-dd"
        for cell in row[1:]:
            cell.number_format = '#,##0'
    _style_table(ws)
    _set_widths(ws, {"A": 14})
    for column in range(2, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(column)].width = 18
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
    ws["B3"].number_format = "yyyy-mm-dd"
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
        ("G4", "Latest Date", f"={SH_HISTDATA}!A{latest_row}"),
        ("J4", "USD Rate", f'=IFERROR(XLOOKUP("Tether",Live_Data!A:A,Live_Data!B:B),XLOOKUP("US Dollar",Live_Data!A:A,Live_Data!B:B))'),
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
        ws.cell(row + 1, col).number_format = "yyyy-mm-dd" if title == "Latest Date" else '#,##0'

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
        ws.cell(row, 4, f"=IFERROR(B{row}/B5,0)")
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
        ws.cell(row, 4, f"=IFERROR(B{row}/B5,0)")
        ws.cell(row, 5, f"=IFERROR(C{row}/D5,0)")
    alloc_end = alloc_header_row + len(CLASS_ORDER)

    price_start = 8
    price_col = 7
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
    for row in range(owner_start + 1, owner_end + 1):
        ws.cell(row, 2).number_format = '#,##0'
        ws.cell(row, 3).number_format = '#,##0'
        ws.cell(row, 4).number_format = '0.0%'
    for row in range(alloc_header_row + 1, alloc_end + 1):
        ws.cell(row, 2).number_format = '#,##0'
        ws.cell(row, 3).number_format = '#,##0'
        ws.cell(row, 4).number_format = '0.0%'
        ws.cell(row, 5).number_format = '0.0%'

    _set_widths(ws, {"A": 22, "B": 20, "C": 18, "D": 14, "E": 14, "G": 28, "H": 18, "I": 12, "J": 18})

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
