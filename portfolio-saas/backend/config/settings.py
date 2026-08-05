"""Django settings for portfolio-saas.

Configuration is environment-driven so the same image runs in dev, CI, and prod.
Prices are fetched centrally and shared across every user (see pricing/), which
is the key reason this design scales: one fetch updates everyone's valuation.
"""
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me-must-be-at-least-32-bytes-long-for-jwt-hs256!")
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
# ENVIRONMENT is a second, independent signal (not derived from DEBUG) so security
# hardening below doesn't vanish just because someone flips DEBUG=1 for a one-off
# debugging session in a prod-like environment.
ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
# H7: refuse to boot a non-debug server on a known-weak key. Checks a denylist of
# known-bad values/prefixes (not just the exact committed default) plus a minimum
# length, so a deploy can't trivially pass this guard with a different-but-weak key.
_WEAK_SECRET_KEY_PREFIXES = ("dev-insecure-change-me", "changeme", "insecure", "django-insecure-")
if not DEBUG:
    if SECRET_KEY.lower().startswith(_WEAK_SECRET_KEY_PREFIXES):
        raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")
    if len(SECRET_KEY) < 50:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be at least 50 characters when DJANGO_DEBUG=0.")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,backend").split(",")

# Frontend origin(s) for CORS. Comma-separated in dev (Vite on 5173).
CORS_ALLOWED_ORIGINS = [
    o for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o
]
CSRF_TRUSTED_ORIGINS = CORS_ALLOWED_ORIGINS

INSTALLED_APPS = [
    "marketdata",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "corsheaders",
    "accounts",
    "portfolio",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "config.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.debug",
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
        ]},
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "portfolio"),
        "USER": os.getenv("POSTGRES_USER", "portfolio"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", "portfolio"),
        "HOST": os.getenv("POSTGRES_HOST", "db"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
    }
}

# A single connection string wins when present. Used by the GitHub Actions
# fetcher, which runs `manage.py fetch_prices` against the prod DB from a
# runner without docker-compose's service hostnames.
_database_url = os.getenv("DATABASE_URL")
if _database_url:
    from urllib.parse import urlparse

    _u = urlparse(_database_url)
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _u.path.lstrip("/") or "portfolio",
        "USER": _u.username or "portfolio",
        "PASSWORD": _u.password or "",
        "HOST": _u.hostname or "db",
        "PORT": str(_u.port or 5432),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "sslmode": os.getenv("PG_SSLMODE", "prefer"),
    }

# Redis caches the global price map and per-user valuations so reads stay cheap
# under load. Falls back to local-memory if REDIS_URL is unset (e.g. quick tests).
if os.getenv("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
            "OPTIONS": {"CONNECTION_CLASS_KWARGS": {"ssl_cert_reqs": None}},
        }
    }
elif not DEBUG:
    # LocMemCache is per-process: with multiple gunicorn workers each worker gets
    # its own cache, silently breaking shared-cache correctness (stale prices,
    # inconsistent valuations). Fail loudly in prod instead of degrading quietly.
    raise ImproperlyConfigured("Set REDIS_URL when DJANGO_DEBUG=0 (LocMemCache is unsafe across gunicorn workers).")
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_USER_MODEL = "accounts.User"

EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_FILE_PATH = os.getenv("EMAIL_FILE_PATH", str(BASE_DIR / "test-emails"))
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "25"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "0") == "1"
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Lattice <no-reply@localhost>")
FRONTEND_VERIFICATION_URL = os.getenv(
    "FRONTEND_VERIFICATION_URL", "http://localhost:5173/verify-email"
)
FRONTEND_PASSWORD_RESET_URL = os.getenv(
    "FRONTEND_PASSWORD_RESET_URL", "http://localhost:5173/reset-password"
)
EMAIL_VERIFICATION_TIMEOUT = 24 * 60 * 60
PASSWORD_RESET_TIMEOUT = 60 * 60

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "config.authentication.PublicDemoUserAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    # H5: throttle anonymous endpoints (login/register) by IP. Authenticated
    # users get their own per-user bucket so no single account can hammer the
    # API; 120/min comfortably covers dashboard usage plus the 2-min price poll.
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": os.getenv("ANON_THROTTLE", "30/min"),
        "user": os.getenv("USER_THROTTLE", "120/min"),
    },
    # M5: render Decimal as a string so large Toman values stay exact on the wire.
    "DEFAULT_RENDERER_CLASSES": ("config.renderers.DecimalStringJSONRenderer",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    # Rotate refresh tokens on use and blacklist the old one, so a stolen refresh
    # token has a single-use window instead of being valid for the full 7 days.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "CHECK_REVOKE_TOKEN": True,
}
JWT_COOKIE_SECURE = not DEBUG
# token_blacklist (INSTALLED_APPS above) is now available for a "log out all
# devices" endpoint (blacklist a user's OutstandingToken set) — add it in
# accounts/views.py, not here.

