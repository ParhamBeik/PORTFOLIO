from celery import shared_task

from . import report


@shared_task(name="perf.tasks.flush_perf_rollups", ignore_result=True)
def flush_perf_rollups():
    """Copy the hourly Redis latency totals into RequestPerfRollup (idempotent)."""
    return report.flush()
