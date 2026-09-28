#!/usr/bin/env bash
# A reused daily backup must still be copied and verified off host.
set -Eeuo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
mkdir -p "${scratch}/bin" "${scratch}/backups" "${scratch}/remote"
: > "${scratch}/.env.production"
: > "${scratch}/passphrase"
chmod 600 "${scratch}/passphrase"

stamp="$(TZ=Asia/Tehran date +%F)"
artifact="${scratch}/backups/daily-${stamp}.dump.enc"
printf 'existing encrypted dump fixture\n' > "${artifact}"
sha256sum "${artifact}" > "${artifact}.sha256"

cat > "${scratch}/bin/openssl" <<'SH'
#!/usr/bin/env bash
while (($#)); do
  if [[ "$1" == -in ]]; then shift; input="$1"; fi
  shift
done
cat "${input}"
SH
cat > "${scratch}/bin/docker" <<'SH'
#!/usr/bin/env bash
cat >/dev/null
SH
cat > "${scratch}/bin/rclone" <<'SH'
#!/usr/bin/env bash
case "$1" in
  copyto) cp "$2" "$3" ;;
  size) printf '{"count":1}\n' ;;
  cat) cat "$2" ;;
  *) exit 2 ;;
esac
SH
chmod +x "${scratch}/bin/openssl" "${scratch}/bin/docker" "${scratch}/bin/rclone"

PATH="${scratch}/bin:${PATH}" \
PROJECT_DIR="${project_dir}" \
ENV_FILE="${scratch}/.env.production" \
BACKUP_DIR="${scratch}/backups" \
BACKUP_PASSPHRASE_FILE="${scratch}/passphrase" \
RCLONE_REMOTE="${scratch}/remote" \
  "${project_dir}/scripts/backup_postgres.sh" >/dev/null

cmp "${artifact}" "${scratch}/remote/$(basename "${artifact}")"
cmp "${artifact}.sha256" "${scratch}/remote/$(basename "${artifact}").sha256"
python3 - "${scratch}/backups/backup-evidence-${stamp}.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as file:
    evidence = json.load(file)
assert evidence["decrypt_verified"] is True
assert evidence["off_host_verified"] is True
PY

rm -f "${artifact}" "${artifact}.sha256"
if PATH="${scratch}/bin:${PATH}" \
  PROJECT_DIR="${project_dir}" ENV_FILE="${scratch}/.env.production" \
  BACKUP_DIR="${scratch}/backups" BACKUP_PASSPHRASE_FILE="${scratch}/passphrase" \
  BACKUP_MIN_FREE_KB=999999999999 \
  "${project_dir}/scripts/backup_postgres.sh" >"${scratch}/guard.log" 2>&1; then
  echo "Backup ignored the free-space guard" >&2
  exit 1
fi
grep -q 'Refusing backup:' "${scratch}/guard.log"
test ! -e "${artifact}"
