"""Compare identical fresh-container jobs, keeping every timing sample."""

from __future__ import annotations

import json
import statistics
from typing import Any

from aidlc.sandbox.models import PROBE_CHECKS, Mode, Runtime, SandboxJob
from aidlc.sandbox.runner import DockerSandbox
from aidlc.tools.models import SourceBundle

RUNTIMES: tuple[Runtime, ...] = ("runc", "runsc", "kata")
MODES: tuple[Mode, ...] = ("probe", "build", "test", "analyze")


async def compare_runtimes(
    backend: DockerSandbox,
    source: SourceBundle,
    repetitions: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not 1 <= repetitions <= 10:
        raise ValueError("Qualification repetitions must be between 1 and 10")
    samples: dict[str, list[dict[str, Any]]] = {runtime: [] for runtime in RUNTIMES}
    unavailable = set()
    order = []
    for repetition in range(repetitions):
        # Rotate the serial order to reduce a systematic first-runtime bias.
        rotation = repetition % len(RUNTIMES)
        for runtime in RUNTIMES[rotation:] + RUNTIMES[:rotation]:
            if runtime in unavailable:
                continue
            order.append({"repetition": repetition + 1, "runtime": runtime})
            jobs = {}
            checks: dict[str, bool] = {}
            for mode in MODES:
                result = await backend.execute(SandboxJob(source=source, mode=mode), runtime)
                jobs[mode] = result.model_dump(mode="json")
                if result.status == "unavailable":
                    unavailable.add(runtime)
                    break
                if mode == "probe":
                    try:
                        raw = json.loads(result.stdout)
                        if not isinstance(raw, dict):
                            raise ValueError
                        checks = {key: raw.get(key) is True for key in sorted(PROBE_CHECKS)}
                    except ValueError, TypeError:
                        checks = {key: False for key in sorted(PROBE_CHECKS)}
            passed = (
                len(jobs) == len(MODES)
                and bool(checks)
                and all(checks.values())
                and all(
                    job["status"] == "completed"
                    and job["execution"]["exit_code"] == 0
                    and job["execution"]["cleanup_succeeded"] is True
                    and not any(
                        job["execution"][flag]
                        for flag in (
                            "timed_out",
                            "output_truncated",
                            "oom_killed",
                        )
                    )
                    for job in jobs.values()
                )
            )
            samples[runtime].append(
                {
                    "repetition": repetition + 1,
                    "status": "unavailable"
                    if runtime in unavailable
                    else "pass"
                    if passed
                    else "fail",
                    "checks": checks,
                    "jobs": jobs,
                }
            )
    runtimes = []
    identities = set()
    for runtime in RUNTIMES:
        trials = samples[runtime]
        passed = len(trials) == repetitions and all(trial["status"] == "pass" for trial in trials)
        status = "pass" if passed else "unavailable" if runtime in unavailable else "fail"
        timings = (
            {
                mode: {
                    "samples_ms": [
                        trial["jobs"][mode]["execution"]["duration_ms"] for trial in trials
                    ],
                    "median_ms": statistics.median(
                        trial["jobs"][mode]["execution"]["duration_ms"] for trial in trials
                    ),
                }
                for mode in MODES
            }
            if passed
            else None
        )
        for trial in trials:
            for job in trial["jobs"].values():
                if job["status"] != "unavailable":
                    execution = job["execution"]
                    identities.add(
                        json.dumps(
                            {
                                "image": execution["image"],
                                "kernel": execution["kernel"],
                                "policy": execution["policy"],
                            },
                            sort_keys=True,
                        )
                    )
        runtimes.append(
            {
                "runtime": runtime,
                "status": status,
                "checks": {
                    key: all(trial["checks"].get(key) is True for trial in trials)
                    for key in sorted(PROBE_CHECKS)
                },
                "jobs": trials[0]["jobs"],
                "samples": trials,
                "timings": timings,
            }
        )
    same_environment = len(identities) == 1
    baseline = runtimes[0]
    for runtime in runtimes:
        comparable = (
            same_environment and baseline["status"] == "pass" and runtime["status"] == "pass"
        )
        runtime["relative_to_runc"] = (
            {
                mode: (
                    runtime["timings"][mode]["median_ms"] / baseline["timings"][mode]["median_ms"]
                    if baseline["timings"][mode]["median_ms"] > 0
                    else None
                )
                for mode in MODES
            }
            if comparable
            else None
        )
    comparison = {
        "repetitions": repetitions,
        "execution_order": order,
        "same_environment": same_environment,
        "timing_scope": "Container creation, workload, control operations, and confirmed teardown",
        "baseline": "runc",
        "statistic": "median",
        "limitations": [
            "Small serial smoke fixture; no statistical significance or benchmark claim.",
            "Isolation probes verify applied controls, not resistance to kernel exploits.",
            "Unavailable or failed runtimes receive no performance ratio.",
        ],
    }
    return runtimes, comparison
