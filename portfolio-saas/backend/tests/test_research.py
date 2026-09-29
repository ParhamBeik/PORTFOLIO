"""Research cannot invent numeric claims or spend past configured ceilings."""

import json
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import requests
from django.test import override_settings
from rest_framework.test import APIClient

from marketdata.models import MarketInstrument
from research.models import ResearchBudgetDay, ResearchRun
from research.observations import build_observations
from research import provider, views

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def legacy_router_test_gate(settings):
    # Existing provider behavior remains covered even though release keeps it off.
    settings.PAID_AI_ENABLED = True


def _client(make_user):
    MarketInstrument.objects.create(
        source=MarketInstrument.Source.TSETMC,
        category=MarketInstrument.Category.STOCK,
        symbol="فولاد", name="Foolad", eligible=True,
    )
    client = APIClient()
    client.force_authenticate(user=make_user())
    return client


def _monthly():
    def point(period, value, extraction_id):
        return {
            "date": "2026-06-01", "period_start_jalali": f"{period[:8]}01",
            "period_end_jalali": period, "value": str(value),
            "source_url": "https://www.codal.ir/Reports/Decision.aspx?LetterSerial=1",
            "report_id": extraction_id, "extraction_id": extraction_id,
            "artifact_id": extraction_id, "artifact_sha256": "a" * 64,
            "source_coordinates": {"row": 30, "column": 17},
        }

    return {
        "status": "verified", "measure": "monthly_sales_revenue",
        "unit": "million_rial", "currency": "IRR",
        "latest_filing_periods": 2, "verified_periods": 2, "withheld_periods": 0,
        "points": [point("1405-02-31", "100", 1), point("1405-03-31", "200", 2)],
    }


def _income():
    return {
        "status": "verified", "latest_filing_periods": 1,
        "verified_periods": 1, "withheld_periods": 0,
        "points": [{
            "period_start_jalali": "1405-01-01", "period_end_jalali": "1405-03-31",
            "scope": "standalone", "audited": False,
            "revenue": "834166799", "net_profit": "120356493",
            "net_margin_pct": "14.43", "artifact_id": 14169,
            "artifact_sha256": "c" * 64, "extraction_id": 42,
            "source_coordinates": {"revenue": {"address": "B4"}, "net_profit": {"address": "B21"}},
        }],
    }


def _balance():
    return {
        "status": "verified", "latest_filing_periods": 1,
        "verified_periods": 1, "withheld_periods": 0,
        "points": [{
            "statement_kind": "balance_sheet", "period_end_jalali": "1405-03-31",
            "scope": "standalone", "audited": False,
            "values": {
                "total_assets": "6278290305", "total_liabilities": "2896232830",
                "total_equity": "3382057475", "cash": "500661833",
                "short_term_borrowings": "563689252", "long_term_borrowings": "254997750",
            },
            "artifact_id": 14170, "artifact_sha256": "d" * 64,
            "extraction_id": 43,
            "source_url": "https://codal.ir/Reports/Decision.aspx?LetterSerial=x&sheetId=0",
            "source_coordinates": {
                "total_assets": {"address": "B22"},
                "total_liabilities": {"address": "B53"},
                "total_equity": {"address": "B35"},
            },
        }],
    }


def _config(tmp_path):
    path = tmp_path / "gapgpt.env"
    path.write_text(
        "GAPGPT_API_KEY=test-secret\n"
        "GAPGPT_BASE_URL=https://api.gapgpt.app/v1\n"
        "GAPGPT_MODEL=gemini-2.5-flash-lite\n"
        "GAPGPT_INPUT_USD_PER_MILLION=0.10\n"
        "GAPGPT_OUTPUT_USD_PER_MILLION=0.40\n"
    )
    return path


def _response(content, cost="0.0004"):
    class Response:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 55, "completion_tokens": 15, "cost_usd": cost},
            }

    return Response()


def test_research_requires_login_and_keeps_provider_secret_off_settings(make_user, tmp_path):
    assert APIClient().get("/api/research/settings/").status_code == 401
    client = _client(make_user)
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.get("/api/research/settings/")
    assert response.status_code == 200
    assert response.data["provider_status"] == "ready"
    assert response.data["provider_model"] == "gemini-2.5-flash-lite"
    assert "test-secret" not in str(response.data)


def test_unreleased_ai_cannot_call_provider_or_charge_user(make_user, monkeypatch):
    client = _client(make_user)
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    with override_settings(PAID_AI_ENABLED=False):
        settings_response = client.get("/api/research/settings/")
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "How did profit change?", "max_cost_usd": "0.01",
        }, format="json")
    assert settings_response.data["provider_status"] == "release_gated"
    assert response.status_code == 503
    assert ResearchRun.objects.count() == 0
    assert ResearchBudgetDay.objects.count() == 0


