"""Evidence-first research: one low-cost AI routing call, numeric claims from SQL."""

from datetime import timedelta
from decimal import Decimal, InvalidOperation
import re

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from marketdata.explore_api import _balance_sheets, _income_statements, _instrument, _monthly_sales
from marketdata.jalali import TEHRAN, from_gregorian

from .budget import BudgetExceeded, finish, reserve
from .models import ResearchRun
from .observations import build_observations
from .provider import (
    ProviderFailure, load_config, reserve_estimate, route_question, routing_messages,
)


_UNSUPPORTED_TOPICS = (
    "valuation", "usd", "dollar", "debt", "cash flow", "cashflow",
    "crypto", "industry", "peer", "dividend", "ارزش گذاری", "سود نقدی",
    "ارزش‌گذاری", "دلار", "صنعت", "رقیب", "رقبا", "رمزارز", "ارز دیجیتال",
    "جریان وجوه", "جریان نقد",
)
_INCOME_TOPICS = ("profit", "margin", "net income", "operating revenue", "سود", "حاشیه")
_BALANCE_TOPICS = (
    "balance sheet", "asset", "liabilit", "equity", "cash", "borrowing",
    "debt", "ترازنامه", "دارایی", "بدهی", "حقوق مالکانه", "موجودی نقد", "تسهیلات",
)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _matching_period_observations(question, observations):
    """An explicit date cannot be answered with a different filing's year/month."""
    text = question.translate(_DIGITS)
    years = set(re.findall(r"(?<!\d)(?:13|14|20)\d{2}(?!\d)", text))
    months = {
        f"{year}-{int(month):02d}"
        for year, month in re.findall(r"(?<!\d)((?:13|14)\d{2})[-/](\d{1,2})(?:[-/]\d{1,2})?(?!\d)", text)
        if 1 <= int(month) <= 12
    }
    if not years and not months:
        return observations
    return {
        key: value for key, value in observations.items()
        if value["sources"] and years.issubset({
            source["period_end_jalali"][:4] for source in value["sources"]
        }) and months.issubset({
            source["period_end_jalali"][:7] for source in value["sources"]
        })
    }


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


def _current_evidence(symbol):
    today = timezone.localtime(timezone.now(), TEHRAN).date()
    end = from_gregorian(today)
    return (
        _monthly_sales(symbol, from_gregorian(today - timedelta(days=365)), end),
        _income_statements(symbol, from_gregorian(today - timedelta(days=3650)), end),
        _balance_sheets(symbol, from_gregorian(today - timedelta(days=3650)), end),
    )


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
            "supported_evidence": [
                "source_reconciled_monthly_sales", "source_reconciled_income_statements",
                "source_reconciled_balance_sheets",
            ],
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

        monthly, income, balance = _current_evidence(symbol)
        observations = build_observations(monthly, income, balance)
        evidence = {
            "scope": "one_tse_company_sales_365_days_statements_3650_days",
            "monthly_sales": monthly,
            "income_statements": income,
            "balance_sheets": balance,
        }
        if not observations:
            return _abstain(request.user, symbol, question, ceiling, evidence, "no_verified_financial_evidence")
        if any(term in question.casefold() for term in _UNSUPPORTED_TOPICS):
            return _abstain(request.user, symbol, question, ceiling, evidence, "question_needs_uncertified_data")
        income_requested = any(term in question.casefold() for term in _INCOME_TOPICS)
        balance_requested = any(term in question.casefold() for term in _BALANCE_TOPICS)
        if not income["points"] and income_requested:
            return _abstain(request.user, symbol, question, ceiling, evidence, "question_needs_uncertified_data")
        if not balance["points"] and balance_requested:
            return _abstain(request.user, symbol, question, ceiling, evidence, "question_needs_uncertified_data")
        if balance_requested and not income_requested:
            observations = {key: value for key, value in observations.items() if key.startswith("balance_")}
        elif income_requested and not balance_requested:
            observations = {key: value for key, value in observations.items() if key.startswith("income_")}
        observations = _matching_period_observations(question, observations)
        if not observations:
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
                "income_latest_filing_periods": income["latest_filing_periods"],
                "income_verified_periods": income["verified_periods"],
                "income_withheld_periods": income["withheld_periods"],
                "balance_latest_filing_periods": balance["latest_filing_periods"],
                "balance_verified_periods": balance["verified_periods"],
                "balance_withheld_periods": balance["withheld_periods"],
            },
        })


class ResearchRunDetailView(APIView):
    def get(self, request, run_id):
        run = ResearchRun.objects.filter(pk=run_id, user=request.user).first()
        if run is None:
            raise NotFound("Research run not found.")
        evidence_state = "not_applicable"
        if run.status == ResearchRun.Status.ANSWERED:
            if (not isinstance(run.evidence, dict)
                    or run.evidence.get("scope") != "one_tse_company_sales_365_days_statements_3650_days"
                    or _instrument(run.symbol) is None):
                evidence_state = "unverifiable"
            else:
                current = build_observations(*_current_evidence(run.symbol))
                evidence_state = "current" if run.selected_observations and all(
                    isinstance(claim, dict)
                    and claim.get("statement") == current.get(claim.get("id"), {}).get("statement")
                    and claim.get("sources") == current.get(claim.get("id"), {}).get("sources")
                    for claim in run.selected_observations
                ) else "changed"
        return Response({
            "run_id": run.pk,
            "created_at": run.created_at.isoformat(),
            "symbol": run.symbol,
            "question": run.question,
            "status": run.status,
            "evidence_state": evidence_state,
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
