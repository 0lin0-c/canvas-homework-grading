"""SQLite-backed grading run store (docs/03 schema)."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.models import GradingRun, StudentResult


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.path = self.settings.resolve_path(self.settings.database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS grading_runs (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def create_run(self, run: GradingRun) -> GradingRun:
        now = _utcnow()
        run.created_at = now
        run.updated_at = now
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO grading_runs (id, payload, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (run.id, run.model_dump_json(), now, now),
            )
            conn.commit()
        self._mirror_json(run)
        return run

    def save_run(self, run: GradingRun) -> GradingRun:
        run.updated_at = _utcnow()
        with self._conn() as conn:
            conn.execute(
                "UPDATE grading_runs SET payload=?, updated_at=? WHERE id=?",
                (run.model_dump_json(), run.updated_at, run.id),
            )
            conn.commit()
        self._mirror_json(run)
        return run

    def get_run(self, run_id: str) -> GradingRun | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM grading_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return GradingRun.model_validate_json(row["payload"])

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, payload, created_at, updated_at FROM grading_runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            data = json.loads(r["payload"])
            out.append(
                {
                    "id": r["id"],
                    "assignment_url": data.get("assignment_url"),
                    "status": data.get("status"),
                    "mode": data.get("mode"),
                    "created_at": r["created_at"],
                    "student_count": len(data.get("students") or []),
                }
            )
        return out

    def get_student(self, run_id: str, student_id: str) -> StudentResult | None:
        run = self.get_run(run_id)
        if not run:
            return None
        for s in run.students:
            if s.student_id == student_id:
                return s
        return None

    def update_student(self, run_id: str, student: StudentResult) -> GradingRun | None:
        run = self.get_run(run_id)
        if not run:
            return None
        for i, s in enumerate(run.students):
            if s.student_id == student.student_id:
                run.students[i] = student
                break
        else:
            run.students.append(student)
        return self.save_run(run)

    def _mirror_json(self, run: GradingRun) -> None:
        """Write sanitized snapshot under outputs/ (gitignored)."""
        out_dir = self.settings.repo_root() / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"run-{run.id}.json"
        data = run.model_dump()
        # Strip attachment URLs (may contain signed query)
        for st in data.get("students") or []:
            sub = st.get("submission") or {}
            for att in sub.get("attachments") or []:
                if att.get("url"):
                    att["url"] = "[redacted]"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


_store: RunStore | None = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore()
    return _store
