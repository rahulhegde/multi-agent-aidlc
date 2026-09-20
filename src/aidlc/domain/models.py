from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    BLOCKED = "blocked"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
    RunStatus.BLOCKED,
}


class StageName(StrEnum):
    PREFLIGHT = "preflight"
    INTAKE = "intake"
    REQUIREMENTS = "requirements"
    DISCOVERY = "discovery"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    INTEGRATION = "integration"
    EVALUATION = "evaluation"
    RELEASE = "release"


STAGE_ORDER = tuple(StageName)


class ArtifactKind(StrEnum):
    HOST_CAPABILITY_REPORT = "host_capability_report"
    SANDBOX_QUALIFICATION_REPORT = "sandbox_qualification_report"
    SANDBOX_RECOVERY_REPORT = "sandbox_recovery_report"
    PROJECT_BRIEF = "project_brief"
    REQUIREMENTS_SPEC = "requirements_spec"
    ARCHITECTURE_DECISION = "architecture_decision"
    UX_SPECIFICATION = "ux_specification"
    THREAT_MODEL = "threat_model"
    TEST_PLAN = "test_plan"
    IMPLEMENTATION_PLAN = "implementation_plan"
    CODE_CHANGE = "code_change"
    INTEGRATED_SOURCE = "integrated_source"
    BUILD_REPORT = "build_report"
    TEST_REPORT = "test_report"
    STATIC_ANALYSIS_REPORT = "static_analysis_report"
    QUALITY_GATE_REPORT = "quality_gate_report"
    REPAIR_REQUEST = "repair_request"
    EVALUATION_REPORT = "evaluation_report"
    RELEASE_BUNDLE = "release_bundle"
    HUMAN_REQUEST = "human_request"
    HUMAN_RESPONSE = "human_response"


class CapabilityStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


class CapabilityCheck(BaseModel):
    name: str
    status: CapabilityStatus
    detail: str
    required: bool = True


class HostCapabilityReport(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    ready: bool
    degraded: bool
    os_id: str
    os_version: str
    architecture: str
    kernel: str
    checks: list[CapabilityCheck]


def project_id_for_run(run_id: str) -> str:
    """Each newly created workflow owns a distinct project workspace."""
    import hashlib

    return "project_" + hashlib.sha256(run_id.encode()).hexdigest()[:32]


class ProjectScopedModel(BaseModel):
    project_id: str = ""

    @model_validator(mode="after")
    def resolve_project(self):
        run_id = getattr(self, "workflow_run_id", None) or getattr(self, "run_id", None)
        if not isinstance(run_id, str):
            raise ValueError("Project scope requires a workflow run ID")
        expected = project_id_for_run(run_id)
        if self.project_id and self.project_id != expected:
            raise ValueError("Project identity does not match workflow run")
        object.__setattr__(self, "project_id", expected)
        return self


class ArtifactMetadata(ProjectScopedModel):
    model_config = ConfigDict(frozen=True)

    current_snapshot: bool = False
    schema_version: str = "1.0"
    artifact_id: str
    kind: ArtifactKind
    workflow_run_id: str
    stage_id: StageName
    parent_artifact_ids: list[str] = Field(default_factory=list)
    producing_agent: str
    model_id: str | None = None
    prompt_version: str | None = None
    execution: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    content_sha256: str


class ArtifactRecord(BaseModel):
    metadata: ArtifactMetadata
    content_path: str


class StageRecord(BaseModel):
    name: StageName
    status: RunStatus = RunStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class WorkflowRun(ProjectScopedModel):
    run_id: str
    idea: str
    status: RunStatus
    current_stage: StageName | None = None
    created_at: datetime
    updated_at: datetime
    error: str | None = None
    stages: list[StageRecord] = Field(default_factory=list)


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    idea: str = Field(min_length=3, max_length=2_000)


class EventRecord(BaseModel):
    event_id: int
    run_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


class DelegatedTask(BaseModel):
    invocation_id: str
    run_id: str
    stage: StageName
    agent: str
    endpoint: str
    context_id: str
    task_id: str | None = None
    state: str = "pending"
    attempts: int = 0
    repair_attempt: int = Field(default=0, ge=0)


class AgentContext(ProjectScopedModel):
    retry_attempt: int = Field(default=0, ge=0)
    run_id: str
    idea: str
    stage: StageName
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    human_response: dict[str, Any] | None = None
    repair_attempt: int = Field(default=0, ge=0)
    repair_feedback: dict[str, Any] | None = None
    repair_artifact_id: str | None = None
    repair_artifact_sha256: str | None = None


class HumanPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str = Field(pattern="^(clarification|approval|evaluation_decision)$")
    question: str = Field(min_length=3, max_length=2000)


class ExecutionMetadata(BaseModel):
    execution_id: str | None = None
    model_id: str = "deterministic"
    prompt_version: str = "deterministic-v1"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str = "unavailable"


class HumanAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action_id: str = Field(min_length=1, max_length=100)
    surface_id: str
    artifact_sha256: str
    version: int = 1
    decision: str = Field(pattern="^(submit|approve|reject|accept|repair)$")
    answer: str = Field(default="", max_length=4000)


class HumanInteraction(BaseModel):
    surface_id: str
    run_id: str
    stage: StageName
    agent: str
    task_id: str
    context_id: str
    prompt: HumanPrompt
    artifact_id: str
    artifact_sha256: str
    version: int = 1
    messages: list[dict[str, Any]]
    response: dict[str, Any] | None = None


class AgentResult(BaseModel):
    artifact_kind: ArtifactKind
    content: dict[str, Any]
    markdown: str
    human_prompt: HumanPrompt | None = None
    execution: ExecutionMetadata = Field(default_factory=ExecutionMetadata)


class ProjectBrief(BaseModel):
    idea: str
    target_user: str
    problem: str
    assumptions: list[str]
    mvp_goal: str


class RequirementsSpec(BaseModel):
    product_name: str
    functional_requirements: list[str]
    non_functional_requirements: list[str]
    acceptance_criteria: list[str]
    exclusions: list[str]
    open_questions: list[str]
