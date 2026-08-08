"""Lossless document parsing plus category-specific canonical fact extraction."""

import io
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings


PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# Each category owns a stable fact namespace. Unknown columns remain in the raw
# table even when they cannot safely become canonical facts.
CATEGORY_FIELDS = {
    1: {"مبلغ": "disclosure.amount", "نرخ": "disclosure.rate", "تاریخ": "disclosure.date"},
    2: {"دارایی": "financial.assets", "بدهی": "financial.liabilities", "درآمد": "financial.revenue", "سود": "financial.profit", "وجه نقد": "financial.cash_flow"},
    3: {"تولید": "production.quantity", "فروش": "sales.quantity", "نرخ فروش": "sales.rate", "مبلغ فروش": "sales.revenue"},
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


def parse_number(value):
    normalized = _clean(value).translate(PERSIAN_DIGITS)
    normalized = normalized.replace("٬", "").replace(",", "").replace("−", "-")
    normalized = normalized.replace("٫", ".").replace("(", "-").replace(")", "")
    normalized = re.sub(r"[^0-9.\-]", "", normalized)
    if not normalized or normalized in ("-", ".", "-."):
        return None
    try:
        return Decimal(normalized)
    except InvalidOperation:
        return None


def parse_excel(content):
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


def _ocr_pdf(content):
    import pytesseract
    from PIL import Image

    texts, confidences = [], []
    with tempfile.TemporaryDirectory(prefix="codal-ocr-") as tmp:
        source = Path(tmp) / "source.pdf"
        source.write_bytes(content)
        subprocess.run(
            ["pdftoppm", "-f", "1", "-l", "50", "-r", "200", "-png", str(source), str(Path(tmp) / "page")],
            check=True,
            timeout=180,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for image_path in sorted(Path(tmp).glob("page-*.png")):
            data = pytesseract.image_to_data(Image.open(image_path), lang="fas+eng", output_type=pytesseract.Output.DICT)
            words = []
            for word, confidence in zip(data["text"], data["conf"]):
                word = word.strip()
                try:
                    score = float(confidence)
                except (TypeError, ValueError):
                    continue
                if word and score >= 0:
                    words.append(word)
                    confidences.append(score / 100)
            texts.append(" ".join(words))
    return "\n".join(texts), (sum(confidences) / len(confidences) if confidences else 0.0)


def parse_pdf(content):
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content), strict=True)
    if len(reader.pages) > 200:
        raise ValueError("pdf_page_limit")
    text = "\n".join(page.extract_text() or "" for page in reader.pages).strip()
    used_ocr = len(text) < 80
    confidence = 1.0
    if used_ocr:
        text, confidence = _ocr_pdf(content)
    sections = [
        {"heading": f"page_{index + 1}", "body": body, "source_coordinates": {"page": index + 1}}
        for index, body in enumerate(filter(None, (part.strip() for part in text.split("\f"))))
    ]
    return ParsedDocument(sections=sections, text=text, confidence=confidence, used_ocr=used_ocr)


def extract_typed_facts(parsed, category, period_end=""):
    fields = CATEGORY_FIELDS.get(category, {})
    facts = []
    for table_index, table in enumerate(parsed.tables):
        headers = table["headers"]
        for row_index, row in enumerate(table["rows"]):
            dimensions = {}
            if row:
                dimensions["label"] = _clean(row[0])
            for col_index, header in enumerate(headers):
                header_text = _clean(header)
                fact_code = next((code for keyword, code in fields.items() if keyword in header_text), None)
                if not fact_code or col_index >= len(row):
                    continue
                value = _clean(row[col_index])
                number = parse_number(value)
                facts.append({
                    "fact_code": fact_code,
                    "numeric_value": number,
                    "text_value": "" if number is not None else value,
                    "period_end": period_end,
                    "dimensions": dimensions,
                    "confidence": parsed.confidence,
                    "quality": "validated",
                    "source_coordinates": {"table_index": table_index, "row": row_index + 2, "column": col_index + 1},
                })
    if not facts:
        for line_index, line in enumerate(parsed.text.splitlines()):
            for keyword, fact_code in fields.items():
                if keyword not in line:
                    continue
                number = parse_number(line)
                facts.append({
                    "fact_code": fact_code,
                    "numeric_value": number,
                    "text_value": line if number is None else "",
                    "period_end": period_end,
                    "dimensions": {},
                    "confidence": parsed.confidence,
                    "quality": "validated",
                    "source_coordinates": {"line": line_index + 1},
                })
    parsed.facts = facts
    return parsed


def category_reconciles(parsed, category):
    """Conservative publication gate: known category plus at least one typed fact."""
    return bool(category in CATEGORY_FIELDS and parsed.facts)


def parse_artifact(kind, content):
    if kind == "excel":
        return parse_excel(content)
    if kind == "html":
        return parse_html(content)
    if kind == "pdf":
        return parse_pdf(content)
    return ParsedDocument()
