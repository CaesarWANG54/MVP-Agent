from __future__ import annotations

import json
import os
import re
import subprocess
from threading import Event, Lock
from time import monotonic, sleep
from datetime import datetime
from pathlib import Path
from typing import Any


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_file(path: Path, payload: dict[str, Any]) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class RunCancelled(RuntimeError):
    """Raised when a user cancels a running MVP job."""


class RunControl:
    def __init__(self) -> None:
        self._cancel_event = Event()
        self._lock = Lock()
        self._processes: set[subprocess.Popen[str]] = set()
        self._reason = "任务已被用户取消。"

    @property
    def cancel_reason(self) -> str:
        return self._reason

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def request_cancel(self, reason: str = "任务已被用户取消。") -> None:
        self._reason = reason.strip() or self._reason
        self._cancel_event.set()
        with self._lock:
            active = list(self._processes)
        for process in active:
            _terminate_process(process)

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise RunCancelled(self._reason)

    def register_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.add(process)
        if self.is_cancelled():
            _terminate_process(process)
            raise RunCancelled(self._reason)

    def unregister_process(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._processes.discard(process)


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                **windows_subprocess_kwargs(),
            )
            deadline = monotonic() + 2.0
            while process.poll() is None and monotonic() < deadline:
                sleep(0.05)
            if process.poll() is None:
                process.kill()
            return
        process.terminate()
        deadline = monotonic() + 1.5
        while process.poll() is None and monotonic() < deadline:
            sleep(0.05)
        if process.poll() is None:
            process.kill()
    except OSError:
        pass


def windows_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        "startupinfo": startupinfo,
    }


def run_subprocess_capture(
    command: list[str],
    *,
    cwd: Path,
    timeout: int,
    input_text: str | None = None,
    run_control: RunControl | None = None,
    env: dict[str, str] | None = None,
    poll_interval: float = 0.25,
) -> subprocess.CompletedProcess[str]:
    stdin = subprocess.PIPE if input_text is not None else None
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        **windows_subprocess_kwargs(),
    )
    if run_control is not None:
        run_control.register_process(process)

    deadline = monotonic() + timeout
    pending_input = input_text
    pi = max(0.05, min(poll_interval, 0.5))
    try:
        while True:
            if run_control is not None:
                run_control.raise_if_cancelled()
            remaining = deadline - monotonic()
            if remaining <= 0:
                _terminate_process(process)
                stdout, stderr = process.communicate()
                raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
            try:
                stdout, stderr = process.communicate(input=pending_input, timeout=min(pi, remaining))
                break
            except subprocess.TimeoutExpired:
                pending_input = None
                continue
    except RunCancelled:
        _terminate_process(process)
        raise
    finally:
        if run_control is not None:
            run_control.unregister_process(process)

    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def clamp_int(value: Any, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def clamp_float(value: Any, minimum: float, maximum: float, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(minimum, min(maximum, parsed))


def normalize_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


from functools import lru_cache


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try cached regex extraction using first 500 chars as key
    cached = _extract_json_cached(text[:500])
    if cached is not None:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass

    fenced_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text, flags=re.IGNORECASE)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"(\{[\s\S]*\})", text)
    if object_match:
        return json.loads(object_match.group(1))

    raise ValueError(f"Could not find JSON object in response: {raw_text[:300]}")


@lru_cache(maxsize=32)
def _extract_json_cached(text_head: str) -> str | None:
    """Cache regex extraction results for common text patterns."""
    fenced_match = re.search(r"```(?:json)?\s*(\{[\s\S]*\})\s*```", text_head, flags=re.IGNORECASE)
    if fenced_match:
        return fenced_match.group(1)
    object_match = re.search(r"(\{[\s\S]*\})", text_head)
    if object_match:
        return object_match.group(1)
    return None


def summarize_stderr(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    return " | ".join(lines[-4:])


def detect_budget_mode(task: str, explicit_mode: str | None) -> str:
    if explicit_mode:
        return explicit_mode

    normalized = task.lower()
    compact = normalized.replace(" ", "")
    if (
        "省api" in compact
        or "省配额" in task
        or "本地优先" in task
        or "saveapi" in compact
        or "low cost" in normalized
        or "cheap" in normalized
    ):
        return "cheap"
    if (
        "最高质量" in task
        or "高质量" in task
        or "最强" in task
        or "premium" in normalized
        or "best quality" in normalized
    ):
        return "premium"
    return "balanced"


def iso_now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso_to_display(value: str | None) -> str:
    if not value:
        return "-"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def epoch_ms_to_display(value: int | float | None) -> str:
    if value in (None, ""):
        return "-"
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return "-"
    return datetime.fromtimestamp(timestamp).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def safe_snippet(value: str | None, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
