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
    "research",
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

# A single connection string wins when present, for any runner that has no
# docker-compose service hostnames to resolve (a one-off `manage.py` invocation
# against a restored database, say). Compose itself sets the POSTGRES_* vars
# above and leaves this unset.
#
# Two things here are easy to get wrong and both were:
#
#   * `urlparse` does NOT percent-decode `username`/`password`. Every reserved
#     character in a password -- `@ / : # ?` -- has to be percent-encoded to
#     form a valid URL, so the raw attribute hands Postgres the literal
#     "p%40ss" and authentication fails with a password that is in fact
#     correct. `unquote` is the documented counterpart.
#   * `sslmode` is a libpq connection parameter, and Django passes exactly
#     `OPTIONS` through to the driver -- an unknown key at the top level of a
#     DATABASES entry is silently dropped. It sat there, so `PG_SSLMODE` was a
#     TLS switch that read as configurable and connected in the clear whatever
#     it was set to.
_database_url = os.getenv("DATABASE_URL")
if _database_url:
    from urllib.parse import unquote, urlparse

    _u = urlparse(_database_url)
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": unquote(_u.path.lstrip("/")) or "portfolio",
        "USER": unquote(_u.username) if _u.username else "portfolio",
        "PASSWORD": unquote(_u.password) if _u.password else "",
        "HOST": _u.hostname or "db",
        "PORT": str(_u.port or 5432),
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "OPTIONS": {"sslmode": os.getenv("PG_SSLMODE", "prefer")},
    }

# Redis caches the global price map and per-user valuations so reads stay cheap
# under load. Falls back to local-memory if REDIS_URL is unset (e.g. quick tests).
if os.getenv("REDIS_URL"):
    # No OPTIONS. There used to be `{"CONNECTION_CLASS_KWARGS": {"ssl_cert_reqs":
    # None}}` here, which django-redis has never read -- its connection factory
    # knows CONNECTION_POOL_KWARGS and REDIS_CLIENT_KWARGS, and silently ignores
    # any other key. So it disabled nothing, and what it claimed to disable was
    # TLS certificate verification, which is not something to switch off by
    # default. A `rediss://` deployment that genuinely needs a relaxed check
    # should say so explicitly under CONNECTION_POOL_KWARGS.
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": os.environ["REDIS_URL"],
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
        "analytics": os.getenv("ANALYTICS_THROTTLE", "60/min"),
        "research": os.getenv("RESEARCH_THROTTLE", "10/hour"),
        # Tight on purpose: this endpoint sends mail. The anon bucket is 30/min
        # and would let a bot empty an SMTP quota. Tests clear the cache.
        "password_reset": os.getenv("PASSWORD_RESET_THROTTLE", "5/hour"),
        # Credential-guessing deserves its own bucket. On the shared `anon`
        # rate these two competed with the signed-out page's own boot requests
        # (registration status, CSRF), so several people signing up from one
        # office IP could exhaust the bucket by using the app correctly while a
        # brute-force attempt still got 30 guesses a minute. Env-tunable so a
        # room full of guests can be accommodated without a deploy.
        "login": os.getenv("LOGIN_THROTTLE", "20/min"),
        "register": os.getenv("REGISTER_THROTTLE", "10/min"),
    },
    # M5: render Decimal as a string so large Toman values stay exact on the wire.
    "DEFAULT_RENDERER_CLASSES": ("config.api.DecimalStringJSONRenderer",),
}

# Companion to the `analytics` throttle above: that bounds requests per minute,
# these bound how many may be *in flight* at once (portfolio/views/_common.py).
# The optimizer holds a worker for seconds at a time, so the rate limit alone
# does not stop a handful of concurrent solves from starving the pool. Defaults
# are the literals this guard has always used; they are named here so a test run
# can raise them the same way it raises the throttles.
ANALYTICS_MAX_CONCURRENT_PER_USER = int(os.getenv("ANALYTICS_MAX_CONCURRENT_PER_USER", "2"))
ANALYTICS_MAX_CONCURRENT_GLOBAL = int(os.getenv("ANALYTICS_MAX_CONCURRENT_GLOBAL", "5"))

