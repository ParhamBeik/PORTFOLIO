"""Midnight-burst weekly probes.

`archive_maintenance` at 04:10 Tehran is after the archive burst empties
TSETMC. Claiming a probe then stamps `last_probe_at` and parks the state
another week with no call. This job runs at 00:05 while that wallet still
has remaining requests and takes at most two slots.
"""
from celery import shared_task


@shared_task(ignore_result=True)
def claim_burst_probes():
    from . import suspension
    from .quota import TSETMC, archive_capacity
    from .tasks import run_archive_state

    leftover = archive_capacity().get(TSETMC, 0)
    if leftover <= 0:
        return []
    probe_ids = suspension.claim_probe_batch(limit=min(2, leftover))
    for state_id in probe_ids:
        run_archive_state.si(state_id).apply_async()
    return probe_ids