def test_unsupported_question_abstains_without_provider_call(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "What was net profit in USD?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 200
    assert response.data["status"] == "abstained"
    assert response.data["cost_usd"] == "0"
    assert ResearchBudgetDay.objects.count() == 0


@pytest.mark.parametrize("question", [
    "What were sales in 1404?",
    "What were sales in ۱۴۰۵/۰۱?",
    "What were sales in 2020?",
])
def test_explicit_uncovered_period_never_routes_to_latest_fact(make_user, monkeypatch, question):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    response = client.post("/api/research/runs/", {
        "symbol": "فولاد", "question": question, "max_cost_usd": "0.01",
    }, format="json")
    assert response.status_code == 200
    assert response.data["status"] == "abstained"
    assert response.data["cost_usd"] == "0"


def test_model_selects_server_claim_and_usage_is_charged(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(
        provider.requests, "post",
        lambda *args, **kwargs: _response({"supported": True, "selected_ids": ["highest"]}),
    )
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "Which month had highest sales?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 200, response.data
    assert response.data["status"] == "answered"
    assert response.data["claims"][0]["statement"] == "Highest among verified months: 1405-03-31 at 200 million Rial."
    assert response.data["claims"][0]["sources"][0]["artifact_id"] == 2
    assert response.data["cost_usd"] == "0.000400"
    detail = client.get(f"/api/research/runs/{response.data['run_id']}/")
    assert detail.status_code == 200
    assert detail.data["claims"] == response.data["claims"]
    assert detail.data["evidence_state"] == "current"
    assert detail.data["evidence"]["monthly_sales"]["points"][1]["artifact_sha256"] == "a" * 64
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly() | {"points": []})
    changed = client.get(f"/api/research/runs/{response.data['run_id']}/")
    assert changed.data["evidence_state"] == "changed"
    assert changed.data["status"] == "answered"
    assert changed.data["claims"] == response.data["claims"]  # immutable historical record
    run = ResearchRun.objects.get(pk=response.data["run_id"])
    run.evidence = {"scope": "unsupported_old_tool"}
    run.save(update_fields=["evidence"])
    assert client.get(f"/api/research/runs/{run.pk}/").data["evidence_state"] == "unverifiable"
    another = APIClient()
    another.force_authenticate(user=make_user(email="other@test.test"))
    assert another.get(f"/api/research/runs/{response.data['run_id']}/").status_code == 404
    day = ResearchBudgetDay.objects.get()
    assert day.reserved_usd == 0
    assert day.spent_usd == Decimal("0.000400")


def test_income_question_uses_certified_cells_without_monthly_sales(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: {
        "points": [], "latest_filing_periods": 0, "verified_periods": 0, "withheld_periods": 0,
    })
    monkeypatch.setattr(views, "_income_statements", lambda *args: _income())
    monkeypatch.setattr(
        provider.requests, "post",
        lambda *args, **kwargs: _response({"supported": True, "selected_ids": ["income_standalone_net_profit"]}),
    )
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "What was its net profit?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 200, response.data
    assert response.data["status"] == "answered"
    assert "120,356,493 million Rial" in response.data["claims"][0]["statement"]
    assert response.data["claims"][0]["sources"][0]["source_coordinates"]["net_profit"]["address"] == "B21"
    assert response.data["coverage"]["income_verified_periods"] == 1


def test_balance_question_uses_source_cells_and_no_income_data(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: {
        "points": [], "latest_filing_periods": 0, "verified_periods": 0, "withheld_periods": 0,
    })
    monkeypatch.setattr(views, "_income_statements", lambda *args: {
        "points": [], "latest_filing_periods": 0, "verified_periods": 0, "withheld_periods": 0,
    })
    monkeypatch.setattr(views, "_balance_sheets", lambda *args: _balance())
    monkeypatch.setattr(
        provider.requests, "post",
        lambda *args, **kwargs: _response({"supported": True, "selected_ids": ["balance_standalone_total_assets"]}),
    )
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "What are its total assets on the balance sheet?",
            "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 200, response.data
    assert response.data["status"] == "answered"
    assert "6,278,290,305 million Rial" in response.data["claims"][0]["statement"]
    assert response.data["claims"][0]["sources"][0]["source_coordinates"]["total_assets"]["address"] == "B22"
    assert response.data["coverage"]["balance_verified_periods"] == 1


def test_balance_question_without_balance_evidence_abstains_before_model(make_user, monkeypatch):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    response = client.post("/api/research/runs/", {
        "symbol": "فولاد", "question": "How much debt does its balance sheet show?",
        "max_cost_usd": "0.01",
    }, format="json")
    assert response.status_code == 200
    assert response.data["status"] == "abstained"
    assert response.data["cost_usd"] == "0"


def test_latest_statement_claim_is_withheld_when_a_newer_period_is_unverified():
    monthly = {"points": []}
    income = _income() | {"latest_period_by_scope": {"standalone": "1405-06-31"}}
    balance = _balance() | {"latest_period_by_scope": {"standalone": "1405-06-31"}}
    assert build_observations(monthly, income, balance) == {}


@pytest.mark.parametrize("question", [
    "How much debt does this company have?",
    "What was its cash flow?",
    "جریان وجوه نقد شرکت چقدر بود؟",
])
def test_unsupported_balance_question_abstains_without_model(make_user, monkeypatch, question):
    client = _client(make_user)
    monkeypatch.setattr(views, "_balance_sheets", lambda *args: _balance())
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    response = client.post("/api/research/runs/", {
        "symbol": "فولاد", "question": question,
        "max_cost_usd": "0.01",
    }, format="json")
    assert response.status_code == 200
    assert response.data["status"] == "abstained"
    assert response.data["cost_usd"] == "0"


def test_budget_rejects_before_provider_call(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(provider.requests, "post", lambda *args, **kwargs: pytest.fail("provider called"))
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "Which month had highest sales?", "max_cost_usd": "0.000001",
        }, format="json")
    assert response.status_code == 429
    assert ResearchRun.objects.count() == 0


