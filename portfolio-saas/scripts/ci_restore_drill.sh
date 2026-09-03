#!/usr/bin/env bash
set -Eeuo pipefail

backend_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../backend" && pwd)"
work_dir="${RESTORE_WORK_DIR:-$PWD}"
source_db="${RESTORE_SOURCE_DB:-portfolio_restore_source}"
target_db="${RESTORE_TARGET_DB:-portfolio_restore_target}"
artifact="${RESTORE_ARTIFACT:-${work_dir}/restore-drill.dump}"
source_evidence="${work_dir}/source-evidence.json"
target_evidence="${work_dir}/target-evidence.json"
evidence="${RESTORE_EVIDENCE:-${work_dir}/restore-evidence.json}"
export PGPASSWORD="${TEST_PG_PASSWORD:-}"
pg=(--host "${TEST_PG_HOST:-127.0.0.1}" --port "${TEST_PG_PORT:-5432}" --username "${TEST_PG_USER:-portfolio}")
mkdir -p "${work_dir}"

manage() {
  DJANGO_SETTINGS_MODULE=config.settings \
  DJANGO_DEBUG=1 \
  ENVIRONMENT=dev \
  POSTGRES_DB="$1" \
  POSTGRES_USER="${TEST_PG_USER:-portfolio}" \
  POSTGRES_PASSWORD="${TEST_PG_PASSWORD:-}" \
  POSTGRES_HOST="${TEST_PG_HOST:-127.0.0.1}" \
  POSTGRES_PORT="${TEST_PG_PORT:-5432}" \
    python "${backend_dir}/manage.py" "${@:2}"
}

cleanup() {
  dropdb "${pg[@]}" --if-exists "${source_db}" >/dev/null 2>&1 || true
  dropdb "${pg[@]}" --if-exists "${target_db}" >/dev/null 2>&1 || true
  rm -f "${artifact}" "${source_evidence}" "${target_evidence}" "${evidence}"
}
trap cleanup EXIT
cleanup
createdb "${pg[@]}" "${source_db}"

manage "${source_db}" migrate --noinput >/dev/null
manage "${source_db}" restore_drill_evidence --seed > "${source_evidence}"
pg_dump "${pg[@]}" --format=custom --no-owner --no-acl --file "${artifact}" "${source_db}"
pg_restore --list "${artifact}" >/dev/null

createdb "${pg[@]}" "${target_db}"
pg_restore "${pg[@]}" --no-owner --no-acl --dbname "${target_db}" "${artifact}"
manage "${target_db}" restore_drill_evidence > "${target_evidence}"
cmp "${source_evidence}" "${target_evidence}"

python - "${source_evidence}" "${evidence}" <<'PY'
import json
import sys
from datetime import datetime, timezone

source = json.load(open(sys.argv[1], encoding="utf-8"))
json.dump(
    {
        "status": "passed",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        **source,
    },
    open(sys.argv[2], "w", encoding="utf-8"),
    indent=2,
    sort_keys=True,
)
PY
