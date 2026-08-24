"""Peer-relative outlier suspension for `ArchiveFetchState` rows.

A symbol is only suspect *relative to its peers on the same endpoint* --
never on an absolute threshold. If every symbol on an endpoint is failing,
the provider is down, not the symbols; suspending all of them would erase
active fetch coverage for no reason. So detection is two-stage:

1. An outage guard: if a large share of an endpoint's states are already at
   or above the failure floor, refuse to suspend anything on that endpoint.
2. Otherwise, flag a state only if it clears an absolute floor (so a quiet
   endpoint with a handful of states can't produce noise-driven outliers)
   *and* sits above a Tukey IQR fence computed from its own peers.

The detection math (`_outlier_candidates`) is a pure function over plain
values -- no DB access -- so it is unit-testable without constructing model
rows. The `scan_endpoint_for_outliers` / `suspend_outliers` wrappers are the
only pieces that touch the database.

Suspension is reversible and never deletes a row: it only sets
`suspended_at`/`suspension_reason`/`suspension_evidence` on the existing
`ArchiveFetchState`. Recovery is two paths: a weekly lowest-priority probe
that clears suspension on clean data, and a manual operator override
(force-retry or permanent blacklist).
"""
import statistics
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import ArchiveFetchState

# --- Detection thresholds -----------------------------------------------

# Below this many consecutive failures, a state is not even a candidate --
# matches the existing "wedged" signal already used elsewhere in the archive
# scheduler (consecutive_failures >= 5), so suspension kicks in exactly where
# an operator would already call a state stuck.
MIN_ABSOLUTE_FAILURES = 5

# Need at least this many peer states on an endpoint before trusting a
# distribution computed from them. Below this, quartiles are noise and any
# single failing state looks like an "outlier" by definition.
MIN_PEER_SAMPLE = 5

# Classic Tukey fence multiplier (Q3 + 1.5*IQR) for "far enough above the pack
# to be a genuine outlier, not just worse luck". Standard default, not hand-fit.
OUTLIER_IQR_MULTIPLIER = 1.5

# If this share (or more) of an endpoint's states are already at/above the
# failure floor, treat it as a provider-wide outage and suspend nobody on
# that endpoint this pass. Configurable because different endpoints may want
# a stricter or looser bar for "is this actually the provider".
OUTAGE_SHARE_THRESHOLD = 0.5

# Weekly lowest-priority auto-probe cadence for suspended (non-blacklisted) states.
PROBE_INTERVAL = timedelta(days=7)

REASON_PEER_OUTLIER = "peer_outlier"
REASON_OPERATOR_BLACKLIST = "operator_blacklist"


def _disabled_endpoints():
    # Imported lazily: archive.py imports nothing from here, and a module-level
    # import would close that loop.
    from .archive import disabled_endpoints

    return disabled_endpoints()


@dataclass
class SuspensionCandidate:
    state_id: int
    symbol: str
    value: int
    peer_median: float
    peer_q1: float
    peer_q3: float
    sample_size: int
    evidence: dict = field(init=False)

    def __post_init__(self):
        self.evidence = {
            "signal": "consecutive_failures",
            "value": self.value,
            "peer_median": self.peer_median,
            "peer_q1": self.peer_q1,
            "peer_q3": self.peer_q3,
            "sample_size": self.sample_size,
        }


def _outlier_candidates(peers):
    """Pure detection: `peers` is an iterable of (state_id, symbol, consecutive_failures).

    Returns a list of `SuspensionCandidate`, or [] if the sample is too small
    or looks like a provider-wide outage. No DB access -- unit-testable directly.
    """
    peers = list(peers)
    n = len(peers)
    if n < MIN_PEER_SAMPLE:
        return []

    failures = sorted(value for _, _, value in peers)
    failing_share = sum(1 for f in failures if f >= MIN_ABSOLUTE_FAILURES) / n
    if failing_share >= OUTAGE_SHARE_THRESHOLD:
        return []  # provider-wide outage guard: most peers are broken too

    q1, median, q3 = statistics.quantiles(failures, n=4, method="inclusive")
    fence = q3 + OUTLIER_IQR_MULTIPLIER * (q3 - q1)

    candidates = []
    for state_id, symbol, value in peers:
        if value < MIN_ABSOLUTE_FAILURES or value <= fence:
            continue
        candidates.append(SuspensionCandidate(
            state_id=state_id, symbol=symbol, value=value,
            peer_median=median, peer_q1=q1, peer_q3=q3, sample_size=n,
        ))
    return candidates


