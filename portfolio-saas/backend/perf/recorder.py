"""Hot-path accumulator: one Redis round trip per observation.

Every observation (server request, browser page load, browser API call) lands
in an hourly Redis hash as running totals, under a field prefix naming its
dimensions. `flush_perf_rollups` copies those totals into `RequestPerfRollup`.

Recording must never fail or slow the request it measures: every Redis error
is swallowed, and without Redis (tests, bare local dev) observations go to an
in-process store with the same shape so the flush path is still exercised.
"""
import logging
import threading
import time
from datetime import datetime, timezone as dt_timezone

from portfolio.live.redis_client import get_redis

logger = logging.getLogger(__name__)

# Upper bounds (ms) of the latency histogram; the last bucket is open-ended.
# Stored histograms are positional, so only ever APPEND a bound.
BUCKETS_MS = (10, 25, 50, 100, 200, 350, 500, 750, 1000, 1500, 2500, 4000, 6000, 10000, 20000, 30000)
HIST_LEN = len(BUCKETS_MS) + 1

KEY_PREFIX = "perf:h:"
KEY_TTL_SECONDS = 3 * 24 * 3600
SEP = "\x1f"

METRICS = ("count", "errors", "client_errors", "sum_ms", "max_ms",
           "sum_db_queries", "sum_db_ms", "sum_inflight")

_LUA = """
local k, p = KEYS[1], ARGV[1]
local ms = tonumber(ARGV[2])
redis.call('HINCRBY', k, p .. 'count', 1)
if ARGV[3] ~= '0' then redis.call('HINCRBY', k, p .. 'errors', 1) end
if ARGV[4] ~= '0' then redis.call('HINCRBY', k, p .. 'client_errors', 1) end
redis.call('HINCRBYFLOAT', k, p .. 'sum_ms', ARGV[2])
local cur = tonumber(redis.call('HGET', k, p .. 'max_ms') or '0')
if ms > cur then redis.call('HSET', k, p .. 'max_ms', ARGV[2]) end
if ARGV[5] ~= '0' then redis.call('HINCRBY', k, p .. 'sum_db_queries', ARGV[5]) end
if ARGV[6] ~= '0' then redis.call('HINCRBYFLOAT', k, p .. 'sum_db_ms', ARGV[6]) end
if ARGV[7] ~= '0' then redis.call('HINCRBY', k, p .. 'sum_inflight', ARGV[7]) end
redis.call('HINCRBY', k, p .. 'h' .. ARGV[8], 1)
redis.call('EXPIRE', k, tonumber(ARGV[9]))
return 1
"""

_script = None
_script_client = None
_local_store: dict[int, dict[str, float]] = {}
_local_lock = threading.Lock()
_last_error_log = 0.0


def hour_bucket(ts: float | None = None) -> int:
    """Epoch seconds of the UTC hour containing `ts`."""
    ts = time.time() if ts is None else ts
    return int(ts // 3600 * 3600)


def bucket_datetime(bucket: int) -> datetime:
    return datetime.fromtimestamp(bucket, tz=dt_timezone.utc)


def histogram_index(ms: float) -> int:
    for index, bound in enumerate(BUCKETS_MS):
        if ms <= bound:
            return index
    return len(BUCKETS_MS)


def field_prefix(source: str, route: str, method: str, market_state: str) -> str:
    return SEP.join((source, route, method, market_state)) + SEP


def _get_script(client):
    global _script, _script_client
    if _script is None or _script_client is not client:
        _script = client.register_script(_LUA)
        _script_client = client
    return _script


def record(*, source, route, method="", market_state="", ms, status=200,
           db_queries=0, db_ms=0.0, inflight=0, ts=None):
    """Add one observation. Never raises."""
    global _last_error_log
    ms = max(0.0, float(ms))
    bucket = hour_bucket(ts)
    prefix = field_prefix(source, route, method, market_state)
    is_error = 1 if (status is None or status >= 500 or status == 0) else 0
    is_client_error = 1 if status is not None and 400 <= status < 500 else 0
    hist_index = histogram_index(ms)
    try:
        client = get_redis()
        if client is None:
            _record_local(bucket, prefix, ms, is_error, is_client_error,
                          db_queries, db_ms, inflight, hist_index)
            return
        _get_script(client)(
            keys=[f"{KEY_PREFIX}{bucket}"],
            args=[prefix, f"{ms:.3f}", is_error, is_client_error, int(db_queries),
                  f"{float(db_ms):.3f}" if db_ms else "0", int(inflight),
                  hist_index, KEY_TTL_SECONDS],
        )
    except Exception as exc:  # pragma: no cover - depends on Redis failing
        now = time.monotonic()
        if now - _last_error_log > 60:
            _last_error_log = now
            logger.warning("perf: could not record observation: %s", exc)


def _record_local(bucket, prefix, ms, is_error, is_client_error, db_queries,
                  db_ms, inflight, hist_index):
    with _local_lock:
        store = _local_store.setdefault(bucket, {})

        def add(name, value):
            store[prefix + name] = store.get(prefix + name, 0) + value

        add("count", 1)
        add("errors", is_error)
        add("client_errors", is_client_error)
        add("sum_ms", ms)
        store[prefix + "max_ms"] = max(store.get(prefix + "max_ms", 0), ms)
        add("sum_db_queries", int(db_queries))
        add("sum_db_ms", float(db_ms))
        add("sum_inflight", int(inflight))
        add(f"h{hist_index}", 1)


def read_bucket(bucket: int) -> dict[tuple, dict]:
    """Return {(source, route, method, state): {metric: value, "hist": [...]}}."""
    client = get_redis()
    if client is None:
        with _local_lock:
            raw = dict(_local_store.get(bucket, {}))
    else:
        raw = client.hgetall(f"{KEY_PREFIX}{bucket}") or {}

    rows: dict[tuple, dict] = {}
    for field, value in raw.items():
        if isinstance(field, bytes):
            field = field.decode()
        parts = field.split(SEP)
        if len(parts) != 5:
            continue
        dims, metric = tuple(parts[:4]), parts[4]
        row = rows.setdefault(dims, {"hist": [0] * HIST_LEN})
        number = float(value)
        if metric.startswith("h") and metric[1:].isdigit():
            index = int(metric[1:])
            if index < HIST_LEN:
                row["hist"][index] = int(number)
        elif metric in METRICS:
            row[metric] = number
    return rows


def reset_local_store():
    """Test helper."""
    with _local_lock:
        _local_store.clear()


def percentile_from_hist(hist, fraction: float):
    """Upper bound (ms) of the bucket holding the given percentile, or None.

    Coarse by construction -- it reports "p95 <= 750 ms", never an exact value.
    The open-ended top bucket reports the largest finite bound.
    """
    total = sum(hist or [])
    if not total:
        return None
    target = fraction * total
    running = 0
    for index, count in enumerate(hist):
        running += count
        if running >= target:
            return BUCKETS_MS[min(index, len(BUCKETS_MS) - 1)]
    return BUCKETS_MS[-1]
