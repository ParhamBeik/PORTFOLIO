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

# A fresh dump on the database host is not a recoverable backup after host
# loss. A VPS upload or the Mac pull records true only after checking off-host
# bytes; require the receipt to match the latest artifact and checksum.
if [[ -n "${latest}" ]]; then
  stamp="$(basename "${latest}" | sed -n 's/^daily-\(.*\)\.dump\.enc$/\1/p')"
  evidence="${backup_dir}/backup-evidence-${stamp}.json"
  if [[ ! -r "${evidence}" || ! -r "${latest}.sha256" ]] || ! python3 - "${evidence}" "$(basename "${latest}")" "${latest}.sha256" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as source:
        record = json.load(source)
    with open(sys.argv[3], encoding="utf-8") as source:
        digest, filename = source.read().split()
    valid = (
        bool(re.fullmatch(r"[0-9a-f]{64}", digest))
        and filename == sys.argv[2]
        and record.get("database_artifact") == sys.argv[2]
        and record.get("database_sha256") == digest
        and record.get("decrypt_verified") is True
        and record.get("off_host_verified") is True
    )
except (OSError, ValueError):
    valid = False
sys.exit(0 if valid else 1)
PY
  then
    failures+=("latest database backup has no verified off-host copy")
  fi
fi

# Every long-running service in docker-compose.prod.yml, i.e. the ones carrying
# `restart: unless-stopped`. `migrate` is deliberately absent: it is `restart:
# "no"` and is SUPPOSED to have exited by the time this runs.
#
# The line here used to be `"${compose[@]}" ps --status running >/dev/null ||
# failures+=(...)`, which can never fail. `docker compose ps` exits 0 whether it
# lists eight containers or none -- an empty result set is not an error to it --
# so the `||` branch was unreachable and this hourly check reported a completely
# dead stack as healthy. Verified against docker compose v29.7.2: a project with
# zero running containers still exits 0.
#
# Asking for the names and looking for each one turns that into a real
# assertion, and names the service that is actually down instead of saying
# "container health check failed" about all eight.
expected_services=(db redis broker minio backend celery_worker_live celery_beat frontend)
archive_worker_enabled="$(awk -F= '$1=="ARCHIVE_WORKER_ENABLED"{print $2; exit}' "${env_file}")"
codal_worker_enabled="$(awk -F= '$1=="CODAL_WORKER_ENABLED"{print $2; exit}' "${env_file}")"
if [[ "${archive_worker_enabled:-1}" == "1" ]]; then
  expected_services+=(celery_worker_archive)
fi
if [[ "${codal_worker_enabled:-1}" == "1" ]]; then
  expected_services+=(celery_worker_codal)
fi
running_services="$("${compose[@]}" ps --status running --services 2>/dev/null || true)"
for service in "${expected_services[@]}"; do
  grep -qx -- "${service}" <<<"${running_services}" || failures+=("service ${service} is not running")
done

if ((${#failures[@]})); then
  payload="$(printf '%s\n' "${failures[@]}" | python3 -c 'import json,sys; print(json.dumps({"event":"host-ops-check","details":{"failures":[line.strip() for line in sys.stdin if line.strip()]}}))')"
  [[ -z "${ALERT_WEBHOOK_URL:-}" ]] || curl -fsS -m 5 -H 'Content-Type: application/json' -d "${payload}" "${ALERT_WEBHOOK_URL}" >/dev/null || true
  printf '%s\n' "${failures[@]}" >&2
  exit 1
fi
