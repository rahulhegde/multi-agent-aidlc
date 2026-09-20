import hashlib
from dataclasses import replace

import pytest

from aidlc.agents.catalog import AGENT_SPECS
from aidlc.domain.models import ArtifactKind, StageName
from aidlc.orchestration.context import (
    build_agent_context,
    context_cache_key,
    context_input_artifact_ids,
    select_agent_artifacts,
)

SPECS = {item.name: item for item in AGENT_SPECS}


def artifact(kind: ArtifactKind, producer: str, suffix: str) -> dict:
    return {
        "metadata": {
            "artifact_id": f"art_{suffix}",
            "kind": kind,
            "producing_agent": producer,
            "content_sha256": hashlib.sha256(suffix.encode()).hexdigest(),
        },
        "content": {"value": suffix},
    }


def test_each_specialist_receives_only_declared_inputs_in_contract_order():
    artifacts = [
        artifact(ArtifactKind.HOST_CAPABILITY_REPORT, "host-preflight", "host"),
        artifact(ArtifactKind.PROJECT_BRIEF, "intake-agent", "brief"),
        artifact(ArtifactKind.REQUIREMENTS_SPEC, "requirements-agent", "requirements"),
        artifact(ArtifactKind.ARCHITECTURE_DECISION, "architecture-agent", "architecture"),
        artifact(ArtifactKind.UX_SPECIFICATION, "ux-agent", "ux"),
        artifact(ArtifactKind.IMPLEMENTATION_PLAN, "planning-agent", "plan"),
        artifact(ArtifactKind.CODE_CHANGE, "backend-agent", "backend-old"),
        artifact(ArtifactKind.CODE_CHANGE, "frontend-agent", "frontend"),
        artifact(ArtifactKind.CODE_CHANGE, "backend-agent", "backend-current"),
    ]

    selected = select_agent_artifacts(SPECS["backend-agent"], artifacts)

    assert [item["metadata"]["artifact_id"] for item in selected] == [
        "art_requirements",
        "art_architecture",
        "art_plan",
        "art_backend-current",
    ]


def test_missing_required_input_fails_before_delegation():
    with pytest.raises(ValueError, match="planning-agent requires requirements_spec"):
        select_agent_artifacts(SPECS["planning-agent"], [])


def test_context_omits_idea_and_unrelated_repair_state_for_independent_agents():
    inputs = [
        artifact(ArtifactKind.PROJECT_BRIEF, "intake-agent", "brief"),
        artifact(ArtifactKind.REQUIREMENTS_SPEC, "requirements-agent", "requirements"),
    ]

    context = build_agent_context(
        spec=SPECS["ux-agent"],
        run_id="run_context",
        idea="private original idea",
        artifacts=inputs,
        repair_feedback={"unrelated": True},
    )

    assert context.idea == ""
    assert context.repair_feedback is None
    assert [item["metadata"]["artifact_id"] for item in context.artifacts] == [
        "art_brief",
        "art_requirements",
    ]
    assert context.stage == StageName.DISCOVERY


def test_reviewer_gets_integrated_source_without_component_source_duplication():
    artifacts = [
        artifact(ArtifactKind.CODE_CHANGE, "backend-agent", "backend"),
        artifact(ArtifactKind.CODE_CHANGE, "frontend-agent", "frontend"),
        artifact(ArtifactKind.CODE_CHANGE, "test-agent", "tests"),
        artifact(
            ArtifactKind.INTEGRATED_SOURCE,
            "trusted-integration-service",
            "integrated",
        ),
    ]

    selected = select_agent_artifacts(SPECS["build-agent"], artifacts)

    assert [item["metadata"]["artifact_id"] for item in selected] == ["art_integrated"]


def test_reviewer_falls_back_to_component_sources_before_trusted_integration():
    artifacts = [
        artifact(ArtifactKind.CODE_CHANGE, "frontend-agent", "frontend"),
        artifact(ArtifactKind.CODE_CHANGE, "backend-agent", "backend"),
        artifact(ArtifactKind.CODE_CHANGE, "test-agent", "tests"),
    ]

    selected = select_agent_artifacts(SPECS["build-agent"], artifacts)

    assert [item["metadata"]["artifact_id"] for item in selected] == [
        "art_backend",
        "art_frontend",
        "art_tests",
    ]


