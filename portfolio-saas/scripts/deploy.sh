#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")

[[ -r "${env_file}" ]] || { echo "Create ${env_file} from .env.production.example" >&2; exit 1; }
[[ "$(stat -c '%a' "${env_file}")" =~ ^[46]00$ ]] || { echo "${env_file} must have mode 400 or 600" >&2; exit 1; }
docker network inspect vps-edge >/dev/null 2>&1 || { echo "Docker network vps-edge is missing; the VPS reverse proxy at /opt/apps/vps-edge must already be running" >&2; exit 1; }
domain="$(awk -F= '$1=="PORTFOLIO_DOMAIN"{print $2; exit}' "${env_file}")"
[[ -n "${domain}" ]] || { echo "PORTFOLIO_DOMAIN is missing from ${env_file}" >&2; exit 1; }
mail_host="$(awk -F= '$1=="EMAIL_HOST"{print $2; exit}' "${env_file}")"
if [[ -z "${mail_host}" || "${mail_host}" == "localhost" || "${mail_host}" == "127.0.0.1" ]]; then
  echo "WARNING: EMAIL_HOST is '${mail_host:-<empty>}'. Self-service password reset cannot send mail; use the superuser recovery link until a relay is configured." >&2
fi

# Enough room to finish. A deploy writes a ~1.7 GB encrypted dump and then
# builds images, and running out midway is the worst moment to do it: the dump
# lands truncated over the day's restore point and Postgres shares the device.
#
# The box is not ours alone -- three other stacks live on it and the largest
# single consumer is a neighbour's 34 GB media volume -- so free space moves for
# reasons this repository cannot see. Measured 2026-09-09: 31.6 GB free of
# 147.4 GB, filling at ~0.7 GB/day, which is why this is a check and not a
# comment.
free_bytes="$(df -PB1 /var/lib/docker | awk 'NR==2 {print $4}')"
free_gb=$(( free_bytes / 1024 / 1024 / 1024 ))
if (( free_gb < 10 )); then
  echo "Only ${free_gb} GB free on the Docker device. A deploy needs room for a ~1.7 GB dump plus an image build; refusing to start one that cannot finish." >&2
  exit 1
elif (( free_gb < 20 )); then
  echo "WARNING: ${free_gb} GB free on the Docker device. See the Ops console's disk meter." >&2
fi

if [[ -n "$("${compose[@]}" ps -q db)" ]] && "${compose[@]}" exec -T db pg_isready >/dev/null 2>&1; then
  "${project_dir}/scripts/backup_postgres.sh"
fi
"${compose[@]}" build
"${compose[@]}" run --rm migrate
"${compose[@]}" up -d --remove-orphans
"${compose[@]}" exec -T backend python manage.py check --deploy --fail-level WARNING
"${compose[@]}" exec -T backend python manage.py migrate --check
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/ready/"
"${compose[@]}" exec -T celery_worker_live celery -A config inspect ping
"${compose[@]}" exec -T celery_worker_archive celery -A config inspect ping

# Beat is a scheduler, not a worker: `celery inspect ping` does not answer for
# it, so it was the one service this script never verified -- and it is the one
# whose failure is invisible. On 2026-09-05 a Dockerfile change left /app owned
# by root, beat could not create its `celerybeat-schedule`, and it crash-looped
# from the first second. The backend, both workers and the frontend only ever
# READ from /app, so all four reported healthy, every check below passed, and
# the deploy went green while the price loop, the archive tick and every
# nightly job were stopped. Prices went stale for the twelve minutes it took a
# human to notice.
#
# Uptime, not liveness. A crash-looping container is genuinely "running" for
# part of each cycle, so a single point-in-time `ps` can sample it mid-cycle and
# pass. Requiring it to have survived a restart interval is what makes this a
# real assertion.
beat_cid="$("${compose[@]}" ps -q celery_beat)"
[[ -n "${beat_cid}" ]] || { echo "celery_beat has no container; the scheduler is not deployed" >&2; exit 1; }
sleep 45
beat_running="$(docker inspect -f '{{.State.Running}}' "${beat_cid}")"
beat_started="$(docker inspect -f '{{.State.StartedAt}}' "${beat_cid}")"
beat_uptime=$(( $(date -u +%s) - $(date -u -d "${beat_started}" +%s) ))
if [[ "${beat_running}" != "true" || "${beat_uptime}" -lt 30 ]]; then
  echo "celery_beat is not stable (running=${beat_running}, uptime=${beat_uptime}s): the scheduler is down, so nothing is on a schedule." >&2
  "${compose[@]}" logs --tail 30 celery_beat >&2
  exit 1
fi

curl -fsS "https://${domain}/api/health/"
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/prices/"

