"""Map Stripe webhook events to tier changes via the shared set_user_tier service.

No Stripe SDK import here: the function takes a plain event dict, so it is trivial
to unit-test and a local gateway (Zarinpal/NextPay) can reuse this exact mapping
by passing its own event shape through `apply_subscription_event`.
"""
from django.contrib.auth import get_user_model

from accounts.services import set_user_tier

User = get_user_model()

PRO_EVENTS = {
    "checkout.session.completed",
    "invoice.paid",
    "invoice.payment_succeeded",
}
DOWNGRADE_EVENTS = {"customer.subscription.deleted"}


def _user_from_event(event_obj):
    """Resolve the user for a webhook object.

    By Stripe customer id first (set on the user after the first checkout); if
    the user does not carry it yet (first-ever checkout), fall back to the
    client_reference_id we passed when creating the Checkout Session.
    """
    customer_id = event_obj.get("customer")
    if customer_id:
        user = User.objects.filter(customer_id=customer_id).first()
        if user:
            return user, customer_id
    ref = event_obj.get("client_reference_id")
    if ref:
        user = User.objects.filter(id=ref).first()
        if user:
            return user, customer_id or ""
    return None, customer_id or ""


def apply_subscription_event(event) -> bool:
    """Apply the tier change implied by a Stripe event. Returns whether a user matched."""
    event_type = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    if event_type in PRO_EVENTS:
        user, customer_id = _user_from_event(obj)
        if user is None:
            return False
        set_user_tier(user, User.Tier.PRO, customer_id=customer_id)
        return True

    if event_type in DOWNGRADE_EVENTS:
        user, _ = _user_from_event(obj)
        if user is None:
            return False
        set_user_tier(user, User.Tier.FREE)
        return True

    return False
