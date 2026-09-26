#!/usr/bin/env bash
# Exercise the copy cutover's worker guard and pre-migration rollback with fake Docker.
set -Eeuo pipefail
if ((BASH_VERSINFO[0] < 4)); then
  echo "broker copy deploy test requires Bash 4+" >&2
  exit 0
fi

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
mkdir -p "${scratch}/bin" "${scratch}/scripts" "${scratch}/receipts"
cp "${source_dir}/scripts/deploy.sh" "${scratch}/scripts/deploy.sh"
cp "${source_dir}/scripts/watchdog_prices.sh" "${scratch}/scripts/watchdog_prices.sh"
touch "${scratch}/docker-compose.prod.yml"
cat >"${scratch}/env" <<'ENV'
PORTFOLIO_DOMAIN=portfolio.example.com
EMAIL_HOST=smtp.example.com
ARCHIVE_WORKER_ENABLED=1
CODAL_WORKER_ENABLED=1
ENV
chmod 600 "${scratch}/env"

cat >"${scratch}/bin/df" <<'SH'
#!/usr/bin/env bash
printf 'Filesystem 1B-blocks Used Available Use%% Mounted on\nmock 40000000000 0 30000000000 0%% /\n'
SH
cat >"${scratch}/bin/stat" <<'SH'
#!/usr/bin/env bash
if [[ "${1:-}" == "-c" && "${2:-}" == "%a" ]]; then
  printf '600\n'
else
  /usr/bin/stat "$@"
fi
SH
cat >"${scratch}/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${MOCK_DOCKER_LOG}"
if [[ " $* " == *" network inspect vps-edge "* ]]; then exit 0; fi
if [[ " $* " == *" inspect -f "*".Config.Env"* ]]; then
  if [[ "${MOCK_BROKER_UNKNOWN:-0}" == "1" ]]; then
    printf 'CELERY_BROKER_URL=redis://other:6379/2\n'
  else
    printf 'CELERY_BROKER_URL=redis://redis:6379/2\n'
  fi
  exit 0
fi
if [[ " $* " == *" inspect -f "*".State.Status"* ]]; then
  [[ "${*: -1}" == "old-archive" ]] && printf 'paused\n' || printf 'running\n'
  exit 0
fi
if [[ " $* " == *" ps -q --all "* ]]; then
  case "${*: -1}" in
    backend) printf 'old-backend\n';;
    celery_worker_live) printf 'old-live\n';;
    celery_worker_archive) printf 'old-archive\n';;
    celery_beat) printf 'old-beat\n';;
  esac
  exit 0
fi
if [[ " $* " == *" ps -q db "* ]]; then exit 0; fi
if [[ " $* " == *" redis-cli -n 2 --raw LLEN "* ]]; then
  [[ "${*: -1}" == "live" ]] && printf '0\n' || printf '1\n'
  exit 0
fi
if [[ " $* " == *" exec -T broker redis-cli ping "* ]]; then printf 'PONG\n'; exit 0; fi
if [[ " $* " == *"/tmp/copy_celery_queues.py"* ]]; then
  if [[ "${MOCK_COPY_FAIL:-0}" == "1" ]]; then exit 1; fi
  printf '{"queues":{"archive":{"count":1},"codal":{"count":1}}}\n'
  exit 0
fi
if [[ " $* " == *" run --rm migrate "* ]]; then exit 1; fi
exit 0
SH
chmod +x "${scratch}/bin/df" "${scratch}/bin/stat" "${scratch}/bin/docker"

run_deploy() {
  PATH="${scratch}/bin:${PATH}" PROJECT_DIR="${scratch}" ENV_FILE="${scratch}/env" \
    BACKUP_DIR="${scratch}/receipts" BROKER_HANDOFF_MODE=copy \
    MOCK_DOCKER_LOG="${scratch}/docker.log" "${scratch}/scripts/deploy.sh"
}

: >"${scratch}/docker.log"
if run_deploy >"${scratch}/out" 2>"${scratch}/err"; then
  echo "Worker guard accepted re-enabling a paused or missing consumer" >&2
  exit 1
fi
grep -q 'Archive worker was not running' "${scratch}/err" || { cat "${scratch}/err" >&2; exit 1; }
if grep -q ' build\| stop\| copy_celery_queues.py' "${scratch}/docker.log"; then
  echo "Worker guard ran deployment work before refusing" >&2
  exit 1
fi

sed -i.bak 's/WORKER_ENABLED=1/WORKER_ENABLED=0/g' "${scratch}/env"
: >"${scratch}/docker.log"
if MOCK_BROKER_UNKNOWN=1 run_deploy >"${scratch}/out" 2>"${scratch}/err"; then
  echo "Unknown broker was accepted" >&2
  exit 1
fi
grep -q 'unknown broker' "${scratch}/err"
if grep -q ' build\| stop' "${scratch}/docker.log"; then
  echo "Unknown broker passed the preflight" >&2
  exit 1
fi

: >"${scratch}/docker.log"
if MOCK_COPY_FAIL=1 run_deploy >"${scratch}/out" 2>"${scratch}/err"; then
  echo "Failed queue copy did not abort" >&2
  exit 1
fi
grep -q 'Queue copy failed' "${scratch}/err"
grep -q ' start backend\| start celery_worker_live\| start celery_beat' "${scratch}/docker.log"
if grep -q ' start celery_worker_archive\| run --rm migrate' "${scratch}/docker.log"; then
  echo "Failed queue copy resumed paid work or ran migration" >&2
  exit 1
fi

: >"${scratch}/docker.log"
if run_deploy >"${scratch}/out" 2>"${scratch}/err"; then
  echo "Migration stub should have stopped the cutover" >&2
  exit 1
fi
grep -q 'copy_celery_queues.py' "${scratch}/docker.log" || { cat "${scratch}/err" "${scratch}/docker.log" >&2; exit 1; }
grep -q 'run --rm migrate' "${scratch}/docker.log" || { cat "${scratch}/err" "${scratch}/docker.log" >&2; exit 1; }
if grep -q ' start celery_worker_archive\| up -d --remove-orphans' "${scratch}/docker.log"; then
  echo "Cutover started paid work or continued past failed migration" >&2
  exit 1
fi
receipt="$(find "${scratch}/receipts" -type f | head -1)"
[[ -n "${receipt}" && "$(/usr/bin/stat -c '%a' "${receipt}")" == 600 ]]

: >"${scratch}/docker.log"
PATH="${scratch}/bin:${PATH}" PROJECT_DIR="${scratch}" ENV_FILE="${scratch}/env" \
  MOCK_DOCKER_LOG="${scratch}/docker.log" flock -x "${scratch}/env" \
  bash "${scratch}/scripts/watchdog_prices.sh" >"${scratch}/out" 2>"${scratch}/err"
grep -q 'deployment in progress' "${scratch}/out"
[[ ! -s "${scratch}/docker.log" ]]
