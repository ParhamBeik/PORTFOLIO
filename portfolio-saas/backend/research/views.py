"""Evidence-first research: one low-cost AI routing call, numeric claims from SQL."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from marketdata.explore_api import _instrument, _monthly_sales
from marketdata.jalali import TEHRAN, from_gregorian

from .budget import BudgetExceeded, finish, reserve
from .models import ResearchRun
from .observations import build_observations
from .provider import (
    ProviderFailure, load_config, reserve_estimate, route_question, routing_messages,
)


_UNSUPPORTED_TOPICS = (
    "profit", "margin", "net income", "balance sheet", "valuation", "usd", "dollar",
    "crypto", "industry", "peer", "سود", "حاشیه", "ترازنامه", "ارزش گذاری",
    "ارزش‌گذاری", "دلار", "صنعت", "رقیب", "رقبا", "رمزارز", "ارز دیجیتال",
)


def _abstain(user, symbol, question, ceiling, evidence, reason):
    run = ResearchRun.objects.create(
        user=user, symbol=symbol, question=question,
        max_cost_usd=ceiling, actual_cost_usd=Decimal(0), cost_basis="no_model_call",
        status=ResearchRun.Status.ABSTAINED, finished_at=timezone.now(),
        evidence=evidence, failure_code=reason,
    )
    return Response({
        "run_id": run.pk, "status": run.status,
        "reason": reason, "claims": [],
        "cost_usd": "0", "cost_basis": "no_model_call",
        "evidence": evidence,
    })


class ResearchSettingsView(APIView):
    def get(self, request):
        try:
            provider = load_config()
            state = "ready" if provider else "not_configured"
        except ProviderFailure:
            provider = None
            state = "invalid_config"
        return Response({
            "provider_status": state,
            "provider_model": provider.model if provider else None,
            "max_run_usd": str(settings.RESEARCH_MAX_RUN_USD),
            "daily_budget_usd": str(settings.RESEARCH_DAILY_BUDGET_USD),
            "default_run_usd": str(min(settings.RESEARCH_MAX_RUN_USD, Decimal("0.01"))),
            "supported_evidence": ["source_reconciled_monthly_sales"],
        })


class ResearchRunView(APIView):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "research"

    def post(self, request):
        symbol = str(request.data.get("symbol", "")).strip()
        question = str(request.data.get("question", "")).strip()
        if not symbol or len(symbol) > 64 or _instrument(symbol) is None:
            return Response({"symbol": "Choose an eligible TSE stock."}, status=400)
        if not 3 <= len(question) <= 600:
            return Response({"question": "Enter a question of 3–600 characters."}, status=400)
        try:
            ceiling = Decimal(str(request.data.get("max_cost_usd", "")))
        except (InvalidOperation, TypeError):
            return Response({"max_cost_usd": "Enter a USD amount."}, status=400)
        if (not ceiling.is_finite() or ceiling <= 0
                or ceiling > settings.RESEARCH_MAX_RUN_USD
                or ceiling.as_tuple().exponent < -6):
            return Response({"max_cost_usd": "Choose a positive amount within the server limit, to six decimals."}, status=400)

        today = timezone.localtime(timezone.now(), TEHRAN).date()
        monthly = _monthly_sales(
            symbol, from_gregorian(today - timedelta(days=365)), from_gregorian(today),
        )
        observations = build_observations(monthly)
        evidence = {
            "scope": "one_tse_company_monthly_sales_last_365_days",
            "monthly_sales": monthly,
        }
        if not monthly["points"]:
            return _abstain(request.user, symbol, question, ceiling, evidence, "no_verified_monthly_sales")
        if any(term in question.casefold() for term in _UNSUPPORTED_TOPICS):
            return _abstain(request.user, symbol, question, ceiling, evidence, "question_needs_uncertified_data")
        try:
            config = load_config()
        except ProviderFailure as exc:
            return Response({"provider": exc.code}, status=503)
        if config is None:
            return Response({"provider": "gapgpt_not_configured"}, status=503)
        catalog = [
            {"id": key, "description": item["description"]}
            for key, item in observations.items()
        ]
        messages = routing_messages(question, catalog)
        estimated = reserve_estimate(config, messages)
        try:
            run = reserve(request.user, symbol, question, ceiling, estimated, config.model, evidence)
        except BudgetExceeded as exc:
            return Response({"budget": str(exc), "reservation_usd": str(estimated)}, status=429)
        try:
            result = route_question(config, messages, set(observations))
        except ProviderFailure as exc:
            finish(run, result=exc.usage, failure_code=exc.code)
            return Response({
                "run_id": run.pk, "status": ResearchRun.Status.FAILED,
                "provider": exc.code, "cost_basis": run.cost_basis,
                "cost_usd": str(run.actual_cost_usd) if run.actual_cost_usd is not None else None,
                "reserved_usd": str(run.reserved_usd),
            }, status=502)
        selected = result.selected_ids if result.supported else []
        claims = [
            {"id": key, "statement": observations[key]["statement"], "sources": observations[key]["sources"]}
            for key in selected
        ]
        finish(run, result=result, selected=claims)
        if run.status == ResearchRun.Status.FAILED:
            return Response({
                "run_id": run.pk, "status": run.status,
                "budget": run.failure_code, "cost_usd": str(run.actual_cost_usd),
                "cost_basis": run.cost_basis,
            }, status=502)
        return Response({
            "run_id": run.pk,
            "status": run.status,
            "claims": claims,
            "reason": "unsupported_by_verified_tools" if not claims else "",
            "cost_usd": str(run.actual_cost_usd),
            "cost_basis": run.cost_basis,
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
            "provider_model": run.provider_model,
            "coverage": {
                "latest_filing_periods": monthly["latest_filing_periods"],
                "verified_periods": monthly["verified_periods"],
                "withheld_periods": monthly["withheld_periods"],
            },
        })


class ResearchRunDetailView(APIView):
    def get(self, request, run_id):
        run = ResearchRun.objects.filter(pk=run_id, user=request.user).first()
        if run is None:
            raise NotFound("Research run not found.")
        return Response({
            "run_id": run.pk,
            "created_at": run.created_at.isoformat(),
            "symbol": run.symbol,
            "question": run.question,
            "status": run.status,
            "claims": run.selected_observations,
            "evidence": run.evidence,
            "failure_code": run.failure_code,
            "max_cost_usd": str(run.max_cost_usd),
            "reserved_usd": str(run.reserved_usd),
            "cost_usd": str(run.actual_cost_usd) if run.actual_cost_usd is not None else None,
            "cost_basis": run.cost_basis,
            "provider_model": run.provider_model,
            "tokens_in": run.tokens_in,
            "tokens_out": run.tokens_out,
        })
