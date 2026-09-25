"""Lossless document parsing plus category-specific canonical fact extraction.

Ported from the pipeline stripped in commit 2ea22be. OCR (scanned-PDF fallback,
originally `pytesseract`+`poppler-utils`) is deliberately not restored here --
`parse_pdf` returns whatever text pypdf extracts, empty for a scanned document,
never worse than dropping the document entirely. Revisit when OCR support is
added back as its own change.
"""

import io
import re
from datetime import timedelta
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .jalali import from_gregorian, is_jalali, normalize_jalali, to_gregorian


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Each category owns a stable fact namespace. Unknown columns remain in the raw
# table even when they cannot safely become canonical facts.
CATEGORY_FIELDS = {
    1: {"مبلغ": "disclosure.amount", "نرخ": "disclosure.rate", "تاریخ": "disclosure.date"},
    2: {"دارایی": "financial.assets", "بدهی": "financial.liabilities", "درآمد": "financial.revenue", "سود": "financial.profit", "وجه نقد": "financial.cash_flow"},
    # Monthly activity reports (گزارش فعالیت ماهانه) are the largest uniform
    # family in the corpus -- 17,038 documents (see the coverage census), every
    # one with an Excel link, so they parse without OCR. "مقدار فروش" and
    # "تعداد فروش" are both used for quantity depending on the template vintage.
    3: {
        "تولید": "production.quantity",
        "مقدار تولید": "production.quantity",
        "فروش": "sales.quantity",
        "مقدار فروش": "sales.quantity",
        "تعداد فروش": "sales.quantity",
        "نرخ فروش": "sales.rate",
        "نرخ": "sales.rate",
        "مبلغ فروش": "sales.revenue",
        "مبلغ": "sales.revenue",
    },
    4: {"پیش بینی": "board.forecast", "ریسک": "board.risk", "عملکرد": "board.kpi"},
    5: {"اظهارنظر": "auditor.opinion", "بند": "auditor.qualification", "تاکید": "auditor.emphasis"},
    6: {"حاضرین": "assembly.quorum", "سود نقدی": "assembly.dividend", "تصویب": "assembly.approval"},
    7: {"سرمایه فعلی": "capital.current", "سرمایه جدید": "capital.new", "درصد افزایش": "capital.percentage", "آورده": "capital.funding_source"},
    8: {"تعداد سهام": "portfolio.shares", "بهای تمام شده": "portfolio.cost", "ارزش بازار": "portfolio.market_value", "سود": "portfolio.gain_loss"},
    9: {"کمیته": "governance.committee", "عضو": "governance.member", "سمت": "governance.role", "استقلال": "governance.independence"},
    10: {"دارایی": "subsidiary.assets", "بدهی": "subsidiary.liabilities", "درآمد": "subsidiary.revenue", "سود": "subsidiary.profit"},
    11: {"حجم عرضه": "prospectus.offer_volume", "قیمت عرضه": "prospectus.offer_price", "مصرف وجوه": "prospectus.use_of_proceeds", "تعهد": "prospectus.commitment"},
}


@dataclass
class ParsedDocument:
    tables: list = field(default_factory=list)
    sections: list = field(default_factory=list)
    facts: list = field(default_factory=list)
    text: str = ""
    confidence: float = 1.0
    used_ocr: bool = False


def _clean(value):
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()


