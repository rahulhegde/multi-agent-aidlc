from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import httpx

from aidlc.config import Settings
from aidlc.domain.models import ArtifactKind, RunStatus, StageName, utc_now
from aidlc.sandbox.comparison import compare_runtimes
from aidlc.sandbox.models import Runtime, SandboxJob, SandboxPolicy
from aidlc.sandbox.runner import DockerSandbox, resolve_image
from aidlc.storage.artifacts import ArtifactStore
from aidlc.storage.database import WorkflowDatabase
from aidlc.tools.models import AnalysisRequest, SourceBundle

FIXTURE = SourceBundle.model_validate(
    {
        "files": [
            {"path": "example.py", "content": "def answer():\n    return 42\n"},
            {
                "path": "test_example.py",
                "content": (
                    "import unittest\nfrom example import answer\n\n"
                    "class Example(unittest.TestCase):\n"
                    "    def test_answer(self):\n        self.assertEqual(answer(), 42)\n"
                ),
            },
        ]
    }
)


def _publish_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as temporary:
        json.dump(value, temporary, indent=2)
    os.replace(temporary.name, path)


def build_image(settings: Settings) -> dict[str, Any]:
    context = Path(__file__).resolve().parents[3] / "sandbox"
    if not (context / "Dockerfile").is_file():
        raise ValueError("Run build-image from a source checkout containing sandbox/Dockerfile")
    with tempfile.TemporaryDirectory(prefix="aidlc-image-build-") as directory:
        iidfile = Path(directory) / "image-id"
        subprocess.run(
            [
                "docker",
                "--host=unix:///var/run/docker.sock",
                "--config",
                directory,
                "build",
                "--platform=linux/amd64",
                "--iidfile",
                str(iidfile),
                str(context),
            ],
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
            check=True,
        )
        image = iidfile.read_text().strip()
    # The final image is selected by content ID, never by a mutable tag.
    resolve_image(Settings(sandbox_image=image))
    manifest = {
        "image": image,
        "built_at": utc_now().isoformat(),
        "platform": "linux/amd64",
        "python": "3.14.7",
        "ruff": "0.16.7",
        "node": "24.21.0",
        "react": "19.3.0",
        "inputs": {
            name: hashlib.sha256((context / name).read_bytes()).hexdigest()
            for name in (
                "Dockerfile",
                "requirements.txt",
                "worker.py",
                "react/package.json",
                "react/package-lock.json",
                "react/vite.config.mjs",
                "react/tsconfig.json",
            )
        },
    }
    _publish_json(settings.data_dir / "sandbox-image.json", manifest)
    return manifest


def latest_qualification(settings: Settings) -> dict[str, Any] | None:
    try:
        pointer = json.loads((settings.data_dir / "sandbox-qualification.json").read_text())
        database = WorkflowDatabase(settings.database_path)
        record = database.get_artifact(pointer["artifact_id"])
        if record is None or record.metadata.kind != ArtifactKind.SANDBOX_QUALIFICATION_REPORT:
            return None
        content = ArtifactStore.read_content(record)
        return {"artifact_id": record.metadata.artifact_id, **content}
    except OSError, ValueError, KeyError, TypeError:
        return None


