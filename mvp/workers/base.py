from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any

from ..models import WorkerSpec
from ..utils import RunControl


class WorkerError(RuntimeError):
    """Raised when a worker invocation fails."""


@dataclass(slots=True)
class WorkerHealth:
    worker_id: str
    ok: bool
    summary: str


class BaseWorker(ABC):
    QUICK_PING_TEXT = "Reply with exactly: MVP_QUICK_PING_OK"
    DEEP_PING_PROMPT = (
        'Return one compact JSON object only: '
        '{"summary":"deep ping ok","deliverable":"worker reachable","confidence":0.95}'
    )

    def __init__(self, spec: WorkerSpec, workspace_root: Path, runs_dir: Path) -> None:
        self.spec = spec
        self.workspace_root = workspace_root
        self.runs_dir = runs_dir

    @abstractmethod
    def invoke_text(self, prompt: str, allow_write: bool = False, run_control: RunControl | None = None) -> str:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> WorkerHealth:
        raise NotImplementedError

    def invoke_text_mode(
        self,
        prompt: str,
        allow_write: bool = False,
        run_control: RunControl | None = None,
        mode: str = "default",
    ) -> str:
        del mode
        return self.invoke_text(prompt, allow_write=allow_write, run_control=run_control)

    def invoke_json(self, prompt: str, allow_write: bool = False, run_control: RunControl | None = None) -> dict[str, Any]:
        from ..utils import extract_json_object

        raw_text = self.invoke_text(prompt, allow_write=allow_write, run_control=run_control)
        parsed = extract_json_object(raw_text)
        parsed["_raw_response"] = raw_text
        return parsed

    def invoke_json_mode(
        self,
        prompt: str,
        allow_write: bool = False,
        run_control: RunControl | None = None,
        mode: str = "default",
    ) -> dict[str, Any]:
        from ..utils import extract_json_object

        raw_text = self.invoke_text_mode(
            prompt,
            allow_write=allow_write,
            run_control=run_control,
            mode=mode,
        )
        parsed = extract_json_object(raw_text)
        parsed["_raw_response"] = raw_text
        return parsed

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, Any]:
        started = perf_counter()
        try:
            text = self.invoke_text(self.QUICK_PING_TEXT, allow_write=False, run_control=run_control).strip()
            return {
                "ok": True,
                "summary": "quick ping ok",
                "deliverable": text[:220],
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, Any]:
        started = perf_counter()
        try:
            payload = self.invoke_json(self.DEEP_PING_PROMPT, allow_write=False, run_control=run_control)
            return {
                "ok": True,
                "summary": str(payload.get("summary") or "deep ping ok").strip(),
                "deliverable": str(payload.get("deliverable") or "").strip(),
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
