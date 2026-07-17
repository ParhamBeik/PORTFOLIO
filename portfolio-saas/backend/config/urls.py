"""Root URL configuration."""
from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("accounts.urls")),
    path("api/", include("portfolios.urls")),
    path("api/", include("pricing.urls")),
    path("api/billing/", include("billing.urls")),
    path("api/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