async def qualify(settings: Settings, repetitions: int = 3) -> dict[str, Any]:
    backend = DockerSandbox(settings)
    runtimes, comparison = await compare_runtimes(backend, FIXTURE, repetitions)
    report = {
        "generated_at": utc_now().isoformat(),
        "fixture": "python-stdlib-answer-v1",
        "source_sha256": hashlib.sha256(
            json.dumps(
                FIXTURE.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        "baseline_qualified": all(item["status"] == "pass" for item in runtimes[:2]),
        "microvm_qualified": runtimes[2]["status"] == "pass",
        "runtimes": runtimes,
        "comparison": comparison,
    }
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    run_id = f"run_{uuid4().hex}"
    database.create_run(run_id, "Sandbox runtime qualification fixture")
    database.update_run(
        run_id,
        RunStatus.BLOCKED,
        error="Standalone runtime qualification; no generated MVP lifecycle execution.",
    )
    artifact = ArtifactStore(settings.artifact_dir).write(
        run_id=run_id,
        stage=StageName.PREFLIGHT,
        kind=ArtifactKind.SANDBOX_QUALIFICATION_REPORT,
        producing_agent="trusted-sandbox-qualifier",
        content=report,
        markdown="# Sandbox qualification\n\n"
        + "\n".join(f"- {item['runtime']}: {item['status']}" for item in runtimes),
    )
    database.add_artifact(artifact)
    database.append_event(
        run_id,
        "sandbox.qualified",
        {
            "artifact_id": artifact.metadata.artifact_id,
            "runtimes": [
                {"runtime": item["runtime"], "status": item["status"]} for item in runtimes
            ],
        },
    )
    _publish_json(
        settings.data_dir / "sandbox-qualification.json",
        {"artifact_id": artifact.metadata.artifact_id},
    )
    return {"run_id": run_id, "artifact_id": artifact.metadata.artifact_id, **report}


async def run_artifact(settings: Settings, arguments: argparse.Namespace) -> dict[str, Any]:
    return await execute_artifact(
        settings,
        arguments.artifact_id,
        arguments.content_sha256,
        arguments.command,
        arguments.runtime,
        arguments.timeout_seconds,
    )


async def execute_artifact(
    settings: Settings,
    artifact_id: str,
    content_sha256: str,
    mode: Literal["build", "test"],
    runtime: Runtime | None = None,
    timeout_seconds: int = 60,
) -> dict[str, Any]:
    if mode not in {"build", "test"}:
        raise ValueError("Expected a fixed build or test sandbox job")
    request = AnalysisRequest(artifact_id=artifact_id, content_sha256=content_sha256)
    database = WorkflowDatabase(settings.database_path)
    database.initialize()
    record = database.get_artifact(request.artifact_id)
    if record is None or record.metadata.kind not in {
        ArtifactKind.CODE_CHANGE,
        ArtifactKind.INTEGRATED_SOURCE,
    }:
        raise ValueError("Expected an immutable code_change source artifact")
    path = (Path(record.content_path) / "content.json").resolve()
    if (
        record.metadata.content_sha256 != request.content_sha256
        or not path.is_relative_to(settings.artifact_dir.resolve())
        or path.stat().st_size > 1_048_576
    ):
        raise ValueError("Source artifact hash or containment mismatch")
    source = SourceBundle.model_validate(ArtifactStore.read_content(record))
    result = await DockerSandbox(settings).execute(
        SandboxJob(
            source=source,
            mode=mode,
            policy=SandboxPolicy(timeout_seconds=timeout_seconds),
        ),
        runtime,
    )
    content = {
        "source_artifact_id": request.artifact_id,
        "source_sha256": request.content_sha256,
        **result.model_dump(mode="json"),
    }
    artifact = ArtifactStore(settings.artifact_dir).write(
        run_id=record.metadata.workflow_run_id,
        stage=StageName.INTEGRATION,
        kind=ArtifactKind.BUILD_REPORT if mode == "build" else ArtifactKind.TEST_REPORT,
        producing_agent="trusted-sandbox-service",
        content=content,
        markdown=f"# Sandbox {mode}\n\n{result.status} ({result.execution.runtime})",
        parent_artifact_ids=[request.artifact_id],
        execution=result.execution.model_dump(mode="json"),
    )
    database.add_artifact(artifact)
    database.append_event(
        record.metadata.workflow_run_id,
        "sandbox.executed",
        {
            "artifact_id": artifact.metadata.artifact_id,
            "source_artifact_id": request.artifact_id,
            "status": result.status,
            "execution": result.execution.model_dump(mode="json"),
        },
    )
    return {"artifact_id": artifact.metadata.artifact_id, **content}


def run() -> None:
    parser = argparse.ArgumentParser(description="Build and qualify the trusted Python sandbox")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build-image")
    qualification = commands.add_parser("qualify")
    qualification.add_argument("--repetitions", type=int, choices=range(1, 11), default=3)
    commands.add_parser("reconcile")
    preparation = commands.add_parser("prepare-runtime")
    preparation.add_argument("runtime", choices=["runsc", "kata"])
    preparation.add_argument("--archive", type=Path)
    preparation.add_argument("--existing-daemon", type=Path)
    for mode in ("build", "test"):
        command = commands.add_parser(mode)
        command.add_argument("artifact_id")
        command.add_argument("content_sha256")
        command.add_argument("--runtime", choices=["runc", "runsc", "kata"])
        command.add_argument("--timeout-seconds", type=int, choices=range(1, 61), default=60)
    arguments = parser.parse_args()
    settings = Settings()
    try:
        if arguments.command == "build-image":
            result = build_image(settings)
        elif arguments.command == "qualify":
            result = asyncio.run(qualify(settings, arguments.repetitions))
        elif arguments.command == "reconcile":
            from aidlc.sandbox.recovery import publish_recovery

            result = publish_recovery(settings, asyncio.run(DockerSandbox(settings).reconcile()))
        elif arguments.command == "prepare-runtime":
            from aidlc.sandbox.provisioning import prepare_runtime

            result = prepare_runtime(
                settings,
                arguments.runtime,
                arguments.archive,
                arguments.existing_daemon,
            )
        else:
            result = asyncio.run(run_artifact(settings, arguments))
    except (
        ValueError,
        OSError,
        subprocess.CalledProcessError,
        httpx.HTTPError,
        tarfile.TarError,
    ) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))
    if result.get("status") in {"failed", "unavailable", "timed_out", "output_limit"}:
        raise SystemExit(1)
    if arguments.command == "qualify" and not result["baseline_qualified"]:
        raise SystemExit(1)
