"""Django settings for portfolio-saas.

Configuration is environment-driven so the same image runs in dev, CI, and prod.
Prices are fetched centrally and shared across every user (see pricing/), which
is the key reason this design scales: one fetch updates everyone's valuation.
"""
import math
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
    "config.observability.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"  # gunicorn runs uvicorn workers; there is no WSGI entrypoint

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
    # Translates domain errors (currently CpiUnavailable) that any
    # basis-accepting endpoint can raise into honest responses instead of 500s.
    "EXCEPTION_HANDLER": "config.api.handle",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
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
    "DEFAULT_RENDERER_CLASSES": ("config.api.DecimalStringJSONRenderer",),
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(os.getenv("REFRESH_TOKEN_LIFETIME_DAYS", "30"))),
    "USER_ID_FIELD": "id",
    "USER_ID_CLAIM": "user_id",
    # Rotate refresh tokens on use and blacklist the old one, so a stolen refresh
    # token has a single-use window instead of being valid for the full 7 days.
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "CHECK_REVOKE_TOKEN": True,
}
JWT_COOKIE_SECURE = not DEBUG
REGISTRATION_OPEN = False
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "30"))
SNAPSHOT_PRUNE_ENABLED = os.getenv(
    "SNAPSHOT_PRUNE_ENABLED", "1" if ENVIRONMENT == "production" else "0"
) == "1"
PRICE_RETENTION_DAYS = int(os.getenv("PRICE_RETENTION_DAYS", "14"))
PRICE_PRUNE_ENABLED = os.getenv(
    "PRICE_PRUNE_ENABLED", "1" if ENVIRONMENT == "production" else "0"
) == "1"
VPS_DISK_BUDGET_GB = int(os.getenv("VPS_DISK_BUDGET_GB", "250"))
MARKETDATA_HTTP_CONNECT_TIMEOUT = float(os.getenv("MARKETDATA_HTTP_CONNECT_TIMEOUT", "5"))
MARKETDATA_HTTP_READ_TIMEOUT = float(os.getenv("MARKETDATA_HTTP_READ_TIMEOUT", "12"))
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

# Seconds to sleep between BrsApi calls inside one sync task (paid API courtesy).
MARKETDATA_FETCH_DELAY = float(os.getenv("MARKETDATA_FETCH_DELAY", "0.05"))
# NOTE: there is deliberately no MARKETDATA_DAILY_REQUEST_LIMIT any more. The
# provider meters each API key separately (~10,000/day TSETMC, ~1,500/day BRS),
# so one number could never describe the account -- and the one that was here
# capped the pair at 9,800, which let a full TSETMC backfill refuse gold/currency
# calls with 79% of that plan unspent. The limit is now learned from the
# provider's own `account` block and enforced by its refusal; see marketdata/quota.py.
MARKETDATA_WINDOW_LIMIT = int(os.getenv("MARKETDATA_WINDOW_LIMIT", "1000"))
MARKETDATA_WINDOW_SECONDS = int(os.getenv("MARKETDATA_WINDOW_SECONDS", "300"))

# Per-bucket ceilings, applied WITHIN each provider plan. LIVE and OTHER have a
# bounded, knowable daily cost so they keep a cap; ARCHIVE has an effectively
# infinite backlog and deliberately has none (see marketdata/quota.bucket_budget).
#
# The floor is the live reserve's lower bound, used when the plan cannot be read
# (cold DB, mid-migration): over-reserving only slows the backfill, while
# under-reserving gets customer-facing price fetches refused.
MARKETDATA_LIVE_REQUEST_FLOOR = int(os.getenv("MARKETDATA_LIVE_REQUEST_FLOOR", "1200"))
MARKETDATA_LIVE_REQUEST_HEADROOM = int(os.getenv("MARKETDATA_LIVE_REQUEST_HEADROOM", "500"))
MARKETDATA_OTHER_REQUEST_BUDGET = int(os.getenv("MARKETDATA_OTHER_REQUEST_BUDGET", "200"))

