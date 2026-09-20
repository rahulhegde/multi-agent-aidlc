import asyncio
from dataclasses import replace

import pytest
from pydantic import ValidationError

from aidlc.a2a.client import A2AInvoker
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, AgentResult, ArtifactKind, RunStatus, StageName
from aidlc.evaluation.cli import evaluate_run
from aidlc.evaluation.gates import evaluate_gates
from aidlc.evaluation.models import EvaluationReport, RubricScore, repair_decision
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.sandbox.cli import FIXTURE
from aidlc.sandbox.models import SandboxExecution, SandboxPolicy, SandboxResult
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from aidlc.tools.models import AnalysisReport, AnalyzerExecution
from tests.helpers import reference_transport
from tests.reference_agents import AGENTS_BY_STAGE, requirements_agent


def execution(**overrides):
    return (
        SandboxExecution(
            runtime="runc",
            isolation="OCI test fixture",
            image="sha256:" + "a" * 64,
            kernel="test",
            exit_code=0,
            policy=SandboxPolicy(),
        )
        .model_copy(update=overrides)
        .model_dump(mode="json")
    )


def analysis_report(source, **overrides):
    metadata = source["metadata"]
    return (
        AnalysisReport(
            status="completed",
            analysis_id="analysis_fixture",
            artifact_id=metadata["artifact_id"],
            content_sha256=metadata["content_sha256"],
            run_id=metadata["workflow_run_id"],
            result_uri="aidlc://analysis/fixture",
            summary={"errors": 0, "warnings": 0},
            execution=AnalyzerExecution.model_validate({"duration_ms": 1, **execution()}),
        ).model_dump(mode="json")
        | overrides
    )


class Evidence:
    def __init__(self, path):
        self.settings = Settings(data_dir=path)
        self.database = WorkflowDatabase(self.settings.database_path)
        self.database.initialize()
        self.database.create_run("run_fixture", "Build a testable answer function")
        self.store = ArtifactStore(self.settings.artifact_dir)
        self.items = []
        requirements = asyncio.run(requirements_agent(self.context()))
        self.publish(ArtifactKind.REQUIREMENTS_SPEC, requirements.content, "requirements-agent")
        self.source = self.publish(
            ArtifactKind.CODE_CHANGE, FIXTURE.model_dump(mode="json"), "backend-agent"
        )
        for kind in (ArtifactKind.BUILD_REPORT, ArtifactKind.TEST_REPORT):
            self.publish(
                kind,
                {
                    "source_artifact_id": self.source["metadata"]["artifact_id"],
                    "source_sha256": self.source["metadata"]["content_sha256"],
                    "status": "completed",
                    "execution": execution(),
                    "stderr": "Ran 1 test in 0.001s\n\nOK\n",
                },
                "trusted-sandbox-service",
                [self.source["metadata"]["artifact_id"]],
            )
        self.publish(
            ArtifactKind.STATIC_ANALYSIS_REPORT,
            analysis_report(self.source),
            "static-analysis-agent",
            [self.source["metadata"]["artifact_id"]],
        )

    def publish(self, kind, content, producer, parents=None):
        record = self.store.write(
            run_id="run_fixture",
            stage=StageName.INTEGRATION,
            kind=kind,
            producing_agent=producer,
            content=content,
            markdown="# Fixture evidence",
            parent_artifact_ids=parents,
            execution=content.get("execution", {}),
        )
        self.database.add_artifact(record)
        item = {"metadata": record.metadata.model_dump(mode="json"), "content": content}
        self.items.append(item)
        return item

    def context(self):
        return AgentContext(
            run_id="run_fixture",
            idea="Build an answer function",
            stage=StageName.EVALUATION,
            artifacts=self.items,
        )


def test_current_source_gates_pass_and_standalone_publication_preserves_run_state(tmp_path):
    evidence = Evidence(tmp_path)
    assert evaluate_gates(evidence.context()).verdict == "passed"
    before = evidence.database.get_run("run_fixture")
    result = evaluate_run(evidence.settings, "run_fixture")
    assert result["verdict"] == "passed"
    assert evidence.database.get_run("run_fixture") == before
    record = evidence.database.get_artifact(result["artifact_id"])
    assert record is not None and record.metadata.kind == ArtifactKind.QUALITY_GATE_REPORT
    assert evidence.store.read_content(record)["deferred_checks"]


