#!/usr/bin/env bash
# Lighthouse against the production image, then assert the headers Lighthouse
# cannot see from a category score.
#
# Two checks, because they fail differently. Lighthouse scores what a browser
# experiences; `assert_header` proves the nginx config that produced it is the
# one that ships. A category score of 100 from a server with no CSP is a green
# build that lies, and the whole point of the gate is that a red build means
# something.
set -Eeuo pipefail

cd "$(dirname "$0")/.."

IMAGE=portfolio-frontend-lh
NAME=portfolio-frontend-lh-run
STUB=portfolio-backend-lh-stub
NET=portfolio-lh-net
PORT=8088

cleanup() {
  docker rm -f "$NAME" "$STUB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  rm -f /tmp/lh-stub.conf
}
trap cleanup EXIT
cleanup

echo "==> building the production image"
docker build -q -f Dockerfile.prod -t "$IMAGE" . >/dev/null

# A stub for the API, so the audit measures the frontend and not the absence of
# a backend. Without it nginx proxies /api to nothing, the 502 is logged as a
# browser console error, and Lighthouse drops Best Practices to 0.96 for a
# reason that does not exist in production.
#
# 401 rather than 200 on purpose: that is the honest answer to a session-restore
# call with no cookie, and it is the path the app already handles (App.jsx treats
# anonymous as a normal state). Faking a signed-in user would audit a page the
# visitor never sees.
echo "==> starting an API stub"
cat > /tmp/lh-stub.conf <<'CONF'
server {
    listen 8000;
    default_type application/json;
    location / { return 401 '{"detail":"Authentication credentials were not provided."}'; }
}
CONF
docker network create "$NET" >/dev/null
docker run -d --name "$STUB" --network "$NET" --network-alias backend \
  -v /tmp/lh-stub.conf:/etc/nginx/conf.d/default.conf:ro nginx:alpine >/dev/null

echo "==> starting the frontend on :$PORT"
docker run -d --name "$NAME" --network "$NET" -p "$PORT:80" "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
  curl -fsS -o /dev/null "http://localhost:$PORT/" 2>/dev/null && break
  sleep 1
done
curl -fsS -o /dev/null "http://localhost:$PORT/" || { echo "container never became ready"; docker logs "$NAME"; exit 1; }

fail=0
assert_header() {
  local url="$1" header="$2" expect="${3:-}"
  local value
  value=$(curl -sI -H 'Accept-Encoding: gzip' "$url" | tr -d '\r' | grep -i "^$header:" || true)
  if [ -z "$value" ]; then
    echo "  MISSING  $header  on $url"
    fail=1
  elif [ -n "$expect" ] && ! grep -qi -- "$expect" <<<"$value"; then
    echo "  WRONG    $header  on $url -> $value (expected to contain '$expect')"
    fail=1
  else
    echo "  ok       $header  on $url"
  fi
}

echo "==> response headers on the document"
DOC="http://localhost:$PORT/"
assert_header "$DOC" "content-security-policy" "script-src 'self'"
assert_header "$DOC" "strict-transport-security" "max-age=31536000"
assert_header "$DOC" "x-content-type-options" "nosniff"
assert_header "$DOC" "referrer-policy"
assert_header "$DOC" "cross-origin-opener-policy" "same-origin"
# The shell is the only file whose name survives a deploy, so it must be
# revalidated. Without this a browser's heuristic freshness can keep serving the
# previous index.html, which names asset hashes the new deploy no longer has.
assert_header "$DOC" "cache-control" "no-cache"

echo "==> response headers and compression on a hashed asset"
# The nginx `add_header` inheritance trap: `location /assets/` sets its own
# Cache-Control, which silently drops every header inherited from the server
# block unless the snippet is re-included there. This is the check that catches
# that regression, and it is the reason the headers live in a snippet at all.
ASSET_PATH=$(docker exec "$NAME" sh -c 'ls /usr/share/nginx/html/assets/*.js | head -1' | sed 's#/usr/share/nginx/html##')
ASSET="http://localhost:$PORT$ASSET_PATH"
echo "  asset: $ASSET_PATH"
assert_header "$ASSET" "content-encoding" "gzip"
assert_header "$ASSET" "cache-control" "immutable"
assert_header "$ASSET" "content-security-policy" "script-src 'self'"

[ "$fail" -eq 0 ] || { echo "header assertions failed"; exit 1; }

echo "==> lighthouse"
npx --yes @lhci/cli@0.14.x autorun
