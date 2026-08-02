#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
backup_dir="${BACKUP_DIR:-/var/backups/portfolio}"
disk_limit="${DISK_USAGE_THRESHOLD_PERCENT:-85}"
backup_age_hours="${BACKUP_FRESHNESS_THRESHOLD_HOURS:-26}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")
failures=()

disk_used="$(df -P "${project_dir}" | awk 'NR==2 {gsub("%","",$5); print $5}')"
((disk_used < disk_limit)) || failures+=("disk usage ${disk_used}%")

latest="$(find "${backup_dir}" -type f -name 'daily-*.dump.enc' -print0 2>/dev/null | xargs -0 ls -1t 2>/dev/null | head -1 || true)"
if [[ -z "${latest}" ]] || (( $(date +%s) - $(stat -f %m "${latest}" 2>/dev/null || stat -c %Y "${latest}") > backup_age_hours * 3600 )); then
  failures+=("backup older than ${backup_age_hours}h")
fi

"${compose[@]}" ps --status running >/dev/null || failures+=("container health check failed")

if ((${#failures[@]})); then
  payload="$(printf '%s\n' "${failures[@]}" | python3 -c 'import json,sys; print(json.dumps({"event":"host-ops-check","details":{"failures":[line.strip() for line in sys.stdin if line.strip()]}}))')"
  [[ -z "${ALERT_WEBHOOK_URL:-}" ]] || curl -fsS -m 5 -H 'Content-Type: application/json' -d "${payload}" "${ALERT_WEBHOOK_URL}" >/dev/null || true
  printf '%s\n' "${failures[@]}" >&2
  exit 1
fi
