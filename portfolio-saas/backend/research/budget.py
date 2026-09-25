"""PostgreSQL spend reservation, shared across web workers and users."""

from decimal import Decimal, ROUND_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from marketdata.jalali import TEHRAN

from .models import ResearchBudgetDay, ResearchRun


class BudgetExceeded(RuntimeError):
    pass


def _today():
    return timezone.localdate(timezone.now(), TEHRAN)


@transaction.atomic
def reserve(user, symbol, question, max_cost_usd, estimated_cost, model, evidence):
    day, _ = ResearchBudgetDay.objects.select_for_update().get_or_create(date=_today())
    if estimated_cost > max_cost_usd:
        raise BudgetExceeded("run_ceiling_below_one_call_reservation")
    if day.reserved_usd + day.spent_usd + estimated_cost > settings.RESEARCH_DAILY_BUDGET_USD:
        raise BudgetExceeded("daily_research_budget_exhausted")
    day.reserved_usd += estimated_cost
    day.save(update_fields=["reserved_usd"])
    return ResearchRun.objects.create(
        user=user, symbol=symbol, question=question,
        max_cost_usd=max_cost_usd, reserved_usd=estimated_cost,
        provider_model=model, evidence=evidence,
    )


@transaction.atomic
def finish(run, result=None, selected=None, failure_code=""):
    day = ResearchBudgetDay.objects.select_for_update().get(date=timezone.localtime(run.created_at, TEHRAN).date())
    run.finished_at = timezone.now()
    if result is None:
        # A timed-out or malformed response might still have been billed. Keep
        # the reservation until the day closes; never silently refund unknown spend.
        run.status = ResearchRun.Status.FAILED
        run.failure_code = failure_code
        run.cost_basis = "unknown_reserved"
        run.save(update_fields=["status", "failure_code", "cost_basis", "finished_at"])
        return run
    actual = result.actual_cost_usd.quantize(Decimal("0.000001"), rounding=ROUND_UP)
    day.reserved_usd -= run.reserved_usd
    day.spent_usd += actual
    day.save(update_fields=["reserved_usd", "spent_usd"])
    run.actual_cost_usd = actual
    run.cost_basis = result.cost_basis
    run.tokens_in = result.tokens_in
    run.tokens_out = result.tokens_out
    if actual > run.max_cost_usd:
        failure_code = "provider_cost_exceeded_run_ceiling"
        selected = []
    run.selected_observations = selected or []
    run.status = (
        ResearchRun.Status.FAILED if failure_code
        else ResearchRun.Status.ANSWERED if selected else ResearchRun.Status.ABSTAINED
    )
    run.failure_code = failure_code
    run.save(update_fields=[
        "actual_cost_usd", "cost_basis", "tokens_in", "tokens_out",
        "selected_observations", "status", "failure_code", "finished_at",
    ])
    return run
