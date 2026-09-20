"""Validated outputs and role instructions for all reasoning spokes."""

from pydantic import BaseModel, ConfigDict, Field

from aidlc.tools.models import SourceBundle


class ArchitectureDecision(BaseModel):
    decision: str
    components: list[str]
    constraints: list[str]
    trade_offs: list[str]


class UxSpecification(BaseModel):
    journey: list[str]
    screens: list[str]
    accessibility: list[str]


class ThreatModel(BaseModel):
    assets: list[str]
    threats: list[str]
    controls: list[str]


class TestPlan(BaseModel):
    levels: list[str]
    critical_cases: list[str]
    requirement_coverage: list[str]


class ImplementationPlan(BaseModel):
    backend_tasks: list[str]
    frontend_tasks: list[str]
    test_tasks: list[str]
    shared_contracts: list[str]
    dependency_order: list[str]
    completion_criteria: list[str]


class SpecialistOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    markdown: str


class ArchitectureOutput(SpecialistOutput):
    content: ArchitectureDecision


class UxOutput(SpecialistOutput):
    content: UxSpecification


class SecurityOutput(SpecialistOutput):
    content: ThreatModel


class TestPlanOutput(SpecialistOutput):
    content: TestPlan


class PlanningOutput(SpecialistOutput):
    content: ImplementationPlan


class SourceOutput(SourceBundle):
    """Implementation model output contains only file paths and complete source text."""


class ExecutionReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: dict[str, str | list[str]]
    markdown: str


class ReleaseNotes(BaseModel):
    run_instructions: list[str] = Field(min_length=1)
    architecture_summary: str
    known_limitations: list[str]


class ReleaseOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: ReleaseNotes
    markdown: str


# Versioned goal contracts are consumed by runtime prompts and documented in docs/agent-goals.md.
GOAL_VERSION = "aidlc-agent-goals-v4"

LEARNING_OUTPUT_POLICY = (
    "This is a learning project. Return the smallest useful deliverable for your role. "
    "Use 1-3 short, representative items per list by default; lists need not be exhaustive. "
    "Include more only when needed to satisfy explicit approved requirements, required schema "
    "fields, or mandatory quality gates. Keep prose to short sentences and markdown to a brief "
    "summary; do not duplicate JSON lists or source code in markdown. Avoid speculative features, "
    "alternative designs, lengthy background, and production-scale infrastructure. Planning and "
    "design should describe one simple approach. Implementation should produce the fewest complete "
    "runnable files allowed by role ownership and the pinned toolchain; never truncate code or use "
    "ellipsis placeholders. Tests should cover a few representative observable behaviors and "
    "required acceptance criteria. Reviews should report actionable findings with brief evidence; "
    "evaluation must still include every rubric judgment. Release should give only essential "
    "run steps and material limitations. Preserve required structured output and human approvals."
)

