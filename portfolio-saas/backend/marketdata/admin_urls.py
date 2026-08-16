"""Staff-only /api/admin/* operations endpoints."""
from django.urls import path

from . import admin_api

urlpatterns = [
    path("overview/", admin_api.AdminOverviewView.as_view(), name="admin-ops-overview"),
    path("workflows/", admin_api.AdminWorkflowListView.as_view(), name="admin-ops-workflows"),
    path("logs/", admin_api.AdminLogListView.as_view(), name="admin-ops-logs"),
    path("archive-states/", admin_api.AdminArchiveStateListView.as_view(), name="admin-ops-archive-states"),
    path("archive-states/retry/", admin_api.AdminArchiveRetryView.as_view(), name="admin-ops-archive-retry"),
    path("assets/", admin_api.AdminAssetListView.as_view(), name="admin-ops-assets"),
    path("assets/<str:key>/evidence/", admin_api.AdminAssetEvidenceView.as_view(), name="admin-ops-asset-evidence"),
    path("assets/<str:key>/retry/", admin_api.AdminAssetRetryView.as_view(), name="admin-ops-asset-retry"),
    path("assets/<str:key>/recompute-integrity/", admin_api.AdminAssetRecomputeIntegrityView.as_view(), name="admin-ops-asset-recompute"),
    path("assets/<str:key>/refresh/", admin_api.AdminAssetRefreshView.as_view(), name="admin-ops-asset-refresh"),
]
