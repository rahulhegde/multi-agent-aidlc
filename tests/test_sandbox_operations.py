"""Cross-process ownership, strict qualification, and pinned runtime preparation."""

import asyncio
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile

import pytest

from aidlc.config import Settings
from aidlc.sandbox.cli import FIXTURE, qualify
from aidlc.sandbox.comparison import compare_runtimes
from aidlc.sandbox.leases import JobLease, sandbox_owner
from aidlc.sandbox.models import PROBE_CHECKS, SandboxExecution, SandboxResult
from aidlc.sandbox.provisioning import daemon_config, prepare_runtime
from aidlc.sandbox.recovery import publish_recovery
from aidlc.sandbox.runner import DockerSandbox
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from tests.test_sandbox import IMAGE, backend

NAME = "aidlc-sandbox-" + "a" * 32
ID = "b" * 64


class RecoveryDocker:
    def __init__(self, settings, *, foreign=False, cleanup_failure=False):
        self.commands = []
        self.exists = True
        self.cleanup_failure = cleanup_failure
        self.definition = {
            "Id": ID,
            "Name": "/" + NAME,
            "Config": {
                "Labels": {
                    "aidlc.sandbox": "true",
                    "aidlc.job": NAME,
                    "aidlc.owner": "foreign" if foreign else sandbox_owner(settings),
                }
            },
        }

    async def capture(self, command, **kwargs):
        self.commands.append(command)
        operation = command[4]
        if operation == "ps":
            return 0, (ID + "\n").encode() if self.exists else b"", b""
        if operation == "inspect":
            return 0, json.dumps([self.definition]).encode(), b""
        if operation == "rm":
            assert command[-1] == ID
            if self.cleanup_failure:
                return 1, b"", b"denied"
            self.exists = False
            return 0, b"removed", b""
        raise AssertionError(command)


