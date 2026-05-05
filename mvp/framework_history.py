from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import read_recent_worker_calls
from .utils import (
    iso_to_display,
    load_json_file,
    run_subprocess_capture,
    safe_snippet,
    save_json_file,
)


def _sort_value(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _source_family(source: str) -> str:
    if source.startswith("OpenClaw"):
        return "OpenClaw"
    if source.startswith("Codex"):
        return "Codex"
    return source


def _balance_timeline_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if len(rows) <= limit:
        return rows
    per_family_cap = max(4, limit // 4)
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    counters: dict[str, int] = {}
    for row in rows:
        family = _source_family(str(row.get("source", "")))
        current = counters.get(family, 0)
        if current < per_family_cap:
            counters[family] = current + 1
            selected.append(row)
        else:
            deferred.append(row)
    for row in deferred:
        if len(selected) >= limit:
            break
        selected.append(row)
    return selected[:limit]


def _openclaw_store_cache_path(runs_dir: Path) -> Path:
    return runs_dir / "openclaw_session_stores.json"


def _openclaw_binary() -> str:
    return shutil.which("openclaw.cmd") or shutil.which("openclaw") or "openclaw.cmd"


def _discover_openclaw_store_paths(workspace_root: Path, runs_dir: Path) -> list[Path]:
    del workspace_root
    cache_path = _openclaw_store_cache_path(runs_dir)
    if cache_path.exists():
        cached = load_json_file(cache_path)
        paths = [Path(item) for item in cached.get("paths", []) if Path(item).exists()]
        if paths:
            return paths

    try:
        completed = run_subprocess_capture(
            [_openclaw_binary(), "sessions", "--all-agents", "--json"],
            cwd=runs_dir,
            timeout=45,
        )
    except FileNotFoundError:
        return []
    if completed.returncode != 0:
        return []

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return []

    paths = [
        Path(str(item.get("path")))
        for item in payload.get("stores", [])
        if item.get("path") and Path(str(item.get("path"))).exists()
    ]
    if paths:
        save_json_file(cache_path, {"paths": [str(item) for item in paths]})
    return paths


def _read_jsonl_objects(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def _content_to_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    chunks.append(str(text).strip())
            elif isinstance(item, str):
                chunks.append(item.strip())
        return "\n".join(chunk for chunk in chunks if chunk).strip()
    if isinstance(content, dict):
        text = content.get("text")
        if text:
            return str(text).strip()
    return ""


def _role_label(role: str) -> str:
    mapping = {
        "user": "用户",
        "assistant": "助手",
        "developer": "开发者",
        "system": "系统",
    }
    return mapping.get(role, role or "消息")


def _openclaw_message_rows(session: dict[str, Any], agent_id: str, session_file: Path, per_session: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for item in _read_jsonl_objects(session_file):
        if item.get("type") != "message":
            continue
        message = item.get("message", {})
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        text = _content_to_text(message.get("content"))
        error_message = str(message.get("errorMessage") or "").strip()
        preview = text or error_message
        if not preview:
            continue
        messages.append(
            {
                "role": role,
                "text": preview,
                "timestamp": str(item.get("timestamp") or ""),
                "model": str(message.get("model") or ""),
                "provider": str(message.get("provider") or ""),
            }
        )
    for message in messages[-per_session:]:
        preview = safe_snippet(message["text"], 88) or "-"
        rows.append(
            {
                "timestamp": iso_to_display(message["timestamp"]),
                "sort_key": message["timestamp"],
                "source": "OpenClaw消息",
                "title": f"{agent_id} | {_role_label(message['role'])}",
                "subtitle": preview,
                "detail": "\n".join(
                    [
                        f"角色：{_role_label(message['role'])}",
                        f"摘要：{preview}",
                        f"模型：{message['provider']}/{message['model']}".rstrip("/"),
                        f"会话：{session.get('sessionId', '-')}",
                        f"文件：{session_file}",
                        "",
                        message["text"],
                    ]
                ).strip(),
                "kind": "framework_message",
                "framework": "OpenClaw",
                "agent": agent_id,
                "message_role": message["role"],
                "session_id": str(session.get("sessionId", "")),
                "source_path": str(session_file),
            }
        )
    return rows


def read_openclaw_sessions(workspace_root: Path, runs_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    session_rows: list[dict[str, Any]] = []
    sessions: list[dict[str, Any]] = []
    for store_path in _discover_openclaw_store_paths(workspace_root, runs_dir):
        try:
            payload = load_json_file(store_path)
        except Exception:
            continue
        for key, session in payload.items():
            if not isinstance(session, dict):
                continue
            parts = str(key).split(":")
            agent_id = parts[1] if len(parts) > 1 else key
            sessions.append({"agent_id": agent_id, "session": session})
    sessions.sort(key=lambda item: _sort_value(item["session"].get("updatedAt")), reverse=True)
    for item in sessions[: max(4, limit)]:
        session = item["session"]
        agent_id = item["agent_id"]
        channel = str(session.get("lastChannel") or session.get("deliveryContext", {}).get("channel") or "-")
        session_file = Path(str(session.get("sessionFile", "")))
        skill_count = len(session.get("skillsSnapshot", {}).get("skills", []))
        session_rows.append(
            {
                "timestamp": iso_to_display(
                    datetime.fromtimestamp(_sort_value(session.get("updatedAt")) / 1000.0).astimezone().isoformat()
                )
                if session.get("updatedAt")
                else "-",
                "sort_key": _sort_value(session.get("updatedAt")),
                "source": "OpenClaw",
                "title": f"{agent_id} | {channel}",
                "subtitle": f"会话 {session.get('sessionId', '-')}",
                "detail": "\n".join(
                    [
                        f"认证配置：{session.get('authProfileOverride', 'unknown')}",
                        f"来源：{session.get('origin', {}).get('label', '-')}",
                        f"会话文件：{session_file or '-'}",
                        f"技能数：{skill_count}",
                    ]
                ),
                "kind": "framework_session",
                "framework": "OpenClaw",
                "agent": agent_id,
                "source_path": str(session_file),
            }
        )
        if session_file.exists():
            session_rows.extend(_openclaw_message_rows(session, agent_id, session_file))
    session_rows.sort(key=lambda item: _sort_value(item.get("sort_key")), reverse=True)
    return session_rows[:limit]


def _read_codex_session_meta(path: Path) -> dict[str, Any] | None:
    meta = None
    last_message = ""
    for item in _read_jsonl_objects(path):
        item_type = str(item.get("type") or "")
        if item_type == "session_meta":
            meta = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
        if item_type == "event_msg":
            payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
            if payload.get("type") == "agent_message":
                last_message = str(payload.get("message") or "").strip() or last_message
    if not isinstance(meta, dict):
        return None
    return {
        "session_id": str(meta.get("id", "")),
        "timestamp": str(meta.get("timestamp", "")),
        "cwd": str(meta.get("cwd", "")),
        "cli_version": str(meta.get("cli_version", "")),
        "model_provider": str(meta.get("model_provider", "")),
        "path": str(path),
        "last_message": last_message,
    }


def _codex_message_rows(path: Path, session_id: str, cwd: str, per_session: int = 4) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for item in _read_jsonl_objects(path):
        if item.get("type") != "event_msg":
            continue
        payload = item.get("payload", {}) if isinstance(item.get("payload"), dict) else {}
        event_type = str(payload.get("type") or "")
        if event_type == "user_message":
            role = "user"
        elif event_type == "agent_message":
            role = "assistant"
        else:
            continue
        text = str(payload.get("message") or "").strip()
        if not text:
            continue
        messages.append(
            {
                "role": role,
                "text": text,
                "timestamp": str(item.get("timestamp") or ""),
                "phase": str(payload.get("phase") or ""),
            }
        )
    rows: list[dict[str, Any]] = []
    for message in messages[-per_session:]:
        preview = safe_snippet(message["text"], 88) or "-"
        rows.append(
            {
                "timestamp": iso_to_display(message["timestamp"]),
                "sort_key": message["timestamp"],
                "source": "Codex消息",
                "title": f"{session_id[:8]} | {_role_label(message['role'])}",
                "subtitle": preview,
                "detail": "\n".join(
                    [
                        f"角色：{_role_label(message['role'])}",
                        f"阶段：{message['phase'] or '-'}",
                        f"工作区：{cwd or '-'}",
                        f"文件：{path}",
                        "",
                        message["text"],
                    ]
                ).strip(),
                "kind": "framework_message",
                "framework": "Codex",
                "agent": "codex",
                "message_role": message["role"],
                "session_id": session_id,
                "source_path": str(path),
            }
        )
    return rows


def read_codex_sessions(limit: int = 20) -> list[dict[str, Any]]:
    root = Path.home() / ".codex" / "sessions"
    if not root.exists():
        return []

    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda item: item.stat().st_mtime, reverse=True)
    rows: list[dict[str, Any]] = []
    for path in files[: max(limit * 2, 8)]:
        meta = _read_codex_session_meta(path)
        if not meta:
            continue
        rows.append(
            {
                "timestamp": iso_to_display(meta["timestamp"]),
                "sort_key": path.stat().st_mtime,
                "source": "Codex",
                "title": f"会话 {meta['session_id'][:8]}",
                "subtitle": f"{meta['model_provider']} | {Path(meta['cwd']).name or meta['cwd']}",
                "detail": "\n".join(
                    [
                        f"CLI：{meta['cli_version'] or '-'}",
                        f"工作区：{meta['cwd'] or '-'}",
                        f"文件：{meta['path']}",
                        f"最近回复：{safe_snippet(meta['last_message'], 220) or '-'}",
                    ]
                ),
                "kind": "framework_session",
                "framework": "Codex",
                "agent": "codex",
                "source_path": meta["path"],
            }
        )
        rows.extend(_codex_message_rows(path, meta["session_id"], meta["cwd"]))
        if len(rows) >= limit * 3:
            break
    rows.sort(key=lambda item: _sort_value(item.get("sort_key")), reverse=True)
    return rows[:limit]


def read_mvp_reports(runs_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reports = sorted(runs_dir.glob("mvp_run_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    for path in reports[:limit]:
        try:
            payload = load_json_file(path)
        except Exception:
            continue
        rows.append(
            {
                "timestamp": iso_to_display(datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()),
                "sort_key": path.stat().st_mtime,
                "source": "MVP",
                "title": safe_snippet(str(payload.get("task", "")), 72) or path.stem,
                "subtitle": f"{payload.get('profile', 'balanced')} | {payload.get('status', 'unknown')}",
                "detail": "\n".join(
                    [
                        f"任务：{payload.get('task', '-')}",
                        f"总结：{payload.get('leader_notes', '-')}",
                        f"报告：{path}",
                    ]
                ),
                "kind": "mvp_report",
                "framework": "MVP",
                "agent": "leader",
                "report_path": str(path),
            }
        )
    return rows


def read_worker_call_timeline(runs_dir: Path, limit: int = 30) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in read_recent_worker_calls(runs_dir, limit=limit):
        rows.append(
            {
                "timestamp": iso_to_display(str(item.get("timestamp", ""))),
                "sort_key": str(item.get("timestamp", "")),
                "source": str(item.get("framework", "Worker")),
                "title": f"{item.get('worker_id', 'unknown')} | {item.get('status', 'unknown')}",
                "subtitle": f"{item.get('target', '-')} | {item.get('access_mode', '未标注权限')}",
                "detail": "\n".join(
                    [
                        f"阶段：{item.get('stage', '-')}",
                        f"耗时：{item.get('duration_ms', '-')} ms",
                        f"可写工作区：{item.get('allow_write', False)}",
                        f"访问模式：{item.get('access_mode', '未标注权限')}",
                        f"摘要：{safe_snippet(str(item.get('summary', '')), 220)}",
                    ]
                ),
                "kind": "worker_call",
                "framework": str(item.get("framework", "Worker")),
                "agent": str(item.get("worker_id", "unknown")),
            }
        )
    rows.sort(key=lambda item: _sort_value(item.get("sort_key")), reverse=True)
    return rows[:limit]


def read_framework_timeline(workspace_root: Path, runs_dir: Path, limit: int = 60) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rows.extend(read_mvp_reports(runs_dir, limit=limit))
    rows.extend(read_worker_call_timeline(runs_dir, limit=limit))
    rows.extend(read_openclaw_sessions(workspace_root, runs_dir, limit=limit))
    rows.extend(read_codex_sessions(limit=limit))
    rows.sort(key=lambda item: _sort_value(item.get("sort_key")), reverse=True)
    return _balance_timeline_rows(rows, limit)