GOAL_CONTRACTS = {
    "intake-agent": (
        "Turn the original idea and any human answer into a bounded project brief.",
        "Original idea, supplied human response, and any existing project brief.",
        "ProjectBrief: idea, target_user, problem, assumptions, and mvp_goal; readable markdown.",
        "Identify the user and problem, distinguish assumptions from facts, and define an "
        "achievable MVP. "
        "Ask at most one material clarification using the clarification field; use an existing "
        "answer.",
        "LLM reasoning only; no source changes, execution, or MCP analysis is needed.",
    ),
    "requirements-agent": (
        "Translate the brief into verifiable, internally consistent MVP requirements.",
        "Project brief, original idea, and supplied human decisions.",
        "RequirementsSpec in the required schema, plus readable markdown.",
        "Define observable acceptance criteria, scope, and constraints. Preserve the brief's goal; "
        "make unresolved assumptions explicit rather than silently expanding scope.",
        "LLM reasoning only; no source changes or execution.",
    ),
    "architecture-agent": (
        "Choose the smallest architecture that satisfies approved requirements and runtime "
        "constraints.",
        "Project brief, requirements specification, and relevant repair feedback.",
        "ArchitectureDecision: decision, components, constraints, and trade_offs.",
        "Explain component responsibilities, interfaces, and concrete alternatives/tradeoffs. "
        "Respect the standard-library Python backend and pinned React toolchain; avoid "
        "unsupported services.",
        "LLM reasoning only; produce design, not code or execution evidence.",
    ),
    "ux-agent": (
        "Define usable interface behavior for the required user journeys.",
        "Project brief and requirements specification.",
        "UxSpecification: journey, screens, and accessibility.",
        "Cover primary journeys, input validation, loading/error/empty/success behavior, and "
        "keyboard "
        "and accessible-label expectations. Tie screens to requirements without adding "
        "unapproved scope.",
        "LLM reasoning only; do not implement UI source or claim browser validation.",
    ),
    "security-agent": (
        "Identify project-specific risks and actionable controls for the proposed MVP.",
        "Project brief and requirements specification; supplied design artifacts if available.",
        "ThreatModel: assets, threats, and controls.",
        "Relate threats to assets and trust boundaries, prioritize controls, and describe "
        "residual risks. "
        "Distinguish proposed controls from implemented or verified controls.",
        "LLM threat modeling only; no vulnerability scanner is exposed to this role.",
    ),
    "test-planner-agent": (
        "Turn acceptance criteria into a feasible validation strategy.",
        "Requirements specification and project brief; supplied discovery artifacts if available.",
        "TestPlan: levels, critical_cases, and requirement_coverage.",
        "Map each acceptance criterion to concrete cases including negative/boundary behavior. "
        "Distinguish planned tests from executed tests; mark browser/integration checks as "
        "deferred "
        "where the current validation environment cannot run them.",
        "LLM planning only; no test execution or source changes.",
    ),
    "planning-agent": (
        "Produce an executable dependency-aware plan that reconciles discovery into shared "
        "contracts.",
        "Requirements, architecture, UX, threat model, and test plan.",
        "ImplementationPlan: backend_tasks, frontend_tasks, test_tasks, shared_contracts, "
        "dependency_order, and completion_criteria.",
        "Specify exact API routes, methods, payloads, response/error shapes, and ownership. "
        "Plan the backend entirely in backend/api.py, with no additional backend modules. "
        "Resolve design inconsistencies explicitly and define verifiable completion criteria. "
        "Parallel implementation roles must be able to work from this same plan.",
        "LLM planning only; no implementation or execution.",
    ),
    "backend-agent": (
        "Deliver complete backend source satisfying assigned requirements and shared API "
        "contracts.",
        "Approved requirements, architecture, implementation plan, current source, and repair "
        "feedback.",
        "SourceOutput: files containing exactly backend/api.py and its complete source text.",
        "Implement routes, validation, errors, and business behavior with the standard library. "
        "Put all backend code in exactly one file: backend/api.py. "
        "Do not create backend/__init__.py or additional backend modules. "
        "Keep python -m backend.api runnable at 127.0.0.1:8081 using a namespace package. "
        "Preserve shared contracts during repair; never modify ui/ or tests/.",
        "LLM source generation. Artifact-bound analyze_code MCP is available only when input "
        "source "
        "exists; it analyzes that input, not newly drafted files. No host shell or sandbox "
        "execution.",
    ),
    "frontend-agent": (
        "Deliver a complete React TypeScript UI implementing the approved journeys and API "
        "contracts.",
        "Requirements, UX, architecture, implementation plan, current source, and repair feedback.",
        "SourceOutput: files of complete ui/ source including index.html, src/main.tsx, and "
        "tsconfig.json.",
        "Use the pinned toolchain and relative /api URLs. Implement required interaction and "
        "accessible feedback states. Do not supply custom Vite configuration or a lockfile; "
        "any package.json must exactly match the trusted toolchain. Modify only ui/.",
        "LLM source generation; input-artifact MCP analysis if exposed. No host execution or "
        "browser "
        "testing. Trusted sandbox services perform the subsequent TypeScript and Vite build.",
    ),
    "test-agent": (
        "Deliver meaningful backend tests that demonstrate acceptance behavior and detect "
        "regressions.",
        "Requirements, test plan, shared API/module contracts, available source, and repair "
        "feedback.",
        "SourceOutput: files of complete tests/ Python source including tests/__init__.py.",
        "Use standard-library unittest and approved imports. Assert behavior, edge cases, and "
        "failures; "
        "avoid placeholder assertions. Respect parallel execution: use shared contracts when "
        "sibling "
        "source is not yet supplied. Modify only tests/; UI checks currently cover its build.",
        "LLM test generation; input-artifact MCP analysis if exposed. Trusted validation "
        "executes tests "
        "after integration; this role must not claim its generated tests have passed.",
    ),
    "build-agent": (
        "Explain whether the current integrated source builds, using actual trusted execution "
        "evidence.",
        "Current integrated source and repair_feedback.execution_evidence from the sandbox "
        "wrapper.",
        "ExecutionReviewOutput plus markdown; the wrapper supplies authoritative status/report "
        "IDs.",
        "Identify failed steps, affected files, and bounded repairs from actual reports. Explain "
        "Python "
        "compilation and React TypeScript/Vite results separately. Do not replace measured status "
        "or infer success from source inspection.",
        "The trusted wrapper must run sandbox build before LLM review. No model-selected shell "
        "commands "
        "or dependency scripts; do not modify source or gate results.",
    ),
    "validation-agent": (
        "Assess actual test and UI-build evidence against planned acceptance coverage.",
        "Requirements, test plan, current integrated source, and sandbox execution_evidence.",
        "ExecutionReviewOutput plus markdown; the wrapper supplies authoritative status/report "
        "IDs.",
        "Report executed test outcomes, failures, and unverified coverage. UI build success does "
        "not "
        "prove browser interactions, usability, or end-to-end behavior. Recommend targeted repairs "
        "without inventing test counts or overriding trusted reports.",
        "The trusted wrapper must run sandbox tests and UI build before LLM review. No source "
        "changes "
        "or arbitrary execution; browser/integration testing is outside current scope.",
    ),
    "static-analysis-agent": (
        "Explain authenticated MCP analysis findings for the current source and recommend repairs.",
        "Current source and execution_evidence returned by the authenticated MCP analysis wrapper.",
        "ExecutionReviewOutput plus markdown; the wrapper retains authoritative MCP "
        "reports/status.",
        "Identify concrete findings and affected files, distinguish tool failures from clean "
        "analysis, "
        "and explain the limited scope of the configured analyzer. Do not claim a security audit "
        "or alter measured findings.",
        "The trusted wrapper must call MCP analysis before LLM review. This role reviews results; "
        "it cannot edit source, bypass authentication, or run arbitrary tools.",
    ),
    "evaluation-agent": (
        "Judge MVP readiness against requirements and six rubrics using current source and "
        "evidence.",
        "Original idea, requirements/design, current source, quality gates, execution reports, "
        "and the current repair attempt.",
        "EvaluationReport with six scores and evidence IDs, summary, recommended_repairs, and "
        "markdown. The trusted wrapper attaches the deterministic quality-gate report.",
        "Judge requirement_coverage, mvp_completeness, usability, architecture, maintainability, "
        "and risk_acceptance with concrete rationale and existing input artifact IDs. Determine "
        "applicability only from the approved requirements: generated designs or source do not "
        "create new evaluation requirements. For an applicable rubric, provide a 0-to-1 score. "
        "For a rubric not requested or implied by the requirements, set applicability to "
        "not_applicable, set score to null, cite the requirements artifact, and explain why. "
        "Exclude out-of-scope implementation features (for example, an unrequested UI) from all "
        "scores; mention them only as non-scoring observations or risks. "
        "When evidence is insufficient return not_evaluated and null scores. Recommend bounded "
        "repairs and never alter deterministic gates or claim deferred checks were executed. "
        "The human reviewer makes the final accept-or-repair decision.",
        "LLM evidence review; artifact-bound analyze_code MCP is available for current source. "
        "No source changes, sandbox command selection, or authority to approve release.",
    ),
    "release-agent": (
        "Prepare reproducible handoff notes for the exact source accepted by gates and evaluation.",
        "Latest integrated source, quality gates, evaluation, architecture, and "
        "execution evidence.",
        "ReleaseNotes: run_instructions, architecture_summary, known_limitations, and markdown; "
        "the trusted release assembler adds exact source files and evidence references.",
        "Describe backend/UI startup and trusted dependencies accurately. State deferred checks "
        "and "
        "known limitations. Without explicit human acceptance, release assembly requires passing "
        "gates and the configured evaluation threshold. Do not regenerate source, fabricate "
        "acceptance, or claim deployment.",
        "LLM handoff writing only. Trusted code assembles source and pinned assets and enforces "
        "release "
        "eligibility; no deployment or external publication tools are exposed.",
    ),
}


def goal_instructions(name: str) -> str:
    """Render the same explicit role contract used by runtime and documentation."""
    objective, inputs, output, completion, tools = GOAL_CONTRACTS[name]
    return "\n".join(
        f"{label}: {value}"
        for label, value in (
            ("Goal version", GOAL_VERSION),
            ("Learning output limits", LEARNING_OUTPUT_POLICY),
            ("Objective", objective),
            ("Inputs", inputs),
            ("Deliverables", output),
            ("Completion criteria and boundaries", completion),
            ("Tools and execution ownership", tools),
        )
    )
