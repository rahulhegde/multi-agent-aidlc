"""Publish an explicitly labeled source fixture; this is not a generated MVP."""

import json
from uuid import uuid4

from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind, RunStatus, StageName
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase

settings = Settings()
database = WorkflowDatabase(settings.database_path)
database.initialize()
run_id = f"run_{uuid4().hex}"
database.create_run(run_id, "Authenticated static-analysis example fixture")
database.update_run(
    run_id, RunStatus.BLOCKED, error="Standalone MCP example; no lifecycle execution was requested."
)
artifact = ArtifactStore(settings.artifact_dir).write(
    run_id=run_id,
    stage=StageName.IMPLEMENTATION,
    kind=ArtifactKind.CODE_CHANGE,
    producing_agent="explicit-analysis-example",
    content={
        "files": [
            {"path": "src/example.py", "content": "import os\n\ndef answer():\n    return 42\n"}
        ]
    },
    markdown=(
        "# Static-analysis example\n\nExplicit fixture with an unused import for Ruff to detect."
    ),
)
database.add_artifact(artifact)
print(
    json.dumps(
        {
            "run_id": run_id,
            "artifact_id": artifact.metadata.artifact_id,
            "content_sha256": artifact.metadata.content_sha256,
        },
        indent=2,
    )
)
