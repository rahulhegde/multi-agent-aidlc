"""Spoke handler contract, independent of transport and model provider."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aidlc.domain.models import AgentContext, AgentResult, StageName

AgentHandler = Callable[[AgentContext], Awaitable[AgentResult]]


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    name: str
    stage: StageName
    handler: AgentHandler