# What each plan's daily ceiling is EXPECTED to be, per subscription. Used only
# to size the live reserve and the archive cap until the provider discloses its
# own number, which then wins (see quota.effective_limit).
#
# This is not the ceiling the 2026-08-24 outage was about. That was one SHARED
# counter across two wallets, so spending either drained both. These are
# per-plan and they are not a spend cap -- the provider's refusal and the
# circuit breaker still decide when to stop. Without them the reserve cannot be
# computed at all before the provider first errors, and `row.limit` is 0 on 8 of
# any 10 days: that gap is what let archive spend 10,034 of 10,000 requests
# before dawn on 2026-08-26 while live_used sat at 0.
MARKETDATA_PLAN_LIMIT_TSETMC = int(os.getenv("MARKETDATA_PLAN_LIMIT_TSETMC", "10000"))
MARKETDATA_PLAN_LIMIT_BRS = int(os.getenv("MARKETDATA_PLAN_LIMIT_BRS", "1500"))
# Archive stops this far short of the ceiling so the wallet is never actually
# exhausted. Exhaustion trips the breaker, and the breaker is what took the live
# lane down with it.
MARKETDATA_PLAN_SAFETY_MARGIN = int(os.getenv("MARKETDATA_PLAN_SAFETY_MARGIN", "150"))
MARKETDATA_ARCHIVE_BATCH_SIZE = int(os.getenv("MARKETDATA_ARCHIVE_BATCH_SIZE", "120"))
# Pending work, not active workers. Sized to keep archive workers busy between
# scheduler ticks without exceeding the archive slice of the 5-minute window
# (~750 req / 5 min). Was 4 and starved fill rate (~4 HTTP/min vs ~100+ capacity).
MARKETDATA_ARCHIVE_QUEUE_LIMIT = int(
    os.getenv("MARKETDATA_ARCHIVE_QUEUE_LIMIT", "48")
)
# Share of each archive batch reserved for intraday ticks, the one endpoint with
# an unbounded backlog (one symbol-day per request, and grow_tick_windows keeps
# reopening finished windows back to each symbol's listing date). A share rather
# than strict priority: at ~5M requests to exhaust, strict priority would starve
# candles, price history, gold and Codal for well over a year. The remainder goes
# to every other endpoint, and either lane takes slots the other cannot fill.
MARKETDATA_TICK_QUOTA_SHARE = float(os.getenv("MARKETDATA_TICK_QUOTA_SHARE", "0.70"))

# Per-day HISTORICAL_PER_DAY endpoints (ticks) walk one calendar day per request,
# so the trailing window is bounded to keep cost finite. Trading days only -- a
# non-trading day simply has no daily candle, so it is never requested.
MARKETDATA_TICK_WINDOW_DAYS = int(os.getenv("MARKETDATA_TICK_WINDOW_DAYS", "90"))
# Relative |tick_vol - candle_vol| / max(...) allowed before quarantine. Measured
# mismatch distribution: ~75% of provider disagreements sit under 1%; the long
# tail (near-total disagreement) still rejects. Override via env if needed.
MARKETDATA_TICK_VOLUME_TOLERANCE = float(
    os.getenv("MARKETDATA_TICK_VOLUME_TOLERANCE", "0.01")
)

# Master switch for the whole Codal subsystem. Off means: no `codal` queue route,
# no beat entry, no archive states claimed for CODAL_ANNOUNCEMENTS, no extraction
# enqueued at ingest, and no Ops panel -- the code and the stored rows survive,
# nothing runs. codal.ir is unreachable from the production VPS (TCP 443 times
# out) and CODAL_HTTP_PROXY is unset, so every attempt burned CPU retrying a
# connect that cannot succeed: ~20,000 no-op workflow runs and 583 connect
# timeouts in one day. Turn back on once the network path exists.
CODAL_ENABLED = os.getenv("CODAL_ENABLED", "0") == "1"
# Codal announcements are paged 20 per request and a mature symbol has ~50 pages,
# so "all history for all symbols" is ~32,000 requests -- more than three days of
# the whole archive budget. Only page 1 was ever fetched, which stored 2% and
# still reported verified. Bound the target to the newest N pages per symbol so
# the state can honestly converge; raise it when the backlog is otherwise idle.
MARKETDATA_CODAL_MAX_PAGES = int(os.getenv("MARKETDATA_CODAL_MAX_PAGES", "5"))
# 20/day left 96.7% of the 76,868-row backlog (74,303 rows) never even attempted
# -- at 20/day it clears in ~10 years. Raised alongside the beat schedule itself
# running every 6h instead of once/day (config/celery.py); still bounded well
# under the shared ARCHIVE quota's daily budget.
CODAL_EXTRACT_BATCH_SIZE = int(os.getenv("CODAL_EXTRACT_BATCH_SIZE", "200"))
# A Codal report sits in FETCHING while its artifact downloads. Past this age it
# is not in flight, it is stranded -- a dead worker or a hung socket -- and is
# eligible to be queued again.
CODAL_FETCHING_STALE_SECONDS = int(os.getenv("CODAL_FETCHING_STALE_SECONDS", "1800"))

