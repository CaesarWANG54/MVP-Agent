from __future__ import annotations

import json
import os
import shutil
from time import perf_counter
from typing import Any

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object, run_subprocess_capture, summarize_stderr
from .base import BaseWorker, WorkerError, WorkerHealth


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ExternalCliAgentWorker(BaseWorker):
    INVOKE_COMMAND_KEY = "invoke_command"
    HEALTH_COMMAND_KEY = "health_command"
    QUICK_PING_COMMAND_KEY = "quick_ping_command"
    DEEP_PING_COMMAND_KEY = "deep_ping_command"

    def _binary(self) -> str:
        binary = str(self.spec.config.get("binary", "")).strip()
        if not binary:
            raise WorkerError(f"External CLI worker '{self.spec.worker_id}' is missing config.binary.")
        return shutil.which(binary) or binary

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        extra = self.spec.config.get("env", {})
        if isinstance(extra, dict):
            for key, value in extra.items():
                if str(key).strip():
                    env[str(key).strip()] = str(value)
        return env

    def _timeout_for_mode(self, mode: str) -> int:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return int(self.spec.config.get("quick_timeout", self.spec.config.get("timeout", 120)))
        if normalized == "deep":
            return int(self.spec.config.get("deep_timeout", self.spec.config.get("timeout", 120)))
        return int(self.spec.config.get("timeout", 120))

    def _allow_write_value(self, allow_write: bool) -> str:
        if allow_write and self.spec.supports_workspace_write:
            return str(self.spec.config.get("write_mode_value", "write"))
        return str(self.spec.config.get("read_mode_value", "read"))

    def _context(self, prompt: str, allow_write: bool, mode: str) -> dict[str, str]:
        return {
            "prompt": prompt,
            "mode": mode.strip().lower() or "default",
            "allow_write": "true" if allow_write and self.spec.supports_workspace_write else "false",
            "write_mode": self._allow_write_value(allow_write),
            "worker_id": self.spec.worker_id,
            "display_name": self.spec.display_name,
            "workspace_root": str(self.workspace_root),
            "model": str(self.spec.config.get("model", "")).strip(),
        }

    def _render(self, value: str, context: dict[str, str]) -> str:
        return value.format_map(_SafeFormatDict(context))

    def _command_for(self, key: str, prompt: str, allow_write: bool, mode: str) -> tuple[list[str], str | None]:
        raw = self.spec.config.get(key)
        if not isinstance(raw, list) or not raw:
            raise WorkerError(f"External CLI worker '{self.spec.worker_id}' is missing config.{key}.")
        context = self._context(prompt, allow_write, mode)
        command = [self._render(str(item), context) for item in raw]
        prompt_transport = str(self.spec.config.get("prompt_transport", "stdin")).strip().lower()
        if prompt_transport == "inline":
            return command, None
        return command, prompt

    def _extract_text(self, stdout: str, stderr: str) -> str:
        parser = str(self.spec.config.get("response_parser", "text")).strip().lower()
        blob = stdout.strip() or stderr.strip()
        if not blob:
            raise WorkerError("External CLI returned an empty response.")
        if parser == "json_field":
            payload = json.loads(blob)
            text_field = str(self.spec.config.get("response_text_field", "result")).strip()
            text = str(payload.get(text_field) or "").strip()
            if text:
                return text
            raise WorkerError(f"External CLI returned JSON without '{text_field}'.")
        return blob

    def _run_command(
        self,
        key: str,
        *,
        prompt: str,
        allow_write: bool,
        mode: str,
        run_control: RunControl | None,
    ) -> str:
        command, input_text = self._command_for(key, prompt, allow_write, mode)
        timeout = self._timeout_for_mode(mode)
        completed = run_subprocess_capture(
            command,
            cwd=self.workspace_root,
            timeout=timeout,
            input_text=input_text,
            run_control=run_control,
            env=self._env(),
        )
        if completed.returncode != 0:
            raise WorkerError(summarize_stderr(completed.stderr) or completed.stdout.strip() or "External CLI returned an error.")
        return self._extract_text(completed.stdout, completed.stderr)

    def _record(self, stage: str, started: float, allow_write: bool, status: str, summary: str, access_mode: str) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": str(self.spec.platform.get("framework_label") or self.spec.display_name),
                "target": str(self.spec.platform.get("target_label") or self.spec.config.get("model") or self.spec.worker_id),
                "stage": stage,
                "status": status,
                "allow_write": allow_write,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "access_mode": access_mode,
                "invocation_mode": stage.split(":", 1)[1] if ":" in stage else "default",
            },
            buffered=stage == "quick_ping",
        )

    def invoke_text(self, prompt: str, allow_write: bool = False, run_control: RunControl | None = None) -> str:
        return self.invoke_text_mode(prompt, allow_write=allow_write, run_control=run_control, mode="default")

    def invoke_text_mode(
        self,
        prompt: str,
        allow_write: bool = False,
        run_control: RunControl | None = None,
        mode: str = "default",
    ) -> str:
        started = perf_counter()
        normalized_mode = mode.strip().lower() or "default"
        access_mode = self._allow_write_value(allow_write)
        try:
            text = self._run_command(
                self.INVOKE_COMMAND_KEY,
                prompt=prompt,
                allow_write=allow_write,
                mode=normalized_mode,
                run_control=run_control,
            )
        except Exception as exc:
            self._record(f"invoke:{normalized_mode}", started, allow_write, "failed", str(exc), access_mode)
            raise WorkerError(f"External CLI worker '{self.spec.worker_id}' failed: {exc}") from exc
        self._record(f"invoke:{normalized_mode}", started, allow_write, "completed", text, access_mode)
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        try:
            if isinstance(self.spec.config.get(self.QUICK_PING_COMMAND_KEY), list):
                text = self._run_command(
                    self.QUICK_PING_COMMAND_KEY,
                    prompt=self.QUICK_PING_TEXT,
                    allow_write=False,
                    mode="quick",
                    run_control=run_control,
                )
                summary = text[:220]
            else:
                health = self.health_check()
                summary = health.summary
            self._record("quick_ping", started, False, "completed", summary, self._allow_write_value(False))
            return {
                "ok": True,
                "summary": summary,
                "deliverable": str(self.spec.platform.get("target_label") or self.spec.config.get("model") or self.spec.worker_id),
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            self._record("quick_ping", started, False, "failed", str(exc), self._allow_write_value(False))
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        try:
            prompt = self.DEEP_PING_PROMPT
            command_key = (
                self.DEEP_PING_COMMAND_KEY
                if isinstance(self.spec.config.get(self.DEEP_PING_COMMAND_KEY), list)
                else self.INVOKE_COMMAND_KEY
            )
            text = self._run_command(
                command_key,
                prompt=prompt,
                allow_write=False,
                mode="deep",
                run_control=run_control,
            )
            payload = extract_json_object(text)
            summary = str(payload.get("summary") or "deep ping ok").strip()
            deliverable = str(payload.get("deliverable") or "").strip()
            self._record("deep_ping", started, False, "completed", summary or text, self._allow_write_value(False))
            return {
                "ok": True,
                "summary": summary or "deep ping ok",
                "deliverable": deliverable,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            self._record("deep_ping", started, False, "failed", str(exc), self._allow_write_value(False))
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }

    def health_check(self) -> WorkerHealth:
        if not isinstance(self.spec.config.get(self.HEALTH_COMMAND_KEY), list):
            binary = self._binary()
            return WorkerHealth(self.spec.worker_id, True, f"{binary} configured | use quick/deep ping for roundtrip tests")
        try:
            text = self._run_command(
                self.HEALTH_COMMAND_KEY,
                prompt="",
                allow_write=False,
                mode="quick",
                run_control=None,
            )
        except Exception as exc:
            return WorkerHealth(self.spec.worker_id, False, str(exc).strip() or "health check failed")
        return WorkerHealth(self.spec.worker_id, True, text[:220] or "external cli reachable")
