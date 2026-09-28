#!/usr/bin/env bash
set -Eeuo pipefail

project_dir="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
env_file="${ENV_FILE:-${project_dir}/.env.production}"
compose=(docker compose -f "${project_dir}/docker-compose.prod.yml" --env-file "${env_file}")

[[ -r "${env_file}" ]] || { echo "Create ${env_file} from .env.production.example" >&2; exit 1; }
[[ "$(stat -c '%a' "${env_file}")" =~ ^[46]00$ ]] || { echo "${env_file} must have mode 400 or 600" >&2; exit 1; }
# The host price watchdog takes a shared lock before it can restart old
# workers. Hold the exclusive lock through queue freeze, migration, and health
# checks so a cron tick cannot revive a producer during the copy.
exec {deploy_lock_fd}<"${env_file}"
flock -n -x "${deploy_lock_fd}" || { echo "Another deployment or watchdog is active" >&2; exit 1; }
docker network inspect vps-edge >/dev/null 2>&1 || { echo "Docker network vps-edge is missing; the VPS reverse proxy at /opt/apps/vps-edge must already be running" >&2; exit 1; }
domain="$(awk -F= '$1=="PORTFOLIO_DOMAIN"{print $2; exit}' "${env_file}")"
[[ -n "${domain}" ]] || { echo "PORTFOLIO_DOMAIN is missing from ${env_file}" >&2; exit 1; }
archive_worker_enabled="$(awk -F= '$1=="ARCHIVE_WORKER_ENABLED"{print $2; exit}' "${env_file}")"
codal_worker_enabled="$(awk -F= '$1=="CODAL_WORKER_ENABLED"{print $2; exit}' "${env_file}")"
archive_worker_enabled="${archive_worker_enabled:-1}"
codal_worker_enabled="${codal_worker_enabled:-1}"
handoff_mode="${BROKER_HANDOFF_MODE:-$(awk -F= '$1=="BROKER_HANDOFF_MODE"{print $2; exit}' "${env_file}")}"
handoff_mode="${handoff_mode:-drain}"
[[ "${archive_worker_enabled}" =~ ^[01]$ && "${codal_worker_enabled}" =~ ^[01]$ ]] || {
  echo "ARCHIVE_WORKER_ENABLED and CODAL_WORKER_ENABLED must be 0 or 1" >&2; exit 1;
}
[[ "${handoff_mode}" == "drain" || "${handoff_mode}" == "copy" ]] || {
  echo "BROKER_HANDOFF_MODE must be drain or copy" >&2; exit 1;
}
if [[ "${handoff_mode}" == "copy" ]]; then
  # Root cron on the VPS still invokes a watchdog from an older checkout.
  # It cannot see this env-file lock and could restart old workers mid-copy.
  legacy_watchdog="${LEGACY_WATCHDOG_PATH:-/opt/apps/portfolio-saas/scripts/watchdog_prices.sh}"
  if [[ "${legacy_watchdog}" != "${project_dir}/scripts/watchdog_prices.sh" && -x "${legacy_watchdog}" ]]; then
    echo "Legacy watchdog is executable at ${legacy_watchdog}; disable it and migrate root cron before queue copy." >&2
    exit 1
  fi
fi
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
if (( free_gb < 5 )); then
  echo "Only ${free_gb} GB free on the Docker device. A deploy needs room for a ~1.7 GB dump plus an image build; refusing to start one that cannot finish." >&2
  exit 1
elif (( free_gb < 15 )); then
  echo "WARNING: ${free_gb} GB free on the Docker device. See the Ops console's disk meter." >&2
fi

# A broker cutover cannot drain a queue whose old consumer is absent or paused.
# Check before the backup/build, and especially before stopping the API: an
# older Compose stack can have queued Codal jobs but no Codal worker at all.
backend_cid="$("${compose[@]}" ps -q --all backend)"
legacy_broker=0
if [[ -n "${backend_cid}" ]]; then
  if ! broker_environment="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${backend_cid}")"; then
    echo "Cannot inspect the current backend broker; refusing deployment." >&2
    exit 1
  fi
  if grep -qx 'CELERY_BROKER_URL=redis://redis:6379/2' <<<"${broker_environment}"; then
    legacy_broker=1
  elif ! grep -qx 'CELERY_BROKER_URL=redis://broker:6379/0' <<<"${broker_environment}"; then
    echo "Current backend uses an unknown broker; refusing deployment." >&2
    exit 1
  fi
