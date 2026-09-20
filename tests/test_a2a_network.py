"""Loopback integration tests: real HTTP/SSE, not ASGITransport buffering."""

import asyncio
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

import pytest
import uvicorn
from sse_starlette.sse import AppStatus

from aidlc.a2a.server import create_agent_app
from aidlc.config import Settings
from aidlc.domain.models import HostCapabilityReport, RunStatus, StageName
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from tests.helpers import reference_definitions
from tests.reference_agents import AGENTS_BY_STAGE


@asynccontextmanager
async def loopback_fleet(settings, *, human_gates=False):
    # Uvicorn's shutdown hook sets SSE's process-global exit flag. Restore it
    # so later in-process ASGI tests do not inherit a stopped SSE service.
    previous_exit = AppStatus.should_exit
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        settings = replace(
            settings,
            human_gates_enabled=human_gates,
            a2a_base_url=f"http://127.0.0.1:{listener.getsockname()[1]}",
        )
        server = uvicorn.Server(
            uvicorn.Config(
                create_agent_app(settings, definitions=reference_definitions(settings)),
                log_level="critical",
                lifespan="on",
                timeout_graceful_shutdown=3,
            )
        )
        worker = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if worker.done():
                        await worker
                        raise RuntimeError("Fleet did not start")
                    await asyncio.sleep(0.01)
            with patch(
                "aidlc.orchestration.workflow.WorkflowOrchestrator._integrate",
                lambda self, run, parents: parents,
            ):
                yield settings
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(worker, 5)
            finally:
                AppStatus.should_exit = previous_exit


def ready_report(_settings):
    return HostCapabilityReport(
        ready=True,
        degraded=False,
        os_id="ubuntu",
        os_version="26.04",
        architecture="x86_64",
        kernel="test",
        checks=[],
    )


def test_loopback_workflow_has_serial_boundaries_and_bounded_parallelism(tmp_path, monkeypatch):
    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    active, peak = 0, 0

    def instrument(definition):
        async def handler(context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.05)
                return await definition.handler(context)
            finally:
                active -= 1

        return replace(definition, handler=handler)

    monkeypatch.setitem(
        AGENTS_BY_STAGE,
        StageName.DISCOVERY,
        tuple(instrument(item) for item in AGENTS_BY_STAGE[StageName.DISCOVERY]),
    )

    async def exercise():
        async with loopback_fleet(Settings(data_dir=tmp_path, max_parallel_agents=2)) as settings:
            database = WorkflowDatabase(settings.database_path)
            database.initialize()
            hub = WorkflowOrchestrator(settings, database, ArtifactStore(settings.artifact_dir))
            try:
                run = hub.create_run("Build a task list")
                await asyncio.wait_for(hub.wait(run.run_id), 8)
                result = database.get_run(run.run_id)
                assert result is not None and result.status == RunStatus.COMPLETED
                assert peak == 2 and active == 0
                events = database.events_after(run.run_id)
                ordered = [(event.event_type, event.payload.get("stage")) for event in events]
                assert ordered.index(("stage.completed", "requirements")) < ordered.index(
                    ("stage.started", "discovery")
                )
                assert ordered.index(("stage.completed", "discovery")) < ordered.index(
                    ("stage.started", "planning")
                )
                tasks = database.list_delegated_tasks(run.run_id)
                assert len(tasks) == 15
                assert all(task.state == "completed" and task.attempts == 1 for task in tasks)
            finally:
                await hub.shutdown()

    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_loopback_cancellation_and_timeout_reach_remote_task(tmp_path, monkeypatch, mode):
    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    definition = AGENTS_BY_STAGE[StageName.INTAKE][0]

    async def slow(context):
        await asyncio.sleep(10)
        return await definition.handler(context)

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(definition, handler=slow),))

    async def exercise():
        async with loopback_fleet(
            Settings(data_dir=tmp_path, task_timeout_seconds=1 if mode == "timeout" else 10)
        ) as settings:
            database = WorkflowDatabase(settings.database_path)
            database.initialize()
            hub = WorkflowOrchestrator(settings, database, ArtifactStore(settings.artifact_dir))
            try:
                run = hub.create_run("Build a task list")
                async with asyncio.timeout(3):
                    while True:
                        tasks = database.list_delegated_tasks(run.run_id)
                        if tasks and tasks[0].state == "working":
                            break
                        await asyncio.sleep(0.01)
                if mode == "cancel":
                    assert hub.cancel(run.run_id)
                await asyncio.wait_for(hub.wait(run.run_id), 5)
                result = database.get_run(run.run_id)
                assert result is not None
                assert result.status == (
                    RunStatus.CANCELED if mode == "cancel" else RunStatus.FAILED
                )
                assert database.list_delegated_tasks(run.run_id)[0].state == "canceled"
                assert any(
                    event.event_type == "a2a.cancellation_result"
                    for event in database.events_after(run.run_id)
                )
            finally:
                await hub.shutdown()

    asyncio.run(exercise())
