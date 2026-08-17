from unittest.mock import patch

from marketdata.workflows import WorkflowOutcome


def test_workflow_stdout_is_a_compact_human_summary():
    with (
        patch("marketdata.workflows.logger.info") as info,
        patch("marketdata.models.WorkflowRun.objects.create"),
    ):
        WorkflowOutcome(
            "archive_state", endpoint="stock_candle_adjusted", symbol="TEST"
        ).finish(
            "retry",
            rows_received=42,
            rows_accepted=19,
            http_attempts=1,
            quota_attempts=1,
            duration_ms=321,
            error_code="ReadTimeout",
            metadata={"reason": "Provider request failed\n(ReadTimeout)"},
        )

    message = info.call_args.args[0]
    assert message.startswith(
        "workflow=archive_state outcome=retry endpoint=stock_candle_adjusted symbol=TEST"
    )
    assert "rows=42/19" in message
    assert "error=ReadTimeout" in message
    assert "reason=Provider request failed (ReadTimeout)" in message
    assert not message.startswith("{")
