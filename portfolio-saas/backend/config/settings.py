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

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "1") == "1"
# H7: refuse to boot a non-debug server on the committed default key.
if not DEBUG and SECRET_KEY == "dev-insecure-change-me":
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY when DJANGO_DEBUG=0.")
ALLOWED_HOSTS = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,backend").split(",")

# Frontend origin(s) for CORS. Comma-separated in dev (Vite on 5173).
CORS_ALLOWED_ORIGINS = [
    o for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "accounts",
    "portfolios",
    "pricing",
    "billing",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
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
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

AUTH_USER_MODEL = "accounts.User"

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
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    # H5: throttle anonymous endpoints (login/register) by IP. Authenticated
    # requests bypass AnonRateThrottle automatically.
    "DEFAULT_THROTTLE_CLASSES": ("rest_framework.throttling.AnonRateThrottle",),
    "DEFAULT_THROTTLE_RATES": {"anon": os.getenv("ANON_THROTTLE", "30/min")},
    # M5: render Decimal as a string so large Toman values stay exact on the wire.
    "DEFAULT_RENDERER_CLASSES": ("config.renderers.DecimalStringJSONRenderer",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
}

# Market data sources. The BRS/TSETMC keys are the same shape as the original
# PORTFOLIO project's settings.json, now 12-factor env vars.
BRS_API_KEY = os.getenv("BRS_API_KEY", "")
BRS_URL = os.getenv(
    "BRS_URL", "https://BrsApi.ir/Api/Market/Gold_Currency.php"
)
TSETMC_API_KEY = os.getenv("TSETMC_API_KEY", "")
TSETMC_URL = os.getenv("TSETMC_URL", "https://BrsApi.ir/Api/Tsetmc/AllSymbols.php")
TSETMC_SYMBOL_URL = os.getenv(
    "TSETMC_SYMBOL_URL", "https://BrsApi.ir/Api/Tsetmc/Symbol.php"
)
TSETMC_HISTORY_URL = os.getenv(
    "TSETMC_HISTORY_URL", "https://BrsApi.ir/Api/Tsetmc/History.php"
)

# Manual prices for assets that have no reliable API (e.g. Swiss gold bars).
# Coerced to Decimal once at load so the extractor never re-parses them.
MANUAL_PRICES = {
    "swiss_gold_bar_1g": Decimal(os.getenv("SWISS_GOLD_BAR_1G", "25900000")),
    "swiss_gold_bar_2_5g": Decimal(os.getenv("SWISS_GOLD_BAR_2_5G", "61610000")),
}

# Celery beat drives the real-time fetch loop. The broker uses Redis DB 2 to stay
# clear of the cache (DB 1); both fall back to localhost when REDIS_URL is unset.
_redis_default = os.getenv("REDIS_URL", "redis://localhost:6379/2").rsplit("/", 1)[0] + "/2"
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", _redis_default)
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", CELERY_BROKER_URL)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE  # defined above; Celery needs its own copy
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # survive a broker restart

# Stripe billing. The Checkout price id and the webhook signing secret come from
# the Stripe dashboard; the webhook endpoint is signature-verified and idempotent
# (see billing/). All four fall back to empty/placeholder so the app still boots
# and tests run without a Stripe account.
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.getenv("STRIPE_PRO_PRICE_ID", "")
STRIPE_SUCCESS_URL = os.getenv("STRIPE_SUCCESS_URL", "http://localhost:5173/billing?status=success")
STRIPE_CANCEL_URL = os.getenv("STRIPE_CANCEL_URL", "http://localhost:5173/billing?status=cancel")

# Production-only security posture (M7, M8). These are evaluated at settings
# import; tests/dev boot with DEBUG=True so neither branch runs there.
if not DEBUG:
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
