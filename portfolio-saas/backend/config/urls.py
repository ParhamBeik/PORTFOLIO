"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from accounts.views import CookieTokenRefreshView
from portfolio.views import AdminCleanPricesExecuteView, AdminCleanPricesScanView
from .health import HealthView, PriceFeedView, ReadyView

# Monkeypatch django admin index page to inject operational telemetry
from django.contrib import admin
from marketdata.admin_telemetry import get_admin_telemetry_context

original_admin_index = admin.site.index

def custom_admin_index(request, extra_context=None):
    if extra_context is None:
        extra_context = {}
    try:
        extra_context.update(get_admin_telemetry_context())
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Failed to fetch admin telemetry context: %s", e)
    return original_admin_index(request, extra_context=extra_context)

admin.site.index = custom_admin_index

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/health/ready/", ReadyView.as_view(), name="health-ready"),
    path("api/health/prices/", PriceFeedView.as_view(), name="health-prices"),
    path("api/auth/", include("accounts.urls")),
    path("api/admin/clean-prices/scan/", AdminCleanPricesScanView.as_view(), name="admin-clean-prices-scan"),
    path("api/admin/clean-prices/execute/", AdminCleanPricesExecuteView.as_view(), name="admin-clean-prices-execute"),
    path("api/admin/", include("marketdata.admin_api")),
    path("api/", include("portfolio.urls")),
    path(
        "api/token/refresh/",
        CookieTokenRefreshView.as_view(),
        name="token_refresh",
    ),
]
