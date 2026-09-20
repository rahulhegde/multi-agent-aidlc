"""Trusted image entry point. No caller-controlled commands or dependencies."""

import compileall
import json
import os
import shutil
import socket
import subprocess
import sys
import unittest
from pathlib import Path, PurePosixPath


def probe():
    checks = {"non_root": os.getuid() == 65532}
    status = Path("/proc/self/status").read_text()
    checks["no_capabilities"] = "CapEff:\t0000000000000000" in status
    checks["no_new_privileges"] = "NoNewPrivs:\t1" in status
    checks["no_credentials"] = set(os.environ) <= {
        "PATH",
        "LANG",
        "RAYON_NUM_THREADS",
        "PYTHONDONTWRITEBYTECODE",
        "HOSTNAME",
        "HOME",
        "LC_CTYPE",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
    }
    checks["no_host_sockets"] = not Path("/var/run/docker.sock").exists()
    checks["no_host_home"] = not Path("/root/.ssh").exists() and not Path("/root/.aws").exists()
    for name, path in (("root_read_only", "/opt/aidlc/probe"),):
        try:
            Path(path).touch()
            checks[name] = False
        except OSError:
            checks[name] = True
    # Container mount evidence distinguishes a read-only root from UID permissions.
    mounts = Path("/proc/mounts").read_text().splitlines()
    checks["root_mount_read_only"] = any(
        line.split()[1] == "/" and "ro" in line.split()[3].split(",") for line in mounts
    )
    for target in ("/workspace", "/tmp"):
        stat = os.statvfs(target)
        checks[f"bounded_tmpfs:{target}"] = stat.f_blocks * stat.f_frsize <= 16 * 1024**2
        path = Path(target) / "probe"
        path.write_text("ephemeral")
        path.unlink()
    checks["network_interfaces"] = {p.name for p in Path("/sys/class/net").iterdir()} == {"lo"}
    with socket.socket() as connection:
        connection.settimeout(0.5)
        checks["network_denied"] = connection.connect_ex(("192.0.2.1", 443)) != 0
    print(json.dumps(checks))
    return 0 if all(checks.values()) else 1


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "probe":
        return probe()
    if mode not in {"analyze", "build", "test"}:
        return 2
    raw = sys.stdin.buffer.read(1_048_577)
    if len(raw) > 1_048_576:
        return 2
    payload = json.loads(raw)
    files = payload["files"]
    if not 1 <= len(files) <= 50 or sum(len(f["content"].encode()) for f in files) > 262_144:
        return 2
    seen = set()
    for source in files:
        name = source["path"]
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or any(p.startswith(".") or not p for p in name.split("/"))
            or path.suffix
            not in {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".html", ".css", ".md", ".txt"}
            or any(part in {"node_modules", "dist", "__pycache__"} for part in path.parts)
            or name in seen
        ):
            return 2
        seen.add(name)
        destination = Path("/workspace") / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source["content"], encoding="utf-8")
    environment = {
        "LANG": "C.UTF-8",
        "RAYON_NUM_THREADS": "1",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
    }
    os.environ.clear()
    os.environ.update(environment)
    if mode == "analyze":
        command = [
            "/usr/local/bin/ruff",
            "check",
            "--isolated",
            "--no-cache",
            "--output-format=json",
            "--select=E4,E7,E9,F",
            "--target-version=py314",
            ".",
        ]
    elif mode == "build":
        if not compileall.compile_dir("/workspace", quiet=1, force=True):
            return 1
        return build_react(environment)
    else:
        if build_react(environment):
            return 1
        sys.path.insert(0, "/workspace")
        suite = unittest.TestLoader().discover("/workspace", pattern="test_*.py")
        if not suite.countTestCases():
            print("No unittest cases in source bundle", file=sys.stderr)
            return 2
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    return subprocess.run(command, env=environment, cwd="/workspace", check=False).returncode


def build_react(environment):
    ui = Path("/workspace/ui")
    if not ui.exists():
        return 0
    if not (ui / "index.html").is_file() or not (ui / "src/main.tsx").is_file():
        print("React UI requires index.html and src/main.tsx", file=sys.stderr)
        return 2
    trusted = Path("/opt/aidlc/react")
    for name in ("package.json", "package-lock.json", "tsconfig.json"):
        shutil.copyfile(trusted / name, ui / name)
    (ui / "node_modules").symlink_to(trusted / "node_modules", target_is_directory=True)
    commands = [
        [
            "node",
            str(trusted / "node_modules/typescript/bin/tsc"),
            "--noEmit",
            "-p",
            str(ui / "tsconfig.json"),
        ],
        [
            "node",
            str(trusted / "node_modules/vite/bin/vite.js"),
            "build",
            str(ui),
            "--config",
            str(trusted / "vite.config.mjs"),
            "--configLoader",
            "native",
        ],
    ]
    for command in commands:
        code = subprocess.run(command, env=environment, cwd=ui, check=False).returncode
        if code:
            return code
    print("React dependency toolchain and production build passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
