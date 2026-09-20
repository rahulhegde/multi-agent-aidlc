#!/usr/bin/env python3
"""Export one AIDLC run as relative-path JSON for the static project landing page."""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any


def get_json(base_url: str, path: str) -> Any:
    with urllib.request.urlopen(f"{base_url.rstrip('/')}{path}", timeout=30) as response:
        return json.load(response)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project_id")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--output-root", type=Path, default=Path("docs/project-data"))
    args = parser.parse_args()

    runs = get_json(args.api_base, "/api/runs")
    matches = [run for run in runs if run["project_id"] == args.project_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected one run for {args.project_id}; found {len(matches)}")

    run = matches[0]
    run_id = run["run_id"]
    endpoints = {
        name: get_json(args.api_base, f"/api/runs/{run_id}/{name}")
        for name in ("artifacts", "finops", "tasks", "history", "interactions", "evaluation")
    }
    snapshot = {
        "export_version": 1,
        "project_id": args.project_id,
        "run": run,
        "config": get_json(args.api_base, "/api/config"),
        "platform": get_json(args.api_base, "/api/platform"),
        "sandboxes": get_json(args.api_base, "/api/platform/sandboxes"),
        **endpoints,
    }

    destination = args.output_root / args.project_id
    write_json(destination / "snapshot.json", snapshot)
    for artifact in endpoints["artifacts"]:
        artifact_id = artifact["metadata"]["artifact_id"]
        content = get_json(args.api_base, f"/api/artifacts/{artifact_id}")
        write_json(destination / "artifacts" / f"{artifact_id}.json", content)

    print(f"Exported {args.project_id} to {destination}")


if __name__ == "__main__":
    main()
