#!/usr/bin/env bash
# Installs hourly ops health checks and daily production backup restore drills in cron.
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
scripts_dir="${project_dir}/scripts"

cron_comment="# Portfolio SaaS Ops & Restore Drills"
hourly_ops="0 * * * * ${scripts_dir}/ops_check.sh >> /var/log/portfolio-ops-check.log 2>&1"
daily_restore="30 2 * * * ${scripts_dir}/restore_production_backup.sh >> /var/log/portfolio-restore-drill.log 2>&1"

existing_cron="$(crontab -l 2>/dev/null || true)"

if echo "${existing_cron}" | grep -q "restore_production_backup.sh"; then
  echo "Ops check and restore drill are already scheduled in crontab."
  exit 0
fi

(
  echo "${existing_cron}"
  echo ""
  echo "${cron_comment}"
  echo "${hourly_ops}"
  echo "${daily_restore}"
) | crontab -

echo "Installed cron schedule:"
echo " - Hourly: ${scripts_dir}/ops_check.sh"
echo " - Daily (02:30): ${scripts_dir}/restore_production_backup.sh"
