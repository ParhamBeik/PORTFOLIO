"""Monthly activity reports (گزارش فعالیت ماهانه) -- the largest uniform family.

17,793 documents across 725 companies, every one carrying an Excel link, so they
parse without OCR. They are monthly production and sales per product, which makes
them the richest recurring financial dataset in the corpus.

NOTE: codal.ir is unreachable from this environment, so the column layout below
is modelled on the published template rather than confirmed against a real file.
The extraction rules are what these tests pin; the header spellings may need one
correction the first time this runs against live documents.
"""
import io

import pytest
from openpyxl import Workbook

from marketdata.codal_parsers import extract_typed_facts, parse_excel

PRODUCTION_AND_SALES = 3


def _workbook(rows, sheet_title="فروش داخلی"):
    book = Workbook()
    sheet = book.active
    sheet.title = sheet_title
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


HEADERS = ["نام محصول", "واحد", "مقدار تولید", "مقدار فروش", "نرخ فروش", "مبلغ فروش"]


def _facts_by_code(rows, **kwargs):
    parsed = parse_excel(_workbook([HEADERS] + rows, **kwargs))
    parsed = extract_typed_facts(parsed, PRODUCTION_AND_SALES, period_end="1405-04-31")
    grouped = {}
    for fact in parsed.facts:
        grouped.setdefault(fact["fact_code"], []).append(fact)
    return grouped


def test_rate_and_revenue_are_not_recorded_as_quantity():
    """"فروش" is a substring of "نرخ فروش" and "مبلغ فروش".

    Under first-match ordering all three columns produced `sales.quantity`, so a
    price and a rial total were indistinguishable from a tonnage.
    """
    grouped = _facts_by_code([["ورق گرم", "تن", 1200, 1100, 450000, 495000000]])

    assert set(grouped) == {
        "production.quantity", "sales.quantity", "sales.rate", "sales.revenue",
    }
    assert grouped["production.quantity"][0]["numeric_value"] == 1200
    assert grouped["sales.quantity"][0]["numeric_value"] == 1100
    assert grouped["sales.rate"][0]["numeric_value"] == 450000
    assert grouped["sales.revenue"][0]["numeric_value"] == 495000000


def test_product_and_unit_describe_the_row_rather_than_becoming_facts():
    grouped = _facts_by_code([["ورق گرم", "تن", 1200, 1100, 450000, 495000000]])

    dimensions = grouped["sales.revenue"][0]["dimensions"]
    assert dimensions["product"] == "ورق گرم"
    assert dimensions["unit"] == "تن"
    # A unit label is not a measurement; it must not appear as a numeric fact.
    assert not any(code.endswith(".unit") for code in grouped)


def test_sales_channel_is_taken_from_the_sheet():
    """The same product appears twice a month, home and export. Without the
    channel the two rows are indistinguishable and silently conflict."""
    domestic = _facts_by_code([["ورق گرم", "تن", 1200, 1100, 450000, 495000000]])
    export = _facts_by_code(
        [["ورق گرم", "تن", 1200, 900, 620000, 558000000]], sheet_title="فروش صادراتی"
    )

    assert domestic["sales.rate"][0]["dimensions"]["channel"] == "domestic"
    assert export["sales.rate"][0]["dimensions"]["channel"] == "export"


def test_persian_digits_and_thousands_separators_parse():
    grouped = _facts_by_code([["ورق گرم", "تن", "۱٬۲۰۰", "۱٬۱۰۰", "۴۵۰٬۰۰۰", "۴۹۵٬۰۰۰٬۰۰۰"]])

    assert grouped["production.quantity"][0]["numeric_value"] == 1200
    assert grouped["sales.revenue"][0]["numeric_value"] == 495000000


def test_every_product_row_is_extracted():
    grouped = _facts_by_code([
        ["ورق گرم", "تن", 1200, 1100, 450000, 495000000],
        ["ورق سرد", "تن", 800, 750, 520000, 390000000],
        ["تیرآهن", "تن", 300, 290, 380000, 110200000],
    ])

    assert len(grouped["sales.revenue"]) == 3
    assert {f["dimensions"]["product"] for f in grouped["sales.revenue"]} == {
        "ورق گرم", "ورق سرد", "تیرآهن",
    }
