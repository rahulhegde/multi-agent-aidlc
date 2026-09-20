from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from aidlc import __version__
from aidlc.a2a.client import A2AInvoker
from aidlc.config import Settings
from aidlc.domain.models import TERMINAL_RUN_STATUSES, ArtifactKind, CreateRunRequest, HumanAction
from aidlc.orchestration.finops import finops_report
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.platform.preflight import inspect_host
from aidlc.sandbox.cli import latest_qualification
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase


def create_app(
    settings: Settings | None = None, *, agent_http_client: httpx.AsyncClient | None = None
) -> FastAPI:
    configured = settings or Settings()
    database = WorkflowDatabase(configured.database_path)
    artifacts = ArtifactStore(configured.artifact_dir)
    orchestrator = WorkflowOrchestrator(
        configured, database, artifacts, A2AInvoker(configured, database, agent_http_client)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        database.recover_interrupted_runs()
        yield
        await orchestrator.shutdown()

    app = FastAPI(title="Multi-Agent AIDLC", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.settings = configured
    app.state.database = database
    app.state.artifacts = artifacts
    app.state.orchestrator = orchestrator

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/config")
    async def harness_config() -> dict[str, object]:
        # Expose non-secret defaults so the learning UI can explain agent behavior.
        return {
            "model": configured.model,
            "reasoning_effort": configured.reasoning_effort,
            "max_agent_steps": configured.max_agent_steps,
            "max_parallel_agents": configured.max_parallel_agents,
            "task_timeout_seconds": configured.task_timeout_seconds,
            "max_repair_attempts": configured.max_repair_attempts,
            "max_evaluation_repair_attempts": configured.max_evaluation_repair_attempts,
            "evaluation_score_threshold": configured.evaluation_score_threshold,
            "quality_gate_profile": "python-react-v1",
            "max_clarification_rounds": configured.max_clarification_rounds,
            "token_budget_per_phase_per_run": configured.token_budget_per_phase_per_run,
            "sandbox_backend": configured.sandbox_backend,
            "sandbox_execution_enabled": configured.sandbox_execution_enabled,
            "sandbox_network_enabled": configured.sandbox_network_enabled,
            "sandbox_recovery": "Local job leases; startup, every 30s, before each job",
            "sandbox_image": configured.sandbox_image or "sandbox-image.json (build-image)",
            "general_purpose_subagent_enabled": configured.general_purpose_subagent_enabled,
            "require_plan_approval": configured.require_plan_approval,
            "a2ui_protocol_version": configured.a2ui_protocol_version,
            "a2ui_catalog": {
                "id": "urn:aidlc:human-controls:v1",
                "components": ["Text", "Column", "TextField", "Button"],
                "actions": ["submit", "approve", "reject"],
            },
            "host_preflight_enforced": configured.enforce_host_preflight,
            "execution_mode": configured.agent_mode,
            "human_gates_enabled": configured.human_gates_enabled,
            "require_requirements_approval": configured.require_requirements_approval,
            "require_release_approval": configured.require_release_approval,
            "native_delegation": "disabled: strict A2A-only policy",
            "agent_communication": "A2A 1.0 JSONRPC over HTTP",
            "a2a_base_url": configured.a2a_base_url,
            "a2a_max_retries": configured.a2a_max_retries,
            "mcp": {
                "enabled": configured.mcp_enabled,
                "server": "aidlc-tools",
                "url": configured.mcp_url,
                "protocol_version": "2026-07-28",
                "oidc_issuer": configured.oidc_issuer,
                "scopes": ["analysis:run", "analysis:read"],
                "analysis_runtime": configured.sandbox_backend
                if configured.analysis_backend == "sandbox"
                else "local-analyzer",
            },
            "active_limits": [
                "max_parallel_agents",
                "task_timeout_seconds",
                "a2a_max_retries",
                "max_agent_steps (deep)",
                "max_repair_attempts (deep)",
                "max_evaluation_repair_attempts (workflow)",
                "token_budget_per_phase_per_run (reported usage, post-call)",
            ],
            "reasoning_effort_applied": False,
            "cost_rates_configured": configured.input_cost_per_million is not None
            and configured.output_cost_per_million is not None,
        }

    @app.get("/api/platform")
    async def platform_report():
        return await asyncio.to_thread(inspect_host, configured)

    @app.get("/api/platform/sandboxes")
    async def sandbox_qualification():
        return await asyncio.to_thread(latest_qualification, configured)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(request: CreateRunRequest):
        return orchestrator.create_run(request.idea.strip())

    @app.get("/api/runs")
    async def list_runs():
        return database.list_runs()

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        run = database.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run

    @app.post("/api/runs/{run_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
    async def cancel_run(run_id: str) -> dict[str, bool]:
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return {"accepted": orchestrator.cancel(run_id)}

    @app.post("/api/runs/{run_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    async def retry_run(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            return orchestrator.retry(run_id)
        except (ValueError, OSError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/runs/{run_id}/artifacts")
    async def list_artifacts(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return database.list_artifacts(run_id)

    @app.get("/api/runs/{run_id}/history")
    async def event_history(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return database.events_after(run_id)

    @app.get("/api/runs/{run_id}/tasks")
    async def delegated_tasks(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return database.list_delegated_tasks(run_id)

    @app.get("/api/runs/{run_id}/finops")
    async def run_finops(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return finops_report(database.events_after(run_id))

    @app.get("/api/runs/{run_id}/interactions")
    async def human_interactions(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return database.list_interactions(run_id)

    @app.get("/api/runs/{run_id}/evaluation")
    async def evaluation_view(run_id: str):
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        records = database.list_artifacts(run_id)

        def latest(kind: ArtifactKind):
            record = next((item for item in reversed(records) if item.metadata.kind == kind), None)
            return (
                {"artifact_id": record.metadata.artifact_id, **artifacts.read_content(record)}
                if record
                else None
            )

        gates = latest(ArtifactKind.QUALITY_GATE_REPORT)
        evaluation = latest(ArtifactKind.EVALUATION_REPORT)
        events = database.events_after(run_id)
        decision = next(
            (
                event.payload
                for event in reversed(events)
                if event.event_type == "evaluation.decision"
                and gates
                and evaluation
                and event.payload.get("quality_gate_artifact_id") == gates["artifact_id"]
                and event.payload.get("evaluation_artifact_id") == evaluation["artifact_id"]
            ),
            None,
        )
        return {
            "quality_gates": gates,
            "agent_evaluation": evaluation,
            "decision": decision,
            "repair_attempts": sum(event.event_type == "repair.started" for event in events),
        }

    @app.post("/api/runs/{run_id}/actions")
    async def human_action(
        run_id: str, action: HumanAction, authorization: str | None = Header(default=None)
    ):
        # A loopback-only learning identity, not production OAuth/OIDC.
        if not secrets.compare_digest(
            authorization or "", f"Bearer {configured.human_action_token}"
        ):
            raise HTTPException(status_code=401, detail="Human action authentication required")
        try:
            return orchestrator.respond(run_id, action)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/artifacts/{artifact_id}")
    async def get_artifact(artifact_id: str):
        record = database.get_artifact(artifact_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return {"metadata": record.metadata, "content": artifacts.read_content(record)}

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        if database.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            cursor = int(last_event_id or 0)
        except ValueError:
            cursor = 0

        async def event_source() -> AsyncIterator[str]:
            nonlocal cursor
            while not await request.is_disconnected():
                events = database.events_after(run_id, cursor)
                for event in events:
                    cursor = event.event_id
                    payload = event.model_dump(mode="json")
                    yield (
                        f"id: {event.event_id}\n"
                        f"event: {event.event_type}\n"
                        f"data: {json.dumps(payload)}\n\n"
                    )
                run = database.get_run(run_id)
                if run is None or (run.status in TERMINAL_RUN_STATUSES and not events):
                    break
                if not events:
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("aidlc.main:app", host="127.0.0.1", port=8000, reload=False)
