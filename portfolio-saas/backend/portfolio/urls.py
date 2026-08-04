"""All /api/ routes for the portfolio app.

Grouped top-down the way a request would find them: catalog & CRUD, valuation,
prices (including the SSE stream), then the Pro analytics endpoints.
"""
import importlib
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
    AssetRankingView,
    FrontierView,
    HoldingDetailView,
    HoldingListCreateView,
    LiabilityDetailView,
    LiabilityListCreateView,
    LedgerListCreateView,
    LedgerReverseView,
    LedgerImportView,
    LedgerImportCommitView,
    AccountPerformanceView,
    AccountDataQualityView,
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
    DiscoveryView,
    PerformanceView,
    IntegrityView,
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
    path("accounts/<int:account_id>/liabilities/", LiabilityListCreateView.as_view(),
         name="liability-list"),
    path("accounts/<int:account_id>/liabilities/<int:pk>/", LiabilityDetailView.as_view(),
         name="liability-detail"),
    path("accounts/<int:account_id>/ledger/", LedgerListCreateView.as_view(),
         name="ledger-list"),
    path("accounts/<int:account_id>/ledger/<int:entry_id>/reverse/",
         LedgerReverseView.as_view(), name="ledger-reverse"),
    path("accounts/<int:account_id>/imports/preview/",
         LedgerImportView.as_view(), name="ledger-import-preview"),
    path("accounts/<int:account_id>/imports/commit/",
         LedgerImportCommitView.as_view(), name="ledger-import-commit"),
    path("accounts/<int:account_id>/performance/",
         AccountPerformanceView.as_view(), name="account-performance"),
    path("accounts/<int:account_id>/data-quality/",
         AccountDataQualityView.as_view(), name="account-data-quality"),
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
    path("analytics/asset-ranking/", AssetRankingView.as_view(), name="asset-ranking"),
    path("optimization/", OptimizationView.as_view(), name="optimization"),
    path("optimization/frontier/", FrontierView.as_view(), name="optimization-frontier"),
    path("assets/returns/", AssetReturnsView.as_view(), name="assets-returns"),
    path("discovery/", DiscoveryView.as_view(), name="discovery"),
    path("performance/", PerformanceView.as_view(), name="performance"),
    path("integrity/", IntegrityView.as_view(), name="integrity"),

    # Webhook for brsapi to notify of new prices (triggers optimization run)
    path("marketdata/webhook/brsapi/", importlib.import_module(".views", package=__package__).BrsApiWebhookView.as_view(), name="brs-webhook"),

    # Optimization snapshots API (MVP)
    path("optimization/snapshots/", importlib.import_module(".views", package=__package__).OptimizationSnapshotListView.as_view(), name="optimization-snapshots"),
    path("optimization/snapshots/latest/", importlib.import_module(".views", package=__package__).OptimizationSnapshotLatestView.as_view(), name="optimization-snapshots-latest"),
]