# Market data sources. The BRS/TSETMC keys are the same shape as the original
# PORTFOLIO project's settings.json, now 12-factor env vars.
BRS_API_KEY = os.getenv("BRS_API_KEY", "")
BRS_URL = os.getenv(
    "BRS_URL", "https://Api.BrsApi.ir/Market/Gold_Currency.php"
)
TSETMC_API_KEY = os.getenv("TSETMC_API_KEY", "")
TSETMC_URL = os.getenv("TSETMC_URL", "https://Api.BrsApi.ir/Tsetmc/AllSymbols.php")
TSETMC_SYMBOL_URL = os.getenv(
    "TSETMC_SYMBOL_URL", "https://Api.BrsApi.ir/Tsetmc/Symbol.php"
)
# Per-tier portfolio ceilings, read by accounts.features.limit_for(). Unset
# means "use the registry default" (Free 3, Pro unlimited); PRO_PORTFOLIO_LIMIT
# exists so a deployment can cap Pro without a code change.
FREE_PORTFOLIO_LIMIT = os.getenv("FREE_PORTFOLIO_LIMIT")
PRO_PORTFOLIO_LIMIT = os.getenv("PRO_PORTFOLIO_LIMIT")

# Seconds to sleep between BrsApi calls inside one sync task (paid API courtesy).
MARKETDATA_FETCH_DELAY = float(os.getenv("MARKETDATA_FETCH_DELAY", "0.05"))
MARKETDATA_DAILY_REQUEST_LIMIT = int(os.getenv("MARKETDATA_DAILY_REQUEST_LIMIT", "9800"))
MARKETDATA_WINDOW_LIMIT = int(os.getenv("MARKETDATA_WINDOW_LIMIT", "1000"))
MARKETDATA_WINDOW_SECONDS = int(os.getenv("MARKETDATA_WINDOW_SECONDS", "300"))

# Per-bucket daily budget. LIVE gets a floor, not a leftover: live is the
# customer-facing path, so it is reserved first and archive takes the remainder.
# Replaces the legacy MARKETDATA_ARCHIVE_REQUEST_RESERVE=8820, which reserved for
# archive and left live to fight for what was left -- backwards.
MARKETDATA_LIVE_REQUEST_FLOOR = int(os.getenv("MARKETDATA_LIVE_REQUEST_FLOOR", "4320"))
MARKETDATA_LIVE_REQUEST_HEADROOM = int(os.getenv("MARKETDATA_LIVE_REQUEST_HEADROOM", "500"))
MARKETDATA_ARCHIVE_REQUEST_BUDGET = int(os.getenv("MARKETDATA_ARCHIVE_REQUEST_BUDGET", "4980"))
MARKETDATA_OTHER_REQUEST_BUDGET = int(os.getenv("MARKETDATA_OTHER_REQUEST_BUDGET", "200"))
# Kept as a backwards-compatible alias so older management commands and tests that
# still read it keep working; the archive budget above is the authoritative value.
MARKETDATA_ARCHIVE_REQUEST_RESERVE = MARKETDATA_ARCHIVE_REQUEST_BUDGET
MARKETDATA_ARCHIVE_BATCH_SIZE = int(os.getenv("MARKETDATA_ARCHIVE_BATCH_SIZE", "120"))

# Per-day HISTORICAL_PER_DAY endpoints (ticks) walk one calendar day per request,
# so the trailing window is bounded to keep cost finite. Trading days only -- a
# non-trading day simply has no daily candle, so it is never requested.
MARKETDATA_TICK_WINDOW_DAYS = int(os.getenv("MARKETDATA_TICK_WINDOW_DAYS", "90"))

# Codal announcements are paged 20 per request and a mature symbol has ~50 pages,
# so "all history for all symbols" is ~32,000 requests -- more than three days of
# the whole archive budget. Only page 1 was ever fetched, which stored 2% and
# still reported verified. Bound the target to the newest N pages per symbol so
# the state can honestly converge; raise it when the backlog is otherwise idle.
MARKETDATA_CODAL_MAX_PAGES = int(os.getenv("MARKETDATA_CODAL_MAX_PAGES", "5"))

# Live poll cadence by market state (seconds). Beat still ticks every minute; the
# task itself decides whether enough time has passed, so the cadence can change
# without a beat restart. See marketdata/market_state.py for the arithmetic.
# A flat 2 minutes across all three states: 720 cycles a day, every hour covered.
MARKETDATA_LIVE_INTERVAL_OPEN = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OPEN", "120"))
MARKETDATA_LIVE_INTERVAL_DAYTIME = int(os.getenv("MARKETDATA_LIVE_INTERVAL_DAYTIME", "120"))
MARKETDATA_LIVE_INTERVAL_OVERNIGHT = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OVERNIGHT", "120"))

# Provider calls one live cycle makes: BRS Gold, BRS Crypto, BRS Commodity,
# TSETMC AllSymbols, TSETMC Options, TSETMC ETF NAV.
MARKETDATA_LIVE_REQUESTS_PER_CYCLE = int(
    os.getenv("MARKETDATA_LIVE_REQUESTS_PER_CYCLE", "6")
)