# GapGPT credentials are projected from the News deployment into one read-only
# file on the portfolio backend. No provider secret is stored in a model or sent
# to the browser. A run chooses its own smaller cap in the research UI.
GAPGPT_CONFIG_FILE = os.getenv("GAPGPT_CONFIG_FILE", "/run/secrets/gapgpt.env")
RESEARCH_MAX_RUN_USD = Decimal(os.getenv("RESEARCH_MAX_RUN_USD", "0.10"))
RESEARCH_DAILY_BUDGET_USD = Decimal(os.getenv("RESEARCH_DAILY_BUDGET_USD", "1.00"))
RESEARCH_MAX_OUTPUT_TOKENS = int(os.getenv("RESEARCH_MAX_OUTPUT_TOKENS", "160"))

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
REGISTRATION_OPEN = os.getenv("REGISTRATION_OPEN", "1") == "1"

# Mail. `.env.production.example` has named these for some time; Django does
# not read EMAIL_* from the environment on its own, so a filled-in env file
# previously configured nothing. Console in DEBUG when no host is set, so a
# local reset still prints a link. Production without EMAIL_HOST still boots
# -- password reset then logs the send failure rather than taking the API down.
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_TIMEOUT = 10
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.getenv("EMAIL_USE_TLS", "1") == "1"
EMAIL_USE_SSL = os.getenv("EMAIL_USE_SSL", "0") == "1"
if EMAIL_USE_SSL:
    EMAIL_USE_TLS = False
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "noreply@localhost")
if EMAIL_HOST:
    EMAIL_BACKEND = os.getenv(
        "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
    )
elif DEBUG:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
else:
    EMAIL_BACKEND = os.getenv(
        "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
    )
FRONTEND_PASSWORD_RESET_URL = os.getenv(
    "FRONTEND_PASSWORD_RESET_URL", "http://localhost:5173/reset-password"
)
FRONTEND_VERIFICATION_URL = os.getenv(
    "FRONTEND_VERIFICATION_URL", "http://localhost:5173/verify-email"
)
SNAPSHOT_RETENTION_DAYS = int(os.getenv("SNAPSHOT_RETENTION_DAYS", "30"))
SNAPSHOT_PRUNE_ENABLED = os.getenv(
    "SNAPSHOT_PRUNE_ENABLED", "1" if ENVIRONMENT == "production" else "0"
) == "1"
PRICE_RETENTION_DAYS = int(os.getenv("PRICE_RETENTION_DAYS", "14"))
PRICE_PRUNE_ENABLED = os.getenv(
    "PRICE_PRUNE_ENABLED", "1" if ENVIRONMENT == "production" else "0"
) == "1"
# Where to statvfs for the disk projection. Not a size: the size is measured.
# `VPS_DISK_BUDGET_GB` used to live here at 250 while the device was 147 GB,
# which is why `admin_telemetry.project_disk` no longer takes a budget on faith.
DISK_USAGE_PATH = os.getenv("DISK_USAGE_PATH", "/")
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
BRSAPI_ACCOUNT_PHONE = os.getenv("BRSAPI_ACCOUNT_PHONE", "")
# Only enable after a VPS-origin panel read is verified to return the two
# account meters without spending a market-data request or triggering blocks.
MARKETDATA_PANEL_METER_ENABLED = os.getenv("MARKETDATA_PANEL_METER_ENABLED", "0") == "1"
TSETMC_URL = os.getenv("TSETMC_URL", "https://Api.BrsApi.ir/Tsetmc/AllSymbols.php")
TSETMC_SYMBOL_URL = os.getenv(
    "TSETMC_SYMBOL_URL", "https://Api.BrsApi.ir/Tsetmc/Symbol.php"
)

