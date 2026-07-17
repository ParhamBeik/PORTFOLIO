from django.urls import path

from .views import CheckoutView, webhook

urlpatterns = [
    path("checkout/", CheckoutView.as_view(), name="billing-checkout"),
    path("webhook/", webhook, name="billing-webhook"),
]
