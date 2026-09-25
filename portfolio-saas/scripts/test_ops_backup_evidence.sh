#!/usr/bin/env bash
# The hourly check must not call a same-host dump a recoverable backup.
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
mkdir -p "${scratch}/bin" "${scratch}/backups"
touch "${scratch}/env" "${scratch}/backups/daily-2026-09-25.dump.enc"
cat > "${scratch}/bin/docker" <<'SH'
#!/usr/bin/env bash
if [[ " $* " == *" ps --status running --services "* ]]; then
  printf '%s\n' db redis minio backend celery_worker_live celery_worker_archive celery_beat frontend
fi
SH
chmod +x "${scratch}/bin/docker"

run_check() {
  PATH="${scratch}/bin:${PATH}" PROJECT_DIR="${project_dir}" ENV_FILE="${scratch}/env" \
    BACKUP_DIR="${scratch}/backups" DISK_USAGE_THRESHOLD_PERCENT=100 \
    "${project_dir}/scripts/ops_check.sh"
}

cat > "${scratch}/backups/backup-evidence-2026-09-25.json" <<'JSON'
{"database_artifact":"daily-2026-09-25.dump.enc","off_host_verified":false}
JSON
if run_check 2>"${scratch}/error"; then
  echo "Ops check accepted a local-only backup" >&2
  exit 1
fi
grep -q 'no verified off-host copy' "${scratch}/error"

cat > "${scratch}/backups/backup-evidence-2026-09-25.json" <<'JSON'
{"database_artifact":"daily-2026-09-25.dump.enc","off_host_verified":true}
JSON
run_check