# --------------------------------------------------------------- direct sources
#
# BrsApi resells data that the origins publish for free. Its TSETMC product is
# metered at ~10,000/day; the current Market product has a 1,500/day limit. The paid
# ceiling -- not disk or CPU -- bounds stock-history backfill. Direct origins
# also remove a provider dependency from the live path.
#
# They split into two groups by REACHABILITY, measured from the production VPS
# (Frankfurt, AS202269) on 2026-08-31:
#
#   Reachable directly       tgju.org, apiv2.nobitex.ir, api.wallex.ir
#   Blocked at L3            *.tsetmc.com, tse.ir, fipiran.ir, codal.ir
#
# The blocked group silently drops the SYN from any non-Iranian source address
# (`nc -z` times out; ICMP is dropped too, and traceroute dies one hop inside
# their network). That is a geo-block by the securities organisation's network,
# not a route failure and not TLS filtering -- so no header, SNI or User-Agent
# change can defeat it. The only fix is an egress hop inside Iran, which is what
# IRAN_EGRESS_PROXY is for. Everything downstream of it is already built and
# tested; setting this variable is the whole activation.
IRAN_EGRESS_PROXY = os.getenv("IRAN_EGRESS_PROXY", "")

# TGJU: 962 live gold/FX/commodity instruments in ONE ~180KB request, plus daily
# history to 1390/09/05 (2011-11-26) for the dollar and 1389 for the coin. That
# is deeper than the BRS gold history and costs nothing, which is why it leads
# the gold/currency migration.
TGJU_ENABLED = os.getenv("TGJU_ENABLED", "1") == "1"

# Beta, alpha and the TSE-index benchmark line (`diagnostics._load_index_returns`,
# `ComparisonView`). This has been dead code in production since it was written:
# the flag was never defined anywhere, so it defaulted False, and only tests ever
# ran it under `override_settings`.
#
# The reason was real -- BrsApi publishes the index as a LIVE snapshot and sells
# no history, so `MarketIndexData` held about two weeks of rows and a benchmark
# drawn from that would have been invented. TGJU carries the full daily TEDPIX
# series (2,751 observations back to ~1394), which is data BrsApi cannot sell at
# any price, so the premise no longer holds.
#
# Enabling is safe without a data check: `_load_index_returns` already returns
# None when fewer than two observations exist, and every caller treats None as
# "no benchmark" rather than as zero.
HISTORICAL_BENCHMARK_ENABLED = os.getenv("HISTORICAL_BENCHMARK_ENABLED", "1") == "1"
TGJU_LIVE_URL = os.getenv("TGJU_LIVE_URL", "https://call1.tgju.org/ajax.json")
TGJU_HISTORY_URL = os.getenv(
    "TGJU_HISTORY_URL",
    "https://api.tgju.org/v1/market/indicator/summary-table-data",
)

# Nobitex quotes RIAL (`-rls` pairs). Its UDF history is capped near 500 candles
# per request, so it is the live/cross-check source, not the history source.
NOBITEX_ENABLED = os.getenv("NOBITEX_ENABLED", "1") == "1"
NOBITEX_BASE_URL = os.getenv("NOBITEX_BASE_URL", "https://apiv2.nobitex.ir")

# Wallex quotes TOMAN (`*TMN` pairs) and returned 2,755 daily candles for
# USDTTMN in a single request -- the full series back to 2018-11-27, with no
# 500-row cap. It is therefore the crypto HISTORY source, with Nobitex as the
# independent second opinion on live prices.
WALLEX_ENABLED = os.getenv("WALLEX_ENABLED", "1") == "1"
WALLEX_BASE_URL = os.getenv("WALLEX_BASE_URL", "https://api.wallex.ir")

# Direct TSETMC, used only when IRAN_EGRESS_PROXY is set (see above). Left
# defined unconditionally so the code path is tested on every run and the switch
# is a deploy-time env change rather than a code change.
TSETMC_DIRECT_ENABLED = os.getenv("TSETMC_DIRECT_ENABLED", "0") == "1"
TSETMC_DIRECT_BASE_URL = os.getenv("TSETMC_DIRECT_BASE_URL", "https://cdn.tsetmc.com")

