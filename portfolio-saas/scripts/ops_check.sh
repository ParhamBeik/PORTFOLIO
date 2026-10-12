#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
disk_limit="${DISK_USAGE_THRESHOLD_PERCENT:-85}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")
failures=()

disk_used="$(df -P "${project_dir}" | awk 'NR==2 {gsub("%","",$5); print $5}')"
((disk_used < disk_limit)) || failures+=("disk usage ${disk_used}%")

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
  # host_alert.py reads the ALERT_* keys from the env file. This used to curl
  # "${ALERT_WEBHOOK_URL:-}" straight from its own environment, which cron
  # leaves empty, so the hourly check failed silently on the box it guards.
  detail="$(IFS=';'; printf '%s' "${failures[*]}")"
  python3 "${project_dir}/scripts/host_alert.py" --env-file "${env_file}" \
    --compose-file "${project_dir}/docker-compose.prod.yml" host-ops-check "${detail}" || true
  printf '%s\n' "${failures[@]}" >&2
  exit 1
fi
