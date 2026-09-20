from aidlc.domain.models import RunStatus, StageName
from aidlc.storage.database import WorkflowDatabase


def test_database_persists_workflow_and_events(tmp_path):
    database = WorkflowDatabase(tmp_path / "aidlc.sqlite3")
    database.initialize()
    database.create_run("run_one", "Build a tiny task list")
    database.update_stage("run_one", StageName.INTAKE, RunStatus.RUNNING)
    database.update_run("run_one", RunStatus.RUNNING, StageName.INTAKE)
    database.append_event("run_one", "stage.started", {"stage": "intake"})

    run = database.get_run("run_one")
    assert run is not None
    assert run.status == RunStatus.RUNNING
    assert run.current_stage == StageName.INTAKE
    assert run.stages[1].status == RunStatus.RUNNING
    assert database.events_after("run_one")[0].event_type == "stage.started"

    database.recover_interrupted_runs()
    recovered = database.get_run("run_one")
    assert recovered is not None
    assert recovered.status == RunStatus.BLOCKED
    assert database.events_after("run_one")[-1].event_type == "run.interrupted"
