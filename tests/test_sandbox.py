"""Execution policy, failure cleanup, pinned inputs, and defensive evidence parsing."""

import argparse
import asyncio
import json
import os
import sys

import pytest

from aidlc.config import Settings
from aidlc.sandbox.analyzer import configured_analyzer
from aidlc.sandbox.cli import FIXTURE, latest_qualification, qualify, run_artifact
from aidlc.sandbox.models import PROBE_CHECKS, SandboxJob, SandboxPolicy
from aidlc.sandbox.runner import DockerSandbox, resolve_image
from aidlc.tools.models import AnalysisJob, SourceBundle

IMAGE = "sha256:" + "a" * 64


class ScriptedDocker:
    """Control-plane fixture; real isolation is tested separately against Docker."""

    def __init__(self, failure=None):
        self.failure = failure
        self.commands = []
        self.containers = set()
        self.started = asyncio.Event()
        self.payloads = []
        self.definition = None

    async def capture(self, command, **kwargs):
        self.commands.append(command)
        assert kwargs["limit"] <= 262_144
        operation = command[4]
        if operation == "info":
            if self.failure == "daemon":
                return 1, b"", b"denied"
            return (
                0,
                json.dumps(
                    {"OSType": "linux", "Architecture": "x86_64", "Runtimes": {"runc": {}}}
                ).encode(),
                b"",
            )
        if operation == "image":
            return (
                0,
                json.dumps(
                    [{"Architecture": "amd64", "Os": "linux", "Id": IMAGE, "Config": {}}]
                ).encode(),
                b"",
            )
        if operation == "create":
            name = command[command.index("--name") + 1]
            self.containers.add(name)

            def argument(flag):
                return next(arg.split("=", 1)[1] for arg in command if arg.startswith(flag + "="))

            self.definition = {
                "Config": {"User": "65532:65532"},
                "Mounts": [],
                "HostConfig": {
                    "Runtime": argument("--runtime"),
                    "NetworkMode": "none",
                    "ReadonlyRootfs": self.failure != "policy",
                    "Privileged": False,
                    "Memory": int(argument("--memory")),
                    "MemorySwap": int(argument("--memory-swap")),
                    "NanoCpus": int(argument("--cpus")) * 10**9,
                    "PidsLimit": int(argument("--pids-limit")),
                    "CgroupnsMode": "private",
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges=true"],
                    "Tmpfs": {"/workspace": "bounded", "/tmp": "bounded"},
                },
                "State": {"Running": False, "ExitCode": 0, "OOMKilled": self.failure == "oom"},
            }
            if self.failure == "create":
                return 1, b"", b"failed"
            return 0, b"b" * 64, b""
        if operation == "inspect":
            return 0, json.dumps([self.definition]).encode(), b""
        if operation == "start":
            self.payloads.append(kwargs["payload"])
            self.started.set()
            if self.failure in {"timeout", "cancel"}:
                await asyncio.sleep(30)
            if self.failure == "output":
                from aidlc.tools.analyzer import OutputLimitError

                raise ExceptionGroup("bounded stream", [OutputLimitError()])
            if self.failure == "invalid":
                return 0, b"not-json", b""
            if self.failure == "path":
                return (
                    0,
                    json.dumps(
                        [
                            {
                                "filename": "/workspace/../escape.py",
                                "code": "F401",
                                "location": {"row": 1, "column": 1},
                                "message": "unused",
                            }
                        ]
                    ).encode(),
                    b"",
                )
            if command[-1] in self.containers:
                mode = next(c[-1] for c in reversed(self.commands) if c[4] == "create")
                return (
                    0,
                    json.dumps(dict.fromkeys(PROBE_CHECKS, True)).encode()
                    if mode == "probe"
                    else b"[]",
                    b"",
                )
        if operation == "rm":
            if self.failure == "cleanup":
                return 1, b"", b"denied"
            self.containers.discard(command[-1])
            return 0, b"removed", b""
        if operation == "ps":
            return 0, b'"remaining"' if self.containers else b"", b""
        raise AssertionError(command)