def test_new_source_requires_new_evidence_even_when_content_hash_is_unchanged(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.publish(ArtifactKind.CODE_CHANGE, FIXTURE.model_dump(mode="json"), "backend-agent")
    report = evaluate_gates(evidence.context())
    assert report.verdict == "blocked"
    assert all(gate.status == "not_executed" for gate in report.gates[-3:])


@pytest.mark.parametrize(
    "defect",
    [
        "source_hash",
        "parent",
        "producer",
        "runtime",
        "cleanup",
        "timeout",
        "oom",
        "unpinned_image",
        "network",
        "zero_tests",
        "failed",
        "manifest_execution",
    ],
)
def test_invalid_execution_evidence_cannot_pass(tmp_path, defect):
    evidence = Evidence(tmp_path)
    original = next(
        item for item in evidence.items if item["metadata"]["kind"] == ArtifactKind.TEST_REPORT
    )
    content = {**original["content"], "execution": dict(original["content"]["execution"])}
    parents = [evidence.source["metadata"]["artifact_id"]]
    producer = "trusted-sandbox-service"
    if defect == "source_hash":
        content["source_sha256"] = "f" * 64
    elif defect == "parent":
        parents = []
    elif defect == "producer":
        producer = "untrusted-agent"
    elif defect == "runtime":
        content["execution"]["runtime"] = "runsc"
    elif defect == "cleanup":
        content["execution"]["cleanup_succeeded"] = False
    elif defect == "timeout":
        content["execution"]["timed_out"] = True
    elif defect == "oom":
        content["execution"]["oom_killed"] = True
    elif defect == "unpinned_image":
        content["execution"]["image"] = "python:latest"
    elif defect == "network":
        content["execution"]["policy"] = {**content["execution"]["policy"], "network": "host"}
    elif defect == "zero_tests":
        content["stderr"] = "Ran 0 tests in 0.001s\nOK"
    elif defect == "failed":
        content["status"] = "failed"
        content["execution"]["exit_code"] = 1
    item = evidence.publish(ArtifactKind.TEST_REPORT, content, producer, parents)
    if defect == "manifest_execution":
        item["metadata"]["execution"] = {}
    report = evaluate_gates(evidence.context())
    assert report.verdict == "failed"
    assert next(gate for gate in report.gates if gate.name == "unit_tests").status == "failed"


def test_ruff_completed_with_findings_still_fails_the_static_gate(tmp_path):
    evidence = Evidence(tmp_path)
    content = analysis_report(
        evidence.source,
        findings=[
            {
                "rule_id": "F401",
                "severity": "error",
                "path": "example.py",
                "line": 1,
                "column": 1,
                "message": "Unused import",
                "fingerprint": "fixture",
            }
        ],
    )
    evidence.publish(
        ArtifactKind.STATIC_ANALYSIS_REPORT,
        {"reports": [content]},
        "static-analysis-agent",
        [evidence.source["metadata"]["artifact_id"]],
    )
    assert evaluate_gates(evidence.context()).verdict == "failed"


def test_tampered_or_foreign_artifacts_block_evaluation(tmp_path):
    evidence = Evidence(tmp_path)
    evidence.source["content"] = {"files": []}
    report = evaluate_gates(evidence.context())
    assert report.verdict == "blocked" and report.gates[0].status == "failed"
    evidence.source["content"] = FIXTURE.model_dump(mode="json")
    evidence.source["metadata"]["workflow_run_id"] = "run_other"
    assert evaluate_gates(evidence.context()).verdict == "blocked"


def graded(score):
    value = RubricScore(
        score=score, rationale="Fixture rubric judgment", evidence_artifact_ids=["art_fixture"]
    )
    return EvaluationReport(
        status="evaluated",
        summary="Fixture assessment",
        requirement_coverage=value,
        mvp_completeness=value,
        usability=value,
        architecture=value,
        maintainability=value,
        risk_acceptance=value,
    )


def test_human_evaluation_decision_is_final_and_automatic_limits_are_exact(tmp_path):
    evidence = Evidence(tmp_path)
    gates = evaluate_gates(evidence.context())
    assert repair_decision(gates, graded(0.8), 0, 2, 0.8) == "passed"
    assert repair_decision(gates, graded(0.79), 0, 2, 0.8) == "repair"
    assert repair_decision(gates, graded(0.79), 0, 2, 0.8, "accept") == "passed"
    assert repair_decision(gates, graded(1), 0, 2, 0.8, "repair") == "repair"
    assert repair_decision(gates, graded(0.79), 2, 2, 0.8) == "exhausted"
    gates.verdict = "failed"
    assert repair_decision(gates, graded(1), 0, 2, 0.8) == "repair"
    assert repair_decision(gates, graded(1), 0, 2, 0.8, "accept") == "passed"
    assert repair_decision(gates, graded(1), 0, 0, 0.8) == "exhausted"
    gates.verdict = "blocked"
    assert repair_decision(gates, graded(1), 0, 2, 0.8) == "deferred"
    assert repair_decision(gates, graded(1), 0, 2, 0.8, "accept") == "passed"


def test_missing_scores_and_invalid_configuration_are_rejected():
    with pytest.raises(ValidationError, match="six rubric"):
        EvaluationReport(status="evaluated", summary="Incomplete evaluation")
    with pytest.raises(ValidationError):
        RubricScore(score=float("nan"), rationale="Invalid score")
    with pytest.raises(ValidationError, match="requires a score"):
        RubricScore(rationale="Missing applicable score")
    with pytest.raises(ValidationError, match="cannot publish a score"):
        RubricScore(
            applicability="not_applicable",
            score=0.5,
            rationale="This rubric is outside the requirements",
        )
    with pytest.raises(ValueError):
        Settings(max_evaluation_repair_attempts=-1)
    with pytest.raises(ValueError):
        Settings(evaluation_score_threshold=1.1)


def test_not_applicable_rubrics_are_excluded_from_overall_score():
    report = graded(0.8)
    report.usability = RubricScore(
        applicability="not_applicable",
        rationale="The approved requirements do not request a user interface.",
        evidence_artifact_ids=["art_requirements"],
    )

    assert report.overall_score() == pytest.approx(0.8)


@pytest.mark.parametrize(
    "always_fail,limit,expected",
    [
        (False, 2, RunStatus.COMPLETED),
        (True, 2, RunStatus.FAILED),
        (True, 0, RunStatus.FAILED),
    ],
)
def test_bounded_repair_keeps_current_snapshots_and_task_history(
    tmp_path,
    monkeypatch,
    always_fail,
    limit,
    expected,
):
    async def implementation(context):
        if context.repair_attempt:
            assert context.repair_feedback["quality_gates"]["verdict"] == "failed"
            run = WorkflowDatabase(tmp_path / "aidlc.sqlite3").get_run(context.run_id)
            assert run is not None
            stage = next(item for item in run.stages if item.name == StageName.IMPLEMENTATION)
            assert stage.status == RunStatus.RUNNING and stage.finished_at is None
        content = FIXTURE.model_dump(mode="json")
        content["files"][0]["content"] += f"\n# attempt {context.repair_attempt}\n"
        return AgentResult(
            artifact_kind=ArtifactKind.CODE_CHANGE,
            content=content,
            markdown="# Explicit repair fixture",
        )

    async def static(context):
        latest = {}
        for item in context.artifacts:
            if item["metadata"]["kind"] == ArtifactKind.CODE_CHANGE:
                latest[item["metadata"]["producing_agent"]] = item
        return AgentResult(
            artifact_kind=ArtifactKind.STATIC_ANALYSIS_REPORT,
            content={"reports": [analysis_report(item) for item in latest.values()]},
            markdown="# Simulated isolated analysis",
        )

    async def sandbox(_self, job, _runtime=None):
        first_attempt = "# attempt 0" in job.source.files[0].content
        failed = job.mode == "test" and (always_fail or first_attempt)
        return SandboxResult(
            status="failed" if failed else "completed",
            execution=SandboxExecution.model_validate(execution(exit_code=1 if failed else 0)),
            stderr="Ran 1 test in 0.001s\n" + ("FAILED" if failed else "OK"),
        )

    definitions = dict(AGENTS_BY_STAGE)
    definitions[StageName.IMPLEMENTATION] = tuple(
        replace(item, handler=implementation) for item in definitions[StageName.IMPLEMENTATION]
    )
    definitions[StageName.INTEGRATION] = tuple(
        replace(item, handler=static) if item.name == "static-analysis-agent" else item
        for item in definitions[StageName.INTEGRATION]
    )
    monkeypatch.setattr("tests.helpers.AGENTS_BY_STAGE", definitions)
    monkeypatch.setattr("aidlc.sandbox.cli.DockerSandbox.execute", sandbox)

    async def exercise():
        settings = Settings(
            data_dir=tmp_path,
            enforce_host_preflight=False,
            sandbox_execution_enabled=True,
            max_evaluation_repair_attempts=limit,
        )
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        store = ArtifactStore(settings.artifact_dir)
        async with reference_transport(settings) as client:
            hub = WorkflowOrchestrator(
                settings, database, store, A2AInvoker(settings, database, client)
            )
            try:
                run = hub.create_run("Build an answer function")
                await hub.wait(run.run_id)
                result = database.get_run(run.run_id)
                assert result is not None and result.status == expected, result
                attempts = limit if always_fail else 1
                records = database.list_artifacts(run.run_id)
                gates = [
                    store.read_content(item)
                    for item in records
                    if item.metadata.kind == ArtifactKind.QUALITY_GATE_REPORT
                ]
                assert [item["repair_attempt"] for item in gates] == [attempts]
                assert gates[-1]["verdict"] == ("failed" if always_fail else "passed")
                repairs = [
                    item for item in records if item.metadata.kind == ArtifactKind.REPAIR_REQUEST
                ]
                assert len(repairs) == attempts
                tasks = database.list_delegated_tasks(run.run_id)
                implementations = [item for item in tasks if item.stage == StageName.IMPLEMENTATION]
                assert len(implementations) == 3 * (attempts + 1)
                assert len({item.task_id for item in implementations}) == len(implementations)
                assert all(item.state == "completed" for item in implementations)
                releases = [
                    item for item in records if item.metadata.kind == ArtifactKind.RELEASE_BUNDLE
                ]
                assert len(releases) == (0 if always_fail else 1)
                if releases:
                    assert store.read_content(releases[0])["status"] == "withheld"
                if always_fail:
                    assert any(
                        event.event_type == "repair.exhausted"
                        for event in database.events_after(run.run_id)
                    )
            finally:
                await hub.shutdown()

    asyncio.run(exercise())
