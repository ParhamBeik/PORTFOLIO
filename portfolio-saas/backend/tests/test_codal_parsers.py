"""Unit tests for marketdata/codal_parsers.py: Persian number normalization,
the longest-keyword-match fix (see _match_fact_code docstring), and that
parse_pdf degrades to an empty ParsedDocument rather than crashing or OCR'ing
when there is no text layer."""
from decimal import Decimal

import pytest

from marketdata import codal_parsers


def test_parse_number_handles_persian_digits_and_thousands_separators():
    assert codal_parsers.parse_number("۱٬۲۳۴٫۵") == Decimal("1234.5")


def test_parse_number_handles_parenthesized_negatives():
    assert codal_parsers.parse_number("(500)") == Decimal("-500")


def test_parse_number_returns_none_for_non_numeric():
    assert codal_parsers.parse_number("سود") is None


def test_longest_keyword_match_does_not_collapse_rate_and_revenue_into_quantity():
    fields = codal_parsers.CATEGORY_FIELDS[3]
    assert codal_parsers._match_fact_code("نرخ فروش", fields) == "sales.rate"
    assert codal_parsers._match_fact_code("مبلغ فروش", fields) == "sales.revenue"
    assert codal_parsers._match_fact_code("مقدار فروش", fields) == "sales.quantity"


def test_extract_typed_facts_from_a_table():
    parsed = codal_parsers.ParsedDocument(tables=[{
        "name": "sheet1", "sheet_name": "sheet1",
        "headers": ["محصول", "مقدار فروش", "نرخ فروش"],
        "rows": [["کاما", "۱۰۰", "۲۰۰۰"]],
        "source_coordinates": {},
    }])
    result = codal_parsers.extract_typed_facts(parsed, category=3)
    codes = {fact["fact_code"] for fact in result.facts}
    assert codes == {"sales.quantity", "sales.rate"}
    assert codal_parsers.category_reconciles(result, category=3) is True


def test_category_reconciles_is_false_without_facts():
    parsed = codal_parsers.ParsedDocument()
    assert codal_parsers.category_reconciles(parsed, category=3) is False


def test_parse_pdf_with_no_text_layer_yields_empty_document_not_a_crash():
    from io import BytesIO

    # pypdf ships in requirements.txt (the Codal worker needs it) but is not
    # required to run the rest of the suite on a bare local venv.
    PdfWriter = pytest.importorskip("pypdf").PdfWriter

    buffer = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(buffer)

    parsed = codal_parsers.parse_pdf(buffer.getvalue())
    assert parsed.text == ""
    assert parsed.used_ocr is False
    assert parsed.confidence == 1.0
