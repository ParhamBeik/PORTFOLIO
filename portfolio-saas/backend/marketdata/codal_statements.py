"""Certify Codal statement cells by label and arithmetic, never from PDF text."""

import json
import re
import unicodedata
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

import jdatetime
from bs4 import BeautifulSoup

from .codal_parsers import parse_number
from .jalali import normalize_jalali


_LETTERS = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی", "ھ": "ه", "ة": "ه"})


def _canonical(value):
    text = unicodedata.normalize("NFKC", str(value or "")).translate(_LETTERS)
    return re.sub(r"[^\w]", "", text)


def _html_soup(content):
    # Some Codal V9 pages declare their charset very late. BeautifulSoup can
    # guess a legacy encoding despite the original bytes being valid UTF-8,
    # corrupting Persian issuer, symbol, and unit labels. Keep the raw bytes
    # for hashing; decode only the parser's in-memory view.
    if isinstance(content, bytes):
        try:
            content = content.decode("utf-8")
        except UnicodeDecodeError:
            pass
    return BeautifulSoup(content, "lxml")


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


# Every Codal statement template -- product V5-V9, bank, insurance, investment,
# service, agriculture, building, leasing -- serves the same `datasource` cell
# grid: a label in column 1, one column per period, each body cell stamped with
# its own `periodEndToDate`. Reading by label and by the current-period column,
# then proving the arithmetic, covers all of them; the V9-only reader this
# replaced (fixed rows and table ids) covered ~3% of filings.

_INCOME_LABELS = {
    "income.operating_revenue": ("جمعدرآمدهایعملیاتی", "درآمدهایعملیاتی"),
    "income.cost_of_revenue": ("بهایتمامشدهدرآمدهایعملیاتی",),
    "income.gross_profit": ("سودزیانناخالص",),
    "income.operating_profit": ("سودزیانعملیاتی",),
    "income.continuing_profit": ("سودزیانخالصعملیاتدرحالتداوم",),
    "income.discontinued_profit": ("سودزیانخالصعملیاتمتوقفشده",),
    "income.net_profit": ("سودزیانخالص",),
}
_BALANCE_LABELS = {
    "balance.cash": ("موجودینقد",),
    "balance.current_assets": ("جمعداراییهایجاری",),
    "balance.noncurrent_assets": ("جمعداراییهایغیرجاری",),
    "balance.total_assets": ("جمعداراییها",),
    "balance.current_liabilities": ("جمعبدهیهایجاری",),
    "balance.noncurrent_liabilities": ("جمعبدهیهایغیرجاری",),
    "balance.total_liabilities": ("جمعبدهیها",),
    "balance.total_equity": ("جمعحقوقمالکانه", "جمعحقوقصاحبانسهام"),
    "balance.short_term_borrowings": ("تسهیلاتمالی",),
    "balance.long_term_borrowings": ("تسهیلاتمالیبلندمدت",),
    "balance.liabilities_and_equity": (
        "جمعحقوقمالکانهوبدهیها", "جمعبدهیهاوحقوقمالکانه",
        "جمعبدهیهاوحقوقصاحبانسهام", "جمعحقوقصاحبانسهاموبدهیها",
    ),
}
# Sheet titles as Codal's tab list and `datasource.sheets[].title_Fa` name them.
# Older filings call the balance sheet "ترازنامه", and its equity "حقوق صاحبان سهام".
_SHEET_TITLES = {
    ("income", False): ("صورتسودوزیان",),
    ("income", True): ("صورتسودوزیانتلفیقی",),
    ("balance", False): ("صورتوضعیتمالی", "ترازنامه"),
    ("balance", True): ("صورتوضعیتمالیتلفیقی", "ترازنامهتلفیقی"),
}