# Politeness pacing for the free origins. Wallex served 60 requests in 17.1s with
# no rate-limit headers and no throttling, i.e. it will let us take far more than
# we should. Self-imposed, because an unmetered origin that stops answering is
# worse than a metered one that bills us.
DIRECT_SOURCE_MIN_INTERVAL = float(os.getenv("DIRECT_SOURCE_MIN_INTERVAL", "0.25"))
# After this many consecutive connect-level failures the origin is parked, with
# one probe per cooldown to notice recovery. Same shape as the Codal breaker,
# which is what stopped ~583 doomed connects a day against an unreachable host.
DIRECT_SOURCE_FAILURE_THRESHOLD = int(os.getenv("DIRECT_SOURCE_FAILURE_THRESHOLD", "8"))
DIRECT_SOURCE_COOLDOWN_SECONDS = int(os.getenv("DIRECT_SOURCE_COOLDOWN_SECONDS", "600"))

# Seconds to sleep between BrsApi calls inside one sync task (paid API courtesy).
MARKETDATA_FETCH_DELAY = float(os.getenv("MARKETDATA_FETCH_DELAY", "0.05"))
# NOTE: there is deliberately no MARKETDATA_DAILY_REQUEST_LIMIT any more. The
# provider products have separate meters (currently 10,000 AIO and 1,500
# Market CGCC requests/day). The limit comes from a provider observation when
# available, with a per-product fallback; see marketdata/quota.py.
MARKETDATA_WINDOW_LIMIT = int(os.getenv("MARKETDATA_WINDOW_LIMIT", "1000"))
MARKETDATA_WINDOW_SECONDS = int(os.getenv("MARKETDATA_WINDOW_SECONDS", "300"))

# Per-bucket ceilings, applied WITHIN each provider plan. LIVE and OTHER have a
# bounded, knowable daily cost so they keep a cap; ARCHIVE has an effectively
# infinite backlog and deliberately has none (see marketdata/quota.bucket_budget).
#
# FLOOR + HEADROOM is the live bucket's own daily ceiling -- the safety valve
# that stops a mis-set cadence spending a wallet, deliberately a constant rather
# than something derived from the plan (a cap that grows with what it caps can
# never bind). The live reserve is a forward simulation of the real planner.
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
# The September 22 panel reports a 1,500-request Market CGCC limit. A
# provider-observed limit supersedes this fallback; zero means unmetered.
MARKETDATA_PLAN_LIMIT_BRS = int(os.getenv("MARKETDATA_PLAN_LIMIT_BRS", "1500"))
# Archive stops this far short of the ceiling so the wallet is never actually
# exhausted. Exhaustion trips the breaker, and the breaker is what took the live
# lane down with it.
#
# 150 -> 50 on 2026-09-09. A margin is insurance against not knowing what the
# provider has really billed. Once the account-panel reader is verified and
# enabled, it checks every five minutes; until then this margin remains a
# conservative fallback, not a guarantee of exact reconciliation.
MARKETDATA_PLAN_SAFETY_MARGIN = int(os.getenv("MARKETDATA_PLAN_SAFETY_MARGIN", "50"))
# How long a tripped plan breaker stays closed before it lets ONE request
# through to ask the provider again. The breaker used to latch until Tehran
# midnight, which assumes a quota-shaped response can only mean a spent wallet
# -- but `looks_like_quota_error` fires on any 5xx whose body contains "limit",
# so an origin blip cost a whole plan for the rest of the day. 0 disables the
# probe and restores the latched behaviour.
MARKETDATA_BREAKER_RETRY_SECONDS = int(
    os.getenv("MARKETDATA_BREAKER_RETRY_SECONDS", "900")
)
MARKETDATA_ARCHIVE_BATCH_SIZE = int(os.getenv("MARKETDATA_ARCHIVE_BATCH_SIZE", "120"))
# OFF by default since 2026-09-06: archive spends its whole day ceiling as fast
# as the window limiter allows, starting at 00:01, and idles once the wallet
# reaches the live reserve. See `quota.archive_allowance_now` for why the
# pro-rata curve this replaces was the wrong fix for the 2026-08-26 incident.
# Turn on only if a provider starts punishing bursts.
MARKETDATA_ARCHIVE_PACE_ENABLED = os.getenv("MARKETDATA_ARCHIVE_PACE_ENABLED", "0") == "1"
# Only consulted when pacing is ON. Hour of the Tehran quota day by which the
# archive's paced allowance reaches the FULL day ceiling. Not 24: a ramp that
# only tops out at 23:59:59 leaves no time to actually spend the last of it, so
# the day structurally ends a few percent short.
MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR = int(
    os.getenv("MARKETDATA_ARCHIVE_PACE_FULL_BY_HOUR", "21")
)
# How long a state waits after an `archive_paced` refusal. Minutes, because the
# ramp advances on its own -- this is a "not yet" from our own scheduler, not a
# refusal from the provider. It used to be deferred to the next quota day, which
# parked ~7,000 states nightly and left ~4,000 TSETMC requests a day unspent.
# Jittered +/-50% at the call site so a refused batch does not return as a herd.
# How long an archive state waits after being refused because the live lane
# still needs the remaining quota. Much longer than a pacing wait: the reserve
# releases in steps as the live cadence winds down (the big one is the 13:00
# Tehran session close), not continuously.
MARKETDATA_LIVE_RESERVED_RETRY_SECONDS = int(
    os.getenv("MARKETDATA_LIVE_RESERVED_RETRY_SECONDS", "1800")
)
MARKETDATA_ARCHIVE_PACED_RETRY_SECONDS = int(
    os.getenv("MARKETDATA_ARCHIVE_PACED_RETRY_SECONDS", "180")
)
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
#
# Cut 0.70 -> 0.25 on 2026-09-04. Ticks were taking the majority of a batch while
# three of the four DAILY endpoints sat ~96% incomplete (78-93 of 1,969 symbols
# each). Those dailies are what the returns matrix, risk and optimization pages
# read; intraday depth is not. The lanes still lend each other unused slots, so
# once the dailies converge the ticks reclaim the batch automatically -- this
# changes the order things finish in, not the total spend.
MARKETDATA_TICK_QUOTA_SHARE = float(os.getenv("MARKETDATA_TICK_QUOTA_SHARE", "0.25"))
# While the per-symbol DAILY endpoints are still short of
# MARKETDATA_DAILY_GATE_COMPLETENESS, ticks drop to this share instead. The two
# lanes are not comparable work: a daily endpoint buys a symbol's entire history
# in one request (~5,900 requests finishes all three), while ticks buy one
# symbol-day each against a ~2.1M-day universe. A fixed split lets the unbounded
# lane hold up the bounded one forever, which is why the dailies had sat unfinished
# for months while ticks took 67% of every day's meter.
MARKETDATA_TICK_SHARE_WHILE_DAILY_GAPS = float(
    os.getenv("MARKETDATA_TICK_SHARE_WHILE_DAILY_GAPS", "0.05")
)
MARKETDATA_DAILY_GATE_COMPLETENESS = float(
    os.getenv("MARKETDATA_DAILY_GATE_COMPLETENESS", "0.95")
)

