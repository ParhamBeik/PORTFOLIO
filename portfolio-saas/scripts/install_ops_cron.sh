#!/usr/bin/env bash
# Installs the hourly live-service and disk health check.
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
scripts_dir="${project_dir}/scripts"
hourly_ops="0 * * * * ${scripts_dir}/ops_check.sh >> /var/log/portfolio-ops-check.log 2>&1"
existing_cron="$(crontab -l 2>/dev/null || true)"
if grep -Fq "${scripts_dir}/ops_check.sh" <<<"${existing_cron}"; then
  echo "Ops check is already scheduled."
  exit 0
fi
printf '%s\n%s\n' "${existing_cron}" "${hourly_ops}" | crontab -
echo "Installed hourly ops check: ${scripts_dir}/ops_check.sh"
