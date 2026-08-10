#!/usr/bin/env bash
set -Eeuo pipefail

edge_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
env_file="${ENV_FILE:-${edge_dir}/.env}"
compose=(docker compose -p vps-edge -f "${edge_dir}/docker-compose.yml" --env-file "${env_file}")

[[ -r "${env_file}" ]] || { echo "Create ${env_file} from .env.example" >&2; exit 1; }
[[ "$(stat -c '%a' "${env_file}")" =~ ^[46]00$ ]] || { echo "${env_file} must have mode 400 or 600" >&2; exit 1; }
"${compose[@]}" config --quiet
"${compose[@]}" run --rm --no-deps edge caddy validate --config /etc/caddy/Caddyfile
"${compose[@]}" up -d
