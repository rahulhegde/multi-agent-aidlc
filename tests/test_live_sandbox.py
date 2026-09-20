"""Opt-in verification of Python tests and React builds in the real OCI sandbox."""

import asyncio
import os

import pytest

from aidlc.config import Settings
from aidlc.sandbox.models import SandboxJob
from aidlc.sandbox.runner import DockerSandbox
from aidlc.tools.models import SourceBundle
from tests.test_live import source_files


@pytest.mark.skipif(
    os.getenv("AIDLC_TEST_SANDBOX") != "true", reason="Requires local Docker sandbox image"
)
@pytest.mark.parametrize(
    "mode,broken,expected",
    [("build", False, "completed"), ("test", False, "completed"), ("build", True, "failed")],
)
def test_real_python_react_sandbox(mode, broken, expected):
    files = sum(
        (source_files(role) for role in ("backend-agent", "frontend-agent", "test-agent")), []
    )
    if broken:
        next(file for file in files if file["path"] == "ui/src/main.tsx")["content"] += (
            '\nconst broken: number = "wrong";\n'
        )
    result = asyncio.run(
        DockerSandbox(Settings()).execute(
            SandboxJob(
                source=SourceBundle.model_validate({"files": files}),
                mode=mode,
            )
        )
    )
    assert result.status == expected, result.model_dump()
    assert result.execution.policy.network == "none"
    assert result.execution.cleanup_succeeded
    if not broken:
        assert "React dependency toolchain and production build passed" in result.stdout
    if mode == "test":
        assert "Ran 1 test in" in result.stderr
