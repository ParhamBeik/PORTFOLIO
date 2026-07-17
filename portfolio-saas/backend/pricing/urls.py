from django.urls import path

from .sse import PriceStreamView
from .views import InsightsView, LatestPricesView, PriceHistoryView

urlpatterns = [
    path("prices/latest/", LatestPricesView.as_view(), name="prices-latest"),
    path("prices/stream/", PriceStreamView.as_view(), name="prices-stream"),
    path("prices/history/", PriceHistoryView.as_view(), name="prices-history"),
    path("insights/", InsightsView.as_view(), name="insights"),
]
