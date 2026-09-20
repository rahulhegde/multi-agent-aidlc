from __future__ import annotations

from pathlib import Path

from aidlc.config import Settings
from aidlc.sandbox.models import SandboxJob, SandboxPolicy
from aidlc.sandbox.runner import DockerSandbox
from aidlc.tools.analyzer import Analyzer, analyze_with_ruff, ruff_findings
from aidlc.tools.models import AnalysisJob, AnalyzerExecution, AnalyzerResult


def configured_analyzer(settings: Settings) -> Analyzer:
    if settings.analysis_backend == "local-analyzer":
        return analyze_with_ruff
    if settings.analysis_backend != "sandbox":
        raise ValueError("AIDLC_ANALYSIS_BACKEND must be sandbox or local-analyzer")
    backend = DockerSandbox(settings)

    async def analyze(job: AnalysisJob) -> AnalyzerResult:
        result = await backend.execute(
            SandboxJob(
                source=job.source,
                mode="analyze",
                policy=SandboxPolicy(
                    timeout_seconds=job.timeout_seconds, max_output_bytes=job.max_output_bytes
                ),
            )
        )
        execution = AnalyzerExecution.model_validate(result.execution.model_dump())
        findings = []
        if result.status == "completed":
            try:
                findings = ruff_findings(result.stdout, Path("/workspace"))
                if any(
                    finding.path not in {file.path for file in job.source.files}
                    for finding in findings
                ):
                    raise ValueError("Unexpected finding path")
            except ValueError, KeyError, TypeError:
                result.status = "failed"
                execution.reason = "Sandbox returned invalid Ruff evidence"
        return AnalyzerResult(status=result.status, findings=findings, execution=execution)

    return analyze
