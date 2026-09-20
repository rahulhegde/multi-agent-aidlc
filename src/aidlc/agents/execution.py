"""A2A build/test specialists use the trusted service for current source bundles."""

from aidlc.agents.base import AgentDefinition
from aidlc.agents.source import current_sources
from aidlc.config import Settings
from aidlc.domain.models import AgentContext, AgentResult, ArtifactKind
from aidlc.sandbox.cli import execute_artifact
from aidlc.tools.models import SourceBundle


def with_sandbox_execution(definition: AgentDefinition, settings: Settings) -> AgentDefinition:
    mode = "build" if definition.name == "build-agent" else "test"
    kind = ArtifactKind.BUILD_REPORT if mode == "build" else ArtifactKind.TEST_REPORT

    async def execute(context: AgentContext) -> AgentResult:
        sources = current_sources(context)
        if not sources:
            raise ValueError("No current executable source bundle")
        for source in sources:
            SourceBundle.model_validate(source["content"])
        reports = []
        for source in sources:
            metadata = source["metadata"]
            if metadata["workflow_run_id"] != context.run_id:
                raise ValueError("Execution source belongs to another run")
            reports.append(
                await execute_artifact(
                    settings,
                    metadata["artifact_id"],
                    metadata["content_sha256"],
                    mode,
                )
            )
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
            "report_artifact_ids": [report["artifact_id"] for report in reports],
            "model_review": review.content,
        }
        return AgentResult(
            artifact_kind=kind,
            content=content,
            markdown=review.markdown,
            execution=review.execution,
        )

    return AgentDefinition(definition.name, definition.stage, execute)