def _datasource(content):
    """The page's `var datasource = {...};` object, or None."""
    if not content or len(content) > 3_000_000:
        return None
    text = content.decode("utf-8", "ignore") if isinstance(content, bytes) else str(content)
    match = re.search(r"\bvar\s+datasource\s*=\s*", text)
    if not match:
        return None
    try:
        source, _ = json.JSONDecoder().raw_decode(text[match.end():])
    except ValueError:
        return None
    return source if isinstance(source, dict) else None


def statement_sheet_urls(page, source_url, *, is_consolidated):
    """`{"income"|"balance": url}` for the tabs this filing page lists.

    An audited filing opens on the auditor's opinion (sheetId 19): 61% of the
    stored statement pages held no figures at all, because nothing ever asked
    for the statement tabs. The tab list on the page names them, so the URL is
    built from what the filing itself advertises, never guessed.
    """
    try:
        parts = urlsplit(source_url)
    except ValueError:
        return {}
    if (parts.scheme != "https" or parts.hostname not in ("codal.ir", "www.codal.ir")
            or parts.path != "/Reports/Decision.aspx"):
        return {}
    # The query is kept as raw text: decoding it as a form (`parse_qsl`) turns a
    # serial's literal `+` into a space, and the rebuilt URL names another filing.
    params = [part for part in parts.query.split("&")
              if part and not part.lower().startswith("sheetid=")]
    if sum(part.startswith("LetterSerial=") for part in params) != 1:
        return {}
    options = {
        _canonical(option.get_text(" ", strip=True)): option.get("value")
        for option in _html_soup(page).select("option[value]")
    }
    urls = {}
    for kind in ("income", "balance"):
        sheet_id = next((options[name] for name in _SHEET_TITLES[(kind, is_consolidated)]
                         if options.get(name)), None)
        if sheet_id and sheet_id.isdigit():
            query = "&".join(params + [f"sheetId={sheet_id}"])
            urls[kind] = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))
    return urls


def statement_abstain_reason(content, *, title, extracted=False):
    """Why no statement facts reconciled, coarse enough to count.

    The parser abstains silently by design; this names the bucket so the
    backlog says what it would take to read it instead of one opaque
    "no_typed_facts".
    """
    if re.search(r"\(\s*شرکت", str(title or "")):
        return "subsidiary"
    if _datasource(content) is None:
        return "no_datasource"
    return "unreconciled" if extracted else "no_statement_sheet"


def _unit(text):
    """The Rial scale `text` states; "" when it names none, None when it names
    one this reader does not convert (Toman, a foreign currency, billions).

    A statement of amounts in no currency read here ("مبالغ به هزار درهم") is
    None too, not "": "" lets the page-level Rial note stand in for it."""
    canonical = _canonical(text)
    # Foreign currencies too, including the Rials that are not Iran's (Omani,
    # Qatari, Saudi, Yemeni): "ریال" alone would read them as IRR.
    if any(word in canonical for word in (
        "تومان", "دلار", "یورو", "میلیارد", "درهم", "دینار", "پوند", "یوان", "روبل", "لیر",
        "فرانک", "عمان", "قطر", "سعود", "یمن",
    )):
        return None
    if "میلیونریال" in canonical:
        return "million_rial"
    if "هزارریال" in canonical:
        return "thousand_rial"
    if "ریال" in canonical:
        return "rial"
    if "مبالغ" in canonical or "مبلغ" in canonical:
        return None
    return ""


def parse_statement(content, *, symbol, company_name, title, period_end, is_consolidated, is_audited):
    """Facts from whichever income/balance sheet this page carries, any template.

    Each fact is `reconciled` only when the sheet's own arithmetic holds
    (income: net = continuing + discontinued, and gross = revenue + cost where
    both exist; balance: assets = liabilities + equity = the stated total).
    Otherwise the facts are kept as `extracted`, which no reader shows. A
    malformed payload yields nothing: it is external data, and raising left the
    report in FETCHING to be retried forever.
    """
    try:
        return _parse_statement(
            content, symbol=symbol, company_name=company_name, title=title, period_end=period_end,
            is_consolidated=is_consolidated, is_audited=is_audited,
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError):
        return []


