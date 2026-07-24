"""All /api/ routes for the portfolio app.

Grouped top-down the way a request would find them: catalog & CRUD, valuation,
prices (including the SSE stream), then the Pro analytics endpoints.
"""
from django.urls import path

from .live.sse import PriceStreamView
from .views import (
    AccountDetailView,
    AccountListCreateView,
    AccountValuationView,
    AdminCleanPricesExecuteView,
    AdminCleanPricesScanView,
    AnalyticsView,
    AssetListView,
    AssetReturnsView,
    FrontierView,
    HoldingDetailView,
    HoldingListCreateView,
    InsightsView,
    LatestPricesView,
    OptimizationView,
    PriceHistoryView,
    SnapshotListView,
    TradeView,
    TransactionListView,
    TransactionDestroyView,
    TransactionUndoView,
    ValuationView,
)

urlpatterns = [
    # Catalog + account/holding CRUD (FREE)
    path("assets/", AssetListView.as_view(), name="asset-list"),
    path("accounts/", AccountListCreateView.as_view(), name="account-list"),
    path("accounts/<int:pk>/", AccountDetailView.as_view(), name="account-detail"),
    path("accounts/<int:account_id>/holdings/", HoldingListCreateView.as_view(),
         name="holding-list"),
    path("accounts/<int:account_id>/holdings/<int:pk>/", HoldingDetailView.as_view(),
         name="holding-detail"),
    path("accounts/<int:account_id>/trades/", TradeView.as_view(), name="trade-create"),
    path("transactions/", TransactionListView.as_view(), name="transaction-list"),
    path("transactions/<int:pk>/", TransactionDestroyView.as_view(), name="transaction-detail"),
    path("transactions/<int:tx_id>/undo/", TransactionUndoView.as_view(), name="transaction-undo"),
    # Valuation + net-worth history (FREE)
    path("accounts/<int:account_id>/valuation/", AccountValuationView.as_view(),
         name="account-valuation"),
    path("valuation/", ValuationView.as_view(), name="valuation"),
    path("snapshots/", SnapshotListView.as_view(), name="snapshot-list"),
    # Live prices (FREE)
    path("prices/latest/", LatestPricesView.as_view(), name="prices-latest"),
    path("prices/stream/", PriceStreamView.as_view(), name="prices-stream"),
    path("prices/history/", PriceHistoryView.as_view(), name="prices-history"),
    # Admin DB Data Repair
    path("admin/clean-prices/scan/", AdminCleanPricesScanView.as_view(), name="admin-clean-prices-scan"),
    path("admin/clean-prices/execute/", AdminCleanPricesExecuteView.as_view(), name="admin-clean-prices-execute"),
    # Pro analytics
    path("insights/", InsightsView.as_view(), name="insights"),
    path("analytics/", AnalyticsView.as_view(), name="analytics"),
    path("optimization/", OptimizationView.as_view(), name="optimization"),
    path("optimization/frontier/", FrontierView.as_view(), name="optimization-frontier"),
    path("assets/returns/", AssetReturnsView.as_view(), name="assets-returns"),
]
