import asyncio
from dataclasses import replace

import pytest

from aidlc.a2a.client import A2AInvoker
from aidlc.agents.catalog import AGENTS_BY_STAGE
from aidlc.config import Settings
from aidlc.domain.models import (
    AgentResult,
    ArtifactKind,
    ExecutionMetadata,
    HostCapabilityReport,
    RunStatus,
    StageName,
)
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from tests.helpers import reference_transport


def test_deterministic_workflow_records_serial_and_parallel_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aidlc.orchestration.workflow.inspect_host",
        lambda _settings: HostCapabilityReport(
            ready=False,
            degraded=True,
            os_id="ubuntu",
            os_version="26.04",
            architecture="x86_64",
            kernel="test",
            checks=[],
        ),
    )
    asyncio.run(_exercise_workflow(tmp_path))


def test_strict_preflight_blocks_before_agent_work(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aidlc.orchestration.workflow.inspect_host",
        lambda _settings: HostCapabilityReport(
            ready=False,
            degraded=True,
            os_id="ubuntu",
            os_version="26.04",
            architecture="x86_64",
            kernel="test",
            checks=[],
        ),
    )

    async def exercise():
        settings = Settings(data_dir=tmp_path, enforce_host_preflight=True)
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        orchestrator = WorkflowOrchestrator(
            settings, database, ArtifactStore(settings.artifact_dir)
        )
        run = orchestrator.create_run("Build a tiny task list")
        await orchestrator.wait(run.run_id)
        result = database.get_run(run.run_id)
        assert result is not None
        assert result.status == RunStatus.BLOCKED
        assert len(database.list_artifacts(run.run_id)) == 1
        assert not any(
            event.event_type == "agent.started" for event in database.events_after(run.run_id)
        )

    asyncio.run(exercise())


async def _exercise_workflow(tmp_path):
    settings = Settings(data_dir=tmp_path, enforce_host_preflight=False)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    async with reference_transport(settings) as http_client:
        orchestrator = WorkflowOrchestrator(
            settings,
            database,
            ArtifactStore(settings.artifact_dir),
            A2AInvoker(settings, database, http_client),
        )
        created = orchestrator.create_run("Build a tiny task list")
        await orchestrator.wait(created.run_id)

    completed = database.get_run(created.run_id)
    assert completed is not None
    assert completed.status == RunStatus.COMPLETED
    assert all(stage.status == RunStatus.COMPLETED for stage in completed.stages)

    artifacts = database.list_artifacts(created.run_id)
    assert len(artifacts) == 17
    delegated = database.list_delegated_tasks(created.run_id)
    assert len(delegated) == 15
    assert all(task.task_id and task.state == "completed" for task in delegated)
    discovery_agents = {
        artifact.metadata.producing_agent
        for artifact in artifacts
        if artifact.metadata.stage_id == StageName.DISCOVERY
    }
    assert discovery_agents == {
        "architecture-agent",
        "ux-agent",
        "security-agent",
        "test-planner-agent",
    }
    assert any(
        event.event_type == "preflight.override" for event in database.events_after(created.run_id)
    )
    started = {
        event.payload["agent"]: event.payload
        for event in database.events_after(created.run_id)
        if event.event_type == "agent.started"
    }
    for artifact_record in artifacts:
        if artifact_record.metadata.producing_agent not in started:
            continue
        invocation = started[artifact_record.metadata.producing_agent]
        assert artifact_record.metadata.parent_artifact_ids == invocation["input_artifact_ids"]
        assert invocation["context_bytes"] > 0
        assert len(invocation["context_sha256"]) == 64

    discovery_inputs = {
        database.get_artifact(artifact_id).metadata.kind
        for artifact_id in started["ux-agent"]["input_artifact_ids"]
    }
    assert discovery_inputs == {ArtifactKind.PROJECT_BRIEF, ArtifactKind.REQUIREMENTS_SPEC}


def test_cancellation_before_first_stage_is_persisted(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path)
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        orchestrator = WorkflowOrchestrator(
            settings, database, ArtifactStore(settings.artifact_dir)
        )
        run = orchestrator.create_run("Build a tiny task list")
        assert orchestrator.cancel(run.run_id)
        await asyncio.gather(orchestrator.wait(run.run_id), return_exceptions=True)
        result = database.get_run(run.run_id)
        assert result is not None
        assert result.status == RunStatus.CANCELED

    asyncio.run(exercise())


