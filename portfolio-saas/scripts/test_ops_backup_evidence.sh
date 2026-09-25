#!/usr/bin/env bash
# The hourly check must not call a same-host dump a recoverable backup.
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
mkdir -p "${scratch}/bin" "${scratch}/backups"
stamp="$(date +%F)"
artifact="daily-${stamp}.dump.enc"
touch "${scratch}/env" "${scratch}/backups/${artifact}"
digest="$(printf 'a%.0s' {1..64})"
printf '%s  %s\n' "${digest}" "${artifact}" > "${scratch}/backups/${artifact}.sha256"
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

evidence="${scratch}/backups/backup-evidence-${stamp}.json"
printf '{"database_artifact":"%s","database_sha256":"%s","decrypt_verified":true,"off_host_verified":false}\n' \
  "${artifact}" "${digest}" > "${evidence}"
if run_check 2>"${scratch}/error"; then
  echo "Ops check accepted a local-only backup" >&2
  exit 1
fi
grep -q 'no verified off-host copy' "${scratch}/error"

printf '{"database_artifact":"%s","database_sha256":"%s","decrypt_verified":true,"off_host_verified":true}\n' \
  "${artifact}" "${digest}" > "${evidence}"
run_check

printf '{"database_artifact":"%s","database_sha256":"bad","decrypt_verified":true,"off_host_verified":true}\n' \
  "${artifact}" > "${evidence}"
if run_check 2>"${scratch}/error"; then
  echo "Ops check accepted a receipt for the wrong bytes" >&2
  exit 1
fi
grep -q 'no verified off-host copy' "${scratch}/error"
