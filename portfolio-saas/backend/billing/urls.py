from django.urls import path

from .views import PaymentHistoryView, ZarinpalRequestView, zarinpal_callback

urlpatterns = [
    path("payments/", PaymentHistoryView.as_view(), name="billing-payments"),
    path("zarinpal/request/", ZarinpalRequestView.as_view(), name="billing-zarinpal-request"),
    path("zarinpal/callback/", zarinpal_callback, name="billing-zarinpal-callback"),
]
