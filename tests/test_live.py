"""Live fleet paths use real Deep Agents graphs with an explicitly scripted test model."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aidlc.a2a.client import A2AInvoker
from aidlc.a2a.server import create_agent_app
from aidlc.agents.catalog import AGENT_SPECS
from aidlc.agents.deep import _profile, migrate
from aidlc.agents.live import create_definitions
from aidlc.agents.source import merge_sources, validate_source_output
from aidlc.config import Settings, load_environment
from aidlc.domain.models import AgentContext, ArtifactKind, RunStatus, StageName
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.sandbox.models import SandboxExecution, SandboxResult
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from aidlc.tools.models import AnalysisReport, AnalyzerExecution, SourceBundle
from tests.test_a2a_network import ready_report
from tests.test_deep import ScriptedModel
from tests.test_evaluation import execution, graded


def source_files(role):
    if role == "backend-agent":
        return [
            {"path": "backend/api.py", "content": "def answer():\n    return 42\n"},
        ]
    if role == "test-agent":
        return [
            {"path": "tests/__init__.py", "content": ""},
            {
                "path": "tests/test_api.py",
                "content": (
                    "import unittest\nfrom backend.api import answer\n"
                    "class ApiTest(unittest.TestCase):\n"
                    "    def test_answer(self):\n        self.assertEqual(answer(), 42)\n"
                ),
            },
        ]
    return [
        {
            "path": "ui/index.html",
            "content": '<div id="root"></div><script type="module" src="/src/main.tsx"></script>',
        },
        {
            "path": "ui/src/main.tsx",
            "content": (
                'import { createRoot } from "react-dom/client";\n'
                'createRoot(document.getElementById("root")!).render(<h1>Task list</h1>);\n'
            ),
        },
        {"path": "ui/tsconfig.json", "content": "{}"},
    ]


def output_for(role, context):
    content = {
        "intake-agent": {
            "idea": context.idea,
            "target_user": "Students",
            "problem": "Track tasks",
            "assumptions": [],
            "mvp_goal": "Manage tasks",
        },
        "requirements-agent": {
            "product_name": "Tasks",
            "functional_requirements": ["Track tasks"],
            "non_functional_requirements": [],
            "acceptance_criteria": ["Tasks persist"],
            "exclusions": [],
            "open_questions": [],
        },
        "architecture-agent": {
            "decision": "React and Python",
            "components": ["UI", "API"],
            "constraints": [],
            "trade_offs": [],
        },
        "ux-agent": {"journey": ["Add task"], "screens": ["Tasks"], "accessibility": ["Labels"]},
        "security-agent": {
            "assets": ["Tasks"],
            "threats": ["Untrusted input"],
            "controls": ["Validation"],
        },
        "test-planner-agent": {
            "levels": ["unit"],
            "critical_cases": ["Create task"],
            "requirement_coverage": ["Track tasks"],
        },
        "planning-agent": {
            "backend_tasks": ["API"],
            "frontend_tasks": ["UI"],
            "test_tasks": ["Tests"],
            "shared_contracts": ["GET /api/tasks"],
            "dependency_order": ["API", "UI"],
            "completion_criteria": ["Passing tests"],
        },
        "release-agent": {
            "run_instructions": ["python -m backend.api", "cd ui && npm ci && npm run dev"],
            "architecture_summary": "React and Python",
            "known_limitations": ["No browser tests"],
        },
    }.get(role, {"summary": "Reviewed actual evidence", "repairs": []})
    if role in {"backend-agent", "frontend-agent", "test-agent"}:
        return {"files": source_files(role)}
    if role == "evaluation-agent":
        content = graded(0.9).model_dump(mode="json")
        ids = [item["metadata"]["artifact_id"] for item in context.artifacts]
        for value in content.values():
            if isinstance(value, dict) and "score" in value:
                value["evidence_artifact_ids"] = ids[-1:]
    return {"content": content, "markdown": f"# {role}"}


class RoleModel(ScriptedModel):
    role: str

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        payload = next(
            message.content for message in reversed(messages) if isinstance(message, HumanMessage)
        )
        assert isinstance(payload, str)
        context = AgentContext.model_validate_json(payload)
        spec = next(spec for spec in AGENT_SPECS if spec.name == self.role)
        schema, _ = _profile(spec.stage, spec.name)
        self.responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": schema.__name__,
                        "id": self.role,
                        "type": "tool_call",
                        "args": output_for(self.role, context),
                    }
                ],
                usage_metadata={"input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
                response_metadata={"model_name": "scripted-live-test"},
            )
        ]
        return super()._generate(messages, stop, run_manager, **kwargs)


def live_settings(tmp_path):
    return Settings(
        data_dir=tmp_path,
        model="test:scripted",
        mcp_enabled=True,
        sandbox_execution_enabled=True,
        human_gates_enabled=False,
        enforce_host_preflight=False,
    )


def test_shared_environment_and_live_defaults(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        'AIDLC_MODEL="test:shared"\nAIDLC_MCP_ENABLED=true\nAIDLC_SANDBOX_EXECUTION_ENABLED=true\n'
    )
    for name in ("AIDLC_MODEL", "AIDLC_MCP_ENABLED", "AIDLC_SANDBOX_EXECUTION_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    load_environment(env)
    hub, fleet = Settings(), Settings()
    assert hub.model == fleet.model == "test:shared"
    assert hub.agent_mode == "deep" and hub.mcp_enabled and hub.sandbox_execution_enabled
    monkeypatch.setenv("AIDLC_MODEL", "test:exported")
    load_environment(env)
    assert Settings().model == "test:exported"


def test_phase_token_budget_per_run_configuration(monkeypatch):
    monkeypatch.delenv("AIDLC_TOKEN_BUDGET_PER_PHASE_PER_RUN", raising=False)
    assert Settings().token_budget_per_phase_per_run == 100_000
    monkeypatch.setenv("AIDLC_TOKEN_BUDGET_PER_PHASE_PER_RUN", "200000")
    assert Settings().token_budget_per_phase_per_run == 200_000
    monkeypatch.setenv("AIDLC_TOKEN_BUDGET_PER_PHASE_PER_RUN", "0")
    with pytest.raises(ValueError, match="Phase token budget per run must be greater than zero"):
        Settings()


def test_fleet_rejects_reference_or_disabled_execution(tmp_path):
    settings = live_settings(tmp_path)
    assert len(create_definitions(settings)) == 15
    with pytest.raises(ValueError, match="Reference mode is removed"):
        create_definitions(replace(settings, agent_mode="reference"))
    with pytest.raises(ValueError, match="requires MCP"):
        create_definitions(replace(settings, mcp_enabled=False))


@pytest.mark.parametrize(
    "spec",
    [spec for spec in AGENT_SPECS if spec.stage != StageName.RELEASE],
    ids=lambda spec: spec.name,
)
def test_every_spoke_has_a_real_model_profile(spec, tmp_path):
    context = AgentContext(run_id="run_profile", idea="Build tasks", stage=spec.stage)
    if spec.stage == StageName.EVALUATION:
        context.artifacts = [
            {
                "metadata": {"artifact_id": "art_known", "kind": ArtifactKind.REQUIREMENTS_SPEC},
                "content": {},
            }
        ]
    result = asyncio.run(
        migrate(
            spec, live_settings(tmp_path), model=RoleModel(role=spec.name, responses=[])
        ).handler(context)
    )
    assert result.artifact_kind == spec.artifact_kind
    assert result.execution.model_id == "test:scripted"
    assert result.execution.input_tokens == 20


def test_source_ownership_and_integration(tmp_path):
    context = AgentContext(
        run_id="run_source",
        idea="Tasks",
        stage=StageName.INTEGRATION,
        artifacts=[
            {
                "metadata": {"kind": ArtifactKind.CODE_CHANGE, "producing_agent": role},
                "content": {"files": source_files(role)},
            }
            for role in ("backend-agent", "frontend-agent", "test-agent")
        ],
    )
    combined = merge_sources(context)
    assert {file.path for file in combined.files} >= {
        "backend/api.py",
        "tests/test_api.py",
        "ui/src/main.tsx",
    }
    with pytest.raises(ValueError, match="may write only"):
        validate_source_output(
            "backend-agent", SourceBundle.model_validate({"files": source_files("frontend-agent")})
        )
    context.artifacts.pop()
    with pytest.raises(ValueError, match="all three"):
        merge_sources(context)


def test_live_workflow_calls_all_models_and_validates_integrated_project(tmp_path, monkeypatch):
    from aidlc.agents.deep import build_graph as original_build_graph

    called = set()

    def graph(settings, definition, model=None, *, tools=None):
        called.add(definition.name)
        return original_build_graph(
            settings, definition, RoleModel(role=definition.name, responses=[]), tools=tools
        )

    monkeypatch.setattr("aidlc.agents.deep.build_graph", graph)
    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)

    async def sandbox(self, job, runtime=None):
        assert {file.path for file in job.source.files} >= {
            "backend/api.py",
            "tests/test_api.py",
            "ui/src/main.tsx",
        }
        return SandboxResult(
            status="completed",
            execution=SandboxExecution.model_validate(execution()),
            stderr="Ran 1 test in 0.001s\nOK\n",
            stdout="React production build passed",
        )

    monkeypatch.setattr("aidlc.sandbox.cli.DockerSandbox.execute", sandbox)

    async def analyze(self, request):
        db = WorkflowDatabase(self.settings.database_path)
        record = db.get_artifact(request.artifact_id)
        assert record is not None
        assert record.metadata.kind == ArtifactKind.INTEGRATED_SOURCE
        return AnalysisReport(
            status="completed",
            analysis_id="analysis_live",
            artifact_id=request.artifact_id,
            content_sha256=request.content_sha256,
            run_id=record.metadata.workflow_run_id,
            result_uri="analysis://reports/analysis_live",
            summary={"errors": 0, "warnings": 0},
            execution=AnalyzerExecution.model_validate({"duration_ms": 1, **execution()}),
        )

    monkeypatch.setattr("aidlc.tools.client.ToolsClient.analyze", analyze)

    async def exercise():
        settings = live_settings(tmp_path)
        fleet = create_agent_app(settings)
        db = WorkflowDatabase(settings.database_path)
        db.initialize()
        store = ArtifactStore(settings.artifact_dir)
        async with (
            fleet.router.lifespan_context(fleet),
            httpx.AsyncClient(transport=httpx.ASGITransport(fleet)) as client,
        ):
            hub = WorkflowOrchestrator(settings, db, store, A2AInvoker(settings, db, client))
            run = hub.create_run("Build tasks")
            try:
                await asyncio.wait_for(hub.wait(run.run_id), 30)
                completed = db.get_run(run.run_id)
                assert completed is not None and completed.status == RunStatus.COMPLETED
                assert called == {spec.name for spec in AGENT_SPECS}
                records = db.list_artifacts(run.run_id)
                assert all(item.metadata.project_id == run.project_id for item in records)
                assert all(
                    Path(item.content_path).is_relative_to(store.project_path(run.run_id))
                    for item in records
                )
                descriptor = json.loads(
                    (store.project_path(run.run_id) / "project.json").read_text()
                )
                scope = next(
                    item for item in records if item.metadata.kind == ArtifactKind.PROJECT_BRIEF
                )
                assert descriptor["scope_artifact_id"] == scope.metadata.artifact_id
                release = store.read_content(
                    next(
                        item
                        for item in records
                        if item.metadata.kind == ArtifactKind.RELEASE_BUNDLE
                    )
                )
                assert release["status"] == "released"
                assert "ui/package-lock.json" in {file["path"] for file in release["files"]}
                assert release["source_artifact_id"]
                gate = store.read_content(
                    next(
                        item
                        for item in records
                        if item.metadata.kind == ArtifactKind.QUALITY_GATE_REPORT
                    )
                )
                assert gate["profile"] == "python-react-v1" and gate["verdict"] == "passed"
                evaluation = store.read_content(
                    next(
                        item
                        for item in records
                        if item.metadata.kind == ArtifactKind.EVALUATION_REPORT
                    )
                )
                assert evaluation["quality_gates"] == gate
                events = db.events_after(run.run_id)
                assert sum(event.event_type == "agent.execution" for event in events) == 15
            finally:
                await hub.shutdown()

    asyncio.run(exercise())


def test_model_can_invoke_artifact_bound_mcp_analysis(tmp_path, monkeypatch):
    from aidlc.agents.source import mcp_tools
    from tests.test_evaluation import Evidence, analysis_report

    evidence = Evidence(tmp_path)
    context = evidence.context()
    source = evidence.source
    requested = []

    async def analyze(self, request):
        requested.append(request.artifact_id)
        return AnalysisReport.model_validate(analysis_report(source))

    monkeypatch.setattr("aidlc.tools.client.ToolsClient.analyze", analyze)
    spec = next(spec for spec in AGENT_SPECS if spec.name == "evaluation-agent")
    response = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "analyze_code",
                "id": "analysis",
                "type": "tool_call",
                "args": {"artifact_id": source["metadata"]["artifact_id"]},
            }
        ],
    )
    final = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "EvaluationOutput",
                "id": "review",
                "type": "tool_call",
                "args": output_for(spec.name, context),
            }
        ],
    )
    result = asyncio.run(
        migrate(
            spec, live_settings(tmp_path), model=ScriptedModel(responses=[response, final])
        ).handler(context)
    )
    assert result.content["status"] == "evaluated"
    assert requested == [source["metadata"]["artifact_id"]]
    tool = mcp_tools(live_settings(tmp_path), context, spec)[0]
    with pytest.raises(ValueError, match="input artifact"):
        asyncio.run(tool.ainvoke({"artifact_id": "art_" + "b" * 32}))