def _parse_statement(content, *, symbol, company_name, title, period_end, is_consolidated, is_audited):
    if is_consolidated not in (True, False) or re.search(r"\(\s*شرکت", str(title or "")):
        return []
    source = _datasource(content)
    if not source or not isinstance(source.get("sheets"), list):
        return []
    soup = _html_soup(content)
    named = False
    for selector, expected in (("#ctl00_txbCompanyName", company_name), ("#ctl00_txbSymbol", symbol)):
        node = soup.select_one(selector)
        # The listed symbol can publish a subsidiary's statements: refuse a page
        # that names somebody else -- or nobody, since nothing else in the
        # datasource says whose figures these are.
        if node is None:
            return []
        if expected:
            if _canonical(node.get_text(" ", strip=True)) != _canonical(expected):
                return []
            named = True
    if not named:
        return []
    try:
        end = normalize_jalali(source.get("periodEndToDate"))
        period = Decimal(str(source.get("period") or 0))
        if period != period.to_integral_value():
            return []  # "3.5" is not a three-month statement
        months = int(period)
        start = _period_start(end, months)
        # Income belongs to the issuer's own fiscal year. A changed fiscal year
        # or an inconsistent payload implies a different start; certifying under
        # one we derived would publish a period the filing never stated.
        if start != _period_start(normalize_jalali(source.get("yearEndToDate")), 12):
            start = ""
        if (end != period_end or source.get("isConsolidated") is not is_consolidated
                or (is_audited is not None and source.get("isAudited") is not is_audited)):
            return []
        if is_audited is None:
            # The title did not say; the filing's own data does. Stamping None
            # let an audited statement be shown as unaudited.
            is_audited = source.get("isAudited")
            if not isinstance(is_audited, bool):
                return []
    except (TypeError, ValueError, ArithmeticError):
        return []
    note = soup.select_one("#ctl00_pPriceNote")
    facts = []
    for sheet in source["sheets"]:
        if not isinstance(sheet, dict):
            continue
        sheet_title = _canonical(sheet.get("title_Fa"))
        kind = next((k for (k, consolidated), names in _SHEET_TITLES.items()
                     if consolidated is is_consolidated and sheet_title in names), None)
        if kind is None:
            continue
        tables = [t for t in sheet.get("tables") or []
                  if isinstance(t, dict) and isinstance(t.get("cells"), list)]
        if not tables:
            continue
        table = max(tables, key=lambda t: len(t["cells"]))
        # The table's own unit wins; the page note only fills in when the table
        # states none. A table in a unit we cannot convert is not read at all.
        unit = _unit(table.get("description") or "")
        if unit == "":
            unit = _unit(note.get_text(" ", strip=True) if note else "")
        if not unit:
            continue
        facts += _sheet_facts(kind, sheet, table, end=end, start=start, months=months, unit=unit,
                              is_consolidated=is_consolidated, is_audited=is_audited,
                              template=str(source.get("title_En") or ""))
    return facts


