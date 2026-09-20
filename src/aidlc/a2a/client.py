from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, suppress
from typing import cast

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.types import (
    AgentCard,
    CancelTaskRequest,
    GetTaskRequest,
    Message,
    Part,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskState,
)
from a2a.utils.errors import TaskNotCancelableError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.struct_pb2 import Value

from aidlc.agents.catalog import AGENT_SPECS, AgentSpec
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, AgentResult, DelegatedTask
from aidlc.storage.database import WorkflowDatabase

EventSink = Callable[[str, str, dict[str, object]], object]
TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
    TaskState.TASK_STATE_INPUT_REQUIRED,
    TaskState.TASK_STATE_AUTH_REQUIRED,
}


class RemoteTaskError(RuntimeError):
    pass


class RemoteTaskCanceled(RemoteTaskError):
    pass


def _transient(error: BaseException) -> bool:
    # SDK transport exceptions retain the HTTP error as their cause.
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code >= 500 or error.response.status_code in {408, 429}
    if isinstance(error, httpx.RequestError):
        return True
    return error.__cause__ is not None and _transient(error.__cause__)


def _validate_card(card: AgentCard, spec: AgentSpec, endpoint: str) -> None:
    interfaces = list(card.supported_interfaces)
    if (
        card.name != spec.name
        or not card.capabilities.streaming
        or "application/json" not in card.default_input_modes
        or "application/json" not in card.default_output_modes
        or card.security_requirements
        or not any(skill.id == spec.stage.value for skill in card.skills)
        or len(interfaces) != 1
        or interfaces[0].protocol_version != "1.0"
        or interfaces[0].protocol_binding != "JSONRPC"
        or interfaces[0].url != endpoint
    ):
        # Reject unexpected interfaces rather than letting SDK negotiation redirect requests.
        raise ValueError("Agent Card does not match the pinned A2A contract")


