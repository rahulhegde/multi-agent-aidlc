"""Manage the local development stack; invoked through aidlc.sh."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
BOOT_ID = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
HTTP = build_opener(ProxyHandler({}))


@dataclass(frozen=True)
class Service:
    name: str
    command: list[str]
    url: str


def process_stat(pid: int) -> list[str] | None:
    try:
        # The command name can contain spaces or parentheses.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except FileNotFoundError, ProcessLookupError:
        return None


def process_members(record: dict) -> list[int]:
    """Check boot and process birth time before sending any group signal."""
    pid = record["pid"]
    if record["boot_id"] != BOOT_ID or not isinstance(pid, int) or pid <= 1:
        return []
    leader = process_stat(pid)
    if leader is not None and leader[19] != record["start_time"]:
        return []
    return [
        int(path.name)
        for path in Path("/proc").iterdir()
        if path.name.isdigit()
        and (stat := process_stat(int(path.name))) is not None
        and stat[0] != "Z"
        and stat[2] == str(pid)
        and stat[3] == str(pid)
        and int(stat[19]) >= int(record["start_time"])
    ]


def ready(url: str) -> bool:
    try:
        with HTTP.open(url, timeout=2) as response:
            return response.status == 200
    except URLError, TimeoutError, OSError:
        return False


def local_url(value: str, default_port: int) -> str:
    endpoint = urlsplit(value)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "localhost"}
        or endpoint.username is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("Development service URLs must use loopback HTTP addresses")
    # The backend entry points use their service port when none is supplied.
    return f"http://{endpoint.hostname}:{endpoint.port or default_port}{endpoint.path}"


class Stack:
    def __init__(self, timeout: float = 120, stop_timeout: float = 15) -> None:
        os.chdir(ROOT)
        self.env_file = Path(os.getenv("AIDLC_ENV_FILE", str(ROOT / ".env"))).resolve()
        load_dotenv(self.env_file, override=False)
        os.environ["AIDLC_ENV_FILE"] = str(self.env_file)
        local_bins = [ROOT / ".tools/node/bin", ROOT / ".tools/pnpm/node_modules/.bin"]
        os.environ["PATH"] = os.pathsep.join(
            [str(path) for path in local_bins if path.is_dir()] + [os.environ.get("PATH", "")]
        )
        self.directory = Path(os.getenv("AIDLC_DATA_DIR", ".aidlc-data")) / "dev-services"
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.timeout, self.stop_timeout = timeout, stop_timeout
        self.children: dict[str, subprocess.Popen] = {}
        a2a = local_url(os.getenv("AIDLC_A2A_BASE_URL", "http://127.0.0.1:8001"), 8001)
        mcp = local_url(os.getenv("AIDLC_MCP_URL", "http://127.0.0.1:8002/mcp"), 8002)
        issuer = local_url(
            os.getenv("AIDLC_OIDC_ISSUER", "http://127.0.0.1:8080/realms/aidlc"), 8080
        )
        if urlsplit(a2a).path not in {"", "/"} or urlsplit(mcp).path != "/mcp":
            raise ValueError("A2A must use the root URL and MCP must use /mcp")
        if urlsplit(issuer).port != 8080:
            raise ValueError("dev/compose.yaml exposes Keycloak on port 8080")
        self.identity_url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        self.services = [
            Service(
                "tools",
                [str(ROOT / ".venv/bin/aidlc-tools")],
                mcp.removesuffix("/mcp") + "/.well-known/oauth-protected-resource/mcp",
            ),
            Service("agents", [str(ROOT / ".venv/bin/aidlc-agents")], a2a.rstrip("/") + "/health"),
            Service("api", [str(ROOT / ".venv/bin/aidlc-api")], "http://127.0.0.1:8000/api/health"),
            Service(
                "ui",
                ["pnpm", "--dir", str(ROOT / "ui"), "dev", "--strictPort"],
                "http://127.0.0.1:5173/",
            ),
        ]

    def read_record(self, name: str) -> dict | None:
        path = self.directory / f"{name}.json"
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError
            return value
        except FileNotFoundError:
            return None
        except ValueError:
            raise RuntimeError(f"Invalid service record: {path}") from None

    def write_record(self, name: str, value: dict) -> None:
        path = self.directory / f"{name}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)

    def running(self, name: str) -> bool:
        record = self.read_record(name)
        return bool(record and process_members(record))

    def compose(self, *arguments: str) -> list[str]:
        command = ["docker", "compose"]
        if self.env_file.is_file():
            command += ["--env-file", str(self.env_file)]
        return command + ["-f", str(ROOT / "dev/compose.yaml"), *arguments]

    def container_running(self, container_id: str) -> bool:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container_id],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def wait_ready(self, name: str, url: str, *, managed: bool = False) -> None:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if managed and not self.running(name):
                raise RuntimeError(f"{name} exited. See {self.directory / (name + '.log')}")
            if ready(url):
                print(f"{name}: ready ({url})", flush=True)
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"{name} did not become ready within {self.timeout:g}s. "
            f"See {self.directory / (name + '.log')}"
        )

    def preflight(self) -> None:
        for command in ("docker", "node", "pnpm"):
            if shutil.which(command) is None:
                raise RuntimeError(f"Missing {command}; install the prerequisites in README.md")
        for service in self.services[:-1]:
            if not os.access(service.command[0], os.X_OK):
                raise RuntimeError("Missing backend dependencies. Run 'uv sync --locked'")
        if not (ROOT / "ui/node_modules/.bin/vite").exists():
            raise RuntimeError(
                "Missing UI dependencies. Run 'pnpm --dir ui install --frozen-lockfile'"
            )
        for service in self.services:
            if self.running(service.name):
                continue
            endpoint = urlsplit(service.url)
            try:
                with socket.create_connection((endpoint.hostname, endpoint.port), timeout=0.5):
                    raise RuntimeError(
                        f"Port {endpoint.port} is already in use outside this script. "
                        "Stop that process before starting the stack."
                    )
            except ConnectionRefusedError, TimeoutError:
                pass

    def start_service(self, service: Service) -> None:
        print(f"{service.name}: starting", flush=True)
        with (self.directory / f"{service.name}.log").open("ab") as log:
            process = subprocess.Popen(
                service.command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        stat = process_stat(process.pid)
        if stat is None:
            process.wait()
            raise RuntimeError(f"{service.name} exited before startup; check its log")
        try:
            self.write_record(
                service.name,
                {
                    "pid": process.pid,
                    "start_time": stat[19],
                    "boot_id": BOOT_ID,
                },
            )
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        self.children[service.name] = process

    def start(self) -> None:
        self.preflight()
        started: list[str] = []
        identity_started = False
        try:
            previous = subprocess.run(
                self.compose("ps", "-q", "keycloak"),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            identity_started = not previous or not self.container_running(previous)
            print("keycloak: starting", flush=True)
            container_id = ""
            try:
                with (self.directory / "keycloak.log").open("ab") as log:
                    subprocess.run(
                        self.compose("up", "-d", "keycloak"),
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
            finally:
                # Compose can create a container before failing or being interrupted.
                discovered = subprocess.run(
                    self.compose("ps", "--all", "-q", "keycloak"),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if discovered.returncode == 0:
                    container_id = discovered.stdout.strip()
                    if container_id:
                        self.write_record("keycloak", {"container_id": container_id})
            if not container_id:
                raise RuntimeError("Compose did not return the Keycloak container ID")
            self.wait_ready("keycloak", self.identity_url)
            for service in self.services:
                if not self.running(service.name):
                    self.start_service(service)
                    started.append(service.name)
                self.wait_ready(service.name, service.url, managed=True)
        except Exception, KeyboardInterrupt:
            print("Startup failed; stopping services started by this command.", file=sys.stderr)
            for name in reversed(started):
                try:
                    self.stop_service(name)
                except Exception as error:
                    print(f"Cleanup failed for {name}: {error}", file=sys.stderr)
            if identity_started:
                try:
                    self.stop_identity()
                except Exception as error:
                    print(f"Cleanup failed for keycloak: {error}", file=sys.stderr)
            raise
        print(f"Stack ready. UI: http://127.0.0.1:5173/\nLogs: {self.directory.resolve()}")

    def stop_service(self, name: str) -> None:
        record = self.read_record(name)
        if record and process_members(record):
            print(f"{name}: stopping", flush=True)
            try:
                os.killpg(record["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + self.stop_timeout
            while process_members(record) and time.monotonic() < deadline:
                time.sleep(0.1)
            if process_members(record):
                try:
                    os.killpg(record["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                deadline = time.monotonic() + 5
                while process_members(record) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if process_members(record):
                    raise RuntimeError(
                        f"Could not stop {name}; its process record has been retained"
                    )
        else:
            print(f"{name}: stopped", flush=True)
        child = self.children.pop(name, None)
        if child is not None:
            child.wait(timeout=5)
        (self.directory / f"{name}.json").unlink(missing_ok=True)

    def stop_identity(self) -> None:
        record = self.read_record("keycloak")
        if record:
            subprocess.run(
                [
                    "docker",
                    "stop",
                    "--time",
                    str(int(self.stop_timeout)),
                    record["container_id"],
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
            (self.directory / "keycloak.json").unlink(missing_ok=True)
        print("keycloak: stopped", flush=True)

    def stop(self) -> None:
        errors = []
        for service in reversed(self.services):
            try:
                self.stop_service(service.name)
            except Exception as error:
                errors.append(f"{service.name}: {error}")
        try:
            self.stop_identity()
        except Exception as error:
            errors.append(f"keycloak: {error}")
        if errors:
            raise RuntimeError("Shutdown incomplete: " + "; ".join(errors))

    def status(self) -> bool:
        record = self.read_record("keycloak")
        identity_ok = bool(record and self.container_running(record["container_id"]))
        healthy = identity_ok and ready(self.identity_url)
        print(f"keycloak: {'ready' if healthy else 'running' if identity_ok else 'stopped'}")
        for service in self.services:
            running = self.running(service.name)
            service_ready = running and ready(service.url)
            state = "ready" if service_ready else "running" if running else "stopped"
            print(f"{service.name}: {state}")
            healthy = healthy and service_ready
        return healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="Start, stop, or restart the local AIDLC stack.")
    parser.add_argument("command", choices=("start", "stop", "restart", "status"))
    parser.add_argument(
        "--timeout", type=float, default=120, help="Readiness timeout per service (s)"
    )
    parser.add_argument(
        "--stop-timeout", type=float, default=15, help="Graceful shutdown timeout (s)"
    )
    arguments = parser.parse_args()
    if arguments.timeout <= 0 or arguments.stop_timeout <= 0:
        parser.error("Timeouts must be greater than zero")
    try:
        stack = Stack(arguments.timeout, arguments.stop_timeout)
        with (stack.directory / "services.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(
                    "Another service command is running; try again when it finishes"
                ) from None
            if arguments.command in {"stop", "restart"}:
                stack.stop()
            if arguments.command in {"start", "restart"}:
                stack.start()
            if arguments.command == "status":
                return 0 if stack.status() else 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