# Per-day HISTORICAL_PER_DAY endpoints (ticks) walk one calendar day per request,
# so the trailing window is bounded to keep cost finite. Trading days only -- a
# non-trading day simply has no daily candle, so it is never requested.
MARKETDATA_TICK_WINDOW_DAYS = int(os.getenv("MARKETDATA_TICK_WINDOW_DAYS", "90"))
# --- Reversal-model targeting -------------------------------------------------
# Intraday ticks cost one request per symbol-day against a ~2.1M-day universe, so
# which days get bought matters far more than how many. These parameters define
# the only days worth buying: the ones where the strategy would have acted.
# See marketdata/reversal.py for the full reasoning and the measured counts.
#
# A "trigger day" is one whose LOW fell this far below the previous close -- the
# moment you would buy. It is a positive if the CLOSE finished this far above the
# same previous close. Both are fractions, both measured on the UNADJUSTED series
# because the exchange enforces its band on the traded price.
MARKETDATA_REVERSAL_DIP = float(os.getenv("MARKETDATA_REVERSAL_DIP", "0.02"))
MARKETDATA_REVERSAL_RECOVERY = float(os.getenv("MARKETDATA_REVERSAL_RECOVERY", "0.02"))
# Negatives outnumber positives ~15:1 (124,705 vs 8,020 on the top 300). Taking
# them all would spend ten days of meter to make the training set *more*
# imbalanced. Every positive is kept; negatives are sampled to this ratio.
MARKETDATA_REVERSAL_NEGATIVE_RATIO = int(
    os.getenv("MARKETDATA_REVERSAL_NEGATIVE_RATIO", "2")
)
# Symbols in the targeted universe, ranked by median daily turnover.
MARKETDATA_REVERSAL_UNIVERSE_N = int(os.getenv("MARKETDATA_REVERSAL_UNIVERSE_N", "300"))
# Turnover is measured over sessions on or after this Jalali date, and a symbol
# needs at least this many of them to be rankable at all -- otherwise a symbol
# that traded once, hugely, outranks a genuinely liquid one.
MARKETDATA_REVERSAL_LIQUIDITY_SINCE = os.getenv(
    "MARKETDATA_REVERSAL_LIQUIDITY_SINCE", "1403-01-01"
)
MARKETDATA_REVERSAL_MIN_SESSIONS = int(
    os.getenv("MARKETDATA_REVERSAL_MIN_SESSIONS", "100")
)
# A symbol must actually produce the setup this many times to enter the universe.
# Turnover alone put 118 of the top 300 in with zero positives ever: fixed-income
# ETFs are heavily traded and by construction never move 2% in a day, so intraday
# history for them can never yield a training example.
MARKETDATA_REVERSAL_MIN_POSITIVES = int(
    os.getenv("MARKETDATA_REVERSAL_MIN_POSITIVES", "5")
)
# Confine intraday tick spending to that universe. Outside it a symbol still gets
# every daily endpoint; it just does not get one request per historical day for
# intraday detail nothing will train on. 0 disables the restriction.
MARKETDATA_TICK_UNIVERSE_ONLY = os.getenv(
    "MARKETDATA_TICK_UNIVERSE_ONLY", "1"
) == "1"

