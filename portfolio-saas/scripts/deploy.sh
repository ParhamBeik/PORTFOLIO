#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")

[[ -r "${env_file}" ]] || { echo "Create ${env_file} from .env.production.example" >&2; exit 1; }
[[ "$(stat -c '%a' "${env_file}")" =~ ^[46]00$ ]] || { echo "${env_file} must have mode 400 or 600" >&2; exit 1; }
docker network inspect vps-edge >/dev/null 2>&1 || { echo "Deploy deploy/edge first; Docker network vps-edge is missing" >&2; exit 1; }
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
curl -fsS "https://${domain}/api/health/"
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/prices/"