fi
if (( legacy_broker )); then
  declare -A legacy_service_state=()
  for service in backend celery_worker_live celery_worker_archive celery_worker_codal celery_beat; do
    service_cid="$("${compose[@]}" ps -q --all "${service}")"
    if [[ -n "${service_cid}" ]]; then
      legacy_service_state["${service}"]="$(docker inspect -f '{{.State.Status}}' "${service_cid}")"
    fi
  done
  if [[ "${archive_worker_enabled}" == "1" && "${legacy_service_state[celery_worker_archive]:-}" != "running" ]]; then
    echo "Archive worker was not running; refusing to start a paid provider consumer during deployment." >&2
    exit 1
  fi
  if [[ "${codal_worker_enabled}" == "1" && "${legacy_service_state[celery_worker_codal]:-}" != "running" ]]; then
    echo "Codal worker was not running; validate artifact access and the corrected parser before starting this consumer." >&2
    exit 1
  fi
  for queue in live archive codal; do
    if ! depth="$("${compose[@]}" exec -T redis redis-cli -n 2 --raw LLEN "${queue}")" \
      || [[ ! "${depth}" =~ ^[0-9]+$ ]]; then
      echo "Cannot inspect legacy ${queue} queue; refusing broker cutover." >&2
      exit 1
    fi
    (( depth > 0 )) || continue
    [[ "${handoff_mode}" == "drain" ]] || continue
    worker_cid="$("${compose[@]}" ps -q --all "celery_worker_${queue}")"
    worker_state="missing"
    if [[ -n "${worker_cid}" ]]; then
      worker_state="$(docker inspect -f '{{.State.Running}}:{{.State.Paused}}' "${worker_cid}")"
    fi
    if [[ "${worker_state}" != "true:false" ]]; then
      echo "Legacy ${queue} queue has ${depth} jobs, but its worker is ${worker_state}; refusing broker cutover before stopping the API." >&2
      exit 1
    fi
  done
fi

if [[ -n "$("${compose[@]}" ps -q db)" ]] && "${compose[@]}" exec -T db pg_isready >/dev/null 2>&1; then
  "${project_dir}/scripts/backup_postgres.sh"
fi
"${compose[@]}" build
# The first broker cutover cannot abandon work in Redis DB 2. Drain active
# consumers or explicitly copy quiesced lists to the durable broker. Results
# remain on DB 2 and do not count as outstanding work.
if (( legacy_broker )); then
  drain_timeout="${BROKER_DRAIN_TIMEOUT_SECONDS:-300}"
  [[ "${drain_timeout}" =~ ^[1-9][0-9]*$ ]] || {
    echo "BROKER_DRAIN_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 1
  }
  resume_legacy_services() {
    local failed=0
    for service in backend celery_worker_live celery_worker_archive celery_worker_codal celery_beat; do
      # A worker intentionally paused before deployment must not wake on a
      # failed cutover and unexpectedly spend provider quota.
      if [[ "${legacy_service_state[${service}]:-}" == "running" ]]; then
        if ! "${compose[@]}" start "${service}"; then
          echo "Could not restart legacy ${service}; manual recovery is required." >&2
          failed=1
        fi
      fi
    done
    (( failed == 0 )) || echo "One or more legacy services did not restart." >&2
  }
  if [[ "${handoff_mode}" == "copy" ]]; then
    "${compose[@]}" up -d --no-deps broker
    # Compose starts the container before Redis accepts connections. The first
    # production cutover hit this gap and aborted with the old stack untouched.
    broker_ready=0
    for ((attempt = 1; attempt <= 30; attempt++)); do
      if "${compose[@]}" exec -T broker redis-cli ping 2>/dev/null | grep -qx PONG; then
        broker_ready=1
        break
      fi
      sleep 1
    done
    (( broker_ready )) || {
      echo "New broker is not ready; legacy services are untouched." >&2
      exit 1
    }
    if ! "${compose[@]}" stop celery_beat backend; then
      resume_legacy_services
      echo "Could not quiesce legacy producers; aborted queue copy." >&2
      exit 1
    fi
    for service in celery_worker_live celery_worker_archive celery_worker_codal; do
      # Leave an already paused worker paused until the copy succeeds, so a
      # failed handoff preserves its exact no-provider-call state.
      if [[ "${legacy_service_state[${service}]:-}" == "running" ]]; then
        if ! "${compose[@]}" stop "${service}"; then
          resume_legacy_services
          echo "Could not stop legacy ${service}; aborted queue copy." >&2
          exit 1
        fi
      fi
    done
    receipt_dir="${BACKUP_DIR:-/var/backups/portfolio}"
    mkdir -p "${receipt_dir}"
    receipt_home="$(mktemp -d "${receipt_dir}/broker-handoff-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")"
    receipt="$(mktemp "${receipt_home}/receipt.XXXXXX")"
    if ! "${compose[@]}" run --rm --no-deps -T \
        -v "${project_dir}/scripts/copy_celery_queues.py:/tmp/copy_celery_queues.py:ro" \
        --entrypoint python backend /tmp/copy_celery_queues.py \
        --source redis://redis:6379/2 --target redis://broker:6379/0 \
        --copy --receipt /tmp/broker-handoff.json >"${receipt}" \
        || ! python3 -m json.tool "${receipt}" >/dev/null; then
      rm -f "${receipt}"
      rmdir "${receipt_home}"
      resume_legacy_services
      echo "Queue copy failed; old source queues remain. Inspect the isolated target before retrying." >&2
      exit 1
    fi
    echo "Queue handoff receipt: ${receipt}"
  else
    "${compose[@]}" stop celery_beat backend
  legacy_pending() {
    "${compose[@]}" exec -T redis redis-cli -n 2 --raw EVAL \
      'return redis.call("LLEN","live")+redis.call("LLEN","archive")+redis.call("LLEN","codal")+redis.call("HLEN","unacked")' 0
  }
  drain_deadline=$(( $(date +%s) + drain_timeout ))
  while true; do
    if ! pending="$(legacy_pending)" || [[ ! "${pending}" =~ ^[0-9]+$ ]]; then
      resume_legacy_services
      echo "Could not inspect legacy Celery queue; restored old services and aborted broker cutover." >&2
      exit 1
    fi
    ((pending > 0)) || break
    if (( $(date +%s) >= drain_deadline )); then
      resume_legacy_services
      echo "Legacy Celery queue did not drain; restored old producers and aborted broker cutover." >&2
      exit 1
    fi
    sleep 5
  done
    "${compose[@]}" stop celery_worker_live celery_worker_archive celery_worker_codal
    if ! pending="$(legacy_pending)" || [[ "${pending}" != 0 ]]; then
      resume_legacy_services
      echo "Legacy workers requeued tasks while stopping; restored old services and aborted broker cutover." >&2
      exit 1
    fi
  fi