MARKETDATA_QUOTA_TIMEZONE = os.getenv("MARKETDATA_QUOTA_TIMEZONE", "Asia/Tehran")
MARKETDATA_IGNORE_MARKET_HOURS = os.getenv("MARKETDATA_IGNORE_MARKET_HOURS", "False").lower() in ("true", "1")
# Extra TSE symbols to sync beyond assets with a tse_symbol (comma-separated).
MARKETDATA_EXTRA_SYMBOLS = [
    s.strip() for s in os.getenv("MARKETDATA_EXTRA_SYMBOLS", "").split(",") if s.strip()
]
TSETMC_HISTORY_URL = os.getenv(
    "TSETMC_HISTORY_URL", "https://Api.BrsApi.ir/Tsetmc/History.php"
)

# Manual prices for assets that have no reliable API (e.g. Swiss gold bars).
# Coerced to Decimal once at load so the extractor never re-parses them.
MANUAL_PRICES = {
    "swiss_gold_bar_1g": Decimal(os.getenv("SWISS_GOLD_BAR_1G", "25900000")),
    "swiss_gold_bar_2_5g": Decimal(os.getenv("SWISS_GOLD_BAR_2_5G", "61610000")),
}

_redis_url = os.getenv("REDIS_URL", "")
if _redis_url:
    from urllib.parse import urlparse, urlunparse
    _parsed = urlparse(_redis_url)
    _redis_default = urlunparse((_parsed.scheme, _parsed.netloc, "/2", _parsed.params, _parsed.query, _parsed.fragment))
else:
    _redis_default = "redis://localhost:6379/2"

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", _redis_default)

CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE  # defined above; Celery needs its own copy
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # survive a broker restart

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
QUEUE_BACKLOG_THRESHOLD = int(os.getenv("QUEUE_BACKLOG_THRESHOLD", "100"))
APPLICATION_ERROR_THRESHOLD = int(os.getenv("APPLICATION_ERROR_THRESHOLD", "20"))
PRICE_STALE_THRESHOLD_SECONDS = int(
    os.getenv("PRICE_STALE_THRESHOLD_SECONDS", "900")
)

from config.observability import init_sentry

init_sentry(SENTRY_DSN, environment=ENVIRONMENT)

# Production-only security posture (M7, M8). Gated on ENVIRONMENT rather than
# solely on DEBUG (belt and suspenders): flipping DEBUG=1 for a one-off debugging
# session in a prod/staging environment must not silently drop HSTS/secure-cookie
# hardening.
if ENVIRONMENT != "dev" or not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_REFERRER_POLICY = "same-origin"
    # M8: a non-debug server must not trust a localhost/loopback CORS origin.
    if any("localhost" in o or "127.0.0.1" in o for o in CORS_ALLOWED_ORIGINS):
        raise ImproperlyConfigured(
            "Refusing to start: CORS_ALLOWED_ORIGINS contains localhost with DEBUG=False."
        )

# 12-factor logging: structured lines to stdout only (the container runtime
# collects them). No files — disk in a container is ephemeral and stdout plays
# well with `docker compose logs` / journald / your log shipper.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "console": {
            "format": "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
        },
    },
    "filters": {
        "request_id": {"()": "config.logging.RequestIDFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "filters": ["request_id"],
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # request/response lines (status + path), useful in prod.
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "portfolio": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "marketdata": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # Suspicious-request signals (disallowed host, bad CSRF/session cookie,
        # etc.) that Django's SecurityMiddleware/CommonMiddleware/CSRF raise.
        "django.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# Risk-free rate for analytics
RISK_FREE_RATE_ANNUAL = 0.30
RISK_FREE_RATE_BY_JALALI_YEAR = {
    1399: 0.18,
    1400: 0.20,
    1401: 0.23,
    1402: 0.30,
    1403: 0.30,
    1404: 0.30,
    1405: 0.30,
}
RISK_FREE_RATE_SOURCE = "CBI annual deposit/bond-rate assumptions; manually reviewed"

# Cumulative annual CPI index derived from SCI annual CPI releases, base 1398=100.
CPI_BY_JALALI_YEAR = {
    1398: 100.0,
    1399: 136.4,
    1400: 191.2,
    1401: 278.8,
    1402: 392.3,
    1403: 519.8,
    1404: 680.9,
}
CPI_SOURCE = "Statistical Center of Iran annual CPI releases; manually reviewed"


def rate_for(jalali_year):
    return float(
        RISK_FREE_RATE_BY_JALALI_YEAR.get(jalali_year, RISK_FREE_RATE_ANNUAL)
    )


def cpi_for(jalali_year):
    years = sorted(CPI_BY_JALALI_YEAR)
    if jalali_year <= years[0]:
        return float(CPI_BY_JALALI_YEAR[years[0]])
    if jalali_year >= years[-1]:
        return float(CPI_BY_JALALI_YEAR[years[-1]])
    return float(CPI_BY_JALALI_YEAR[jalali_year])


# Django only exposes uppercase names through django.conf.settings.
RATE_FOR = rate_for
CPI_FOR = cpi_for
