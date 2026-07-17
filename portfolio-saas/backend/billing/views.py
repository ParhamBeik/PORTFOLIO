"""Stripe Checkout creation + signature-verified webhook.

Raw Stripe (not dj-stripe): two endpoints, mapped to the existing User.customer_id
/ User.tier. The tier flip is delegated to `accounts.services.set_user_tier`, so a
local Iranian gateway can mirror this without touching the User model.
"""
import stripe
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import WebhookEvent
from .services import apply_subscription_event

stripe.api_key = settings.STRIPE_SECRET_KEY


class CheckoutView(APIView):
    """Create a Stripe Checkout Session for the Pro subscription.

    Returns the hosted Checkout URL for the frontend to redirect to.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": settings.STRIPE_PRO_PRICE_ID}],
            client_reference_id=str(request.user.id),
            customer_email=request.user.email,
            success_url=settings.STRIPE_SUCCESS_URL,
            cancel_url=settings.STRIPE_CANCEL_URL,
        )
        return Response({"url": session.url})


@csrf_exempt
@require_POST
def webhook(request):
    """Stripe webhook: signature-verified, idempotent on the Stripe event id."""
    sig = request.META.get("HTTP_STRIPE_SIGNATURE", "")
    try:
        event = stripe.Webhook.construct_event(
            request.body, sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return HttpResponse(status=400)

    event_id = event.get("id", "")
    event_type = event.get("type", "")
    try:
        with transaction.atomic():
            # Inserting the event id first makes the whole change idempotent: a
            # Stripe retry hits the unique constraint, the transaction rolls back,
            # and we simply acknowledge.
            WebhookEvent.objects.create(event_id=event_id, type=event_type)
            apply_subscription_event(event)
    except IntegrityError:
        return HttpResponse(status=200)
    return HttpResponse(status=200)
