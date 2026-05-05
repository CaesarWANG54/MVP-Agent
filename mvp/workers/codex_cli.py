from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from time import perf_counter

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object, run_subprocess_capture, summarize_stderr
from .base import BaseWorker, WorkerError, WorkerHealth


class CodexCliWorker(BaseWorker):
    def _binary(self) -> str:
        return shutil.which("codex.cmd") or shutil.which("codex") or "codex.cmd"

    def _model(self) -> str:
        return str(self.spec.config.get("model", "gpt-5.4"))

    def _sandbox_for(self, allow_write: bool) -> str:
        write_sandbox = str(self.spec.config.get("write_sandbox", "workspace-write"))
        read_sandbox = str(self.spec.config.get("read_sandbox", "read-only"))
        return write_sandbox if allow_write and self.spec.supports_workspace_write else read_sandbox

    def _execute_prompt(
        self,
        prompt: str,
        *,
        allow_write: bool,
        run_control: RunControl | None = None,
        timeout: int | None = None,
        persist_sessions: bool | None = None,
    ) -> tuple[str, str]:
        model = self._model()
        timeout = int(timeout if timeout is not None else self.spec.config.get("timeout", 1200))
        persist_sessions = bool(self.spec.config.get("persist_sessions", True) if persist_sessions is None else persist_sessions)
        sandbox = self._sandbox_for(allow_write)

        with tempfile.TemporaryDirectory(prefix="mvp-codex-") as tmpdir:
            output_file = Path(tmpdir) / "last_message.txt"
            command = [
                self._binary(),
                "exec",
                "--model",
                model,
                "--skip-git-repo-check",
                "--color",
                "never",
                "--output-last-message",
                str(output_file),
                "-",
            ]
            if sandbox == "danger-full-access":
                command[4:4] = ["--dangerously-bypass-approvals-and-sandbox"]
            else:
                command[4:4] = ["--sandbox", sandbox]
            if not persist_sessions:
                command.insert(8 if sandbox == "danger-full-access" else 9, "--ephemeral")

            completed = run_subprocess_capture(
                command,
                cwd=self.workspace_root,
                input_text=prompt,
                timeout=timeout + 10,
                run_control=run_control,
            )

            if completed.returncode != 0:
                raise WorkerError(summarize_stderr(completed.stderr) or completed.stdout.strip() or "Codex returned an error.")

            if output_file.exists():
                text = output_file.read_text(encoding="utf-8").strip()
            else:
                text = completed.stdout.strip()

        if not text:
            raise WorkerError("Codex returned an empty response.")
        return text, sandbox

    def _record(self, stage: str, started: float, allow_write: bool, status: str, summary: str, sandbox: str) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": "Codex",
                "target": self._model(),
                "stage": stage,
                "status": status,
                "allow_write": allow_write,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "access_mode": sandbox,
            },
        )

    def invoke_text(self, prompt: str, allow_write: bool = False, run_control: RunControl | None = None) -> str:
        started = perf_counter()
        sandbox = self._sandbox_for(allow_write)
        try:
            text, sandbox = self._execute_prompt(prompt, allow_write=allow_write, run_control=run_control)
        except Exception as exc:
            self._record("invoke", started, allow_write, "failed", str(exc), sandbox)
            raise WorkerError(f"Codex worker '{self.spec.worker_id}' failed: {exc}") from exc

        self._record("invoke", started, allow_write, "completed", text, sandbox)
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        sandbox = self._sandbox_for(False)
        del run_control
        try:
            health = self.health_check()
            status = "completed" if health.ok else "failed"
            self._record("quick_ping", started, False, status, health.summary, sandbox)
            return {
                "ok": health.ok,
                "summary": health.summary,
                "deliverable": self._model() if health.ok else "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            self._record("quick_ping", started, False, "failed", str(exc), sandbox)
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        sandbox = self._sandbox_for(False)
        timeout = int(self.spec.config.get("deep_timeout", 180))
        try:
            text, sandbox = self._execute_prompt(
                self.DEEP_PING_PROMPT,
                allow_write=False,
                run_control=run_control,
                timeout=timeout,
                persist_sessions=False,
            )
            payload = extract_json_object(text)
            summary = str(payload.get("summary") or "deep ping ok").strip()
            deliverable = str(payload.get("deliverable") or "").strip()
            self._record("deep_ping", started, False, "completed", summary or text, sandbox)
            return {
                "ok": True,
                "summary": summary or "deep ping ok",
                "deliverable": deliverable,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            self._record("deep_ping", started, False, "failed", str(exc), sandbox)
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }

    def health_check(self) -> WorkerHealth:
        completed = run_subprocess_capture([self._binary(), "--version"], cwd=self.workspace_root, timeout=15)
        if completed.returncode != 0:
            return WorkerHealth(self.spec.worker_id, False, summarize_stderr(completed.stderr) or "codex --version failed")

        version = completed.stdout.strip() or completed.stderr.strip()
        read_sandbox = str(self.spec.config.get("read_sandbox", "read-only"))
        write_sandbox = str(self.spec.config.get("write_sandbox", "workspace-write"))
        sandbox_summary = f"read={read_sandbox}, write={write_sandbox if self.spec.supports_workspace_write else read_sandbox}"
        return WorkerHealth(self.spec.worker_id, True, f"{version} | model {self._model()} | {sandbox_summary}")
