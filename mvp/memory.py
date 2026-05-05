"""Persistent long-term memory for MVP Agent — survives across runs.

Uses SQLite for storage. Tracks conversations, worker performance,
codebase knowledge, and task decomposition patterns.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class MemoryStats:
    total_tasks: int = 0
    total_workers: int = 0
    top_workers: list[dict[str, Any]] = field(default_factory=list)
    recent_patterns: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task        TEXT    NOT NULL,
    profile     TEXT    NOT NULL,
    plan_json   TEXT    NOT NULL,
    outcomes_json TEXT  NOT NULL,
    status      TEXT    NOT NULL,
    leader_notes TEXT   NOT NULL DEFAULT '',
    tags        TEXT    NOT NULL DEFAULT '[]',
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_perf (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id     TEXT    NOT NULL,
    task_kind     TEXT    NOT NULL,
    success       INTEGER NOT NULL,
    duration_ms   INTEGER NOT NULL,
    review_decision TEXT  NOT NULL DEFAULT '',
    task_id       TEXT    NOT NULL DEFAULT '',
    recorded_at   REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS task_patterns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_sig      TEXT    NOT NULL,
    subtask_kinds TEXT    NOT NULL,
    worker_assignments TEXT NOT NULL DEFAULT '[]',
    success       INTEGER NOT NULL,
    used_count    INTEGER NOT NULL DEFAULT 1,
    last_used_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS code_memory (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path     TEXT    NOT NULL,
    symbols       TEXT    NOT NULL DEFAULT '[]',
    dependencies  TEXT    NOT NULL DEFAULT '[]',
    summary       TEXT    NOT NULL DEFAULT '',
    category      TEXT    NOT NULL DEFAULT 'unknown',
    last_seen_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_learning (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_sig        TEXT    NOT NULL,
    required_skills TEXT    NOT NULL DEFAULT '[]',
    capability_gaps TEXT    NOT NULL DEFAULT '[]',
    success         INTEGER NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_created ON conversations(created_at);
CREATE INDEX IF NOT EXISTS idx_worker_perf_worker    ON worker_perf(worker_id, task_kind);
CREATE INDEX IF NOT EXISTS idx_worker_perf_recorded  ON worker_perf(recorded_at);
CREATE INDEX IF NOT EXISTS idx_task_patterns_sig     ON task_patterns(task_sig);
CREATE INDEX IF NOT EXISTS idx_code_memory_path      ON code_memory(file_path);
CREATE INDEX IF NOT EXISTS idx_code_memory_category  ON code_memory(category);
CREATE INDEX IF NOT EXISTS idx_skill_learning_sig    ON skill_learning(task_sig);
CREATE INDEX IF NOT EXISTS idx_skill_learning_created ON skill_learning(created_at);
"""


# ---------------------------------------------------------------------------
# MemoryStore
# ---------------------------------------------------------------------------

