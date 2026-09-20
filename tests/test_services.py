"""Exercise lifecycle management with real child processes and a fake Docker CLI."""

import json
import subprocess
import sys
import time

import pytest

from scripts import services


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", services.os.environ["PATH"])
    monkeypatch.setenv("AIDLC_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("AIDLC_DATA_DIR", str(tmp_path / "data"))
    manager = services.Stack(timeout=0.1, stop_timeout=0.2)
    manager.services = [
        services.Service(
            name, [sys.executable, "-c", "import time; time.sleep(60)"], f"http://{name}"
        )
        for name in ("tools", "agents", "api", "ui")
    ]
    monkeypatch.setattr(manager, "preflight", lambda: None)
    monkeypatch.setattr(services, "ready", lambda _url: True)
    docker = {"running": False, "stops": 0}

    def docker_run(command, **_kwargs):
        assert command[0] == "docker"
        output = ""
        if "ps" in command:
            output = "test-keycloak\n" if docker["running"] else ""
        elif "up" in command:
            docker["running"] = True
        elif "stop" in command:
            docker["running"] = False
            docker["stops"] += 1
        else:
            pytest.fail(f"Unexpected Docker command: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    monkeypatch.setattr(services.subprocess, "run", docker_run)
    monkeypatch.setattr(manager, "container_running", lambda _id: docker["running"])
    try:
        yield manager, docker
    finally:
        manager.stop()


def test_start_is_idempotent_and_stop_preserves_data(stack):
    manager, docker = stack
    data = manager.directory.parent / "aidlc.sqlite3"
    data.write_bytes(b"persistent-data")
    manager.start()
    records = {service.name: manager.read_record(service.name) for service in manager.services}
    manager.start()
    assert records == {
        service.name: manager.read_record(service.name) for service in manager.services
    }
    assert manager.status()
    manager.stop()
    manager.stop()
    assert not any(manager.running(service.name) for service in manager.services)
    assert not docker["running"]
    assert docker["stops"] == 1
    assert data.read_bytes() == b"persistent-data"


def test_restart_replaces_processes(stack):
    manager, docker = stack
    manager.start()
    previous = manager.read_record("api")
    manager.stop()
    manager.start()
    assert manager.read_record("api") != previous
    assert manager.status()
    assert docker["stops"] == 1


def test_failed_start_rolls_back_only_new_services(stack, monkeypatch):
    manager, docker = stack
    manager.start_service(manager.services[0])
    previous = manager.read_record("tools")
    original = manager.wait_ready

    def wait(name, url, **kwargs):
        if name == "agents":
            raise RuntimeError("startup failure")
        original(name, url, **kwargs)

    monkeypatch.setattr(manager, "wait_ready", wait)
    with pytest.raises(RuntimeError, match="startup failure"):
        manager.start()
    assert manager.running("tools")
    assert manager.read_record("tools") == previous
    assert not manager.running("agents")
    assert not docker["running"]


def test_stop_kills_descendants_when_leader_has_exited(stack, tmp_path):
    manager, _docker = stack
    marker = tmp_path / "child.json"
    code = (
        "import json, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(marker)!r}, 'w').write(json.dumps(child.pid)); "
        "time.sleep(60)"
    )
    manager.start_service(services.Service("api", [sys.executable, "-c", code], "http://api"))
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = json.loads(marker.read_text())
        record = manager.read_record("api")
        assert child_pid in services.process_members(record)
        # Killing the launcher must not leave its server child unmanaged.
        manager.children["api"].kill()
        manager.children["api"].wait(timeout=5)
        assert manager.running("api")
        manager.stop_service("api")
        assert not services.process_members(record)
    finally:
        manager.stop_service("api")


def test_stale_record_does_not_kill_reused_pid(stack):
    manager, _docker = stack
    manager.start_service(manager.services[0])
    original = manager.read_record("tools")
    manager.write_record("api", {**original, "start_time": str(int(original["start_time"]) + 1)})
    manager.stop_service("api")
    assert manager.running("tools")
    assert not (manager.directory / "api.json").exists()


def test_selected_env_file_and_exported_values(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "ROOT", tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", services.os.environ["PATH"])
    configuration = tmp_path / "custom.env"
    configuration.write_text(
        "AIDLC_A2A_BASE_URL=http://localhost:9001\n"
        "AIDLC_MCP_URL=http://localhost:9002/mcp\n"
        "VITE_HUMAN_ACTION_TOKEN=from-file\n"
    )
    monkeypatch.setenv("AIDLC_ENV_FILE", str(configuration))
    monkeypatch.setenv("AIDLC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AIDLC_A2A_BASE_URL", raising=False)
    monkeypatch.delenv("AIDLC_MCP_URL", raising=False)
    monkeypatch.setenv("VITE_HUMAN_ACTION_TOKEN", "exported-value")
    manager = services.Stack()
    assert (
        manager.services[0].url == "http://localhost:9002/.well-known/oauth-protected-resource/mcp"
    )
    assert manager.services[1].url == "http://localhost:9001/health"
    assert services.os.environ["VITE_HUMAN_ACTION_TOKEN"] == "exported-value"
    assert manager.compose("up") == [
        "docker",
        "compose",
        "--env-file",
        str(configuration),
        "-f",
        str(tmp_path / "dev/compose.yaml"),
        "up",
    ]


def test_readiness_timeout_rolls_back_stack(stack, monkeypatch):
    manager, docker = stack
    monkeypatch.setattr(services, "ready", lambda url: url != manager.services[1].url)
    with pytest.raises(RuntimeError, match="agents did not become ready"):
        manager.start()
    assert not any(manager.running(service.name) for service in manager.services)
    assert not docker["running"]


def test_compose_failure_after_container_creation_is_cleaned_up(stack, monkeypatch):
    manager, docker = stack
    original = services.subprocess.run

    def failing_compose(command, **kwargs):
        result = original(command, **kwargs)
        if "up" in command:
            raise subprocess.CalledProcessError(1, command)
        return result

    monkeypatch.setattr(services.subprocess, "run", failing_compose)
    with pytest.raises(subprocess.CalledProcessError):
        manager.start()
    assert docker["stops"] == 1
    assert not docker["running"]


def test_stop_escalates_for_process_ignoring_term(stack, tmp_path):
    manager, _docker = stack
    marker = tmp_path / "ready"
    code = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"open({str(marker)!r}, 'w').close(); time.sleep(60)"
    )
    manager.start_service(services.Service("api", [sys.executable, "-c", code], "http://api"))
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    process = manager.children["api"]
    manager.stop_service("api")
    assert process.returncode == -services.signal.SIGKILL


def test_shutdown_continues_after_service_failure(stack, monkeypatch):
    manager, docker = stack
    manager.start()
    original = manager.stop_service

    def failing_stop(name):
        if name == "ui":
            raise RuntimeError("test failure")
        original(name)

    with monkeypatch.context() as patch:
        patch.setattr(manager, "stop_service", failing_stop)
        with pytest.raises(RuntimeError, match="Shutdown incomplete: ui: test failure"):
            manager.stop()
    assert manager.running("ui")
    assert not manager.running("api")
    assert not manager.running("agents")
    assert not manager.running("tools")
    assert not docker["running"]
