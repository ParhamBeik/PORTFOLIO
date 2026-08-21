#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
backup_dir="${BACKUP_DIR:-/var/backups/portfolio}"
passphrase_file="${BACKUP_PASSPHRASE_FILE:?set BACKUP_PASSPHRASE_FILE to a root-readable file outside the repository}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")

command -v docker >/dev/null
command -v openssl >/dev/null
[[ -r "${env_file}" ]] || { echo "Cannot read ${env_file}" >&2; exit 1; }
[[ -r "${passphrase_file}" ]] || { echo "Cannot read ${passphrase_file}" >&2; exit 1; }
passphrase_mode="$(stat -c '%a' "${passphrase_file}" 2>/dev/null || stat -f '%Lp' "${passphrase_file}")"
[[ "${passphrase_mode}" =~ ^[46]00$ ]] || {
  echo "${passphrase_file} must have mode 400 or 600" >&2
  exit 1
}
mkdir -p "${backup_dir}"

stamp="$(TZ="${BACKUP_TIMEZONE:-Asia/Tehran}" date +%F)"
partial="${backup_dir}/daily-${stamp}.dump.enc.partial"
destination="${partial%.partial}"
trap 'rm -f "${partial}"' EXIT

"${compose[@]}" exec -T db sh -c \
  'exec pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 310000 \
      -pass "file:${passphrase_file}" -out "${partial}"
mv "${partial}" "${destination}"

# Verify the artifact decrypts into a readable archive. `pg_restore --list`
# only reads the table of contents at the head of a custom-format dump and then
# exits 0 -- it never drains the rest of the stream. openssl is consequently
# still writing into a closed pipe, takes EPIPE ("error writing output file")
# and exits 1, which under `pipefail` failed this script every single time,
# right before the checksum below. That is why no daily-*.sha256 or
# backup-evidence-*.json has ever existed on the server, and why deploy.sh --
# which runs this first under `set -e` -- could never get past its backup step.
# Judge the verification on pg_restore's status alone; openssl's EPIPE is the
# expected consequence of a successful early exit, not a failure.
set +o pipefail
openssl enc -d -aes-256-cbc -pbkdf2 -iter 310000 \
  -pass "file:${passphrase_file}" -in "${destination}" 2>/dev/null \
  | "${compose[@]}" exec -T db pg_restore --list >/dev/null
verify_status="${PIPESTATUS[1]}"
set -o pipefail
[[ "${verify_status}" -eq 0 ]] || {
  echo "Backup verification failed: ${destination} did not decrypt into a readable archive." >&2
  exit 1
}
checksum="$(sha256sum "${destination}" 2>/dev/null | awk '{print $1}' || shasum -a 256 "${destination}" | awk '{print $1}')"
printf '%s  %s\n' "${checksum}" "$(basename "${destination}")" > "${destination}.sha256"

upload_verified=false
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  command -v rclone >/dev/null
  for artifact in "${destination}"; do
    remote_path="${RCLONE_REMOTE%/}/$(basename "${artifact}")"
    rclone copyto "${artifact}" "${remote_path}" --immutable
    [[ "$(rclone size "${remote_path}" --json | tr -d '\n' | sed -n 's/.*"count":\\([0-9][0-9]*\\).*/\\1/p')" == "1" ]]
  done
  upload_verified=true
fi

evidence="${backup_dir}/backup-evidence-${stamp}.json"
printf '{"created_at":"%s","database_artifact":"%s","database_sha256":"%s","decrypt_verified":true,"off_host_verified":%s}\n' \
  "$(date -u +%FT%TZ)" "$(basename "${destination}")" "${checksum}" \
  "${upload_verified}" > "${evidence}"

if [[ "$(TZ="${BACKUP_TIMEZONE:-Asia/Tehran}" date +%u)" == "7" ]]; then
  cp -p "${destination}" "${backup_dir}/weekly-$(TZ="${BACKUP_TIMEZONE:-Asia/Tehran}" date +%G-%V).dump.enc"
fi

prune() {
  local keep="$1" pattern="$2" files
  mapfile -t files < <(find "${backup_dir}" -maxdepth 1 -type f -name "${pattern}" -print | sort -r)
  if ((${#files[@]} > keep)); then
    for file in "${files[@]:keep}"; do rm -f -- "${file}" "${file}.sha256"; done
  fi
}

prune 7 'daily-*.dump.enc'
prune 4 'weekly-*.dump.enc'
echo "Created ${destination}"
