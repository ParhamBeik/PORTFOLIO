"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

from accounts.serializers import PasswordAwareTokenRefreshSerializer
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
        TokenRefreshView.as_view(serializer_class=PasswordAwareTokenRefreshSerializer),
        name="token_refresh",
    ),
]
