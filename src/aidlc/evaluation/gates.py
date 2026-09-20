"""Check immutable, current-source evidence; no program execution or model calls."""

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from aidlc.agents.source import current_sources
from aidlc.domain.models import AgentContext, ArtifactKind, ArtifactMetadata, RequirementsSpec
from aidlc.evaluation.models import QualityGate, QualityGateReport
from aidlc.sandbox.models import SandboxExecution
from aidlc.tools.models import AnalysisReport, SourceBundle


def evaluate_gates(context: AgentContext, runtime: str = "runc") -> QualityGateReport:
    gates: list[QualityGate] = []
    artifacts = context.artifacts
    try:
        for artifact in artifacts:
            metadata = ArtifactMetadata.model_validate(artifact["metadata"])
            canonical = json.dumps(
                artifact["content"], sort_keys=True, separators=(",", ":")
            ).encode()
            if (
                metadata.workflow_run_id != context.run_id
                or hashlib.sha256(canonical).hexdigest() != metadata.content_sha256
            ):
                raise ValueError("Artifact run identity or content hash mismatch")
        if len({_id(item) for item in artifacts}) != len(artifacts):
            raise ValueError("Duplicate artifact identity")
        gates.append(
            QualityGate(
                name="artifact_integrity",
                status="passed",
                detail="Input manifests, run identity, and content hashes validate.",
                evidence_artifact_ids=[_id(item) for item in artifacts],
            )
        )
    except (KeyError, TypeError, ValueError) as error:
        return QualityGateReport(
            run_id=context.run_id,
            repair_attempt=context.repair_attempt,
            verdict="blocked",
            gates=[QualityGate(name="artifact_integrity", status="failed", detail=str(error))],
        )

    requirements = [
        item for item in artifacts if item["metadata"]["kind"] == ArtifactKind.REQUIREMENTS_SPEC
    ]
    if not requirements:
        gates.append(
            QualityGate(
                name="requirements_schema",
                status="not_executed",
                detail="No requirements artifact is available.",
            )
        )
    else:
        latest = requirements[-1]
        try:
            spec = RequirementsSpec.model_validate(latest["content"])
            if not spec.functional_requirements or not spec.acceptance_criteria:
                raise ValueError("Requirements and acceptance criteria must be nonempty")
            status, detail = "passed", "Requirements schema and acceptance criteria validate."
        except (ValidationError, ValueError) as error:
            status, detail = "failed", str(error)
        gates.append(
            QualityGate(
                name="requirements_schema",
                status=status,
                detail=detail,
                evidence_artifact_ids=[_id(latest)],
            )
        )

    # Repaired outputs replace the previous output from that specialist for evaluation.
    sources = current_sources(context)
    executable = []
    for item in sources:
        try:
            SourceBundle.model_validate(item["content"])
            executable.append(item)
        except ValidationError:
            pass
    ready = bool(sources) and len(executable) == len(sources)
    gates.append(
        QualityGate(
            name="executable_source",
            status="passed" if ready else "not_executed",
            detail="Current project source is a validated Python/React bundle."
            if ready
            else "Current implementation includes proposals or unsupported/missing source.",
            evidence_artifact_ids=[_id(item) for item in sources],
        )
    )
    for name, kind in (
        ("build", ArtifactKind.BUILD_REPORT),
        ("unit_tests", ArtifactKind.TEST_REPORT),
        ("static_analysis", ArtifactKind.STATIC_ANALYSIS_REPORT),
    ):
        gates.append(_execution_gate(name, kind, executable, artifacts, runtime, ready))
    verdict = (
        "blocked"
        if any(gate.status == "not_executed" for gate in gates)
        else ("failed" if any(gate.status == "failed" for gate in gates) else "passed")
    )
    react = any(
        file.path.startswith("ui/")
        for item in executable
        for file in SourceBundle.model_validate(item["content"]).files
    )
    return QualityGateReport(
        profile="python-react-v1" if react else "python-stdlib-v1",
        run_id=context.run_id,
        repair_attempt=context.repair_attempt,
        verdict=verdict,
        gates=gates,
        source_artifact_ids=[_id(item) for item in executable],
        deferred_checks=[
            "backend type checking" if react else "type checking",
            "integration and browser tests",
            "dependency and security scanning",
            "requirement-to-test coverage",
            "runsc/Kata comparison",
        ],
    )