# Deep tier cap for tick window growth: held symbols plus top N by liquidity.
MARKETDATA_DEEP_TIER_N = int(os.getenv("MARKETDATA_DEEP_TIER_N", "100"))
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
CODAL_ENABLED = os.getenv("CODAL_ENABLED", "1") == "1"
# Deployment can retain Codal routing and queued jobs while scaling its paid
# extraction consumer to zero during the broker handoff.
CODAL_WORKER_ENABLED = os.getenv("CODAL_WORKER_ENABLED", "1") == "1"
# Codal announcements are paged 20 per request and a mature symbol has ~50 pages,
# so "all history for all symbols" is ~32,000 requests -- more than three days of
# the whole archive budget. Only page 1 was ever fetched, which stored 2% and
# still reported verified. Bound the target to the newest N pages per symbol so
# the state can honestly converge. Every refresh rereads all N pages to verify
# that coverage; raising N increases both backfill and weekly refresh cost.
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
#
# Falls back to IRAN_EGRESS_PROXY because codal.ir and tsetmc.com are blocked by
# the same mechanism from the same networks, so one Iranian hop fixes both. The
# Codal-specific name stays first for deployments that already set it and for
# the case where Codal needs a different path than the market feeds.
CODAL_HTTP_PROXY = os.getenv("CODAL_HTTP_PROXY", "") or IRAN_EGRESS_PROXY
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
CODAL_PARSER_VERSION = os.getenv("CODAL_PARSER_VERSION", "4")
CODAL_STATEMENT_PARSER_VERSION = os.getenv("CODAL_STATEMENT_PARSER_VERSION", "5")

# 14, not 30. At 30 the nightly prune had never deleted a row -- the ledger was
# only 20 days old -- while the table grew to 877 MB on 845k rows, because
# volume per day rose roughly tenfold once bulk backfill started. Two weeks is
# still far more than any operator question needs, and it is the one signal that
# has no other copy, so it is trimmed by retention rather than by hand.
WORKFLOW_RETENTION_DAYS = int(os.getenv("WORKFLOW_RETENTION_DAYS", "14"))

