#!/usr/bin/env bash
# Run on the Mac. A receipt reaches the VPS only after the local copy is verified.
set -Eeuo pipefail
umask 077

host="${BACKUP_SSH_HOST:-45.139.10.12}"
remote_dir="/var/backups/portfolio"
local_dir="${BACKUP_LOCAL_DIR:-${HOME}/Backups/portfolio/automated}"
passphrase_file="${BACKUP_PASSPHRASE_FILE:-${HOME}/Backups/portfolio/backup-passphrase}"
pg_restore="${PG_RESTORE:-/opt/homebrew/opt/postgresql@16/bin/pg_restore}"
mkdir -p "${local_dir}"
[[ -r "${passphrase_file}" && -x "${pg_restore}" ]] || {
  printf 'Backup passphrase or pg_restore unavailable\n' >&2
  exit 1
}

artifact_path="$(ssh -o BatchMode=yes "${host}" \
  "find ${remote_dir} -maxdepth 1 -type f -name 'daily-????-??-??.dump.enc' -print | sort | tail -n 1")"
[[ "${artifact_path}" =~ ^/var/backups/portfolio/daily-[0-9]{4}-[0-9]{2}-[0-9]{2}\.dump\.enc$ ]] || {
  printf 'No valid daily backup found on %s\n' "${host}" >&2
  exit 1
}
artifact="${artifact_path##*/}"
checksum_line="$(ssh -o BatchMode=yes "${host}" "cat '${artifact_path}.sha256'")"
read -r expected_hash checksum_name <<<"${checksum_line}"
[[ "${expected_hash}" =~ ^[0-9a-f]{64}$ && "${checksum_name}" == "${artifact}" ]] || {
  printf 'Invalid checksum for %s\n' "${artifact}" >&2
  exit 1
}

destination="${local_dir}/${artifact}"
partial="${destination}.partial.$$"
receipt="${local_dir}/backup-evidence-${artifact#daily-}"
receipt="${receipt%.dump.enc}.json"
remote_partial="${remote_dir}/.$(basename "${receipt}").partial.$$"
trap 'rm -f -- "${partial}" "${receipt}.partial.$$"' EXIT

local_hash=""
if [[ -f "${destination}" ]]; then
  local_hash="$(shasum -a 256 "${destination}" | awk '{print $1}')"
fi
if [[ "${local_hash}" != "${expected_hash}" ]]; then
  scp -q "${host}:${artifact_path}" "${partial}"
  local_hash="$(shasum -a 256 "${partial}" | awk '{print $1}')"
  [[ "${local_hash}" == "${expected_hash}" ]] || {
    printf 'Off-host checksum mismatch for %s\n' "${artifact}" >&2
    exit 1
  }
  mv -f "${partial}" "${destination}"
fi

openssl enc -d -aes-256-cbc -pbkdf2 -iter 310000 \
  -pass "file:${passphrase_file}" -in "${destination}" \
  | "${pg_restore}" --file=/dev/null

python3 - "${receipt}.partial.$$" "${artifact}" "${expected_hash}" <<'PY'
import datetime
import json
import sys

with open(sys.argv[1], "w", encoding="utf-8") as target:
    json.dump({
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "database_artifact": sys.argv[2],
        "database_sha256": sys.argv[3],
        "decrypt_verified": True,
        "off_host_verified": True,
        "off_host_method": "mac_pull",
    }, target)
    target.write("\n")
PY
mv -f "${receipt}.partial.$$" "${receipt}"
scp -q "${receipt}" "${host}:${remote_partial}"
ssh -o BatchMode=yes "${host}" \
  "chmod 600 '${remote_partial}' && mv -f '${remote_partial}' '${remote_dir}/$(basename "${receipt}")'"

# Only this dedicated automation directory is pruned. Keep the last three
# verified daily copies; manual and external backups live outside it.
kept=0
while IFS= read -r old; do
  ((kept+=1))
  if ((kept > 3)); then
    old_stamp="${old##*/}"
    old_stamp="${old_stamp#daily-}"
    old_stamp="${old_stamp%.dump.enc}"
    rm -f -- "${old}" "${local_dir}/backup-evidence-${old_stamp}.json"
  fi
done < <(find "${local_dir}" -maxdepth 1 -type f \
  -name 'daily-????-??-??.dump.enc' -print | sort -r)
printf 'Verified %s on Mac and published receipt\n' "${artifact}"