def scan_endpoint_for_outliers(endpoint):
    """DB-backed wrapper: peer set is every non-suspended, non-blacklisted state
    on `endpoint`. Read-only -- does not mutate anything."""
    peers = ArchiveFetchState.objects.filter(
        endpoint=endpoint, blacklisted=False, suspended_at__isnull=True,
    ).values_list("id", "symbol", "consecutive_failures")
    return _outlier_candidates(peers)


def suspend_outliers(endpoint=None, *, now=None):
    """Detect and suspend peer-relative outliers, one endpoint or all of them.

    Intended as a periodic (e.g. nightly) sweep -- see module docstring at the
    call site in archive.py for suggested wiring. Returns the list of suspended
    state ids.
    """
    now = now or timezone.now()
    endpoints = (
        [endpoint] if endpoint else
        list(ArchiveFetchState.objects.order_by().values_list("endpoint", flat=True).distinct())
    )
    suspended = []
    for ep in endpoints:
        for candidate in scan_endpoint_for_outliers(ep):
            updated = ArchiveFetchState.objects.filter(
                pk=candidate.state_id, suspended_at__isnull=True, blacklisted=False,
            ).update(
                suspended_at=now,
                suspension_reason=REASON_PEER_OUTLIER,
                suspension_evidence=candidate.evidence,
            )
            if updated:
                suspended.append(candidate.state_id)
    return suspended


# --- Recovery -------------------------------------------------------------

def claim_probe_batch(limit=5, *, now=None):
    """Lease up to `limit` suspended, non-blacklisted states due for a weekly probe.

    Mirrors `claim_archive_maintenance`'s select_for_update/skip_locked lease
    pattern in archive.py so probe claims never race real archive claims for
    the same rows. Callers should feed the returned pks through the normal
    `run_archive_state()` fetch path (a probe IS a real fetch, just the
    lowest-priority one) and then call `try_recover()` on the result.
    """
    now = now or timezone.now()
    cutoff = now - PROBE_INTERVAL
    due = Q(last_probe_at__isnull=True) | Q(last_probe_at__lte=cutoff)
    with transaction.atomic():
        states = list(
            ArchiveFetchState.objects.select_for_update(skip_locked=True)
            .filter(due, suspended_at__isnull=False, blacklisted=False)
            # The third lease path, and the one most likely to hold a disabled
            # endpoint's rows: a subsystem is usually switched off *because* it
            # kept failing, and repeated failure is what suspends a state in the
            # first place. Production carries 10 suspended Codal states, which
            # this would otherwise re-probe every week.
            .exclude(endpoint__in=_disabled_endpoints())
            .order_by("last_probe_at")[:limit]
        )
        ArchiveFetchState.objects.filter(pk__in=[s.pk for s in states]).update(
            last_probe_at=now,
        )
    return [s.pk for s in states]


def try_recover(state, *, now=None):
    """Un-suspend a probed state if its fetch came back clean.

    Call after `run_archive_state()` has updated `state` for a pk that came
    from `claim_probe_batch`. Returns True if the state was recovered.
    """
    if not state.suspended_at or state.blacklisted:
        return False
    if state.verified_complete and state.consecutive_failures == 0:
        unsuspend(state, now=now)
        return True
    return False


# --- Manual override (for the ops admin API) -------------------------------

def unsuspend(state, *, now=None):
    """Clear suspension state and reset the failure counter. Never deletes the row."""
    state.suspended_at = None
    state.suspension_reason = ""
    state.suspension_evidence = {}
    state.consecutive_failures = 0
    state.save(update_fields=[
        "suspended_at", "suspension_reason", "suspension_evidence", "consecutive_failures",
    ])


def force_retry(state, *, now=None):
    """Operator override: clear suspension and schedule an immediate retry."""
    now = now or timezone.now()
    unsuspend(state, now=now)
    state.next_attempt_at = now
    state.save(update_fields=["next_attempt_at"])


def blacklist(state, *, now=None):
    """Operator override: this pair is never coming back. Stays suspended
    (excluded from normal claiming) but is also excluded from the weekly probe."""
    now = now or timezone.now()
    state.blacklisted = True
    if not state.suspended_at:
        state.suspended_at = now
        state.suspension_reason = REASON_OPERATOR_BLACKLIST
    state.save(update_fields=["blacklisted", "suspended_at", "suspension_reason"])


def unblacklist(state):
    """Lift the blacklist. Leaves any existing suspension in place -- the state
    still needs a clean probe or a manual `force_retry` to resume fetching."""
    state.blacklisted = False
    state.save(update_fields=["blacklisted"])
