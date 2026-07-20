"""Zarinpal payment flow: request an authority, then verify on callback.

Two endpoints replace the Stripe Checkout + webhook pair. Unlike Stripe's
server-to-server webhook, Zarinpal redirects the *user's browser* back to our
callback URL with `Authority` + `Status` query params; we verify server-side
(never trust the browser), activate Pro idempotently, then redirect to the
frontend with a status flag so the UI can show success/cancel.
"""
from django.conf import settings
from django.shortcuts import redirect
from django.views.decorators.csrf import csrf_exempt
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Payment
from .services import activate_pro
from .zarinpal import ZarinpalError, request_payment, start_pay_url, verify_payment


def _amount_rial() -> int:
    """Pro price in Rial. Our config is in Toman; Zarinpal takes Rial (×10)."""
    return int(settings.PRO_PRICE_TOMAN) * 10


class ZarinpalRequestView(APIView):
    """Create a pending Payment and return the hosted payment redirect URL.

    The authority is obtained from Zarinpal *before* the Payment row is written,
    so the row's unique `authority` is real from the moment it exists (no
    placeholder, no unique-constraint race). On gateway refusal we return 502
    without creating a row.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount_rial = _amount_rial()
        try:
            authority = request_payment(
                amount_rial=amount_rial,
                description=f"Pro subscription - {request.user.email}",
                callback_url=settings.ZARINPAL_CALLBACK_URL,
            )
        except (ZarinpalError, Exception):
            # Gateway refused or unreachable. Surface a generic message; the
            # detail is not safe/ useful to echo to the client.
            return Response(
                {"detail": "Payment gateway could not start the transaction."},
                status=502,
            )
        Payment.objects.create(
            user=request.user,
            authority=authority,
            amount_rial=amount_rial,
            status=Payment.Status.PENDING,
        )
        return Response({"redirect_url": start_pay_url(authority)})


@csrf_exempt
def zarinpal_callback(request):
    """Browser redirect from Zarinpal after the user pays (or cancels).

    Verifies server-side, activates Pro (idempotent), then redirects to the
    frontend billing page with a `status` query flag. CSRF-exempt because
    Zarinpal GET-redirects here with no CSRF token.
    """
    authority = request.GET.get("Authority", "")
    status = request.GET.get("Status", "")
    front = settings.ZARINPAL_FRONTEND_URL

    if status != "OK":
        # User closed the page or cancelled on Zarinpal. No Payment state change
        # (the row stays PENDING — harmless, and useful for reconciliation).
        return redirect(f"{front}?status=cancel")

    payment = Payment.objects.filter(authority=authority).first()
    if payment is None:
        return redirect(f"{front}?status=error")

    if payment.status != Payment.Status.VERIFIED:
        try:
            ok, ref_id, _code = verify_payment(
                authority=authority, amount_rial=payment.amount_rial
            )
        except Exception:
            return redirect(f"{front}?status=error")
        if not ok:
            payment.status = Payment.Status.FAILED
            payment.save(update_fields=["status"])
            return redirect(f"{front}?status=cancel")
        payment.ref_id = ref_id
        payment.save(update_fields=["ref_id"])
        activate_pro(payment)  # idempotent: no-ops if already verified

    # Just activated, or a replay/refresh of an already-verified payment.
    ref = payment.ref_id or ""
    suffix = f"?status=success&ref_id={ref}" if ref else "?status=success"
    return redirect(f"{front}{suffix}")