def _id(artifact: dict[str, Any]) -> str:
    return artifact["metadata"]["artifact_id"]


def _execution_gate(name, kind, sources, artifacts, runtime, ready) -> QualityGate:
    if not ready:
        return QualityGate(
            name=name,
            status="not_executed",
            detail="Requires executable source from every implementation specialist.",
        )
    evidence = []
    failures = []
    missing = []
    for source in sources:
        candidates = []
        for artifact in artifacts:
            if artifact["metadata"]["kind"] != kind:
                continue
            content = artifact["content"]
            reports = content.get("reports", [content])
            if not isinstance(reports, list):
                continue
            for report in reports:
                if isinstance(report, dict) and report.get(
                    "artifact_id"
                    if kind == ArtifactKind.STATIC_ANALYSIS_REPORT
                    else "source_artifact_id"
                ) == _id(source):
                    candidates.append((artifact, report))
        if not candidates:
            missing.append(_id(source))
            continue
        artifact, report = candidates[-1]
        evidence.append(_id(artifact))
        try:
            _check_report(kind, source, artifact, report, runtime)
        except (KeyError, TypeError, ValueError) as error:
            failures.append(f"{_id(source)}: {error}")
    status = "not_executed" if missing else "failed" if failures else "passed"
    details = failures + (
        [f"Missing current-source evidence: {', '.join(missing)}"] if missing else []
    )
    return QualityGate(
        name=name,
        status=status,
        detail="; ".join(details)
        if details
        else f"Current-source {name} evidence passes under {runtime}.",
        evidence_artifact_ids=list(dict.fromkeys(evidence)),
    )


def _check_report(kind, source, artifact, report, runtime):
    analysis = kind == ArtifactKind.STATIC_ANALYSIS_REPORT
    hash_key = "content_sha256" if analysis else "source_sha256"
    if report[hash_key] != source["metadata"]["content_sha256"]:
        raise ValueError("Evidence source hash mismatch")
    if _id(source) not in artifact["metadata"]["parent_artifact_ids"]:
        raise ValueError("Evidence manifest does not reference the source parent")
    producer = artifact["metadata"]["producing_agent"]
    if producer not in ({"static-analysis-agent"} if analysis else {"trusted-sandbox-service"}):
        raise ValueError("Evidence was not published by the trusted execution service")
    if analysis:
        validated = AnalysisReport.model_validate(report)
        if validated.run_id != source["metadata"]["workflow_run_id"]:
            raise ValueError("Analysis belongs to another run")
        if validated.findings:
            raise ValueError(f"Static analysis has {len(validated.findings)} findings")
    else:
        if artifact["metadata"]["execution"] != report["execution"]:
            raise ValueError("Sandbox manifest and report execution disagree")
        if kind == ArtifactKind.TEST_REPORT and not re.search(
            r"Ran [1-9][0-9]* tests? in ", report.get("stderr", "")
        ):
            raise ValueError("No executed unittest count in the trusted worker output")
    execution = report["execution"]
    # Parse fixed sandbox limits even for the analyzer's extended execution shape.
    SandboxExecution.model_validate(execution)
    if (
        report["status"] != "completed"
        or execution.get("runtime") != runtime
        or execution.get("exit_code") != 0
        or execution.get("cleanup_succeeded") is not True
        or any(
            execution.get(flag) is not False
            for flag in ("timed_out", "output_truncated", "oom_killed")
        )
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", execution.get("image") or "")
    ):
        raise ValueError(
            "Execution failed, lacks pinned isolation evidence, or used another runtime"
        )


def gates_markdown(report: QualityGateReport) -> str:
    return f"# Deterministic quality gates\n\nProfile: {report.profile}\n\n{report.verdict}\n\n" + (
        "\n".join(f"- {gate.name}: **{gate.status}** — {gate.detail}" for gate in report.gates)
    )
