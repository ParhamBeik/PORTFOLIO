#!/usr/bin/env bash
# Pull encrypted VPS backups, retired-app archives, and future Parquet exports
# onto an external USB drive mounted under /Volumes/<NAME>.
#
# Safety: refuses to write to the Mac's internal SSD or any drive with <50 GB free.
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /Volumes/<EXTERNAL_USB_DRIVE>" >&2
  exit 2
fi

DEST_ROOT="${1%/}"
VPS_HOST="${VPS_HOST:-vps}"
MIN_FREE_KB=$((50 * 1024 * 1024)) # 50 GiB minimum free space on USB target

case "${DEST_ROOT}" in
  /Volumes/*) ;;
  *)
    echo "ERROR: Destination must be an external drive under /Volumes/ (got: ${DEST_ROOT})" >&2
    exit 1
    ;;
esac

case "${DEST_ROOT}" in
  "/Volumes/Macintosh HD"|"/Volumes/Macintosh HD/"*|"/Volumes/Recovery"|"/Volumes/Recovery/"*)
    echo "ERROR: Refusing to write to internal Mac volume: ${DEST_ROOT}" >&2
    exit 1
    ;;
esac

if [[ ! -d "${DEST_ROOT}" ]]; then
  echo "ERROR: External drive not mounted at ${DEST_ROOT}" >&2
  exit 1
fi

avail_kb="$(df -Pk "${DEST_ROOT}" | awk 'NR==2 {print $4}')"
if (( avail_kb < MIN_FREE_KB )); then
  echo "ERROR: ${DEST_ROOT} has $((avail_kb / 1024 / 1024)) GiB free (< 50 GiB required)" >&2
  exit 1
fi

ARCHIVE_DIR="${DEST_ROOT}/vps-archive"
mkdir -p "${ARCHIVE_DIR}/backups/portfolio" "${ARCHIVE_DIR}/backups/bama"

echo "==> Pulling encrypted DB backups from ${VPS_HOST}..."
rsync -avP --partial "${VPS_HOST}:/var/backups/portfolio/" "${ARCHIVE_DIR}/backups/portfolio/"
rsync -avP --partial "${VPS_HOST}:/var/backups/bama/" "${ARCHIVE_DIR}/backups/bama/"

echo "==> Verifying SHA-256 checksums..."
for app in portfolio bama; do
  dir="${ARCHIVE_DIR}/backups/${app}"
  for sha_file in "${dir}"/*.sha256; do
    [[ -e "${sha_file}" ]] || continue
    expected_hash="$(awk '{print $1}' "${sha_file}")"
    target_file="${sha_file%.sha256}"
    if [[ ! -f "${target_file}" ]]; then
      echo "ERROR: Missing dump file for ${sha_file}" >&2
      exit 1
    fi
    actual_hash="$(shasum -a 256 "${target_file}" | awk '{print $1}')"
    if [[ "${expected_hash}" != "${actual_hash}" ]]; then
      echo "ERROR: Checksum mismatch for ${target_file}" >&2
      exit 1
    fi
    echo "  OK $(basename "${target_file}") (${actual_hash:0:16}...)"
  done
done

if ssh "${VPS_HOST}" 'test -d /opt/archive/2026-10-15'; then
  echo "==> Pulling retired-app archive (/opt/archive/2026-10-15)..."
  mkdir -p "${ARCHIVE_DIR}/retired-apps/2026-10-15"
  rsync -avP --partial "${VPS_HOST}:/opt/archive/2026-10-15/" "${ARCHIVE_DIR}/retired-apps/2026-10-15/"
  echo
  echo "Retired archive synced to ${ARCHIVE_DIR}/retired-apps/2026-10-15/."
  echo "Once you have verified it on the USB drive, you can free 3.6 GB on the VPS with:"
  echo "  ssh ${VPS_HOST} 'rm -rf /opt/archive/2026-10-15'"
fi

if ssh "${VPS_HOST}" 'test -d /var/backups/parquet'; then
  echo "==> Pulling monthly Parquet exports..."
  mkdir -p "${ARCHIVE_DIR}/parquet"
  rsync -avP --partial "${VPS_HOST}:/var/backups/parquet/" "${ARCHIVE_DIR}/parquet/"
fi

echo
echo "==> Done. Archive summary on ${DEST_ROOT}:"
du -sh "${ARCHIVE_DIR}"/* 2>/dev/null || true
