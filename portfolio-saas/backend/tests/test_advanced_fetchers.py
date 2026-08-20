"""Unit tests for BRS API multi-endpoint fetchers and database models."""
from unittest.mock import patch
import pytest

from marketdata.models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)
from marketdata.fetchers import (
    fetch_candlesticks,
    fetch_codal_announcements,
    fetch_daily_history,
    fetch_gold_currency_free,
    fetch_gold_currency_pro,
    fetch_gold_currency_pro_history_daily,
    fetch_market_index,
    fetch_shareholders,
    fetch_symbol_data,
    fetch_transactions,
)


@pytest.mark.django_db
def test_fetch_gold_currency_free():
    mock_payload = {
        "gold": [
            {"symbol": "IR_GOLD_18K", "name": "طلای 18 عیار", "price": 6214700, "unit": "تومان"}
        ],
        "currency": [
            {"symbol": "USD", "name": "دلار", "price": 81650, "unit": "تومان"}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_gold_currency_free("test_key")
        assert res == mock_payload
        assert len(res["gold"]) == 1
        assert res["currency"][0]["symbol"] == "USD"


@pytest.mark.django_db
def test_fetch_gold_currency_pro_history_daily():
    mock_payload = {
        "symbol": "IR_COIN_EMAMI",
        "name": "سکه امامی",
        "unit": "تومان",
        "history_daily": [
            {"date": "1404/03/21", "open": 73290000, "high": 73610000, "low": 73080000, "close": 73385000}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_gold_currency_pro_history_daily("test_key", "IR_COIN_EMAMI")
        assert res == mock_payload
        record = res["history_daily"][0]

        # Test model persistence
        obj = GoldCurrencyHistory.objects.create(
            symbol=res["symbol"],
            name=res["name"],
            unit=res["unit"],
            date=record["date"],
            open_price=record["open"],
            high_price=record["high"],
            low_price=record["low"],
            close_price=record["close"],
        )
        assert obj.symbol == "IR_COIN_EMAMI"
        assert obj.close_price == 73385000


@pytest.mark.django_db
def test_fetch_symbol_data_and_model_persistence():
    mock_payload = {
        "id": 65883838195688438,
        "l18": "خودرو",
        "l30": "ایران‌ خودرو",
        "l30_en": "Iran Khodro",
        "isin": "IRO1IKCO0001",
        "m": "بورس",
        "cs": "خودرو و ساخت قطعات",
        "z": 301656068000,
        "bvol": 30310685,
        "mv": 1165599046752000,
        "eps": -784,
        "pe": -4.93,
        "state": "مجاز",
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_symbol_data("test_key", "خودرو")
        assert res["l18"] == "خودرو"

        meta = StockSymbolMetadata.objects.create(
            ins_code=res["id"],
            l18=res["l18"],
            l30=res["l30"],
            l30_en=res["l30_en"],
            isin=res["isin"],
            market=res["m"],
            sector=res["cs"],
            shares_count=res["z"],
            base_volume=res["bvol"],
            market_cap=res["mv"],
            eps=res["eps"],
            pe=res["pe"],
            state=res["state"],
        )
        assert meta.ins_code == 65883838195688438
        assert meta.l18 == "خودرو"


@pytest.mark.django_db
def test_fetch_daily_history_and_real_legal():
    mock_payload = [
        {
            "date": "1403-10-19",
            "time": "12:29:59",
            "tno": 7301,
            "tvol": 129326764,
            "tval": 1108829664180,
            "pmin": 8490,
            "pmax": 8680,
            "py": 8430,
            "pf": 8570,
            "pl": 8500,
            "plc": 70,
            "plp": 0.83,
            "pc": 8570,
            "pcc": 140,
            "pcp": 1.66,
            "Buy_CountI": 2416,
            "Buy_CountN": 20,
            "Sell_CountI": 2343,
            "Sell_CountN": 26,
            "Buy_I_Volume": 68461905,
            "Buy_N_Volume": 60864859,
            "Sell_I_Volume": 82617958,
            "Sell_N_Volume": 46708806,
        }
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_daily_history("test_key", "فملی", history_type=0)
        assert len(res) == 1

        rec = res[0]
        hist = DailyStockHistory.objects.create(
            symbol="فملی",
            date=rec["date"],
            time=rec["time"],
            tno=rec["tno"],
            tvol=rec["tvol"],
            tval=rec["tval"],
            pmin=rec["pmin"],
            pmax=rec["pmax"],
            py=rec["py"],
            pf=rec["pf"],
            pl=rec["pl"],
            plc=rec["plc"],
            plp=rec["plp"],
            pc=rec["pc"],
            pcc=rec["pcc"],
            pcp=rec["pcp"],
            buy_count_i=rec["Buy_CountI"],
            buy_count_n=rec["Buy_CountN"],
            sell_count_i=rec["Sell_CountI"],
            sell_count_n=rec["Sell_CountN"],
            buy_i_volume=rec["Buy_I_Volume"],
            buy_n_volume=rec["Buy_N_Volume"],
            sell_i_volume=rec["Sell_I_Volume"],
            sell_n_volume=rec["Sell_N_Volume"],
        )
        assert hist.symbol == "فملی"
        assert hist.buy_count_i == 2416


@pytest.mark.django_db
def test_fetch_candlesticks():
    mock_payload = {
        "l18": "فملی",
        "type": 3,
        "count": 1,
        "candle_daily_adjusted": [
            {"date": "1404-02-24", "open": 7380, "high": 7400, "low": 7280, "close": 7340, "volume": 180715348}
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_candlesticks("test_key", "فملی", candle_type=3)
        assert res["l18"] == "فملی"
        c_data = res["candle_daily_adjusted"][0]

        candle = MarketCandle.objects.create(
            symbol=res["l18"],
            timeframe="1d_adj",
            date_time=c_data["date"],
            open_price=c_data["open"],
            high_price=c_data["high"],
            low_price=c_data["low"],
            close_price=c_data["close"],
            volume=c_data["volume"],
        )
        assert candle.symbol == "فملی"
        assert candle.close_price == 7340


@pytest.mark.django_db
def test_fetch_transactions():
    mock_payload = [
        {"row": 1, "time": "09:01:02", "volume": 100000, "price": 26550, "canceled": 0}
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_transactions("test_key", "اهرم", date="1404-02-22")
        assert len(res) == 1
        t_data = res[0]

        tick = StockTransactionTick.objects.create(
            symbol="اهرم",
            date="1404-02-22",
            time=t_data["time"],
            row=t_data["row"],
            price=t_data["price"],
            volume=t_data["volume"],
            canceled=bool(t_data["canceled"]),
        )
        assert tick.row == 1
        assert tick.price == 26550


@pytest.mark.django_db
def test_fetch_shareholders():
    mock_payload = [
        {"id": 262011, "name": "بانک صادرات ایران", "volume": 27842346668, "percent": 5.16, "change": 0}
    ]
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_shareholders("test_key", "وبملت")
        assert len(res) == 1
        sh = res[0]

        rec = ShareholderRecord.objects.create(
            symbol="وبملت",
            shareholder_id=sh["id"],
            name=sh["name"],
            volume=sh["volume"],
            percent=sh["percent"],
            change=sh["change"],
        )
        assert rec.name == "بانک صادرات ایران"


@pytest.mark.django_db
def test_fetch_codal_announcements():
    mock_payload = {
        "count_announcement": 1,
        "count_page": 1,
        "announcement": [
            {
                "l18": "وبملت",
                "l30": "بانک ملت",
                "title": "صورت‌های مالی میاندوره‌ای",
                "code": "ن-۱۰",
                "date_title": "۱۴۰۳/۰۹/۳۰",
                "date_publish": "۱۴۰۳/۱۰/۳۰",
                "time_publish": "۱۷:۵۵:۴۱",
                "link": "https://codal.ir/Reports/Decision.aspx",
            }
        ]
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_codal_announcements("test_key", symbol="وبملت")
        assert res["count_announcement"] == 1
        item = res["announcement"][0]

        codal = CodalAnnouncement.objects.create(
            symbol=item["l18"],
            company_name=item["l30"],
            title=item["title"],
            code=item["code"],
            date_title=item["date_title"],
            date_publish=item["date_publish"],
            time_publish=item["time_publish"],
            link=item["link"],
        )
        assert codal.symbol == "وبملت"


@pytest.mark.django_db
def test_fetch_market_index():
    mock_payload = {
        "date": "1403-12-18",
        "time": "20:07:44",
        "state": "بسته",
        "index": 2756970.28,
        "index_change": -33000.22,
        "index_equalWeight": 814270.85,
        "index_equalWeight_change": -9859.51,
        "mv": 87748425774158880,
        "tno": 493959,
        "tval": 134757329520715,
        "tvol": 16326463426,
    }
    with patch("marketdata.fetchers.requests.get") as mock_get:
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = mock_payload

        res = fetch_market_index("test_key")
        assert res["index"] == 2756970.28

        idx = MarketIndexData.objects.create(
            date=res["date"],
            time=res["time"],
            state=res["state"],
            index_overall=res["index"],
            index_overall_change=res["index_change"],
            index_equal_weight=res["index_equalWeight"],
            index_equal_weight_change=res["index_equalWeight_change"],
            market_value=res["mv"],
            trade_number=res["tno"],
            trade_value=res["tval"],
            trade_volume=res["tvol"],
        )
        assert idx.index_overall == 2756970.28
