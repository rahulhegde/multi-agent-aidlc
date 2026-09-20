from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    path: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=65_536)

    @field_validator("path")
    @classmethod
    def safe_source_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or "\\" in value
            or ":" in value
            or "\x00" in value
            or any(part.startswith(".") or not part for part in value.split("/"))
            or path.suffix
            not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
                ".mjs",
                ".json",
                ".html",
                ".css",
                ".md",
                ".txt",
            }
            or any(part in {"node_modules", "dist", "__pycache__"} for part in path.parts)
        ):
            raise ValueError(
                "Expected a relative source file path without traversal or build output"
            )
        return value


class SourceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    files: tuple[SourceFile, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def bounded_bundle(self) -> SourceBundle:
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Duplicate source paths")
        if sum(len(file.content.encode()) for file in self.files) > 262_144:
            raise ValueError("Source bundle exceeds 256 KiB")
        return self


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{32}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    language: Literal["python"] = "python"
    ruleset: Literal["default"] = "default"
    timeout_seconds: int = Field(default=60, ge=1, le=60)


class AnalysisJob(BaseModel):
    """The analyzer receives source and limits only; no caller credentials or identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    source: SourceBundle
    timeout_seconds: int
    max_output_bytes: int


class Finding(BaseModel):
    rule_id: str
    severity: Literal["error", "warning"]
    path: str
    line: int
    column: int
    message: str
    fingerprint: str


class AnalyzerExecution(BaseModel):
    runtime: str = "local-analyzer"
    isolation: str = "trusted local Ruff process; no OCI isolation"
    duration_ms: int
    timed_out: bool = False
    output_truncated: bool = False
    image: str | None = None
    container_id: str | None = None
    kernel: str | None = None
    runtime_version: str | None = None
    exit_code: int | None = None
    oom_killed: bool = False
    cleanup_succeeded: bool = True
    reason: str | None = None
    policy: dict[str, str | int] = Field(default_factory=dict)


class AnalyzerResult(BaseModel):
    status: Literal["completed", "failed", "timed_out", "output_limit", "unavailable"]
    tool: dict[str, str] = Field(default_factory=lambda: {"name": "ruff", "version": "0.16.7"})
    findings: list[Finding] = Field(default_factory=list)
    execution: AnalyzerExecution


class AnalysisReport(AnalyzerResult):
    analysis_id: str
    artifact_id: str
    content_sha256: str
    run_id: str
    result_uri: str
    summary: dict[str, int]
