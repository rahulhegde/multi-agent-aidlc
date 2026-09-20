from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.routes import build_resource_metadata_url, create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AnyHttpUrl, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from aidlc import __version__
from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind
from aidlc.sandbox.analyzer import configured_analyzer
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from aidlc.tools.analyzer import Analyzer
from aidlc.tools.auth import OIDCTokenVerifier, permitted, principal, validate_service_url
from aidlc.tools.models import AnalysisJob, AnalysisReport, AnalysisRequest, SourceBundle
from aidlc.tools.repository import ToolRepository

REPORT_ID = re.compile(r"^an_[a-f0-9]{32}$")
RESOURCE_URI = re.compile(r"^analysis://reports/(an_[a-f0-9]{32})$")
SCOPES = ["analysis:run", "analysis:read"]


class ToolScopeMiddleware:
    """HTTP scope challenges supplement the handler's independent authorization check.

    This runs inside the SDK authentication/context middleware, so scopes always
    come from the current request's verified JWT, never the session's first token.
    """

    def __init__(self, app: ASGIApp, repository: ToolRepository, resource: str) -> None:
        self.app, self.repository = app, repository
        self.metadata_url = str(build_resource_metadata_url(AnyHttpUrl(resource)))

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if "access_token" in request.query_params:
            await JSONResponse({"error": "Tokens must use the Authorization header"}, 400)(
                scope, receive, send
            )
            return
        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            self.repository.audit({"decision": "denied", "outcome": "unauthenticated"})
            await self.app(scope, receive, send)  # SDK supplies the 401 discovery challenge.
            return
        if request.method != "POST":
            await self.app(scope, receive, send)
            return
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65_536:
                await JSONResponse({"error": "Request body exceeds 64 KiB"}, 413)(
                    scope, receive, send
                )
                return
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError, UnicodeDecodeError:
            await JSONResponse({"error": "Expected a JSON-RPC object"}, 400)(scope, receive, send)
            return
        method, params = payload.get("method"), payload.get("params") or {}
        required = None
        if method == "tools/call":
            required = "analysis:run"
            registered = isinstance(params, dict) and params.get("name") == "analyze_code"
        elif method == "resources/read":
            required = "analysis:read"
            registered = (
                isinstance(params, dict)
                and isinstance(params.get("uri"), str)
                and RESOURCE_URI.fullmatch(params["uri"]) is not None
            )
        else:
            registered = True
        if required and (not registered or not permitted(user.access_token, required)):
            self.repository.audit(
                {
                    "decision": "denied",
                    "outcome": "insufficient_scope" if registered else "unknown_capability",
                    "client_id": user.access_token.client_id,
                    "subject": user.access_token.subject,
                    "capability": method,
                    "required_scope": required,
                }
            )
            challenge = (
                f'Bearer error="insufficient_scope", scope="{required}", '
                f'resource_metadata="{self.metadata_url}"'
            )
            await JSONResponse(
                {"error": "insufficient_scope", "required_scope": required},
                403,
                headers={"WWW-Authenticate": challenge},
            )(scope, receive, send)
            return
        if method == "tools/call":
            try:
                AnalysisRequest.model_validate(params.get("arguments", {}))
            except ValidationError:
                self.repository.audit(
                    {
                        "decision": "allowed",
                        "outcome": "invalid_arguments",
                        "client_id": user.access_token.client_id,
                        "subject": user.access_token.subject,
                    }
                )
                await JSONResponse({"error": "Invalid analysis arguments"}, 400)(
                    scope, receive, send
                )
                return

        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class AnalysisService:
    def __init__(self, settings: Settings, repository: ToolRepository, analyzer: Analyzer) -> None:
        self.settings, self.repository, self.analyzer = settings, repository, analyzer
        self.database = WorkflowDatabase(settings.database_path)
        self.artifacts = ArtifactStore(settings.artifact_dir)
        self._slots = asyncio.Semaphore(settings.analysis_max_parallel_jobs)

    def _audit(self, token: AccessToken, report: AnalysisReport, outcome: str) -> None:
        tasks = self.database.list_delegated_tasks(report.run_id)
        mapping = next((task for task in tasks if task.agent == "static-analysis-agent"), None)
        event = {
            "decision": "allowed",
            "outcome": outcome,
            "client_id": token.client_id,
            "subject": token.subject,
            "tool": "analyze_code",
            "run_id": report.run_id,
            "artifact_id": report.artifact_id,
            "content_sha256": report.content_sha256,
            "analysis_id": report.analysis_id,
            "runtime": report.execution.runtime,
            "image": report.execution.image,
            "isolation": report.execution.isolation,
            "exit_code": report.execution.exit_code,
            "oom_killed": report.execution.oom_killed,
            "cleanup_succeeded": report.execution.cleanup_succeeded,
            "duration_ms": report.execution.duration_ms,
            "a2a_task_id": mapping.task_id if mapping else None,
            "a2a_context_id": mapping.context_id if mapping else None,
        }
        self.repository.audit(event)
        self.database.append_event(report.run_id, "mcp.analysis", event)

    async def analyze(self, request: AnalysisRequest, token: AccessToken | None) -> AnalysisReport:
        if not permitted(token, "analysis:run"):
            raise ToolError("analysis:run is required for this client")
        assert token is not None
        try:
            record = self.database.get_artifact(request.artifact_id)
            if record is None or record.metadata.kind not in {
                ArtifactKind.CODE_CHANGE,
                ArtifactKind.INTEGRATED_SOURCE,
            }:
                raise ValueError
            if record.metadata.content_sha256 != request.content_sha256:
                raise ValueError
            from pathlib import Path

            content_file = (Path(record.content_path) / "content.json").resolve()
            if (
                not content_file.is_relative_to(self.settings.artifact_dir.resolve())
                or content_file.stat().st_size > 1_048_576
            ):
                raise ValueError
            source = SourceBundle.model_validate(self.artifacts.read_content(record))
        except ValueError, OSError, ValidationError:
            self.repository.audit(
                {
                    "decision": "allowed",
                    "outcome": "invalid_source_artifact",
                    "client_id": token.client_id,
                    "subject": token.subject,
                    "artifact_id": request.artifact_id,
                    "content_sha256": request.content_sha256,
                }
            )
            raise ToolError(
                "Expected an intact code_change source bundle with the supplied hash"
            ) from None
        job = AnalysisJob(
            source=source,
            timeout_seconds=min(request.timeout_seconds, self.settings.analysis_timeout_seconds),
            max_output_bytes=self.settings.analysis_max_output_bytes,
        )
        async with self._slots:
            # Recheck after queuing; an expired token must not start a job.
            if not permitted(token, "analysis:run"):
                raise ToolError("Access token expired while waiting for analysis")
            try:
                result = await self.analyzer(job)
            except asyncio.CancelledError:
                self.repository.audit(
                    {
                        "decision": "allowed",
                        "outcome": "canceled",
                        "client_id": token.client_id,
                        "subject": token.subject,
                        "artifact_id": request.artifact_id,
                    }
                )
                raise
            except Exception:
                self.repository.audit(
                    {
                        "decision": "allowed",
                        "outcome": "analyzer_failed",
                        "client_id": token.client_id,
                        "subject": token.subject,
                        "artifact_id": request.artifact_id,
                    }
                )
                raise ToolError("The configured analyzer failed") from None
        analysis_id = f"an_{uuid4().hex}"
        report = AnalysisReport(
            **result.model_dump(),
            analysis_id=analysis_id,
            artifact_id=request.artifact_id,
            content_sha256=request.content_sha256,
            run_id=record.metadata.workflow_run_id,
            result_uri=f"analysis://reports/{analysis_id}",
            summary={
                "errors": sum(finding.severity == "error" for finding in result.findings),
                "warnings": sum(finding.severity == "warning" for finding in result.findings),
            },
        )
        self.repository.save(principal(token), report)
        self._audit(token, report, report.status)
        return report

    def read(self, analysis_id: str, token: AccessToken | None) -> str:
        if not permitted(token, "analysis:read"):
            raise ToolError("analysis:read is required")
        assert token is not None
        report = (
            self.repository.read(principal(token), analysis_id)
            if REPORT_ID.fullmatch(analysis_id)
            else None
        )
        self.repository.audit(
            {
                "decision": "allowed" if report else "denied",
                "outcome": "read" if report else "not_found",
                "client_id": token.client_id,
                "subject": token.subject,
                "analysis_id": analysis_id if REPORT_ID.fullmatch(analysis_id) else None,
            }
        )
        if report is None:
            raise ToolError("Analysis result not found for this caller")
        return report.model_dump_json()