class MemoryStore:
    """Persistent long-term memory backed by SQLite.

    Thread-safe. Auto-creates the database on first use.
    """

    def __init__(self, db_path: str | Path, max_history: int = 5000) -> None:
        self._db_path = Path(db_path)
        self._max_history = max_history
        self._lock = threading.Lock()
        self._local = threading.local()

    # -- connection management -------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            self._local.conn = conn
        return self._local.conn

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn().execute(sql, params)

    def _execute_many(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        with self._lock:
            return self._conn().executemany(sql, seq)

    # -- conversation memory ---------------------------------------------------

    def remember_task(
        self,
        task: str,
        profile: str,
        plan_json: dict,
        outcomes_json: list[dict],
        status: str,
        leader_notes: str = "",
        tags: list[str] | None = None,
    ) -> int:
        tags_json = json.dumps(tags or [], ensure_ascii=False)
        cur = self._execute(
            """INSERT INTO conversations (task, profile, plan_json, outcomes_json, status, leader_notes, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (task, profile, json.dumps(plan_json, ensure_ascii=False),
             json.dumps(outcomes_json, ensure_ascii=False), status, leader_notes,
             tags_json, time.time()),
        )
        self._conn().commit()
        self._prune_old("conversations", "created_at")
        return cur.lastrowid or 0

    def recall_similar_tasks(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Simple keyword-overlap recall. Returns the most relevant past tasks."""
        tokens = _tokenize(query)
        if not tokens:
            return self._recent_tasks(limit)
        rows = self._execute(
            "SELECT id, task, profile, status, leader_notes, tags, outcomes_json, created_at FROM conversations ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            row_tokens = _tokenize(row[1])
            overlap = len(tokens & row_tokens)
            if overlap == 0:
                continue
            score = overlap * 2.0
            if row[3] == "completed":
                score += 1.0
            tags_data = json.loads(row[5]) if row[5] else []
            tag_overlap = len(tokens & set(t.lower() for t in tags_data))
            score += tag_overlap * 3.0
            scored.append((score, {
                "id": row[0], "task": row[1], "profile": row[2],
                "status": row[3], "leader_notes": row[4], "tags": tags_data,
                "outcomes": json.loads(row[6]) if row[6] else [],
                "created_at": row[7],
            }))
        scored.sort(key=lambda x: -x[0])
        return [item for _, item in scored[:limit]]

    def recent_conversations(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT id, task, profile, status, leader_notes, tags, created_at FROM conversations ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"id": r[0], "task": r[1], "profile": r[2], "status": r[3],
             "leader_notes": r[4], "tags": json.loads(r[5]) if r[5] else [],
             "created_at": r[6]}
            for r in rows
        ]

    def _recent_tasks(self, limit: int = 5) -> list[dict[str, Any]]:
        return self.recent_conversations(limit=limit)

    # -- worker performance memory ---------------------------------------------

    def record_worker_perf(
        self,
        worker_id: str,
        task_kind: str,
        success: bool,
        duration_ms: int,
        review_decision: str = "",
        task_id: str = "",
    ) -> None:
        self._execute(
            """INSERT INTO worker_perf (worker_id, task_kind, success, duration_ms, review_decision, task_id, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (worker_id, task_kind, int(success), duration_ms, review_decision, task_id, time.time()),
        )
        self._conn().commit()
        self._prune_old("worker_perf", "recorded_at")

    def worker_stats(self, worker_id: str) -> dict[str, Any]:
        row = self._execute(
            "SELECT COUNT(*), AVG(duration_ms), SUM(success) FROM worker_perf WHERE worker_id = ?",
            (worker_id,),
        ).fetchone()
        total = row[0] or 0
        return {
            "worker_id": worker_id,
            "total_calls": total,
            "avg_duration_ms": round(row[1] or 0, 1),
            "success_rate": round((row[2] or 0) / max(total, 1), 3),
        }

    def worker_kind_stats(self, worker_id: str, task_kind: str) -> dict[str, Any]:
        row = self._execute(
            "SELECT COUNT(*), SUM(success), AVG(duration_ms) FROM worker_perf WHERE worker_id = ? AND task_kind = ?",
            (worker_id, task_kind),
        ).fetchone()
        total = row[0] or 0
        return {
            "worker_id": worker_id,
            "task_kind": task_kind,
            "samples": total,
            "pass_rate": round((row[1] or 0) / max(total, 1), 3),
            "avg_duration_ms": round(row[2] or 0, 1),
        }

    def all_worker_stats(self) -> dict[str, dict[str, Any]]:
        rows = self._execute(
            "SELECT worker_id, COUNT(*), AVG(duration_ms), SUM(success) FROM worker_perf GROUP BY worker_id"
        ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            total = row[1]
            result[row[0]] = {
                "total_calls": total,
                "avg_duration_ms": round(row[2] or 0, 1),
                "success_rate": round((row[3] or 0) / max(total, 1), 3),
            }
        return result

    def rank_workers_for_kind(self, task_kind: str) -> list[dict[str, Any]]:
        rows = self._execute(
            """SELECT worker_id, COUNT(*) as n, SUM(success) as s
               FROM worker_perf WHERE task_kind = ? GROUP BY worker_id
               ORDER BY n DESC LIMIT 15""",
            (task_kind,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            total = row[1]
            rate = round((row[2] or 0) / max(total, 1), 3)
            result.append({"worker_id": row[0], "samples": total, "success_rate": rate})
        result.sort(key=lambda x: (-x["success_rate"], -x["samples"]))
        return result

    # -- task pattern memory ---------------------------------------------------

    def learn_pattern(
        self,
        task: str,
        subtask_kinds: list[str],
        worker_assignments: list[str],
        success: bool,
    ) -> None:
        sig = _task_signature(task)
        cur = self._execute(
            "SELECT id, used_count FROM task_patterns WHERE task_sig = ?", (sig,)
        )
        existing = cur.fetchone()
        if existing:
            self._execute(
                """UPDATE task_patterns
                   SET subtask_kinds = ?, worker_assignments = ?, success = ?,
                       used_count = ?, last_used_at = ?
                   WHERE id = ?""",
                (json.dumps(subtask_kinds, ensure_ascii=False),
                 json.dumps(worker_assignments, ensure_ascii=False),
                 int(success), existing[1] + 1, time.time(), existing[0]),
            )
        else:
            self._execute(
                """INSERT INTO task_patterns (task_sig, subtask_kinds, worker_assignments, success, used_count, last_used_at)
                   VALUES (?, ?, ?, ?, 1, ?)""",
                (sig, json.dumps(subtask_kinds, ensure_ascii=False),
                 json.dumps(worker_assignments, ensure_ascii=False), int(success), time.time()),
            )
        self._conn().commit()

    def recall_pattern(self, task: str) -> dict[str, Any] | None:
        sig = _task_signature(task)
        row = self._execute(
            "SELECT subtask_kinds, worker_assignments, success, used_count FROM task_patterns WHERE task_sig = ? ORDER BY used_count DESC LIMIT 1",
            (sig,),
        ).fetchone()
        if not row:
            return None
        return {
            "subtask_kinds": json.loads(row[0]) if row[0] else [],
            "worker_assignments": json.loads(row[1]) if row[1] else [],
            "success": bool(row[2]),
            "used_count": row[3],
        }

    # -- skill learning -------------------------------------------------------

    def remember_skill_profile(
        self,
        task: str,
        required_skills: list[str],
        capability_gaps: list[str],
        success: bool,
    ) -> None:
        self._execute(
            """INSERT INTO skill_learning (task_sig, required_skills, capability_gaps, success, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                _task_signature(task),
                json.dumps(required_skills, ensure_ascii=False),
                json.dumps(capability_gaps, ensure_ascii=False),
                int(success),
                time.time(),
            ),
        )
        self._conn().commit()
        self._prune_old("skill_learning", "created_at")

    def recall_skill_profile(self, task: str) -> dict[str, Any] | None:
        row = self._execute(
            """SELECT required_skills, capability_gaps, success, created_at
               FROM skill_learning WHERE task_sig = ?
               ORDER BY success DESC, created_at DESC LIMIT 1""",
            (_task_signature(task),),
        ).fetchone()
        if not row:
            return None
        return {
            "required_skills": json.loads(row[0]) if row[0] else [],
            "capability_gaps": json.loads(row[1]) if row[1] else [],
            "success": bool(row[2]),
            "created_at": row[3],
        }

    def top_capability_gaps(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._execute(
            "SELECT capability_gaps FROM skill_learning ORDER BY created_at DESC LIMIT 300"
        ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            for gap in json.loads(row[0]) if row[0] else []:
                key = str(gap).strip()
                if not key:
                    continue
                counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [{"gap": gap, "count": count} for gap, count in ranked[:limit]]

    # -- code memory -----------------------------------------------------------

    def remember_code(
        self,
        file_path: str,
        symbols: list[str],
        dependencies: list[str],
        summary: str = "",
        category: str = "unknown",
    ) -> None:
        self._execute(
            """INSERT OR REPLACE INTO code_memory (file_path, symbols, dependencies, summary, category, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (file_path, json.dumps(symbols, ensure_ascii=False),
             json.dumps(dependencies, ensure_ascii=False), summary, category, time.time()),
        )
        self._conn().commit()

    def find_files_by_symbol(self, symbol_name: str) -> list[str]:
        rows = self._execute(
            "SELECT file_path FROM code_memory WHERE symbols LIKE ? LIMIT 20",
            (f"%{symbol_name}%",),
        ).fetchall()
        return [r[0] for r in rows]

    def find_files_by_category(self, category: str) -> list[str]:
        rows = self._execute(
            "SELECT file_path FROM code_memory WHERE category = ? LIMIT 50",
            (category,),
        ).fetchall()
        return [r[0] for r in rows]

    def code_summary(self, file_path: str) -> dict[str, Any] | None:
        row = self._execute(
            "SELECT symbols, dependencies, summary, category FROM code_memory WHERE file_path = ?",
            (file_path,),
        ).fetchone()
        if not row:
            return None
        return {
            "symbols": json.loads(row[0]) if row[0] else [],
            "dependencies": json.loads(row[1]) if row[1] else [],
            "summary": row[2],
            "category": row[3],
        }

    # -- agent state (key-value) -----------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self._execute(
            "INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn().commit()

    def get_state(self, key: str, default: str = "") -> str:
        row = self._execute(
            "SELECT value FROM agent_state WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else default

    # -- stats -----------------------------------------------------------------

    def stats(self) -> MemoryStats:
        total_tasks = self._execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        total_workers = self._execute("SELECT COUNT(DISTINCT worker_id) FROM worker_perf").fetchone()[0]
        top_rows = self._execute(
            "SELECT worker_id, COUNT(*) as n, SUM(success) as s FROM worker_perf GROUP BY worker_id ORDER BY n DESC LIMIT 8"
        ).fetchall()
        top_workers = [
            {"worker_id": r[0], "calls": r[1], "success_rate": round((r[2] or 0) / max(r[1], 1), 3)}
            for r in top_rows
        ]
        patterns = self._execute(
            "SELECT task_sig FROM task_patterns ORDER BY last_used_at DESC LIMIT 8"
        ).fetchall()
        return MemoryStats(
            total_tasks=total_tasks,
            total_workers=total_workers,
            top_workers=top_workers,
            recent_patterns=[r[0] for r in patterns],
        )

    # -- maintenance -----------------------------------------------------------

    def _prune_old(self, table: str, col: str) -> None:
        count_row = self._execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        if (count_row[0] or 0) <= self._max_history:
            return
        keep = self._max_history
        self._execute(
            f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM {table} ORDER BY {col} DESC LIMIT ?)",
            (keep,),
        )
        self._conn().commit()

    def compact(self) -> None:
        with self._lock:
            self._conn().execute("PRAGMA optimize")
            self._conn().execute("VACUUM")

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    import re
    tokens = re.findall(r"[a-zA-Z一-鿿_][a-zA-Z0-9_一-鿿]*", text.lower())
    return set(tokens)


def _task_signature(task: str) -> str:
    """Create a normalized signature for pattern matching."""
    import re
    lowered = task.lower().strip()
    # Normalize whitespace
    lowered = re.sub(r"\s+", " ", lowered)
    # Extract key tokens: alphanumeric + CJK
    tokens = re.findall(r"[a-z0-9_一-鿿]+", lowered)
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for t in tokens:
        if t not in seen and len(t) >= 2:
            seen.add(t)
            unique.append(t)
    sig = " ".join(unique[:12])
    return sig if sig else lowered[:80]