_MAX_DECIMAL = Decimal("1e26")
_NUMBER = re.compile(r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?\Z")


def parse_number(value):
    normalized = _clean(value).translate(PERSIAN_DIGITS)
    normalized = normalized.replace("٬", ",").replace("٫", ".").replace("−", "-")
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = "-" + normalized[1:-1].strip()
    if not _NUMBER.fullmatch(normalized):
        return None
    try:
        parsed = Decimal(normalized.replace(",", ""))
        if abs(parsed) >= _MAX_DECIMAL:
            return None
        return parsed
    except InvalidOperation:
        return None


def parse_excel(content):
    sample = content[:2048].lstrip().lower()
    if b"<html" in sample or b"<!doctype html" in sample or b"<table" in sample:
        return parse_html(content)
    from openpyxl import load_workbook

    workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    parsed = ParsedDocument()
    try:
        for sheet in workbook.worksheets:
            rows = [[_clean(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
            rows = [row for row in rows if any(row)]
            if not rows:
                continue
            parsed.tables.append({
                "name": sheet.title,
                "sheet_name": sheet.title,
                "headers": rows[0],
                "rows": rows[1:],
                "source_coordinates": {"sheet": sheet.title, "range": sheet.calculate_dimension()},
            })
        parsed.text = "\n".join(" | ".join(row) for table in parsed.tables for row in [table["headers"], *table["rows"]])
    finally:
        workbook.close()
    return parsed


def parse_html(content):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "lxml")
    parsed = ParsedDocument()
    for table_index, table in enumerate(soup.find_all("table")):
        rows = _html_grid(table)
        if rows:
            # Codal's Excel export is often HTML. The first header groups
            # periods with colspan, and the next names the metric. Treating
            # the latter as a data row made the whole report fall through to
            # line matching, which stored headings as revenue candidates.
            header_depth = 2 if len(rows) > 1 and any(
                cell.get("colspan", 1) > 1 for cell in rows[0][1]
            ) else 1
            header_parts = [
                [rows[level][0][column] for level in range(header_depth)]
                for column in range(len(rows[0][0]))
            ]
            data_rows = [
                (physical_row, values)
                for grid_row, (values, _, physical_row) in enumerate(rows)
                if grid_row >= header_depth and any(values)
            ]
            parsed.tables.append({
                "name": table.get("id") or f"table_{table_index}",
                "sheet_name": "",
                "headers": [" | ".join(dict.fromkeys(filter(None, parts))) for parts in header_parts],
                "header_parts": header_parts,
                "header_depth": header_depth,
                "rows": [values for _, values in data_rows],
                "row_numbers": [source_row for source_row, _ in data_rows],
                "source_coordinates": {"css": f"table:nth-of-type({table_index + 1})"},
            })
    for index, node in enumerate(soup.find_all(["h1", "h2", "h3", "p", "section"])):
        body = node.get_text(" ", strip=True)
        if body:
            parsed.sections.append({"heading": node.name, "body": body, "source_coordinates": {"element_index": index}})
    parsed.text = soup.get_text("\n", strip=True)
    return parsed


def _html_grid(table):
    """Expand rowspan/colspan into aligned rows while bounding hostile HTML."""
    grid = []
    pending = {}
    for physical_row, tr in enumerate(table.find_all("tr"), start=1):
        cells = tr.find_all(["th", "td"], recursive=False)
        if not cells:
            continue
        values, metadata = [], []
        column = 0

        def fill_pending():
            nonlocal column
            while column in pending:
                value, remaining = pending[column]
                values.append(value)
                metadata.append({})
                if remaining == 1:
                    del pending[column]
                else:
                    pending[column] = (value, remaining - 1)
                column += 1

        for cell in cells:
            fill_pending()
            try:
                colspan = int(cell.get("colspan", 1))
                rowspan = int(cell.get("rowspan", 1))
            except (ValueError, TypeError) as exc:
                raise ValueError("invalid_html_table_span") from exc
            if not (1 <= colspan <= 256 and 1 <= rowspan <= 256) or column + colspan > 256:
                raise ValueError("html_table_span_limit")
            value = cell.get_text(" ", strip=True)
            for offset in range(colspan):
                values.append(value)
                metadata.append({"colspan": colspan, "rowspan": rowspan})
                if rowspan > 1:
                    pending[column + offset] = (value, rowspan - 1)
            column += colspan
        fill_pending()
        if len(grid) >= 10000:
            raise ValueError("html_table_row_limit")
        grid.append((values, metadata, physical_row))
    if grid:
        width = max(len(values) for values, _, _ in grid)
        for values, metadata, _ in grid:
            values.extend([""] * (width - len(values)))
            metadata.extend([{}] * (width - len(metadata)))
    return grid


def parse_pdf(content):
    """Text-layer extraction only -- no OCR fallback (see module docstring).

    A scanned PDF with no text layer yields an empty ParsedDocument, which
    `extract_report` (codal_pipeline.py) already treats as unparseable and
    routes to `unsupported_template` rather than silently publishing nothing.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=True)
    if len(reader.pages) > 200:
        raise ValueError("pdf_page_limit")
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    sections = [
        {"heading": f"page_{index + 1}", "body": body, "source_coordinates": {"page": index + 1}}
        for index, body in enumerate(filter(None, (part.strip() for part in text.split("\f"))))
    ]
    return ParsedDocument(sections=sections, text=text, confidence=1.0, used_ocr=False)


# Columns whose value describes the row rather than measuring it.
_DIMENSION_HEADERS = {"واحد": "unit", "محصول": "product", "نام محصول": "product"}
# Monthly reports split the same product between home and export sales. Which
# one a row belongs to is stated in the sheet or in the row's own label.
_SALES_CHANNELS = {"داخلی": "domestic", "صادرات": "export", "صادراتی": "export"}


def _match_fact_code(header_text, fields):
    """Longest keyword wins.

    First-match ordering silently mis-typed the most valuable family in the
    corpus: "فروش" is a substring of "نرخ فروش" and "مبلغ فروش", so a monthly
    report's rate and revenue columns were both recorded as sales *quantity* --
    three different measures collapsed onto one fact code.
    """
    matches = [
        (len(keyword), code)
        for keyword, code in fields.items()
        if keyword and keyword in header_text
    ]
    return max(matches)[1] if matches else None


def _row_dimensions(headers, row, table):
    """Describe a row: product, unit, and which sales channel it belongs to."""
    dimensions = {}
    if row:
        dimensions["label"] = _clean(row[0])
    context = f"{table.get('sheet_name', '')} {table.get('name', '')} {dimensions.get('label', '')}"
    for keyword, channel in _SALES_CHANNELS.items():
        if keyword in context:
            dimensions["channel"] = channel
            break
    for col_index, header in enumerate(headers):
        header_text = _clean(header)
        for keyword, name in _DIMENSION_HEADERS.items():
            if keyword in header_text and col_index < len(row):
                value = _clean(row[col_index])
                if value:
                    dimensions[name] = value
    return dimensions


_MONTHLY_PERIOD = re.compile(r"دوره\s*(?:یک\s*ماهه|یکماهه)")
_PERSIAN_DATE = re.compile(r"[۰-۹٠-٩0-9]{4}/[۰-۹٠-٩0-9]{1,2}/[۰-۹٠-٩0-9]{1,2}")


def _monthly_sales_facts(table, period_end):
    """Extract only the current month of a Codal product-sales matrix.

    The same table carries prior month, corrections, YTD, and prior-year YTD;
    collapsing its repeated `مبلغ فروش` leaves six incompatible values with
    the same fact code. A period header, leaf metric, and source cell must all
    be present before a numeric candidate is emitted.
    """
    parts = table.get("header_parts") or []
    if len(parts) < 4 or not is_jalali(period_end):
        return None
    # The report names a one-month ending date, not an explicit starting date.
    # Only infer day 01 when the end is the last Jalali day of that month.
    if from_gregorian(to_gregorian(period_end) + timedelta(days=1))[:7] == period_end[:7]:
        return None
    if "نام محصول" not in parts[0][-1] or "واحد" not in parts[1][-1]:
        return None
    columns = []
    for index, headings in enumerate(parts):
        if len(headings) < 2:
            continue
        group, leaf = headings[0], headings[-1]
        period_match = _PERSIAN_DATE.search(group)
        if not _MONTHLY_PERIOD.search(group) or not period_match:
            continue
        if normalize_jalali(period_match.group()) != period_end:
            continue
        fact_code = {
            "مبلغ فروش": "sales.revenue",
            "تعداد فروش": "sales.quantity",
            "مقدار فروش": "sales.quantity",
            "تعداد تولید": "production.quantity",
            "نرخ فروش": "sales.rate",
        }
        matched = next((code for label, code in fact_code.items() if label in leaf), None)
        if matched == "sales.revenue" and "میلیون ریال" not in " ".join(leaf.split()):
            continue
        if matched == "sales.rate" and "ریال" not in leaf:
            continue
        if matched:
            columns.append((index, matched, leaf))
    if not any(code == "sales.revenue" for _, code, _ in columns):
        return None

    month_start = f"{period_end[:8]}01"
    facts = []
    channel = ""
    for row_index, row in enumerate(table["rows"]):
        label = _clean(row[0])
        if not label:
            continue
        if label.endswith(":"):
            channel = label.rstrip(": ")
            continue
        kind = "total" if label == "جمع" else "subtotal" if label.startswith("جمع ") else "product"
        product_unit = _clean(row[1]) if len(row) > 1 else ""
        for column, code, leaf in columns:
            raw = _clean(row[column]) if column < len(row) else ""
            number = parse_number(raw)
            if number is None:
                continue
            if code == "sales.revenue":
                unit, currency = "million_rial", "IRR"
            elif code == "sales.rate":
                unit, currency = f"rial_per_{product_unit or 'declared_unit'}", "IRR"
            else:
                unit, currency = product_unit, ""
            facts.append({
                "fact_code": code,
                "raw_value": raw,
                "numeric_value": number,
                "text_value": "",
                "unit": unit,
                "currency": currency,
                "period_start": month_start,
                "period_end": period_end,
                "dimensions": {
                    "product": label,
                    "channel": "" if kind == "total" else channel,
                    "row_kind": kind,
                    "period_basis": "one_month",
                    "declared_quantity_unit": product_unit,
                },
                "confidence": 1.0,
                "quality": "extracted",
                "source_coordinates": {
                    **table.get("source_coordinates", {}),
                    "row": table.get("row_numbers", [])[row_index] if table.get("row_numbers") else row_index + table.get("header_depth", 1) + 1,
                    "column": column + 1,
                    "column_heading": leaf,
                },
            })
    return facts


def reconcile_monthly_sales(facts):
    """Return the one source-arithmetic-checked monthly total, if provable.

    This checks the published subtotals against product rows, then the grand
    total against domestic + export + services - returns - discounts. Missing
    components, duplicate totals, mixed periods or mixed units fail closed.
    """
    revenue = [fact for fact in facts if fact["fact_code"] == "sales.revenue"]
    if not revenue:
        return None
    periods = {(fact.get("period_start"), fact.get("period_end")) for fact in revenue}
    if len(periods) != 1 or not all(
        fact.get("unit") == "million_rial"
        and fact.get("currency") == "IRR"
        and fact.get("dimensions", {}).get("period_basis") == "one_month"
        for fact in revenue
    ):
        return None

    def normalized(fact):
        return fact["dimensions"]["product"].replace("ي", "ی").replace("ك", "ک").strip()

    by_label = {}
    for fact in revenue:
        by_label.setdefault(normalized(fact), []).append(fact)
    required = (
        "جمع فروش داخلی", "جمع فروش صادراتی", "جمع درآمد ارائه خدمات",
        "جمع برگشت از فروش", "تخفیفات", "جمع",
    )
    if any(len(by_label.get(label, [])) != 1 for label in required):
        return None
    total = by_label["جمع"][0]
    if total["dimensions"].get("row_kind") != "total":
        return None
    for channel, label in (
        ("فروش داخلی", "جمع فروش داخلی"),
        ("فروش صادراتی", "جمع فروش صادراتی"),
        ("درآمد ارائه خدمات", "جمع درآمد ارائه خدمات"),
    ):
        products = [
            fact["numeric_value"] for fact in revenue
            if fact["dimensions"].get("row_kind") == "product"
            and fact["dimensions"].get("channel", "").replace("ي", "ی") == channel
        ]
        subtotal = by_label[label][0]["numeric_value"]
        if (products and sum(products) != subtotal) or (not products and subtotal != 0):
            return None
    expected = (
        by_label["جمع فروش داخلی"][0]["numeric_value"]
        + by_label["جمع فروش صادراتی"][0]["numeric_value"]
        + by_label["جمع درآمد ارائه خدمات"][0]["numeric_value"]
        - by_label["جمع برگشت از فروش"][0]["numeric_value"]
        - by_label["تخفیفات"][0]["numeric_value"]
    )
    return total if total["numeric_value"] == expected else None


def extract_typed_facts(parsed, category, period_end=""):
    fields = CATEGORY_FIELDS.get(category, {})
    facts = []
    if category == 3:
        monthly = [_monthly_sales_facts(table, period_end) for table in parsed.tables]
        if any(result is not None for result in monthly):
            parsed.facts = [fact for result in monthly if result is not None for fact in result]
            return parsed
    for table_index, table in enumerate(parsed.tables):
        if category == 3 and table.get("header_depth", 1) > 1:
            # An unfamiliar multi-period matrix cannot safely be interpreted
            # by keyword matching a leaf heading alone.
            continue
        headers = table["headers"]
        for row_index, row in enumerate(table["rows"]):
            dimensions = _row_dimensions(headers, row, table)
            for col_index, header in enumerate(headers):
                header_text = _clean(header)
                if any(key in header_text for key in _DIMENSION_HEADERS):
                    continue
                fact_code = _match_fact_code(header_text, fields)
                if not fact_code or col_index >= len(row):
                    continue
                value = _clean(row[col_index])
                number = parse_number(value)
                facts.append({
                    "fact_code": fact_code,
                    "raw_value": value,
                    "numeric_value": number,
                    "text_value": "" if number is not None else value,
                    "period_end": period_end,
                    "dimensions": dimensions,
                    "confidence": parsed.confidence,
                    "quality": "extracted",
                    "source_coordinates": {"table_index": table_index, "row": row_index + table.get("header_depth", 1) + 1, "column": col_index + 1},
                })
    if not facts and not parsed.tables:
        # No tables at all -- a PDF document. One fact per line at most, under
        # the same longest-match rule the table path uses.
        for line_index, line in enumerate(parsed.text.splitlines()):
            fact_code = _match_fact_code(line, fields)
            if not fact_code:
                continue
            # A text line is context, not a numeric cell. The announcement
            # number in a correction notice must never become revenue.
            number = None
            facts.append({
                "fact_code": fact_code,
                "raw_value": line,
                "numeric_value": number,
                "text_value": line if number is None else "",
                "period_end": period_end,
                "dimensions": {},
                "confidence": parsed.confidence,
                "quality": "extracted",
                "source_coordinates": {"line": line_index + 1},
            })
    parsed.facts = facts
    return parsed


def category_reconciles(parsed, category):
    """Extraction-coverage signal, never a financial validation verdict."""
    return bool(category in CATEGORY_FIELDS and parsed.facts)


def parse_artifact(kind, content):
    if kind == "excel":
        return parse_excel(content)
    if kind == "html":
        return parse_html(content)
    if kind == "pdf":
        return parse_pdf(content)
    return ParsedDocument()
