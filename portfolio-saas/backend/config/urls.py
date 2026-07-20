"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from .health import HealthView, ReadyView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/health/ready/", ReadyView.as_view(), name="health-ready"),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("portfolio.urls")),
    path("api/market/", include("marketdata.urls")),
    path("api/billing/", include("billing.urls")),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
