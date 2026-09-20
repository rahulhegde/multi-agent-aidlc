"""Publish deterministic evaluation without advancing workflow or release state."""

import argparse
import json
from pathlib import Path
from typing import Any

from aidlc.config import Settings
from aidlc.domain.models import AgentContext, ArtifactKind, StageName
from aidlc.evaluation.gates import evaluate_gates, gates_markdown
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase


def evaluate_run(settings: Settings, run_id: str) -> dict[str, Any]:
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run = database.get_run(run_id)
    if run is None:
        raise ValueError("Unknown workflow run")
    records = database.list_artifacts(run_id)
    artifacts = []
    for record in records:
        path = (Path(record.content_path) / "content.json").resolve()
        if (
            not path.is_relative_to(settings.artifact_dir.resolve())
            or path.stat().st_size > 1_048_576
        ):
            raise ValueError("Artifact containment or size mismatch")
        artifacts.append(
            {
                "metadata": record.metadata.model_dump(mode="json"),
                "content": ArtifactStore.read_content(record),
            }
        )
    report = evaluate_gates(
        AgentContext(run_id=run_id, idea=run.idea, stage=StageName.EVALUATION, artifacts=artifacts),
        settings.sandbox_backend,
    )
    artifact = ArtifactStore(settings.artifact_dir).write(
        run_id=run_id,
        stage=StageName.EVALUATION,
        kind=ArtifactKind.QUALITY_GATE_REPORT,
        producing_agent="deterministic-quality-gates",
        content=report.model_dump(mode="json"),
        markdown=gates_markdown(report),
        parent_artifact_ids=[item.metadata.artifact_id for item in records],
        prompt_version=report.profile,
    )
    database.add_artifact(artifact)
    database.append_event(
        run_id,
        "quality_gates.completed",
        {
            "artifact_id": artifact.metadata.artifact_id,
            "verdict": report.verdict,
        },
    )
    return {"artifact_id": artifact.metadata.artifact_id, **report.model_dump(mode="json")}


def run() -> None:
    parser = argparse.ArgumentParser(description="Evaluate immutable Python execution evidence")
    parser.add_argument("run_id")
    arguments = parser.parse_args()
    try:
        result = evaluate_run(Settings(), arguments.run_id)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))
    if result["verdict"] != "passed":
        raise SystemExit(1)