def _sheet_facts(kind, sheet, table, *, end, start, months, unit, is_consolidated, is_audited, template):
    cells = [c for c in table["cells"] if isinstance(c, dict) and isinstance(c.get("address"), str)]
    header = {}
    for cell in cells:
        if cell.get("cellGroupName") == "Header" and cell.get("columnCode") not in (None, 1):
            header.setdefault(cell["columnCode"], []).append(cell)
    current = [
        column for column, heads in header.items()
        if any(normalize_jalali(h.get("periodEndToDate")) == end for h in heads)
        and not any("تجدید" in str(h.get("value") or "") for h in heads)
    ]
    if len(current) != 1:
        return []
    column = current[0]
    by_row = {}
    for cell in cells:
        row = re.sub(r"^[A-Z]+", "", cell["address"])
        line = by_row.setdefault(row, {})
        if cell.get("columnCode") in line:
            # Two values at one coordinate: picking either by payload order
            # could make the sums pass on a value the filing contradicts.
            return []
        line[cell.get("columnCode")] = cell
    labels = _INCOME_LABELS if kind == "income" else _BALANCE_LABELS
    found, unreadable = {}, set()
    for code, names in labels.items():
        for name in names:
            rows = [row for row, line in by_row.items()
                    if line.get(1) and _canonical(line[1].get("value")) == name]
            if len(rows) > 1:
                break  # ambiguous label: do not guess which row is meant
            if not rows:
                continue
            value_cell = by_row[rows[0]].get(column)
            label_cell = by_row[rows[0]][1]
            if (value_cell is None
                    or normalize_jalali(value_cell.get("periodEndToDate")) not in ("", end)
                    # Same address row is not enough: the payload's own row
                    # identity must agree, or label and figure are not one line.
                    or any(value_cell.get(key) != label_cell.get(key)
                           for key in ("rowCode", "rowSequence")
                           if key in value_cell or key in label_cell)):
                break
            raw = str(value_cell.get("value") or "").strip()
            blank = raw in ("", "-", "--")
            number = None if blank else parse_number(raw)
            if number is None and not (blank and code == "income.discontinued_profit"):
                # A section heading carries no figure; try the next name. Text
                # that is not a number is no figure either -- and not zero: the
                # sheet's sums can no longer be proven, so nothing is certified.
                if not blank:
                    unreadable.add(code)
                continue
            found[code] = (number if number is not None else Decimal(0), raw, by_row[rows[0]][1], value_cell)
            break
    if kind == "income":
        # An income figure belongs to a period; without a valid start (an
        # unknown `period`, or an end that is not a month end) it is not one.
        ok = bool(start) and income_reconciles({code: item[0] for code, item in found.items()})
        period_start = start
    else:
        ok = balance_reconciles({code: item[0] for code, item in found.items()})
        period_start = ""
    if unreadable - set(found):
        ok = False
    return [{
        "fact_code": code, "raw_value": raw, "numeric_value": number,
        "unit": unit, "currency": "IRR", "period_start": period_start, "period_end": end,
        "dimensions": {
            "statement_scope": "consolidated" if is_consolidated else "standalone",
            "period_months" if kind == "income" else "reporting_months": months,
            "audited": is_audited, "template": template,
        },
        "source_coordinates": {
            "sheet_code": sheet.get("code"), "table_id": table.get("metaTableId"),
            "label_address": label["address"], "address": cell["address"],
        },
        "verification_status": "reconciled" if ok else "extracted",
    } for code, (number, raw, label, cell) in found.items()]


def income_reconciles(values):
    if "income.operating_revenue" not in values or "income.net_profit" not in values:
        return False
    if "income.continuing_profit" not in values:
        return False
    if values["income.net_profit"] != values["income.continuing_profit"] + values.get("income.discontinued_profit", 0):
        return False
    if "income.cost_of_revenue" in values and values["income.cost_of_revenue"] > 0:
        return False  # Codal states costs as negatives; gross = revenue + cost
    if "income.gross_profit" in values and "income.cost_of_revenue" in values:
        return values["income.gross_profit"] == values["income.operating_revenue"] + values["income.cost_of_revenue"]
    return True


def balance_reconciles(values):
    needed = ("balance.total_assets", "balance.total_liabilities", "balance.total_equity")
    if any(code not in values for code in needed) or values["balance.total_assets"] <= 0:
        return False
    total = values["balance.total_assets"]
    if values["balance.total_liabilities"] + values["balance.total_equity"] != total:
        return False
    if values.get("balance.liabilities_and_equity", total) != total:
        return False
    for parts, whole in (
        (("balance.current_assets", "balance.noncurrent_assets"), "balance.total_assets"),
        (("balance.current_liabilities", "balance.noncurrent_liabilities"), "balance.total_liabilities"),
    ):
        if all(p in values for p in parts) and sum(values[p] for p in parts) != values[whole]:
            return False
    return True
