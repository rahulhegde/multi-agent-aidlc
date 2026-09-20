from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import shutil
import tempfile
import time
from typing import Any, cast
from uuid import uuid4

from aidlc.config import Settings
from aidlc.domain.models import utc_now
from aidlc.sandbox.leases import CONTAINER_ID, JOB_NAME, JobLease, sandbox_owner
from aidlc.sandbox.models import Runtime, SandboxExecution, SandboxJob, SandboxResult
from aidlc.tools.analyzer import OutputLimitError, _bounded_read

PINNED_IMAGE = re.compile(r"^(?:sha256:[a-f0-9]{64}|[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64})$")
ISOLATION = {
    "runc": "OCI namespaces, dropped capabilities, default seccomp, shared host kernel",
    "runsc": "gVisor user-space kernel (requires runtime qualification)",
    "kata": "Kata containerd shim microVM (requires runtime qualification)",
}


def resolve_image(settings: Settings) -> str:
    image = settings.sandbox_image
    if not image:
        try:
            image = json.loads((settings.data_dir / "sandbox-image.json").read_text())["image"]
        except OSError, ValueError, KeyError, TypeError:
            raise ValueError(
                "Build the pinned image with aidlc-sandbox build-image first"
            ) from None
    if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
        raise ValueError("Sandbox image must be a full SHA-256 image ID or repository digest")
    return image


class SandboxControlError(Exception):
    pass


