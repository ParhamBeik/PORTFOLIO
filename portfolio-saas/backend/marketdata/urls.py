"""Read-only /api/market/ endpoints (filled in by the warehouse views)."""
from django.urls import path

from . import views

urlpatterns = [
    path("candles/", views.CandlesView.as_view(), name="market-candles"),
    path("history/", views.DailyHistoryView.as_view(), name="market-history"),
    path("ticks/", views.TicksView.as_view(), name="market-ticks"),
    path("index/", views.MarketIndexView.as_view(), name="market-index"),
    path("symbols/", views.SymbolListView.as_view(), name="market-symbols"),
    path("assets/", views.MarketAssetsView.as_view(), name="market-assets"),
    path("performance/", views.PerformanceView.as_view(), name="market-performance"),
    path("compare/", views.CompareView.as_view(), name="market-compare"),
    path("announcements/", views.AnnouncementsView.as_view(), name="market-announcements"),
    path("shareholders/", views.ShareholdersView.as_view(), name="market-shareholders"),
]
