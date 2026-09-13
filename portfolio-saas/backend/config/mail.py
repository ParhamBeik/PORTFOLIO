"""Whether outbound mail is configured well enough to be worth attempting.

This answers the *configuration* half of "can this process send mail" and
nothing more. It exists because two very different callers need the same rule
and were drifting apart:

- `marketdata.admin_telemetry.outbound_mail()` reports mail health on the Ops
  console. It asks this first and then **opens a socket**, because a relay that
  is named but refusing connections is exactly the failure that a settings-only
  check calls healthy.
- `accounts.views` decides whether to offer self-service password reset at all.
  A public, unauthenticated endpoint must not dial a relay to render a link, so
  configuration is the only question it may ask.

So: stricter checks belong on top of this, never inside it.
"""
from django.conf import settings

# Django's own global default for EMAIL_HOST is "localhost", so a loopback host
# is what an environment that never set the variable looks like -- and a mail
# relay running inside the application container is not a deployment anyone
# here intends. Treat it as unset rather than as configured.
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def mail_is_deliverable() -> bool:
    """True when a `send_mail` call has some chance of leaving this process.

    A non-SMTP backend (console, locmem, filebased) counts as deliverable: mail
    is captured locally, which is correct in dev and in the test suite, and a
    developer reading a reset link out of the console is a working flow.
    """
    backend = str(getattr(settings, "EMAIL_BACKEND", ""))
    if "smtp" not in backend:
        return True
    host = str(getattr(settings, "EMAIL_HOST", ""))
    return bool(host) and host not in LOOPBACK_HOSTS
