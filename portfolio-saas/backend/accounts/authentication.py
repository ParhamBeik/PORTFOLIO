"""JWT sign-in that tells Sentry WHICH member hit an error, by id only."""
from django.conf import settings
from rest_framework_simplejwt.authentication import JWTAuthentication


class SentryUserJWTAuthentication(JWTAuthentication):
    """`send_default_pii=False` stays on, so no email, IP or cookie is sent.

    Set here rather than in middleware because DRF authenticates inside the
    view: a middleware only ever sees an anonymous user on a JWT request.
    """

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None and settings.SENTRY_DSN:
            import sentry_sdk

            sentry_sdk.set_user({"id": str(result[0].pk)})
        return result
