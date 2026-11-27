from django.apps import AppConfig
from django.core import checks


class AccountsConfig(AppConfig):
    name = "accounts"

    def ready(self):
        checks.register(mail_configured_check)


def mail_configured_check(app_configs=None, **kwargs):
    """Warn at deploy time (``migrate`` runs the checks) when production cannot send mail.

    Sign-up confirmation and password reset both depend on it, and an SMTP
    backend pointed at the Django default ``localhost`` fails only when a user
    asks for a link -- silently, from their side.
    """
    from django.conf import settings

    from config.mail import mail_is_deliverable

    if settings.DEBUG or mail_is_deliverable():
        return []
    return [
        checks.Warning(
            "Outbound mail is not configured: EMAIL_HOST is empty or a loopback host, "
            "so confirmation and password-reset emails cannot be sent.",
            hint="Set EMAIL_HOST, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD and DEFAULT_FROM_EMAIL "
            "in .env.production (Brevo: smtp-relay.brevo.com, port 587).",
            id="accounts.W001",
        )
    ]
