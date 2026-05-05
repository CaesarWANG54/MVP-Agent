from __future__ import annotations

import json
from statistics import mean
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

from .utils import ensure_directory, iso_now_local, load_jsonl


WORKER_CALLS_FILE = "worker_calls.jsonl"
_AUDIT_LOCK = Lock()
_AUDIT_BUFFERS: dict[str, list[dict[str, Any]]] = {}
_AUDIT_BUFFER_LIMIT = 20
_AUDIT_LAST_FLUSH: dict[str, float] = {}
_AUDIT_FLUSH_INTERVAL = 5.0


def worker_calls_path(runs_dir: Path) -> Path:
    return runs_dir / WORKER_CALLS_FILE


def _buffer_key(runs_dir: Path) -> str:
    return str(runs_dir.resolve()).lower()


def _flush_audit_buffer(runs_dir: Path) -> None:
    key = _buffer_key(runs_dir)
    with _AUDIT_LOCK:
        rows = _AUDIT_BUFFERS.get(key, [])
        if not rows:
            _AUDIT_LAST_FLUSH[key] = monotonic()
            return
        _AUDIT_BUFFERS[key] = []
        _AUDIT_LAST_FLUSH[key] = monotonic()
    if not rows:
        return
    path = worker_calls_path(runs_dir)
    ensure_directory(path.parent)
    with path.open("a", encoding="utf-8") as handle:
        for record in rows:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def append_worker_call(runs_dir: Path, payload: dict[str, Any], *, buffered: bool = False) -> None:
    record = {"timestamp": iso_now_local(), **payload}
    key = _buffer_key(runs_dir)
    now = monotonic()
    with _AUDIT_LOCK:
        buffer = _AUDIT_BUFFERS.setdefault(key, [])
        buffer.append(record)
        last_flush = _AUDIT_LAST_FLUSH.setdefault(key, now)
        should_flush = (
            not buffered
            or len(buffer) >= _AUDIT_BUFFER_LIMIT
            or now - last_flush > _AUDIT_FLUSH_INTERVAL
        )
    if should_flush:
        _flush_audit_buffer(runs_dir)


def flush_audit(runs_dir: Path) -> None:
    """Force-flush buffered audit records. Call after job completion."""
    _flush_audit_buffer(runs_dir)


def read_recent_worker_calls(runs_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    rows = load_jsonl(worker_calls_path(runs_dir))
    rows.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    return rows[:limit]


def summarize_worker_metrics_by_kind(
    runs_dir: Path, limit: int = 30
) -> dict[str, dict[str, dict[str, float]]]:
    """Per-kind worker metrics derived from RunReport JSON files.

    Returns: {worker_id: {kind: {success_rate, pass_rate, avg_confidence, samples}}}
    """
    report_paths = sorted(runs_dir.glob("mvp_run_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for rp in report_paths[:limit]:
        try:
            report = json.loads(rp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for outcome in report.get("outcomes", []):
            assignment = outcome.get("assignment", {})
            worker_id = str(assignment.get("worker_id", "")).strip()
            subtask = assignment.get("subtask", {})
            kind = str(subtask.get("kind", "")).strip().lower()
            result = outcome.get("result", {})
            review = outcome.get("review", {}) or {}
            if not worker_id or not kind:
                continue
            grouped.setdefault(worker_id, {}).setdefault(kind, []).append({
                "status": str(result.get("status", "")).strip().lower(),
                "confidence": float(result.get("confidence", 0.0) or 0.0),
                "review_decision": str(review.get("decision", "")).strip().lower(),
            })

    metrics: dict[str, dict[str, dict[str, float]]] = {}
    for worker_id, kind_map in grouped.items():
        metrics[worker_id] = {}
        for kind, records in kind_map.items():
            if not records:
                continue
            completed = sum(1 for r in records if r["status"] == "completed")
            passed = sum(1 for r in records if r["review_decision"] == "pass")
            confidences = [r["confidence"] for r in records if r["confidence"] > 0]
            metrics[worker_id][kind] = {
                "success_rate": round(completed / len(records), 3),
                "pass_rate": round(passed / len(records), 3),
                "avg_confidence": round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
                "samples": float(len(records)),
            }
    return metrics


def summarize_worker_metrics(runs_dir: Path, limit: int = 80) -> dict[str, dict[str, float]]:
    rows = [
        row
        for row in read_recent_worker_calls(runs_dir, limit=limit)
        if str(row.get("stage", "invoke")).lower().startswith("invoke")
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        worker_id = str(row.get("worker_id", "")).strip()
        if not worker_id:
            continue
        grouped.setdefault(worker_id, []).append(row)

    metrics: dict[str, dict[str, float]] = {}
    for worker_id, worker_rows in grouped.items():
        durations = [
            float(row.get("duration_ms"))
            for row in worker_rows
            if isinstance(row.get("duration_ms"), (int, float))
        ]
        completed = [row for row in worker_rows if str(row.get("status", "")).lower() == "completed"]
        failed = [row for row in worker_rows if str(row.get("status", "")).lower() in {"failed", "error", "empty", "blocked"}]
        if not durations:
            continue
        metrics[worker_id] = {
            "avg_duration_ms": round(mean(durations), 2),
            "recent_duration_ms": float(durations[0]),
            "completed_ratio": round(len(completed) / len(worker_rows), 3) if worker_rows else 0.0,
            "failure_ratio": round(len(failed) / len(worker_rows), 3) if worker_rows else 0.0,
            "samples": float(len(worker_rows)),
        }
    return metrics