def recovery_backend(tmp_path, monkeypatch, **options):
    settings = Settings(data_dir=tmp_path)
    wire = RecoveryDocker(settings, **options)
    monkeypatch.setattr("aidlc.sandbox.runner.shutil.which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(DockerSandbox, "_capture", lambda self, cmd, **kw: wire.capture(cmd, **kw))
    return settings, wire, DockerSandbox(settings)


def test_recovery_preserves_active_jobs_then_removes_abandoned_job(tmp_path, monkeypatch):
    settings, wire, runner = recovery_backend(tmp_path, monkeypatch)
    with JobLease(settings, NAME):
        active = asyncio.run(runner.reconcile())
        assert active["active"] == [ID] and wire.exists
        assert not any(command[4] == "rm" for command in wire.commands)
    recovered = asyncio.run(runner.reconcile())
    assert recovered["status"] == "completed" and recovered["removed"] == [ID]
    assert not wire.exists
    published = publish_recovery(settings, recovered)
    record = WorkflowDatabase(settings.database_path).get_artifact(published["artifact_id"])
    assert record and ArtifactStore.read_content(record) == recovered


def test_sigkill_releases_job_lease_without_pid_or_age_guessing(tmp_path, monkeypatch):
    settings, wire, runner = recovery_backend(tmp_path, monkeypatch)
    program = (
        "import sys,time; from pathlib import Path; from aidlc.config import Settings; "
        "from aidlc.sandbox.leases import JobLease; "
        "lease=JobLease(Settings(data_dir=Path(sys.argv[1])),sys.argv[2]); "
        "print('ready',flush=True); time.sleep(60)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(tmp_path), NAME],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout and child.stdout.readline().strip() == "ready"
        assert asyncio.run(runner.reconcile())["active"] == [ID]
        child.kill()
        child.wait(timeout=5)
        assert asyncio.run(runner.reconcile())["removed"] == [ID]
        assert not wire.exists
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)


def test_foreign_or_legacy_containers_are_preserved(tmp_path, monkeypatch):
    _, wire, runner = recovery_backend(tmp_path, monkeypatch, foreign=True)
    report = asyncio.run(runner.reconcile())
    assert report["ignored"] == [ID] and wire.exists
    assert not any(command[4] == "rm" for command in wire.commands)


def test_recovery_failure_requires_confirmed_absence(tmp_path, monkeypatch):
    _, wire, runner = recovery_backend(tmp_path, monkeypatch, cleanup_failure=True)
    report = asyncio.run(runner.reconcile())
    assert report["status"] == "failed" and report["failed"] == [ID]
    assert wire.exists and not report["removed"]


def test_invalid_recovery_inventory_never_reaches_removal(tmp_path, monkeypatch):
    _, wire, runner = recovery_backend(tmp_path, monkeypatch)

    async def capture(command, **kwargs):
        return 0, b"malformed-id\n", b""

    monkeypatch.setattr(runner, "_capture", capture)
    assert asyncio.run(runner.reconcile())["status"] == "failed"
    assert not wire.commands


@pytest.mark.parametrize("probe", [{"non_root": True}, [], {key: 1 for key in PROBE_CHECKS}])
def test_incomplete_or_nonboolean_probe_cannot_qualify(tmp_path, monkeypatch, probe):
    settings, wire, _ = backend(tmp_path, monkeypatch)
    capture = wire.capture

    async def altered(command, **kwargs):
        result = await capture(command, **kwargs)
        if command[4] == "start" and any(
            c[4] == "create" and c[-1] == "probe" for c in wire.commands[-4:]
        ):
            return 0, json.dumps(probe).encode(), b""
        return result

    monkeypatch.setattr(wire, "capture", altered)
    result = asyncio.run(qualify(settings, repetitions=1))
    assert result["runtimes"][0]["status"] == "fail"
    assert not result["baseline_qualified"]


class TimedBackend:
    def __init__(self, different_image=False, fail_later=False):
        self.calls = []
        self.different_image = different_image
        self.fail_later = fail_later

    async def execute(self, job, runtime):
        self.calls.append((runtime, job.mode))
        from aidlc.sandbox.models import SandboxPolicy

        return SandboxResult(
            status="unavailable"
            if runtime == "kata"
            else (
                "failed"
                if self.fail_later and runtime == "runsc" and len(self.calls) > 10
                else "completed"
            ),
            execution=SandboxExecution(
                runtime=runtime,
                isolation=runtime,
                image=(
                    "sha256:" + "c" * 64 if self.different_image and runtime == "runsc" else IMAGE
                ),
                kernel="fixture-kernel",
                policy=SandboxPolicy(),
                exit_code=0,
                duration_ms=10 if runtime == "runc" else 20,
            ),
            stdout=json.dumps(dict.fromkeys(PROBE_CHECKS, True)) if job.mode == "probe" else "[]",
        )


@pytest.mark.parametrize(
    "different_image,fail_later", [(False, False), (True, False), (False, True)]
)
def test_comparison_rotates_order_and_withholds_invalid_ratios(different_image, fail_later):
    backend = TimedBackend(different_image, fail_later)
    runtimes, comparison = asyncio.run(compare_runtimes(backend, FIXTURE, 3))  # type: ignore[arg-type]
    assert comparison["repetitions"] == 3
    assert [item["runtime"] for item in comparison["execution_order"][:4]] == [
        "runc",
        "runsc",
        "kata",
        "runsc",
    ]
    if different_image or fail_later:
        assert runtimes[1]["relative_to_runc"] is None
    else:
        assert runtimes[1]["timings"]["test"]["samples_ms"] == [20, 20, 20]
        assert runtimes[1]["relative_to_runc"]["test"] == 2
    assert runtimes[2]["status"] == "unavailable" and runtimes[2]["timings"] is None


def make_archive(tmp_path, monkeypatch, *, traversal=False):
    archive = tmp_path / "runtime.tar.bz2"
    with tarfile.open(archive, "w:bz2") as bundle:
        for name in ("runsc", "containerd-shim-runsc-v1", "gvisor-bin/sidecar"):
            member = tarfile.TarInfo("../escape" if traversal else name)
            data = b"trusted fixture"
            member.size, member.mode = len(data), 0o755
            bundle.addfile(member, io.BytesIO(data))
            if traversal:
                break
    digest = hashlib.sha512(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(
        "aidlc.sandbox.provisioning.runtime_lock",
        lambda _: {
            "version": "release-20260907.0",
            "algorithm": "sha512",
            "digest": digest,
        },
    )
    return archive


def test_pinned_runtime_staging_is_idempotent_and_detects_tampering(tmp_path, monkeypatch):
    archive = make_archive(tmp_path, monkeypatch)
    settings = Settings(data_dir=tmp_path / "data")
    result = prepare_runtime(settings, "runsc", archive)
    assert result["status"] == "prepared" and result["qualification_required"]
    assert prepare_runtime(settings, "runsc", archive)["inputs"] == result["inputs"]
    from pathlib import Path

    (Path(result["staged_directory"]) / "runsc").write_text("modified")
    with pytest.raises(ValueError, match="modified"):
        prepare_runtime(settings, "runsc", archive)


def test_wrong_checksum_and_archive_traversal_fail_before_publication(tmp_path, monkeypatch):
    archive = make_archive(tmp_path, monkeypatch, traversal=True)
    settings = Settings(data_dir=tmp_path / "data")
    with pytest.raises(tarfile.FilterError):
        prepare_runtime(settings, "runsc", archive)
    assert not (settings.data_dir / "escape").exists()
    archive.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        prepare_runtime(settings, "runsc", archive)
    assert not list((settings.data_dir / "runtimes").glob("*-plan.json"))


def test_daemon_plan_preserves_options_and_rejects_conflicts():
    existing = {"log-driver": "local", "default-runtime": "runc", "runtimes": {"custom": {}}}
    plan = daemon_config(existing, "runsc")
    assert plan["log-driver"] == "local" and plan["default-runtime"] == "runc"
    assert "custom" in plan["runtimes"] and "runsc" not in existing["runtimes"]
    assert daemon_config(plan, "runsc") == plan
    with pytest.raises(ValueError, match="conflicts"):
        daemon_config({"runtimes": {"runsc": {"path": "/different"}}}, "runsc")


def test_kata_without_kvm_skips_archive_access(tmp_path, monkeypatch):
    monkeypatch.setattr("aidlc.sandbox.provisioning.os.access", lambda *_: False)
    result = prepare_runtime(Settings(data_dir=tmp_path), "kata", tmp_path / "absent")
    assert result["status"] == "unavailable" and not (tmp_path / "runtimes").exists()


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_SANDBOX") != "true",
    reason="Requires the pinned image and Docker socket",
)
def test_real_docker_job_survives_active_scan_and_is_recovered_after_sigkill(tmp_path):
    from aidlc.sandbox.models import SandboxJob
    from aidlc.sandbox.runner import resolve_image

    settings = Settings(data_dir=tmp_path, sandbox_image=resolve_image(Settings()))
    # Die after Docker create and policy verification, before attach. This is
    # precisely the crash window where an in-process finally cannot tear down.
    program = """
import asyncio, sys
from pathlib import Path
from aidlc.config import Settings
from aidlc.sandbox.cli import FIXTURE
from aidlc.sandbox.models import SandboxJob
from aidlc.sandbox.runner import DockerSandbox
class PausedSandbox(DockerSandbox):
    async def _capture(self, command, **kwargs):
        if command[4] == 'start':
            print('created', flush=True)
            await asyncio.sleep(60)
        return await super()._capture(command, **kwargs)
asyncio.run(PausedSandbox(Settings(data_dir=Path(sys.argv[1]), sandbox_image=sys.argv[2]))
    .execute(SandboxJob(source=FIXTURE, mode='build')))
"""

    async def exercise():
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            program,
            str(tmp_path),
            settings.sandbox_image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        runner = DockerSandbox(settings)
        try:
            assert child.stdout
            ready = await asyncio.wait_for(child.stdout.readline(), timeout=30)
            assert ready.strip() == b"created"
            active = await runner.reconcile()
            assert len(active["active"]) == 1 and not active["removed"]
            child.kill()
            await asyncio.wait_for(child.wait(), timeout=5)
            result = await runner.execute(SandboxJob(source=FIXTURE, mode="build"))
            assert result.status == "completed" and result.execution.cleanup_succeeded
            database = WorkflowDatabase(settings.database_path)
            runs = database.list_runs()
            assert len(runs) == 1
            records = database.list_artifacts(runs[0].run_id)
            assert len(records) == 1
            evidence = ArtifactStore.read_content(records[0])
            assert evidence["removed"] == active["active"]
            remaining = await runner.reconcile()
            assert remaining["status"] == "completed" and not remaining["active"]
            assert not remaining["removed"]
        finally:
            if child.returncode is None:
                child.kill()
            await child.communicate()
            await runner.reconcile()

    asyncio.run(exercise())