def backend(tmp_path, monkeypatch, failure=None):
    settings = Settings(data_dir=tmp_path, sandbox_image=IMAGE)
    scripted = ScriptedDocker(failure)
    monkeypatch.setattr("aidlc.sandbox.runner.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        DockerSandbox,
        "_capture",
        lambda self, command, **kwargs: scripted.capture(command, **kwargs),
    )
    return settings, scripted, DockerSandbox(settings)


def test_policy_strips_credentials_and_forbids_host_access(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDLC_MCP_CLIENT_SECRET", "fixture-secret-must-not-reach-workload")
    settings, wire, runner = backend(tmp_path, monkeypatch)
    result = asyncio.run(runner.execute(SandboxJob(source=FIXTURE, mode="build")))
    assert result.status == "completed" and result.execution.cleanup_succeeded
    assert result.execution.image == IMAGE and not wire.containers
    create = next(command for command in wire.commands if command[4] == "create")
    assert "--network=none" in create and "--read-only" in create
    assert "--cap-drop=ALL" in create and "--pull=never" in create
    assert not any(
        arg.startswith(("--env", "--mount", "--volume", "--device", "--privileged"))
        for arg in create
    )
    assert all(b"fixture-secret" not in payload for payload in wire.payloads)
    assert json.loads(wire.payloads[0]) == FIXTURE.model_dump(mode="json")
    # Each job receives a new container and destroys it.
    second = asyncio.run(runner.execute(SandboxJob(source=FIXTURE, mode="test")))
    assert second.execution.container_id and not wire.containers
    names = [
        command[command.index("--name") + 1] for command in wire.commands if command[4] == "create"
    ]
    assert len(set(names)) == 2


@pytest.mark.parametrize(
    "failure,status",
    [
        ("create", "failed"),
        ("policy", "failed"),
        ("oom", "failed"),
        ("timeout", "timed_out"),
        ("output", "output_limit"),
        ("cleanup", "failed"),
    ],
)
def test_failures_destroy_container_and_never_claim_completion(
    tmp_path, monkeypatch, failure, status
):
    _, wire, runner = backend(tmp_path, monkeypatch, failure)
    result = asyncio.run(
        runner.execute(
            SandboxJob(source=FIXTURE, mode="test", policy=SandboxPolicy(timeout_seconds=1))
        )
    )
    assert result.status == status
    assert any(command[4] == "rm" for command in wire.commands)
    assert result.execution.cleanup_succeeded == (failure != "cleanup")
    if failure == "policy":
        assert not wire.started.is_set()
    if failure != "cleanup":
        assert not wire.containers


def test_cancellation_waits_for_container_teardown(tmp_path, monkeypatch):
    _, wire, runner = backend(tmp_path, monkeypatch, "cancel")

    async def exercise():
        task = asyncio.create_task(runner.execute(SandboxJob(source=FIXTURE, mode="test")))
        await wire.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not wire.containers

    asyncio.run(exercise())


@pytest.mark.parametrize("runtime", ["runsc", "kata"])
def test_unavailable_runtime_never_falls_back(tmp_path, monkeypatch, runtime):
    _, wire, runner = backend(tmp_path, monkeypatch)
    result = asyncio.run(runner.execute(SandboxJob(source=FIXTURE, mode="probe"), runtime))
    assert result.status == "unavailable" and result.execution.runtime == runtime
    assert not any(command[4] == "create" for command in wire.commands)


def test_daemon_outage_and_unpinned_image_fail_closed(tmp_path, monkeypatch):
    settings, wire, runner = backend(tmp_path, monkeypatch, "daemon")
    assert (
        asyncio.run(runner.execute(SandboxJob(source=FIXTURE, mode="build"))).status
        == "unavailable"
    )
    assert not any(command[4] == "create" for command in wire.commands)
    with pytest.raises(ValueError, match="full SHA-256"):
        resolve_image(Settings(sandbox_image="python:latest"))
    with pytest.raises(ValueError, match="build-image"):
        resolve_image(Settings(data_dir=tmp_path))


@pytest.mark.parametrize("failure", ["invalid", "path"])
def test_malformed_or_escaping_analysis_output_is_failed(tmp_path, monkeypatch, failure):
    settings, wire, _ = backend(tmp_path, monkeypatch, failure)
    result = asyncio.run(
        configured_analyzer(settings)(
            AnalysisJob(source=FIXTURE, timeout_seconds=5, max_output_bytes=262_144)
        )
    )
    assert result.status == "failed" and not result.findings and not wire.containers


def test_qualification_is_immutable_and_persists_unavailable_evidence(tmp_path, monkeypatch):
    settings, _, _ = backend(tmp_path, monkeypatch)
    result = asyncio.run(qualify(settings))
    assert [item["status"] for item in result["runtimes"]] == ["pass", "unavailable", "unavailable"]
    assert not result["baseline_qualified"] and not result["microvm_qualified"]
    persisted = latest_qualification(settings)
    assert persisted and persisted["artifact_id"] == result["artifact_id"]
    assert persisted["runtimes"] == result["runtimes"]


def test_cli_rejects_source_hash_mismatch_before_execution(tmp_path, monkeypatch):
    from aidlc.domain.models import ArtifactKind, StageName
    from aidlc.storage.artifacts import ArtifactStore
    from aidlc.storage.database import WorkflowDatabase

    settings, wire, _ = backend(tmp_path, monkeypatch)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    database.create_run("run_fixture", "Sandbox artifact fixture")
    artifact = ArtifactStore(settings.artifact_dir).write(
        run_id="run_fixture",
        stage=StageName.IMPLEMENTATION,
        kind=ArtifactKind.CODE_CHANGE,
        producing_agent="fixture",
        content=FIXTURE.model_dump(mode="json"),
        markdown="Fixture",
    )
    database.add_artifact(artifact)
    arguments = argparse.Namespace(
        artifact_id=artifact.metadata.artifact_id,
        content_sha256="0" * 64,
        command="build",
        runtime="runc",
        timeout_seconds=5,
    )
    with pytest.raises(ValueError, match="hash"):
        asyncio.run(run_artifact(settings, arguments))
    assert not wire.commands
    arguments.content_sha256 = artifact.metadata.content_sha256
    result = asyncio.run(run_artifact(settings, arguments))
    evidence = database.get_artifact(result["artifact_id"])
    assert evidence and evidence.metadata.parent_artifact_ids == [artifact.metadata.artifact_id]
    assert result["status"] == "completed"


@pytest.mark.parametrize("mode", ["output", "timeout", "cancel"])
def test_docker_cli_capture_reaps_real_child(tmp_path, monkeypatch, mode):
    real_spawn = asyncio.create_subprocess_exec
    children = []
    started = asyncio.Event()

    async def spawn(*args, **kwargs):
        assert set(kwargs["env"]) == {"PATH", "LANG"}
        child = await real_spawn(*args, **kwargs)
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    runner = DockerSandbox(Settings(data_dir=tmp_path))
    program = "import time; time.sleep(30)" if mode != "output" else "print('x' * 100000)"

    async def exercise():
        task = asyncio.create_task(
            runner._capture([sys.executable, "-c", program], deadline_seconds=1, limit=100)
        )
        if mode == "cancel":
            await started.wait()
            task.cancel()
        with pytest.raises(
            {"timeout": TimeoutError, "output": ExceptionGroup, "cancel": asyncio.CancelledError}[
                mode
            ]
        ):
            await task
        assert children[0].returncode is not None

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_SANDBOX") != "true",
    reason="Build image and set AIDLC_TEST_SANDBOX=true with Docker socket access",
)
def test_real_runc_isolation_and_resource_failure_cleanup(tmp_path, monkeypatch):
    configured = Settings()
    runner = DockerSandbox(Settings(data_dir=tmp_path, sandbox_image=resolve_image(configured)))

    async def exercise():
        probe = await runner.execute(SandboxJob(source=FIXTURE, mode="probe"), "runc")
        assert probe.status == "completed", probe
        assert all(value is True for value in json.loads(probe.stdout).values())
        for mode in ("build", "test", "analyze"):
            result = await runner.execute(SandboxJob(source=FIXTURE, mode=mode), "runc")
            assert result.status == "completed" and result.execution.cleanup_succeeded, result
        for code, expected in [
            (
                "import time\nimport unittest\nclass Slow(unittest.TestCase):\n"
                "    def test_sleep(self):\n        time.sleep(30)\n",
                "timed_out",
            ),
            (
                "import unittest\nclass Loud(unittest.TestCase):\n"
                "    def test_output(self):\n        print('x' * 1000000)\n",
                "output_limit",
            ),
            (
                "import unittest\nclass Memory(unittest.TestCase):\n"
                "    def test_memory(self):\n        self.value = bytearray(1024**3)\n",
                "failed",
            ),
            ("# no test cases\n", "failed"),
        ]:
            source = SourceBundle.model_validate(
                {"files": [{"path": "test_limits.py", "content": code}]}
            )
            result = await runner.execute(
                SandboxJob(
                    source=source,
                    mode="test",
                    policy=SandboxPolicy(timeout_seconds=3, max_output_bytes=4096),
                ),
                "runc",
            )
            assert result.status == expected and result.execution.cleanup_succeeded, result
            if "bytearray" in code:
                assert result.execution.oom_killed
        source = SourceBundle.model_validate(
            {
                "files": [
                    {
                        "path": "test_policy.py",
                        "content": (
                            "import errno\nimport os\nimport subprocess\n"
                            "import sys\nimport unittest\n"
                            "from pathlib import Path\nclass Policy(unittest.TestCase):\n"
                            "    def test_cgroups(self):\n"
                            "        root = Path('/sys/fs/cgroup')\n"
                            "        self.assertEqual((root/'memory.max').read_text().strip(), "
                            "'536870912')\n"
                            "        self.assertEqual((root/'pids.max').read_text().strip(), "
                            "'64')\n"
                            "        self.assertEqual((root/'cpu.max').read_text().strip(), "
                            "'100000 100000')\n"
                            "    def test_disk(self):\n"
                            "        with self.assertRaises(OSError) as failure:\n"
                            "            Path('/workspace/full').write_bytes(b'x' * 33554432)\n"
                            "        self.assertEqual(failure.exception.errno, errno.ENOSPC)\n"
                            "    def test_pids(self):\n"
                            "        children = []\n"
                            "        try:\n"
                            "            with self.assertRaises(OSError):\n"
                            "                for _ in range(100):\n"
                            "                    children.append(subprocess.Popen("
                            "[sys.executable, '-c', "
                            "'import time; time.sleep(30)']))\n"
                            "            self.assertLess(len(children), 64)\n"
                            "        finally:\n"
                            "            for child in children:\n"
                            "                child.kill()\n                child.wait()\n"
                            "    def test_credentials(self):\n"
                            "        self.assertNotIn('AIDLC_MCP_CLIENT_SECRET', os.environ)\n"
                        ),
                    }
                ]
            }
        )
        monkeypatch.setenv("AIDLC_MCP_CLIENT_SECRET", "test-secret-must-not-reach-container")
        result = await runner.execute(SandboxJob(source=source, mode="test"), "runc")
        assert result.status == "completed" and result.execution.cleanup_succeeded, result
        source = SourceBundle.model_validate(
            {
                "files": [
                    {
                        "path": "test_cancel.py",
                        "content": (
                            "import time\nimport unittest\nclass Slow(unittest.TestCase):\n"
                            "    def test_wait(self):\n        time.sleep(30)\n"
                        ),
                    }
                ]
            }
        )
        created = asyncio.Event()
        real_capture = runner._capture
        removed = []

        async def observe(command, **kwargs):
            result = await real_capture(command, **kwargs)
            if command[4] == "inspect":
                created.set()
            if command[4] == "rm":
                removed.append(result[0])
            return result

        monkeypatch.setattr(runner, "_capture", observe)
        task = asyncio.create_task(runner.execute(SandboxJob(source=source, mode="test"), "runc"))
        async with asyncio.timeout(10):
            await created.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert removed == [0]

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_SANDBOX") != "true",
    reason="Build image and set AIDLC_TEST_SANDBOX=true with Docker socket access",
)
def test_real_a2a_build_and_test_publish_trusted_runc_evidence(tmp_path):
    from aidlc.a2a.client import A2AInvoker
    from aidlc.agents.catalog import AGENT_SPECS
    from aidlc.domain.models import AgentContext, ArtifactKind, StageName
    from aidlc.storage.artifacts import ArtifactStore
    from aidlc.storage.database import WorkflowDatabase
    from tests.helpers import reference_transport

    settings = Settings(
        data_dir=tmp_path, sandbox_image=resolve_image(Settings()), sandbox_execution_enabled=True
    )
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    database.create_run("run_a2a_fixture", "Explicit trusted A2A sandbox fixture")
    store = ArtifactStore(settings.artifact_dir)
    source = store.write(
        run_id="run_a2a_fixture",
        stage=StageName.IMPLEMENTATION,
        kind=ArtifactKind.CODE_CHANGE,
        producing_agent="explicit-fixture",
        content=FIXTURE.model_dump(mode="json"),
        markdown="# Trusted fixture",
    )
    database.add_artifact(source)
    context = AgentContext(
        run_id="run_a2a_fixture",
        idea="Explicit fixture",
        stage=StageName.INTEGRATION,
        artifacts=[
            {
                "metadata": source.metadata.model_dump(mode="json"),
                "content": store.read_content(source),
            }
        ],
    )

    async def exercise():
        async with reference_transport(settings) as client:
            invoker = A2AInvoker(settings, database, client)
            for name in ("build-agent", "validation-agent"):
                spec = next(item for item in AGENT_SPECS if item.name == name)
                result = await invoker.invoke(spec, context, lambda *_: None)
                assert result.content["status"] == "completed", result
                record = database.get_artifact(result.content["report_artifact_ids"][0])
                assert (
                    record is not None
                    and record.metadata.producing_agent == "trusted-sandbox-service"
                )
                report = store.read_content(record)
                assert report["source_artifact_id"] == source.metadata.artifact_id
                assert report["source_sha256"] == source.metadata.content_sha256
                assert report["execution"]["runtime"] == "runc"
                assert report["execution"]["image"] == settings.sandbox_image
                assert report["execution"]["cleanup_succeeded"]
                assert report["execution"]["policy"]["network"] == "none"
            assert len(database.list_delegated_tasks("run_a2a_fixture")) == 2

    asyncio.run(exercise())


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_SANDBOX") != "true",
    reason="Build image and set AIDLC_TEST_SANDBOX=true with Docker socket access",
)
def test_real_mcp_uses_runc_with_scoped_authorization(tmp_path):
    import httpx

    from aidlc.tools.server import create_tool_app
    from tests.test_tools import SignedTestIdentity, analysis_call, headers, source_artifact

    identity = SignedTestIdentity()
    settings = Settings(
        data_dir=tmp_path, sandbox_image=resolve_image(Settings()), sandbox_backend="runc"
    )

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(identity.http)) as oidc:
            app = create_tool_app(settings, oidc_http_client=oidc)
            async with app.router.lifespan_context(app):
                artifact = source_artifact(app)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app),
                    base_url=identity.resource.removesuffix("/mcp"),
                ) as client:
                    routing = {"MCP-Method": "tools/call", "MCP-Name": "analyze_code"}
                    denied = await client.post(
                        "/mcp",
                        json=analysis_call(artifact),
                        headers={**headers(identity.token("analysis:read")), **routing},
                    )
                    assert denied.status_code == 403
                    allowed = await client.post(
                        "/mcp",
                        json=analysis_call(artifact),
                        headers={**headers(identity.token("analysis:run")), **routing},
                    )
                    report = allowed.json()["result"]["structuredContent"]
                    assert report["status"] == "completed", report
                    assert report["findings"][0]["rule_id"] == "F401"
                    assert report["findings"][0]["path"] == "src/example.py"
                    execution = report["execution"]
                    assert execution["runtime"] == "runc" and execution["image"]
                    assert execution["policy"]["network"] == "none"
                    assert execution["cleanup_succeeded"]
                    event = app.state.service.database.events_after("run_tools")[-1]
                    assert event.payload["runtime"] == "runc"
                    assert event.payload["image"] == execution["image"]

    asyncio.run(exercise())
