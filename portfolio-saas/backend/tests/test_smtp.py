from unittest.mock import ANY, patch

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend


def test_smtp_connection_receives_finite_timeout():
    with patch("django.core.mail.backends.smtp.smtplib.SMTP") as smtp:
        with EmailBackend(use_tls=False):
            smtp.assert_called_once_with(
                settings.EMAIL_HOST, settings.EMAIL_PORT,
                local_hostname=ANY, timeout=10,
            )