# Artifact download+storage (marketdata/codal_storage.py). No proxy required by
# default -- unset means connect to codal.ir directly, correct on any host that
# can already reach it.
CODAL_HTTP_PROXY = os.getenv("CODAL_HTTP_PROXY", "")
# Reachability breaker (marketdata/codal_storage.py). codal.ir is unreachable from
# some hosts -- from the production VPS, TCP 443 times out outright. Without this
# the extractor retried a dead network path thousands of times a day. After N
# consecutive connect-level failures the whole origin is parked for the cooldown,
# and one probe per cooldown notices when it comes back.
CODAL_ORIGIN_FAILURE_THRESHOLD = int(os.getenv("CODAL_ORIGIN_FAILURE_THRESHOLD", "10"))
CODAL_ORIGIN_COOLDOWN_SECONDS = int(os.getenv("CODAL_ORIGIN_COOLDOWN_SECONDS", "900"))
CODAL_MAX_ARTIFACT_BYTES = int(os.getenv("CODAL_MAX_ARTIFACT_BYTES", str(50 * 1024 * 1024)))
CODAL_S3_ENDPOINT_URL = os.getenv("CODAL_S3_ENDPOINT_URL", "http://minio:9000")
CODAL_S3_BUCKET = os.getenv("CODAL_S3_BUCKET", "codal-artifacts")
CODAL_S3_ACCESS_KEY = os.getenv("CODAL_S3_ACCESS_KEY", "")
CODAL_S3_SECRET_KEY = os.getenv("CODAL_S3_SECRET_KEY", "")
CODAL_S3_REGION = os.getenv("CODAL_S3_REGION", "us-east-1")
# Bump to force every report through a fresh extract_report() pass regardless
# of its current status -- not wired to any auto-reprocessing yet, just the
# version stamp CodalReport/CodalParsedTable/CodalFact rows carry.
CODAL_PARSER_VERSION = os.getenv("CODAL_PARSER_VERSION", "2")

WORKFLOW_RETENTION_DAYS = int(os.getenv("WORKFLOW_RETENTION_DAYS", "30"))

# Live poll cadence by market state (seconds). Beat still ticks every minute; the
# task itself decides whether enough time has passed, so the cadence can change
# without a beat restart. See marketdata/market_state.py for the arithmetic.
# Open: 2 min; daytime/overnight: 5 min (frees quota for archive).
MARKETDATA_LIVE_INTERVAL_OPEN = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OPEN", "120"))
MARKETDATA_LIVE_INTERVAL_DAYTIME = int(os.getenv("MARKETDATA_LIVE_INTERVAL_DAYTIME", "300"))
MARKETDATA_LIVE_INTERVAL_OVERNIGHT = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OVERNIGHT", "300"))

MARKETDATA_QUOTA_TIMEZONE = os.getenv("MARKETDATA_QUOTA_TIMEZONE", "Asia/Tehran")
MARKETDATA_IGNORE_MARKET_HOURS = os.getenv("MARKETDATA_IGNORE_MARKET_HOURS", "False").lower() in ("true", "1")
MARKETDATA_REQUIRE_SHARED_WINDOW = os.getenv(
    "MARKETDATA_REQUIRE_SHARED_WINDOW", "True"
).lower() in ("true", "1")
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
WORKFLOW_FAILURE_RATE_THRESHOLD = float(os.getenv("WORKFLOW_FAILURE_RATE_THRESHOLD", "0.10"))
# Today 1,072 of 1,346 symbols (0.80) fail the integrity gate purely because the
# tick backfill is unfinished. Start just above that so the alert means "coverage
# regressed", and lower it as the archive fills.
INTEGRITY_FAILURE_RATE_THRESHOLD = float(os.getenv("INTEGRITY_FAILURE_RATE_THRESHOLD", "0.85"))
ARCHIVE_PROGRESS_STALE_SECONDS = int(os.getenv("ARCHIVE_PROGRESS_STALE_SECONDS", "1800"))
# consecutive_failures resets only on real progress, so it is the wedged counter.
# Observed split: 482 states sit at 1-2 (ordinary provider timeouts), then a gap.
# By 6 the backoff has capped at 24h -- retrying daily, converging never.
ARCHIVE_WEDGED_FAILURE_THRESHOLD = int(os.getenv("ARCHIVE_WEDGED_FAILURE_THRESHOLD", "6"))
WAREHOUSE_AUDIT_DIR = os.getenv(
    "WAREHOUSE_AUDIT_DIR", str(BASE_DIR / "recovery-manifests")
)
PRICE_STALE_THRESHOLD_SECONDS = int(
    os.getenv("PRICE_STALE_THRESHOLD_SECONDS", "900")
)

