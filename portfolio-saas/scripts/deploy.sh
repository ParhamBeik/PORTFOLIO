#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")

[[ -r "${env_file}" ]] || { echo "Create ${env_file} from .env.production.example" >&2; exit 1; }
[[ "$(stat -c '%a' "${env_file}")" =~ ^[46]00$ ]] || { echo "${env_file} must have mode 400 or 600" >&2; exit 1; }
docker network inspect vps-edge >/dev/null 2>&1 || { echo "Docker network vps-edge is missing; the VPS reverse proxy at /opt/apps/vps-edge must already be running" >&2; exit 1; }
domain="$(awk -F= '$1=="PORTFOLIO_DOMAIN"{print $2; exit}' "${env_file}")"
[[ -n "${domain}" ]] || { echo "PORTFOLIO_DOMAIN is missing from ${env_file}" >&2; exit 1; }

if [[ -n "$("${compose[@]}" ps -q db)" ]] && "${compose[@]}" exec -T db pg_isready >/dev/null 2>&1; then
  "${project_dir}/scripts/backup_postgres.sh"
fi
"${compose[@]}" build
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" exec -T backend python manage.py check --deploy --fail-level WARNING
"${compose[@]}" exec -T backend python manage.py migrate --check
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/ready/"
"${compose[@]}" exec -T celery_worker_live celery -A config inspect ping
"${compose[@]}" exec -T celery_worker_archive celery -A config inspect ping

# Beat is a scheduler, not a worker: `celery inspect ping` does not answer for
# it, so it was the one service this script never verified -- and it is the one
# whose failure is invisible. On 2026-09-05 a Dockerfile change left /app owned
# by root, beat could not create its `celerybeat-schedule`, and it crash-looped
# from the first second. The backend, both workers and the frontend only ever
# READ from /app, so all four reported healthy, every check below passed, and
# the deploy went green while the price loop, the archive tick and every
# nightly job were stopped. Prices went stale for the twelve minutes it took a
# human to notice.
#
# Uptime, not liveness. A crash-looping container is genuinely "running" for
# part of each cycle, so a single point-in-time `ps` can sample it mid-cycle and
# pass. Requiring it to have survived a restart interval is what makes this a
# real assertion.
beat_cid="$("${compose[@]}" ps -q celery_beat)"
[[ -n "${beat_cid}" ]] || { echo "celery_beat has no container; the scheduler is not deployed" >&2; exit 1; }
sleep 45
beat_running="$(docker inspect -f '{{.State.Running}}' "${beat_cid}")"
beat_started="$(docker inspect -f '{{.State.StartedAt}}' "${beat_cid}")"
beat_uptime=$(( $(date -u +%s) - $(date -u -d "${beat_started}" +%s) ))
if [[ "${beat_running}" != "true" || "${beat_uptime}" -lt 30 ]]; then
  echo "celery_beat is not stable (running=${beat_running}, uptime=${beat_uptime}s): the scheduler is down, so nothing is on a schedule." >&2
  "${compose[@]}" logs --tail 30 celery_beat >&2
  exit 1
fi

curl -fsS "https://${domain}/api/health/"
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/prices/"
