from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from ruff import find_ruff_bin

from aidlc.tools.models import AnalysisJob, AnalyzerExecution, AnalyzerResult, Finding

Analyzer = Callable[[AnalysisJob], Awaitable[AnalyzerResult]]


def ruff_findings(output: str | bytes, workspace: Path) -> list[Finding]:
    items = json.loads(output)
    if not isinstance(items, list) or len(items) > 10_000:
        raise ValueError("Invalid Ruff result")
    findings = []
    for item in items:
        path = Path(item["filename"]).relative_to(workspace).as_posix()
        if ".." in Path(path).parts:
            raise ValueError("Ruff finding outside workspace")
        rule = item["code"] or "invalid-syntax"
        line, column = item["location"]["row"], item["location"]["column"]
        fingerprint = hashlib.sha256(
            f"{path}:{rule}:{line}:{column}:{item['message']}".encode()
        ).hexdigest()
        findings.append(
            Finding(
                rule_id=rule,
                severity="warning" if rule == "F401" else "error",
                path=path,
                line=line,
                column=column,
                message=item["message"],
                fingerprint=fingerprint,
            )
        )
    return findings


class OutputLimitError(Exception):
    pass


async def _bounded_read(stream: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while chunk := await stream.read(8192):
        output.extend(chunk)
        if len(output) > limit:
            raise OutputLimitError
    return bytes(output)


async def analyze_with_ruff(job: AnalysisJob) -> AnalyzerResult:
    """Explicit development backend; never executes source and makes no isolation claim."""
    started = time.monotonic()
    timed_out = truncated = False
    findings: list[Finding] = []
    stdout: asyncio.Task[bytes] | None = None
    status = "failed"
    with tempfile.TemporaryDirectory(prefix="aidlc-analysis-") as directory:
        workspace = Path(directory)
        for source in job.source.files:
            destination = workspace / source.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(source.content, encoding="utf-8")
        # No shell, project configuration, plugins, or inherited credentials.
        process = await asyncio.create_subprocess_exec(
            str(find_ruff_bin()),
            "check",
            "--isolated",
            "--no-cache",
            "--output-format=json",
            "--select=E4,E7,E9,F",
            "--target-version=py314",
            ".",
            cwd=workspace,
            env={"LANG": "C.UTF-8", "RAYON_NUM_THREADS": "1"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert process.stdout is not None and process.stderr is not None
        try:
            async with asyncio.timeout(job.timeout_seconds):
                try:
                    async with asyncio.TaskGroup() as readers:
                        stdout = readers.create_task(
                            _bounded_read(process.stdout, job.max_output_bytes)
                        )
                        readers.create_task(_bounded_read(process.stderr, job.max_output_bytes))
                        readers.create_task(process.wait())
                except* OutputLimitError:
                    truncated, status = True, "output_limit"
            if not truncated and process.returncode in {0, 1}:
                assert stdout is not None
                findings = ruff_findings(stdout.result(), workspace)
                status = "completed"
        except TimeoutError:
            timed_out, status = True, "timed_out"
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
    return AnalyzerResult.model_validate(
        {
            "status": status,
            "findings": findings,
            "execution": AnalyzerExecution(
                duration_ms=int((time.monotonic() - started) * 1000),
                timed_out=timed_out,
                output_truncated=truncated,
            ),
        }
    )
