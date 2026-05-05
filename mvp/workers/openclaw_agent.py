from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from threading import Lock
from time import monotonic, perf_counter, time

from ..audit import append_worker_call
from ..utils import RunControl, extract_json_object, run_subprocess_capture, summarize_stderr
from .base import BaseWorker, WorkerError, WorkerHealth

try:
    import winreg
except ImportError:  # pragma: no cover - non-Windows fallback
    winreg = None  # type: ignore[assignment]


class OpenClawAgentWorker(BaseWorker):
    _QUICK_STATUS_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
    _CHILD_ENV_CACHE: tuple[float, dict[str, str]] | None = None
    _JSON_CACHE: dict[str, tuple[int, int, dict[str, object] | None]] = {}
    _VERSION_CACHE: tuple[float, str] | None = None
    _CACHE_LOCK = Lock()

    def _binary(self) -> str:
        return shutil.which("openclaw.cmd") or shutil.which("openclaw") or "openclaw.cmd"

    def _user_env(self, name: str) -> str:
        if os.name != "nt" or winreg is None:
            return ""
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            return ""
        return str(value).strip()

    def _child_env(self) -> dict[str, str]:
        cls = type(self)
        cached = cls._CHILD_ENV_CACHE
        now = monotonic()
        if cached and now - cached[0] <= 30.0:
            return dict(cached[1])
        env = os.environ.copy()
        for name in ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH", "CODEX_HOME"):
            value = self._user_env(name)
            if value:
                env[name] = value
        with cls._CACHE_LOCK:
            cls._CHILD_ENV_CACHE = (now, dict(env))
        return env

    def _thinking_for_mode(self, mode: str) -> str:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return str(self.spec.config.get("quick_thinking", self.spec.config.get("thinking", "low")))
        if normalized == "deep":
            return str(self.spec.config.get("deep_thinking", self.spec.config.get("thinking", "low")))
        return str(self.spec.config.get("thinking", "low"))

    def _timeout_for_mode(self, mode: str) -> int:
        normalized = mode.strip().lower()
        if normalized == "quick":
            return int(self.spec.config.get("quick_timeout", 45))
        if normalized == "deep":
            return int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 900)), 120)))
        return int(self.spec.config.get("timeout", 900))

    def _agent_id(self) -> str:
        return str(self.spec.config.get("agent", "main"))

    def _framework_name(self) -> str:
        agent_id = self._agent_id().lower()
        return "Hermes" if "hermes" in self.spec.worker_id.lower() or "hermes" in agent_id else "OpenClaw"

    def _agent_status(self, timeout: int) -> dict[str, object]:
        command = [
            self._binary(),
            "models",
            "--agent",
            self._agent_id(),
            "status",
            "--json",
        ]
        completed = run_subprocess_capture(command, cwd=self.workspace_root, timeout=timeout, env=self._child_env())
        if completed.returncode != 0:
            raise WorkerError(summarize_stderr(completed.stderr) or completed.stdout.strip() or "OpenClaw model status failed.")
        return extract_json_object(completed.stdout.strip() or completed.stderr.strip())

    def _config_path_candidates(self) -> list[Path]:
        env = self._child_env()
        candidates: list[Path] = []
        explicit = str(env.get("OPENCLAW_CONFIG_PATH", "")).strip()
        if explicit:
            candidates.append(Path(explicit))
        state_dir = str(env.get("OPENCLAW_STATE_DIR", "")).strip()
        if state_dir:
            state_path = Path(state_dir)
            candidates.append(state_path / "openclaw.json")
        home_dir = str(env.get("OPENCLAW_HOME", "")).strip()
        if home_dir:
            home_path = Path(home_dir)
            candidates.append(home_path / ".openclaw" / "openclaw.json")
            candidates.append(home_path / "openclaw.json")
        candidates.append(Path.home() / ".openclaw" / "openclaw.json")
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in candidates:
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(path)
        return deduped

    def _resolve_openclaw_config_path(self) -> Path | None:
        for path in self._config_path_candidates():
            if path.is_file():
                return path
        return None

    def _read_json_file(self, path: Path) -> dict[str, object] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        cache_key = str(path.resolve()).lower()
        cached = self._JSON_CACHE.get(cache_key)
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            payload = cached[2]
            return None if payload is None else dict(payload)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        payload = raw if isinstance(raw, dict) else None
        with self._CACHE_LOCK:
            self._JSON_CACHE[cache_key] = (stat.st_mtime_ns, stat.st_size, payload)
        return None if payload is None else dict(payload)

    def _agent_config_snapshot(self, config_path: Path | None = None) -> tuple[str, str, Path | None]:
        config_path = config_path or self._resolve_openclaw_config_path()
        if config_path is None:
            return self._agent_id(), str(self.spec.config.get("expected_model", "")).strip() or self._agent_id(), None
        payload = self._read_json_file(config_path) or {}
        agents = payload.get("agents", {}) if isinstance(payload.get("agents"), dict) else {}
        defaults = agents.get("defaults", {}) if isinstance(agents.get("defaults"), dict) else {}
        defaults_model = ""
        defaults_model_payload = defaults.get("model", {}) if isinstance(defaults.get("model"), dict) else {}
        if isinstance(defaults_model_payload, dict):
            defaults_model = str(defaults_model_payload.get("primary") or "").strip()
        agent_id = self._agent_id()
        agent_dir: Path | None = None
        model = ""
        entries = agents.get("list", []) if isinstance(agents.get("list"), list) else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "").strip() != agent_id:
                continue
            model = str(entry.get("model") or "").strip()
            raw_agent_dir = str(entry.get("agentDir") or "").strip()
            if raw_agent_dir:
                agent_dir = Path(raw_agent_dir)
            break
        if agent_dir is None:
            agent_dir = config_path.parent / "agents" / agent_id / "agent"
        target = model or defaults_model or str(self.spec.config.get("expected_model", "")).strip() or agent_id
        return agent_id, target, agent_dir

    def _auth_status_for_provider(self, provider: str, agent_dir: Path | None) -> str:
        if not provider:
            return "unknown"
        if agent_dir is None:
            return "unknown"
        auth_profiles = self._read_json_file(agent_dir / "auth-profiles.json") or {}
        auth_state = self._read_json_file(agent_dir / "auth-state.json") or {}
        profiles = auth_profiles.get("profiles", {}) if isinstance(auth_profiles.get("profiles"), dict) else {}
        last_good = auth_state.get("lastGood", {}) if isinstance(auth_state.get("lastGood"), dict) else {}

        profile_id = str(last_good.get(provider) or "").strip()
        if profile_id and profile_id in profiles and isinstance(profiles[profile_id], dict):
            return self._profile_auth_status(profiles[profile_id])

        for key, raw in profiles.items():
            if not isinstance(raw, dict):
                continue
            if str(raw.get("provider") or "").strip() != provider:
                continue
            return self._profile_auth_status(raw)

        env_map = {
            "deepseek": "DEEPSEEK_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "anthropic-openai": "ANTHROPIC_API_KEY",
            "ollama": "OLLAMA_API_KEY",
            "openai": "OPENAI_API_KEY",
        }
        env_name = env_map.get(provider, "")
        if env_name:
            env = self._child_env()
            if str(env.get(env_name, "")).strip():
                return "ok"
        return "missing"

    def _profile_auth_status(self, profile: dict[str, object]) -> str:
        profile_type = str(profile.get("type") or "").strip().lower()
        if profile_type == "oauth":
            expires = profile.get("expires")
            try:
                expires_ms = int(expires)
            except (TypeError, ValueError):
                return "ok"
            remaining_ms = expires_ms - int(time() * 1000)
            if remaining_ms <= 0:
                return "expired"
            if remaining_ms <= 6 * 60 * 60 * 1000:
                return "expiring"
            return "ok"
        if profile_type in {"token", "api_key"}:
            return "ok"
        return "ok" if profile_type else "unknown"

    def _quick_cache_key(self, config_path: Path | None = None) -> str:
        config_path = config_path or self._resolve_openclaw_config_path()
        return f"{self._agent_id()}::{str(config_path or '')}".lower()

    def _local_quick_status(self) -> dict[str, object] | None:
        config_path = self._resolve_openclaw_config_path()
        cache_key = self._quick_cache_key(config_path)
        cached = self._QUICK_STATUS_CACHE.get(cache_key)
        now = monotonic()
        if cached and now - cached[0] <= 20.0:
            return dict(cached[1])

        agent_id, target, agent_dir = self._agent_config_snapshot(config_path)
        provider = target.split("/", 1)[0] if "/" in target else target
        auth_status = self._auth_status_for_provider(provider, agent_dir)
        if not target:
            return None
        payload = {
            "ok": auth_status not in {"missing", "expired"},
            "summary": f"{target} | auth {auth_status} | local state",
            "deliverable": target,
            "latency_ms": 0,
            "ping_mode": "quick",
            "access_mode": "embedded-local-control",
            "target": target,
            "agent_id": agent_id,
        }
        self._QUICK_STATUS_CACHE[cache_key] = (now, payload)
        return dict(payload)

    def _version_summary(self) -> str:
        cls = type(self)
        cached = cls._VERSION_CACHE
        now = monotonic()
        if cached and now - cached[0] <= 600.0:
            return cached[1]
        completed = run_subprocess_capture([self._binary(), "--version"], cwd=self.workspace_root, timeout=10, env=self._child_env())
        if completed.returncode != 0:
            raise WorkerError(summarize_stderr(completed.stderr) or completed.stdout.strip() or "openclaw --version failed")
        version = completed.stdout.strip() or completed.stderr.strip() or "OpenClaw available"
        cls._VERSION_CACHE = (now, version)
        return version

    def _invoke_agent_payload(
        self,
        prompt: str,
        *,
        thinking: str,
        timeout: int,
        run_control: RunControl | None = None,
    ) -> tuple[dict[str, object], str, str, str]:
        agent_id = self._agent_id()
        command = [
            self._binary(),
            "agent",
            "--agent",
            agent_id,
            "--local",
            "--thinking",
            thinking,
            "--timeout",
            str(timeout),
            "--message",
            prompt,
            "--json",
        ]
        completed = run_subprocess_capture(
            command,
            cwd=self.workspace_root,
            timeout=timeout + 30,
            run_control=run_control,
            env=self._child_env(),
        )
        if completed.returncode != 0:
            raise WorkerError(summarize_stderr(completed.stderr) or completed.stdout.strip() or "OpenClaw returned an error.")

        json_blob = completed.stdout.strip() or completed.stderr.strip()
        payload = extract_json_object(json_blob)
        text = str(payload.get("finalAssistantVisibleText") or payload.get("finalAssistantRawText") or "").strip()
        if not text:
            response_payloads = payload.get("payloads") or []
            if isinstance(response_payloads, list):
                for item in response_payloads:
                    if isinstance(item, dict) and item.get("text"):
                        text = str(item.get("text")).strip()
                        if text:
                            break
        if not text:
            raise WorkerError("OpenClaw returned no visible text.")

        system_prompt_report = payload.get("systemPromptReport", {}) if isinstance(payload.get("systemPromptReport"), dict) else {}
        sandbox = system_prompt_report.get("sandbox", {}) if isinstance(system_prompt_report.get("sandbox"), dict) else {}
        access_mode = str(sandbox.get("mode") or "embedded-local").strip()
        if sandbox.get("sandboxed") is False and access_mode == "off":
            access_mode = "embedded-local(sandbox=off)"

        meta = payload.get("meta", {}) if isinstance(payload.get("meta"), dict) else {}
        agent_meta = meta.get("agentMeta", {}) if isinstance(meta.get("agentMeta"), dict) else {}
        provider = str(agent_meta.get("provider") or "").strip()
        model = str(agent_meta.get("model") or "").strip()
        target = f"{agent_id} @ {provider}/{model}".rstrip("/") if provider or model else agent_id
        return payload, text, access_mode, target

    def _record_success(
        self,
        stage: str,
        started: float,
        summary: str,
        access_mode: str,
        target: str,
        session_id: str = "",
        allow_write: bool = False,
        buffered: bool = False,
    ) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": self._framework_name(),
                "target": target,
                "stage": stage,
                "status": "completed",
                "allow_write": allow_write,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "session_id": session_id,
                "access_mode": access_mode,
                "invocation_mode": stage.split(":", 1)[1] if ":" in stage else "default",
            },
            buffered=buffered,
        )

    def _record_failure(self, stage: str, started: float, summary: str, allow_write: bool = False, buffered: bool = False) -> None:
        append_worker_call(
            self.runs_dir,
            {
                "worker_id": self.spec.worker_id,
                "framework": self._framework_name(),
                "target": self._agent_id(),
                "stage": stage,
                "status": "failed",
                "allow_write": allow_write,
                "duration_ms": int((perf_counter() - started) * 1000),
                "summary": summary[:220],
                "access_mode": "embedded-local",
            },
            buffered=buffered,
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
        thinking = self._thinking_for_mode(normalized_mode)
        timeout = self._timeout_for_mode(normalized_mode)
        try:
            payload, text, access_mode, target = self._invoke_agent_payload(
                prompt,
                thinking=thinking,
                timeout=timeout,
                run_control=run_control,
            )
        except Exception as exc:
            self._record_failure(f"invoke:{normalized_mode}", started, str(exc).strip(), allow_write=allow_write)
            raise WorkerError(f"OpenClaw worker '{self.spec.worker_id}' failed: {exc}") from exc

        meta = payload.get("meta", {}) if isinstance(payload.get("meta"), dict) else {}
        agent_meta = meta.get("agentMeta", {}) if isinstance(meta.get("agentMeta"), dict) else {}
        session_id = str(agent_meta.get("sessionId") or "")
        self._record_success(
            f"invoke:{normalized_mode}",
            started,
            text,
            access_mode,
            target,
            session_id=session_id,
            allow_write=allow_write,
        )
        return text

    def quick_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        del run_control
        started = perf_counter()
        try:
            payload = self._local_quick_status()
            if payload is None:
                timeout = int(self.spec.config.get("quick_timeout", 30))
                payload = self._agent_status(timeout=timeout)
                target = str(payload.get("resolvedDefault") or payload.get("defaultModel") or self._agent_id()).strip()
                provider = target.split("/", 1)[0] if "/" in target else target
                auth = payload.get("auth", {}) if isinstance(payload.get("auth"), dict) else {}
                oauth = auth.get("oauth", {}) if isinstance(auth.get("oauth"), dict) else {}
                provider_rows = oauth.get("providers", []) if isinstance(oauth.get("providers"), list) else []
                provider_status = ""
                for row in provider_rows:
                    if isinstance(row, dict) and str(row.get("provider", "")).strip() == provider:
                        provider_status = str(row.get("status") or "").strip()
                        break
                payload = {
                    "ok": True,
                    "summary": f"{target} | auth {provider_status or 'unknown'}",
                    "deliverable": target,
                    "ping_mode": "quick",
                    "access_mode": "embedded-local-control",
                    "target": target,
                }
            summary = str(payload.get("summary") or "").strip()
            access_mode = str(payload.get("access_mode") or "embedded-local-control").strip()
            target = str(payload.get("target") or payload.get("deliverable") or self._agent_id()).strip()
            self._record_success("quick_ping", started, summary, access_mode, target, buffered=True)
            return {
                "ok": bool(payload.get("ok", True)),
                "summary": summary,
                "deliverable": target,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }
        except Exception as exc:
            self._record_failure("quick_ping", started, str(exc).strip(), buffered=True)
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "quick",
            }

    def deep_ping(self, run_control: RunControl | None = None) -> dict[str, object]:
        started = perf_counter()
        thinking = str(self.spec.config.get("deep_thinking", self.spec.config.get("thinking", "low")))
        timeout = int(self.spec.config.get("deep_timeout", min(int(self.spec.config.get("timeout", 900)), 120)))
        try:
            payload, text, access_mode, target = self._invoke_agent_payload(
                self.DEEP_PING_PROMPT,
                thinking=thinking,
                timeout=timeout,
                run_control=run_control,
            )
            parsed = extract_json_object(text)
            meta = payload.get("meta", {}) if isinstance(payload.get("meta"), dict) else {}
            agent_meta = meta.get("agentMeta", {}) if isinstance(meta.get("agentMeta"), dict) else {}
            summary = str(parsed.get("summary") or "deep ping ok").strip()
            deliverable = str(parsed.get("deliverable") or "").strip()
            self._record_success("deep_ping", started, summary or text, access_mode, target, session_id=str(agent_meta.get("sessionId") or ""))
            return {
                "ok": True,
                "summary": summary or "deep ping ok",
                "deliverable": deliverable,
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }
        except Exception as exc:
            self._record_failure("deep_ping", started, str(exc).strip())
            return {
                "ok": False,
                "summary": str(exc).strip(),
                "deliverable": "",
                "latency_ms": int((perf_counter() - started) * 1000),
                "ping_mode": "deep",
            }

    def health_check(self) -> WorkerHealth:
        agent_id = self._agent_id()
        expected_model = str(self.spec.config.get("expected_model", "")).strip()
        try:
            version = self._version_summary()
        except Exception as exc:
            return WorkerHealth(self.spec.worker_id, False, str(exc).strip() or "openclaw --version failed")
        model_summary = expected_model or "model unknown"
        return WorkerHealth(
            self.spec.worker_id,
            True,
            f"{version} | agent '{agent_id}' configured for {model_summary} | use quick/deep ping for roundtrip tests",
        )
