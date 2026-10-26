#!/usr/bin/env bash
# Installs the hourly live-service/disk health check and the one-minute
# server recorder (ops_metrics.sh; read it with ops_report.sh).
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
scripts_dir="${project_dir}/scripts"
hourly_ops="0 * * * * ${scripts_dir}/ops_check.sh >> /var/log/portfolio-ops-check.log 2>&1"
metrics="* * * * * ${scripts_dir}/ops_metrics.sh >> /var/log/vps-metrics.err 2>&1"
existing_cron="$(crontab -l 2>/dev/null || true)"
for entry in "${hourly_ops}" "${metrics}"; do
  script="$(awk '{print $6}' <<<"${entry}")"
  if grep -Fq "${script}" <<<"${existing_cron}"; then
    echo "Already scheduled: ${script}"
  else
    existing_cron="$(printf '%s\n%s\n' "${existing_cron}" "${entry}")"
    echo "Installed: ${entry}"
  fi
done
printf '%s\n' "${existing_cron}" | crontab -