def test_invalid_model_answer_records_known_cost(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(
        provider.requests, "post",
        lambda *args, **kwargs: _response({"supported": True, "selected_ids": ["invented"]}),
    )
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "Which month had highest sales?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 502
    run = ResearchRun.objects.get()
    assert run.status == ResearchRun.Status.FAILED
    assert run.actual_cost_usd == Decimal("0.000400")
    assert ResearchBudgetDay.objects.get().spent_usd == Decimal("0.000400")


def test_unknown_provider_transport_cost_keeps_reservation(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())

    def timeout(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(provider.requests, "post", timeout)
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "Which month had highest sales?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 502
    assert ResearchRun.objects.get().actual_cost_usd is None
    assert ResearchBudgetDay.objects.get().reserved_usd > 0


def test_provider_overrun_is_recorded_and_answer_withheld(make_user, monkeypatch, tmp_path):
    client = _client(make_user)
    monkeypatch.setattr(views, "_monthly_sales", lambda *args: _monthly())
    monkeypatch.setattr(
        provider.requests, "post",
        lambda *args, **kwargs: _response(
            {"supported": True, "selected_ids": ["highest"]}, cost="0.02",
        ),
    )
    with override_settings(GAPGPT_CONFIG_FILE=str(_config(tmp_path))):
        response = client.post("/api/research/runs/", {
            "symbol": "فولاد", "question": "Which month had highest sales?", "max_cost_usd": "0.01",
        }, format="json")
    assert response.status_code == 502
    assert response.data["budget"] == "provider_cost_exceeded_run_ceiling"
    assert ResearchRun.objects.get().actual_cost_usd == Decimal("0.02")
    assert ResearchBudgetDay.objects.get().spent_usd == Decimal("0.02")


def test_news_secret_projection_uses_only_allowlisted_settings(tmp_path, monkeypatch, capsys):
    script = Path(__file__).parents[2] / "deploy/sync_gapgpt_config.py"
    spec = importlib.util.spec_from_file_location("sync_gapgpt_config", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "news.env"
    source.write_text(
        "GAPGPT_API_KEY=test-secret\n"
        "GAPGPT_BASE_URL=https://api.gapgpt.app/v1\n"
        "GAPGPT_MODEL=gemini-2.5-flash-lite\n"
        "OTHER_NEWS_SECRET=do-not-copy\n"
    )
    news_settings = tmp_path / "news_settings.py"
    news_settings.write_text(
        'GAPGPT_INPUT_USD_PER_MILLION = env_float("GAPGPT_INPUT_USD_PER_MILLION", 0.10)\n'
        'GAPGPT_OUTPUT_USD_PER_MILLION = env_float("GAPGPT_OUTPUT_USD_PER_MILLION", 0.40)\n'
    )
    destination = tmp_path / "portfolio-secret" / "gapgpt.env"
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "chown", lambda *args: None)
    monkeypatch.setattr(sys, "argv", [
        str(script), "--source", str(source), "--news-settings", str(news_settings),
        "--destination", str(destination),
    ])
    module.main()
    projected = destination.read_text()
    assert "GAPGPT_API_KEY=test-secret" in projected
    assert "GAPGPT_INPUT_USD_PER_MILLION=0.10" in projected
    assert "OTHER_NEWS_SECRET" not in projected
    assert "do-not-copy" not in projected
    assert destination.stat().st_mode & 0o777 == 0o600
    assert "test-secret" not in capsys.readouterr().out
