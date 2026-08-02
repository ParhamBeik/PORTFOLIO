import logging
from datetime import timedelta

import requests
from celery import shared_task
from django.utils import timezone

from config.alerts import notify

from .models import Payment
from .services import activate_pro, repair_verified_payment
from .zarinpal import verify_payment


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def reconcile_pending_payments():
    cutoff = timezone.now() - timedelta(minutes=15)
    result = {"verified": 0, "failed": 0, "pending": 0, "repaired": 0}
    for payment in Payment.objects.filter(
        status=Payment.Status.PENDING, created_at__lte=cutoff
    ).order_by("id"):
        try:
            ok, ref_id, _code = verify_payment(
                authority=payment.authority, amount_rial=payment.amount_rial
            )
        except requests.RequestException:
            result["pending"] += 1
            continue
        except Exception:
            logger.exception("Payment reconciliation failed for %s", payment.pk)
            result["pending"] += 1
            continue
        if ok:
            activate_pro(payment.authority, ref_id)
            result["verified"] += 1
        else:
            Payment.objects.filter(
                pk=payment.pk, status=Payment.Status.PENDING
            ).update(status=Payment.Status.FAILED)
            result["failed"] += 1

    for payment in Payment.objects.filter(
        status=Payment.Status.VERIFIED, user__isnull=False
    ).select_related("user"):
        if not payment.user.is_pro():
            repair_verified_payment(payment)
            result["repaired"] += 1
            notify(
                "paid-but-not-activated",
                {"payment_id": payment.pk, "user_id": payment.user_id},
                dedupe_seconds=3600,
            )

    aged = Payment.objects.filter(
        status=Payment.Status.PENDING,
        created_at__lte=timezone.now() - timedelta(hours=24),
    ).count()
    if aged:
        notify(
            "payment-pending-24h",
            {"count": aged},
            dedupe_seconds=3600,
        )
    return result