# What the BROWSER receives, which is not what the container sends.
#
# `frontend/security-headers.conf` is careful and `frontend/scripts/lhci.sh`
# asserts it -- but that runs against the container on a local port, and TLS is
# terminated by a VPS-wide Caddy at /opt/apps/vps-edge that this repository does
# not ship. Caddy's `header` directive REPLACES an upstream header rather than
# deferring to it, so a shared snippet there silently overrode ours for every
# real visitor. Measured on 2026-09-09: the container served
# `script-src 'self'` and users got `script-src 'self' 'unsafe-inline'`, from a
# snippet shared with two unrelated apps. CI could not see it by construction --
# it tests a port nobody browses -- and the deploy said "healthy" throughout.
#
# So this asserts the deployed edge, not the image. `'unsafe-inline'` in
# script-src is the specific thing being guarded: the shipped index.html has one
# external module script, no inline bodies and no `on*` handlers, so the app has
# never needed it, and it is the directive that decides whether an injected
# <script> runs. Chart tooltips build HTML out of user-typed holding nicknames,
# which is exactly the surface CSP is the second line of defence for.
echo "==> response headers as a browser sees them"
edge_headers="$(curl -fsSI "https://${domain}/" | tr -d '\r')"
header_fail=0
require_header() {
  local name="$1" expect="$2" line
  line="$(grep -i "^${name}:" <<<"${edge_headers}" || true)"
  if [[ -z "${line}" ]]; then
    echo "  MISSING  ${name}" >&2
    header_fail=1
  elif [[ -n "${expect}" ]] && ! grep -qi -- "${expect}" <<<"${line}"; then
    echo "  WRONG    ${name} -> ${line} (expected to contain '${expect}')" >&2
    header_fail=1
  else
    echo "  ok       ${name}"
  fi
}
require_header "content-security-policy" "script-src"
# Only the script-src directive, isolated. `style-src 'unsafe-inline'` is
# legitimate and deliberate -- React renders `style={{...}}` as style
# attributes, which CSP blocks without it -- so a naive grep for
# "unsafe-inline" across the whole policy would fail on the one place it belongs.
script_src="$(
  grep -i '^content-security-policy:' <<<"${edge_headers}" \
    | tr ';' '\n' | grep -i 'script-src' || true
)"
if grep -qi "unsafe-inline\|unsafe-eval" <<<"${script_src}"; then
  echo "  UNSAFE   script-src allows inline or eval ->${script_src}" >&2
  header_fail=1
else
  echo "  ok       script-src allows neither inline nor eval"
fi
require_header "strict-transport-security" "max-age=31536000"
require_header "x-content-type-options" "nosniff"
require_header "referrer-policy" ""
require_header "cross-origin-opener-policy" "same-origin"
if [[ "${header_fail}" -ne 0 ]]; then
  echo "The edge proxy is not serving the security headers this app ships. See /opt/apps/vps-edge/Caddyfile." >&2
  exit 1
fi

# Every deploy builds on the box, and nothing ever collected the layers it
# superseded: 6.8 GB of build cache had accumulated by 2026-09-09 on a device
# with 31.6 GB free.
#
# 48h, and that number is measured rather than chosen. This box deploys most
# days, so a week-long window reclaimed exactly 0 B -- every cached layer was
# younger than the filter. Stepping down: `until=72h` freed 1.35 GB and a
# further `until=48h` freed 1.23 GB, 2.58 GB together, taking the device from
# 78% to 76%. Two days still covers a same-day rollback rebuild, which is the
# case the cache actually earns its keep on.
#
# Build cache is derived data -- the worst case is a slower build, never a lost
# byte -- so this runs unattended, and it runs after the health checks so a
# failed deploy keeps its layers for the retry.
docker builder prune --force --filter until=48h >/dev/null 2>&1 || true

# Configuration is checked before mutation above. A real relay that is
# temporarily unreachable is degraded, not a reason to roll back healthy code.
if [[ -n "${mail_host}" && "${mail_host}" != "localhost" && "${mail_host}" != "127.0.0.1" ]] \
  && ! "${compose[@]}" exec -T backend python -c "
import socket, sys
from django.conf import settings
import django; django.setup()
try:
    socket.create_connection((settings.EMAIL_HOST, settings.EMAIL_PORT), timeout=5).close()
except Exception as exc:
    sys.exit(f'{type(exc).__name__}: {exc}')
" 2>/dev/null; then
  echo "WARNING: cannot reach the mail relay at ${mail_host}. Password reset will silently send nothing." >&2
elif [[ -n "${mail_host}" && "${mail_host}" != "localhost" && "${mail_host}" != "127.0.0.1" ]]; then
  echo "  ok       outbound mail relay reachable at ${mail_host}"
fi
