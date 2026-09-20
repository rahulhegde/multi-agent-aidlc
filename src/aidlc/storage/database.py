from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from aidlc.domain.models import (
    STAGE_ORDER,
    ArtifactMetadata,
    ArtifactRecord,
    DelegatedTask,
    EventRecord,
    HumanAction,
    HumanInteraction,
    RunStatus,
    StageName,
    StageRecord,
    WorkflowRun,
    utc_now,
)
from aidlc.storage.artifacts import ArtifactStore, project_lock
from aidlc.storage.snapshots import invalidated_kinds


class WorkflowDatabase:
    """Small SQLite repository; SQL stays here instead of leaking into agents."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = threading.RLock()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    idea TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_stage TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS stages (
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT,
                    PRIMARY KEY (run_id, name)
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    stage TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    content_path TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, event_id);
                CREATE TABLE IF NOT EXISTS delegated_tasks (
                    invocation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    record_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS human_interactions (
                    surface_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    record_json TEXT NOT NULL
                );
                """
            )
            connection.commit()

    def save_interaction(self, interaction: HumanInteraction) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO human_interactions VALUES (?, ?, ?)",
                (interaction.surface_id, interaction.run_id, interaction.model_dump_json()),
            )
            connection.commit()

    def list_interactions(self, run_id: str) -> list[HumanInteraction]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM human_interactions WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [HumanInteraction.model_validate_json(row[0]) for row in rows]

    def accept_action(
        self,
        run_id: str,
        action: HumanAction,
        publish_response: Callable[[HumanInteraction], ArtifactRecord],
    ) -> tuple[HumanInteraction, bool]:
        # The transaction serializes duplicate and racing browser actions.
        with self._write_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT record_json FROM human_interactions WHERE surface_id = ? AND run_id = ?",
                (action.surface_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown surface")
            interaction = HumanInteraction.model_validate_json(row[0])
            payload = action.model_dump(mode="json")
            if interaction.response:
                if interaction.response["action"] == payload:
                    return interaction, False
                raise ValueError("Surface already answered; conflicting or stale action")
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run[0] != RunStatus.INPUT_REQUIRED:
                raise ValueError("Run is not waiting for human input")
            if (
                action.version != interaction.version
                or action.artifact_sha256 != interaction.artifact_sha256
            ):
                raise ValueError("Stale artifact or surface version")
            allowed = {
                "clarification": {"submit"},
                "approval": {"approve", "reject"},
                "evaluation_decision": {"accept", "repair"},
            }[interaction.prompt.kind]
            if action.decision not in allowed or (
                action.decision == "submit" and not action.answer
            ):
                raise ValueError("Invalid decision or empty clarification answer")
            interaction.response = {
                "action": payload,
                "actor": "local-learner",
                "answered_at": utc_now().isoformat(),
                "decision": action.decision,
                "answer": action.answer,
                "artifact_id": interaction.artifact_id,
                "artifact_sha256": interaction.artifact_sha256,
                "action_id": action.action_id,
            }
            # Publish the immutable file before committing the decision. A write
            # failure must leave the run waiting, not resume without evidence.
            self._insert_artifact(connection, publish_response(interaction))
            connection.execute(
                "UPDATE human_interactions SET record_json = ? WHERE surface_id = ?",
                (interaction.model_dump_json(), interaction.surface_id),
            )
            connection.commit()
            return interaction, True

    def create_run(self, run_id: str, idea: str) -> WorkflowRun:
        now = utc_now().isoformat()
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, NULL, ?, ?, NULL)",
                (run_id, idea, RunStatus.PENDING, now, now),
            )
            connection.executemany(
                "INSERT INTO stages VALUES (?, ?, ?, NULL, NULL, NULL)",
                [(run_id, stage, RunStatus.PENDING) for stage in STAGE_ORDER],
            )
            connection.commit()
        run = self.get_run(run_id)
        assert run is not None
        return run

    def recover_interrupted_runs(self) -> None:
        """Do not silently leave in-process work marked running after a restart."""
        for run in self.list_runs(limit=10_000):
            if run.status not in {RunStatus.PENDING, RunStatus.RUNNING, RunStatus.INPUT_REQUIRED}:
                continue
            reason = "Process restarted before completion; create a new run to retry."
            if run.current_stage is not None:
                self.update_stage(run.run_id, run.current_stage, RunStatus.BLOCKED, reason)
            self.update_run(run.run_id, RunStatus.BLOCKED, run.current_stage, reason)
            self.append_event(run.run_id, "run.interrupted", {"reason": reason})

    def save_delegated_task(self, task: DelegatedTask) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO delegated_tasks VALUES (?, ?, ?) "
                "ON CONFLICT(invocation_id) DO UPDATE SET record_json = excluded.record_json",
                (task.invocation_id, task.run_id, task.model_dump_json()),
            )
            connection.commit()

    def list_delegated_tasks(self, run_id: str) -> list[DelegatedTask]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT record_json FROM delegated_tasks WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [DelegatedTask.model_validate_json(row["record_json"]) for row in rows]

    def get_run(self, run_id: str) -> WorkflowRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            stages = connection.execute(
                "SELECT * FROM stages WHERE run_id = ?", (run_id,)
            ).fetchall()
        by_name = {StageName(item["name"]): self._stage_from_row(item) for item in stages}
        return WorkflowRun(
            run_id=row["run_id"],
            idea=row["idea"],
            status=RunStatus(row["status"]),
            current_stage=StageName(row["current_stage"]) if row["current_stage"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            error=row["error"],
            stages=[by_name[name] for name in STAGE_ORDER],
        )

    def list_runs(self, limit: int = 50) -> list[WorkflowRun]:
        with self._connect() as connection:
            ids = connection.execute(
                "SELECT run_id FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [run for row in ids if (run := self.get_run(row["run_id"])) is not None]

    def update_run(
        self,
        run_id: str,
        status: RunStatus,
        current_stage: StageName | None = None,
        error: str | None = None,
    ) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """UPDATE runs SET status = ?, current_stage = ?, updated_at = ?, error = ?
                   WHERE run_id = ?""",
                (status, current_stage, utc_now().isoformat(), error, run_id),
            )
            connection.commit()

    def update_stage(
        self,
        run_id: str,
        stage: StageName,
        status: RunStatus,
        error: str | None = None,
    ) -> None:
        now = utc_now().isoformat()
        started_at = now if status == RunStatus.RUNNING else None
        finished_at = (
            now
            if status
            in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELED,
                RunStatus.BLOCKED,
            }
            else None
        )
        with self._write_lock, self._connect() as connection:
            connection.execute(
                """UPDATE stages
                   SET status = ?,
                       started_at = COALESCE(started_at, ?),
                       finished_at = CASE WHEN ? IN ('running', 'input_required') THEN NULL
                                          ELSE COALESCE(?, finished_at) END,
                       error = ?
                   WHERE run_id = ? AND name = ?""",
                (status, started_at, status, finished_at, error, run_id, stage),
            )
            connection.commit()

    def add_artifact(self, artifact: ArtifactRecord) -> None:
        with self._write_lock, self._connect() as connection:
            # Keep lock order consistent with human decisions: SQLite before project files.
            connection.execute("BEGIN IMMEDIATE")
            if artifact.metadata.current_snapshot:
                project = next(
                    parent
                    for parent in Path(artifact.content_path).parents
                    if parent.name == artifact.metadata.project_id
                )
                with project_lock(project, shared=True):
                    # An out-of-order publisher must not index an already superseded snapshot.
                    ArtifactStore.read_content(artifact)
                    self._insert_artifact(connection, artifact)
                    connection.commit()
            else:
                self._insert_artifact(connection, artifact)
                connection.commit()

    @staticmethod
    def _insert_artifact(connection: sqlite3.Connection, artifact: ArtifactRecord) -> None:
        metadata = artifact.metadata
        if metadata.current_snapshot:
            rows = connection.execute(
                "SELECT artifact_id, metadata_json FROM artifacts WHERE run_id = ?",
                (metadata.workflow_run_id,),
            ).fetchall()
            invalidated = invalidated_kinds(metadata.kind)
            for row in rows:
                previous = ArtifactMetadata.model_validate_json(row["metadata_json"])
                same_slot = (
                    previous.kind == metadata.kind
                    and previous.producing_agent == metadata.producing_agent
                )
                if same_slot or (previous.current_snapshot and previous.kind in invalidated):
                    connection.execute(
                        "DELETE FROM artifacts WHERE artifact_id = ?", (row["artifact_id"],)
                    )
        connection.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                metadata.artifact_id,
                metadata.workflow_run_id,
                metadata.stage_id,
                metadata.kind,
                metadata.model_dump_json(),
                artifact.content_path,
                metadata.created_at.isoformat(),
            ),
        )

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT metadata_json, content_path FROM artifacts WHERE run_id = ? "
                "ORDER BY created_at",
                (run_id,),
            ).fetchall()
        return [
            ArtifactRecord(
                metadata=ArtifactMetadata.model_validate_json(row["metadata_json"]),
                content_path=row["content_path"],
            )
            for row in rows
        ]

    def get_artifact(self, artifact_id: str) -> ArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT metadata_json, content_path FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return ArtifactRecord(
            metadata=ArtifactMetadata.model_validate_json(row["metadata_json"]),
            content_path=row["content_path"],
        )

    def append_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int:
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(run_id, event_type, payload_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, event_type, json.dumps(payload), utc_now().isoformat()),
            )
            connection.commit()
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def events_after(self, run_id: str, event_id: int = 0) -> list[EventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id = ? AND event_id > ? ORDER BY event_id",
                (run_id, event_id),
            ).fetchall()
        return [
            EventRecord(
                event_id=row["event_id"],
                run_id=row["run_id"],
                event_type=row["event_type"],
                payload=json.loads(row["payload_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    @staticmethod
    def _stage_from_row(row: sqlite3.Row) -> StageRecord:
        return StageRecord(
            name=StageName(row["name"]),
            status=RunStatus(row["status"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
            error=row["error"],
        )
