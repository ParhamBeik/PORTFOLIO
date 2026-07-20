"""Account-level services shared across the app.

`set_user_tier` is the single chokepoint for changing a subscription tier.
Billing activation (Zarinpal verify) and the DEBUG-only support command both
go through it, so the rules for what flips FREE<->PRO live in one place.
"""
from .models import User


def set_user_tier(user, tier, customer_id=None, expires_at=None):
    """Set a user's subscription tier (and optionally customer id / Pro expiry).

    Returns the refreshed user. `tier` must be a User.Tier value. `expires_at`
    stamps `pro_expires_at` (annual-prepay Pro); pass None to leave it untouched.
    """
    user.tier = tier
    update_fields = ["tier"]
    if customer_id is not None and customer_id != user.customer_id:
        user.customer_id = customer_id
        update_fields.append("customer_id")
    if expires_at is not None:
        user.pro_expires_at = expires_at
        update_fields.append("pro_expires_at")
    user.save(update_fields=update_fields)
    return user
