"""Test settings: local Postgres, no Docker or Redis required.

Inherits production config, then points the database at a local Postgres the test
runner creates/drops on each run and swaps Redis for an in-process cache. MD5
password hashing keeps user-heavy tests fast. This lets the suite run anywhere
with a local postgres, which matters because `get_latest_prices` relies on the
Postgres-only DISTINCT ON clause and cannot be exercised under sqlite.
"""
import os

os.environ["DJANGO_DEBUG"] = "1"
os.environ["ENVIRONMENT"] = "dev"

from .settings import *  # noqa: F401,F403

ENVIRONMENT = "test"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        # `postgres` always exists; the runner connects here to issue CREATE
        # DATABASE for the real test database named via TEST.NAME below.
        "NAME": "postgres",
        "USER": os.getenv("TEST_PG_USER", "parham"),
        "PASSWORD": os.getenv("TEST_PG_PASSWORD", ""),
        "HOST": os.getenv("TEST_PG_HOST", "127.0.0.1"),
        "PORT": os.getenv("TEST_PG_PORT", "5432"),
        "TEST": {"NAME": os.getenv("TEST_DB_NAME", "portfolio_test")},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "pytest",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

DEBUG = False

# The Django test client speaks plain HTTP. Inheriting the production
# SECURE_SSL_REDIRECT turns every request into a 301 before it reaches a view,
# so the suite would assert against redirects instead of behaviour. Production
# posture is unchanged: CI runs `manage.py check --deploy` against
# config.settings, where this stays True.
SECURE_SSL_REDIRECT = False

# No Redis under test, so the shared 5-minute window limiter is unavailable.
# Production refuses archive requests in that case (a per-process window would
# multiply the provider's allowance by the worker count); the suite is one
# process, so let it fall back to the local window and exercise the counters.
MARKETDATA_REQUIRE_SHARED_WINDOW = False
# Production keeps a 150-request gap so archive cannot empty a wallet. The
# suite uses 5–20 request ceilings to pin the reserve arithmetic; that gap
# would zero those wallets and hide the behaviour under test.
MARKETDATA_PLAN_SAFETY_MARGIN = 0

# Run Celery tasks synchronously in-process. This removes the hard dependency on
# a running Redis broker during tests — views that call `.delay()` (e.g.
# RetryArchiveJobView) execute the task inline instead of failing with
# ConnectionRefusedError after 20 retries.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
MARKETDATA_IGNORE_MARKET_HOURS = True