# nightly_series_validation spike gates, as absolute daily log-returns. These
# were previously reachable only as `getattr(settings, ..., math.log(x))`
# fallbacks inside marketdata/tasks.py, so the numbers actually in force were
# invisible here and untunable without a code change. Same values, now declared.
# A day moving more than this is quarantined as `series_spike` unless a
# CorporateAction covers the date.
SERIES_VALIDATION_THRESHOLD_STOCK = float(
    os.getenv("SERIES_VALIDATION_THRESHOLD_STOCK", str(math.log(1.5)))  # +-50%
)
SERIES_VALIDATION_THRESHOLD_CRYPTO = float(
    os.getenv("SERIES_VALIDATION_THRESHOLD_CRYPTO", str(math.log(2.0)))  # +-100%
)
SERIES_VALIDATION_THRESHOLD_COMMODITY = float(
    os.getenv("SERIES_VALIDATION_THRESHOLD_COMMODITY", str(math.log(1.2)))  # +-20%
)
SERIES_VALIDATION_THRESHOLD_CURRENCY = float(
    os.getenv("SERIES_VALIDATION_THRESHOLD_CURRENCY", str(math.log(1.15)))  # +-15%
)
SERIES_VALIDATION_THRESHOLD_GOLD = float(
    os.getenv("SERIES_VALIDATION_THRESHOLD_GOLD", str(math.log(1.2)))  # +-20%
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
        "raw": {"format": "%(message)s"},
    },
    "filters": {
        "request_id": {"()": "config.observability.RequestIDFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "filters": ["request_id"],
        },
        "workflow": {
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
        "workflow": {"handlers": ["workflow"], "level": "INFO", "propagate": False},
        "celery": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "celery.task": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        # Suspicious-request signals (disallowed host, bad CSRF/session cookie,
        # etc.) that Django's SecurityMiddleware/CommonMiddleware/CSRF raise.
        "django.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

class CpiUnavailable(Exception):
    """No configured CPI value for a Jalali year — never a stale clamp.

    Raised by `cpi_for()` for any year outside the table (except extrapolation
    below the earliest known year, which is intentional). `jalali_year` is what
    was asked for; `last_verified_year` is the newest year the table covers.
    Add missing years via CPI_BY_JALALI_YEAR_EXTRA rather than guessing.
    """

    def __init__(self, jalali_year, last_verified_year):
        self.jalali_year = jalali_year
        self.last_verified_year = last_verified_year
        super().__init__(
            f"Inflation-adjusted values are unavailable for Jalali year "
            f"{jalali_year}. Converting to real Toman needs a consumer price "
            f"index for that year, and the table only reaches "
            f"{last_verified_year} — so the figure cannot be computed without "
            f"inventing the missing inflation. Nominal Toman, USD and USDT are "
            f"unaffected. Two ways to resolve it: set CPI_BY_JALALI_YEAR_EXTRA "
            f"to the published figure (JSON, e.g. '{{\"{jalali_year}\": 950.0}}'), "
            f"or set CPI_ESTIMATED_MONTHLY_RATE to an estimated monthly "
            f"inflation rate, which projects forward from {last_verified_year} "
            f"and is labelled as an estimate everywhere it is used."
        )


# Risk-free rate for analytics. This is a hand-maintained ASSUMPTION (CBI
# deposit/bond-rate estimate), not a measured market yield — see
# RISK_FREE_RATE_SOURCE, which callers should surface alongside any Sharpe
# or risk-adjusted number computed with it.
RISK_FREE_RATE_ANNUAL = 0.38
RISK_FREE_RATE_BY_JALALI_YEAR = {
    1399: 0.18,
    1400: 0.20,
    1401: 0.23,
    1402: 0.30,
    1403: 0.30,
    1404: 0.30,
    1405: 0.38,
}
RISK_FREE_RATE_SOURCE = (
    "CBI annual deposit/bond rate; operator-confirmed 38% for 1405. Earlier "
    "years remain the prior hand-maintained estimates"
)

# Cumulative CPI index, base 1398=100. Each entry anchors 1 Farvardin of its
# year; `cpi_for_date` interpolates between consecutive anchors, so year N's
# inflation is encoded in the gap between anchor N and anchor N+1 -- which is
# why the newest anchor alone leaves its own year deflating flat.
CPI_BY_JALALI_YEAR = {
    1398: 100.0,
    1399: 136.4,
    1400: 191.2,
    1401: 278.8,
    1402: 392.3,
    1403: 519.8,
    1404: 680.9,
}
# Everything above is a published SCI figure. Anything added below is not.
CPI_VERIFIED_THROUGH_YEAR = max(CPI_BY_JALALI_YEAR)

# Operator estimate for years SCI has not published yet. Set to 0.0 to restore
# strict behaviour (any unpublished year raises CpiUnavailable).
#
# 6.5%/month compounds to +112%/year. That is roughly TRIPLE the pace of the
# verified series above, whose annual steps run 36.4 / 40.2 / 45.8 / 40.7 /
# 32.5 / 31.0 percent -- about 2.5%/month. This figure is an operator judgement
# that the regime broke, not a continuation of the trend, and it is deliberately
# separated from the verified table so nothing downstream can mistake it for one.
CPI_ESTIMATED_MONTHLY_RATE = float(os.environ.get("CPI_ESTIMATED_MONTHLY_RATE", "0.065"))
# How far past the verified table the estimate may run.
#
# Two years is the minimum that makes the *current* year deflate at all: the
# current year needs its own anchor and the next one to interpolate between.
# Measured against the calendar rather than fixed, or the horizon stops covering
# the year in progress at the next Nowruz and real Toman goes quietly flat
# again -- which is how it broke this time. Capped at four so an unattended
# deployment cannot keep compounding a guess indefinitely; past that,
# CpiUnavailable fires and someone has to look at it.
import jdatetime as _jdatetime  # noqa: E402  (plain library, no Django setup needed)

CPI_ESTIMATE_MAX_YEARS = int(os.environ.get(
    "CPI_ESTIMATE_MAX_YEARS",
    max(2, min(4, _jdatetime.date.today().year + 1 - CPI_VERIFIED_THROUGH_YEAR)),
))

CPI_ESTIMATED_YEARS = set()
if CPI_ESTIMATED_MONTHLY_RATE > 0:
    _cpi_anchor = CPI_BY_JALALI_YEAR[CPI_VERIFIED_THROUGH_YEAR]
    for _ahead in range(1, CPI_ESTIMATE_MAX_YEARS + 1):
        _year = CPI_VERIFIED_THROUGH_YEAR + _ahead
        CPI_BY_JALALI_YEAR[_year] = _cpi_anchor * (1 + CPI_ESTIMATED_MONTHLY_RATE) ** (12 * _ahead)
        CPI_ESTIMATED_YEARS.add(_year)

# Extend without a code deploy once a new year's figure is published, e.g.
# CPI_BY_JALALI_YEAR_EXTRA='{"1405": 950.0}'. Keys may be str or int. A real
# figure supersedes the estimate for that year and is marked verified again.
_cpi_extra_raw = os.environ.get("CPI_BY_JALALI_YEAR_EXTRA", "")
if _cpi_extra_raw:
    import json as _json

    _cpi_extra = {int(year): float(value) for year, value in _json.loads(_cpi_extra_raw).items()}
    CPI_BY_JALALI_YEAR.update(_cpi_extra)
    CPI_ESTIMATED_YEARS -= set(_cpi_extra)
    CPI_VERIFIED_THROUGH_YEAR = max(
        year for year in CPI_BY_JALALI_YEAR if year not in CPI_ESTIMATED_YEARS
    )

CPI_SOURCE = (
    f"Statistical Center of Iran annual CPI releases, verified through "
    f"{CPI_VERIFIED_THROUGH_YEAR}"
    + (
        f"; {'/'.join(str(year) for year in sorted(CPI_ESTIMATED_YEARS))} are an "
        f"OPERATOR ESTIMATE at {CPI_ESTIMATED_MONTHLY_RATE:.1%}/month, not a "
        f"published figure"
        if CPI_ESTIMATED_YEARS
        else ""
    )
)


def rate_for(jalali_year):
    return float(
        RISK_FREE_RATE_BY_JALALI_YEAR.get(jalali_year, RISK_FREE_RATE_ANNUAL)
    )


def cpi_for(jalali_year):
    """CPI index for a Jalali year, or raise CpiUnavailable — never a silent clamp.

    Extrapolating backward below the earliest known year is intentional (the
    index is defined as flat before its base year). Anything at or above the
    newest known year, or any interior gap left by a partial override, is a
    real unknown and must fail loud rather than reuse the last known number.
    """
    years = sorted(CPI_BY_JALALI_YEAR)
    if jalali_year in CPI_BY_JALALI_YEAR:
        return float(CPI_BY_JALALI_YEAR[jalali_year])
    if jalali_year < years[0]:
        return float(CPI_BY_JALALI_YEAR[years[0]])
    raise CpiUnavailable(jalali_year, years[-1])


# Django only exposes uppercase names through django.conf.settings.
RATE_FOR = rate_for
CPI_FOR = cpi_for
