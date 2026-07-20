"""Activate Pro on a verified Zarinpal payment.

The Zarinpal `verify` call (code 100 paid / 101 already-verified) is the only
trigger that flips a user to PRO — there is no recurring billing event to
listen for. `Payment.authority` is unique and `activate_pro` no-ops on an
already-verified row, so a callback replay or a page refresh cannot double-fire
or extend the expiry twice.
"""
from datetime import timedelta

from django.utils import timezone

from accounts.models import User
from accounts.services import set_user_tier

# Annual prepay (Zarinpal has no native recurring billing; this is the Iranian
# SaaS norm). Expiry is stamped from the verify moment, not from any prior expiry.
PRO_DURATION = timedelta(days=365)


def activate_pro(payment) -> bool:
    """Flip a Payment's user to PRO for one year. Idempotent.

    Returns True if this call activated, False if the payment was already
    verified (replay/refresh). Safe to call on an already-verified payment.
    """
    if payment.status == payment.Status.VERIFIED:
        return False
    expires_at = timezone.now() + PRO_DURATION
    set_user_tier(payment.user, User.Tier.PRO, expires_at=expires_at)
    payment.status = payment.Status.VERIFIED
    payment.verified_at = timezone.now()
    payment.save(update_fields=["status", "verified_at"])
    return True