# Live poll cadence by market state (seconds). Beat still ticks every minute; the
# task itself decides whether enough time has passed, so the cadence can change
# without a beat restart. See marketdata/market_state.py for the arithmetic.
#
# The daytime interval MUST stay below `valuation._FRESH_SECONDS` (300). At 300
# it equalled the freshness bar, so a price aged past "fresh" at the same moment
# its replacement became due and every held asset oscillated between Fresh and
# Stale all day -- measured at 244s and 568s minutes apart, with the console's
# headline freshness never settling.
#
# Tightened 2026-09-04 (120/240/240 -> 60/90/180) after eight days of measured
# spend showed both meters far under-used. Cost, per trading day:
#
#   Market/* (1,500/day meter)  gold/FX/crypto, one request per cycle
#     open      4.5h / 60s  = 270
#     daytime  11.5h / 90s  = 460      (07:00-08:30 and 13:00-23:00)
#     overnight 8.0h / 180s = 160      (newly polled at all -- see below)
#     + commodity snapshot beat (900s)  = 96
#     ~= 990 provider calls. TGJU covers the mapped board directly and
#     `fetch_all_markets` can skip the reseller call when it is complete.
#
#   Tsetmc/* (10,000/day meter)  one AllSymbols request per cycle, session only
#     open      4.5h / 60s  = 270  + ~9 state probes + ~54 option/IME snapshots
#     ~= 335, against a 1,700 live budget. The archive keeps ~9,500.
#
# Overnight is no longer a blackout. It was 240s but `live_job_keys` gated the
# gold/currency job to OPEN/CLOSED_DAYTIME, so 23:00-07:00 fetched nothing at
# all: eight hours with no crypto or FX price, on markets that trade around the
# clock. The 1,500/day Market allowance accommodates this overnight cadence,
# subject to the live budget and provider-observed meter.
MARKETDATA_LIVE_INTERVAL_OPEN = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OPEN", "20"))
MARKETDATA_LIVE_INTERVAL_DAYTIME = int(os.getenv("MARKETDATA_LIVE_INTERVAL_DAYTIME", "90"))
MARKETDATA_LIVE_INTERVAL_OVERNIGHT = int(os.getenv("MARKETDATA_LIVE_INTERVAL_OVERNIGHT", "180"))

# How often the provider gold/FX board is fetched even when direct origins already
# cover every symbol the app prices. Normally `fetch_all_markets` skips BrsApi
# whenever TGJU's board is complete, which is correct and is most of why the
# Market/* call volume stayed low -- but it means a TGJU slug that goes stale while
# still answering (a known failure mode of that feed) would never be contradicted
# by anything. This is the second opinion: one provider board every N seconds,
# ~96/day at 900s.
# 0 disables it and restores the old always-skip behaviour.
MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS = int(
    os.getenv("MARKETDATA_BRS_VERIFY_INTERVAL_SECONDS", "900")
)
# Buy the provider gold/FX board on EVERY cycle rather than only as a fallback,
# and let the extractor blend it with the direct origins. Costs ~890 metered
# Market requests on a trading day, and
# BrsApi declares a unit string per row where TGJU needs slug mapping.
MARKETDATA_BLEND_PAID_BOARD = os.getenv("MARKETDATA_BLEND_PAID_BOARD", "1") == "1"
# The live loop's USDT/IRT fallback quote (`Gold_Currency_Pro.php?history=1`)
# calls the Market/* product once per cycle. At the tightened cadence that is ~900
# requests/day for a number the main board already carries and the warehouse
# overlay can supply -- it exists only for when the board echoes the USD peg.
# Cached for this long, so it costs ~144 calls/day rather than ~900.
MARKETDATA_USDT_QUOTE_TTL_SECONDS = int(
    os.getenv("MARKETDATA_USDT_QUOTE_TTL_SECONDS", "600")
)

