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
destination="${backup_dir}/daily-${stamp}.dump.enc"
# PID suffix: root cron and CI deploy used the same .partial name, so a
# simultaneous run could not open the other's file (2026-09-08 CI deploy).
partial="${destination}.partial.$$"
scratch=("${partial}")
# `:-` because bash 3.2 treats an empty array as unbound under `set -u`, and an
# EXIT trap that itself errors would mask the real exit status.
trap 'rm -f "${scratch[@]:-}"' EXIT

# Publish one artifact, reading its content from stdin.
#
# Every write here goes through a temporary file and a rename, and that is not
# stylistic. `>` needs write permission on the *file*; rename(2) needs it only
# on the *directory*. The nightly cron runs as root and CI deploys as `deploy`,
# so the two leave differently-owned files in a shared directory: once root had
# written daily-<date>.dump.enc.sha256, every CI deploy for the rest of that day
# died truncating it -- after paying for a five-minute 1.8 GB dump. The big dump
# itself never hit this only because it was already being renamed into place.
publish() {
  local target="$1" tmp="$1.tmp.$$"
  scratch+=("${tmp}")
  cat > "${tmp}"
  chmod 664 "${tmp}"
  mv -f "${tmp}" "${target}"
}

checksum_file() {
  sha256sum "$1" 2>/dev/null | awk '{print $1}' \
    || shasum -a 256 "$1" | awk '{print $1}'
}

verify_backup() {
  local artifact="$1"
  openssl enc -d -aes-256-cbc -pbkdf2 -iter 310000 \
    -pass "file:${passphrase_file}" -in "${artifact}" 2>/dev/null \
    | "${compose[@]}" exec -T db pg_restore --file=/dev/null
}

# Nightly cron already wrote today's dump. Rewriting it as `deploy` fails when
# the file is root-owned, and a second 1.6 GB dump delays every CI ship.
if [[ -f "${destination}" ]]; then
  echo "Reusing existing ${destination}"
  verify_backup "${destination}" || {
    echo "Existing backup is unreadable: ${destination}" >&2
    exit 1
  }
  checksum="$(checksum_file "${destination}")"
  if [[ -f "${destination}.sha256" ]]; then
    expected_checksum="$(awk 'NR==1 {print $1}' "${destination}.sha256")"
    [[ "${checksum}" == "${expected_checksum}" ]] || {
      echo "Existing backup checksum does not match: ${destination}" >&2
      exit 1
    }
  else
    printf '%s  %s\n' "${checksum}" "$(basename "${destination}")" \
      | publish "${destination}.sha256"
  fi
  echo "Revalidated ${destination}"
else
  "${compose[@]}" exec -T db sh -c \
    'exec pg_dump --format=custom --no-owner --no-acl -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    | openssl enc -aes-256-cbc -salt -pbkdf2 -iter 310000 \
        -pass "file:${passphrase_file}" -out "${partial}"
  mv "${partial}" "${destination}"

  # Generate SQL to /dev/null so pg_restore drains and validates the whole archive.
  # Listing the table of contents only validates the header and can accept a dump
  # truncated after the TOC; pipefail also makes a decrypt failure fatal.
  verify_backup "${destination}" || {
    echo "Backup verification failed: ${destination} did not decrypt into a readable archive." >&2
    exit 1
  }
  checksum="$(checksum_file "${destination}")"
  printf '%s  %s\n' "${checksum}" "$(basename "${destination}")" | publish "${destination}.sha256"
fi

# Off-host copy. Everything above this line still leaves the only copy of the
# database on the same host as the database, so a host loss takes both.
#
# This block had never run -- RCLONE_REMOTE is unset in production and rclone is
# not installed -- and it could not have: the count regex was written with
# doubled backslashes (`\\([0-9]\\)`), which inside single quotes reaches sed as
# a literal backslash rather than a BRE group, so it matched nothing, the
# comparison against "1" was always false, and `set -e` would have killed the
# backup at the first upload. Both bugs are fixed here rather than left for
# whoever eventually turns this on.
upload_verified=false
if [[ -n "${RCLONE_REMOTE:-}" ]]; then
  command -v rclone >/dev/null
  # The checksum travels with the dump. Verifying an off-host artifact means
  # nothing if the only copy of its expected hash is on the host that died.
  for artifact in "${destination}" "${destination}.sha256"; do
    remote_path="${RCLONE_REMOTE%/}/$(basename "${artifact}")"
    rclone copyto "${artifact}" "${remote_path}" --immutable
    remote_count="$(rclone size "${remote_path}" --json | tr -d '\n' \
      | sed -n 's/.*"count":\([0-9][0-9]*\).*/\1/p')"
    [[ "${remote_count}" == "1" ]] || {
      echo "Off-host upload of $(basename "${artifact}") did not verify:" \
           "expected exactly 1 object at ${remote_path}, rclone reported '${remote_count}'." >&2
      exit 1
    }
    # A single object of the right size may still contain wrong bytes. Read
    # the remote object back and compare its digest before claiming recovery.
    remote_checksum="$(rclone cat "${remote_path}" | sha256sum | awk '{print $1}')"
    local_checksum="$(checksum_file "${artifact}")"
    [[ "${remote_checksum}" == "${local_checksum}" ]] || {
      echo "Off-host checksum mismatch: ${remote_path}" >&2
      exit 1
    }
  done
  upload_verified=true
fi

evidence="${backup_dir}/backup-evidence-${stamp}.json"
if [[ "${upload_verified}" == true ]] || [[ ! -f "${evidence}" ]] || \
  ! python3 - "${evidence}" "$(basename "${destination}")" "${checksum}" <<'PY'
import json
import sys
try:
    with open(sys.argv[1], encoding="utf-8") as source:
        evidence = json.load(source)
    valid = (evidence.get("database_artifact") == sys.argv[2]
             and evidence.get("database_sha256") == sys.argv[3]
             and evidence.get("decrypt_verified") is True
             and evidence.get("off_host_verified") is True)
except (OSError, ValueError):
    valid = False
sys.exit(0 if valid else 1)
PY
then
  printf '{"created_at":"%s","database_artifact":"%s","database_sha256":"%s","decrypt_verified":true,"off_host_verified":%s}\n' \
    "$(date -u +%FT%TZ)" "$(basename "${destination}")" "${checksum}" \
    "${upload_verified}" | publish "${evidence}"
fi

if [[ "$(TZ="${BACKUP_TIMEZONE:-Asia/Tehran}" date +%u)" == "7" ]]; then
  # `cp` truncates an existing destination in place, so it carries the same
  # cross-owner hazard as the checksum write did.
  publish "${backup_dir}/weekly-$(TZ="${BACKUP_TIMEZONE:-Asia/Tehran}" date +%G-%V).dump.enc" \
    < "${destination}"
fi

prune() {
  local keep="$1" pattern="$2" files
  files=()
  while IFS= read -r file; do files+=("${file}"); done \
    < <(find "${backup_dir}" -maxdepth 1 -type f -name "${pattern}" -print | sort -r)
  if ((${#files[@]} > keep)); then
    for file in "${files[@]:keep}"; do rm -f -- "${file}" "${file}.sha256"; done
  fi
}

# The production host has limited free space; longer retention belongs on the
# verified off-host destination, not beside the live database.
prune 2 'daily-*.dump.enc'
prune 1 'weekly-*.dump.enc'
echo "Created ${destination}"