def test_evaluator_receives_review_and_trusted_gate_evidence():
    artifacts = [
        artifact(ArtifactKind.REQUIREMENTS_SPEC, "requirements-agent", "requirements"),
        artifact(ArtifactKind.ARCHITECTURE_DECISION, "architecture-agent", "architecture"),
        artifact(ArtifactKind.UX_SPECIFICATION, "ux-agent", "ux"),
        artifact(ArtifactKind.THREAT_MODEL, "security-agent", "threat"),
        artifact(ArtifactKind.TEST_PLAN, "test-planner-agent", "test-plan"),
        artifact(ArtifactKind.BUILD_REPORT, "trusted-sandbox-service", "trusted-build"),
        artifact(ArtifactKind.BUILD_REPORT, "build-agent", "build-review"),
        artifact(ArtifactKind.TEST_REPORT, "trusted-sandbox-service", "trusted-test"),
        artifact(ArtifactKind.TEST_REPORT, "validation-agent", "test-review"),
        artifact(ArtifactKind.STATIC_ANALYSIS_REPORT, "static-analysis-agent", "analysis"),
        artifact(ArtifactKind.QUALITY_GATE_REPORT, "deterministic-quality-gates", "gates"),
    ]

    selected = select_agent_artifacts(SPECS["evaluation-agent"], artifacts)

    assert [item["metadata"]["artifact_id"] for item in selected] == [
        "art_requirements",
        "art_architecture",
        "art_ux",
        "art_threat",
        "art_test-plan",
        "art_build-review",
        "art_test-review",
        "art_trusted-build",
        "art_trusted-test",
        "art_analysis",
        "art_gates",
    ]


def test_repairing_implementation_agent_receives_previous_integrated_source():
    artifacts = [
        artifact(ArtifactKind.REQUIREMENTS_SPEC, "requirements-agent", "requirements"),
        artifact(ArtifactKind.TEST_PLAN, "test-planner-agent", "test-plan"),
        artifact(ArtifactKind.IMPLEMENTATION_PLAN, "planning-agent", "plan"),
        artifact(
            ArtifactKind.INTEGRATED_SOURCE,
            "trusted-integration-service",
            "integrated",
        ),
        artifact(ArtifactKind.CODE_CHANGE, "test-agent", "prior-tests"),
    ]

    selected = select_agent_artifacts(SPECS["test-agent"], artifacts)

    assert [item["metadata"]["artifact_id"] for item in selected] == [
        "art_requirements",
        "art_test-plan",
        "art_plan",
        "art_integrated",
        "art_prior-tests",
    ]


def test_repair_content_is_passed_once_with_artifact_provenance():
    repair = artifact(ArtifactKind.REPAIR_REQUEST, "orchestrator", "repair")
    context = build_agent_context(
        spec=SPECS["build-agent"],
        run_id="run_repair",
        idea="not supplied",
        artifacts=[repair],
        repair_attempt=1,
        repair_feedback={"attempt": 1, "issue": "tests failed"},
    )

    assert context.artifacts == []
    assert context.repair_feedback == {"attempt": 1, "issue": "tests failed"}
    assert context.repair_artifact_id == "art_repair"
    assert context.repair_artifact_sha256 == repair["metadata"]["content_sha256"]
    assert context_input_artifact_ids(context) == ["art_repair"]


def test_cache_key_ignores_workflow_retry_but_versions_the_context_contract():
    inputs = [
        artifact(ArtifactKind.PROJECT_BRIEF, "intake-agent", "brief"),
        artifact(ArtifactKind.REQUIREMENTS_SPEC, "requirements-agent", "requirements"),
    ]
    spec = SPECS["ux-agent"]
    first = build_agent_context(
        spec=spec, run_id="run_cache", idea="ignored", artifacts=inputs
    )
    retry = build_agent_context(
        spec=spec,
        run_id="run_cache",
        idea="ignored",
        artifacts=inputs,
        retry_attempt=2,
    )

    first_key = context_cache_key(spec, first, model_id="provider:model")
    assert first_key == context_cache_key(spec, retry, model_id="provider:model")
    changed = replace(spec, context_contract_version=f"{spec.context_contract_version}-changed")
    assert first_key != context_cache_key(changed, first, model_id="provider:model")
