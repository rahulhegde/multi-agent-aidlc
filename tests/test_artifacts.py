from pathlib import Path

import pytest

from aidlc.domain.models import ArtifactKind, StageName
from aidlc.storage.artifacts import ArtifactStore


def test_artifact_round_trip(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write(
        run_id="run_test",
        stage=StageName.INTAKE,
        kind=ArtifactKind.PROJECT_BRIEF,
        producing_agent="test-agent",
        content={"idea": "Build a tiny task list"},
        markdown="# Brief",
    )

    assert store.read_content(record) == {"idea": "Build a tiny task list"}
    assert len(record.metadata.content_sha256) == 64
    assert Path(record.content_path).is_relative_to(store.project_path("run_test"))
    assert record.metadata.current_snapshot

    (Path(record.content_path) / "content.json").write_text('{"idea":"tampered"}')
    with pytest.raises(ValueError, match="integrity"):
        store.read_content(record)


def test_release_bundle_materializes_relative_source_paths(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    record = store.write(
        run_id="run_release",
        stage=StageName.RELEASE,
        kind=ArtifactKind.RELEASE_BUNDLE,
        producing_agent="release-agent",
        content={
            "status": "released",
            "files": [
                {"path": "backend/api.py", "content": "print('ready')\n"},
                {"path": "ui/src/main.tsx", "content": "export default function App() {}\n"},
            ],
        },
        markdown="# Release",
    )

    source = Path(record.content_path) / "source"
    assert (source / "backend/api.py").read_text() == "print('ready')\n"
    assert (source / "ui/src/main.tsx").read_text() == "export default function App() {}\n"
    assert store.read_content(record)["files"][0]["path"] == "backend/api.py"


def test_replaced_release_bundle_removes_stale_materialized_files(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")

    def publish(path):
        return store.write(
            run_id="run_release",
            stage=StageName.RELEASE,
            kind=ArtifactKind.RELEASE_BUNDLE,
            producing_agent="release-agent",
            content={"status": "released", "files": [{"path": path, "content": "source\n"}]},
            markdown="# Release",
        )

    first = publish("backend/old.py")
    second = publish("backend/new.py")
    source = Path(second.content_path) / "source"
    assert first.content_path == second.content_path
    assert not (source / "backend/old.py").exists()
    assert (source / "backend/new.py").read_text() == "source\n"


def test_project_snapshots_replace_outputs_and_reject_old_identity(tmp_path):
    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(tmp_path / "db.sqlite3")
    database.initialize()
    run = database.create_run("run_project", "Build a task list")
    store = ArtifactStore(tmp_path / "artifacts")
    store.initialize_project(run.run_id, run.idea)

    def publish(run_id, producer, kind, content):
        record = store.write(
            run_id=run_id,
            stage=StageName.IMPLEMENTATION,
            kind=kind,
            producing_agent=producer,
            content=content,
            markdown="# Snapshot",
        )
        database.add_artifact(record)
        return record

    first = publish(run.run_id, "backend-agent", ArtifactKind.CODE_CHANGE, {"value": 1})
    frontend = publish(run.run_id, "frontend-agent", ArtifactKind.CODE_CHANGE, {"value": 2})
    second = publish(run.run_id, "backend-agent", ArtifactKind.CODE_CHANGE, {"value": 3})
    assert first.content_path == second.content_path
    assert first.metadata.artifact_id != second.metadata.artifact_id
    assert second.metadata.project_id == run.project_id
    assert store.read_content(second) == {"value": 3}
    assert store.read_content(frontend) == {"value": 2}
    assert database.get_artifact(first.metadata.artifact_id) is None
    assert len(database.list_artifacts(run.run_id)) == 2
    with pytest.raises(ValueError, match="superseded"):
        store.read_content(first)
    database.create_run("run_other", "Another project")
    other = publish("run_other", "backend-agent", ArtifactKind.CODE_CHANGE, {"value": 9})
    assert other.metadata.project_id != run.project_id
    assert store.read_content(second) == {"value": 3}


def test_source_replacement_invalidates_readiness_snapshots(tmp_path):
    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(tmp_path / "db.sqlite3")
    database.initialize()
    database.create_run("run_test", "A project")
    store = ArtifactStore(tmp_path / "artifacts")

    def publish(kind, producer):
        record = store.write(
            run_id="run_test",
            stage=StageName.INTEGRATION,
            kind=kind,
            producing_agent=producer,
            content={"status": "passed"},
            markdown="# Output",
        )
        database.add_artifact(record)
        return record

    publish(ArtifactKind.CODE_CHANGE, "backend-agent")
    old = [
        publish(ArtifactKind.INTEGRATED_SOURCE, "trusted-integration-service"),
        publish(ArtifactKind.BUILD_REPORT, "build-agent"),
        publish(ArtifactKind.QUALITY_GATE_REPORT, "quality-gates"),
        publish(ArtifactKind.EVALUATION_REPORT, "evaluation-agent"),
        publish(ArtifactKind.RELEASE_BUNDLE, "release-agent"),
    ]
    publish(ArtifactKind.CODE_CHANGE, "backend-agent")
    for record in old:
        assert not Path(record.content_path).exists()
        assert database.get_artifact(record.metadata.artifact_id) is None
    assert len(database.list_artifacts("run_test")) == 1


def test_project_identity_cannot_be_changed_in_context():
    from aidlc.domain.models import AgentContext

    with pytest.raises(ValueError, match="Project identity"):
        AgentContext(
            run_id="run_one", project_id="project_other", idea="Idea", stage=StageName.INTAKE
        )


def test_out_of_order_snapshot_cannot_restore_stale_database_entry(tmp_path):
    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(tmp_path / "db.sqlite3")
    database.initialize()
    database.create_run("run_test", "A project")
    store = ArtifactStore(tmp_path / "artifacts")

    def write(value):
        return store.write(
            run_id="run_test",
            stage=StageName.INTAKE,
            kind=ArtifactKind.PROJECT_BRIEF,
            producing_agent="intake-agent",
            content={"value": value},
            markdown="# Scope",
        )

    old = write(1)
    current = write(2)
    database.add_artifact(current)
    with pytest.raises(ValueError, match="superseded"):
        database.add_artifact(old)
    assert database.list_artifacts("run_test") == [current]


def test_parallel_spokes_keep_separate_current_outputs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from aidlc.storage.database import WorkflowDatabase

    database = WorkflowDatabase(tmp_path / "db.sqlite3")
    database.initialize()
    database.create_run("run_test", "A project")

    def publish(producer):
        store = ArtifactStore(tmp_path / "artifacts")
        for iteration in range(5):
            record = store.write(
                run_id="run_test",
                stage=StageName.IMPLEMENTATION,
                kind=ArtifactKind.CODE_CHANGE,
                producing_agent=producer,
                content={"producer": producer, "iteration": iteration},
                markdown="# Source",
            )
            database.add_artifact(record)

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(publish, ["backend-agent", "frontend-agent"]))
    records = database.list_artifacts("run_test")
    assert len(records) == 2
    assert {ArtifactStore.read_content(record)["producer"] for record in records} == {
        "backend-agent",
        "frontend-agent",
    }
    assert all(ArtifactStore.read_content(record)["iteration"] == 4 for record in records)
