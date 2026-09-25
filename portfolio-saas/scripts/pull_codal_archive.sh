#!/usr/bin/env bash
# Run on the Mac while the external backup drive is mounted. This is a live
# MinIO volume capture; verify with an isolated S3 restore before relying on it.
set -Eeuo pipefail
umask 077

host="${BACKUP_SSH_HOST:-45.139.10.12}"
archive_root="${CODAL_ARCHIVE_DIR:-/Volumes/ADATA HD700/portfolio-backups}"
volume_root="/Volumes/ADATA HD700"
[[ -d "${volume_root}" && "$(stat -f %d "${volume_root}")" != "$(stat -f %d /Volumes)" ]] || {
  printf 'External backup drive is not mounted\n' >&2
  exit 1
}
[[ "${archive_root}" == "${volume_root}/"* ]] || {
  printf 'CODAL_ARCHIVE_DIR must be on the external backup drive\n' >&2
  exit 1
}
command -v zstd >/dev/null
command -v split >/dev/null
mkdir -p "${archive_root}"
free_gb="$(df -g "${archive_root}" | awk 'NR==2 {print $4}')"
((free_gb >= 15)) || { printf 'External backup drive has less than 15 GiB free\n' >&2; exit 1; }

stamp="$(TZ=Asia/Tehran date +%G-W%V)"
destination="${archive_root}/codal-archive-${stamp}"
if [[ -d "${destination}" ]]; then
  (cd "${destination}" && shasum -a 256 -c SHA256SUMS >/dev/null)
  printf 'Existing Codal archive verified: %s\n' "${destination}"
  exit 0
fi
partial="${destination}.partial.$$"
mkdir "${partial}"

ssh -o BatchMode=yes "${host}" \
  'set -e; project=/opt/apps/portfolio-repo/portfolio-saas; cid=$(docker compose --project-directory "$project" -f "$project/docker-compose.prod.yml" --env-file "$project/.env.production" ps -q minio); test -n "$cid"; docker cp "$cid:/data/." -' \
  | zstd -q -T2 -3 \
  | split -b 3500m - "${partial}/codal.tar.zst.part-"

(cd "${partial}" && shasum -a 256 codal.tar.zst.part-* > SHA256SUMS \
  && shasum -a 256 -c SHA256SUMS >/dev/null)
cat "${partial}"/codal.tar.zst.part-* | zstd -dc -q | tar -tf - > "${partial}/files.txt"
metadata_count="$(grep -c '/xl.meta$' "${partial}/files.txt")"
((metadata_count > 1000)) || {
  printf 'Codal archive has only %s object metadata files\n' "${metadata_count}" >&2
  exit 1
}

# This known immutable report must survive every backup at the source digest.
sample_hash="cad5a262ad91c58abfab89874f93770f63cfa5fcabf5bcbb6b20a360807c3dd0"
sample_path="$(grep -E "codal/sha256/ca/${sample_hash}\.xlsx/.*/part\.1$" \
  "${partial}/files.txt" | head -n 1)"
[[ -n "${sample_path}" ]] || { printf 'Known Codal sample is absent\n' >&2; exit 1; }
actual_hash="$(cat "${partial}"/codal.tar.zst.part-* | zstd -dc -q \
  | tar -xOf - "${sample_path}" | tail -c +33 | shasum -a 256 | awk '{print $1}')"
[[ "${actual_hash}" == "${sample_hash}" ]] || {
  printf 'Known Codal sample checksum mismatch\n' >&2
  exit 1
}

printf 'captured_at_utc=%s\nobject_metadata_count=%s\nknown_artifact_sha256=%s\n' \
  "$(date -u +%FT%TZ)" "${metadata_count}" "${sample_hash}" > "${partial}/verification.txt"
mv "${partial}" "${destination}"
printf 'Verified Codal archive %s (%s object metadata files)\n' "${destination}" "${metadata_count}"