def create_tool_app(
    settings: Settings | None = None,
    *,
    oidc_http_client: httpx.AsyncClient | None = None,
    analyzer: Analyzer | None = None,
) -> Starlette:
    configured = settings or Settings()
    resource = validate_service_url(configured.mcp_url)
    if urlsplit(resource).path != "/mcp":
        raise ValueError("AIDLC_MCP_URL must use the /mcp endpoint")
    client = oidc_http_client or httpx.AsyncClient(
        timeout=5, follow_redirects=False, trust_env=False
    )
    verifier = OIDCTokenVerifier(configured.oidc_issuer, resource, client)
    repository = ToolRepository(configured.data_dir / "tools.sqlite3")
    repository.initialize()
    service = AnalysisService(configured, repository, analyzer or configured_analyzer(configured))
    service.database.initialize()
    mcp = MCPServer(
        "aidlc-tools",
        version=__version__,
        token_verifier=verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(configured.oidc_issuer),
            resource_server_url=AnyHttpUrl(resource),
            required_scopes=[],
            validate_token_resource=True,
        ),
    )

    @mcp.tool()
    async def analyze_code(
        artifact_id: str,
        content_sha256: str,
        language: str = "python",
        ruleset: str = "default",
        timeout_seconds: int = 60,
    ) -> AnalysisReport:
        """Analyze an immutable Python source bundle with the fixed default Ruff ruleset."""
        try:
            request = AnalysisRequest.model_validate(
                {
                    "artifact_id": artifact_id,
                    "content_sha256": content_sha256,
                    "language": language,
                    "ruleset": ruleset,
                    "timeout_seconds": timeout_seconds,
                }
            )
        except ValidationError:
            raise ToolError("Invalid artifact reference, language, ruleset, or timeout") from None
        return await service.analyze(request, get_access_token())

    @mcp.resource("analysis://reports/{analysis_id}", mime_type="application/json")
    def analysis_result(analysis_id: str) -> str:
        """Read a stored result owned by the calling identity; requires analysis:read."""
        return service.read(analysis_id, get_access_token())

    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True, max_request_body_size=65_536
    )
    # The transport requires authentication. Capability scopes are enforced per
    # operation, so neither read-only nor run-only callers need both scopes.
    metadata_path = urlsplit(str(build_resource_metadata_url(AnyHttpUrl(resource)))).path
    app.router.routes = [
        route for route in app.routes if getattr(route, "path", None) != metadata_path
    ]
    app.router.routes.extend(
        create_protected_resource_routes(
            resource_url=AnyHttpUrl(resource),
            authorization_servers=[AnyHttpUrl(configured.oidc_issuer)],
            scopes_supported=SCOPES,
            resource_name="aidlc-tools",
        )
    )
    app.user_middleware.append(
        Middleware(ToolScopeMiddleware, repository=repository, resource=resource)
    )
    sdk_lifespan = app.router.lifespan_context
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(application: Starlette):
        try:
            from contextlib import AsyncExitStack

            async with AsyncExitStack() as services:
                if configured.analysis_backend == "sandbox" and analyzer is None:
                    from aidlc.sandbox.recovery import recovery_service

                    await services.enter_async_context(recovery_service(configured))
                state = await services.enter_async_context(sdk_lifespan(application))
                yield state or {}
        finally:
            if oidc_http_client is None:
                await client.aclose()

    app.router.lifespan_context = lifespan
    app.state.repository, app.state.service, app.state.mcp = repository, service, mcp
    return app


def run() -> None:
    import uvicorn

    configured = Settings()
    endpoint = urlsplit(configured.mcp_url)
    if endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The development MCP service must bind to loopback")
    uvicorn.run(
        create_tool_app(configured),
        host=endpoint.hostname,
        port=endpoint.port or 8002,
        access_log=False,
    )
