"""Account-level services shared across the app.

`set_user_tier` is the single chokepoint for changing a subscription tier.
Billing activation (Zarinpal verify) and the DEBUG-only support command both
go through it, so the rules for what flips FREE<->PRO live in one place.
"""
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import User

EMAIL_VERIFICATION_SALT = "accounts.email-verification"


def make_email_verification_token(user: User) -> str:
    return signing.dumps(
        {"user_id": user.pk, "email": user.email},
        salt=EMAIL_VERIFICATION_SALT,
        compress=True,
    )


def verified_user_from_token(token: str) -> User:
    payload = signing.loads(
        token,
        salt=EMAIL_VERIFICATION_SALT,
        max_age=settings.EMAIL_VERIFICATION_TIMEOUT,
    )
    return User.objects.get(pk=payload["user_id"], email=payload["email"])


def send_verification_email(user: User) -> None:
    query = urlencode({"token": make_email_verification_token(user)})
    send_mail(
        "Verify your Lattice email",
        f"Verify your account: {settings.FRONTEND_VERIFICATION_URL}?{query}",
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


def make_password_reset_token(user: User) -> tuple[str, str]:
    return (
        urlsafe_base64_encode(force_bytes(user.pk)),
        default_token_generator.make_token(user),
    )


def send_password_reset_email(user: User) -> None:
    uid, token = make_password_reset_token(user)
    query = urlencode({"uid": uid, "token": token})
    send_mail(
        "Reset your Lattice password",
        f"Reset your password: {settings.FRONTEND_PASSWORD_RESET_URL}?{query}",
        settings.DEFAULT_FROM_EMAIL,
        [user.email],
    )


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
