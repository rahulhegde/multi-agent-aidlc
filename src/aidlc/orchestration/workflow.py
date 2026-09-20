from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any
from uuid import uuid4

from aidlc.a2a.client import A2AInvoker, RemoteTaskCanceled
from aidlc.agents.catalog import AGENTS_BY_STAGE, AgentSpec
from aidlc.agents.source import merge_sources
from aidlc.config import Settings
from aidlc.domain.models import (
    AgentContext,
    AgentResult,
    ArtifactKind,
    ArtifactRecord,
    DelegatedTask,
    HumanAction,
    HumanInteraction,
    RunStatus,
    StageName,
    WorkflowRun,
)
from aidlc.evaluation.gates import evaluate_gates, gates_markdown
from aidlc.evaluation.models import EvaluationReport, QualityGateReport, repair_decision
from aidlc.human import surface_messages
from aidlc.orchestration.context import (
    build_agent_context,
    canonical_context_bytes,
    context_cache_key,
    context_input_artifact_ids,
)
from aidlc.platform.preflight import inspect_host
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase


class WorkflowOrchestrator:
    """The hub owns ordering; specialist agents only receive stage context."""

    def __init__(
        self,
        settings: Settings,
        database: WorkflowDatabase,
        artifacts: ArtifactStore,
        invoker: A2AInvoker | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.artifacts = artifacts
        self.invoker = invoker or A2AInvoker(settings, database)
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def create_run(self, idea: str) -> WorkflowRun:
        run_id = f"run_{uuid4().hex}"
        self.artifacts.initialize_project(run_id, idea)
        run = self.database.create_run(run_id, idea)
        self._event(run_id, "run.created", {"idea": idea})
        task = asyncio.create_task(self._execute(run_id), name=f"workflow:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda _task: self._tasks.pop(run_id, None) if self._tasks.get(run_id) is task else None
        )
        return run

    def retry(self, run_id: str) -> WorkflowRun:
        run = self.database.get_run(run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.status not in {RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.CANCELED}:
            raise ValueError("Only failed, blocked, or canceled runs can be retried")
        if (task := self._tasks.get(run_id)) is not None and not task.done():
            raise ValueError("This run still has an active workflow")
        # Validate the persisted snapshots before scheduling model work.
        self._artifact_context(run_id)
        attempt = 1 + sum(
            event.event_type == "run.retry_requested"
            for event in self.database.events_after(run_id)
        )
        self.database.update_run(run_id, RunStatus.PENDING, run.current_stage)
        self._event(run_id, "run.retry_requested", {"retry_attempt": attempt})
        task = asyncio.create_task(self._execute(run_id, attempt), name=f"workflow:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(
            lambda _task: self._tasks.pop(run_id, None) if self._tasks.get(run_id) is task else None
        )
        result = self.database.get_run(run_id)
        assert result is not None
        return result

    def _saved_outputs(
        self,
        run: WorkflowRun,
        stage: StageName,
        repair_attempt: int = 0,
    ) -> dict[str, ArtifactRecord]:
        completed = (
            next(item for item in run.stages if item.name == stage).status == RunStatus.COMPLETED
        )
        saved_ids = {
            event.payload.get("artifact_id")
            for event in self.database.events_after(run.run_id)
            if event.event_type == "artifact.created"
            and event.payload.get("agent_completed")
            and event.payload.get("repair_attempt", 0) == repair_attempt
        }
        expected = {spec.name: spec.artifact_kind for spec in AGENTS_BY_STAGE[stage]}
        result = {}
        for artifact in self.database.list_artifacts(run.run_id):
            metadata = artifact.metadata
            if (
                metadata.stage_id == stage
                and expected.get(metadata.producing_agent) == metadata.kind
                and (completed or metadata.artifact_id in saved_ids)
            ):
                self.artifacts.read_content(artifact)
                result[metadata.producing_agent] = artifact
        if stage == StageName.INTEGRATION and not any(
            item.metadata.kind == ArtifactKind.INTEGRATED_SOURCE
            for item in self.database.list_artifacts(run.run_id)
        ):
            return {}
        return result

    def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done() or task.cancelling():
            return False
        run = self.database.get_run(run_id)
        if run is not None and run.status == RunStatus.PENDING:
            self.database.update_run(run_id, RunStatus.CANCELED, run.current_stage)
        self._event(run_id, "run.cancellation_requested", {})
        task.cancel()
        return True

    async def wait(self, run_id: str) -> None:
        task = self._tasks.get(run_id)
        if task is not None:
            await task

    async def shutdown(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.invoker.close()

    async def _execute(self, run_id: str, retry_attempt: int = 0) -> None:
        run = self.database.get_run(run_id)
        if run is None:
            return
        current_stage = StageName.PREFLIGHT
        try:
            if retry_attempt:
                await self.invoker.reconcile_run(run_id, self._event)
            self.database.update_run(run_id, RunStatus.RUNNING, current_stage)
            await self._preflight(run)
            refreshed = self.database.get_run(run_id)
            if refreshed is None or refreshed.status == RunStatus.BLOCKED:
                return

            parent_ids = [
                artifact.metadata.artifact_id for artifact in self.database.list_artifacts(run_id)
            ]
            attempt = 0
            feedback = None
            if retry_attempt:
                repairs = [
                    item
                    for item in self._artifact_context(run_id)
                    if item["metadata"]["kind"] == ArtifactKind.REPAIR_REQUEST
                ]
                if repairs:
                    feedback = repairs[-1]["content"]
                    attempt = feedback["attempt"]
            for current_stage in (
                StageName.INTAKE,
                StageName.REQUIREMENTS,
                StageName.DISCOVERY,
                StageName.PLANNING,
                StageName.IMPLEMENTATION,
                StageName.INTEGRATION,
            ):
                stage_attempt = (
                    attempt
                    if current_stage
                    in {
                        StageName.IMPLEMENTATION,
                        StageName.INTEGRATION,
                    }
                    else 0
                )
                saved = (
                    self._saved_outputs(run, current_stage, stage_attempt) if retry_attempt else {}
                )
                if (
                    retry_attempt
                    and next(item for item in run.stages if item.name == current_stage).status
                    == RunStatus.COMPLETED
                    and len(saved) == len(AGENTS_BY_STAGE[current_stage])
                ):
                    parent_ids = [item.metadata.artifact_id for item in saved.values()]
                    self._event(run_id, "stage.reused", {"stage": current_stage})
                    continue
                parent_ids = await self._run_stage(
                    run,
                    current_stage,
                    parent_ids,
                    stage_attempt,
                    feedback if stage_attempt else None,
                    retry_attempt=retry_attempt,
                    reuse_outputs=bool(retry_attempt),
                )

            while True:
                current_stage = StageName.EVALUATION
                parent_ids = await self._run_stage(
                    run,
                    current_stage,
                    parent_ids,
                    attempt,
                    feedback,
                    retry_attempt=retry_attempt,
                    reuse_outputs=bool(retry_attempt),
                )
                context = self._artifact_context(run_id)
                gate_artifact = next(
                    item
                    for item in reversed(context)
                    if item["metadata"]["kind"] == ArtifactKind.QUALITY_GATE_REPORT
                )
                evaluation_artifact = next(
                    item
                    for item in reversed(context)
                    if item["metadata"]["kind"] == ArtifactKind.EVALUATION_REPORT
                )
                gates = QualityGateReport.model_validate(gate_artifact["content"])
                evaluation = EvaluationReport.model_validate(evaluation_artifact["content"])
                human_decision = next(
                    (
                        item.response["decision"]
                        for item in reversed(self.database.list_interactions(run_id))
                        if item.stage == StageName.EVALUATION
                        and item.prompt.kind == "evaluation_decision"
                        and item.response
                        and item.artifact_sha256
                        == evaluation_artifact["metadata"]["content_sha256"]
                    ),
                    None,
                )
                decision = repair_decision(
                    gates,
                    evaluation,
                    attempt,
                    self.settings.max_evaluation_repair_attempts,
                    self.settings.evaluation_score_threshold,
                    human_decision,
                )
                self._event(
                    run_id,
                    "evaluation.decision",
                    {
                        "decision": decision,
                        "repair_attempt": attempt,
                        "quality_gate_artifact_id": gate_artifact["metadata"]["artifact_id"],
                        "evaluation_artifact_id": evaluation_artifact["metadata"]["artifact_id"],
                        "overall_score": evaluation.overall_score(),
                        "human_decision": human_decision,
                    },
                )
                if decision in {"passed", "deferred"}:
                    break
                if decision == "exhausted":
                    self._event(run_id, "repair.exhausted", {"repair_attempts": attempt})
                    raise RuntimeError("Evaluation repair limit exhausted; release withheld")
                attempt += 1
                feedback = {
                    "attempt": attempt,
                    "quality_gates": gates.model_dump(mode="json"),
                    "evaluation": evaluation.model_dump(mode="json"),
                }
                repair = self.artifacts.write(
                    run_id=run_id,
                    stage=StageName.EVALUATION,
                    kind=ArtifactKind.REPAIR_REQUEST,
                    producing_agent="orchestrator",
                    content=feedback,
                    markdown=f"# Repair request\n\nAttempt {attempt}; preserve prior evidence.",
                    parent_artifact_ids=parent_ids + [gate_artifact["metadata"]["artifact_id"]],
                )
                self.database.add_artifact(repair)
                self._event(
                    run_id,
                    "artifact.created",
                    {
                        "artifact_id": repair.metadata.artifact_id,
                        "kind": ArtifactKind.REPAIR_REQUEST,
                        "stage": StageName.EVALUATION,
                    },
                )
                self._event(
                    run_id,
                    "repair.started",
                    {
                        "attempt": attempt,
                        "artifact_id": repair.metadata.artifact_id,
                    },
                )
                parent_ids += [repair.metadata.artifact_id]
                for current_stage in (StageName.IMPLEMENTATION, StageName.INTEGRATION):
                    parent_ids = await self._run_stage(
                        run,
                        current_stage,
                        parent_ids,
                        attempt,
                        feedback,
                        retry_attempt=retry_attempt,
                    )

            current_stage = StageName.RELEASE
            await self._run_stage(run, current_stage, parent_ids, retry_attempt=retry_attempt)

            self.database.update_run(run_id, RunStatus.COMPLETED, StageName.RELEASE)
            self._event(run_id, "run.completed", {})
        except asyncio.CancelledError:
            self.database.update_stage(run_id, current_stage, RunStatus.CANCELED)
            self.database.update_run(run_id, RunStatus.CANCELED, current_stage)
            self._event(run_id, "run.canceled", {"stage": current_stage})
        except Exception as error:
            logging.getLogger(__name__).exception("Workflow %s failed", run_id)
            errors = _leaf_errors(error)
            message = "; ".join(f"{type(item).__name__}: {item}" for item in errors)
            status = (
                RunStatus.CANCELED
                if all(isinstance(item, RemoteTaskCanceled) for item in errors)
                else RunStatus.FAILED
            )
            self.database.update_stage(run_id, current_stage, status, message)
            self.database.update_run(run_id, status, current_stage, message)
            self._event(run_id, f"run.{status}", {"stage": current_stage, "error": message})

    async def _preflight(self, run: WorkflowRun) -> None:
        stage = StageName.PREFLIGHT
        self._stage_started(run.run_id, stage)
        report = await asyncio.to_thread(inspect_host, self.settings)
        content = report.model_dump(mode="json")
        markdown = self._preflight_markdown(content)
        artifact = self.artifacts.write(
            run_id=run.run_id,
            stage=stage,
            kind=ArtifactKind.HOST_CAPABILITY_REPORT,
            producing_agent="host-preflight",
            content=content,
            markdown=markdown,
        )
        self.database.add_artifact(artifact)
        self._event(
            run.run_id,
            "artifact.created",
            {"artifact_id": artifact.metadata.artifact_id, "kind": artifact.metadata.kind},
        )
        if not report.ready and self.settings.enforce_host_preflight:
            reason = "Required host capabilities are unavailable. See the capability report."
            self.database.update_stage(run.run_id, stage, RunStatus.BLOCKED, reason)
            self.database.update_run(run.run_id, RunStatus.BLOCKED, stage, reason)
            self._event(run.run_id, "run.blocked", {"stage": stage, "reason": reason})
            return
        if not report.ready:
            self._event(
                run.run_id,
                "preflight.override",
                {"reason": "AIDLC_ENFORCE_HOST_PREFLIGHT is false"},
            )
        self._stage_completed(run.run_id, stage)

    async def _run_stage(
        self,
        run: WorkflowRun,
        stage: StageName,
        parent_ids: list[str],
        repair_attempt: int = 0,
        repair_feedback: dict[str, Any] | None = None,
        *,
        retry_attempt: int = 0,
        reuse_outputs: bool = False,
    ) -> list[str]:
        saved = self._saved_outputs(run, stage, repair_attempt) if reuse_outputs else {}
        self.database.update_run(run.run_id, RunStatus.RUNNING, stage)
        self._stage_started(run.run_id, stage)
        if stage == StageName.INTEGRATION and not (
            reuse_outputs
            and any(
                item.metadata.kind == ArtifactKind.INTEGRATED_SOURCE
                for item in self.database.list_artifacts(run.run_id)
            )
        ):
            parent_ids = self._integrate(run, parent_ids)
        definitions = AGENTS_BY_STAGE[stage]
        hub_context = AgentContext(
            run_id=run.run_id,
            idea=run.idea,
            stage=stage,
            artifacts=self._artifact_context(run.run_id),
            repair_attempt=repair_attempt,
            repair_feedback=repair_feedback,
            retry_attempt=retry_attempt,
        )
        if stage == StageName.EVALUATION and not (
            saved
            and any(
                item["metadata"]["kind"] == ArtifactKind.QUALITY_GATE_REPORT
                for item in hub_context.artifacts
            )
        ):
            report = evaluate_gates(hub_context, self.settings.sandbox_backend)
            artifact = self.artifacts.write(
                run_id=run.run_id,
                stage=stage,
                kind=ArtifactKind.QUALITY_GATE_REPORT,
                producing_agent="deterministic-quality-gates",
                content=report.model_dump(mode="json"),
                markdown=gates_markdown(report),
                parent_artifact_ids=[
                    item["metadata"]["artifact_id"] for item in hub_context.artifacts
                ],
                prompt_version=report.profile,
            )
            self.database.add_artifact(artifact)
            parent_ids = parent_ids + [artifact.metadata.artifact_id]
            hub_context.artifacts = self._artifact_context(run.run_id)
            self._event(
                run.run_id,
                "artifact.created",
                {
                    "artifact_id": artifact.metadata.artifact_id,
                    "kind": artifact.metadata.kind,
                    "stage": stage,
                },
            )
            self._event(
                run.run_id,
                "quality_gates.completed",
                {
                    "artifact_id": artifact.metadata.artifact_id,
                    "verdict": report.verdict,
                    "repair_attempt": repair_attempt,
                },
            )
        semaphore = asyncio.Semaphore(self.settings.max_parallel_agents)

        async def invoke(definition: AgentSpec):
            if definition.name in saved:
                self._event(run.run_id, "agent.reused", {"agent": definition.name, "stage": stage})
                return saved[definition.name].metadata.artifact_id
            async with semaphore:
                context = build_agent_context(
                    spec=definition,
                    run_id=run.run_id,
                    idea=run.idea,
                    artifacts=hub_context.artifacts,
                    repair_attempt=repair_attempt,
                    repair_feedback=repair_feedback,
                    retry_attempt=retry_attempt,
                )
                serialized_context = canonical_context_bytes(context)
                baseline_context = hub_context.model_copy(
                    update={
                        "repair_artifact_id": context.repair_artifact_id,
                        "repair_artifact_sha256": context.repair_artifact_sha256,
                    }
                )
                baseline_context_bytes = len(canonical_context_bytes(baseline_context))
                input_artifact_ids = context_input_artifact_ids(context)
                cache_key = context_cache_key(
                    definition,
                    context,
                    model_id=self.settings.model,
                    runtime_profile=self._cache_runtime_profile(),
                )
                context_payload = {
                    "agent": definition.name,
                    "stage": stage,
                    "repair_attempt": repair_attempt,
                    "retry_attempt": retry_attempt,
                    "input_artifact_ids": input_artifact_ids,
                    "context_contract_version": definition.context_contract_version,
                    "context_sha256": hashlib.sha256(serialized_context).hexdigest(),
                    "context_bytes": len(serialized_context),
                    "baseline_context_bytes": baseline_context_bytes,
                    "context_bytes_saved": baseline_context_bytes - len(serialized_context),
                    "selected_artifact_count": len(context.artifacts),
                    "baseline_artifact_count": len(hub_context.artifacts),
                    "cache_key": cache_key,
                }
                self._event(run.run_id, "agent.context_selected", context_payload)
                cached = (
                    self._cached_output(run.run_id, definition, cache_key, input_artifact_ids)
                    if definition.cacheable
                    else None
                )
                if cached is not None:
                    self._event(
                        run.run_id,
                        "agent.cache_hit",
                        {
                            **context_payload,
                            "artifact_id": cached.metadata.artifact_id,
                        },
                    )
                    return cached.metadata.artifact_id
                self._event(
                    run.run_id,
                    "agent.started",
                    context_payload,
                )
                result = await self.invoker.invoke(
                    definition, context, self._event, self._human_input
                )
                self._record_execution(
                    run.run_id,
                    definition.name,
                    result,
                    phase=definition.stage,
                    cache_key=cache_key,
                    context_contract_version=definition.context_contract_version,
                    context_bytes=len(serialized_context),
                    baseline_context_bytes=baseline_context_bytes,
                    repair_attempt=repair_attempt,
                    retry_attempt=retry_attempt,
                )
                execution = {
                    **result.execution.model_dump(mode="json"),
                    "cache_key": cache_key,
                    "context_contract_version": definition.context_contract_version,
                    "context_sha256": context_payload["context_sha256"],
                    "context_bytes": len(serialized_context),
                    "baseline_context_bytes": baseline_context_bytes,
                }
                artifact = self.artifacts.write(
                    run_id=run.run_id,
                    stage=stage,
                    kind=result.artifact_kind,
                    producing_agent=definition.name,
                    content=result.content,
                    markdown=result.markdown,
                    parent_artifact_ids=input_artifact_ids,
                    model_id=result.execution.model_id,
                    prompt_version=result.execution.prompt_version,
                    execution=execution,
                )
                self.database.add_artifact(artifact)
                self._event(
                    run.run_id,
                    "artifact.created",
                    {
                        "artifact_id": artifact.metadata.artifact_id,
                        "kind": artifact.metadata.kind,
                        "stage": stage,
                        "agent_completed": True,
                        "repair_attempt": repair_attempt,
                    },
                )
                self._event(
                    run.run_id,
                    "agent.completed",
                    {"agent": definition.name, "stage": stage},
                )
                return artifact.metadata.artifact_id

        # Creating specialist tasks is the fan-out; leaving the task group is the fan-in.
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(invoke(item)) for item in definitions]
        created_ids = [task.result() for task in tasks]
        self._stage_completed(run.run_id, stage)
        return created_ids

    def _integrate(self, run: WorkflowRun, parent_ids: list[str]) -> list[str]:
        context = AgentContext(
            run_id=run.run_id,
            idea=run.idea,
            stage=StageName.INTEGRATION,
            artifacts=self._artifact_context(run.run_id),
        )
        source = merge_sources(context)
        artifact = self.artifacts.write(
            run_id=run.run_id,
            stage=StageName.INTEGRATION,
            kind=ArtifactKind.INTEGRATED_SOURCE,
            producing_agent="trusted-integration-service",
            content=source.model_dump(mode="json"),
            markdown="# Integrated project source",
            parent_artifact_ids=parent_ids,
        )
        self.database.add_artifact(artifact)
        self._event(
            run.run_id,
            "artifact.created",
            {
                "artifact_id": artifact.metadata.artifact_id,
                "kind": artifact.metadata.kind,
                "stage": StageName.INTEGRATION,
            },
        )
        return parent_ids + [artifact.metadata.artifact_id]

    async def _human_input(self, mapping: DelegatedTask, draft: AgentResult) -> dict:
        self._record_execution(mapping.run_id, mapping.agent, draft, phase=mapping.stage)
        if draft.human_prompt is None or mapping.task_id is None:
            raise ValueError("Missing human request or remote task identity")
        fingerprint = hashlib.sha256(draft.model_dump_json().encode()).hexdigest()[:20]
        surface_id = f"{mapping.invocation_id}:{fingerprint}"
        interaction = next(
            (
                item
                for item in self.database.list_interactions(mapping.run_id)
                if item.surface_id == surface_id
            ),
            None,
        )
        if interaction is None:
            clarifications = [
                item
                for item in self.database.list_interactions(mapping.run_id)
                if item.prompt.kind == "clarification"
            ]
            if (
                draft.human_prompt.kind == "clarification"
                and len(clarifications) >= self.settings.max_clarification_rounds
            ):
                raise RuntimeError("Clarification round limit exceeded")
            artifact = self.artifacts.write(
                run_id=mapping.run_id,
                stage=mapping.stage,
                kind=draft.artifact_kind,
                producing_agent=mapping.agent,
                content=draft.content,
                markdown=draft.markdown,
                model_id=draft.execution.model_id,
                prompt_version=draft.execution.prompt_version,
                execution=draft.execution.model_dump(mode="json"),
                parent_artifact_ids=[
                    item.metadata.artifact_id
                    for item in self.database.list_artifacts(mapping.run_id)
                ],
            )
            self.database.add_artifact(artifact)
            interaction = HumanInteraction(
                surface_id=surface_id,
                run_id=mapping.run_id,
                stage=mapping.stage,
                agent=mapping.agent,
                task_id=mapping.task_id,
                context_id=mapping.context_id,
                prompt=draft.human_prompt,
                artifact_id=artifact.metadata.artifact_id,
                artifact_sha256=artifact.metadata.content_sha256,
                messages=surface_messages(
                    surface_id, draft.human_prompt.question, draft.human_prompt.kind
                ),
            )
            request = self.artifacts.write(
                run_id=mapping.run_id,
                stage=mapping.stage,
                kind=ArtifactKind.HUMAN_REQUEST,
                producing_agent=mapping.agent,
                content=interaction.model_dump(mode="json"),
                markdown=f"# Human request\n\n{draft.human_prompt.question}",
                parent_artifact_ids=[artifact.metadata.artifact_id],
            )
            self.database.add_artifact(request)
            self.database.save_interaction(interaction)
        self.database.update_stage(mapping.run_id, mapping.stage, RunStatus.INPUT_REQUIRED)
        self.database.update_run(mapping.run_id, RunStatus.INPUT_REQUIRED, mapping.stage)
        self._event(mapping.run_id, "human.requested", interaction.model_dump(mode="json"))
        while interaction.response is None:
            await asyncio.sleep(0.1)
            interaction = next(
                item
                for item in self.database.list_interactions(mapping.run_id)
                if item.surface_id == surface_id
            )
        self.database.update_stage(mapping.run_id, mapping.stage, RunStatus.RUNNING)
        self.database.update_run(mapping.run_id, RunStatus.RUNNING, mapping.stage)
        return {
            **interaction.response,
            "previous_answers": [
                item.response["answer"]
                for item in self.database.list_interactions(mapping.run_id)
                if item.prompt.kind == "clarification" and item.response
            ],
        }

    def _cached_output(
        self,
        run_id: str,
        definition: AgentSpec,
        cache_key: str,
        input_artifact_ids: list[str],
    ) -> ArtifactRecord | None:
        for artifact in reversed(self.database.list_artifacts(run_id)):
            metadata = artifact.metadata
            if (
                metadata.producing_agent == definition.name
                and metadata.kind == definition.artifact_kind
                and metadata.execution.get("cache_key") == cache_key
                and metadata.execution.get("context_contract_version")
                == definition.context_contract_version
                and metadata.parent_artifact_ids == input_artifact_ids
            ):
                self.artifacts.read_content(artifact)
                return artifact
        return None

    def _cache_runtime_profile(self) -> dict[str, object]:
        """Non-secret configuration that can materially change an agent result."""

        return {
            "reasoning_effort": self.settings.reasoning_effort,
            "max_model_output_tokens": self.settings.max_model_output_tokens,
            "max_implementation_output_tokens": self.settings.max_implementation_output_tokens,
            "sandbox_backend": self.settings.sandbox_backend,
            "sandbox_image": self.settings.sandbox_image,
            "sandbox_execution_enabled": self.settings.sandbox_execution_enabled,
            "analysis_backend": self.settings.analysis_backend,
            "mcp_enabled": self.settings.mcp_enabled,
            "evaluation_score_threshold": self.settings.evaluation_score_threshold,
        }

    def _record_execution(
        self,
        run_id: str,
        agent: str,
        result: AgentResult,
        *,
        phase: StageName,
        cache_key: str | None = None,
        context_contract_version: str | None = None,
        context_bytes: int | None = None,
        baseline_context_bytes: int | None = None,
        repair_attempt: int = 0,
        retry_attempt: int = 0,
    ) -> None:
        execution = result.execution
        run_events = self.database.events_after(run_id)
        previous = [
            event
            for event in run_events
            if event.event_type == "agent.execution"
        ]
        if execution.execution_id and any(
            event.payload.get("execution_id") == execution.execution_id for event in previous
        ):
            return
        if cache_key is None:
            selected = next(
                (
                    event.payload
                    for event in reversed(self.database.events_after(run_id))
                    if event.event_type == "agent.context_selected"
                    and event.payload.get("agent") == agent
                ),
                {},
            )
            cache_key = selected.get("cache_key")
            context_contract_version = selected.get("context_contract_version")
            context_bytes = selected.get("context_bytes")
            baseline_context_bytes = selected.get("baseline_context_bytes")
            repair_attempt = int(selected.get("repair_attempt") or 0)
            retry_attempt = int(selected.get("retry_attempt") or 0)
        self._event(
            run_id,
            "agent.execution",
            {
                "agent": agent,
                "phase": phase,
                "cache_key": cache_key,
                "context_contract_version": context_contract_version,
                "context_bytes": context_bytes,
                "baseline_context_bytes": baseline_context_bytes,
                "repair_attempt": repair_attempt,
                "retry_attempt": retry_attempt,
                **execution.model_dump(mode="json"),
            },
        )
        # A workflow retry is a fresh evaluation attempt. Earlier evaluation
        # results remain in the audit trail, but must not consume the new
        # attempt's phase allowance. Other phases retain their run-wide
        # accounting because their retries can fan out across several agents.
        latest_retry_event_id = 0
        if phase == StageName.EVALUATION:
            latest_retry_event_id = max(
                (
                    event.event_id
                    for event in run_events
                    if event.event_type == "run.retry_requested"
                ),
                default=0,
            )
        phase_agents = {spec.name for spec in AGENTS_BY_STAGE[phase]}
        phase_total = sum(
            (event.payload.get("input_tokens") or 0) + (event.payload.get("output_tokens") or 0)
            for event in previous
            if event.event_id > latest_retry_event_id
            # Agent matching preserves accounting for events recorded before the
            # explicit phase field was introduced.
            if event.payload.get("phase") == phase or event.payload.get("agent") in phase_agents
        )
        phase_total += (execution.input_tokens or 0) + (execution.output_tokens or 0)
        if phase_total > self.settings.token_budget_per_phase_per_run:
            raise RuntimeError(
                "Reported phase token budget per run exceeded; further delegation stopped "
                f"(phase={phase}, reported_tokens={phase_total}, "
                f"limit={self.settings.token_budget_per_phase_per_run}, agent={agent}; "
                "configure AIDLC_TOKEN_BUDGET_PER_PHASE_PER_RUN)"
            )

    def respond(self, run_id: str, action: HumanAction) -> HumanInteraction:
        interaction = next(
            (
                item
                for item in self.database.list_interactions(run_id)
                if item.surface_id == action.surface_id
            ),
            None,
        )
        if interaction is not None and interaction.response is None:
            latest = next(
                (
                    item
                    for item in reversed(self.database.list_delegated_tasks(run_id))
                    if item.agent == interaction.agent and item.stage == interaction.stage
                ),
                None,
            )
            if latest is not None and latest.task_id != interaction.task_id:
                raise ValueError("This approval request was superseded by a retry")

        def publish_response(interaction: HumanInteraction):
            return self.artifacts.write(
                run_id=run_id,
                stage=interaction.stage,
                kind=ArtifactKind.HUMAN_RESPONSE,
                producing_agent="local-learner",
                content=interaction.response or {},
                markdown=f"# Human response\n\n{action.decision}\n\n{action.answer}",
                parent_artifact_ids=[interaction.artifact_id],
            )

        interaction, created = self.database.accept_action(run_id, action, publish_response)
        if created:
            self._event(
                run_id,
                "human.responded",
                {"surface_id": interaction.surface_id, "decision": action.decision},
            )
        return interaction

    def _artifact_context(self, run_id: str) -> list[dict[str, Any]]:
        return [
            {
                "metadata": record.metadata.model_dump(mode="json"),
                "content": self.artifacts.read_content(record),
            }
            for record in self.database.list_artifacts(run_id)
        ]

    def _stage_started(self, run_id: str, stage: StageName) -> None:
        self.database.update_stage(run_id, stage, RunStatus.RUNNING)
        self._event(run_id, "stage.started", {"stage": stage})

    def _stage_completed(self, run_id: str, stage: StageName) -> None:
        self.database.update_stage(run_id, stage, RunStatus.COMPLETED)
        self._event(run_id, "stage.completed", {"stage": stage})

    def _event(self, run_id: str, event_type: str, payload: dict[str, object]) -> None:
        self.database.append_event(run_id, event_type, payload)

    @staticmethod
    def _preflight_markdown(content: dict[str, Any]) -> str:
        lines = ["# Host capability report", "", f"Ready: **{content['ready']}**", ""]
        for check in content["checks"]:
            lines.append(f"- `{check['status']}` **{check['name']}** — {check['detail']}")
        return "\n".join(lines)


def _leaf_errors(error: Exception) -> list[Exception]:
    if isinstance(error, ExceptionGroup):
        return [leaf for item in error.exceptions for leaf in _leaf_errors(item)]
    return [error]
