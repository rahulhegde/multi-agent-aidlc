from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx

from aidlc.a2a.server import create_agent_app
from aidlc.agents.analysis import with_mcp_analysis
from aidlc.agents.execution import with_sandbox_execution
from aidlc.domain.models import ArtifactKind
from aidlc.tools.models import SourceBundle
from tests.reference_agents import AGENTS_BY_STAGE


def reference_definitions(settings, groups=None):
    definitions = {
        item.name: item for group in (groups or AGENTS_BY_STAGE).values() for item in group
    }
    if not settings.human_gates_enabled:
        original = definitions["intake-agent"]

        async def intake(context):
            result = await original.handler(context)
            result.human_prompt = None
            return result

        definitions["intake-agent"] = replace(original, handler=intake)
    if settings.sandbox_execution_enabled:
        for name in ("build-agent", "validation-agent"):
            definitions[name] = with_sandbox_execution(definitions[name], settings)
    if settings.mcp_enabled:
        wrapped = with_mcp_analysis(definitions["static-analysis-agent"], settings)

        async def analyze(context):
            # This historical transport fixture has only one real source producer.
            artifacts = []
            for item in context.artifacts:
                if item["metadata"]["kind"] == ArtifactKind.CODE_CHANGE:
                    try:
                        SourceBundle.model_validate(item["content"])
                    except ValueError:
                        continue
                artifacts.append(item)
            return await wrapped.handler(context.model_copy(update={"artifacts": artifacts}))

        definitions["static-analysis-agent"] = replace(wrapped, handler=analyze)
    return definitions


@asynccontextmanager
async def reference_transport(settings):
    """Real SDK routes and wire serialization; only the network transport is in-process."""
    settings = replace(settings, human_gates_enabled=False)
    app = create_agent_app(settings, definitions=reference_definitions(settings))
    with patch(
        "aidlc.orchestration.workflow.WorkflowOrchestrator._integrate",
        lambda self, run, parents: parents,
    ):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                yield client
