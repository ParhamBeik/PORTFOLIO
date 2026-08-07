"""Ingest tests pinned to payload shapes captured from the live provider.

Every fixture here is a verbatim (trimmed) record from Api.BrsApi.ir observed on
1405-05-04. Each test corresponds to a defect that shipped to production and that
the read-back verifier could not see, because it compared key presence and never
values:

- Codal answers in Persian-Indic digits, so dates were unjoinable.
- Cryptocurrency.php records carry no `symbol`, so 547 coins collapsed onto one.
- Commodity.php answers a dict of category lists, so nothing ever parsed.
- Shareholder.php carries no date, so every roster stored under "".
- History.php?type=1 is the Real/Legal breakdown, not adjusted prices.
"""
import pytest

from marketdata import ingest
from marketdata.models import (
    CodalAnnouncement,
    CommodityHistory,
    CryptoHistory,
    DailyStockHistory,
    GoldCurrencyHistory,
    ShareholderRecord,
)

pytestmark = pytest.mark.django_db


CODAL_PAYLOAD = {
    "count_announcement": 1009,
    "count_page": 51,
    "announcement": [
        {
            "l18": "فملی",
            "l30": "ملی صنایع مس ایران",
            "title": "توضیحات در خصوص اطلاعات و صورت های مالی منتشر شده",
            "code": "ن-۲۶",
            "date_title": None,
            "date_send": "۱۴۰۵/۰۵/۰۳",
            "time_send": "۱۳:۴۸:۵۴",
            "date_publish": "۱۴۰۵/۰۵/۰۳",
            "time_publish": "۱۳:۴۸:۵۴",
        }
    ],
}

CRYPTO_PAYLOAD = [
    {
        "date": "1405/05/04",
        "name_en": "Bitcoin",
        "name": "بیت‌کوین",
        "price": "64545",
        "price_toman": "12087398331",
        "market_cap": 837148080052,
        "id": 300000,
    },
    {
        "date": "1405/05/04",
        "name_en": "Ethereum",
        "name": "اتریوم",
        "price": "1885",
        "price_toman": "352975860",
        "market_cap": 265532066939,
        "id": 300001,
    },
]

COMMODITY_PAYLOAD = {
    "metal_precious": [
        {"date": "1405/05/03", "symbol": "XAUUSD", "price": 4052.85, "unit": "دلار"},
        {"date": "1405/05/03", "symbol": "XAGUSD", "price": 58.2, "unit": "دلار"},
    ],
    "metal_base": [
        {"date": "1405/05/02", "symbol": "Cu", "price": 14015, "unit": "دلار"},
    ],
    "energy": [
        {"date": "1405/05/04", "symbol": "BRENT", "price": 96.78, "unit": "دلار"},
    ],
}

SHAREHOLDER_PAYLOAD = [
    {"id": 188055, "name": "سازمان توسعه ونوسازی معادن", "volume": 121281419017, "percent": 11.55, "change": 0},
    {"id": 192627, "name": "موسسه صندوق بازنشستگی مس", "volume": 82974800148, "percent": 7.902, "change": 0},
]

REAL_LEGAL_PAYLOAD = [
    {
        "date": "1405-04-29",
        "Buy_CountI": 5970,
        "Buy_CountN": 44,
        "Sell_CountI": 2732,
        "Sell_CountN": 31,
        "Buy_I_Volume": 361199757,
        "Buy_N_Volume": 249066289,
        "Sell_I_Volume": 161586006,
        "Sell_N_Volume": 448680040,
        "Buy_I_Value": 7393536609350,
        "Buy_N_Value": 5098345397380,
        "Sell_I_Value": 3307474352240,
        "Sell_N_Value": 9184407654490,
    }
]


