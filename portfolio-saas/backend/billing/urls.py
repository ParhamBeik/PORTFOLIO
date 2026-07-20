from django.urls import path

from .views import ZarinpalRequestView, zarinpal_callback

urlpatterns = [
    path("zarinpal/request/", ZarinpalRequestView.as_view(), name="billing-zarinpal-request"),
    path("zarinpal/callback/", zarinpal_callback, name="billing-zarinpal-callback"),
]
