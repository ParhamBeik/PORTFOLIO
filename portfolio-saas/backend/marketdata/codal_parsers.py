"""Lossless document parsing plus category-specific canonical fact extraction.

Ported from the pipeline stripped in commit 2ea22be. OCR (scanned-PDF fallback,
originally `pytesseract`+`poppler-utils`) is deliberately not restored here --
`parse_pdf` returns whatever text pypdf extracts, empty for a scanned document,
never worse than dropping the document entirely. Revisit when OCR support is
added back as its own change.
"""

import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


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
        rows = [[cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])] for row in table.find_all("tr")]
        rows = [row for row in rows if any(row)]
        if rows:
            parsed.tables.append({
                "name": table.get("id") or f"table_{table_index}",
                "sheet_name": "",
                "headers": rows[0],
                "rows": rows[1:],
                "source_coordinates": {"css": f"table:nth-of-type({table_index + 1})"},
            })
    for index, node in enumerate(soup.find_all(["h1", "h2", "h3", "p", "section"])):
        body = node.get_text(" ", strip=True)
        if body:
            parsed.sections.append({"heading": node.name, "body": body, "source_coordinates": {"element_index": index}})
    parsed.text = soup.get_text("\n", strip=True)
    return parsed


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


def extract_typed_facts(parsed, category, period_end=""):
    fields = CATEGORY_FIELDS.get(category, {})
    facts = []
    for table_index, table in enumerate(parsed.tables):
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
                    "source_coordinates": {"table_index": table_index, "row": row_index + 2, "column": col_index + 1},
                })
    if not facts:
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
