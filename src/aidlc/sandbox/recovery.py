"""Persist trusted recovery evidence without advancing a lifecycle run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind, RunStatus, StageName
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase


def publish_recovery(settings: Settings, report: dict[str, Any]) -> dict[str, Any]:
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run_id = f"run_{uuid4().hex}"
    database.create_run(run_id, "Sandbox orphan recovery")
    database.update_run(run_id, RunStatus.BLOCKED, error="Standalone sandbox recovery evidence.")
    artifact = ArtifactStore(settings.artifact_dir).write(
        run_id=run_id,
        stage=StageName.PREFLIGHT,
        kind=ArtifactKind.SANDBOX_RECOVERY_REPORT,
        producing_agent="trusted-sandbox-recovery",
        content=report,
        markdown=f"# Sandbox recovery\n\n{report['status']}\n\n"
        f"Removed: {len(report['removed'])}; active: {len(report['active'])}; "
        f"failed: {len(report['failed'])}.",
    )
    database.add_artifact(artifact)
    database.append_event(
        run_id,
        "sandbox.recovered",
        {
            "artifact_id": artifact.metadata.artifact_id,
            "status": report["status"],
            "removed": len(report["removed"]),
            "active": len(report["active"]),
            "failed": len(report["failed"]),
        },
    )
    return {"run_id": run_id, "artifact_id": artifact.metadata.artifact_id, **report}


@asynccontextmanager
async def recovery_service(settings: Settings) -> AsyncIterator[None]:
    """Recover at startup and periodically, including delayed daemon creates."""
    from aidlc.sandbox.runner import DockerSandbox

    backend = DockerSandbox(settings)
    previous_status = "completed"

    async def recover() -> None:
        nonlocal previous_status
        report = await backend.reconcile()
        if report["removed"] or report["status"] != previous_status:
            await asyncio.to_thread(publish_recovery, settings, report)
        previous_status = report["status"]

    async def loop() -> None:
        while True:
            await asyncio.sleep(30)
            await recover()

    await recover()
    task = asyncio.create_task(loop(), name="sandbox-orphan-recovery")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
