from __future__ import annotations

from typing import Any

from aidlc.agents.base import AgentDefinition, AgentHandler
from aidlc.domain.models import (
    AgentContext,
    AgentResult,
    ArtifactKind,
    HumanPrompt,
    ProjectBrief,
    RequirementsSpec,
    StageName,
)
from aidlc.evaluation.models import EvaluationReport


def _markdown(title: str, sections: dict[str, Any]) -> str:
    lines = [f"# {title}", "", "> Produced by a deterministic Milestone 1 agent.", ""]
    for heading, value in sections.items():
        lines.extend([f"## {heading.replace('_', ' ').title()}", ""])
        if isinstance(value, list):
            lines.extend(f"- {item}" for item in value)
        else:
            lines.append(str(value))
        lines.append("")
    return "\n".join(lines)


async def intake_agent(context: AgentContext) -> AgentResult:
    content = {
        "idea": context.idea,
        "target_user": (context.human_response or {}).get("answer") or "A user of the proposed MVP",
        "problem": context.idea,
        "assumptions": [
            "The one-line idea is the source of truth for this first pass.",
            "A human can refine assumptions through the interactive clarification gate.",
        ],
        "mvp_goal": f"Deliver the smallest testable product for: {context.idea}",
    }
    return AgentResult(
        artifact_kind=ArtifactKind.PROJECT_BRIEF,
        content=ProjectBrief.model_validate(content).model_dump(mode="json"),
        markdown=_markdown("Project brief", content),
        human_prompt=HumanPrompt(
            kind="clarification", question="Who is the primary user of this MVP?"
        )
        if not context.human_response
        else None,
    )


async def requirements_agent(context: AgentContext) -> AgentResult:
    content = {
        "product_name": "Generated MVP",
        "functional_requirements": [
            f"A user can complete the primary workflow described by: {context.idea}",
            "The system reports validation failures in plain language.",
        ],
        "non_functional_requirements": [
            "Core behavior has automated tests.",
            "Untrusted code executes only through the sandbox boundary.",
            "Workflow events and artifacts are traceable to their producing agent.",
        ],
        "acceptance_criteria": [
            "The primary workflow succeeds for a valid input.",
            "Invalid input produces an actionable error.",
            "The release report links all validation evidence.",
        ],
        "exclusions": ["Production deployment", "Billing", "Multi-region operation"],
        "open_questions": ["Which user persona should be prioritized first?"],
    }
    return AgentResult(
        artifact_kind=ArtifactKind.REQUIREMENTS_SPEC,
        content=RequirementsSpec.model_validate(content).model_dump(mode="json"),
        markdown=_markdown("Requirements specification", content),
    )


async def architecture_agent(context: AgentContext) -> AgentResult:
    content = {
        "decision": "Use a small API, browser UI, and isolated validation worker.",
        "components": ["React workbench", "FastAPI orchestrator", "artifact store", "sandbox"],
        "constraints": ["A2A-only agent communication", "MCP tools require authorization"],
        "trade_offs": ["Optimize for inspectability and learning over throughput."],
    }
    return AgentResult(
        artifact_kind=ArtifactKind.ARCHITECTURE_DECISION,
        content=content,
        markdown=_markdown("Architecture decision", content),
    )


async def ux_agent(context: AgentContext) -> AgentResult:
    content = {
        "journey": ["Enter idea", "Review progress", "Answer clarification", "Inspect artifacts"],
        "screens": ["Workbench", "Run detail", "Artifact viewer"],
        "a2ui_intent": "Render trusted, allow-listed interactive components from agent events.",
    }
    return AgentResult(
        artifact_kind=ArtifactKind.UX_SPECIFICATION,
        content=content,
        markdown=_markdown("UX specification", content),
    )


async def security_agent(context: AgentContext) -> AgentResult:
    content = {
        "assets": ["source code", "credentials", "workflow artifacts"],
        "threats": ["sandbox escape", "prompt injection", "unauthorized MCP invocation"],
        "controls": [
            "default-deny sandbox network",
            "short-lived scoped MCP identity",
            "A2UI component allow-list",
            "immutable audit events",
        ],
    }
    return AgentResult(
        artifact_kind=ArtifactKind.THREAT_MODEL,
        content=content,
        markdown=_markdown("Threat model", content),
    )


async def test_planner_agent(context: AgentContext) -> AgentResult:
    content = {
        "levels": ["unit", "contract", "integration", "end-to-end"],
        "critical_cases": ["happy path", "invalid input", "agent timeout", "sandbox denial"],
        "evidence": ["test report", "static analysis report", "evaluation report"],
    }
    return AgentResult(
        artifact_kind=ArtifactKind.TEST_PLAN,
        content=content,
        markdown=_markdown("Test plan", content),
    )


async def planning_agent(context: AgentContext) -> AgentResult:
    content = {
        "increments": [
            "Create the primary domain model and API.",
            "Implement the main browser workflow.",
            "Add validation and automated tests.",
        ],
        "dependency_order": ["domain", "backend API", "frontend", "validation"],
        "approval_required": True,
    }
    return AgentResult(
        artifact_kind=ArtifactKind.IMPLEMENTATION_PLAN,
        content=content,
        markdown=_markdown("Implementation plan", content),
    )


