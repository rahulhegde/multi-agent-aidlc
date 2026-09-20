from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from aidlc.tools.models import SourceBundle

Runtime = Literal["runc", "runsc", "kata"]
Mode = Literal["analyze", "build", "test", "probe"]
PROBE_CHECKS = frozenset(
    {
        "non_root",
        "no_capabilities",
        "no_new_privileges",
        "no_credentials",
        "no_host_sockets",
        "no_host_home",
        "root_read_only",
        "root_mount_read_only",
        "bounded_tmpfs:/workspace",
        "bounded_tmpfs:/tmp",
        "network_interfaces",
        "network_denied",
    }
)


class SandboxPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cpus: int = Field(default=1, ge=1, le=2)
    memory_bytes: int = Field(default=536_870_912, ge=134_217_728, le=1_073_741_824)
    pids: int = Field(default=64, ge=16, le=128)
    workspace_bytes: int = Field(default=16_777_216, ge=1_048_576, le=16_777_216)
    timeout_seconds: int = Field(default=60, ge=1, le=60)
    max_output_bytes: int = Field(default=262_144, ge=1, le=262_144)
    network: Literal["none"] = "none"


class SandboxJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SourceBundle
    mode: Mode
    policy: SandboxPolicy = Field(default_factory=SandboxPolicy)


class SandboxExecution(BaseModel):
    runtime: Runtime
    isolation: str
    image: str | None = None
    container_id: str | None = None
    kernel: str
    runtime_version: str | None = None
    duration_ms: int = 0
    exit_code: int | None = None
    timed_out: bool = False
    output_truncated: bool = False
    oom_killed: bool = False
    cleanup_succeeded: bool = True
    reason: str | None = None
    policy: SandboxPolicy


class SandboxResult(BaseModel):
    status: Literal["completed", "failed", "timed_out", "output_limit", "unavailable"]
    execution: SandboxExecution
    stdout: str = ""
    stderr: str = ""
