#!/usr/bin/env bash
# Restore on an off-host PostgreSQL/Timescale instance, never on the small VPS.
# The dump is streamed so it does not need a second decrypted copy on disk.
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
backend_dir="${project_dir}/backend"
backup_dir="${BACKUP_DIR:-/var/backups/portfolio}"
target_db="${RESTORE_TARGET_DB:-portfolio_real_restore_drill_$$}"
evidence="${RESTORE_EVIDENCE:-${backup_dir}/restore-drill-evidence-latest.json}"
passphrase_file="${BACKUP_PASSPHRASE_FILE:-}"
: "${TEST_PG_HOST:?set TEST_PG_HOST to an isolated PostgreSQL host or Unix socket directory}"
: "${RESTORE_TIMESCALE_VERSION:?set RESTORE_TIMESCALE_VERSION to the source database extension version}"
[[ "${RESTORE_TIMESCALE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  printf 'Invalid TimescaleDB extension version\n' >&2
  exit 1
}
[[ "${target_db}" =~ ^[a-z][a-z0-9_]*$ && "${target_db}" == *restore* ]] || {
  printf 'Invalid restore test database name\n' >&2
  exit 1
}

export PGPASSWORD="${TEST_PG_PASSWORD:-}"
pg=(--host "${TEST_PG_HOST}" --port "${TEST_PG_PORT:-5432}" --username "${TEST_PG_USER:-portfolio}")

latest_enc="$(find "${backup_dir}" -maxdepth 1 -type f -name 'daily-*.dump.enc' 2>/dev/null | sort -r | head -1 || true)"
latest_raw="$(find "${backup_dir}" -maxdepth 1 -type f -name 'daily-*.dump' 2>/dev/null | sort -r | head -1 || true)"

created=false
cleanup() {
  if [[ "${created}" == true ]]; then
    dropdb "${pg[@]}" --if-exists "${target_db}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [[ -n "${latest_enc}" ]]; then
  if [[ -z "${passphrase_file}" || ! -r "${passphrase_file}" ]]; then
    echo "Encrypted backup found (${latest_enc}) but BACKUP_PASSPHRASE_FILE is missing or unreadable." >&2
    exit 1
  fi
  source_artifact="${latest_enc}"
elif [[ -n "${latest_raw}" ]]; then
  source_artifact="${latest_raw}"
else
  echo "No production dump found in ${backup_dir}" >&2
  exit 1
fi

archive_stream() {
  if [[ -n "${latest_enc}" ]]; then
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 310000 \
      -pass "file:${passphrase_file}" -in "${latest_enc}"
  else
    cat "${latest_raw}"
  fi
}

if [[ "$(psql "${pg[@]}" --dbname postgres -At -v ON_ERROR_STOP=1 \
  -c "SELECT 1 FROM pg_database WHERE datname = '${target_db}'")" == 1 ]]; then
  printf 'Refusing to replace existing database %s\n' "${target_db}" >&2
  exit 1
fi
createdb "${pg[@]}" "${target_db}"
created=true
psql "${pg[@]}" --dbname "${target_db}" -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION timescaledb VERSION '${RESTORE_TIMESCALE_VERSION}';"
psql "${pg[@]}" --dbname "${target_db}" -v ON_ERROR_STOP=1 \
  -c 'SELECT timescaledb_pre_restore();'

echo "Restoring backup into ${target_db}..."
archive_stream | pg_restore "${pg[@]}" --exit-on-error --no-owner --no-acl \
  --dbname "${target_db}"
psql "${pg[@]}" --dbname "${target_db}" -v ON_ERROR_STOP=1 \
  -c 'SELECT timescaledb_post_restore();'

python_bin="python3"
if [[ -x "${project_dir}/.venv/bin/python3" ]]; then
  python_bin="${project_dir}/.venv/bin/python3"
elif [[ -x "${backend_dir}/../.venv/bin/python3" ]]; then
  python_bin="${backend_dir}/../.venv/bin/python3"
fi

manage() {
  DJANGO_SETTINGS_MODULE=config.settings \
  DJANGO_DEBUG=0 \
  DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-restore-drill-production-verification-secret-key-at-least-50-characters-long}" \
  ENVIRONMENT=production \
  POSTGRES_DB="$1" \
  POSTGRES_USER="${TEST_PG_USER:-portfolio}" \
  POSTGRES_PASSWORD="${TEST_PG_PASSWORD:-}" \
  POSTGRES_HOST="${TEST_PG_HOST}" \
  POSTGRES_PORT="${TEST_PG_PORT:-5432}" \
    "${python_bin}" "${backend_dir}/manage.py" "${@:2}"
}

echo "Asserting data integrity..."
target_evidence="$(manage "${target_db}" restore_drill_evidence)"

python3 - "${target_evidence}" "${evidence}" "$(basename "${source_artifact}")" <<'PY'
import json
import sys
from datetime import datetime, timezone

data = json.loads(sys.argv[1])
evidence_path = sys.argv[2]
source_artifact = sys.argv[3]

payload = {
    "status": "passed",
    "verified_at": datetime.now(timezone.utc).isoformat(),
    "source_artifact": source_artifact,
    **data,
}

with open(evidence_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2, sort_keys=True)

print(f"Restore drill succeeded: {payload['counts']}")
PY