class A2AInvoker:
    """Hub-side SDK client: discovery, streamed status, recovery, and cancellation."""

    def __init__(
        self,
        settings: Settings,
        database: WorkflowDatabase,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._http = http_client
        self._owns_http = http_client is None

    async def close(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def reconcile_run(self, run_id: str, emit: EventSink) -> None:
        """Do not launch duplicate work while a previous remote task is still active."""
        terminal = {"completed", "failed", "canceled", "rejected"}
        for mapping in self.database.list_delegated_tasks(run_id):
            if mapping.state in terminal:
                continue
            if not mapping.task_id:
                raise RuntimeError(
                    "Previous remote task identity is unknown; retry cannot safely proceed"
                )
            if self._http is None:
                self._http = httpx.AsyncClient(
                    timeout=self.settings.task_timeout_seconds,
                    trust_env=False,
                )
            spec = next(item for item in AGENT_SPECS if item.name == mapping.agent)
            base = f"{self.settings.a2a_base_url.rstrip('/')}/agents/{spec.name}"
            async with asyncio.timeout(self.settings.a2a_cancel_timeout_seconds):
                card = await A2ACardResolver(self._http, base).get_agent_card()
                endpoint = f"{base}/rpc"
                _validate_card(card, spec, endpoint)
                client = ClientFactory(
                    ClientConfig(
                        httpx_client=self._http,
                        streaming=False,
                        supported_protocol_bindings=["JSONRPC"],
                        accepted_output_modes=["application/json"],
                    )
                ).create(card)
                task = await client.get_task(GetTaskRequest(id=mapping.task_id))
                state = TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()
                if state not in terminal:
                    with suppress(TaskNotCancelableError):
                        await client.cancel_task(CancelTaskRequest(id=mapping.task_id))
                    task = await client.get_task(GetTaskRequest(id=mapping.task_id))
                    state = TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()
                mapping.state = state
                self.database.save_delegated_task(mapping)
                emit(run_id, "a2a.cancellation_result", mapping.model_dump(mode="json"))
                if state not in terminal:
                    raise RuntimeError(f"Previous {spec.name} task is still active; retry withheld")

    async def invoke(
        self,
        spec: AgentSpec,
        context: AgentContext,
        emit: EventSink,
        human_input: Callable[[DelegatedTask, AgentResult], Awaitable[dict]] | None = None,
    ) -> AgentResult:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self.settings.task_timeout_seconds, trust_env=False
            )
        base = f"{self.settings.a2a_base_url.rstrip('/')}/agents/{spec.name}"
        repair_suffix = f":repair:{context.repair_attempt}" if context.repair_attempt else ""
        if context.retry_attempt:
            repair_suffix += f":retry:{context.retry_attempt}"
        mapping = DelegatedTask(
            invocation_id=f"{context.run_id}:{context.stage}:{spec.name}{repair_suffix}",
            run_id=context.run_id,
            stage=context.stage,
            agent=spec.name,
            endpoint=f"{base}/rpc",
            context_id=f"{context.run_id}:{context.stage}{repair_suffix}",
            repair_attempt=context.repair_attempt,
        )
        request = SendMessageRequest(
            message=Message(
                message_id=mapping.invocation_id,
                context_id=mapping.context_id,
                role="ROLE_USER",
                parts=[
                    Part(
                        data=ParseDict(context.model_dump(mode="json"), Value()),
                        media_type="application/json",
                    )
                ],
            )
        )
        client: Client | None = None
        card: AgentCard | None = None

        def update(task_id: str, context_id: str, state: int) -> None:
            if not task_id or context_id != mapping.context_id:
                raise ValueError("A2A task/context identity mismatch")
            if mapping.task_id and task_id != mapping.task_id:
                raise ValueError("Agent changed the delegated task ID")
            mapping.task_id = task_id
            name = TaskState.Name(state).removeprefix("TASK_STATE_").lower()
            changed = mapping.state != name
            mapping.state = name
            self.database.save_delegated_task(mapping)
            if changed:
                emit(context.run_id, "a2a.task_status", mapping.model_dump(mode="json"))

        try:
            async with asyncio.timeout(self.settings.task_timeout_seconds) as timeout:
                for attempt in range(self.settings.a2a_max_retries + 1):
                    mapping.attempts = attempt + 1
                    self.database.save_delegated_task(mapping)
                    try:
                        if client is None:
                            card = await A2ACardResolver(self._http, base).get_agent_card()
                            _validate_card(card, spec, mapping.endpoint)
                            client = ClientFactory(
                                ClientConfig(
                                    httpx_client=self._http,
                                    streaming=True,
                                    supported_protocol_bindings=["JSONRPC"],
                                    accepted_output_modes=["application/json"],
                                )
                            ).create(card)
                            emit(
                                context.run_id,
                                "a2a.agent_discovered",
                                {
                                    "agent": spec.name,
                                    "endpoint": mapping.endpoint,
                                    "protocol": "1.0",
                                },
                            )
                        if mapping.task_id is None:
                            # The SDK's BaseClient returns an async generator with aclose().
                            source = cast(
                                AsyncGenerator[StreamResponse], client.send_message(request)
                            )
                            async with aclosing(source) as stream:
                                async for event in stream:
                                    if event.HasField("task"):
                                        update(
                                            event.task.id,
                                            event.task.context_id,
                                            event.task.status.state,
                                        )
                                    elif event.HasField("status_update"):
                                        item = event.status_update
                                        update(item.task_id, item.context_id, item.status.state)
                                    elif event.HasField("artifact_update"):
                                        item = event.artifact_update
                                        if (
                                            item.task_id != mapping.task_id
                                            or item.context_id != mapping.context_id
                                        ):
                                            raise ValueError(
                                                "Artifact task/context identity mismatch"
                                            )
                                        emit(
                                            context.run_id,
                                            "a2a.artifact_received",
                                            {
                                                "agent": spec.name,
                                                "task_id": item.task_id,
                                                "artifact_id": item.artifact.artifact_id,
                                            },
                                        )
                                    else:
                                        raise ValueError(
                                            "Expected A2A task events, not a direct message"
                                        )
                        if mapping.task_id is None:
                            raise ValueError("Agent stream ended without a task")
                        # GetTask recovers lost streams and returns aggregated artifacts.
                        while True:
                            task = await client.get_task(GetTaskRequest(id=mapping.task_id))
                            update(task.id, task.context_id, task.status.state)
                            if task.status.state == TaskState.TASK_STATE_INPUT_REQUIRED:
                                draft = self._decode_result(spec, task)
                                if human_input is None or draft.human_prompt is None:
                                    raise RemoteTaskError("Agent requested unsupported human input")
                                # Human thinking time is not model execution time.
                                timeout.reschedule(None)
                                response = await human_input(mapping, draft)
                                timeout.reschedule(
                                    asyncio.get_running_loop().time()
                                    + self.settings.task_timeout_seconds
                                )
                                resumed = context.model_copy(update={"human_response": response})
                                continuation = SendMessageRequest(
                                    message=Message(
                                        message_id=f"{mapping.invocation_id}:human:{response['action_id']}",
                                        task_id=task.id,
                                        context_id=task.context_id,
                                        role="ROLE_USER",
                                        parts=[
                                            Part(
                                                data=ParseDict(
                                                    resumed.model_dump(mode="json"), Value()
                                                ),
                                                media_type="application/json",
                                            )
                                        ],
                                    )
                                )
                                source = cast(
                                    AsyncGenerator[StreamResponse],
                                    client.send_message(continuation),
                                )
                                async with aclosing(source) as stream:
                                    async for event in stream:
                                        if event.HasField("task"):
                                            update(
                                                event.task.id,
                                                event.task.context_id,
                                                event.task.status.state,
                                            )
                                        elif event.HasField("status_update"):
                                            update(
                                                event.status_update.task_id,
                                                event.status_update.context_id,
                                                event.status_update.status.state,
                                            )
                                continue
                            if task.status.state in TERMINAL:
                                return self._result(spec, task)
                            await asyncio.sleep(self.settings.a2a_poll_interval_seconds)
                    except Exception as error:
                        if not _transient(error) or attempt >= self.settings.a2a_max_retries:
                            raise
                        emit(
                            context.run_id,
                            "a2a.retry",
                            {
                                "agent": spec.name,
                                "attempt": attempt + 2,
                                "invocation_id": mapping.invocation_id,
                                "reason": str(error),
                            },
                        )
                        await asyncio.sleep(self.settings.a2a_retry_delay_seconds * (2**attempt))
        except asyncio.CancelledError, TimeoutError:
            await asyncio.shield(self._cancel(client, card, request, mapping, emit))
            raise
        except Exception as error:
            if mapping.state in {"submitted", "working", "input_required"}:
                await asyncio.shield(self._cancel(client, card, request, mapping, emit))
            if mapping.state == "pending":
                mapping.state = "failed"
                self.database.save_delegated_task(mapping)
            emit(
                context.run_id,
                "a2a.invocation_failed",
                {
                    "agent": spec.name,
                    "reason": str(error),
                    "task_id": mapping.task_id,
                },
            )
            raise
        raise RuntimeError("A2A retry loop ended unexpectedly")

    @staticmethod
    def _result(spec: AgentSpec, task: Task) -> AgentResult:
        if task.status.state == TaskState.TASK_STATE_CANCELED:
            raise RemoteTaskCanceled(f"{spec.name} task {task.id} was canceled")
        if task.status.state != TaskState.TASK_STATE_COMPLETED:
            detail = " ".join(part.text for part in task.status.message.parts)
            raise RemoteTaskError(f"{spec.name}: {TaskState.Name(task.status.state)} {detail}")
        return A2AInvoker._decode_result(spec, task)

    @staticmethod
    def _decode_result(spec: AgentSpec, task: Task) -> AgentResult:
        if len(task.artifacts) != 1 or len(task.artifacts[0].parts) != 1:
            raise ValueError("Expected exactly one structured result artifact")
        part = task.artifacts[0].parts[0]
        if not part.HasField("data") or part.media_type != "application/json":
            raise ValueError("Expected application/json result artifact")
        result = AgentResult.model_validate(MessageToDict(part.data))
        if result.artifact_kind != spec.artifact_kind:
            raise ValueError(f"Unexpected artifact kind from {spec.name}")
        return result

    async def _cancel(
        self,
        client: Client | None,
        card: AgentCard | None,
        request: SendMessageRequest,
        mapping: DelegatedTask,
        emit: EventSink,
    ) -> None:
        if client is None or card is None:
            return
        try:
            async with asyncio.timeout(self.settings.a2a_cancel_timeout_seconds):
                if mapping.task_id is None:
                    # Lost the first response: the same messageId recovers the reserved task.
                    recovery = ClientFactory(
                        ClientConfig(
                            httpx_client=self._http,
                            streaming=False,
                            polling=True,
                        )
                    ).create(card)
                    async for event in recovery.send_message(request):
                        if event.HasField("task"):
                            mapping.task_id = event.task.id
                if mapping.task_id:
                    with suppress(TaskNotCancelableError):
                        await client.cancel_task(CancelTaskRequest(id=mapping.task_id))
                    task = await client.get_task(GetTaskRequest(id=mapping.task_id))
                    mapping.state = (
                        TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()
                    )
                    self.database.save_delegated_task(mapping)
                    emit(mapping.run_id, "a2a.cancellation_result", mapping.model_dump(mode="json"))
        except Exception as error:
            emit(
                mapping.run_id,
                "a2a.cancellation_failed",
                {"agent": mapping.agent, "reason": str(error)},
            )