def _implementation_agent(area: str, files: list[str]) -> AgentHandler:
    async def run(context: AgentContext) -> AgentResult:
        content = {
            "area": area,
            "status": "proposed",
            "files": files,
            "note": (
                "Milestone 1 records a deterministic proposal; "
                "execution arrives with sandbox agents."
            ),
        }
        return AgentResult(
            artifact_kind=ArtifactKind.CODE_CHANGE,
            content=content,
            markdown=_markdown(f"{area.title()} code change", content),
        )

    return run


async def build_agent(context: AgentContext) -> AgentResult:
    content = {
        "status": "not_executed",
        "reason": "A sandbox executor is introduced after the deterministic workflow milestone.",
        "expected_command": "uv run pytest && pnpm build",
    }
    return AgentResult(
        artifact_kind=ArtifactKind.BUILD_REPORT,
        content=content,
        markdown=_markdown("Build report", content),
    )


async def test_agent(context: AgentContext) -> AgentResult:
    content = {
        "status": "not_executed",
        "planned_suites": ["unit", "API contract", "browser smoke"],
        "reason": "No generated project is executed in Milestone 1.",
    }
    return AgentResult(
        artifact_kind=ArtifactKind.TEST_REPORT,
        content=content,
        markdown=_markdown("Test report", content),
    )


async def static_analysis_agent(context: AgentContext) -> AgentResult:
    content = {
        "status": "not_executed",
        "planned_tools": ["ruff", "pyright", "eslint"],
        "reason": "This becomes an authenticated MCP sandbox tool in a later milestone.",
    }
    return AgentResult(
        artifact_kind=ArtifactKind.STATIC_ANALYSIS_REPORT,
        content=content,
        markdown=_markdown("Static analysis report", content),
    )


async def evaluation_agent(context: AgentContext) -> AgentResult:
    report = EvaluationReport(
        status="not_evaluated", summary="Scripted test fixture has no model judgment."
    )
    return AgentResult(
        artifact_kind=ArtifactKind.EVALUATION_REPORT,
        content=report.model_dump(mode="json"),
        markdown="# Scripted evaluation fixture",
    )


async def release_agent(context: AgentContext) -> AgentResult:
    content = {
        "status": "withheld",
        "reason": (
            "This workflow does not yet assemble a generated MVP. The Python gate profile "
            "and model review do not constitute full release qualification."
        ),
        "quality_gate_artifact_id": next(
            (
                item["metadata"]["artifact_id"]
                for item in reversed(context.artifacts)
                if item["metadata"]["kind"] == ArtifactKind.QUALITY_GATE_REPORT
            ),
            None,
        ),
        "evaluation_artifact_id": next(
            (
                item["metadata"]["artifact_id"]
                for item in reversed(context.artifacts)
                if item["metadata"]["kind"] == ArtifactKind.EVALUATION_REPORT
            ),
            None,
        ),
        "artifact_count": len(context.artifacts),
    }
    return AgentResult(
        artifact_kind=ArtifactKind.RELEASE_BUNDLE,
        content=content,
        markdown=_markdown("Release bundle", content),
    )


AGENTS_BY_STAGE: dict[StageName, tuple[AgentDefinition, ...]] = {
    StageName.INTAKE: (AgentDefinition("intake-agent", StageName.INTAKE, intake_agent),),
    StageName.REQUIREMENTS: (
        AgentDefinition("requirements-agent", StageName.REQUIREMENTS, requirements_agent),
    ),
    # Discovery specialists are intentionally independent and run in parallel.
    StageName.DISCOVERY: (
        AgentDefinition("architecture-agent", StageName.DISCOVERY, architecture_agent),
        AgentDefinition("ux-agent", StageName.DISCOVERY, ux_agent),
        AgentDefinition("security-agent", StageName.DISCOVERY, security_agent),
        AgentDefinition("test-planner-agent", StageName.DISCOVERY, test_planner_agent),
    ),
    StageName.PLANNING: (AgentDefinition("planning-agent", StageName.PLANNING, planning_agent),),
    # Backend, frontend, and tests demonstrate a second fan-out/fan-in boundary.
    StageName.IMPLEMENTATION: (
        AgentDefinition(
            "backend-agent",
            StageName.IMPLEMENTATION,
            _implementation_agent("backend", ["src/api.py", "tests/test_api.py"]),
        ),
        AgentDefinition(
            "frontend-agent",
            StageName.IMPLEMENTATION,
            _implementation_agent("frontend", ["ui/src/App.tsx"]),
        ),
        AgentDefinition(
            "test-agent",
            StageName.IMPLEMENTATION,
            _implementation_agent("tests", ["tests/test_acceptance.py"]),
        ),
    ),
    StageName.INTEGRATION: (
        AgentDefinition("build-agent", StageName.INTEGRATION, build_agent),
        AgentDefinition("validation-agent", StageName.INTEGRATION, test_agent),
        AgentDefinition("static-analysis-agent", StageName.INTEGRATION, static_analysis_agent),
    ),
    StageName.EVALUATION: (
        AgentDefinition("evaluation-agent", StageName.EVALUATION, evaluation_agent),
    ),
    StageName.RELEASE: (AgentDefinition("release-agent", StageName.RELEASE, release_agent),),
}