class DockerSandbox:
    """No shell, bind mounts, caller credentials, image pulls, or runtime fallback.

    The Docker socket is used only by this trusted service, never by the workload.
    Named containers allow teardown even if creation is interrupted before its reply.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.analysis_max_parallel_jobs)
        self._recovery_lock = asyncio.Lock()

    async def reconcile(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(30):
                return await self._reconcile()
        except TimeoutError:
            return {
                "generated_at": utc_now().isoformat(),
                "owner": sandbox_owner(self.settings),
                "status": "failed",
                "removed": [],
                "active": [],
                "ignored": [],
                "failed": [],
                "reason": "Sandbox recovery exceeded its 30-second deadline",
            }

    async def _reconcile(self) -> dict[str, Any]:
        """Remove only owner-bound jobs whose execution lease is no longer held."""
        report: dict[str, Any] = {
            "generated_at": utc_now().isoformat(),
            "owner": sandbox_owner(self.settings),
            "status": "completed",
            "removed": [],
            "active": [],
            "ignored": [],
            "failed": [],
        }
        executable = shutil.which("docker")
        if executable is None:
            return {**report, "status": "unavailable", "reason": "Docker CLI is unavailable"}
        with tempfile.TemporaryDirectory(prefix="aidlc-docker-recovery-") as directory:
            docker = [executable, "--host=unix:///var/run/docker.sock", "--config", directory]

            async def capture(*args: str):
                return await self._capture([*docker, *args], deadline_seconds=5, limit=65_536)

            try:
                code, output, _ = await capture(
                    "ps",
                    "--all",
                    "--no-trunc",
                    "--filter",
                    "label=aidlc.sandbox=true",
                    "--filter",
                    f"label=aidlc.owner={report['owner']}",
                    "--format",
                    "{{.ID}}",
                )
                if code:
                    raise SandboxControlError("Cannot list sandbox jobs for recovery")
                identifiers = output.decode().splitlines()
                if len(identifiers) > 1000 or any(
                    not CONTAINER_ID.fullmatch(identifier) for identifier in identifiers
                ):
                    raise SandboxControlError("Invalid or excessive recovery inventory")
                for identifier in identifiers:
                    code, output, _ = await capture("inspect", identifier)
                    if code:
                        # Concurrent cleanup is allowed only when absence is confirmed.
                        code, remaining, _ = await capture(
                            "ps",
                            "--all",
                            "--no-trunc",
                            "--filter",
                            f"id={identifier}",
                            "--format",
                            "{{.ID}}",
                        )
                        if code or remaining.strip():
                            report["failed"].append(identifier)
                        continue
                    definition = json.loads(output)[0]
                    labels = definition["Config"].get("Labels") or {}
                    name = definition["Name"].removeprefix("/")
                    if (
                        definition["Id"] != identifier
                        or not JOB_NAME.fullmatch(name)
                        or labels.get("aidlc.sandbox") != "true"
                        or labels.get("aidlc.owner") != report["owner"]
                        or labels.get("aidlc.job") != name
                    ):
                        report["ignored"].append(identifier)
                        continue
                    try:
                        lease = JobLease(self.settings, name)
                    except BlockingIOError:
                        report["active"].append(identifier)
                        continue
                    with lease:
                        # IDs are immutable; using the ID also avoids removing a new
                        # container if somebody reuses the original name.
                        await capture("rm", "--force", identifier)
                        code, remaining, _ = await capture(
                            "ps",
                            "--all",
                            "--no-trunc",
                            "--filter",
                            f"id={identifier}",
                            "--format",
                            "{{.ID}}",
                        )
                        report["failed" if code or remaining.strip() else "removed"].append(
                            identifier
                        )
                if report["failed"]:
                    report["status"] = "failed"
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                IndexError,
                TimeoutError,
                ExceptionGroup,
                SandboxControlError,
            ):
                report["status"] = "failed"
                report["reason"] = "Sandbox recovery could not be confirmed"
        return report

    async def _capture(
        self, command: list[str], *, deadline_seconds: int, limit: int, payload: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            *command,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None

        async def feed():
            assert process.stdin is not None and payload is not None
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except BrokenPipeError, ConnectionResetError:
                pass
            finally:
                process.stdin.close()

        try:
            async with asyncio.timeout(deadline_seconds):
                async with asyncio.TaskGroup() as group:
                    stdout = group.create_task(_bounded_read(process.stdout, limit))
                    stderr = group.create_task(_bounded_read(process.stderr, limit))
                    group.create_task(process.wait())
                    if payload is not None:
                        group.create_task(feed())
            return process.returncode or 0, stdout.result(), stderr.result()
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def execute(self, job: SandboxJob, runtime: Runtime | None = None) -> SandboxResult:
        selected = runtime or self.settings.sandbox_backend
        if selected not in ISOLATION:
            raise ValueError("Unsupported sandbox runtime")
        async with self._slots:
            async with self._recovery_lock:
                report = await self.reconcile()
                if report["removed"] or report["status"] == "failed":
                    from aidlc.sandbox.recovery import publish_recovery

                    await asyncio.to_thread(publish_recovery, self.settings, report)
                if report["status"] != "completed":
                    return SandboxResult(
                        status="unavailable",
                        execution=SandboxExecution(
                            runtime=cast(Runtime, selected),
                            isolation=ISOLATION[selected],
                            kernel=platform.release(),
                            policy=job.policy,
                            reason=report.get("reason", "Sandbox orphan recovery failed"),
                        ),
                    )
            name = f"aidlc-sandbox-{uuid4().hex}"
            with JobLease(self.settings, name):
                return await self._execute(job, cast(Runtime, selected), name)

    async def _execute(self, job: SandboxJob, runtime: Runtime, name: str) -> SandboxResult:
        started = time.monotonic()
        execution = SandboxExecution(
            runtime=runtime,
            isolation=ISOLATION[runtime],
            kernel=platform.release(),
            policy=job.policy,
        )
        result = SandboxResult(status="unavailable", execution=execution)
        executable = shutil.which("docker")
        try:
            execution.image = await asyncio.to_thread(resolve_image, self.settings)
        except ValueError as error:
            execution.reason = str(error)
            return result
        if executable is None:
            execution.reason = "Docker CLI is unavailable"
            return result
        if runtime == "kata" and not (os.access("/dev/kvm", os.R_OK | os.W_OK)):
            execution.reason = (
                "Kata is unavailable: /dev/kvm is not accessible to the sandbox service"
            )
            return result
        with tempfile.TemporaryDirectory(prefix="aidlc-docker-config-") as directory:
            docker = [executable, "--host=unix:///var/run/docker.sock", "--config", directory]

            async def control(*arguments: str):
                code, output, _error = await self._capture(
                    [*docker, *arguments], deadline_seconds=5, limit=65_536
                )
                if code:
                    raise SandboxControlError("Docker control operation failed")
                return json.loads(output)

            try:
                info = await control("info", "--format", "{{json .}}")
                if info["OSType"] != "linux" or info["Architecture"] not in {"x86_64", "amd64"}:
                    raise SandboxControlError("Sandbox requires a Linux amd64 Docker daemon")
                if runtime not in info["Runtimes"]:
                    execution.reason = f"Runtime {runtime} is not registered with Docker"
                    return result
                features = (
                    info["Runtimes"][runtime]
                    .get("status", {})
                    .get("org.opencontainers.runtime-spec.features")
                )
                if features:
                    execution.runtime_version = (
                        json.loads(features)
                        .get("annotations", {})
                        .get("org.opencontainers.runc.version")
                    )
                image = (await control("image", "inspect", execution.image))[0]
                if image["Architecture"] != "amd64" or image["Os"] != "linux":
                    raise SandboxControlError("Pinned image must be Linux amd64")
                # Tags cannot change the selected image after verification.
                execution.image = image["Id"]
                if image["Config"].get("Volumes"):
                    raise SandboxControlError("Sandbox image must not declare volumes")
            except (
                SandboxControlError,
                OSError,
                TimeoutError,
                ValueError,
                KeyError,
                TypeError,
                ExceptionGroup,
            ):
                execution.reason = (
                    "Docker/image unavailable; check service socket access and build-image"
                )
                return result

            policy = job.policy
            command = [
                *docker,
                "create",
                "--name",
                name,
                "--label=aidlc.sandbox=true",
                f"--label=aidlc.owner={sandbox_owner(self.settings)}",
                f"--label=aidlc.job={name}",
                "--interactive",
                "--pull=never",
                f"--runtime={runtime}",
                "--network=none",
                "--read-only",
                "--user=65532:65532",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges=true",
                "--cpus=" + str(policy.cpus),
                "--memory=" + str(policy.memory_bytes),
                "--memory-swap=" + str(policy.memory_bytes),
                "--pids-limit=" + str(policy.pids),
                "--ulimit=nofile=128:128",
                "--ulimit=core=0:0",
                "--ipc=private",
                "--cgroupns=private",
                "--shm-size=1048576",
                "--log-driver=none",
                "--workdir=/workspace",
                f"--tmpfs=/workspace:rw,noexec,nosuid,nodev,size={policy.workspace_bytes},mode=1777",
                f"--tmpfs=/tmp:rw,noexec,nosuid,nodev,size={policy.workspace_bytes},mode=1777",
                "--entrypoint=python",
                execution.image,
                "-I",
                "/opt/aidlc/worker.py",
                job.mode,
            ]

            async def cleanup():
                try:
                    code, _, _ = await self._capture(
                        [*docker, "rm", "--force", name], deadline_seconds=5, limit=65_536
                    )
                    if code == 0:
                        return True
                    # A failed create can leave no container; verify that before claiming teardown.
                    code, remaining, _ = await self._capture(
                        [
                            *docker,
                            "ps",
                            "--all",
                            "--filter",
                            f"name=^{name}$",
                            "--format",
                            "{{json .}}",
                        ],
                        deadline_seconds=5,
                        limit=65_536,
                    )
                    return code == 0 and not remaining.strip()
                except Exception:
                    return False

            try:
                async with asyncio.timeout(policy.timeout_seconds):
                    code, output, _ = await self._capture(command, deadline_seconds=5, limit=65_536)
                    if code:
                        raise SandboxControlError("Container creation failed")
                    execution.container_id = output.decode().strip()
                    definition = (await control("inspect", name))[0]
                    host = definition["HostConfig"]
                    expected = {
                        "Runtime": runtime,
                        "NetworkMode": "none",
                        "ReadonlyRootfs": True,
                        "Privileged": False,
                        "Memory": policy.memory_bytes,
                        "MemorySwap": policy.memory_bytes,
                        "NanoCpus": policy.cpus * 10**9,
                        "PidsLimit": policy.pids,
                        "CgroupnsMode": "private",
                    }
                    if (
                        any(host.get(key) != value for key, value in expected.items())
                        or host.get("Binds")
                        or host.get("Devices")
                        or definition.get("Mounts")
                        or host.get("CapDrop") != ["ALL"]
                        or "no-new-privileges=true" not in host.get("SecurityOpt", [])
                        or definition["Config"]["User"] != "65532:65532"
                        or set(host.get("Tmpfs", {})) != {"/workspace", "/tmp"}
                    ):
                        raise SandboxControlError(
                            "Docker did not apply the required sandbox policy"
                        )
                    code, output, error = await self._capture(
                        [*docker, "start", "--attach", "--interactive", name],
                        deadline_seconds=policy.timeout_seconds,
                        limit=policy.max_output_bytes,
                        payload=job.source.model_dump_json().encode(),
                    )
                    state = (await control("inspect", name))[0]["State"]
                    if state["Running"]:
                        raise SandboxControlError("Docker attach ended before the workload")
                    execution.exit_code = state["ExitCode"]
                    execution.oom_killed = state["OOMKilled"]
                    result.stdout, result.stderr = (
                        output.decode(errors="replace"),
                        error.decode(errors="replace"),
                    )
                    accepted = {0, 1} if job.mode == "analyze" else {0}
                    result.status = (
                        "completed"
                        if (
                            code in accepted
                            and execution.exit_code in accepted
                            and not execution.oom_killed
                        )
                        else "failed"
                    )
            except TimeoutError:
                result.status, execution.timed_out = "timed_out", True
            except SandboxControlError as error:
                result.status, execution.reason = "failed", str(error)
            except ExceptionGroup as errors:
                if errors.subgroup(OutputLimitError):
                    result.status, execution.output_truncated = "output_limit", True
                else:
                    result.status, execution.reason = "failed", "Sandbox output collection failed"
            except OSError, ValueError, KeyError, TypeError:
                result.status, execution.reason = "failed", "Sandbox control or output invalid"
            finally:
                task = asyncio.create_task(cleanup())
                try:
                    execution.cleanup_succeeded = await asyncio.shield(task)
                except asyncio.CancelledError:
                    execution.cleanup_succeeded = await task
                    raise
                execution.duration_ms = int((time.monotonic() - started) * 1000)
                if not execution.cleanup_succeeded:
                    result.status, execution.reason = (
                        "failed",
                        "Container teardown could not be confirmed",
                    )
            return result
