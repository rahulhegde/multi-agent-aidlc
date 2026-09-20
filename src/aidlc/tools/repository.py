from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from aidlc.domain.models import utc_now
from aidlc.tools.models import AnalysisReport


class ToolRepository:
    """Durable owner-bound results and a redacted audit trail, independent of MCP sessions."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS analysis_results (
                    analysis_id TEXT PRIMARY KEY, owner TEXT NOT NULL, report_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tool_audit (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                """
            )

    def save(self, owner: str, report: AnalysisReport) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO analysis_results VALUES (?, ?, ?)",
                (report.analysis_id, owner, report.model_dump_json()),
            )

    def read(self, owner: str, analysis_id: str) -> AnalysisReport | None:
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT report_json FROM analysis_results WHERE analysis_id = ? AND owner = ?",
                (analysis_id, owner),
            ).fetchone()
        return AnalysisReport.model_validate_json(row[0]) if row else None

    def audit(self, payload: dict[str, Any]) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO tool_audit(created_at, payload_json) VALUES (?, ?)",
                (utc_now().isoformat(), json.dumps(payload)),
            )

    def audit_history(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as connection:
            rows = connection.execute(
                "SELECT created_at, payload_json FROM tool_audit ORDER BY event_id"
            ).fetchall()
        return [{"created_at": row[0], **json.loads(row[1])} for row in rows]
