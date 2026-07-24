"""Database-backed & thread-safe log event buffer for real-time SSE streaming to the Admin Portal."""
import collections
import logging
import threading
import time
import os
from datetime import datetime, timezone

SERVICE_NAME = os.environ.get("SERVICE_NAME", "backend")

_LOG_BUFFER = collections.deque(maxlen=300)
_LOCK = threading.Lock()


class SystemLogHandler(logging.Handler):
    """Logging handler that captures system diagnostics across all Celery & Django processes."""

    def emit(self, record):
        if getattr(record, "_logged_to_system_log", False):
            return
        record._logged_to_system_log = True
        try:
            msg = self.format(record)
            if not msg:
                return

            category = "GENERAL"
            if "[PRICE_SPIKE_BLOCKED]" in msg:
                category = "SPIKE_BLOCKED"
            elif "[LIVE_PRICE_FORWARD_FILL]" in msg:
                category = "FORWARD_FILL"
            elif "[BRS_FETCH_ERROR]" in msg or "[TSETMC_FETCH_ERROR]" in msg or "[ARCHIVE_FETCH_ERROR]" in msg:
                category = "FETCH_ERROR"
            elif "[QUOTA" in msg or "quota" in msg.lower():
                category = "QUOTA"
            elif "[INGEST" in msg or "verified complete" in msg or "archive_tick" in msg:
                category = "INGEST"
            elif "ERROR" in record.levelname:
                category = "SYSTEM_ERROR"
            elif "WARNING" in record.levelname or "WARN" in record.levelname:
                category = "SYSTEM_WARN"

            iso_ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat()
            entry = {
                "id": f"{time.time()}:{record.created}",
                "timestamp": iso_ts,
                "level": record.levelname,
                "category": category,
                "logger": record.name,
                "message": msg,
                "service": SERVICE_NAME,
            }

            with _LOCK:
                _LOG_BUFFER.append(entry)

            try:
                from marketdata.models import SystemLogEvent
                SystemLogEvent.objects.create(
                    level=record.levelname,
                    category=category,
                    logger_name=record.name[:100],
                    message=msg,
                    service=SERVICE_NAME,
                )
            except Exception:
                pass
        except Exception:
            self.handleError(record)


# Attach handler to root logger so all marketdata, portfolio, & celery logs are captured
handler = SystemLogHandler()
handler.setFormatter(logging.Formatter("%(message)s"))
handler.setLevel(logging.INFO)

for logger_name in ("", "portfolio", "marketdata", "celery.task", "celery"):
    lg = logging.getLogger(logger_name)
    if not any(isinstance(h, SystemLogHandler) for h in lg.handlers):
        lg.addHandler(handler)

try:
    from celery.signals import after_setup_logger, after_setup_task_logger

    @after_setup_logger.connect
    @after_setup_task_logger.connect
    def setup_celery_logging(logger=None, **kwargs):
        if logger and not any(isinstance(h, SystemLogHandler) for h in logger.handlers):
            logger.addHandler(handler)
except Exception:
    pass


def get_recent_logs(limit=100) -> list:
    """Retrieve the most recent system log entries from PostgreSQL database or fallback buffer."""
    try:
        from marketdata.models import SystemLogEvent
        db_logs = list(SystemLogEvent.objects.order_by("-timestamp")[:limit])
        if db_logs:
            return [
                {
                    "id": f"{log.id}",
                    "timestamp": log.timestamp.isoformat(),
                    "level": log.level,
                    "category": log.category,
                    "logger": log.logger_name,
                    "message": log.message,
                    "service": log.service,
                }
                for log in db_logs
            ]
    except Exception:
        pass

    with _LOCK:
        return list(reversed(_LOG_BUFFER))[:limit]
