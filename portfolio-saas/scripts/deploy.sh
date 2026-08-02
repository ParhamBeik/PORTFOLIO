#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")
domain="${DOMAIN:?set DOMAIN}"

"${project_dir}/scripts/backup_postgres.sh"
"${compose[@]}" build
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" exec -T backend python manage.py check --deploy --fail-level WARNING
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/ready/"
"${compose[@]}" exec -T celery_worker_live celery -A config inspect ping
"${compose[@]}" exec -T celery_worker_archive celery -A config inspect ping
curl -fsS "https://${domain}/api/health/"
curl -fsS "https://${domain}/api/health/prices/"
