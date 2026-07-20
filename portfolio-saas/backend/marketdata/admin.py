"""Admin for the warehouse tables — browse/filter by symbol and date.

Dates here are source-native Jalali strings (CharFields), so no date_hierarchy;
plain ordering + search covers the browse cases.
"""
from django.contrib import admin

from .models import (
    CodalAnnouncement,
    DailyStockHistory,
    GoldCurrencyHistory,
    MarketCandle,
    MarketIndexData,
    ShareholderRecord,
    StockSymbolMetadata,
    StockTransactionTick,
)


@admin.register(StockSymbolMetadata)
class StockSymbolMetadataAdmin(admin.ModelAdmin):
    list_display = ("l18", "l30", "sector", "market", "pe", "eps", "market_cap", "updated_at")
    list_filter = ("market", "sector")
    search_fields = ("l18", "l30", "isin")


@admin.register(DailyStockHistory)
class DailyStockHistoryAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "pl", "pc", "tvol", "plp", "is_adjusted")
    list_filter = ("symbol", "is_adjusted")
    search_fields = ("symbol",)


@admin.register(MarketCandle)
class MarketCandleAdmin(admin.ModelAdmin):
    list_display = ("symbol", "timeframe", "date_time", "open_price", "close_price", "volume")
    list_filter = ("symbol", "timeframe")
    search_fields = ("symbol",)


@admin.register(StockTransactionTick)
class StockTransactionTickAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "time", "row", "price", "volume", "canceled")
    list_filter = ("symbol", "canceled")
    search_fields = ("symbol",)


@admin.register(ShareholderRecord)
class ShareholderRecordAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "name", "percent", "volume", "change")
    list_filter = ("symbol",)
    search_fields = ("symbol", "name")


@admin.register(CodalAnnouncement)
class CodalAnnouncementAdmin(admin.ModelAdmin):
    list_display = ("symbol", "title", "code", "date_publish", "time_publish")
    list_filter = ("symbol",)
    search_fields = ("symbol", "title")


@admin.register(GoldCurrencyHistory)
class GoldCurrencyHistoryAdmin(admin.ModelAdmin):
    list_display = ("symbol", "date", "close_price", "open_price", "unit")
    list_filter = ("symbol",)
    search_fields = ("symbol", "name")


@admin.register(MarketIndexData)
class MarketIndexDataAdmin(admin.ModelAdmin):
    list_display = ("date", "time", "index_overall", "index_overall_change", "trade_value")
    search_fields = ("date",)
