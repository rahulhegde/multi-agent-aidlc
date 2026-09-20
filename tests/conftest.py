"""Historical protocol fixtures explicitly disable external execution.

Live behavior is tested separately with injected models and execution transports.
"""

import pytest


@pytest.fixture(autouse=True)
def external_services_are_explicit(monkeypatch):
    monkeypatch.setenv("AIDLC_MCP_ENABLED", "false")
    monkeypatch.setenv("AIDLC_SANDBOX_EXECUTION_ENABLED", "false")
