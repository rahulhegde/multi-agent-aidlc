"""Trusted integration, source ownership, and artifact-bound model tools."""

import json
from pathlib import Path

from langchain_core.tools import tool

from aidlc.config import Settings
from aidlc.domain.models import AgentContext, ArtifactKind, StageName
from aidlc.evaluation.models import EvaluationReport, QualityGateReport
from aidlc.tools.client import ToolsClient
from aidlc.tools.models import AnalysisRequest, SourceBundle

REACT_ASSETS = Path(__file__).resolve().parents[3] / "sandbox" / "react"


def react_dependencies() -> dict:
    return json.loads((REACT_ASSETS / "package.json").read_text())


def current_sources(context: AgentContext) -> list[dict]:
    integrated = [
        item
        for item in context.artifacts
        if item["metadata"]["kind"] == ArtifactKind.INTEGRATED_SOURCE
    ]
    if integrated:
        return integrated[-1:]
    latest = {}
    for item in context.artifacts:
        if item["metadata"]["kind"] == ArtifactKind.CODE_CHANGE:
            latest[item["metadata"]["producing_agent"]] = item
    return list(latest.values())


def validate_source_output(agent: str, source: SourceBundle) -> None:
    prefix = {"backend-agent": "backend/", "frontend-agent": "ui/", "test-agent": "tests/"}[agent]
    if any(not file.path.startswith(prefix) for file in source.files):
        raise ValueError(f"{agent} may write only {prefix}")
    if agent != "frontend-agent" and any(not file.path.endswith(".py") for file in source.files):
        raise ValueError("Backend and test source must use standard-library Python")
    if agent == "frontend-agent":
        paths = {file.path for file in source.files}
        if not {"ui/index.html", "ui/src/main.tsx", "ui/tsconfig.json"} <= paths:
            raise ValueError("Frontend must include index.html, src/main.tsx and tsconfig.json")
        if any(Path(file.path).name.startswith("vite.config") for file in source.files):
            raise ValueError("Vite configuration is supplied by the trusted toolchain")
        for file in source.files:
            if file.path == "ui/package.json" and json.loads(file.content) != react_dependencies():
                raise ValueError(
                    "Frontend dependencies and scripts must match the pinned toolchain"
                )
            if file.path == "ui/package-lock.json":
                raise ValueError("The dependency lockfile is supplied by the trusted toolchain")


def merge_sources(context: AgentContext) -> SourceBundle:
    latest = {}
    for item in context.artifacts:
        if item["metadata"]["kind"] == ArtifactKind.CODE_CHANGE:
            latest[item["metadata"]["producing_agent"]] = item
    if set(latest) != {"backend-agent", "frontend-agent", "test-agent"}:
        raise ValueError("Integration requires source from all three implementation agents")
    files = []
    for name in ("backend-agent", "frontend-agent", "test-agent"):
        source = SourceBundle.model_validate(latest[name]["content"])
        validate_source_output(name, source)
        files.extend(source.files)
    return SourceBundle(files=tuple(files))


def mcp_tools(settings: Settings, context: AgentContext, definition) -> list:
    if definition.stage not in {StageName.IMPLEMENTATION, StageName.EVALUATION}:
        return []
    sources = {item["metadata"]["artifact_id"]: item for item in current_sources(context)}

    @tool
    async def analyze_code(artifact_id: str) -> dict:
        """Analyze a current hash-verified source snapshot through authenticated MCP."""
        if artifact_id not in sources:
            raise ValueError("Analysis requires an input artifact belonging to this invocation")
        item = sources[artifact_id]
        if item["metadata"]["workflow_run_id"] != context.run_id:
            raise ValueError("Analysis source belongs to another run")
        report = await ToolsClient(settings).analyze(
            AnalysisRequest(
                artifact_id=artifact_id, content_sha256=item["metadata"]["content_sha256"]
            )
        )
        return report.model_dump(mode="json")

    return [analyze_code] if settings.mcp_enabled and sources else []


def release_bundle(context: AgentContext, notes: dict, settings: Settings) -> dict:
    def latest(kind):
        return next(
            item for item in reversed(context.artifacts) if item["metadata"]["kind"] == kind
        )

    gate = latest(ArtifactKind.QUALITY_GATE_REPORT)
    review = latest(ArtifactKind.EVALUATION_REPORT)
    gates = QualityGateReport.model_validate(gate["content"])
    evaluation = EvaluationReport.model_validate(review["content"])
    score = evaluation.overall_score()
    human_acceptance = any(
        item["metadata"]["kind"] == ArtifactKind.HUMAN_RESPONSE
        and item["content"].get("decision") == "accept"
        and item["content"].get("artifact_sha256") == review["metadata"]["content_sha256"]
        for item in context.artifacts
    )
    if not human_acceptance and (
        gates.verdict != "passed"
        or score is None
        or score < settings.evaluation_score_threshold
    ):
        raise ValueError("Release requires passing execution evidence and model evaluation")
    source = latest(ArtifactKind.INTEGRATED_SOURCE)
    files = list(SourceBundle.model_validate(source["content"]).model_dump(mode="json")["files"])
    # Reproducible dependencies/configuration come from trusted image inputs, never model guesses.
    files = [file for file in files if file["path"] not in {"ui/package.json", "ui/tsconfig.json"}]
    for name in ("package.json", "package-lock.json", "vite.config.mjs", "tsconfig.json"):
        files.append({"path": f"ui/{name}", "content": (REACT_ASSETS / name).read_text()})
    return {
        "status": "released",
        **notes,
        "files": files,
        "source_artifact_id": source["metadata"]["artifact_id"],
        "quality_gate_artifact_id": gate["metadata"]["artifact_id"],
        "evaluation_artifact_id": review["metadata"]["artifact_id"],
        "evidence_artifact_ids": [item["metadata"]["artifact_id"] for item in context.artifacts],
    }
