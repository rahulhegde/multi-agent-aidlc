from __future__ import annotations

from aidlc.agents.base import AgentDefinition
from aidlc.agents.source import current_sources
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, AgentResult, ArtifactKind
from aidlc.tools.client import ToolsClient
from aidlc.tools.models import AnalysisRequest


def with_mcp_analysis(definition: AgentDefinition, settings: Settings) -> AgentDefinition:
    client = ToolsClient(settings)

    async def analyze(context: AgentContext) -> AgentResult:
        source_artifacts = current_sources(context)
        if not source_artifacts:
            raise ValueError("MCP analysis requires current executable source")
        reports = []
        for artifact in source_artifacts:
            metadata = artifact["metadata"]
            if metadata["workflow_run_id"] != context.run_id:
                raise ValueError("Analysis source belongs to a different run")
            report = await client.analyze(
                AnalysisRequest(
                    artifact_id=metadata["artifact_id"],
                    content_sha256=metadata["content_sha256"],
                    timeout_seconds=min(60, settings.analysis_timeout_seconds),
                )
            )
            reports.append(report.model_dump(mode="json"))
        review = await definition.handler(
            context.model_copy(
                update={
                    "repair_feedback": {
                        **(context.repair_feedback or {}),
                        "execution_evidence": reports,
                    }
                }
            )
        )
        content = {
            "status": "completed"
            if all(report["status"] == "completed" for report in reports)
            else "failed",
            "transport": "MCP Streamable HTTP",
            "reports": reports,
            "model_review": review.content,
        }
        return AgentResult(
            artifact_kind=ArtifactKind.STATIC_ANALYSIS_REPORT,
            content=content,
            markdown=review.markdown,
            execution=review.execution,
        )

    return AgentDefinition(definition.name, definition.stage, analyze)
