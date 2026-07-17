"""Test settings: local Postgres, no Docker or Redis required.

Inherits production config, then points the database at a local Postgres the test
runner creates/drops on each run and swaps Redis for an in-process cache. MD5
password hashing keeps user-heavy tests fast. This lets the suite run anywhere
with a local postgres, which matters because `get_latest_prices` relies on the
Postgres-only DISTINCT ON clause and cannot be exercised under sqlite.
"""
import os

from .settings import *  # noqa: F401,F403

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

DEBUG = False
