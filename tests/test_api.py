import asyncio
from dataclasses import replace

import httpx

from aidlc.config import Settings
from aidlc.domain.models import HostCapabilityReport, RunStatus
from aidlc.main import create_app
from tests.helpers import reference_transport


def test_retry_reuses_completed_stages_and_successful_parallel_agents(tmp_path, monkeypatch):
    from aidlc.domain.models import StageName
    from tests.reference_agents import AGENTS_BY_STAGE
    from tests.test_a2a_network import ready_report

    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    calls = {}
    definitions = []
    for definition in AGENTS_BY_STAGE[StageName.IMPLEMENTATION]:
        original = definition.handler
        name = definition.name

        async def implementation(context, original=original, name=name):
            calls[name] = calls.get(name, 0) + 1
            if name == "backend-agent" and calls[name] == 1:
                await asyncio.sleep(0.2)
                raise RuntimeError("temporary backend failure")
            return await original(context)

        definitions.append(replace(definition, handler=implementation))
    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.IMPLEMENTATION, tuple(definitions))

    async def exercise():
        settings = Settings(data_dir=tmp_path, enforce_host_preflight=False)
        async with reference_transport(settings) as agents:
            app = create_app(settings, agent_http_client=agents)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://hub",
                ) as client:
                    response = await client.post("/api/runs", json={"idea": "Build a task list"})
                    run_id = response.json()["run_id"]
                    hub = app.state.orchestrator
                    await hub.wait(run_id)
                    database = app.state.database
                    assert database.get_run(run_id).status == RunStatus.FAILED
                    before = {
                        item.metadata.producing_agent: item.metadata.artifact_id
                        for item in database.list_artifacts(run_id)
                    }
                    assert "frontend-agent" in before and "test-agent" in before
                    # Simulate a stale hub task state after losing an A2A update.
                    previous = next(
                        item
                        for item in database.list_delegated_tasks(run_id)
                        if item.agent == "backend-agent"
                    )
                    previous.state = "working"
                    database.save_delegated_task(previous)
                    assert (await client.post("/api/runs/missing/retry")).status_code == 404
                    response = await client.post(f"/api/runs/{run_id}/retry")
                    assert response.status_code == 202, response.text
                    assert response.json()["run_id"] == run_id
                    assert (await client.post(f"/api/runs/{run_id}/retry")).status_code == 409
                    await hub.wait(run_id)
                    assert database.get_run(run_id).status == RunStatus.COMPLETED
                    after = {
                        item.metadata.producing_agent: item.metadata.artifact_id
                        for item in database.list_artifacts(run_id)
                    }
                    for name in (
                        "intake-agent",
                        "requirements-agent",
                        "planning-agent",
                        "frontend-agent",
                        "test-agent",
                    ):
                        assert after[name] == before[name]
                    assert calls == {"backend-agent": 2, "frontend-agent": 1, "test-agent": 1}
                    tasks = database.list_delegated_tasks(run_id)
                    backend_tasks = [item for item in tasks if item.agent == "backend-agent"]
                    assert len(backend_tasks) == 2
                    assert backend_tasks[0].task_id != backend_tasks[1].task_id
                    assert ":retry:1" in backend_tasks[1].invocation_id
                    assert (await client.post(f"/api/runs/{run_id}/retry")).status_code == 409

    asyncio.run(exercise())


def test_api_creates_and_exposes_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "aidlc.orchestration.workflow.inspect_host",
        lambda _settings: HostCapabilityReport(
            ready=True,
            degraded=False,
            os_id="ubuntu",
            os_version="26.04",
            architecture="x86_64",
            kernel="test",
            checks=[],
        ),
    )

    async def exercise():
        settings = Settings(data_dir=tmp_path, enforce_host_preflight=False)
        async with reference_transport(settings) as agents:
            app = create_app(settings, agent_http_client=agents)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://hub"
                ) as client:
                    response = await client.post(
                        "/api/runs", json={"idea": "Build a tiny task list"}
                    )
                    assert response.status_code == 202
                    run_id = response.json()["run_id"]
                    await app.state.orchestrator.wait(run_id)
                    result = await client.get(f"/api/runs/{run_id}")
                    assert result.json()["status"] == RunStatus.COMPLETED, result.json()
                    artifacts = await client.get(f"/api/runs/{run_id}/artifacts")
                    assert len(artifacts.json()) == 17
                    artifact_id = artifacts.json()[0]["metadata"]["artifact_id"]
                    assert "content" in (await client.get(f"/api/artifacts/{artifact_id}")).json()
                    assert (
                        "event: run.completed"
                        in (await client.get(f"/api/runs/{run_id}/events")).text
                    )
                    assert len((await client.get(f"/api/runs/{run_id}/tasks")).json()) == 15
                    finops = (await client.get(f"/api/runs/{run_id}/finops")).json()
                    assert finops["totals"]["context_selections"] == 15
                    assert finops["totals"]["context_bytes_saved"] > 0
                    assert (await client.get("/api/runs/missing/finops")).status_code == 404
                    evaluation = (await client.get(f"/api/runs/{run_id}/evaluation")).json()
                    assert evaluation["quality_gates"]["verdict"] == "blocked"
                    assert evaluation["agent_evaluation"]["status"] == "not_evaluated"
                    assert evaluation["decision"]["decision"] == "deferred"
                    assert evaluation["decision"]["overall_score"] is None
                    assert evaluation["repair_attempts"] == 0
                    assert (await client.get("/api/runs/missing/evaluation")).status_code == 404
                    assert (await client.post("/api/runs", json={"idea": "   "})).status_code == 422

    asyncio.run(exercise())
