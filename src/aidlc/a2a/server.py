from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import Event, EventQueue
from a2a.server.request_handlers import DefaultRequestHandler, validate_request_params
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    CancelTaskRequest,
    Message,
    Part,
    SendMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)
from a2a.utils.errors import (
    ContentTypeNotSupportedError,
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from aidlc import __version__
from aidlc.a2a.task_store import SQLiteTaskStore
from aidlc.agents.base import AgentDefinition
from aidlc.agents.catalog import AGENT_SPECS, AgentSpec
from aidlc.agents.live import create_definitions
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, AgentResult, HumanPrompt, StageName

INTERRUPTING_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
}


def json_part(content: dict) -> Part:
    return Part(data=ParseDict(content, Value()), media_type="application/json")


class SpecialistExecutor(AgentExecutor):
    """The only place where protocol input becomes a specialist handler call."""

    def __init__(self, definition: AgentDefinition, settings: Settings | None = None) -> None:
        self.definition = definition
        self.settings = settings or Settings(human_gates_enabled=False)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id, context_id = context.task_id or "", context.context_id or ""
        updater = TaskUpdater(event_queue, task_id, context_id)
        # The handler has already persisted the initial task reservation.
        # Only publish a new Task when used without that reservation.
        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                    history=[context.message] if context.message else [],
                )
            )
        await updater.start_work()
        try:
            assert context.message is not None
            payload = MessageToDict(context.message.parts[0].data)
            agent_context = AgentContext.model_validate(payload)
            prior = context.current_task
            if agent_context.human_response and prior is not None and prior.artifacts:
                result = AgentResult.model_validate(
                    MessageToDict(prior.artifacts[-1].parts[0].data)
                )
                response = agent_context.human_response
                canonical = json.dumps(
                    result.content, sort_keys=True, separators=(",", ":")
                ).encode()
                if response.get("artifact_sha256") != hashlib.sha256(canonical).hexdigest():
                    raise ValueError("Human response does not match the waiting artifact hash")
                original = AgentContext.model_validate(
                    MessageToDict(prior.history[0].parts[0].data)
                )
                if agent_context.model_dump(exclude={"human_response"}) != original.model_dump(
                    exclude={"human_response"}
                ):
                    raise ValueError("Continuation changed the original agent context")
                if response.get("decision") == "reject":
                    await updater.failed(
                        updater.new_agent_message([Part(text="Human rejected draft")])
                    )
                    return
                if result.human_prompt and result.human_prompt.kind == "approval":
                    if response.get("decision") != "approve":
                        raise ValueError("Approval requires an approve or reject decision")
                    result.human_prompt = None
                elif result.human_prompt and result.human_prompt.kind == "evaluation_decision":
                    if response.get("decision") not in {"accept", "repair"}:
                        raise ValueError("Evaluation requires an accept or repair decision")
                    result.human_prompt = None
                else:
                    result = await self.definition.handler(agent_context)
            else:
                result = await self.definition.handler(agent_context)
                gates = {
                    StageName.REQUIREMENTS: self.settings.require_requirements_approval,
                    StageName.PLANNING: self.settings.require_plan_approval,
                }
                if self.settings.human_gates_enabled and gates.get(self.definition.stage):
                    result.human_prompt = HumanPrompt(
                        kind="approval", question=f"Approve the {self.definition.stage} artifact?"
                    )
                if (
                    self.settings.human_gates_enabled
                    and self.definition.stage == StageName.EVALUATION
                    and self.settings.require_release_approval
                ):
                    result.human_prompt = HumanPrompt(
                        kind="evaluation_decision",
                        question=(
                            "Review the quality gates and model evaluation. Your decision is "
                            "final: accept the evaluated result and continue to release, or "
                            "send it to repair."
                        ),
                    )
            await updater.add_artifact(
                [json_part(result.model_dump(mode="json"))],
                name=result.artifact_kind.value,
                artifact_id=f"{task_id}:result",
                last_chunk=True,
            )
            if result.human_prompt:
                await updater.requires_input()
            else:
                await updater.complete()
        except asyncio.CancelledError:
            # The SDK calls cancel() and owns the terminal cancellation event.
            raise
        except Exception as error:
            await updater.failed(
                updater.new_agent_message([Part(text=f"{type(error).__name__}: {error}")])
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


class IdempotentHandler(DefaultRequestHandler):
    """Reuse a durable task for a repeated messageId, including concurrent retries."""

    def __init__(
        self,
        executor: SpecialistExecutor,
        store: SQLiteTaskStore,
        card: AgentCard,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        super().__init__(agent_executor=executor, task_store=store, agent_card=card)
        self.store = store
        self.stage = executor.definition.stage
        self.poll_interval_seconds = poll_interval_seconds

    def _prepare(self, params: SendMessageRequest, context: ServerCallContext) -> tuple[Task, bool]:
        modes = params.configuration.accepted_output_modes
        if modes and "application/json" not in modes:
            raise ContentTypeNotSupportedError("This specialist produces application/json")
        parts = params.message.parts
        if (
            len(parts) != 1
            or not parts[0].HasField("data")
            or parts[0].media_type != "application/json"
        ):
            raise ContentTypeNotSupportedError("Expected one application/json data part")
        try:
            payload = AgentContext.model_validate(MessageToDict(parts[0].data))
        except ValueError as error:
            raise InvalidParamsError("Invalid AgentContext payload") from error
        if payload.stage != self.stage:
            raise InvalidParamsError("This specialist does not support the requested stage")
        if params.message.task_id and payload.human_response is None:
            raise InvalidParamsError("A continuation requires a human response")
        task, created = self.store.reserve(params.message, context)
        if created:
            # The reservation is already persisted before SDK execution starts.
            params.message.task_id = task.id
            params.message.context_id = task.context_id
        return task, created

    @validate_request_params
    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        task, created = self._prepare(params, context)
        if created:
            return await super().on_message_send(params, context)
        if not params.configuration.return_immediately:
            while task.status.state not in INTERRUPTING_STATES:
                await asyncio.sleep(self.poll_interval_seconds)
                task = await self._snapshot(task.id, context)
        return task

    @validate_request_params
    async def on_message_send_stream(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncIterator[Event]:
        task, created = self._prepare(params, context)
        if not created:
            async for event in self._replay(task, context):
                yield event
            return
        first = True
        async for event in super().on_message_send_stream(params, context):
            if first:
                # Execution is now started; A2A requires a Task first on the wire.
                yield task
                first = False
            yield event

    async def _snapshot(self, task_id: str, context: ServerCallContext) -> Task:
        task = await self.store.get(task_id, context)
        if task is None:
            raise TaskNotFoundError()
        return task

    async def _replay(self, task: Task, context: ServerCallContext) -> AsyncIterator[Event]:
        """Replay a durable snapshot, then tail updates without executing again."""
        yield task
        seen = {artifact.artifact_id for artifact in task.artifacts}
        while task.status.state not in INTERRUPTING_STATES:
            await asyncio.sleep(self.poll_interval_seconds)
            latest = await self._snapshot(task.id, context)
            for artifact in latest.artifacts:
                if artifact.artifact_id not in seen:
                    seen.add(artifact.artifact_id)
                    yield TaskArtifactUpdateEvent(
                        task_id=task.id,
                        context_id=task.context_id,
                        artifact=artifact,
                        last_chunk=True,
                    )
            if latest.status != task.status:
                yield TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=latest.status,
                )
            task = latest

    @validate_request_params
    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        task = await self.store.get(params.id, context)
        if task is None:
            raise TaskNotFoundError()
        if task.status.state in {
            TaskState.TASK_STATE_COMPLETED,
            TaskState.TASK_STATE_CANCELED,
            TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED,
        }:
            raise TaskNotCancelableError("Task is already terminal")
        return await super().on_cancel_task(params, context)


def agent_card(spec: AgentSpec, settings: Settings) -> AgentCard:
    return AgentCard(
        name=spec.name,
        version=__version__,
        description=f"AIDLC {spec.stage} specialist; mode={settings.agent_mode}; loopback only.",
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[
            AgentSkill(
                id=spec.stage.value,
                name=spec.name,
                description=f"Produce {spec.artifact_kind}",
                tags=[
                    "aidlc",
                    "deep",
                    spec.stage.value,
                ],
                input_modes=["application/json"],
                output_modes=["application/json"],
            )
        ],
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url=f"{settings.a2a_base_url.rstrip('/')}/agents/{spec.name}/rpc",
            )
        ],
        # Empty security requirements deliberately mean unauthenticated local development.
    )


