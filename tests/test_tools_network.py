"""Real MCP HTTP sockets, A2A-to-MCP delegation, and opt-in real Keycloak qualification."""

import asyncio
import json
import os
import socket
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
import uvicorn
from sse_starlette.sse import AppStatus

from aidlc.config import Settings
from aidlc.domain.models import AgentResult, ArtifactKind, RunStatus, StageName
from aidlc.orchestration.workflow import WorkflowOrchestrator
from aidlc.tools.client import ToolsClient
from aidlc.tools.models import AnalysisRequest
from aidlc.tools.server import create_tool_app
from tests.reference_agents import AGENTS_BY_STAGE
from tests.test_a2a_network import loopback_fleet, ready_report
from tests.test_tools import SignedTestIdentity, analysis_call, source_artifact


@asynccontextmanager
async def loopback_tools(settings, *, oidc_http_client=None, port=0):
    previous_exit = AppStatus.should_exit
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        configured = replace(settings, mcp_url=f"http://127.0.0.1:{listener.getsockname()[1]}/mcp")
        app = create_tool_app(configured, oidc_http_client=oidc_http_client)
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="critical",
                lifespan="on",
                access_log=False,
                timeout_graceful_shutdown=3,
            )
        )
        worker = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if worker.done():
                        await worker
                        raise RuntimeError("MCP service did not start")
                    await asyncio.sleep(0.01)
            yield configured, app
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(worker, 5)
            finally:
                AppStatus.should_exit = previous_exit


def test_loopback_a2a_specialist_calls_mcp_and_publishes_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr("aidlc.orchestration.workflow.inspect_host", ready_report)
    backend = AGENTS_BY_STAGE[StageName.IMPLEMENTATION][0]

    async def source_fixture(_context):
        return AgentResult(
            artifact_kind=ArtifactKind.CODE_CHANGE,
            content={"files": [{"path": "src/example.py", "content": "import os\n"}]},
            markdown="# Explicit test source fixture",
        )

    monkeypatch.setitem(
        AGENTS_BY_STAGE,
        StageName.IMPLEMENTATION,
        (replace(backend, handler=source_fixture), *AGENTS_BY_STAGE[StageName.IMPLEMENTATION][1:]),
    )
    identity = SignedTestIdentity()

    async def signed_test_token(_self, scope):
        return identity.token(scope)

    monkeypatch.setattr(ToolsClient, "_token", signed_test_token)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(identity.http)) as oidc:
            async with loopback_tools(
                Settings(data_dir=tmp_path, mcp_enabled=True, analysis_backend="local-analyzer"),
                oidc_http_client=oidc,
            ) as (settings, app):
                identity.resource = settings.mcp_url
                async with loopback_fleet(settings) as fleet_settings:
                    database = app.state.service.database
                    hub = WorkflowOrchestrator(
                        fleet_settings, database, app.state.service.artifacts
                    )
                    try:
                        run = hub.create_run("Analyze an explicit test source bundle")
                        await asyncio.wait_for(hub.wait(run.run_id), 15)
                        result = database.get_run(run.run_id)
                        assert result is not None and result.status == RunStatus.COMPLETED, result
                        reports = [
                            artifact
                            for artifact in database.list_artifacts(run.run_id)
                            if artifact.metadata.kind == ArtifactKind.STATIC_ANALYSIS_REPORT
                        ]
                        content = app.state.service.artifacts.read_content(reports[0])
                        assert content["status"] == "completed"
                        assert content["reports"][0]["findings"][0]["rule_id"] == "F401"
                        events = [
                            event
                            for event in database.events_after(run.run_id)
                            if event.event_type == "mcp.analysis"
                        ]
                        assert len(events) == 1
                        mapping = next(
                            task
                            for task in database.list_delegated_tasks(run.run_id)
                            if task.agent == "static-analysis-agent"
                        )
                        assert events[0].payload["a2a_task_id"] == mapping.task_id
                        assert events[0].payload["a2a_context_id"] == mapping.context_id
                        assert "Bearer" not in json.dumps(events[0].payload)
                    finally:
                        await hub.shutdown()

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_KEYCLOAK") != "true",
    reason="Start dev/compose.yaml and set AIDLC_TEST_KEYCLOAK=true",
)
def test_real_keycloak_client_credentials_and_scope_denial(tmp_path):
    async def exercise():
        settings = Settings(data_dir=tmp_path, analysis_backend="local-analyzer")
        async with httpx.AsyncClient(timeout=2, trust_env=False) as probe:
            async with asyncio.timeout(55):
                while True:
                    try:
                        response = await probe.get(
                            f"{settings.oidc_issuer}/.well-known/openid-configuration"
                        )
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.25)
        # The committed Keycloak audience mapper targets this exact development resource.
        async with loopback_tools(settings, port=8002) as (configured, app):
            artifact = source_artifact(app)
            client = ToolsClient(configured)
            report = await client.analyze(
                AnalysisRequest(
                    artifact_id=artifact.metadata.artifact_id,
                    content_sha256=artifact.metadata.content_sha256,
                )
            )
            assert report.status == "completed" and report.findings[0].rule_id == "F401"
            assert (await client.read(report.analysis_id))["analysis_id"] == report.analysis_id
            # Acquire a real planning token without any analysis scope.
            planning = ToolsClient(
                replace(
                    configured,
                    mcp_client_id="planning-agent",
                    mcp_client_secret=os.environ["AIDLC_PLANNING_CLIENT_SECRET"],
                )
            )
            token = await planning._token("")
            async with httpx.AsyncClient(timeout=5, trust_env=False) as wire:
                response = await wire.post(
                    configured.mcp_url,
                    json=analysis_call(artifact),
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": "2026-07-28",
                        "MCP-Method": "tools/call",
                        "MCP-Name": "analyze_code",
                    },
                )
                assert response.status_code == 403
                assert 'scope="analysis:run"' in response.headers["www-authenticate"]
            audit = app.state.repository.audit_history()
            assert any(event.get("outcome") == "insufficient_scope" for event in audit)
            assert token not in json.dumps(audit)

    asyncio.run(exercise())
