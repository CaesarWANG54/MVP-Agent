from __future__ import annotations

import json
import os
from time import perf_counter
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object, summarize_stderr
from .base import BaseWorker, WorkerError, WorkerHealth


class _SafeFormatDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class ServiceMeshAgentWorker(BaseWorker):
    def _base_url(self) -> str:
        base_url = str(self.spec.config.get("base_url", "")).strip().rstrip("/")
        if not base_url:
            raise WorkerError(f"Service mesh worker '{self.spec.worker_id}' is missing config.base_url.")
        return base_url

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

    def _render_str(self, value: str, context: dict[str, str]) -> str:
        return value.format_map(_SafeFormatDict(context))

    def _render_value(self, value: Any, context: dict[str, str]) -> Any:
        if isinstance(value, str):
            return self._render_str(value, context)
        if isinstance(value, list):
            return [self._render_value(item, context) for item in value]
        if isinstance(value, dict):
            return {str(key): self._render_value(item, context) for key, item in value.items()}
        return value

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        raw_headers = self.spec.config.get("headers", {})
        if isinstance(raw_headers, dict):
            for key, value in raw_headers.items():
                if str(key).strip():
                    headers[str(key).strip()] = str(value)
        auth_env = str(self.spec.config.get("auth_env", "")).strip()
        if auth_env:
            secret = os.environ.get(auth_env, "").strip()
            if secret:
                auth_header = str(self.spec.config.get("auth_header", "Authorization")).strip() or "Authorization"
                auth_scheme = str(self.spec.config.get("auth_scheme", "Bearer")).strip()
                headers[auth_header] = f"{auth_scheme} {secret}".strip()
        return headers

    def _join_url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self._base_url()}/{path.lstrip('/')}"

    def _request_payload(self, prompt: str, allow_write: bool, mode: str) -> Any:
        template = self.spec.config.get("request_template")
        context = self._context(prompt, allow_write, mode)
        if isinstance(template, (dict, list, str)):
            return self._render_value(template, context)
        return {
            "prompt": prompt,
            "mode": context["mode"],
            "allow_write": allow_write and self.spec.supports_workspace_write,
            "write_mode": context["write_mode"],
            "worker_id": self.spec.worker_id,
            "workspace_root": str(self.workspace_root),
            "model": context["model"],
        }

    def _perform_request(
        self,
        *,
        path: str,
        method: str,
        payload: Any,
        mode: str,
        run_control: RunControl | None,
    ) -> tuple[int, str]:
        if run_control is not None:
            run_control.raise_if_cancelled()
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urlrequest.Request(
            self._join_url(path),
            data=data,
            headers=self._headers(),
            method=method.upper(),
        )
        timeout = self._timeout_for_mode(mode)
        try:
            with urlrequest.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                status = int(getattr(response, "status", 200) or 200)
        except urlerror.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise WorkerError(f"HTTP {exc.code}: {body[:220] or exc.reason}") from exc
        except urlerror.URLError as exc:
            raise WorkerError(str(exc.reason or exc)) from exc
        except Exception as exc:
            raise WorkerError(str(exc)) from exc
        if run_control is not None:
            run_control.raise_if_cancelled()
        return status, body

    def _extract_text(self, body: str) -> str:
        parser = str(self.spec.config.get("response_parser", "text")).strip().lower()
        blob = body.strip()
        if not blob:
            raise WorkerError("Service mesh agent returned an empty response.")
        if parser == "json_field":
            payload = json.loads(blob)
            text_field = str(self.spec.config.get("response_text_field", "result")).strip() or "result"
            text = str(payload.get(text_field) or "").strip()
            if text:
                return text
            raise WorkerError(f"Service mesh JSON did not include '{text_field}'.")
        return blob

    def _record(self, stage: str, started: float, allow_write: bool, status: str, summary: str, access_mode: str) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": str(self.spec.platform.get("framework_label") or self.spec.display_name),
                "target": str(self.spec.platform.get("target_label") or self.spec.config.get("base_url") or self.spec.worker_id),
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
            path = str(self.spec.config.get("invoke_path", "")).strip()
            if not path:
                raise WorkerError(f"Service mesh worker '{self.spec.worker_id}' is missing config.invoke_path.")
            method = str(self.spec.config.get("invoke_method", "POST")).strip() or "POST"
            _status_code, body = self._perform_request(
                path=path,
                method=method,
                payload=self._request_payload(prompt, allow_write, normalized_mode),
                mode=normalized_mode,
                run_control=run_control,
            )
            text = self._extract_text(body)
        except Exception as exc:
            self._record(f"invoke:{normalized_mode}", started, allow_write, "failed", str(exc), access_mode)
            raise WorkerError(f"Service mesh worker '{self.spec.worker_id}' failed: {exc}") from exc
        self._record(f"invoke:{normalized_mode}", started, allow_write, "completed", text, access_mode)
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        try:
            ping_path = str(self.spec.config.get("quick_ping_path", "")).strip()
            if ping_path:
                method = str(self.spec.config.get("quick_ping_method", "POST")).strip() or "POST"
                _status_code, body = self._perform_request(
                    path=ping_path,
                    method=method,
                    payload=self._request_payload(self.QUICK_PING_TEXT, False, "quick"),
                    mode="quick",
                    run_control=run_control,
                )
                summary = self._extract_text(body)[:220]
            else:
                health = self.health_check()
                summary = health.summary
            self._record("quick_ping", started, False, "completed", summary, self._allow_write_value(False))
            return {
                "ok": True,
                "summary": summary,
                "deliverable": str(self.spec.platform.get("target_label") or self.spec.config.get("base_url") or self.spec.worker_id),
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
            ping_path = str(self.spec.config.get("deep_ping_path", "")).strip() or str(self.spec.config.get("invoke_path", "")).strip()
            if not ping_path:
                raise WorkerError(f"Service mesh worker '{self.spec.worker_id}' is missing config.deep_ping_path and config.invoke_path.")
            method = str(self.spec.config.get("deep_ping_method", self.spec.config.get("invoke_method", "POST"))).strip() or "POST"
            _status_code, body = self._perform_request(
                path=ping_path,
                method=method,
                payload=self._request_payload(self.DEEP_PING_PROMPT, False, "deep"),
                mode="deep",
                run_control=run_control,
            )
            payload = extract_json_object(body)
            summary = str(payload.get("summary") or "deep ping ok").strip()
            deliverable = str(payload.get("deliverable") or "").strip()
            self._record("deep_ping", started, False, "completed", summary or body, self._allow_write_value(False))
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
        path = str(self.spec.config.get("health_path", "")).strip()
        if not path:
            return WorkerHealth(self.spec.worker_id, True, f"{self._base_url()} configured | use quick/deep ping for roundtrip tests")
        method = str(self.spec.config.get("health_method", "GET")).strip() or "GET"
        payload = None if method.upper() == "GET" else self._request_payload("", False, "quick")
        try:
            _status_code, body = self._perform_request(
                path=path,
                method=method,
                payload=payload,
                mode="quick",
                run_control=None,
            )
        except Exception as exc:
            return WorkerHealth(self.spec.worker_id, False, str(exc).strip() or summarize_stderr(str(exc)))
        summary = body.strip()[:220] or "service mesh reachable"
        return WorkerHealth(self.spec.worker_id, True, summary)
