from django.apps import AppConfig
from django.core import checks


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        checks.register(mail_configured_check)


def mail_configured_check(app_configs=None, **kwargs):
    """Say at deploy time (``migrate`` runs the checks) when production cannot send mail.

    Sign-up confirmation and password reset both depend on it, and an SMTP
    backend pointed at the Django default ``localhost`` fails only when a user
    asks for a link -- silently, from their side.

    It is an Info, not a Warning: ``deploy.sh`` runs ``check --deploy
    --fail-level WARNING``, and a missing mail relay is a known, safe state
    (``signup_open()`` keeps sign-up closed until mail works). As a Warning it
    failed and rolled back the 2026-11-27 release on a box with no relay yet.
    """
    from django.conf import settings

    from config.mail import mail_is_deliverable

    if settings.DEBUG or mail_is_deliverable():
        return []
    return [
        checks.Info(
            "Outbound mail is not configured: EMAIL_HOST is empty or a loopback host, "
            "so confirmation and password-reset emails cannot be sent.",
            hint="Set EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD and DEFAULT_FROM_EMAIL "
            "in .env.production (Brevo: smtp-relay.brevo.com, port 587).",
            id="accounts.I001",
        )
    ]
