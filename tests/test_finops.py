from datetime import UTC, datetime

from aidlc.domain.models import EventRecord
from aidlc.orchestration.finops import finops_report


def event(event_id: int, event_type: str, **payload) -> EventRecord:
    return EventRecord(
        event_id=event_id,
        run_id="run_finops",
        event_type=event_type,
        payload=payload,
        created_at=datetime.now(UTC),
    )


def test_finops_report_separates_actual_usage_from_context_byte_savings():
    report = finops_report(
        [
            event(
                1,
                "agent.context_selected",
                agent="ux-agent",
                context_bytes=400,
                baseline_context_bytes=1000,
            ),
            event(
                2,
                "agent.started",
                agent="ux-agent",
                context_bytes=400,
            ),
            event(
                3,
                "agent.execution",
                agent="ux-agent",
                input_tokens=250,
                output_tokens=50,
                cost_usd=0.02,
                repair_attempt=0,
                retry_attempt=0,
            ),
            event(4, "agent.completed", agent="ux-agent"),
            event(
                5,
                "agent.context_selected",
                agent="ux-agent",
                context_bytes=400,
                baseline_context_bytes=1000,
            ),
            event(6, "agent.cache_hit", agent="ux-agent", context_bytes=400),
        ]
    )

    assert report["measurement_version"] == "aidlc-finops-v1"
    assert report["totals"] == {
        "context_selections": 2,
        "model_executions": 1,
        "cache_hits": 1,
        "selected_context_bytes": 800,
        "model_context_bytes_sent": 400,
        "cache_avoided_context_bytes": 400,
        "broad_context_baseline_bytes": 2000,
        "context_bytes_saved": 1200,
        "context_reduction_percent": 60.0,
        "actual_input_tokens": 250,
        "actual_output_tokens": 50,
        "actual_total_tokens": 300,
        "repair_tokens": None,
        "workflow_retry_tokens": None,
        "tokens_per_successful_output": 150.0,
        "actual_cost_usd": 0.02,
        "successful_outputs": 2,
    }
    assert report["agents"][0]["agent"] == "ux-agent"
