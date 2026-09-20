import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.server.context import ServerCallContext
from a2a.types import (
    Artifact,
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from google.protobuf.json_format import MessageToDict

from aidlc.a2a.client import A2AInvoker, RemoteTaskError
from aidlc.a2a.server import agent_card, create_agent_app, json_part
from aidlc.a2a.task_store import SQLiteTaskStore
from aidlc.agents.catalog import AGENT_SPECS
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, ArtifactKind, StageName
from aidlc.storage.database import WorkflowDatabase
from tests.helpers import reference_definitions, reference_transport
from tests.reference_agents import AGENTS_BY_STAGE

SPEC = AGENT_SPECS[0]


def request(idea="Build a task list", message_id="test-message"):
    return SendMessageRequest(
        message=Message(
            message_id=message_id,
            context_id="test-context",
            role="ROLE_USER",
            parts=[
                json_part(
                    AgentContext(run_id="run_test", idea=idea, stage=StageName.INTAKE).model_dump(
                        mode="json"
                    )
                )
            ],
        )
    )


async def sdk_client(http, settings, *, streaming=True, polling=False, name=SPEC.name):
    card = await A2ACardResolver(http, f"{settings.a2a_base_url}/agents/{name}").get_agent_card()
    return ClientFactory(
        ClientConfig(
            httpx_client=http,
            streaming=streaming,
            polling=polling,
            supported_protocol_bindings=["JSONRPC"],
            accepted_output_modes=["application/json"],
        )
    ).create(card)


def test_cards_and_stream_contract(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path, human_gates_enabled=False)
        async with reference_transport(settings) as http:
            for spec in AGENT_SPECS:
                card = await A2ACardResolver(
                    http, f"{settings.a2a_base_url}/agents/{spec.name}"
                ).get_agent_card()
                assert card.name == spec.name
                assert card.supported_interfaces[0].protocol_version == "1.0"
                assert card.capabilities.streaming
                assert card.skills[0].id == spec.stage
                assert list(card.default_input_modes) == ["application/json"]
                assert not card.security_requirements
            client = await sdk_client(http, settings)
            events = [event async for event in client.send_message(request())]
            assert events[0].HasField("task")
            states = [
                event.status_update.status.state
                for event in events
                if event.HasField("status_update")
            ]
            assert states == [TaskState.TASK_STATE_WORKING, TaskState.TASK_STATE_COMPLETED]
            assert sum(event.HasField("artifact_update") for event in events) == 1
            task = await client.get_task(GetTaskRequest(id=events[0].task.id))
            assert task.status.state == TaskState.TASK_STATE_COMPLETED
            assert (
                MessageToDict(task.artifacts[0].parts[0].data)["artifact_kind"] == "project_brief"
            )
            page = await client.list_tasks(ListTasksRequest(page_size=1, include_artifacts=True))
            assert page.total_size == 1
            assert len(page.tasks[0].artifacts) == 1
            with pytest.raises(TaskNotCancelableError):
                await client.cancel_task(CancelTaskRequest(id=task.id))
            with pytest.raises(TaskNotFoundError):
                await client.get_task(GetTaskRequest(id="missing"))

    asyncio.run(exercise())


def test_duplicate_messages_replay_same_task_across_restart(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path)
        async with reference_transport(settings) as http:
            client = await sdk_client(http, settings, streaming=False)
            first = [event async for event in client.send_message(request())][0].task
            duplicate = [event async for event in client.send_message(request())][0].task
            assert first.id == duplicate.id
            with pytest.raises(InvalidParamsError, match="different input"):
                _ = [event async for event in client.send_message(request("Different idea"))]
        async with reference_transport(settings) as http:
            client = await sdk_client(http, settings)
            replay = [event async for event in client.send_message(request())]
            assert len(replay) == 1
            assert replay[0].task.id == first.id
            assert replay[0].task.status.state == TaskState.TASK_STATE_COMPLETED
            other = await sdk_client(http, settings, name="requirements-agent")
            with pytest.raises(TaskNotFoundError):
                await other.get_task(GetTaskRequest(id=first.id))

    asyncio.run(exercise())


def test_concurrent_duplicates_have_one_logical_execution(tmp_path, monkeypatch):
    original = AGENTS_BY_STAGE[StageName.INTAKE][0]
    executions = 0

    async def slow(context):
        nonlocal executions
        executions += 1
        await asyncio.sleep(0.05)
        return await original.handler(context)

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(original, handler=slow),))

    async def exercise():
        settings = Settings(data_dir=tmp_path)
        async with reference_transport(settings) as http:
            client = await sdk_client(http, settings)

            async def send():
                return [event async for event in client.send_message(request())]

            first, second = await asyncio.gather(send(), send())
            assert first[0].task.id == second[0].task.id
            assert executions == 1
            assert second[-1].status_update.status.state == TaskState.TASK_STATE_COMPLETED

    asyncio.run(exercise())


