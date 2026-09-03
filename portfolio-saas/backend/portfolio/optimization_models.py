from django.db import models
from django.conf import settings
from django.utils import timezone

try:
    # Django 3.1+: JSONField on core
    from django.db.models import JSONField
except Exception:
    # Fallback for older Django versions
    from django.contrib.postgres.fields import JSONField


class OptimizationSnapshot(models.Model):
    """Persisted optimization payload produced by the optimizer.

    Stores the raw payload (the same dict returned by portfolio.services.optimize)
    so the historical timeline can be displayed in the frontend and the admin can
    inspect runs. `account` is nullable — a null account denotes a global-market
    optimization snapshot.
    """

    SCENARIO_CHOICES = (
        ("max_sharpe", "Max Sharpe"),
        ("min_volatility", "Min Volatility"),
        ("equal_weight", "Equal Weight"),
        ("risk_parity", "Risk Parity"),
        ("hrp", "HRP"),
        ("my_optimal", "My Optimal"),
    )

    id = models.AutoField(primary_key=True)
    account = models.ForeignKey(
        "portfolio.Account",
        on_delete=models.CASCADE,
        related_name="optimization_snapshots",
        null=True,
        blank=True,
    )
    scenario = models.CharField(max_length=32, choices=SCENARIO_CHOICES, default="max_sharpe")
    basis = models.CharField(max_length=32, default="real_toman")
    # Lookback window this snapshot was optimized over (365/1095/1825/3650 for
    # the "Best Possible Portfolio Overall" page). Queryable so the view can
    # fetch "the latest snapshot per (window_days, scenario)" without parsing
    # the payload JSON.
    window_days = models.PositiveIntegerField(default=180, db_index=True)
    payload = JSONField()
    price_version = models.CharField(max_length=64, blank=True, default="")
    as_of = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_optimization_snapshots",
    )
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "scenario", "basis", "-created_at"], name="opt_snap_lookup_idx"),
        ]

    def __str__(self):
        acct = f"account={self.account_id}" if self.account_id else "global"
        return f"OptimizationSnapshot({self.scenario}/{self.basis}) {acct} @ {self.created_at.isoformat()}"