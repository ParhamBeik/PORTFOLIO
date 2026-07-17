"""Account-level services shared across the app.

`set_user_tier` is the single chokepoint for changing a subscription tier.
Billing webhooks (Stripe, or a future local gateway) and the DEBUG-only support
command both go through it, so the rules for what flips FREE<->PRO live in one
place.
"""
from .models import User


def set_user_tier(user, tier, customer_id=None):
    """Set a user's subscription tier (and optionally their billing customer id).

    Returns the refreshed user. `tier` must be a User.Tier value.
    """
    user.tier = tier
    update_fields = ["tier"]
    if customer_id is not None and customer_id != user.customer_id:
        user.customer_id = customer_id
        update_fields.append("customer_id")
    user.save(update_fields=update_fields)
    return user