MARKETDATA_QUOTA_TIMEZONE = os.getenv("MARKETDATA_QUOTA_TIMEZONE", "Asia/Tehran")
MARKETDATA_IGNORE_MARKET_HOURS = os.getenv("MARKETDATA_IGNORE_MARKET_HOURS", "False").lower() in ("true", "1")
MARKETDATA_REQUIRE_SHARED_WINDOW = os.getenv(
    "MARKETDATA_REQUIRE_SHARED_WINDOW", "True"
).lower() in ("true", "1")
# Extra TSE symbols to sync beyond assets with a tse_symbol (comma-separated).
MARKETDATA_EXTRA_SYMBOLS = [
    s.strip() for s in os.getenv("MARKETDATA_EXTRA_SYMBOLS", "").split(",") if s.strip()
]
# TSETMC_HISTORY_URL was removed here: nothing read it. Every provider URL is
# declared once in marketdata/endpoints.py, which is what fetchers.py builds
# requests from, so this knob looked configurable and silently was not -- the
# worst kind of dead setting, because setting it in .env.production would have
# had no effect and given no warning. It was absent from the env example, from
# compose and from production, so removing it changes nothing.

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
# NOT `TIME_ZONE`. Django stores UTC, but every crontab in config/celery.py is
# declared in Tehran wall-clock time and says so. `config_from_object(...,
# namespace="CELERY")` resolves after `app.conf.update(timezone=...)`, so this
# name -- not the one in celery.py -- is what beat actually runs on, and setting
# it to UTC fired every job 3.5h off its own comment: "Tehran midnight" integrity
# ran at 03:30, and the 23:59 daily price rollup ran at 03:29 the NEXT Tehran day,
# rolling up the wrong day's ticks. Verified in prod: app.conf.timezone == 'UTC'.
CELERY_TIMEZONE = "Asia/Tehran"
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True  # survive a broker restart

SENTRY_DSN = os.getenv("SENTRY_DSN", "")
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL", "")
# Second, independent alert channel. Both must be set for it to be used; either
# channel alone is enough to stop `notify` logging `undelivered=1`.
ALERT_TELEGRAM_BOT_TOKEN = os.getenv("ALERT_TELEGRAM_BOT_TOKEN", "")
ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")
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
            f"or set CPI_ESTIMATED_ANNUAL_RATE to an estimated annual "
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
# Stated ANNUALLY, because that is the unit the figure is actually known in:
# nobody quotes Iranian inflation per month, and the two knobs that used to
# exist here disagreed by a factor of three without either of them looking
# wrong. The previous default was 6.5%/MONTH, which compounds to +112%/year --
# roughly triple both the verified series above (annual steps of 36.4 / 40.2 /
# 45.8 / 40.7 / 32.5 / 31.0 percent) and the CBI deposit rate this file already
# carries for the same year (RISK_FREE_RATE_BY_JALALI_YEAR[1405] = 0.38). At
# that pace one year of "vs inflation" halves a portfolio's real value on the
# chart no matter how it performed, which is what shipped.
#
# 37% is the middle of the 35-40% band the operator confirmed for 1405 and sits
# inside the range every published year has landed in. It remains a projection,
# not a release, and is kept out of CPI_VERIFIED_THROUGH_YEAR so nothing
# downstream can mistake it for one.
CPI_ESTIMATED_ANNUAL_RATE = float(os.environ.get("CPI_ESTIMATED_ANNUAL_RATE", "0.37"))
# The monthly form the anchors compound in. Still overridable on its own for an
# operator who has a monthly figure in hand; setting it wins over the annual one.
CPI_ESTIMATED_MONTHLY_RATE = float(os.environ.get(
    "CPI_ESTIMATED_MONTHLY_RATE",
    str((1.0 + CPI_ESTIMATED_ANNUAL_RATE) ** (1.0 / 12) - 1.0),
))
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

# The annual pace the projection actually runs at, derived from the monthly rate
# in force so a monthly override is reported honestly rather than as the annual
# default it overrode. This is the number the UI prints; nobody reads a monthly
# CPI step and knows what it means for a year of their net worth.
CPI_ESTIMATED_ANNUAL_RATE_EFFECTIVE = (
    (1.0 + CPI_ESTIMATED_MONTHLY_RATE) ** 12 - 1.0
    if CPI_ESTIMATED_MONTHLY_RATE > 0
    else 0.0
)
CPI_SOURCE = (
    f"Statistical Center of Iran annual CPI releases, verified through "
    f"{CPI_VERIFIED_THROUGH_YEAR}"
    + (
        f"; {'/'.join(str(year) for year in sorted(CPI_ESTIMATED_YEARS))} are an "
        f"OPERATOR ESTIMATE at {CPI_ESTIMATED_ANNUAL_RATE_EFFECTIVE:.0%}/year, "
        f"not a published figure"
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