def test_input_and_version_rejections(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path)
        async with reference_transport(settings) as http:
            client = await sdk_client(http, settings)
            invalid = request()
            invalid.message.parts[0].CopyFrom(Part(text="not structured input"))
            with pytest.raises(ContentTypeNotSupportedError):
                _ = [event async for event in client.send_message(invalid)]
            wrong_stage = request()
            wrong_stage.message.parts[0].CopyFrom(
                json_part(
                    {"run_id": "run_test", "idea": "Build a task list", "stage": "requirements"}
                )
            )
            with pytest.raises(InvalidParamsError):
                _ = [event async for event in client.send_message(wrong_stage)]
            response = await http.post(
                f"{settings.a2a_base_url}/agents/{SPEC.name}/rpc",
                headers={"A2A-Version": "9.9"},
                json={
                    "jsonrpc": "2.0",
                    "id": "version-check",
                    "method": "GetTask",
                    "params": {"id": "missing"},
                },
            )
            assert "error" in response.json()
            assert "version" in response.json()["error"]["message"].lower()

    asyncio.run(exercise())


def test_cancel_running_task_and_failed_task(tmp_path, monkeypatch):
    original = AGENTS_BY_STAGE[StageName.INTAKE][0]
    started = None

    async def slow(context):
        assert started is not None
        started.set()
        await asyncio.sleep(10)
        return await original.handler(context)

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(original, handler=slow),))

    async def exercise():
        nonlocal started
        started = asyncio.Event()
        settings = Settings(data_dir=tmp_path)
        async with reference_transport(settings) as http:
            client = await sdk_client(http, settings, streaming=False, polling=True)
            initial = [event async for event in client.send_message(request())][0].task
            await asyncio.wait_for(started.wait(), 2)
            canceled = await client.cancel_task(CancelTaskRequest(id=initial.id))
            assert canceled.status.state == TaskState.TASK_STATE_CANCELED
            assert (
                await client.get_task(GetTaskRequest(id=initial.id))
            ).status.state == TaskState.TASK_STATE_CANCELED

    asyncio.run(exercise())

    async def fail(_context):
        raise ValueError("Expected learning-test failure")

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(original, handler=fail),))

    async def check_failure():
        settings = Settings(data_dir=tmp_path / "failed")
        async with reference_transport(settings) as http:
            database = WorkflowDatabase(settings.database_path)
            database.initialize()
            database.create_run("run_test", "Build a task list")
            invoker = A2AInvoker(settings, database, http)
            with pytest.raises(RemoteTaskError, match="learning-test failure"):
                await invoker.invoke(
                    SPEC,
                    AgentContext(
                        run_id="run_test", idea="Build a task list", stage=StageName.INTAKE
                    ),
                    database.append_event,
                )
            assert database.list_delegated_tasks("run_test")[0].state == "failed"

    asyncio.run(check_failure())


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, prefix: bytes):
        self.prefix = prefix

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix
        raise httpx.ReadError("Injected lost A2A stream")


class FaultTransport(httpx.AsyncBaseTransport):
    """Inject a transport fault after the real SDK server has accepted a message."""

    def __init__(self, app, *, first_event=False, unavailable=False):
        self.inner = httpx.ASGITransport(app=app)
        self.first_event = first_event
        self.unavailable = unavailable
        self.sends = 0

    async def handle_async_request(self, request):
        if self.unavailable:
            raise httpx.ConnectError("Injected unavailable agent", request=request)
        payload = json.loads(request.content) if request.method == "POST" else {}
        if payload.get("method") == "SendStreamingMessage":
            self.sends += 1
            response = await self.inner.handle_async_request(request)
            if self.sends == 1:
                body = await response.aread()
                await response.aclose()
                prefix = body.split(b"\n\n", 1)[0] + b"\n\n" if self.first_event else b""
                return httpx.Response(200, headers=response.headers, stream=BrokenStream(prefix))
            return response
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


@pytest.mark.parametrize("first_event,expected_sends", [(False, 2), (True, 1)])
def test_lost_stream_recovery_without_duplicate_execution(
    tmp_path, monkeypatch, first_event, expected_sends
):
    original = AGENTS_BY_STAGE[StageName.INTAKE][0]
    executions = 0

    async def counted(context):
        nonlocal executions
        executions += 1
        return await original.handler(context)

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(original, handler=counted),))

    async def exercise():
        settings = Settings(data_dir=tmp_path, human_gates_enabled=False)
        app = create_agent_app(settings, definitions=reference_definitions(settings))
        transport = FaultTransport(app, first_event=first_event)
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        database.create_run("run_test", "Build a task list")
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport) as http:
            invoker = A2AInvoker(settings, database, http)
            result = await invoker.invoke(
                SPEC,
                AgentContext(run_id="run_test", idea="Build a task list", stage=StageName.INTAKE),
                database.append_event,
            )
            assert result.artifact_kind == ArtifactKind.PROJECT_BRIEF
            assert transport.sends == expected_sends
            assert executions == 1
            mapping = database.list_delegated_tasks("run_test")[0]
            assert mapping.attempts == 2 and mapping.state == "completed"
            assert (
                len(
                    [
                        event
                        for event in database.events_after("run_test")
                        if event.event_type == "a2a.retry"
                    ]
                )
                == 1
            )

    asyncio.run(exercise())


