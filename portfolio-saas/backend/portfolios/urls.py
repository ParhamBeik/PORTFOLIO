from django.urls import path

from .views import (
    AccountDetailView,
    AccountListCreateView,
    AccountValuationView,
    AssetListView,
    HoldingDetailView,
    HoldingListCreateView,
    ValuationView,
)

urlpatterns = [
    path("assets/", AssetListView.as_view(), name="asset-list"),
    path("accounts/", AccountListCreateView.as_view(), name="account-list"),
    path("accounts/<int:pk>/", AccountDetailView.as_view(), name="account-detail"),
    path("accounts/<int:account_id>/holdings/", HoldingListCreateView.as_view(),
         name="holding-list"),
    path("accounts/<int:account_id>/holdings/<int:pk>/", HoldingDetailView.as_view(),
         name="holding-detail"),
    path("accounts/<int:account_id>/valuation/", AccountValuationView.as_view(),
         name="account-valuation"),
    path("valuation/", ValuationView.as_view(), name="valuation"),
]