def test_cache_lookup_requires_matching_key_and_contract_version(tmp_path):
    settings = Settings(data_dir=tmp_path)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run = database.create_run("run_cache_lookup", "Build a cache")
    store = ArtifactStore(settings.artifact_dir)
    store.initialize_project(run.run_id, run.idea)
    spec = AGENTS_BY_STAGE[StageName.REQUIREMENTS][0]
    artifact = store.write(
        run_id=run.run_id,
        stage=spec.stage,
        kind=spec.artifact_kind,
        producing_agent=spec.name,
        content={"cached": True},
        markdown="# Cached",
        execution={
            "cache_key": "matching-key",
            "context_contract_version": spec.context_contract_version,
        },
    )
    database.add_artifact(artifact)
    orchestrator = WorkflowOrchestrator(settings, database, store)

    assert orchestrator._cached_output(run.run_id, spec, "matching-key", []) == artifact
    assert orchestrator._cached_output(run.run_id, spec, "different-key", []) is None
    changed = replace(spec, context_contract_version=f"{spec.context_contract_version}-changed")
    assert orchestrator._cached_output(run.run_id, changed, "matching-key", []) is None


def test_token_budget_is_cumulative_per_phase_per_run(tmp_path):
    settings = Settings(data_dir=tmp_path, token_budget_per_phase_per_run=100)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run = database.create_run("run_phase_budget", "Build a budget test")
    store = ArtifactStore(settings.artifact_dir)
    store.initialize_project(run.run_id, run.idea)
    orchestrator = WorkflowOrchestrator(settings, database, store)

    def result(execution_id: str, tokens: int) -> AgentResult:
        return AgentResult(
            artifact_kind=ArtifactKind.CODE_CHANGE,
            content={},
            markdown="# Result",
            execution=ExecutionMetadata(
                execution_id=execution_id,
                input_tokens=tokens,
                output_tokens=0,
            ),
        )

    orchestrator._record_execution(
        run.run_id,
        "backend-agent",
        result("implementation-1", 60),
        phase=StageName.IMPLEMENTATION,
    )
    # A separate phase receives its own full allowance.
    orchestrator._record_execution(
        run.run_id,
        "build-agent",
        result("integration-1", 60),
        phase=StageName.INTEGRATION,
    )
    # A later execution in implementation accumulates with its earlier usage.
    with pytest.raises(
        RuntimeError,
        match=r"phase=implementation, reported_tokens=110, limit=100",
    ):
        orchestrator._record_execution(
            run.run_id,
            "backend-agent",
            result("implementation-2", 50),
            phase=StageName.IMPLEMENTATION,
            retry_attempt=1,
        )


def test_evaluation_token_budget_resets_after_workflow_retry(tmp_path):
    settings = Settings(data_dir=tmp_path, token_budget_per_phase_per_run=100)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run = database.create_run("run_evaluation_retry_budget", "Build a budget test")
    store = ArtifactStore(settings.artifact_dir)
    store.initialize_project(run.run_id, run.idea)
    orchestrator = WorkflowOrchestrator(settings, database, store)

    def result(execution_id: str, tokens: int) -> AgentResult:
        return AgentResult(
            artifact_kind=ArtifactKind.EVALUATION_REPORT,
            content={},
            markdown="# Result",
            execution=ExecutionMetadata(
                execution_id=execution_id,
                input_tokens=tokens,
                output_tokens=0,
            ),
        )

    orchestrator._record_execution(
        run.run_id,
        "evaluation-agent",
        result("evaluation-before-retry", 80),
        phase=StageName.EVALUATION,
    )
    database.append_event(run.run_id, "run.retry_requested", {"retry_attempt": 1})

    # Usage from before the retry is retained for audit but no longer counts.
    orchestrator._record_execution(
        run.run_id,
        "evaluation-agent",
        result("evaluation-after-retry", 80),
        phase=StageName.EVALUATION,
        retry_attempt=1,
    )
    # Calls made within the same retry still share the phase allowance.
    with pytest.raises(
        RuntimeError,
        match=r"phase=evaluation, reported_tokens=110, limit=100",
    ):
        orchestrator._record_execution(
            run.run_id,
            "evaluation-agent",
            result("evaluation-after-retry-2", 30),
            phase=StageName.EVALUATION,
            retry_attempt=1,
        )
