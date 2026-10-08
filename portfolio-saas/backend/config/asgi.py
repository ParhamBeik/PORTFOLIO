"""ASGI config for portfolio-saas."""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()

# Import the URLconf -- and through it pandas, scipy and cvxpy -- while the
# worker boots, not inside the first request it serves. Django resolves it
# lazily, so every worker recycle (`--max-requests`) used to hand ~1.5 s of
# imports to whichever user's request arrived first.
from django.urls import get_resolver  # noqa: E402

get_resolver().url_patterns
