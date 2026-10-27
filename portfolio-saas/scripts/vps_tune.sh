#!/usr/bin/env bash
# One-time (idempotent) host tuning for the shared 8 GB / 4-core VPS. Run as root:
#   sudo scripts/vps_tune.sh
# Each setting and why: docs/MONITORING.md. Re-running changes nothing.
set -Eeuo pipefail

conf=/etc/sysctl.d/99-vps-tune.conf
cat >"${conf}" <<'EOF'
# Managed by portfolio-saas/scripts/vps_tune.sh
# Prefer dropping page cache over swapping out database memory (default 60).
vm.swappiness = 10
# Keep directory/inode caches a little longer (many small MinIO files).
vm.vfs_cache_pressure = 50
# Start writing dirty pages early and in small amounts instead of letting
# 1.6 GB (20% of RAM) pile up and stall every process when it flushes.
vm.dirty_background_bytes = 134217728
vm.dirty_bytes = 536870912
# A crashing worker once wrote 84 GB of core files and wedged Postgres.
kernel.core_pattern = |/bin/true
EOF
sysctl -q -p "${conf}"

# Transparent huge pages "always" causes latency stalls with Postgres; use
# "madvise" (only programs that ask for them). Persist with a one-shot unit.
unit=/etc/systemd/system/thp-madvise.service
cat >"${unit}" <<'EOF'
[Unit]
Description=Set transparent hugepages to madvise
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'echo madvise > /sys/kernel/mm/transparent_hugepage/enabled'
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now thp-madvise.service >/dev/null

echo "swappiness=$(sysctl -n vm.swappiness) thp=$(cat /sys/kernel/mm/transparent_hugepage/enabled) core_pattern=$(sysctl -n kernel.core_pattern)"
