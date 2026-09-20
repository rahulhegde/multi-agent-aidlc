"""Run-level model usage and deterministic context-reduction reporting."""

from __future__ import annotations

from typing import Any

from aidlc.domain.models import EventRecord


def finops_report(events: list[EventRecord]) -> dict[str, Any]:
    contexts = [event.payload for event in events if event.event_type == "agent.context_selected"]
    starts = [event.payload for event in events if event.event_type == "agent.started"]
    executions = [event.payload for event in events if event.event_type == "agent.execution"]
    cache_hits = [event.payload for event in events if event.event_type == "agent.cache_hit"]

    agents = sorted(
        {
            str(item.get("agent"))
            for item in [*contexts, *executions, *cache_hits]
            if item.get("agent")
        }
    )

    def total(items: list[dict[str, Any]], field: str) -> int:
        return sum(int(item.get(field) or 0) for item in items)

    def cost(items: list[dict[str, Any]]) -> float | None:
        values = [float(item["cost_usd"]) for item in items if item.get("cost_usd") is not None]
        return sum(values) if values else None

    def reported_tokens(items: list[dict[str, Any]], field: str) -> int | None:
        values = [int(item[field]) for item in items if item.get(field) is not None]
        return sum(values) if values else None

    def combined_tokens(items: list[dict[str, Any]]) -> int | None:
        input_tokens = reported_tokens(items, "input_tokens")
        output_tokens = reported_tokens(items, "output_tokens")
        if input_tokens is None and output_tokens is None:
            return None
        return (input_tokens or 0) + (output_tokens or 0)

    def metrics(agent: str | None = None) -> dict[str, Any]:
        selected = [item for item in contexts if agent is None or item.get("agent") == agent]
        used = [item for item in executions if agent is None or item.get("agent") == agent]
        hits = [item for item in cache_hits if agent is None or item.get("agent") == agent]
        delegated = [item for item in starts if agent is None or item.get("agent") == agent]
        selected_bytes = total(selected, "context_bytes")
        baseline_bytes = total(selected, "baseline_context_bytes")
        saved_bytes = baseline_bytes - selected_bytes
        input_tokens = reported_tokens(used, "input_tokens")
        output_tokens = reported_tokens(used, "output_tokens")
        all_tokens = combined_tokens(used)
        successful_outputs = len(
            [
                event
                for event in events
                if event.event_type == "agent.completed"
                and (agent is None or event.payload.get("agent") == agent)
            ]
        ) + len(hits)
        return {
            "context_selections": len(selected),
            "model_executions": len(used),
            "cache_hits": len(hits),
            "selected_context_bytes": selected_bytes,
            "model_context_bytes_sent": total(delegated, "context_bytes"),
            "cache_avoided_context_bytes": total(hits, "context_bytes"),
            "broad_context_baseline_bytes": baseline_bytes,
            "context_bytes_saved": saved_bytes,
            "context_reduction_percent": (
                round(saved_bytes * 100 / baseline_bytes, 2) if baseline_bytes else 0.0
            ),
            "actual_input_tokens": input_tokens,
            "actual_output_tokens": output_tokens,
            "actual_total_tokens": all_tokens,
            "repair_tokens": combined_tokens(
                [item for item in used if int(item.get("repair_attempt") or 0) > 0]
            ),
            "workflow_retry_tokens": combined_tokens(
                [item for item in used if int(item.get("retry_attempt") or 0) > 0]
            ),
            "tokens_per_successful_output": (
                round(all_tokens / successful_outputs, 2)
                if successful_outputs and all_tokens is not None
                else None
            ),
            "actual_cost_usd": cost(used),
            "successful_outputs": successful_outputs,
        }

    return {
        "measurement_version": "aidlc-finops-v1",
        "notes": {
            "actual_tokens": "Provider-reported model usage.",
            "context_savings": (
                "Deterministic serialized-byte comparison with the former broad context; "
                "it is not an estimate of tokens or currency."
            ),
        },
        "totals": metrics(),
        "agents": [{"agent": agent, **metrics(agent)} for agent in agents],
    }
