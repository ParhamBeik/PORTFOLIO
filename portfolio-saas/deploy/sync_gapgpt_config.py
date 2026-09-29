#!/usr/bin/env python3
"""Project only GapGPT settings from News into a portfolio-readable secret.

Run on the VPS before deploying the portfolio backend and after News settings
change. The News .env remains the source of truth; no key is printed or copied
into the repository. A missing price falls back to the deployed News code's own
default, failing closed if that definition cannot be found.
"""

import argparse
import os
import re
import shlex
import tempfile
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

KEYS = (
    "GAPGPT_API_KEY", "GAPGPT_BASE_URL", "GAPGPT_MODEL",
    "GAPGPT_INPUT_USD_PER_MILLION", "GAPGPT_OUTPUT_USD_PER_MILLION",
)


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        if key in KEYS:
            parts = shlex.split(raw, comments=True)
            if len(parts) != 1:
                raise ValueError(f"invalid {key} in News settings")
            values[key] = parts[0]
    return values


def news_default(settings_text, key):
    match = re.search(rf'{key}\s*=\s*env_float\("{key}",\s*([0-9.]+)\)', settings_text)
    if match is None:
        raise ValueError(f"News does not define a readable {key} default")
    return match.group(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/opt/apps/news-intel/deploy/.env"))
    parser.add_argument("--news-settings", type=Path, default=Path("/opt/apps/news-intel/backend/config/settings/base.py"))
    parser.add_argument("--destination", type=Path, default=Path("/opt/apps/portfolio-secrets/gapgpt.env"))
    args = parser.parse_args()
    values = read_env(args.source)
    if not values.get("GAPGPT_API_KEY") or not values.get("GAPGPT_MODEL"):
        raise ValueError("News has no configured GapGPT key or model")
    url = urlparse(values.get("GAPGPT_BASE_URL", ""))
    if url.scheme != "https" or url.hostname != "api.gapgpt.app" or url.path != "/v1":
        raise ValueError("News GapGPT URL is not the approved HTTPS endpoint")
    defaults = args.news_settings.read_text(encoding="utf-8")
    for key in ("GAPGPT_INPUT_USD_PER_MILLION", "GAPGPT_OUTPUT_USD_PER_MILLION"):
        values.setdefault(key, news_default(defaults, key))
        price = Decimal(values[key])
        if not price.is_finite() or price <= 0:
            raise ValueError(f"invalid {key}")
    if os.geteuid() != 0:
        raise PermissionError("Run as root so the projected file can be owned by container UID 1000")
    args.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=args.destination.parent,
        prefix=".gapgpt-", delete=False,
    ) as handle:
        temporary = Path(handle.name)
        for key in KEYS:
            handle.write(f"{key}={shlex.quote(values[key])}\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chown(temporary, 1000, 0)
        os.chmod(temporary, 0o600)
        os.replace(temporary, args.destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Projected {len(KEYS)} GapGPT settings to {args.destination}; no secret value displayed")


if __name__ == "__main__":
    main()
