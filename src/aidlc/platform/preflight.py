from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
from pathlib import Path

from aidlc.config import Settings
from aidlc.domain.models import CapabilityCheck, CapabilityStatus, HostCapabilityReport


def _read_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _command_version(command: str, *arguments: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip().splitlines()
    return output[0][:240] if output else executable


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            candidate.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _registered_runtimes() -> dict[str, object]:
    executable = shutil.which("docker")
    if executable is None:
        return {}
    try:
        result = subprocess.run(
            [
                executable,
                "--host=unix:///var/run/docker.sock",
                "info",
                "--format",
                "{{json .Runtimes}}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        if result.returncode or len(result.stdout) > 65_536:
            return {}
        value = json.loads(result.stdout)
        return value if isinstance(value, dict) else {}
    except OSError, subprocess.TimeoutExpired, ValueError:
        return {}


def inspect_host(settings: Settings) -> HostCapabilityReport:
    """Inspect only: preflight never installs packages or changes host settings."""

    release = _read_os_release()
    os_id = release.get("ID", platform.system().lower())
    os_version = release.get("VERSION_ID", platform.release())
    architecture = platform.machine().lower()
    checks: list[CapabilityCheck] = []

    os_ok = os_id == settings.required_host_os and os_version.startswith(
        settings.required_host_version
    )
    checks.append(
        CapabilityCheck(
            name="operating_system",
            status=CapabilityStatus.PASS if os_ok else CapabilityStatus.FAIL,
            detail=f"found {os_id} {os_version}; expected Ubuntu 26.04.x",
        )
    )
    arch_ok = architecture in {"x86_64", "amd64"}
    checks.append(
        CapabilityCheck(
            name="architecture",
            status=CapabilityStatus.PASS if arch_ok else CapabilityStatus.FAIL,
            detail=f"found {architecture}; this learning profile targets amd64",
        )
    )

    cpu_count = os.cpu_count() or 0
    checks.append(
        CapabilityCheck(
            name="cpu",
            status=CapabilityStatus.PASS if cpu_count >= 4 else CapabilityStatus.WARN,
            detail=f"{cpu_count} logical CPUs detected; 4 or more recommended",
            required=False,
        )
    )
    try:
        memory_gib = (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) / (1024**3)
    except OSError, ValueError:
        memory_gib = 0
    checks.append(
        CapabilityCheck(
            name="memory",
            status=CapabilityStatus.PASS if memory_gib >= 8 else CapabilityStatus.FAIL,
            detail=f"{memory_gib:.1f} GiB total memory; at least 8 GiB required",
        )
    )

    cgroup_ok = Path("/sys/fs/cgroup/cgroup.controllers").exists()
    checks.append(
        CapabilityCheck(
            name="cgroup_v2",
            status=CapabilityStatus.PASS if cgroup_ok else CapabilityStatus.FAIL,
            detail="unified hierarchy detected" if cgroup_ok else "cgroup.controllers is absent",
        )
    )

    try:
        apparmor_enabled = (
            Path("/sys/module/apparmor/parameters/enabled").read_text().strip().lower() == "y"
        )
    except OSError:
        apparmor_enabled = False
    checks.append(
        CapabilityCheck(
            name="apparmor",
            status=CapabilityStatus.PASS if apparmor_enabled else CapabilityStatus.FAIL,
            detail="enabled" if apparmor_enabled else "not enabled or status is inaccessible",
        )
    )

    systemd_available = Path("/run/systemd/system").exists()
    checks.append(
        CapabilityCheck(
            name="systemd",
            status=CapabilityStatus.PASS if systemd_available else CapabilityStatus.FAIL,
            detail="system manager detected"
            if systemd_available
            else "system manager not detected",
        )
    )

    docker_version = _command_version("docker", "--version")
    checks.append(
        CapabilityCheck(
            name="docker",
            status=CapabilityStatus.PASS if docker_version else CapabilityStatus.UNAVAILABLE,
            detail=docker_version or "docker CLI is not installed",
        )
    )

    daemon_version = _command_version("docker", "info", "--format", "{{.ServerVersion}}")
    checks.append(
        CapabilityCheck(
            name="docker_daemon",
            status=CapabilityStatus.PASS if daemon_version else CapabilityStatus.UNAVAILABLE,
            detail=daemon_version or "Docker daemon is unavailable or access is denied",
        )
    )

    runtime_commands = {
        "runc": ("runc", "--version"),
        "runsc": ("runsc", "--version"),
        "kata": ("kata-runtime", "--version"),
    }
    registered = _registered_runtimes()
    for runtime, command in runtime_commands.items():
        runtime_version = _command_version(*command)
        available = runtime in registered
        checks.append(
            CapabilityCheck(
                name=f"sandbox_runtime:{runtime}",
                status=CapabilityStatus.PASS if available else CapabilityStatus.UNAVAILABLE,
                detail=(
                    f"registered with Docker; {runtime_version or 'CLI version unavailable'}; "
                    "workload qualification is separate"
                )
                if available
                else "not registered with Docker, or daemon access is denied",
                required=runtime == settings.sandbox_backend,
            )
        )

    if settings.mcp_enabled and settings.analysis_backend == "sandbox":
        from aidlc.sandbox.runner import resolve_image

        try:
            image = resolve_image(settings)
            image_detail, image_available = image, True
        except ValueError as error:
            image_detail, image_available = str(error), False
        checks.append(
            CapabilityCheck(
                name="sandbox_image",
                detail=image_detail,
                status=CapabilityStatus.PASS if image_available else CapabilityStatus.UNAVAILABLE,
            )
        )

    if settings.sandbox_backend not in runtime_commands:
        checks.append(
            CapabilityCheck(
                name="sandbox_backend",
                status=CapabilityStatus.FAIL,
                detail=f"Unsupported backend: {settings.sandbox_backend}",
            )
        )

    uses_microvm = settings.sandbox_backend == "kata"
    kvm_available = Path("/dev/kvm").exists() and os.access("/dev/kvm", os.R_OK | os.W_OK)
    checks.append(
        CapabilityCheck(
            name="kvm",
            status=(CapabilityStatus.PASS if kvm_available else CapabilityStatus.UNAVAILABLE),
            detail="/dev/kvm is usable" if kvm_available else "/dev/kvm is not usable",
            required=uses_microvm and settings.require_kvm_for_microvm,
        )
    )

    for module in ("vhost_vsock", "vhost_net"):
        loaded = (Path("/sys/module") / module).exists()
        checks.append(
            CapabilityCheck(
                name=f"kernel_module:{module}",
                status=CapabilityStatus.PASS if loaded else CapabilityStatus.UNAVAILABLE,
                detail="loaded"
                if loaded
                else "not loaded; Kata qualification must verify availability",
                required=uses_microvm,
            )
        )

    disk = shutil.disk_usage(settings.data_dir.parent)
    free_gib = disk.free / (1024**3)
    checks.append(
        CapabilityCheck(
            name="disk_space",
            status=CapabilityStatus.PASS if free_gib >= 5 else CapabilityStatus.FAIL,
            detail=f"{free_gib:.1f} GiB free; at least 5 GiB required",
        )
    )

    for port in (8000, 5173):
        available = _port_available(port)
        checks.append(
            CapabilityCheck(
                name=f"port:{port}",
                status=CapabilityStatus.PASS if available else CapabilityStatus.WARN,
                detail=(
                    "available"
                    if available
                    else "already in use (may be this application's service)"
                ),
                required=False,
            )
        )

    ready = all(
        check.status not in {CapabilityStatus.FAIL, CapabilityStatus.UNAVAILABLE}
        for check in checks
        if check.required
    )
    degraded = any(check.status != CapabilityStatus.PASS for check in checks)
    return HostCapabilityReport(
        ready=ready,
        degraded=degraded,
        os_id=os_id,
        os_version=os_version,
        architecture=architecture,
        kernel=platform.release(),
        checks=checks,
    )
