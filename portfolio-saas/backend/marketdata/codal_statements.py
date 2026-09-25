"""Certify income cells from one known Codal V9 sheet, never from PDF text."""

import json
import re
import unicodedata

import jdatetime
from bs4 import BeautifulSoup

from .codal_parsers import parse_number
from .jalali import normalize_jalali


_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ھ": "ه", "ة": "ه"})
_LABELS = {
    "income.operating_revenue": "درآمدهایعملیاتی",
    "income.cost_of_revenue": "بهایتمامشدهدرآمدهایعملیاتی",
    "income.gross_profit": "سودزیانناخالص",
    "income.continuing_profit": "سودزیانخالصعملیاتدرحالتداوم",
    "income.discontinued_profit": "سودزیانخالصعملیاتمتوقفشده",
    "income.net_profit": "سودزیانخالص",
}
_ROWS = {
    False: {
        "income.operating_revenue": 3, "income.cost_of_revenue": 4,
        "income.gross_profit": 5, "income.continuing_profit": 15,
        "income.discontinued_profit": 21, "income.net_profit": 17,
    },
    True: {
        "income.operating_revenue": 3, "income.cost_of_revenue": 4,
        "income.gross_profit": 5, "income.continuing_profit": 17,
        "income.discontinued_profit": 18, "income.net_profit": 19,
    },
}
_SHEETS = {
    False: ("FinancialStatement-Listed-Product-V9", 1, "Income Statement", "IncomeStatement", 3220, "7"),
    True: (
        "FinancialStatement-Consolidated-Listed-Product-V9", 13,
        "Consolidated Income Statement", "ConsolidatedIncomeStatement", 3227, "2",
    ),
}


def _canonical(value):
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_LETTERS)
    return re.sub(r"[^\w]", "", text)


def _period_start(period_end, months):
    try:
        year, month, day = map(int, period_end.split("-"))
        end = jdatetime.date(year, month, day)
        if months not in (3, 6, 9, 12) or (end + jdatetime.timedelta(days=1)).day != 1:
            return ""
        first_month = year * 12 + month - months
        start_year, zero_month = divmod(first_month, 12)
        return f"{start_year:04d}-{zero_month + 1:02d}-01"
    except (ValueError, TypeError, OverflowError):
        return ""


def parse_income_statement(
    content, *, symbol, company_name, title, period_end, is_consolidated, is_audited,
):
    """Return reconciled current-period facts or abstain for an unknown scope/template."""
    if len(content) > 2_000_000 or is_consolidated not in (True, False):
        return []
    if re.search(r"\(\s*شرکت", str(title or "")):
        # The listed symbol can publish a subsidiary's own statements.
        return []
    soup = BeautifulSoup(content, "lxml")
    company_node = soup.select_one("#ctl00_txbCompanyName")
    symbol_node = soup.select_one("#ctl00_txbSymbol")
    unit_node = soup.select_one("#ctl00_pPriceNote")
    if not all((company_node, symbol_node, unit_node)):
        return []
    if (_canonical(company_node.get_text(" ", strip=True)) != _canonical(company_name)
            or _canonical(symbol_node.get_text(" ", strip=True)) != _canonical(symbol)
            or "میلیونریال" not in _canonical(unit_node.get_text(" ", strip=True))):
        return []

    scripts = [script.string or script.get_text() for script in soup.find_all("script")
               if "var datasource =" in (script.string or script.get_text())]
    if len(scripts) != 1:
        return []
    match = re.search(r"\bvar\s+datasource\s*=\s*", scripts[0])
    if not match:
        return []
    try:
        source, offset = json.JSONDecoder().raw_decode(scripts[0][match.end():])
        if not scripts[0][match.end() + offset:].lstrip().startswith(";"):
            return []
        expected_title, sheet_code, sheet_name, alias, table_id, table_version = _SHEETS[is_consolidated]
        end = normalize_jalali(source["periodEndToDate"])
        months = source["period"]
        start = _period_start(end, months)
        fiscal_start = _period_start(normalize_jalali(source["yearEndToDate"]), 12)
        if (source["title_En"] != expected_title or source["type"] != 6
                or source["isConsolidated"] is not is_consolidated
                or source["isAudited"] is not is_audited
                or end != period_end or not start or start != fiscal_start
                or not isinstance(source["sheets"], list)):
            return []
        sheets = [sheet for sheet in source["sheets"] if sheet.get("code") == sheet_code
                  and sheet.get("title_En") == sheet_name]
        if len(sheets) != 1:
            return []
        tables = [table for table in sheets[0]["tables"] if table.get("aliasName") == alias
                  and table.get("metaTableId") == table_id]
        if (len(tables) != 1 or tables[0].get("versionNo") != table_version
                or not 1 <= len(tables[0]["cells"]) <= 1000):
            return []
        table = tables[0]
        if table.get("description") and "میلیونریال" not in _canonical(table["description"]):
            return []
        cells = {}
        for cell in table["cells"]:
            address = cell.get("address")
            if not isinstance(address, str) or address in cells:
                return []
            cells[address] = cell
        facts = []
        values = {}
        for code, row in _ROWS[is_consolidated].items():
            labels = [cell for cell in cells.values() if cell.get("columnCode") == 1
                      and cell.get("rowCode") == row
                      and _canonical(cell.get("value")) == _LABELS[code]]
            if len(labels) != 1 or not re.fullmatch(r"A[1-9][0-9]*", labels[0]["address"]):
                return []
            label = labels[0]
            current = cells["B" + label["address"][1:]]
            value = parse_number(current["value"])
            if (value is None or current.get("columnCode") != 2
                    or current.get("rowSequence") != label.get("rowSequence")
                    or normalize_jalali(current.get("periodEndToDate")) != end
                    or current.get("address", "")[0:1] != "B"):
                return []
            values[code] = value
            facts.append({
                "fact_code": code, "raw_value": str(current["value"]),
                "numeric_value": value, "unit": "million_rial", "currency": "IRR",
                "period_start": start, "period_end": end,
                "dimensions": {
                    "statement_scope": "consolidated" if is_consolidated else "standalone",
                    "period_months": months, "audited": is_audited,
                    "issuer": company_node.get_text(" ", strip=True),
                    "template": expected_title,
                },
                "source_coordinates": {
                    "sheet_code": sheet_code, "table_id": table_id,
                    "label_address": label["address"], "address": current["address"],
                },
                "verification_status": "reconciled",
            })
        if (values["income.operating_revenue"] <= 0
                or values["income.cost_of_revenue"] > 0
                or values["income.gross_profit"] != (
                    values["income.operating_revenue"] + values["income.cost_of_revenue"])
                or values["income.net_profit"] != (
                    values["income.continuing_profit"] + values["income.discontinued_profit"])):
            return []
        return facts
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return []