def test_retry_limit_for_unavailable_agents(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path)
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        database.create_run("run_test", "Build a task list")
        transport = FaultTransport(
            create_agent_app(settings, definitions=reference_definitions(settings)),
            unavailable=True,
        )
        async with httpx.AsyncClient(transport=transport) as http:
            with pytest.raises(Exception, match="unavailable agent"):
                await A2AInvoker(settings, database, http).invoke(
                    SPEC,
                    AgentContext(
                        run_id="run_test", idea="Build a task list", stage=StageName.INTAKE
                    ),
                    database.append_event,
                )
        mapping = database.list_delegated_tasks("run_test")[0]
        assert mapping.attempts == 3 and mapping.state == "failed"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "parts",
    [
        [],
        [Part(text="wrong output")],
        [json_part({"wrong": "schema"})],
        [
            json_part(
                {"artifact_kind": "requirements_spec", "content": {}, "markdown": "Wrong kind"}
            )
        ],
    ],
)
def test_invalid_result_artifacts_are_rejected(parts):
    task = Task(
        id="task_test",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[Artifact(artifact_id="artifact_test", parts=parts)],
    )
    with pytest.raises(ValueError):
        A2AInvoker._result(SPEC, task)


def test_timeout_recovers_task_when_first_response_was_not_received(tmp_path, monkeypatch):
    original = AGENTS_BY_STAGE[StageName.INTAKE][0]

    async def slow(context):
        await asyncio.sleep(10)
        return await original.handler(context)

    monkeypatch.setitem(AGENTS_BY_STAGE, StageName.INTAKE, (replace(original, handler=slow),))

    async def exercise():
        settings = Settings(data_dir=tmp_path, task_timeout_seconds=1)
        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        database.create_run("run_test", "Build a task list")
        async with reference_transport(settings) as http:
            with pytest.raises(TimeoutError):
                await A2AInvoker(settings, database, http).invoke(
                    SPEC,
                    AgentContext(
                        run_id="run_test", idea="Build a task list", stage=StageName.INTAKE
                    ),
                    database.append_event,
                )
        task = database.list_delegated_tasks("run_test")[0]
        assert task.task_id is not None and task.state == "canceled"

    asyncio.run(exercise())


def test_interrupted_fleet_tasks_become_failed_without_rerun(tmp_path):
    async def exercise():
        path = tmp_path / "a2a.sqlite3"
        context = ServerCallContext()
        store = SQLiteTaskStore(path, SPEC.name)
        task, created = store.reserve(request().message, context)
        assert created
        task.status.state = TaskState.TASK_STATE_WORKING
        await store.save(task, context)
        restored = SQLiteTaskStore(path, SPEC.name)
        replay, created = restored.reserve(request().message, context)
        assert not created and replay.id == task.id
        assert replay.status.state == TaskState.TASK_STATE_FAILED

    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["identity", "version", "endpoint", "extra_interface"])
def test_unexpected_agent_cards_are_rejected_without_delegating(tmp_path, fault):
    async def exercise():
        settings = Settings(data_dir=tmp_path)
        card = agent_card(SPEC, settings)
        if fault == "identity":
            card.name = "someone-else"
        elif fault == "version":
            card.supported_interfaces[0].protocol_version = "0.3"
        elif fault == "endpoint":
            card.supported_interfaces[0].url = "http://unexpected.invalid/rpc"
        else:
            interface = card.supported_interfaces.add()
            interface.CopyFrom(card.supported_interfaces[0])
            interface.url = "http://unexpected.invalid/rpc"

        def respond(req):
            assert req.method == "GET"  # No delegation to any advertised endpoint.
            return httpx.Response(200, json=MessageToDict(card))

        database = WorkflowDatabase(settings.database_path)
        database.initialize()
        database.create_run("run_test", "Build a task list")
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises(ValueError, match="Agent Card"):
                await A2AInvoker(settings, database, http).invoke(
                    SPEC,
                    AgentContext(
                        run_id="run_test", idea="Build a task list", stage=StageName.INTAKE
                    ),
                    database.append_event,
                )
        assert database.list_delegated_tasks("run_test")[0].attempts == 1

    asyncio.run(exercise())
