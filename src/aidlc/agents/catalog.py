"""Scheduling and input contracts; the hub never imports specialist handlers."""

from dataclasses import dataclass

from aidlc.agents.profiles import GOAL_VERSION
from aidlc.domain.models import ArtifactKind, StageName

CONTEXT_CONTRACT_VERSION = "aidlc-context-v3"


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    """A deterministic artifact dependency for one specialist invocation."""

    kind: ArtifactKind
    producer: str | None = None
    required: bool = True
    fallback_kind: ArtifactKind | None = None


@dataclass(frozen=True, slots=True)
class AgentSpec:
    name: str
    stage: StageName
    artifact_kind: ArtifactKind
    inputs: tuple[ArtifactInput, ...] = ()
    include_idea: bool = False
    context_contract_version: str = CONTEXT_CONTRACT_VERSION
    prompt_version: str = GOAL_VERSION
    cacheable: bool = True


_ROLES = (
    ("intake-agent", StageName.INTAKE, ArtifactKind.PROJECT_BRIEF),
    ("requirements-agent", StageName.REQUIREMENTS, ArtifactKind.REQUIREMENTS_SPEC),
    ("architecture-agent", StageName.DISCOVERY, ArtifactKind.ARCHITECTURE_DECISION),
    ("ux-agent", StageName.DISCOVERY, ArtifactKind.UX_SPECIFICATION),
    ("security-agent", StageName.DISCOVERY, ArtifactKind.THREAT_MODEL),
    ("test-planner-agent", StageName.DISCOVERY, ArtifactKind.TEST_PLAN),
    ("planning-agent", StageName.PLANNING, ArtifactKind.IMPLEMENTATION_PLAN),
    ("backend-agent", StageName.IMPLEMENTATION, ArtifactKind.CODE_CHANGE),
    ("frontend-agent", StageName.IMPLEMENTATION, ArtifactKind.CODE_CHANGE),
    ("test-agent", StageName.IMPLEMENTATION, ArtifactKind.CODE_CHANGE),
    ("build-agent", StageName.INTEGRATION, ArtifactKind.BUILD_REPORT),
    ("validation-agent", StageName.INTEGRATION, ArtifactKind.TEST_REPORT),
    ("static-analysis-agent", StageName.INTEGRATION, ArtifactKind.STATIC_ANALYSIS_REPORT),
    ("evaluation-agent", StageName.EVALUATION, ArtifactKind.EVALUATION_REPORT),
    ("release-agent", StageName.RELEASE, ArtifactKind.RELEASE_BUNDLE),
)


def _input(
    kind: ArtifactKind,
    producer: str | None = None,
    *,
    required: bool = True,
    fallback_kind: ArtifactKind | None = None,
) -> ArtifactInput:
    return ArtifactInput(
        kind=kind,
        producer=producer,
        required=required,
        fallback_kind=fallback_kind,
    )


