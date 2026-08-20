#!/usr/bin/env bash
# Restarts the live-price Celery lane when /api/health/prices/ reports stale
# (503) -- the safety net README.md already documents ("an on-VPS cron
# restarts Celery on failure") but that never actually existed on the VPS,
# which is how a 41-hour live-price outage on 2026-08-18/20 went unnoticed.
#
# ponytail: this only recovers a hung/crashed Celery worker (dead process,
# stuck task holding a worker slot forever). It does NOT detect or clear a
# Postgres-side lock (e.g. a stuck TimescaleDB compression job) blocking the
# query the worker is waiting on -- restarting Celery only drops that
# worker's DB connection, it doesn't touch the lock holder. That was the
# actual 2026-08-20 root cause and needed a manual `pg_terminate_backend`.
# Upgrade path: add a pg_stat_activity check for locks held longer than N
# minutes and terminate the blocking backend before restarting Celery.
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")
domain="$(awk -F= '$1=="PORTFOLIO_DOMAIN"{print $2; exit}' "${env_file}")"
cooldown_file="/var/run/portfolio-watchdog.last_restart"
cooldown_seconds="${WATCHDOG_COOLDOWN_SECONDS:-900}"

log() { echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') watchdog_prices: $*"; }

if curl -fsS --max-time 10 "https://${domain}/api/health/prices/" >/dev/null 2>&1; then
    exit 0
fi

log "prices health check failed (stale or unreachable)"

now="$(date +%s)"
last="$(cat "${cooldown_file}" 2>/dev/null || echo 0)"
if (( now - last < cooldown_seconds )); then
    log "restarted ${cooldown_seconds}s ago or less, skipping to avoid restart-looping"
    exit 0
fi

log "restarting celery_worker_live and celery_beat"
"${compose[@]}" restart celery_worker_live celery_beat
echo "${now}" > "${cooldown_file}"
log "restart issued"
