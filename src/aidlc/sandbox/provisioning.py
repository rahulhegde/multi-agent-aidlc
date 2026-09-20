"""Stage checksum-pinned runtime archives and reviewable host configuration."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx

from aidlc.config import Settings
from aidlc.domain.models import utc_now

MAX_ARCHIVE_BYTES = 2 * 1024**3
MAX_EXTRACTED_BYTES = 8 * 1024**3


def runtime_lock(runtime: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "dev" / "runtime-lock.json"
    try:
        return json.loads(path.read_text())[runtime]
    except OSError, ValueError, KeyError, TypeError:
        raise ValueError(
            "Run prepare-runtime from a checkout containing dev/runtime-lock.json"
        ) from None


def daemon_config(existing: dict[str, Any], runtime: str) -> dict[str, Any]:
    """Preserve existing daemon options and reject conflicting registrations."""
    if runtime not in {"runsc", "kata"}:
        raise ValueError("Unsupported runtime registration")
    entry = (
        {"path": "/opt/aidlc/runsc-release-20260907.0/runsc"}
        if runtime == "runsc"
        else {"runtimeType": "/opt/kata/bin/containerd-shim-kata-v2"}
    )
    runtimes = existing.get("runtimes", {})
    if not isinstance(runtimes, dict):
        raise ValueError("Existing Docker runtimes must be an object")
    if runtime in runtimes and runtimes[runtime] != entry:
        raise ValueError(f"Existing {runtime} registration conflicts with the pinned plan")
    return {**existing, "runtimes": {**runtimes, runtime: entry}}


def _extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:*") as bundle:
        total = 0
        seen = set()
        for member in bundle:
            total += member.size
            if total > MAX_EXTRACTED_BYTES or len(seen) >= 100_000:
                raise ValueError("Runtime archive exceeds extraction limits")
            if member.name in seen:
                raise ValueError("Duplicate runtime archive member")
            seen.add(member.name)
            bundle.extract(member, destination, filter="data")


def _inputs(directory: Path) -> dict[str, str]:
    inputs = {}
    for path in sorted(directory.rglob("*")):
        name = str(path.relative_to(directory))
        if path.is_symlink():
            inputs[name] = "symlink:" + os.readlink(path)
        elif path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            inputs[name] = f"sha256:{digest}:mode={path.stat().st_mode & 0o777:o}"
    return inputs


def prepare_runtime(
    settings: Settings,
    runtime: Literal["runsc", "kata"],
    archive: Path | None = None,
    existing_daemon: Path | None = None,
) -> dict[str, Any]:
    if runtime not in {"runsc", "kata"}:
        raise ValueError("Only runsc and Kata archives are provisioned")
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"}:
        raise ValueError("Runtime provisioning requires Linux amd64")
    pin = runtime_lock(runtime)
    if runtime == "kata" and not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return {
            "status": "unavailable",
            "runtime": runtime,
            "version": pin["version"],
            "reason": "Kata provisioning skipped: /dev/kvm is not usable by the sandbox service",
        }
    existing = json.loads(existing_daemon.read_text()) if existing_daemon else {}
    if not isinstance(existing, dict):
        raise ValueError("Existing Docker configuration must be an object")
    configuration = daemon_config(existing, runtime)
    root = settings.data_dir / "runtimes"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{runtime}-{pin['version']}"
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=root) as temporary:
        work = Path(temporary)
        downloaded = work / "archive"
        if archive is None:
            with httpx.stream("GET", pin["url"], follow_redirects=True, timeout=30) as response:
                response.raise_for_status()
                size = 0
                with downloaded.open("wb") as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_ARCHIVE_BYTES:
                            raise ValueError("Runtime archive exceeds download limit")
                        output.write(chunk)
        else:
            if not archive.is_file() or archive.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ValueError("Runtime archive is absent or exceeds size limit")
            shutil.copyfile(archive, downloaded)
        with downloaded.open("rb") as source:
            digest = hashlib.file_digest(source, pin["algorithm"]).hexdigest()
        if digest != pin["digest"]:
            raise ValueError("Runtime archive checksum differs from dev/runtime-lock.json")
        extracted = work / "payload"
        extracted.mkdir()
        _extract(downloaded, extracted)
        required = (
            ("runsc", "containerd-shim-runsc-v1", "gvisor-bin")
            if runtime == "runsc"
            else ("opt/kata/bin/containerd-shim-kata-v2", "opt/kata/share/defaults")
        )
        if any(not (extracted / path).exists() for path in required):
            raise ValueError("Pinned runtime archive is missing required binaries or sidecars")
        inputs = _inputs(extracted)
        if destination.exists():
            if _inputs(destination) != inputs:
                raise ValueError("Staged runtime was modified; select a clean data directory")
        else:
            os.replace(extracted, destination)
    report = {
        "status": "prepared",
        "runtime": runtime,
        "version": pin["version"],
        "prepared_at": utc_now().isoformat(),
        "archive": pin,
        "staged_directory": str(destination),
        "inputs": inputs,
        "daemon_config": configuration,
        "install_destination": "/opt/aidlc/runsc-release-20260907.0" if runtime == "runsc" else "/",
        "qualification_required": True,
    }
    (root / f"{runtime}-plan.json").write_text(json.dumps(report, indent=2))
    (root / f"{runtime}-daemon.json").write_text(json.dumps(configuration, indent=2))
    return report
