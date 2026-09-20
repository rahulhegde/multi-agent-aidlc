from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any
from uuid import uuid4

from aidlc.domain.models import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRecord,
    StageName,
    project_id_for_run,
)
from aidlc.storage.snapshots import SNAPSHOT_KINDS, invalidated_kinds
from aidlc.tools.models import SourceBundle


@contextmanager
def project_lock(project: Path, *, shared: bool = False):
    project.mkdir(parents=True, exist_ok=True)
    with (project / ".storage.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_text(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ArtifactStore:
    """Project-owned current snapshots and separate execution/decision records."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def project_path(self, run_id: str) -> Path:
        return self.root / "projects" / project_id_for_run(run_id)

    def initialize_project(self, run_id: str, idea: str) -> None:
        project = self.project_path(run_id)
        with project_lock(project):
            atomic_text(
                project / "project.json",
                json.dumps(
                    {"project_id": project_id_for_run(run_id), "run_id": run_id, "idea": idea},
                    indent=2,
                ),
            )

    def write(
        self,
        *,
        run_id: str,
        stage: StageName,
        kind: ArtifactKind,
        producing_agent: str,
        content: dict[str, Any],
        markdown: str,
        parent_artifact_ids: list[str] | None = None,
        model_id: str | None = None,
        prompt_version: str = "deterministic-v1",
        execution: dict[str, Any] | None = None,
    ) -> ArtifactRecord:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", producing_agent):
            raise ValueError("Unsafe artifact producer")
        release_source = (
            SourceBundle.model_validate({"files": content["files"]})
            if kind == ArtifactKind.RELEASE_BUNDLE and "files" in content
            else None
        )
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
        snapshot = kind in SNAPSHOT_KINDS and producing_agent != "trusted-sandbox-service"
        artifact_id = f"art_{uuid4().hex}"
        metadata = ArtifactMetadata(
            artifact_id=artifact_id,
            current_snapshot=snapshot,
            kind=kind,
            workflow_run_id=run_id,
            stage_id=stage,
            parent_artifact_ids=parent_artifact_ids or [],
            producing_agent=producing_agent,
            model_id=model_id,
            prompt_version=prompt_version,
            execution=execution or {},
            content_sha256=hashlib.sha256(canonical).hexdigest(),
        )
        project = self.project_path(run_id)
        destination = (
            project / "current" / stage / producing_agent / kind
            if snapshot
            else project / "records" / stage / artifact_id
        )
        with project_lock(project):
            if snapshot:
                invalidated = invalidated_kinds(kind)
                for manifest in (project / "current").glob("*/*/*/manifest.json"):
                    previous = ArtifactMetadata.model_validate_json(manifest.read_text())
                    if previous.kind in invalidated:
                        shutil.rmtree(manifest.parent)
            destination.mkdir(parents=True, exist_ok=True)
            # Readers take the same project lock: the three-file snapshot is published as a unit.
            atomic_text(destination / "content.json", json.dumps(content, indent=2, sort_keys=True))
            atomic_text(destination / "content.md", markdown.rstrip() + "\n")
            atomic_text(destination / "manifest.json", metadata.model_dump_json(indent=2))
            if release_source is not None:
                temporary_source = destination / f".source-{uuid4().hex}"
                published_source = destination / "source"
                try:
                    for source_file in release_source.files:
                        target = temporary_source.joinpath(*source_file.path.split("/"))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        atomic_text(target, source_file.content)
                    shutil.rmtree(published_source, ignore_errors=True)
                    os.replace(temporary_source, published_source)
                finally:
                    shutil.rmtree(temporary_source, ignore_errors=True)
            if kind == ArtifactKind.PROJECT_BRIEF:
                descriptor = project / "project.json"
                identity = (
                    json.loads(descriptor.read_text())
                    if descriptor.exists()
                    else {"project_id": metadata.project_id, "run_id": run_id}
                )
                identity.update(
                    scope_artifact_id=artifact_id,
                    scope_path=str(destination.relative_to(project)),
                    scope_sha256=metadata.content_sha256,
                )
                atomic_text(descriptor, json.dumps(identity, indent=2))
        return ArtifactRecord(metadata=metadata, content_path=str(destination))

    @staticmethod
    def read_content(record: ArtifactRecord) -> dict[str, Any]:
        destination = Path(record.content_path)
        project = next(
            (parent for parent in destination.parents if parent.name == record.metadata.project_id),
            None,
        )
        lock = project_lock(project, shared=True) if project else nullcontext()
        with lock:
            manifest = destination / "manifest.json"
            if manifest.exists():
                stored = ArtifactMetadata.model_validate_json(manifest.read_text())
                if stored.artifact_id != record.metadata.artifact_id:
                    raise ValueError(f"Artifact snapshot superseded: {record.metadata.artifact_id}")
            content = json.loads((destination / "content.json").read_text(encoding="utf-8"))
            canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(canonical).hexdigest() != record.metadata.content_sha256:
                raise ValueError(f"Artifact integrity check failed: {record.metadata.artifact_id}")
            return content
