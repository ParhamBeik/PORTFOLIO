#!/usr/bin/env bash
# Restores the latest actual production backup into an isolated scratch database
# and asserts table integrity with `manage.py restore_drill_evidence`.
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
backend_dir="${project_dir}/backend"
backup_dir="${BACKUP_DIR:-/var/backups/portfolio}"
target_db="${RESTORE_TARGET_DB:-portfolio_real_restore_drill}"
evidence="${RESTORE_EVIDENCE:-${backup_dir}/restore-drill-evidence-latest.json}"
passphrase_file="${BACKUP_PASSPHRASE_FILE:-}"

export PGPASSWORD="${TEST_PG_PASSWORD:-}"
pg=(--host "${TEST_PG_HOST:-127.0.0.1}" --port "${TEST_PG_PORT:-5432}" --username "${TEST_PG_USER:-portfolio}")

latest_enc="$(find "${backup_dir}" -maxdepth 1 -type f -name 'daily-*.dump.enc' 2>/dev/null | sort -r | head -1 || true)"
latest_raw="$(find "${backup_dir}" -maxdepth 1 -type f -name 'daily-*.dump' 2>/dev/null | sort -r | head -1 || true)"

dump_file=""
temp_dump=""

cleanup() {
  if [[ -n "${temp_dump}" && -f "${temp_dump}" ]]; then
    rm -f "${temp_dump}"
  fi
  dropdb "${pg[@]}" --if-exists "${target_db}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ -n "${latest_enc}" ]]; then
  if [[ -z "${passphrase_file}" || ! -r "${passphrase_file}" ]]; then
    echo "Encrypted backup found (${latest_enc}) but BACKUP_PASSPHRASE_FILE is missing or unreadable." >&2
    exit 1
  fi
  temp_dump="$(mktemp "${backup_dir}/restore-drill-XXXXXX.dump")"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 310000 \
    -pass "file:${passphrase_file}" -in "${latest_enc}" -out "${temp_dump}"
  dump_file="${temp_dump}"
elif [[ -n "${latest_raw}" ]]; then
  dump_file="${latest_raw}"
else
  echo "No production dump found in ${backup_dir}" >&2
  exit 1
fi

echo "Verifying archive table of contents..."
pg_restore --list "${dump_file}" >/dev/null

cleanup
createdb "${pg[@]}" "${target_db}"

echo "Restoring backup into ${target_db}..."
pg_restore "${pg[@]}" --no-owner --no-acl --dbname "${target_db}" "${dump_file}"

manage() {
  DJANGO_SETTINGS_MODULE=config.settings \
  DJANGO_DEBUG=0 \
  ENVIRONMENT=production \
  POSTGRES_DB="$1" \
  POSTGRES_USER="${TEST_PG_USER:-portfolio}" \
  POSTGRES_PASSWORD="${TEST_PG_PASSWORD:-}" \
  POSTGRES_HOST="${TEST_PG_HOST:-127.0.0.1}" \
  POSTGRES_PORT="${TEST_PG_PORT:-5432}" \
    python3 "${backend_dir}/manage.py" "${@:2}"
}

echo "Asserting data integrity..."
target_evidence="$(manage "${target_db}" restore_drill_evidence)"

python3 - "${target_evidence}" "${evidence}" "$(basename "${latest_enc:-$latest_raw}")" <<'PY'
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

