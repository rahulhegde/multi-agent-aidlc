"""Deterministic, least-context projections for specialist invocations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from aidlc.agents.catalog import AgentSpec, ArtifactInput
from aidlc.domain.models import AgentContext


def _matches(artifact: dict[str, Any], dependency: ArtifactInput) -> bool:
    metadata = artifact["metadata"]
    return metadata["kind"] == dependency.kind and (
        dependency.producer is None or metadata["producing_agent"] == dependency.producer
    )


def select_agent_artifacts(
    spec: AgentSpec, artifacts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Select one current snapshot per declared dependency, in contract order."""

    selected: list[dict[str, Any]] = []
    for dependency in spec.inputs:
        matches = [item for item in artifacts if _matches(item, dependency)]
        if not matches and dependency.fallback_kind is not None:
            fallback = [
                item for item in artifacts if item["metadata"]["kind"] == dependency.fallback_kind
            ]
            latest_by_producer = {item["metadata"]["producing_agent"]: item for item in fallback}
            matches = [latest_by_producer[name] for name in sorted(latest_by_producer)]
        if not matches:
            if dependency.required:
                producer = f" from {dependency.producer}" if dependency.producer else ""
                raise ValueError(f"{spec.name} requires {dependency.kind.value}{producer}")
            continue
        if (
            dependency.fallback_kind is not None
            and matches[0]["metadata"]["kind"] == dependency.fallback_kind
        ):
            selected.extend(matches)
        else:
            selected.append(matches[-1])
    return selected


def build_agent_context(
    *,
    spec: AgentSpec,
    run_id: str,
    idea: str,
    artifacts: list[dict[str, Any]],
    repair_attempt: int = 0,
    repair_feedback: dict[str, Any] | None = None,
    retry_attempt: int = 0,
) -> AgentContext:
    """Create the complete wire payload for one independent specialist."""

    repair_artifact = next(
        (
            item
            for item in reversed(artifacts)
            if item["metadata"]["kind"] == "repair_request"
        ),
        None,
    )
    return AgentContext(
        run_id=run_id,
        idea=idea if spec.include_idea else "",
        stage=spec.stage,
        artifacts=select_agent_artifacts(spec, artifacts),
        repair_attempt=repair_attempt,
        repair_feedback=repair_feedback if repair_attempt else None,
        repair_artifact_id=(
            repair_artifact["metadata"]["artifact_id"]
            if repair_attempt and repair_artifact
            else None
        ),
        repair_artifact_sha256=(
            repair_artifact["metadata"]["content_sha256"]
            if repair_attempt and repair_artifact
            else None
        ),
        retry_attempt=retry_attempt,
    )


def canonical_context_bytes(context: AgentContext) -> bytes:
    return json.dumps(
        context.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()


def context_input_artifact_ids(context: AgentContext) -> list[str]:
    ids = [item["metadata"]["artifact_id"] for item in context.artifacts]
    if context.repair_artifact_id:
        ids.append(context.repair_artifact_id)
    return ids


def context_cache_key(
    spec: AgentSpec,
    context: AgentContext,
    *,
    model_id: str,
    runtime_profile: dict[str, Any] | None = None,
) -> str:
    """Hash only semantic inputs; workflow retry bookkeeping is deliberately excluded."""

    payload = {
        "agent": spec.name,
        "model_id": model_id,
        "prompt_version": spec.prompt_version,
        "context_contract_version": spec.context_contract_version,
        "idea": context.idea,
        "artifact_hashes": [
            item["metadata"].get("content_sha256")
            or hashlib.sha256(
                json.dumps(item["content"], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            for item in context.artifacts
        ],
        "human_response": context.human_response,
        "repair_attempt": context.repair_attempt,
        "repair_feedback": context.repair_feedback,
        "repair_artifact_sha256": context.repair_artifact_sha256,
        "runtime_profile": runtime_profile or {},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
