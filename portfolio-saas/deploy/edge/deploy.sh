#!/usr/bin/env bash
set -Eeuo pipefail
echo "Caddy is not deployed from this repository." >&2
echo "Live stack: /opt/apps/vps-edge  (project vps-edge, network vps-edge)" >&2
exit 1
