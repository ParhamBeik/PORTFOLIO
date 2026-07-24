"""Activate Pro on a verified Zarinpal payment.

The Zarinpal `verify` call (code 100 paid / 101 already-verified) is the only
trigger that flips a user to PRO — there is no recurring billing event to
listen for. `Payment.authority` is unique and `activate_pro` no-ops on an
already-verified row, so a callback replay or a page refresh cannot double-fire
or extend the expiry twice.
"""
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from accounts.services import set_user_tier
from .models import Payment

# Annual prepay (Zarinpal has no native recurring billing; this is the Iranian
# SaaS norm). Expiry is stamped from the verify moment, not from any prior expiry.
PRO_DURATION = timedelta(days=365)


@transaction.atomic
def activate_pro(authority: str, ref_id: str) -> Payment:
    """Atomically verify one payment and extend Pro from the later of now/expiry.

    Returns the locked Payment. Replays return the existing verified state.
    """
    payment = (
        Payment.objects.select_for_update()
        .select_related("user")
        .get(authority=authority)
    )
    if payment.status == payment.Status.VERIFIED:
        return payment
    user = User.objects.select_for_update().get(pk=payment.user_id)
    now = timezone.now()
    expires_at = max(now, user.pro_expires_at or now) + PRO_DURATION
    set_user_tier(user, User.Tier.PRO, expires_at=expires_at)
    payment.status = payment.Status.VERIFIED
    payment.ref_id = ref_id
    payment.verified_at = now
    payment.save(update_fields=["status", "ref_id", "verified_at"])
    return payment