_INPUTS: dict[str, tuple[ArtifactInput, ...]] = {
    "intake-agent": (
        _input(ArtifactKind.PROJECT_BRIEF, "intake-agent", required=False),
        _input(ArtifactKind.HUMAN_RESPONSE, required=False),
    ),
    "requirements-agent": (
        _input(ArtifactKind.PROJECT_BRIEF),
        _input(ArtifactKind.HUMAN_RESPONSE, required=False),
    ),
    "architecture-agent": (
        _input(ArtifactKind.PROJECT_BRIEF),
        _input(ArtifactKind.REQUIREMENTS_SPEC),
    ),
    "ux-agent": (
        _input(ArtifactKind.PROJECT_BRIEF),
        _input(ArtifactKind.REQUIREMENTS_SPEC),
    ),
    "security-agent": (
        _input(ArtifactKind.PROJECT_BRIEF),
        _input(ArtifactKind.REQUIREMENTS_SPEC),
    ),
    "test-planner-agent": (
        _input(ArtifactKind.PROJECT_BRIEF),
        _input(ArtifactKind.REQUIREMENTS_SPEC),
    ),
    "planning-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.ARCHITECTURE_DECISION),
        _input(ArtifactKind.UX_SPECIFICATION),
        _input(ArtifactKind.THREAT_MODEL),
        _input(ArtifactKind.TEST_PLAN),
    ),
    "backend-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.ARCHITECTURE_DECISION),
        _input(ArtifactKind.IMPLEMENTATION_PLAN),
        _input(ArtifactKind.INTEGRATED_SOURCE, required=False),
        _input(ArtifactKind.CODE_CHANGE, "backend-agent", required=False),
    ),
    "frontend-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.ARCHITECTURE_DECISION),
        _input(ArtifactKind.UX_SPECIFICATION),
        _input(ArtifactKind.IMPLEMENTATION_PLAN),
        _input(ArtifactKind.INTEGRATED_SOURCE, required=False),
        _input(ArtifactKind.CODE_CHANGE, "frontend-agent", required=False),
    ),
    "test-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.TEST_PLAN),
        _input(ArtifactKind.IMPLEMENTATION_PLAN),
        _input(ArtifactKind.INTEGRATED_SOURCE, required=False),
        _input(ArtifactKind.CODE_CHANGE, "test-agent", required=False),
    ),
    "build-agent": (
        _input(
            ArtifactKind.INTEGRATED_SOURCE,
            required=False,
            fallback_kind=ArtifactKind.CODE_CHANGE,
        ),
    ),
    "validation-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.TEST_PLAN),
        _input(
            ArtifactKind.INTEGRATED_SOURCE,
            required=False,
            fallback_kind=ArtifactKind.CODE_CHANGE,
        ),
    ),
    "static-analysis-agent": (
        _input(
            ArtifactKind.INTEGRATED_SOURCE,
            required=False,
            fallback_kind=ArtifactKind.CODE_CHANGE,
        ),
    ),
    "evaluation-agent": (
        _input(ArtifactKind.REQUIREMENTS_SPEC),
        _input(ArtifactKind.ARCHITECTURE_DECISION),
        _input(ArtifactKind.UX_SPECIFICATION),
        _input(ArtifactKind.THREAT_MODEL),
        _input(ArtifactKind.TEST_PLAN),
        _input(ArtifactKind.INTEGRATED_SOURCE, required=False),
        _input(ArtifactKind.BUILD_REPORT),
        _input(ArtifactKind.TEST_REPORT),
        # Deterministic gates use the immutable reports emitted by the trusted
        # sandbox, while build/validation agents publish review snapshots of
        # the same execution. Supply both so every gate evidence ID is a real
        # evaluator input and can be cited by the rubric.
        _input(
            ArtifactKind.BUILD_REPORT,
            "trusted-sandbox-service",
            required=False,
        ),
        _input(
            ArtifactKind.TEST_REPORT,
            "trusted-sandbox-service",
            required=False,
        ),
        _input(ArtifactKind.STATIC_ANALYSIS_REPORT),
        _input(ArtifactKind.QUALITY_GATE_REPORT),
    ),
    "release-agent": (
        _input(ArtifactKind.INTEGRATED_SOURCE, required=False),
        _input(ArtifactKind.QUALITY_GATE_REPORT),
        _input(ArtifactKind.EVALUATION_REPORT),
        _input(ArtifactKind.HUMAN_RESPONSE, required=False),
    ),
}

_IDEA_AGENTS = {"intake-agent", "requirements-agent"}

AGENT_SPECS = tuple(
    AgentSpec(
        *role,
        inputs=_INPUTS[role[0]],
        include_idea=role[0] in _IDEA_AGENTS,
        cacheable=role[0] != "intake-agent",
    )
    for role in _ROLES
)
AGENTS_BY_STAGE = {
    stage: tuple(spec for spec in AGENT_SPECS if spec.stage == stage)
    for stage in StageName
    if stage != StageName.PREFLIGHT
}