def create_agent_app(
    settings: Settings | None = None, *, definitions: dict[str, AgentDefinition] | None = None
) -> FastAPI:
    configured = settings or Settings()
    handlers: list[IdempotentHandler] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as services:
            if configured.sandbox_execution_enabled:
                from aidlc.sandbox.recovery import recovery_service

                await services.enter_async_context(recovery_service(configured))
            try:
                yield
            finally:
                await asyncio.gather(*(handler.aclose() for handler in handlers))

    app = FastAPI(title="AIDLC A2A specialist fleet", version=__version__, lifespan=lifespan)
    definitions = definitions if definitions is not None else create_definitions(configured)
    if set(definitions) != {spec.name for spec in AGENT_SPECS}:
        raise ValueError("Every spoke must have an implementation")
    for spec in AGENT_SPECS:
        card = agent_card(spec, configured)
        store = SQLiteTaskStore(configured.data_dir / "a2a-tasks.sqlite3", spec.name)
        handler = IdempotentHandler(
            SpecialistExecutor(definitions[spec.name], configured),
            store,
            card,
            poll_interval_seconds=configured.a2a_poll_interval_seconds,
        )
        handlers.append(handler)
        prefix = f"/agents/{spec.name}"
        add_a2a_routes_to_fastapi(
            app,
            agent_card_routes=create_agent_card_routes(
                card, card_url=f"{prefix}/.well-known/agent-card.json"
            ),
            jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url=f"{prefix}/rpc"),
        )

    @app.get("/health")
    async def health():
        return {"status": "ok", "protocol": "1.0", "agents": len(handlers)}

    app.state.handlers = handlers
    return app


def run() -> None:
    from urllib.parse import urlsplit

    import uvicorn

    settings = Settings()
    endpoint = urlsplit(settings.a2a_base_url)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"localhost", "127.0.0.1"}
        or endpoint.path not in {"", "/"}
        or endpoint.username is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("The unauthenticated development fleet must bind to a loopback HTTP URL")
    uvicorn.run(create_agent_app(settings), host=endpoint.hostname, port=endpoint.port or 8001)
