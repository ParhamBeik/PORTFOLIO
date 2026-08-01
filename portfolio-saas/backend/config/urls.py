"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from accounts.views import CookieTokenRefreshView
from .health import HealthView, PriceFeedView, ReadyView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/health/ready/", ReadyView.as_view(), name="health-ready"),
    path("api/health/prices/", PriceFeedView.as_view(), name="health-prices"),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("portfolio.urls")),
    path("api/market/", include("marketdata.urls")),
    path("api/billing/", include("billing.urls")),
    path(
        "api/token/refresh/",
        CookieTokenRefreshView.as_view(),
        name="token_refresh",
    ),
]
