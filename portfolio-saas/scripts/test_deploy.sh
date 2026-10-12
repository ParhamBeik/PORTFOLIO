#!/usr/bin/env bash
# Exercise deploy.sh's ordering, image handling and pruning, and the watchdog's
# deploy lock, against a fake Docker. Nothing here talks to a real daemon.
set -Eeuo pipefail
if ((BASH_VERSINFO[0] < 4)); then
  echo "deploy test requires Bash 4+" >&2
  exit 0
fi

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scratch="$(mktemp -d)"
trap 'rm -rf "${scratch}"' EXIT
mkdir -p "${scratch}/bin" "${scratch}/scripts"
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
printf 'prev\n' >"${scratch}/release"

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
cat >"${scratch}/bin/sleep" <<'SH'
#!/usr/bin/env bash
exit 0
SH
# The edge as a browser sees it: every header deploy.sh requires.
cat >"${scratch}/bin/curl" <<'SH'
#!/usr/bin/env bash
if [[ " $* " == *" -fsSI "* ]]; then
  printf 'HTTP/2 200\r\n'
  printf "content-security-policy: default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'\r\n"
  printf 'strict-transport-security: max-age=31536000; includeSubDomains\r\n'
  printf 'x-content-type-options: nosniff\r\n'
  printf 'referrer-policy: same-origin\r\n'
  printf 'cross-origin-opener-policy: same-origin\r\n'
fi
exit 0
SH
cat >"${scratch}/bin/docker" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"${MOCK_DOCKER_LOG}"
if [[ " $* " == *" network inspect vps-edge "* ]]; then exit 0; fi
if [[ " $* " == *" ps -q celery_beat"* ]]; then printf 'beat-cid\n'; exit 0; fi
if [[ " $* " == *" inspect -f {{.State.Running}} "* ]]; then printf 'true\n'; exit 0; fi
if [[ " $* " == *" inspect -f {{.State.StartedAt}} "* ]]; then printf '2026-01-01T00:00:00Z\n'; exit 0; fi
if [[ "$1" == "images" ]]; then
  for tag in cur prev old1 old2 latest '<none>'; do printf '%s:%s\n' "${*: -1}" "${tag}"; done
  exit 0
fi
if [[ " $* " == *" run --rm migrate "* ]]; then [[ "${MOCK_MIGRATE_FAIL:-0}" == "0" ]]; exit; fi
exit 0
SH
chmod +x "${scratch}/bin/"*

run_deploy() {
  env PATH="${scratch}/bin:${PATH}" PROJECT_DIR="${scratch}" ENV_FILE="${scratch}/env" \
    RELEASE_FILE="${scratch}/release" MOCK_DOCKER_LOG="${scratch}/docker.log" \
    "$@" "${scratch}/scripts/deploy.sh" >"${scratch}/out" 2>"${scratch}/err"
}
fail() { echo "FAIL: $*" >&2; cat "${scratch}/err" "${scratch}/docker.log" >&2; exit 1; }
line_of() { grep -n -- "$1" "${scratch}/docker.log" | head -1 | cut -d: -f1; }

# A CI deploy: both images pulled from GHCR, nothing built on the box, the old
# stack stopped before migrate and the new one started only after it.
: >"${scratch}/docker.log"
run_deploy IMAGE_TAG=cur || fail "CI deploy did not succeed"
grep -qx 'pull ghcr.io/parhambeik/portfolio-backend:cur' "${scratch}/docker.log" || fail "backend not pulled"
grep -qx 'pull ghcr.io/parhambeik/portfolio-frontend:cur' "${scratch}/docker.log" || fail "frontend not pulled"
grep -qx 'tag ghcr.io/parhambeik/portfolio-backend:cur portfolio-saas-backend:latest' "${scratch}/docker.log" || fail "backend not tagged"
grep -qx 'tag ghcr.io/parhambeik/portfolio-frontend:cur portfolio-saas-frontend:latest' "${scratch}/docker.log" || fail "frontend not tagged"
grep -q ' build' "${scratch}/docker.log" && fail "built on the box despite IMAGE_TAG"
stop_at="$(line_of ' stop celery_beat')"; migrate_at="$(line_of ' run --rm migrate')"; up_at="$(line_of ' up -d --remove-orphans')"
[[ -n "${stop_at}" && -n "${migrate_at}" && -n "${up_at}" ]] || fail "stop, migrate or up missing"
(( stop_at < migrate_at && migrate_at < up_at )) || fail "stop -> migrate -> up out of order"

# Pruning keeps the release now serving and the previous one, removes older
# SHAs, and never reaches for a blanket prune that would hit other stacks.
for repo in backend frontend; do
  for tag in old1 old2; do
    grep -qx "rmi ghcr.io/parhambeik/portfolio-${repo}:${tag}" "${scratch}/docker.log" || fail "${repo}:${tag} not pruned"
  done
  for tag in cur prev latest '<none>'; do
    grep -qx "rmi ghcr.io/parhambeik/portfolio-${repo}:${tag}" "${scratch}/docker.log" && fail "${repo}:${tag} was pruned"
  done
done
(( "$(line_of 'rmi ')" > up_at )) || fail "pruned before the new stack was up"
grep -q 'image prune\|system prune' "${scratch}/docker.log" && fail "blanket prune would touch other stacks"

# A failed migration leaves the new stack unstarted and prunes nothing.
: >"${scratch}/docker.log"
run_deploy IMAGE_TAG=cur MOCK_MIGRATE_FAIL=1 && fail "failed migration was accepted"
grep -q ' up -d --remove-orphans' "${scratch}/docker.log" && fail "new stack started after a failed migration"
grep -q '^rmi ' "${scratch}/docker.log" && fail "pruned after a failed migration"

# A manual deploy with only BACKEND_IMAGE builds just the frontend.
: >"${scratch}/docker.log"
run_deploy BACKEND_IMAGE=ghcr.io/example/portfolio-backend:abc || fail "BACKEND_IMAGE deploy did not succeed"
grep -qx 'pull ghcr.io/example/portfolio-backend:abc' "${scratch}/docker.log" || fail "BACKEND_IMAGE not pulled"
grep -q ' build frontend$' "${scratch}/docker.log" || fail "frontend not built"
grep -q ' build$' "${scratch}/docker.log" && fail "backend rebuilt despite BACKEND_IMAGE"
grep -q '^rmi ' "${scratch}/docker.log" && fail "pruned without IMAGE_TAG"

# The watchdog yields while a deploy holds the env-file lock.
: >"${scratch}/docker.log"
PATH="${scratch}/bin:${PATH}" PROJECT_DIR="${scratch}" ENV_FILE="${scratch}/env" \
  MOCK_DOCKER_LOG="${scratch}/docker.log" flock -x "${scratch}/env" \
  bash "${scratch}/scripts/watchdog_prices.sh" >"${scratch}/out" 2>"${scratch}/err"
grep -q 'deployment in progress' "${scratch}/out" || fail "watchdog did not yield to the deploy lock"
[[ ! -s "${scratch}/docker.log" ]] || fail "watchdog touched Docker during a deploy"

echo "deploy tests passed"
