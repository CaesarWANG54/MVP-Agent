from __future__ import annotations

from .models import ReviewResult, RunProfile, Subtask, WorkerResult
from .prompting import build_review_prompt
from .utils import RunControl, clamp_float, normalize_str_list
from .worker_platform import worker_mode_family
from .workers.registry import WorkerRegistry


class Reviewer:
    def __init__(self, registry: WorkerRegistry) -> None:
        self.registry = registry

    def review(
        self,
        subtask: Subtask,
        result: WorkerResult,
        profile: RunProfile,
        run_control: RunControl | None = None,
    ) -> ReviewResult:
        prompt = build_review_prompt(subtask, result)
        code_lane = (
            subtask.kind in {"coding", "architecture", "testing", "qa", "security", "performance"}
            or subtask.needs_write_access
            or bool(subtask.target_files)
            or bool(subtask.validation_steps)
        )
        fast_lane = (
            not code_lane
            and
            not subtask.needs_write_access
            and subtask.difficulty <= 2
            and subtask.risk <= 2
            and result.status == "completed"
        )
        reviewer_candidates = []
        for candidate in self._candidate_order(profile, code_lane, fast_lane):
            if candidate not in reviewer_candidates:
                reviewer_candidates.append(candidate)
        payload = None
        reviewer_used = profile.reviewer_worker
        last_error = ""
        for candidate in reviewer_candidates:
            if candidate not in self.registry.worker_specs or not self.registry.worker_specs[candidate].enabled:
                continue
            reviewer = self.registry.get(candidate)
            for mode in self._review_modes(candidate, profile, code_lane, fast_lane):
                try:
                    payload = self._invoke_json_mode(
                        reviewer,
                        prompt,
                        allow_write=False,
                        run_control=run_control,
                        mode=mode,
                    )
                    reviewer_used = candidate
                    break
                except Exception as exc:
                    last_error = f"{candidate}[{mode}]: {exc}"
            if payload is not None:
                break

        if payload is None:
            raise RuntimeError(f"All reviewer fallbacks failed. Last error: {last_error}")

        return ReviewResult(
            reviewer_worker=reviewer_used,
            decision=str(payload.get("decision") or "revise").strip().lower(),
            summary=str(payload.get("summary") or "").strip(),
            findings=normalize_str_list(payload.get("findings")),
            confidence=clamp_float(payload.get("confidence"), 0.0, 1.0, 0.5),
            raw_response=str(payload.get("_raw_response", "")),
        )

    def _candidate_order(self, profile: RunProfile, code_lane: bool, fast_lane: bool) -> list[str]:
        if code_lane:
            if profile.name == "balanced":
                return [profile.reviewer_worker, "ollama_reviewer", "codex_auditor"]
            if profile.name == "cheap":
                return ["ollama_reviewer", profile.reviewer_worker, "codex_auditor"]
            return ["codex_auditor", profile.reviewer_worker, "claude_strategist", "ollama_reviewer"]
        if fast_lane:
            return ["ollama_reviewer", profile.reviewer_worker, "claude_strategist", "codex_auditor"]
        if profile.name == "balanced":
            return [profile.reviewer_worker, "ollama_reviewer", "codex_auditor"]
        return [profile.reviewer_worker, "codex_auditor", "ollama_reviewer"]

    def _review_modes(self, worker_id: str, profile: RunProfile, code_lane: bool, fast_lane: bool) -> list[str]:
        spec = self.registry.worker_specs.get(worker_id)
        if spec is None:
            return ["default"]
        mode_family = worker_mode_family(spec)
        if mode_family == "local_llm":
            return ["quick"] if fast_lane else ["deep", "quick"]
        if mode_family in {"agent_cli", "extension_bridge", "service_mesh"}:
            if fast_lane:
                return ["quick", "deep"]
            if profile.name == "balanced":
                return ["quick"]
            return ["deep", "quick"] if code_lane else ["quick", "deep"]
        return ["default"]

    def _invoke_json_mode(
        self,
        worker,
        prompt: str,
        *,
        allow_write: bool,
        run_control: RunControl | None,
        mode: str,
    ) -> dict[str, object]:
        if hasattr(worker, "invoke_json_mode"):
            return worker.invoke_json_mode(
                prompt,
                allow_write=allow_write,
                run_control=run_control,
                mode=mode,
            )
        return worker.invoke_json(prompt, allow_write=allow_write, run_control=run_control)
