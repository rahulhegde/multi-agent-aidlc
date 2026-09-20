"""Exercise human gates over the real SDK protocol, not direct agent calls."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from aidlc.a2a.server import create_agent_app
from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind, RunStatus
from aidlc.main import create_app
from tests.helpers import reference_definitions
from tests.test_a2a_network import ready_report


@pytest.mark.parametrize("reject", [False, True])
def test_interactive_workflow_and_action_validation(tmp_path, monkeypatch, reject):
    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    monkeypatch.setattr(
        "aidlc.orchestration.workflow.WorkflowOrchestrator._integrate",
        lambda self, run, parents: parents,
    )

    async def exercise():
        settings = Settings(data_dir=tmp_path, human_gates_enabled=True, task_timeout_seconds=2)
        fleet = create_agent_app(settings, definitions=reference_definitions(settings))
        async with fleet.router.lifespan_context(fleet):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fleet)) as agents:
                app = create_app(settings, agent_http_client=agents)
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url="http://hub"
                    ) as http:
                        run_id = (
                            await http.post("/api/runs", json={"idea": "Build a task list"})
                        ).json()["run_id"]
                        observed = []
                        async with asyncio.timeout(15):
                            while True:
                                run = (await http.get(f"/api/runs/{run_id}")).json()
                                if run["status"] in {"completed", "failed"}:
                                    break
                                interactions = (
                                    await http.get(f"/api/runs/{run_id}/interactions")
                                ).json()
                                pending = [item for item in interactions if not item["response"]]
                                if not pending or run["status"] != "input_required":
                                    await asyncio.sleep(0.02)
                                    continue
                                item = pending[0]
                                if not observed:
                                    # Browser refreshes retain the surface, and human thinking
                                    # time must not consume the two-second task timeout.
                                    await asyncio.sleep(2.1)
                                    refreshed = (
                                        await http.get(f"/api/runs/{run_id}/interactions")
                                    ).json()
                                    assert refreshed == interactions
                                observed.append(item)
                                if item["prompt"]["kind"] == "clarification":
                                    decision = "submit"
                                elif item["prompt"]["kind"] == "evaluation_decision":
                                    decision = "repair" if reject else "accept"
                                else:
                                    decision = "reject" if reject else "approve"
                                action = {
                                    "action_id": f"action-{len(observed)}",
                                    "surface_id": item["surface_id"],
                                    "artifact_sha256": item["artifact_sha256"],
                                    "version": item["version"],
                                    "decision": decision,
                                    "answer": "Study group members",
                                }
                                route = f"/api/runs/{run_id}/actions"
                                assert (await http.post(route, json=action)).status_code == 401
                                headers = {"Authorization": f"Bearer {settings.human_action_token}"}
                                assert (
                                    await http.post(
                                        route, json={**action, "version": 99}, headers=headers
                                    )
                                ).status_code == 409
                                response = await http.post(route, json=action, headers=headers)
                                assert response.status_code == 200, response.text
                                assert (
                                    await http.post(route, json=action, headers=headers)
                                ).json() == response.json()
                                assert (
                                    await http.post(
                                        route,
                                        json={**action, "answer": "conflicting"},
                                        headers=headers,
                                    )
                                ).status_code == 409
                        assert run["status"] == (
                            RunStatus.FAILED if reject else RunStatus.COMPLETED
                        ), run
                        assert len(observed) == (2 if reject else 4)
                        remote = (await http.get(f"/api/runs/{run_id}/tasks")).json()
                        for item in observed:
                            mapping = next(
                                task for task in remote if task["task_id"] == item["task_id"]
                            )
                            assert mapping["context_id"] == item["context_id"]
                            assert mapping["state"] == (
                                "failed" if reject and item == observed[-1] else "completed"
                            )
                        artifacts = app.state.database.list_artifacts(run_id)
                        assert sum(
                            item.metadata.kind == ArtifactKind.HUMAN_RESPONSE for item in artifacts
                        ) == len(observed)
                        brief = next(
                            item
                            for item in artifacts
                            if item.metadata.kind == ArtifactKind.PROJECT_BRIEF
                            and app.state.artifacts.read_content(item)["target_user"]
                            == "Study group members"
                        )
                        assert brief

    asyncio.run(exercise())


def test_deep_mode_requires_model(tmp_path):
    with pytest.raises(ValueError, match="AIDLC_MODEL"):
        create_agent_app(
            replace(Settings(data_dir=tmp_path, model="provider:model"), agent_mode="deep")
        )


def test_waiting_run_is_explicitly_blocked_on_hub_restart(tmp_path):
    from aidlc.domain.models import StageName
    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(Settings(data_dir=tmp_path).database_path)
    database.initialize()
    database.create_run("run_waiting", "Build a task list")
    database.update_run("run_waiting", RunStatus.INPUT_REQUIRED, StageName.INTAKE)
    database.recover_interrupted_runs()
    run = database.get_run("run_waiting")
    assert run is not None and run.status == RunStatus.BLOCKED


def test_response_artifact_failure_does_not_accept_action(tmp_path):
    from aidlc.domain.models import HumanAction, HumanInteraction, HumanPrompt, StageName
    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(tmp_path / "aidlc.sqlite3")
    database.initialize()
    database.create_run("run_waiting", "Build a task list")
    database.update_run("run_waiting", RunStatus.INPUT_REQUIRED, StageName.REQUIREMENTS)
    database.save_interaction(
        HumanInteraction(
            surface_id="surface",
            run_id="run_waiting",
            stage=StageName.REQUIREMENTS,
            agent="requirements-agent",
            task_id="task",
            context_id="context",
            prompt=HumanPrompt(kind="approval", question="Approve requirements?"),
            artifact_id="draft",
            artifact_sha256="hash",
            messages=[],
        )
    )

    def unable_to_publish(_interaction):
        raise OSError("Disk unavailable")

    with pytest.raises(OSError, match="Disk unavailable"):
        database.accept_action(
            "run_waiting",
            HumanAction(
                action_id="action", surface_id="surface", artifact_sha256="hash", decision="approve"
            ),
            unable_to_publish,
        )
    assert database.list_interactions("run_waiting")[0].response is None
    assert database.list_artifacts("run_waiting") == []


def test_cancel_while_waiting_over_real_http(tmp_path, monkeypatch):
    from aidlc.orchestration.workflow import WorkflowOrchestrator
    from aidlc.storage.artifacts import ArtifactStore
    from aidlc.storage.database import WorkflowDatabase
    from tests.test_a2a_network import loopback_fleet

    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    monkeypatch.setattr(
        "aidlc.orchestration.workflow.WorkflowOrchestrator._integrate",
        lambda self, run, parents: parents,
    )

    async def exercise():
        async with loopback_fleet(Settings(data_dir=tmp_path), human_gates=True) as settings:
            database = WorkflowDatabase(settings.database_path)
            database.initialize()
            hub = WorkflowOrchestrator(settings, database, ArtifactStore(settings.artifact_dir))
            try:
                run = hub.create_run("Build a task list")
                async with asyncio.timeout(5):
                    # Poll durable workflow state rather than an agent-local event.
                    while not database.list_interactions(run.run_id):  # noqa: ASYNC110
                        await asyncio.sleep(0.02)
                assert hub.cancel(run.run_id)
                await asyncio.wait_for(hub.wait(run.run_id), 5)
                result = database.get_run(run.run_id)
                assert result is not None and result.status == RunStatus.CANCELED
                assert database.list_delegated_tasks(run.run_id)[0].state == "canceled"
            finally:
                await hub.shutdown()

    asyncio.run(exercise())