def test_persian_digits_fold_to_ascii():
    assert ingest.normalize_jalali("۱۴۰۵/۰۵/۰۳") == "1405-05-03"
    assert ingest.fold_digits("۱۳:۴۸:۵۴") == "13:48:54"
    assert ingest.fold_digits("ن-۲۶") == "ن-26"
    # Arabic-Indic digits fold too, and ASCII input is untouched.
    assert ingest.normalize_jalali("١٤٠٥/٠٥/٠٣") == "1405-05-03"
    assert ingest.normalize_jalali("1405-05-03") == "1405-05-03"


def test_codal_stores_joinable_ascii_dates():
    created, _ = ingest.ingest_codal(CODAL_PAYLOAD)
    assert created == 1
    row = CodalAnnouncement.objects.get()
    assert row.date_publish == "1405-05-03"
    assert row.time_publish == "13:48:54"
    assert row.code == "ن-26"


def test_crypto_keeps_every_coin_apart():
    created, _ = ingest.ingest_crypto_history("CRYPTO", CRYPTO_PAYLOAD)
    assert created == 2
    assert set(CryptoHistory.objects.values_list("symbol", flat=True)) == {
        "Bitcoin",
        "Ethereum",
    }
    btc = CryptoHistory.objects.get(symbol="Bitcoin")
    assert btc.date == "1405-05-04"
    assert float(btc.close_price_usd) == 64545.0
    assert btc.market_cap == 837148080052


def test_commodity_reads_the_nested_category_lists():
    created, _ = ingest.ingest_commodity_history("COMMODITIES", COMMODITY_PAYLOAD)
    assert created == 4
    assert set(CommodityHistory.objects.values_list("symbol", flat=True)) == {
        "XAUUSD",
        "XAGUSD",
        "Cu",
        "BRENT",
    }
    assert CommodityHistory.objects.get(symbol="BRENT").date == "1405-05-04"


def test_shareholder_rows_get_a_date_when_the_caller_omits_one():
    created, _ = ingest.ingest_shareholders("فملی", SHAREHOLDER_PAYLOAD)
    assert created == 2
    assert not ShareholderRecord.objects.filter(date="").exists()
    assert ShareholderRecord.objects.values("date").distinct().count() == 1


def test_real_legal_lands_on_the_existing_price_row():
    DailyStockHistory.objects.create(
        symbol="فملی", date="1405-04-29", is_adjusted=False, pl=20470
    )
    updated, skipped = ingest.ingest_real_legal("فملی", REAL_LEGAL_PAYLOAD)
    assert (updated, skipped) == (1, 0)
    row = DailyStockHistory.objects.get(symbol="فملی", is_adjusted=False)
    assert row.buy_count_i == 5970
    assert row.sell_n_value == 9184407654490
    # The price it was carrying is untouched, and no phantom adjusted row appears.
    assert row.pl == 20470
    assert not DailyStockHistory.objects.filter(is_adjusted=True).exists()


def test_real_legal_skips_days_with_no_price_row_yet():
    updated, skipped = ingest.ingest_real_legal("فملی", REAL_LEGAL_PAYLOAD)
    assert updated == 0 and skipped == 1


def test_gold_usdt_is_canonicalised_on_read_and_write():
    payload = {
        "symbol": "USDT",
        "name": "تتر",
        "unit": "ریال",
        "history_daily": [
            {"date": "1405-05-04", "open": 1000000, "high": 1010000, "low": 990000, "close": 1005000}
        ],
    }
    assert ingest.canonical_gold_symbol(payload) == "USDT_IRT"
    created, _ = ingest.ingest_gold_currency_history(payload)
    assert created == 1
    assert GoldCurrencyHistory.objects.filter(symbol="USDT_IRT").exists()
    # Storage unit is Rial; Rial-declared quotes pass through unchanged.
    assert float(GoldCurrencyHistory.objects.get().close_price) == 1005000.0


def test_flatten_records_handles_all_three_payload_shapes():
    assert len(ingest.flatten_records(CRYPTO_PAYLOAD)) == 2
    assert len(ingest.flatten_records(COMMODITY_PAYLOAD)) == 4
    assert ingest.flatten_records({"date": "1405-05-04"}) == [{"date": "1405-05-04"}]
    assert ingest.flatten_records(None) == []
