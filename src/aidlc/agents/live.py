"""The runnable fleet has only live, evidence-producing specialist implementations."""

from aidlc.agents.analysis import with_mcp_analysis
from aidlc.agents.catalog import AGENT_SPECS
from aidlc.agents.deep import migrate
from aidlc.agents.execution import with_sandbox_execution
from aidlc.config import Settings


def create_definitions(settings: Settings) -> dict:
    if settings.agent_mode != "deep":
        raise ValueError("Reference mode is removed; AIDLC_AGENT_MODE must be deep")
    if not settings.model or settings.model == "provider:model":
        raise ValueError("Set AIDLC_MODEL and provider credentials in the shared .env file")
    if not settings.mcp_enabled or not settings.sandbox_execution_enabled:
        raise ValueError("The learning workflow requires MCP and sandbox execution enabled")
    definitions = {spec.name: migrate(spec, settings) for spec in AGENT_SPECS}
    for name in ("build-agent", "validation-agent"):
        definitions[name] = with_sandbox_execution(definitions[name], settings)
    definitions["static-analysis-agent"] = with_mcp_analysis(
        definitions["static-analysis-agent"], settings
    )
    return definitions
