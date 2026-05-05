from __future__ import annotations

import json
import os
import shutil
from time import perf_counter

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object, run_subprocess_capture, summarize_stderr
from .base import BaseWorker, WorkerError, WorkerHealth

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]


class ClaudeCliWorker(BaseWorker):
    def _binary(self) -> str:
        return shutil.which("claude.cmd") or shutil.which("claude") or "claude.cmd"

    def _user_env(self, name: str) -> str:
        if os.name != "nt" or winreg is None:
            return ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return ""
        return str(value).strip()

    def _first_non_empty(self, *values: str) -> str:
        for value in values:
            if str(value).strip():
                return str(value).strip()
        return ""

    def _model(self) -> str:
        return self._first_non_empty(
            str(self.spec.config.get("model", "")),
            self._user_env("ANTHROPIC_MODEL"),
            os.environ.get("ANTHROPIC_MODEL", ""),
            os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
            os.environ.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
            "deepseek-v4-pro[1m]",
        )

    def _effort(self) -> str:
        return str(self.spec.config.get("effort", "max"))

    def _effort_for_mode(self, mode: str) -> str:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return str(self.spec.config.get("quick_effort", "low"))
        if normalized == "deep":
            return str(self.spec.config.get("deep_effort", self._effort()))
        return self._effort()

    def _timeout_for_mode(self, mode: str) -> int:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return int(self.spec.config.get("quick_timeout", 45))
        if normalized == "deep":
            return int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 1200)), 180)))
        return int(self.spec.config.get("timeout", 1200))

    def _permission_mode(self, allow_write: bool) -> str:
        write_mode = str(self.spec.config.get("write_permission_mode", "acceptEdits"))
        read_mode = str(self.spec.config.get("read_permission_mode", "plan"))
        return write_mode if allow_write and self.spec.supports_workspace_write else read_mode

    def _child_env(self) -> dict[str, str]:
        return self._child_env_for_mode("default")

    def _child_env_for_mode(self, mode: str) -> dict[str, str]:
        env = os.environ.copy()
        api_key = self._first_non_empty(
            str(self.spec.config.get("api_key", "")),
            self._user_env("DEEPSEEK_API_KEY"),
            self._user_env("ANTHROPIC_API_KEY"),
            self._user_env("ANTHROPIC_AUTH_TOKEN"),
            os.environ.get("DEEPSEEK_API_KEY", ""),
            os.environ.get("ANTHROPIC_AUTH_TOKEN", ""),
            os.environ.get("ANTHROPIC_API_KEY", ""),
        )
        base_url = self._first_non_empty(
            str(self.spec.config.get("base_url", "")),
            self._user_env("ANTHROPIC_BASE_URL"),
            os.environ.get("ANTHROPIC_BASE_URL", ""),
            "https://api.deepseek.com/anthropic",
        )
        subagent_model = self._first_non_empty(
            str(self.spec.config.get("subagent_model", "")),
            self._user_env("CLAUDE_CODE_SUBAGENT_MODEL"),
            os.environ.get("CLAUDE_CODE_SUBAGENT_MODEL", ""),
        )
        if api_key:
            env["DEEPSEEK_API_KEY"] = api_key
            env["ANTHROPIC_API_KEY"] = api_key
            env["ANTHROPIC_AUTH_TOKEN"] = api_key
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_MODEL"] = self._model()
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = self._model()
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = self._model()
        if subagent_model:
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = subagent_model
        env["CLAUDE_CODE_EFFORT_LEVEL"] = self._effort_for_mode(mode)
        env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
        env.setdefault("CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK", "1")
        return env

    def _parse_envelope(self, stdout: str, stderr: str) -> tuple[str, dict[str, object]]:
        blob = stdout.strip() or stderr.strip()
        if not blob:
            raise WorkerError("Claude returned an empty response.")
        try:
            payload = json.loads(blob)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"Claude returned invalid JSON output: {blob[:300]}") from exc
        if bool(payload.get("is_error")):
            raise WorkerError(str(payload.get("result") or "Claude returned an error.").strip())
        text = str(payload.get("result") or "").strip()
        if not text:
            raise WorkerError("Claude returned no result text.")
        return text, payload

    def _execute_prompt(
        self,
        prompt: str,
        *,
        allow_write: bool,
        run_control: RunControl | None = None,
        timeout: int | None = None,
        mode: str = "default",
    ) -> tuple[str, dict[str, object], str]:
        timeout = int(timeout if timeout is not None else self._timeout_for_mode(mode))
        permission_mode = self._permission_mode(allow_write)
        command = [
            self._binary(),
            "-p",
            "--bare",
            "--output-format",
            "json",
            "--permission-mode",
            permission_mode,
            "--model",
            self._model(),
            "--effort",
            self._effort_for_mode(mode),
            "--no-session-persistence",
        ]
        completed = run_subprocess_capture(
            command,
            cwd=self.workspace_root,
            input_text=prompt,
            timeout=timeout + 10,
            run_control=run_control,
            env=self._child_env_for_mode(mode),
        )
        if completed.returncode != 0:
            try:
                _, payload = self._parse_envelope(completed.stdout, completed.stderr)
                message = str(payload.get("result") or "Claude returned an error.").strip()
            except Exception:
                message = summarize_stderr(completed.stderr) or completed.stdout.strip() or "Claude returned an error."
            raise WorkerError(message)

        result_text, payload = self._parse_envelope(completed.stdout, completed.stderr)
        return result_text, payload, permission_mode

    def _record(self, stage: str, started: float, allow_write: bool, status: str, summary: str, access_mode: str) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": "Claude",
                "target": self._model(),
                "stage": stage,
                "status": status,
                "allow_write": allow_write,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "access_mode": f"claude:{access_mode}",
                "invocation_mode": stage.split(":", 1)[1] if ":" in stage else "default",
            },
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
        permission_mode = self._permission_mode(allow_write)
        normalized_mode = mode.strip().lower() or "default"
        try:
            text, _, permission_mode = self._execute_prompt(
                prompt,
                allow_write=allow_write,
                run_control=run_control,
                mode=normalized_mode,
            )
        except Exception as exc:
            self._record(f"invoke:{normalized_mode}", started, allow_write, "failed", str(exc), permission_mode)
            raise WorkerError(f"Claude worker '{self.spec.worker_id}' failed: {exc}") from exc

        self._record(f"invoke:{normalized_mode}", started, allow_write, "completed", text, permission_mode)
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        del run_control
        try:
            health = self.health_check()
            status = "completed" if health.ok else "failed"
            self._record("quick_ping", started, False, status, health.summary, self._permission_mode(False))
            return {
                "ok": health.ok,
                "summary": health.summary,
                "deliverable": self._model() if health.ok else "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            self._record("quick_ping", started, False, "failed", str(exc), self._permission_mode(False))
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        permission_mode = self._permission_mode(False)
        timeout = int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 1200)), 180)))
        try:
            text, _, permission_mode = self._execute_prompt(
                self.DEEP_PING_PROMPT,
                allow_write=False,
                run_control=run_control,
                timeout=timeout,
                mode="deep",
            )
            payload = extract_json_object(text)
            summary = str(payload.get("summary") or "deep ping ok").strip()
            deliverable = str(payload.get("deliverable") or "").strip()
            self._record("deep_ping", started, False, "completed", summary or text, permission_mode)
            return {
                "ok": True,
                "summary": summary or "deep ping ok",
                "deliverable": deliverable,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            self._record("deep_ping", started, False, "failed", str(exc), permission_mode)
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }

    def health_check(self) -> WorkerHealth:
        env = self._child_env()
        completed = run_subprocess_capture(
            [self._binary(), "--version"],
            cwd=self.workspace_root,
            timeout=15,
            env=env,
        )
        if completed.returncode != 0:
            return WorkerHealth(self.spec.worker_id, False, summarize_stderr(completed.stderr) or "claude --version failed")
        version = completed.stdout.strip() or completed.stderr.strip() or "Claude Code available"
        base_url = env.get("ANTHROPIC_BASE_URL", "").strip() or "default-endpoint"
        provider_label = "DeepSeek Anthropic" if "deepseek.com/anthropic" in base_url else base_url
        read_mode = self._permission_mode(False)
        write_mode = self._permission_mode(True) if self.spec.supports_workspace_write else read_mode
        return WorkerHealth(
            self.spec.worker_id,
            True,
            f"{version} | model {self._model()} | {provider_label} | read={read_mode}, write={write_mode}",
        )
