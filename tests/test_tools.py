"""OAuth/MCP contracts use signed test JWTs and real SDK HTTP routes, not static tokens."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextResourceContents

from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind, StageName
from aidlc.tools.analyzer import analyze_with_ruff
from aidlc.tools.auth import OIDCTokenVerifier
from aidlc.tools.models import AnalysisJob, SourceBundle
from aidlc.tools.server import create_tool_app


class SignedTestIdentity:
    """Test-only OIDC discovery/JWKS fixture; never used by application startup."""

    issuer = "http://127.0.0.1:8080/realms/aidlc"
    resource = "http://127.0.0.1:8002/mcp"

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        self.requests = []

    def token(self, scopes="analysis:run analysis:read", **claims):
        payload = {
            "iss": self.issuer,
            "aud": self.resource,
            "sub": "test-subject",
            "azp": "static-analysis-agent",
            "typ": "Bearer",
            "scope": scopes,
            "iat": int(time.time()) - 1,
            "exp": int(time.time()) + 300,
            **claims,
        }
        return jwt.encode(payload, self.key, algorithm="RS256", headers={"kid": self.kid})

    def http(self, request):
        self.requests.append(request)
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
                    "token_endpoint": f"{self.issuer}/protocol/openid-connect/token",
                },
            )
        if request.url.path.endswith("/certs"):
            key = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(
                200, json={"keys": [{**key, "kid": self.kid, "use": "sig", "alg": "RS256"}]}
            )
        if request.url.path.endswith("/token"):
            return httpx.Response(200, json={"token_type": "Bearer", "access_token": self.token()})
        return httpx.Response(404)


@asynccontextmanager
async def tool_transport(tmp_path, identity, *, analyzer=analyze_with_ruff):
    async def routing_headers(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            request.headers["MCP-Method"] = payload["method"]
            params = payload["params"]
            if payload["method"] in {"tools/call", "resources/read"}:
                request.headers["MCP-Name"] = params.get("name") or params["uri"]

    settings = Settings(data_dir=tmp_path, mcp_url=identity.resource, oidc_issuer=identity.issuer)
    async with httpx.AsyncClient(transport=httpx.MockTransport(identity.http)) as oidc:
        app = create_tool_app(settings, oidc_http_client=oidc, analyzer=analyzer)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=identity.resource.removesuffix("/mcp"),
                event_hooks={"request": [routing_headers]},
            ) as http:
                yield app, http


def source_artifact(app, content=None):
    database = app.state.service.database
    database.create_run("run_tools", "Explicit static-analysis fixture")
    artifact = app.state.service.artifacts.write(
        run_id="run_tools",
        stage=StageName.IMPLEMENTATION,
        kind=ArtifactKind.CODE_CHANGE,
        producing_agent="test-source-fixture",
        content=content or {"files": [{"path": "src/example.py", "content": "import os\n"}]},
        markdown="# Test source fixture",
    )
    database.add_artifact(artifact)
    return artifact


def rpc(method, params=None):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
    }


def analysis_call(artifact):
    return rpc(
        "tools/call",
        {
            "name": "analyze_code",
            "arguments": {
                "artifact_id": artifact.metadata.artifact_id,
                "content_sha256": artifact.metadata.content_sha256,
            },
        },
    )


def test_mcp_metadata_and_unauthenticated_challenge(tmp_path):
    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity) as (app, http):
            metadata = await http.get("/.well-known/oauth-protected-resource/mcp")
            assert metadata.status_code == 200
            assert metadata.json()["resource"] == identity.resource
            assert metadata.json()["authorization_servers"] == [identity.issuer]
            assert metadata.json()["scopes_supported"] == ["analysis:run", "analysis:read"]
            assert metadata.json()["bearer_methods_supported"] == ["header"]
            response = await http.post("/mcp", json=rpc("tools/list"))
            assert response.status_code == 401
            assert "resource_metadata=" in response.headers["www-authenticate"]
            assert "oauth-protected-resource/mcp" in response.headers["www-authenticate"]
            assert app.state.repository.audit_history()[-1]["outcome"] == "unauthenticated"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "invalid",
    [
        {"aud": "some-other-resource"},
        {"iss": "https://wrong-issuer.example"},
        {"exp": 1},
        {"nbf": 4_000_000_000},
        {"iat": 4_000_000_000},
        {"sub": ""},
        {"azp": ""},
        {"typ": "ID"},
        {"exp": "4000000000"},
        {"scope": []},
    ],
)
def test_invalid_access_claims_are_denied_before_analysis(tmp_path, invalid):
    async def forbidden(_job):
        pytest.fail("Unauthorized requests must never reach the analyzer")

    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity, analyzer=forbidden) as (_app, http):
            response = await http.post(
                "/mcp", json=rpc("tools/list"), headers=headers(identity.token(**invalid))
            )
            assert response.status_code == 401
            assert "invalid_token" in response.headers["www-authenticate"]

    asyncio.run(exercise())


def test_signature_algorithm_missing_claim_and_key_rotation():
    async def exercise():
        identity = SignedTestIdentity()
        async with httpx.AsyncClient(transport=httpx.MockTransport(identity.http)) as http:
            verifier = OIDCTokenVerifier(identity.issuer, identity.resource, http, cache_seconds=0)
            assert await verifier.verify_token(identity.token()) is not None
            assert (
                await verifier.verify_token(identity.token().rsplit(".", 1)[0] + ".ZmFrZQ") is None
            )
            payload = jwt.decode(identity.token(), options={"verify_signature": False})
            forged = jwt.encode(
                payload,
                "test-only-secret-at-least-32-bytes!",
                algorithm="HS256",
                headers={"kid": identity.kid},
            )
            assert await verifier.verify_token(forged) is None
            del payload["exp"]
            missing = jwt.encode(
                payload, identity.key, algorithm="RS256", headers={"kid": identity.kid}
            )
            assert await verifier.verify_token(missing) is None
            identity.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            identity.kid = "rotated-test-key"
            assert await verifier.verify_token(identity.token()) is not None

    asyncio.run(exercise())


def test_per_call_scopes_owner_bound_reads_and_durable_results(tmp_path):
    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity) as (app, http):
            artifact = source_artifact(app)
            call = analysis_call(artifact)
            run_token = identity.token("analysis:run")
            read_token = identity.token("analysis:read")
            denied = await http.post("/mcp", json=call, headers=headers(read_token))
            assert denied.status_code == 403
            assert 'scope="analysis:run"' in denied.headers["www-authenticate"]
            # Even a scope-bearing planning identity is denied by application policy.
            planning = await http.post(
                "/mcp", json=call, headers=headers(identity.token(azp="planning-agent"))
            )
            assert planning.status_code == 403
            response = await http.post("/mcp", json=call, headers=headers(run_token))
            assert response.status_code == 200, response.text
            report = response.json()["result"]["structuredContent"]
            assert report["status"] == "completed"
            assert report["findings"][0]["rule_id"] == "F401"
            assert report["findings"][0]["path"] == "src/example.py"
            assert report["execution"]["runtime"] == "local-analyzer"
            read = rpc("resources/read", {"uri": report["result_uri"]})
            no_read_scope = await http.post("/mcp", json=read, headers=headers(run_token))
            assert no_read_scope.status_code == 403
            allowed = await http.post("/mcp", json=read, headers=headers(read_token))
            assert json.loads(allowed.json()["result"]["contents"][0]["text"]) == report
            wrong_owner = await http.post(
                "/mcp",
                json=read,
                headers=headers(identity.token("analysis:read", sub="other-subject")),
            )
            assert "error" in wrong_owner.json()
            audit = json.dumps(app.state.repository.audit_history())
            assert run_token not in audit and read_token not in audit and "import os" not in audit
            events = app.state.service.database.events_after("run_tools")
            assert events[-1].event_type == "mcp.analysis"
        async with tool_transport(tmp_path, identity) as (_restarted, http):
            persisted = await http.post("/mcp", json=read, headers=headers(read_token))
            assert json.loads(persisted.json()["result"]["contents"][0]["text"]) == report

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "failure", ["hash", "tamper", "proposal", "path", "wrong_kind", "missing", "symlink"]
)
def test_invalid_artifacts_never_reach_analyzer(tmp_path, failure):
    async def forbidden(_job):
        pytest.fail("Invalid artifacts must not start an analyzer")

    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity, analyzer=forbidden) as (app, http):
            content = None
            if failure == "proposal":
                content = {"files": ["src/proposed.py"], "status": "proposed"}
            if failure == "path":
                content = {"files": [{"path": "../../escape.py", "content": "print('bad')"}]}
            artifact = source_artifact(app, content)
            call = analysis_call(artifact)
            if failure == "hash":
                call["params"]["arguments"]["content_sha256"] = "0" * 64
            if failure == "missing":
                call["params"]["arguments"]["artifact_id"] = "art_" + "0" * 32
            if failure in {"tamper", "symlink"}:
                from pathlib import Path

                path = Path(artifact.content_path) / "content.json"
                if failure == "tamper":
                    path.write_text('{"files": []}')
                else:
                    outside = tmp_path / "outside.json"
                    outside.write_text(path.read_text())
                    path.unlink()
                    path.symlink_to(outside)
            if failure == "wrong_kind":
                other = app.state.service.artifacts.write(
                    run_id="run_tools",
                    stage=StageName.INTAKE,
                    kind=ArtifactKind.PROJECT_BRIEF,
                    producing_agent="fixture",
                    content={"files": [{"path": "demo.py", "content": ""}]},
                    markdown="fixture",
                )
                app.state.service.database.add_artifact(other)
                call = analysis_call(other)
            response = await http.post("/mcp", json=call, headers=headers(identity.token()))
            assert response.json()["result"]["isError"] is True
            assert app.state.repository.audit_history()[-1]["outcome"] == "invalid_source_artifact"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "arguments",
    [
        {"language": "javascript"},
        {"ruleset": "shell"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 61},
        {"command": "env"},
        {"path": "/etc/passwd"},
    ],
)
def test_tool_arguments_are_allowlisted(tmp_path, arguments):
    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity) as (app, http):
            call = analysis_call(source_artifact(app))
            call["params"]["arguments"].update(arguments)
            response = await http.post("/mcp", json=call, headers=headers(identity.token()))
            assert response.status_code == 400

    asyncio.run(exercise())


def test_sdk_client_negotiates_and_reads_over_authenticated_http(tmp_path):
    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity) as (app, _http):
            artifact = source_artifact(app)
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app),
                headers={"Authorization": f"Bearer {identity.token()}"},
            ) as wire:
                async with Client(
                    streamable_http_client(identity.resource, http_client=wire)
                ) as client:
                    assert client.protocol_version == "2026-07-28"
                    assert [tool.name for tool in (await client.list_tools()).tools] == [
                        "analyze_code"
                    ]
                    result = await client.call_tool(
                        "analyze_code", analysis_call(artifact)["params"]["arguments"]
                    )
                    assert not result.is_error and result.structured_content is not None
                    read = await client.read_resource(result.structured_content["result_uri"])
                    assert isinstance(read.contents[0], TextResourceContents)
                    assert (
                        json.loads(read.contents[0].text)["analysis_id"]
                        == result.structured_content["analysis_id"]
                    )
            # In-process SDK connections bypass HTTP auth; the handler still refuses them.
            async with Client(app.state.mcp) as unauthenticated:
                result = await unauthenticated.call_tool(
                    "analyze_code", analysis_call(artifact)["params"]["arguments"]
                )
                assert result.is_error

    asyncio.run(exercise())


def test_analyzer_gets_only_sanitized_job_and_concurrency_is_bounded(tmp_path):
    active = peak = 0

    async def observe(job):
        nonlocal active, peak
        assert set(job.model_dump()) == {"source", "timeout_seconds", "max_output_bytes"}
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return await analyze_with_ruff(job)
        finally:
            active -= 1

    async def exercise():
        identity = SignedTestIdentity()
        async with tool_transport(tmp_path, identity, analyzer=observe) as (app, http):
            artifact = source_artifact(app)
            responses = await asyncio.gather(
                *[
                    http.post(
                        "/mcp", json=analysis_call(artifact), headers=headers(identity.token())
                    )
                    for _ in range(5)
                ]
            )
            assert all(
                response.json()["result"]["structuredContent"]["status"] == "completed"
                for response in responses
            )
            assert peak == 2 and active == 0

    asyncio.run(exercise())


def test_ruff_does_not_execute_source_or_inherit_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDLC_MCP_CLIENT_SECRET", "test-credential-must-not-be-inherited")
    sentinel = tmp_path / "must-not-exist"
    source = f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
    real_spawn = asyncio.create_subprocess_exec

    async def check_spawn(*args, **kwargs):
        assert kwargs["env"] == {"LANG": "C.UTF-8", "RAYON_NUM_THREADS": "1"}
        assert "--isolated" in args and "--no-cache" in args
        assert "shell" not in kwargs
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", check_spawn)
    job = AnalysisJob(
        source=SourceBundle.model_validate({"files": [{"path": "example.py", "content": source}]}),
        timeout_seconds=5,
        max_output_bytes=262144,
    )
    result = asyncio.run(analyze_with_ruff(job))
    assert result.status == "completed" and not sentinel.exists()


def test_ruff_output_limit(tmp_path):
    job = AnalysisJob(
        source=SourceBundle.model_validate(
            {"files": [{"path": "example.py", "content": "import os\n"}]}
        ),
        timeout_seconds=5,
        max_output_bytes=10,
    )
    result = asyncio.run(analyze_with_ruff(job))
    assert result.status == "output_limit" and result.execution.output_truncated


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_analyzer_timeout_and_cancellation_reap_child_and_workspace(tmp_path, monkeypatch, mode):
    import sys
    from pathlib import Path

    real_spawn = asyncio.create_subprocess_exec
    processes = []
    workspaces = []
    started = asyncio.Event()

    async def slow_trusted_test_process(*_args, **kwargs):
        workspaces.append(Path(kwargs["cwd"]))
        process = await real_spawn(sys.executable, "-c", "import time; time.sleep(30)", **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_trusted_test_process)
    job = AnalysisJob(
        source=SourceBundle.model_validate({"files": [{"path": "example.py", "content": ""}]}),
        timeout_seconds=1,
        max_output_bytes=262144,
    )

    async def exercise():
        task = asyncio.create_task(analyze_with_ruff(job))
        if mode == "cancel":
            async with asyncio.timeout(5):
                await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "timed_out" and result.execution.timed_out
        assert processes[0].returncode is not None
        assert not workspaces[0].exists()

    asyncio.run(exercise())


def test_oidc_discovery_mismatch_and_provider_outage_fail_closed():
    async def exercise():
        identity = SignedTestIdentity()

        def mismatched(_request):
            return httpx.Response(200, json={"issuer": "https://other-issuer.example"})

        def unavailable(_request):
            return httpx.Response(503)

        for provider in (mismatched, unavailable):
            async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
                verifier = OIDCTokenVerifier(identity.issuer, identity.resource, http)
                assert await verifier.verify_token(identity.token()) is None

    asyncio.run(exercise())
