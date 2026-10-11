#!/usr/bin/env python3
"""Send one operator alert from the VPS host, through the app's own channels.

The host cron scripts (ops_check.sh, watchdog_prices.sh) run outside every
container, exactly when the app may be down, so they cannot call
`config.observability.notify`. They used to have no working channel at all:
ops_check.sh read ALERT_WEBHOOK_URL from cron's empty environment and the
watchdog only wrote a log line, so a 3am worker restart reached nobody.

This reads the same ALERT_* keys from the production env file (an exported
variable wins, for tests and manual runs), posts to the webhook and the
Telegram-compatible bot exactly as `notify()` does, and pushes the same record
shape onto the console's "Recent alerts" list with redis-cli inside the redis
container. Every step is best-effort and independent: one failing channel must
not suppress another, and nothing here may fail the calling script.

Stdlib only -- it runs on the bare host.

Usage: host_alert.py --env-file F --compose-file F EVENT DETAIL
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

KEYS = ("ALERT_WEBHOOK_URL", "ALERT_TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID", "ALERT_TELEGRAM_API_BASE")
# Mirrors config/observability.py: RECENT_ALERTS_KEY, RECENT_ALERTS_MAX, and the
# cache instance's DB (REDIS_URL redis://redis:6379/1 in docker-compose.prod.yml).
RECENT_ALERTS_KEY = "ops:recent_alerts"
RECENT_ALERTS_MAX = 50
REDIS_DB = "1"
DEFAULT_API_BASE = "https://tapi.bale.ai"


def read_env_file(path):
    """KEY=value lines, value verbatim after the first '=' (URLs carry '=')."""
    values = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    except OSError:
        pass
    return values


def alert_config(env_file, environ=None):
    environ = os.environ if environ is None else environ
    from_file = read_env_file(env_file)
    return {key: environ.get(key) or from_file.get(key, "") for key in KEYS}


def _post_json(url, body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status >= 400:
            raise OSError(f"HTTP {response.status}")


def send(config, event, details):
    """Returns (delivered, configured). Never raises."""
    delivered = False
    url = config["ALERT_WEBHOOK_URL"]
    token, chat_id = config["ALERT_TELEGRAM_BOT_TOKEN"], config["ALERT_TELEGRAM_CHAT_ID"]
    if url:
        try:
            _post_json(url, {"event": event, "request_id": "host", "details": details})
            delivered = True
        except Exception as exc:
            print(f"host_alert: webhook failed for {event}: {type(exc).__name__}", file=sys.stderr)
    if token and chat_id:
        base = (config["ALERT_TELEGRAM_API_BASE"] or DEFAULT_API_BASE).rstrip("/")
        body = json.dumps(details, sort_keys=True, ensure_ascii=False)
        try:
            _post_json(
                f"{base}/bot{token}/sendMessage",
                {"chat_id": chat_id, "text": f"[{event}] host\n{body}"[:4096], "disable_web_page_preview": True},
            )
            delivered = True
        except Exception as exc:
            # The exception text can carry the URL, and the URL carries the token.
            print(f"host_alert: telegram failed for {event}: {type(exc).__name__}", file=sys.stderr)
    return delivered, bool(url or (token and chat_id))


def record(compose_file, env_file, event, details, delivered, run=subprocess.run):
    entry = json.dumps(
        {
            "at": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "source": "host",
            "delivered": delivered,
            "details": json.dumps(details, sort_keys=True, ensure_ascii=False)[:500],
        },
        ensure_ascii=False,
    )
    base = ["docker", "compose", "-f", compose_file, "--env-file", env_file, "exec", "-T", "redis", "redis-cli", "-n", REDIS_DB]
    try:
        run(base + ["-x", "LPUSH", RECENT_ALERTS_KEY], input=entry.encode("utf-8"), timeout=15, check=True, capture_output=True)
        run(base + ["LTRIM", RECENT_ALERTS_KEY, "0", str(RECENT_ALERTS_MAX - 1)], timeout=15, check=True, capture_output=True)
        return True
    except Exception as exc:
        print(f"host_alert: could not record {event} for the console: {type(exc).__name__}", file=sys.stderr)
        return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--compose-file", required=True)
    parser.add_argument("event")
    parser.add_argument("detail")
    args = parser.parse_args(argv)
    details = {"host": socket.gethostname(), "detail": args.detail}
    delivered, configured = send(alert_config(args.env_file), args.event, details)
    record(args.compose_file, args.env_file, args.event, details, delivered)
    state = "delivered" if delivered else ("undelivered" if configured else "undelivered (no channel configured)")
    print(f"host_alert: {args.event} {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
