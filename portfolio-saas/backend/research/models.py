"""Per-user research evidence and shared provider spend, separate from market data."""

from django.conf import settings
from django.db import models


class ResearchBudgetDay(models.Model):
    date = models.DateField(unique=True)
    reserved_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    spent_usd = models.DecimalField(max_digits=12, decimal_places=6, default=0)


class ResearchRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        ANSWERED = "answered", "Answered"
        ABSTAINED = "abstained", "Abstained"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    symbol = models.CharField(max_length=64)
    question = models.TextField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    max_cost_usd = models.DecimalField(max_digits=8, decimal_places=6)
    reserved_usd = models.DecimalField(max_digits=8, decimal_places=6, default=0)
    actual_cost_usd = models.DecimalField(max_digits=8, decimal_places=6, null=True, blank=True)
    cost_basis = models.CharField(max_length=24, blank=True, default="")
    provider_model = models.CharField(max_length=128, blank=True, default="")
    tokens_in = models.PositiveIntegerField(null=True, blank=True)
    tokens_out = models.PositiveIntegerField(null=True, blank=True)
    selected_observations = models.JSONField(default=list)
    evidence = models.JSONField(default=dict)
    failure_code = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["user", "-created_at"])]
