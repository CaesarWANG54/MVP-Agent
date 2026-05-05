from __future__ import annotations

import json
import urllib.error
import urllib.request
from time import perf_counter

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object
from .base import BaseWorker, WorkerError, WorkerHealth


class OllamaApiWorker(BaseWorker):
    def _model(self) -> str:
        return str(self.spec.config.get("model", "qwen3.5:9b"))

    def _think_for_mode(self, mode: str) -> bool:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return bool(self.spec.config.get("quick_think", self.spec.config.get("think", False)))
        if normalized == "deep":
            return bool(self.spec.config.get("deep_think", self.spec.config.get("think", False)))
        return bool(self.spec.config.get("think", False))

    def _timeout_for_mode(self, mode: str) -> int:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return int(self.spec.config.get("quick_timeout", 20))
        if normalized == "deep":
            return int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 240)), 45)))
        return int(self.spec.config.get("timeout", 240))

    def _num_predict_for_mode(self, mode: str) -> int | None:
        normalized = mode.strip().lower()
        if normalized == "quick":
            value = self.spec.config.get("quick_num_predict")
        elif normalized == "deep":
            value = self.spec.config.get("deep_num_predict")
        else:
            value = self.spec.config.get("num_predict")
        return None if value is None else int(value)

    def _generate(
        self,
        prompt: str,
        *,
        think: bool,
        temperature: float,
        timeout: int,
        num_predict: int | None = None,
        run_control: RunControl | None = None,
    ) -> str:
        options: dict[str, object] = {"temperature": temperature}
        if num_predict is not None:
            options["num_predict"] = int(num_predict)
        payload = {
            "model": self._model(),
            "prompt": prompt,
            "stream": False,
            "think": think,
            "options": options,
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if run_control is not None:
            run_control.raise_if_cancelled()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if run_control is not None:
            run_control.raise_if_cancelled()
        return str(raw.get("response", "")).strip()

    def _record(self, stage: str, started: float, status: str, summary: str) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": "Ollama",
                "target": self._model(),
                "stage": stage,
                "status": status,
                "allow_write": False,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "access_mode": "local-http",
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
        del allow_write
        started = perf_counter()
        normalized_mode = mode.strip().lower() or "default"
        think = self._think_for_mode(normalized_mode)
        temperature = float(self.spec.config.get("temperature", 0.2))
        timeout = self._timeout_for_mode(normalized_mode)
        num_predict = self._num_predict_for_mode(normalized_mode)
        try:
            text = self._generate(
                prompt,
                think=think,
                temperature=temperature,
                timeout=timeout,
                num_predict=num_predict,
                run_control=run_control,
            )
        except urllib.error.URLError as exc:
            self._record(f"invoke:{normalized_mode}", started, "failed", str(exc))
            raise WorkerError(f"Ollama worker '{self.spec.worker_id}' failed: {exc}") from exc
        except Exception as exc:
            self._record(f"invoke:{normalized_mode}", started, "failed", str(exc))
            raise WorkerError(f"Ollama worker '{self.spec.worker_id}' failed: {exc}") from exc

        if not text:
            self._record(f"invoke:{normalized_mode}", started, "empty", "Ollama returned an empty response.")
            raise WorkerError(f"Ollama worker '{self.spec.worker_id}' returned an empty response.")

        self._record(f"invoke:{normalized_mode}", started, "completed", text)
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        del run_control
        try:
            health = self.health_check()
            status = "completed" if health.ok else "failed"
            self._record("quick_ping", started, status, health.summary)
            return {
                "ok": health.ok,
                "summary": health.summary,
                "deliverable": self._model() if health.ok else "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            self._record("quick_ping", started, "failed", str(exc))
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        timeout = int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 240)), 45)))
        try:
            text = self._generate(
                self.DEEP_PING_PROMPT,
                think=False,
                temperature=0.0,
                timeout=timeout,
                num_predict=int(self.spec.config.get("deep_num_predict", 96)),
                run_control=run_control,
            )
            if not text:
                raise WorkerError("Ollama returned an empty response.")
            payload = extract_json_object(text)
            summary = str(payload.get("summary") or "deep ping ok").strip()
            deliverable = str(payload.get("deliverable") or "").strip()
            self._record("deep_ping", started, "completed", summary or text)
            return {
                "ok": True,
                "summary": summary or "deep ping ok",
                "deliverable": deliverable,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            self._record("deep_ping", started, "failed", str(exc))
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }

    def health_check(self) -> WorkerHealth:
        request = urllib.request.Request("http://127.0.0.1:11434/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            return WorkerHealth(self.spec.worker_id, False, f"Ollama not reachable: {exc}")

        model_name = self._model()
        models = [item.get("name") for item in payload.get("models", [])]
        if model_name in models:
            return WorkerHealth(self.spec.worker_id, True, f"reachable, model '{model_name}' installed")
        return WorkerHealth(self.spec.worker_id, False, f"reachable, but model '{model_name}' is missing")