fi
# This release drops legacy quantity/price columns. Old web and worker images
# must not continue reading or writing those columns while migrate runs.
# Keep DB/Redis/MinIO up; the new images start only after migration succeeds.
"${compose[@]}" stop celery_beat celery_worker_live celery_worker_archive celery_worker_codal backend frontend
"${compose[@]}" run --rm migrate
if (( legacy_broker )) && [[ "${handoff_mode}" == "copy" ]]; then
  # Recheck immediately before new workers can consume. An out-of-band producer
  # writing to the old Redis after the receipt would otherwise strand work.
  if ! "${compose[@]}" run --rm --no-deps -T \
      -v "${project_dir}/scripts/copy_celery_queues.py:/tmp/copy_celery_queues.py:ro" \
      --entrypoint python backend /tmp/copy_celery_queues.py \
      --source redis://redis:6379/2 --target redis://broker:6379/0 \
      >"${receipt_home}/verify.json" \
      || ! python3 - "${receipt}" "${receipt_home}/verify.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    receipt = json.load(source)
with open(sys.argv[2], encoding="utf-8") as source:
    current = json.load(source)
if not (
    current["source_unacked"] == current["target_unacked"] == 0
    and receipt["queues"] == current["source_queues"] == current["target_queues"]
):
    raise SystemExit("Copied queues changed since receipt")
PY
  then
    echo "Queue verification failed after migration; new workers remain stopped. Inspect both brokers and schema before recovery." >&2
    exit 1
  fi
fi
"${compose[@]}" up -d --remove-orphans \
  --scale "celery_worker_archive=${archive_worker_enabled}" \
  --scale "celery_worker_codal=${codal_worker_enabled}"
"${compose[@]}" exec -T backend python manage.py check --deploy --fail-level WARNING
"${compose[@]}" exec -T backend python manage.py migrate --check
curl -fsS --retry 12 --retry-delay 5 "https://${domain}/api/health/ready/"
"${compose[@]}" exec -T celery_worker_live celery -A config inspect ping
if [[ "${archive_worker_enabled}" == "1" ]]; then
  "${compose[@]}" exec -T celery_worker_archive celery -A config inspect ping
fi
if [[ "${codal_worker_enabled}" == "1" ]]; then
  "${compose[@]}" exec -T celery_worker_codal celery -A config inspect ping
fi

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
