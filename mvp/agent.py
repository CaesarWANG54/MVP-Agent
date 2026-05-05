"""MVP Agent — LLM-native orchestration with ReAct reasoning and memory.

The Agent is the central decision-maker. It uses a ReAct (Reason + Act)
loop to decompose tasks, dispatch to workers, observe results, and
synthesize final outputs. It learns from every run via the MemoryStore.

Architecture:
  1. Observe: read task + recall relevant past experience
  2. Reason: chain-of-thought analysis of what needs to happen
  3. Act: delegate subtasks to workers
  4. Reflect: review outcomes, replan if needed
  5. Synthesize: produce a coherent final response
  6. Learn: persist patterns for future use
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from .audit import summarize_worker_metrics, summarize_worker_metrics_by_kind
from .code_intel import CodeIntel, FileIntel
from .code_stream import build_code_stream_chunks
from .framework_history import read_framework_timeline
from .memory import MemoryStore
from .models import (
    Assignment, AssignmentOutcome, LeaderTraceEntry, ReviewResult,
    RunProfile, RunReport, Subtask, TaskPlan, WorkerResult, WorkerSpec,
)
from .planner import Planner
from .prompting import (
    build_execution_prompt, build_replan_prompt, build_review_prompt,
    build_synthesis_prompt,
)
from .review import Reviewer
from .router import rank_workers, route_subtask
from .skill_mesh import SkillMesh
from .system_stats import collect_system_stats
from .utils import (
    RunCancelled, RunControl, clamp_float, clamp_int, ensure_directory,
    load_json_file, normalize_str_list, save_json_file,
)
from .worker_platform import describe_worker_platform, worker_mode_family
from .workers.registry import WorkerRegistry


RunEventCallback = Callable[[dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Agent reasoning trace
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Thought:
    content: str
    action: str = ""              # "plan", "delegate", "review", "replan", "synthesize"
    worker_id: str = ""
    confidence: float = 1.0


@dataclass(slots=True)
class AgentContext:
    """Accumulated context during a reasoning loop."""
    task: str
    profile: RunProfile
    thoughts: list[Thought] = field(default_factory=list)
    recalled_tasks: list[dict[str, Any]] = field(default_factory=list)
    recalled_pattern: dict[str, Any] | None = None
    recalled_skill_profile: dict[str, Any] | None = None
    code_context: dict[str, FileIntel] = field(default_factory=dict)
    completed_count: int = 0
    total_count: int = 0


# ---------------------------------------------------------------------------
# Progress tracker
# ---------------------------------------------------------------------------

class ProgressTracker:
    def __init__(self, total_units: int) -> None:
        self.total_units = max(total_units, 1)
        self._completed_units = 0
        self._lock = Lock()

    def advance(self, units: int = 1) -> tuple[int, int]:
        with self._lock:
            self._completed_units += units
            return self._completed_units, self.total_units

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            return self._completed_units, self.total_units


# ---------------------------------------------------------------------------
# MvpAgent
# ---------------------------------------------------------------------------

class MvpAgent:
    """LLM-native orchestration agent with memory and code intelligence."""

    FAILED_RESULT_STATUSES = {"failed", "error", "blocked", "empty", "cancelled"}

    def __init__(
        self,
        config_path: str | None = None,
        memory_path: str | None = None,
    ) -> None:
        from .config import build_profiles, build_worker_specs, load_config

        self.config = load_config(config_path)
        self.workspace_root = Path(self.config["workspace_root"]).resolve()
        self.runs_dir = ensure_directory(Path(self.config["runs_dir"]).resolve())
        self.worker_specs = build_worker_specs(self.config)
        self.profiles = build_profiles(self.config)
        self.registry = WorkerRegistry(self.worker_specs, self.workspace_root, self.runs_dir)
        skills_config = self.config.get("skills", {})
        self.skill_mesh = (
            SkillMesh(
                skills_config.get("discover_roots", []),
                max_cards=int(skills_config.get("max_cards", 256) or 256),
            )
            if skills_config.get("enabled", True)
            else None
        )
        self.planner = Planner(self.registry, self.worker_specs, skill_mesh=self.skill_mesh)
        self.reviewer = Reviewer(self.registry)

        # Long-term memory
        mem_path = Path(memory_path) if memory_path else (self.runs_dir / "mvp_memory.db")
        self.memory = MemoryStore(mem_path)

        # Code intelligence
        self.code_intel = CodeIntel(self.workspace_root)

        self._health_cache: tuple[float, list[dict[str, object]]] | None = None
        self._health_cache_ttl_s = 45.0

    # -- public API ------------------------------------------------------------

    def get_profile(self, name: str) -> RunProfile:
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise ValueError(f"Unknown profile '{name}'. Available: {available}")
        return self.profiles[name]

    def list_workers(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        metrics = summarize_worker_metrics(self.runs_dir, limit=80)
        mem_stats = self.memory.all_worker_stats()
        for spec in self.registry.all_enabled_specs():
            labels = self._worker_labels(spec)
            worker_metric = metrics.get(spec.worker_id, {})
            mem_stat = mem_stats.get(spec.worker_id, {})
            rows.append({
                "worker_id": spec.worker_id,
                "display_name": spec.display_name,
                "type": spec.worker_type,
                "role": spec.role,
                "capabilities": spec.capabilities,
                "cost_tier": spec.cost_tier,
                "quality_tier": spec.quality_tier,
                "speed_tier": spec.speed_tier,
                "local_only": spec.local_only,
                "api_cost": spec.api_cost,
                "supports_workspace_write": spec.supports_workspace_write,
                "framework_label": labels["framework_label"],
                "backend_label": labels["backend_label"],
                "target_label": labels["target_label"],
                "avg_latency_ms": worker_metric.get("avg_duration_ms"),
                "failure_ratio": worker_metric.get("failure_ratio"),
                "memory_success_rate": mem_stat.get("success_rate"),
                "memory_calls": mem_stat.get("total_calls"),
            })
        return rows

    def list_skills(self, limit: int = 40) -> list[dict[str, object]]:
        if self.skill_mesh is None:
            return []
        return self.skill_mesh.catalog_summary(limit=limit)

    def health(self, force_refresh: bool = False) -> list[dict[str, object]]:
        if self._health_cache and not force_refresh:
            cached_at, rows = self._health_cache
            if perf_counter() - cached_at <= self._health_cache_ttl_s:
                return [dict(row) for row in rows]

        specs = self.registry.all_enabled_specs()
        def collect(spec):
            health = self.registry.get(spec.worker_id).health_check()
            labels = self._worker_labels(spec)
            mem_stat = self.memory.worker_stats(spec.worker_id)
            return {
                "worker_id": health.worker_id, "ok": health.ok,
                "summary": health.summary,
                "framework_label": labels["framework_label"],
                "backend_label": labels["backend_label"],
                "target_label": labels["target_label"],
                "memory_calls": mem_stat.get("total_calls", 0),
                "memory_success_rate": mem_stat.get("success_rate"),
            }
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as executor:
            rows = list(executor.map(collect, specs))
        self._health_cache = (perf_counter(), rows)
        return [dict(row) for row in rows]

    def ping(self, mode: str = "quick") -> list[dict[str, object]]:
        ping_mode = mode.strip().lower()
        if ping_mode not in {"quick", "deep"}:
            raise ValueError("Ping mode must be 'quick' or 'deep'.")
        method_name = "quick_ping" if ping_mode == "quick" else "deep_ping"

        def collect(spec: WorkerSpec) -> dict[str, object]:
            labels = self._worker_labels(spec)
            worker = self.registry.get(spec.worker_id)
            try:
                payload = getattr(worker, method_name)()
            except Exception as exc:
                payload = {
                    "ok": False,
                    "summary": str(exc).strip(),
                    "deliverable": "",
                    "latency_ms": 0,
                    "ping_mode": ping_mode,
                }
            return {
                "worker_id": spec.worker_id,
                "ok": bool(payload.get("ok")),
                "latency_ms": int(payload.get("latency_ms", 0) or 0),
                "summary": str(payload.get("summary") or f"{ping_mode} ping ok").strip(),
                "deliverable": str(payload.get("deliverable") or "").strip(),
                "framework_label": labels["framework_label"],
                "backend_label": labels["backend_label"],
                "target_label": labels["target_label"],
                "ping_mode": str(payload.get("ping_mode") or ping_mode),
            }

        specs = self.registry.all_enabled_specs()
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as executor:
            return list(executor.map(collect, specs))

    def quick_ping(self) -> list[dict[str, object]]:
        return self.ping("quick")

    def deep_ping(self) -> list[dict[str, object]]:
        return self.ping("deep")

    def smoke_test(self) -> list[dict[str, object]]:
        prompt = (
            'Return one compact JSON object only: '
            '{"status":"completed","summary":"smoke ok","deliverable":"worker reachable",'
            '"artifacts":[],"changes_made":[],"risks":[],"recommended_next_steps":[],"confidence":0.95}'
        )

        def collect(spec: WorkerSpec) -> dict[str, object]:
            labels = self._worker_labels(spec)
            worker = self.registry.get(spec.worker_id)
            started = perf_counter()
            try:
                payload = worker.invoke_json(prompt=prompt, allow_write=False)
                return {
                    "worker_id": spec.worker_id,
                    "ok": True,
                    "latency_ms": int((perf_counter() - started) * 1000),
                    "summary": str(payload.get("summary") or "smoke ok").strip(),
                    "deliverable": str(payload.get("deliverable") or "").strip(),
                    "framework_label": labels["framework_label"],
                    "backend_label": labels["backend_label"],
                    "target_label": labels["target_label"],
                }
            except Exception as exc:
                return {
                    "worker_id": spec.worker_id,
                    "ok": False,
                    "latency_ms": int((perf_counter() - started) * 1000),
                    "summary": str(exc).strip(),
                    "deliverable": "",
                    "framework_label": labels["framework_label"],
                    "backend_label": labels["backend_label"],
                    "target_label": labels["target_label"],
                }

        specs = self.registry.all_enabled_specs()
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as executor:
            return list(executor.map(collect, specs))

    def list_report_paths(self, limit: int = 50) -> list[Path]:
        reports = sorted(self.runs_dir.glob("mvp_run_*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        return reports[:limit]

    def load_report_payload(self, report_path: str | Path) -> dict[str, object]:
        return load_json_file(Path(report_path))

    def framework_timeline(self, limit: int = 60) -> list[dict[str, object]]:
        return read_framework_timeline(self.workspace_root, self.runs_dir, limit=limit)

    def system_snapshot(self) -> dict[str, object]:
        return collect_system_stats(self.workspace_root)

    def run(
        self,
        task: str,
        profile_name: str,
        plan_only: bool = False,
        event_callback: RunEventCallback | None = None,
        run_control: RunControl | None = None,
    ) -> RunReport:
        """Execute a task through the ReAct reasoning loop."""
        started_total = perf_counter()
        profile = self.get_profile(profile_name)
        ctx = AgentContext(task=task, profile=profile)

        # --- Phase 1: Observe — recall memory ---
        self._think(ctx, f"Observing task: {task[:120]}", action="observe")
        ctx.recalled_tasks = self.memory.recall_similar_tasks(task, limit=5)
        ctx.recalled_pattern = self.memory.recall_pattern(task)
        if hasattr(self.memory, "recall_skill_profile"):
            ctx.recalled_skill_profile = self.memory.recall_skill_profile(task)
        if ctx.recalled_pattern and ctx.recalled_pattern.get("success"):
            self._think(ctx, f"Found successful past pattern: {ctx.recalled_pattern['subtask_kinds']}")
        if ctx.recalled_skill_profile and ctx.recalled_skill_profile.get("success"):
            self._think(
                ctx,
                f"Found successful skill profile: {ctx.recalled_skill_profile.get('required_skills', [])}",
            )

        # Gather code context
        if self._is_code_task(task):
            repo_hints = self.planner.repo_index.infer_files(task, limit=8)
            if repo_hints:
                ctx.code_context = self.code_intel.analyze_many(repo_hints[:5])
                symbols_found = sum(len(f.symbols) for f in ctx.code_context.values())
                self._think(ctx, f"Code analysis: {len(ctx.code_context)} files, {symbols_found} symbols")

        # --- Phase 2: Reason — plan ---
        self._think(ctx, "Reasoning about task decomposition...", action="plan")
        plan = self.planner.create_plan(task, profile, run_control=run_control)

        # Enhance plan with memory context
        if ctx.recalled_pattern:
            plan = self._enrich_plan_with_memory(plan, ctx.recalled_pattern)
        if ctx.recalled_skill_profile:
            plan = self._enrich_plan_with_skill_memory(plan, ctx.recalled_skill_profile)

        self._emit(event_callback, "run_started", progress=0.05,
                   message=f"MVP Agent analyzing task: {task[:80]}",
                   profile=profile.name, plan_only=plan_only)

        performance: dict[str, Any] = {
            "planning_ms": 0, "execution_ms": 0, "review_ms": 0,
            "synthesis_ms": 0, "total_ms": 0, "assignments": [],
            "memory_recall_count": len(ctx.recalled_tasks),
            "required_skills": list(plan.required_skills),
            "capability_gaps": list(plan.capability_gaps),
            "doctrine": list(plan.doctrine),
        }
        leader_trace: list[LeaderTraceEntry] = []
        planned_assignments: list[Assignment] = []
        outcomes: list[AssignmentOutcome] = []
        status = "unknown"
        leader_notes = ""
        synthesis_worker = ""
        synthesis_raw = ""

        # Route subtasks — memory-aware
        worker_metrics = summarize_worker_metrics(self.runs_dir, limit=80)
        per_kind_metrics = summarize_worker_metrics_by_kind(self.runs_dir, limit=30)
        # Merge in memory-based metrics
        mem_stats = self.memory.all_worker_stats()
        for wid, stats in mem_stats.items():
            if wid not in worker_metrics:
                worker_metrics[wid] = {}
            worker_metrics[wid].setdefault("avg_duration_ms", stats.get("avg_duration_ms", 0))
            mem_failure = 1.0 - stats.get("success_rate", 1.0)
            current_failure = worker_metrics[wid].get("failure_ratio", 0)
            worker_metrics[wid]["failure_ratio"] = max(current_failure, mem_failure) if mem_failure > 0 else current_failure

        for subtask in plan.subtasks:
            assignment = route_subtask(
                subtask, self.worker_specs, profile,
                worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics,
            )
            planned_assignments.append(assignment)
        self._record_trace(leader_trace, "planning", "plan_created",
                           f"Agent planned {len(plan.subtasks)} subtasks: {plan.summary}")

        planning_ms = int((perf_counter() - started_total) * 1000)
        performance["planning_ms"] = planning_ms
        self._emit(event_callback, "planning_completed", progress=0.18 if not plan_only else 1.0,
                   message=f"Plan: {len(planned_assignments)} subtasks ready.",
                   assignments=[{"task_id": a.subtask.task_id, "title": a.subtask.title,
                                 "worker_id": a.worker_id, "kind": a.subtask.kind}
                                for a in planned_assignments])

        if plan_only:
            status, leader_notes = "planned", "Agent produced plan without execution."
            return self._build_report(task, profile_name, profile, status, leader_notes,
                                      plan, planned_assignments, outcomes, performance,
                                      leader_trace, "", "", started_total)

        # --- Phase 3: Act — execute ---
        try:
            progress = ProgressTracker(len(planned_assignments) * 3)
            completed_outcomes: dict[str, AssignmentOutcome] = {}
            execution_started = perf_counter()

            ctx.total_count = len(planned_assignments)
            for i, assignment in enumerate(planned_assignments):
                self._raise_if_cancelled(run_control)
                ctx.completed_count = i
                self._think(ctx, f"Delegating: {assignment.subtask.title} -> {assignment.worker_id}",
                            action="delegate", worker_id=assignment.worker_id)

                outcome, perf_row = self._execute_one(
                    assignment, profile, event_callback, run_control,
                    progress, i + 1, len(planned_assignments),
                    worker_metrics, per_kind_metrics, completed_outcomes,
                )
                outcomes.append(outcome)
                completed_outcomes[outcome.assignment.subtask.task_id] = outcome
                performance["execution_ms"] += int(perf_row.get("execution_ms", 0))
                performance["review_ms"] += int(perf_row.get("review_ms", 0))
                performance["assignments"].append(perf_row)

                # --- Phase 4: Reflect — learn from each subtask ---
                self._reflect_and_learn(ctx, outcome)

            performance["execution_ms"] = int((perf_counter() - execution_started) * 1000)

            # --- Phase 5: Synthesize ---
            self._think(ctx, "Synthesizing final response...", action="synthesize")
            synthesis_started = perf_counter()
            synthesis_worker, synthesis_raw = self._synthesize(task, profile, outcomes)
            performance["synthesis_ms"] = int((perf_counter() - synthesis_started) * 1000)

            status, leader_notes = self._determine_status(outcomes)
            if synthesis_raw:
                leader_notes = synthesis_raw

        except RunCancelled as exc:
            status = "cancelled"
            leader_notes = str(exc).strip() or "Task cancelled by user."
            self._emit(event_callback, "run_cancelled", progress=0.9, message=leader_notes)

        # --- Phase 6: Learn — persist to memory ---
        self._persist_learning(task, profile, plan, outcomes, status)

        report = self._build_report(task, profile_name, profile, status, leader_notes,
                                    plan, planned_assignments, outcomes, performance,
                                    leader_trace, synthesis_worker, synthesis_raw, started_total)
        report.report_path = str(self._persist_report(report))
        self._emit(event_callback, "run_completed", progress=1.0,
                   message=f"Agent complete: {status}", status=status,
                   report_path=report.report_path)
        return report

    # -- internal: execution ---------------------------------------------------

    def _execute_one(
        self, assignment, profile, event_callback, run_control,
        progress, task_index, total_tasks, worker_metrics, per_kind_metrics,
        upstream_results,
    ) -> tuple[AssignmentOutcome, dict[str, Any]]:
        """Execute a single assignment with review and fallback."""
        subtask = assignment.subtask
        perf_row = {"task_id": subtask.task_id, "title": subtask.title,
                     "execution_ms": 0, "review_ms": 0, "reroutes": 0,
                     "worker_id": assignment.worker_id}
        reroute_budget = 2
        current = assignment
        attempted: set[str] = set()
        previous_attempt = None

        while True:
            self._emit(event_callback, "assignment_started",
                       progress=self._progress(progress),
                       message=f"Executing: {subtask.title}",
                       worker_id=current.worker_id, task_id=subtask.task_id)

            exec_start = perf_counter()
            try:
                current, payload = self._invoke_worker(
                    current, profile, event_callback, run_control,
                    attempted, worker_metrics, per_kind_metrics,
                    upstream_results, previous_attempt,
                )
            except RuntimeError as exc:
                # All fallbacks exhausted
                result = WorkerResult(worker_id="none", status="error",
                                      summary=str(exc), deliverable="",
                                      artifacts=[], changes_made=[], risks=[str(exc)],
                                      recommended_next_steps=[], confidence=0.0, raw_response="")
                review = ReviewResult(reviewer_worker="", decision="fail",
                                      summary="All workers failed.", findings=[str(exc)],
                                      confidence=0.0, raw_response="")
                return AssignmentOutcome(assignment=current, result=result, review=review), perf_row

            perf_row["execution_ms"] += int((perf_counter() - exec_start) * 1000)
            perf_row["worker_id"] = current.worker_id
            attempted.add(current.worker_id)

            result = WorkerResult(
                worker_id=current.worker_id,
                status=str(payload.get("status") or "partial").strip().lower(),
                summary=str(payload.get("summary") or "").strip(),
                deliverable=str(payload.get("deliverable") or "").strip(),
                artifacts=normalize_str_list(payload.get("artifacts")),
                changes_made=normalize_str_list(payload.get("changes_made")),
                risks=normalize_str_list(payload.get("risks")),
                recommended_next_steps=normalize_str_list(payload.get("recommended_next_steps")),
                confidence=clamp_float(payload.get("confidence"), 0.0, 1.0, 0.5),
                raw_response=str(payload.get("_raw_response", "")),
            )
            progress.advance()

            # Review
            review_start = perf_counter()
            review = self.reviewer.review(subtask, result, profile, run_control=run_control)
            perf_row["review_ms"] += int((perf_counter() - review_start) * 1000)
            progress.advance()

            if result.status in self.FAILED_RESULT_STATUSES and reroute_budget > 0:
                fallback = self._pick_fallback(subtask, profile, attempted, worker_metrics, per_kind_metrics)
                if fallback:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current = self._reroute(fallback, f"execution {result.status}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": [f"Failed: {result.status}"],
                    }
                    self._emit(event_callback, "assignment_rerouted",
                               message=f"Rerouting {subtask.title} -> {current.worker_id}",
                               task_id=subtask.task_id, previous_worker=result.worker_id,
                               next_worker=current.worker_id)
                    continue

            if review.decision == "fail" and reroute_budget > 0:
                fallback = self._pick_fallback(subtask, profile, attempted, worker_metrics, per_kind_metrics)
                if fallback:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current = self._reroute(fallback, f"review fail by {review.reviewer_worker}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": review.findings,
                    }
                    self._emit(event_callback, "assignment_rerouted",
                               message=f"Review fail — rerouting {subtask.title} -> {current.worker_id}",
                               task_id=subtask.task_id, previous_worker=result.worker_id,
                               next_worker=current.worker_id)
                    continue

            return AssignmentOutcome(assignment=current, result=result, review=review), perf_row

    def _invoke_worker(self, assignment, profile, event_callback, run_control,
                       exclude, worker_metrics, per_kind_metrics,
                       upstream_results, previous_attempt):
        prompt = build_execution_prompt(assignment, mode="delegated",
                                        upstream_results=upstream_results,
                                        previous_attempt=previous_attempt)
        ranked = rank_workers(assignment.subtask, self.worker_specs, profile,
                              exclude=exclude, worker_metrics=worker_metrics,
                              per_kind_metrics=per_kind_metrics)
        last_error = ""
        for pos, candidate in enumerate(ranked):
            self._raise_if_cancelled(run_control)
            worker = self.registry.get(candidate.worker_id)
            try:
                payload, _ = self._invoke_json_with_modes(
                    worker,
                    prompt=prompt,
                    allow_write=candidate.subtask.needs_write_access,
                    run_control=run_control,
                    modes=self._execution_modes(candidate, profile),
                )
                if candidate.worker_id != assignment.worker_id:
                    candidate = self._reroute(candidate, f"fallback: {assignment.worker_id}")
                return candidate, payload
            except Exception as exc:
                last_error = f"{candidate.worker_id}: {exc}"
                if pos + 1 < len(ranked):
                    self._emit(event_callback, "assignment_rerouted",
                               message=f"{candidate.worker_id} failed, trying {ranked[pos + 1].worker_id}",
                               task_id=assignment.subtask.task_id,
                               previous_worker=candidate.worker_id,
                               next_worker=ranked[pos + 1].worker_id)
        raise RuntimeError(f"All workers exhausted. Last: {last_error}")

    def _synthesize(self, task, profile, outcomes):
        outcome_dicts = [{
            "title": o.assignment.subtask.title,
            "worker_id": o.assignment.worker_id,
            "status": o.result.status,
            "summary": o.result.summary,
            "deliverable": o.result.deliverable,
            "review_decision": o.review.decision if o.review else "none",
        } for o in outcomes]
        prompt = build_synthesis_prompt(task, profile.name, outcome_dicts)
        if profile.name == "balanced":
            candidates = [
                "claude_strategist",
                "ollama_planner",
                "ollama_reviewer",
                "hermes_designer",
                "openclaw_coder",
            ]
        elif profile.name == "premium":
            candidates = [
                "hermes_designer",
                "claude_strategist",
                "codex_architect",
                "ollama_planner",
            ]
        else:
            candidates = ["ollama_planner", "ollama_reviewer", "hermes_designer"]
        for c in candidates:
            if c not in self.worker_specs or not self.worker_specs[c].enabled:
                continue
            try:
                worker = self.registry.get(c)
                payload, _ = self._invoke_json_with_modes(
                    worker,
                    prompt=prompt,
                    allow_write=False,
                    run_control=None,
                    modes=self._synthesis_modes(c, profile),
                )
                synthesis = str(payload.get("synthesis") or "").strip()
                if synthesis:
                    findings = normalize_str_list(payload.get("key_findings"))
                    gaps = normalize_str_list(payload.get("gaps"))
                    parts = [synthesis]
                    if findings:
                        parts.append("\nKey findings:\n" + "\n".join(f"- {f}" for f in findings))
                    if gaps:
                        parts.append("\nTo be addressed:\n" + "\n".join(f"- {g}" for g in gaps))
                    return c, "\n\n".join(parts)
            except Exception:
                continue
        return "", ""

    def _invoke_json_with_modes(self, worker, *, prompt: str, allow_write: bool, run_control, modes: list[str]) -> tuple[dict[str, Any], str]:
        normalized_modes: list[str] = []
        for mode in modes or ["default"]:
            normalized = (mode or "default").strip().lower() or "default"
            if normalized not in normalized_modes:
                normalized_modes.append(normalized)
        last_exc: Exception | None = None
        for mode in normalized_modes:
            try:
                if hasattr(worker, "invoke_json_mode"):
                    payload = worker.invoke_json_mode(
                        prompt,
                        allow_write=allow_write,
                        run_control=run_control,
                        mode=mode,
                    )
                else:
                    payload = worker.invoke_json(
                        prompt,
                        allow_write=allow_write,
                        run_control=run_control,
                    )
                return payload, mode
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No invocation modes available.")

    def _execution_modes(self, assignment, profile) -> list[str]:
        spec = self.worker_specs.get(assignment.worker_id)
        if spec is None:
            return ["default"]
        subtask = assignment.subtask
        mode_family = worker_mode_family(spec)
        if mode_family == "local_llm":
            if not subtask.needs_write_access and subtask.difficulty <= 2 and subtask.risk <= 2:
                return ["quick"]
            return ["deep", "quick"]
        if mode_family == "service_mesh":
            if not subtask.needs_write_access and subtask.kind in {
                "planning", "architecture", "design", "requirements",
                "review", "documentation", "summarization", "coordination",
            }:
                return ["quick", "deep"]
            return ["deep", "quick"]
        if mode_family == "extension_bridge":
            if not subtask.needs_write_access:
                return ["quick", "deep"]
            return ["deep"]
        if mode_family != "agent_cli":
            return ["default"]

        quick_friendly_kinds = {
            "planning", "architecture", "design", "requirements",
            "research", "documentation", "summarization", "coordination", "review",
        }
        if not subtask.needs_write_access and subtask.kind in quick_friendly_kinds:
            return ["quick", "deep"]
        if (
            profile.name == "balanced"
            and not subtask.needs_write_access
            and subtask.difficulty <= 3
            and subtask.risk <= 3
        ):
            return ["quick", "deep"]
        if (
            profile.name == "balanced"
            and spec.worker_id == "openclaw_coder"
            and subtask.kind in {"testing", "qa"}
            and subtask.difficulty <= 3
        ):
            return ["quick", "deep"]
        return ["deep"]

    def _synthesis_modes(self, worker_id: str, profile) -> list[str]:
        spec = self.worker_specs.get(worker_id)
        if spec is None:
            return ["default"]
        mode_family = worker_mode_family(spec)
        if mode_family == "local_llm":
            return ["quick", "deep"] if profile.name != "cheap" else ["quick"]
        if mode_family in {"agent_cli", "extension_bridge", "service_mesh"}:
            if profile.name == "balanced":
                return ["quick"]
            return ["quick", "deep"]
        return ["default"]

    # -- internal: memory & learning -------------------------------------------

    def _reflect_and_learn(self, ctx: AgentContext, outcome: AssignmentOutcome) -> None:
        """After each subtask, reflect and update memory."""
        success = outcome.result.status == "completed" and (
            outcome.review is None or outcome.review.decision in {"pass", "revise"}
        )
        self.memory.record_worker_perf(
            worker_id=outcome.assignment.worker_id,
            task_kind=outcome.assignment.subtask.kind,
            success=success,
            duration_ms=0,
            review_decision=outcome.review.decision if outcome.review else "",
            task_id=outcome.assignment.subtask.task_id,
        )

    def _persist_learning(self, task, profile, plan, outcomes, status):
        """Persist full task experience to long-term memory."""
        try:
            self.memory.remember_task(
                task=task,
                profile=profile.name,
                plan_json=plan.to_dict(),
                outcomes_json=[o.to_dict() for o in outcomes],
                status=status,
                tags=self._extract_tags(task),
            )
            subtask_kinds = [s.kind for s in plan.subtasks]
            worker_ids = [o.assignment.worker_id for o in outcomes] if outcomes else []
            self.memory.learn_pattern(
                task=task,
                subtask_kinds=subtask_kinds,
                worker_assignments=worker_ids,
                success=status == "completed",
            )
            if hasattr(self.memory, "remember_skill_profile"):
                self.memory.remember_skill_profile(
                    task=task,
                    required_skills=list(plan.required_skills),
                    capability_gaps=list(plan.capability_gaps),
                    success=status == "completed",
                )
        except Exception:
            pass  # Memory persistence is best-effort

    def _enrich_plan_with_memory(self, plan, pattern):
        """Use recalled patterns to improve the plan."""
        if not pattern or not pattern.get("success"):
            return plan
        # If pattern suggests successful worker assignments, note them
        remembered_workers = pattern.get("worker_assignments", [])
        if remembered_workers and not plan.execution_strategy.endswith(" [memory-augmented]"):
            plan = plan.__class__(
                summary=plan.summary,
                execution_strategy=plan.execution_strategy + " [memory-augmented]",
                subtasks=plan.subtasks,
                planner_worker=plan.planner_worker,
                raw_response=plan.raw_response,
                doctrine=plan.doctrine,
                required_skills=plan.required_skills,
                capability_gaps=plan.capability_gaps,
            )
        return plan

    def _enrich_plan_with_skill_memory(self, plan, skill_profile):
        if not skill_profile or not skill_profile.get("success"):
            return plan
        required_skills = list(plan.required_skills)
        for item in skill_profile.get("required_skills", []):
            skill_id = str(item).strip()
            if skill_id and skill_id not in required_skills:
                required_skills.append(skill_id)
        capability_gaps = list(plan.capability_gaps)
        for item in skill_profile.get("capability_gaps", []):
            gap = str(item).strip()
            if gap and gap not in capability_gaps:
                capability_gaps.append(gap)
        return plan.__class__(
            summary=plan.summary,
            execution_strategy=plan.execution_strategy,
            subtasks=plan.subtasks,
            planner_worker=plan.planner_worker,
            raw_response=plan.raw_response,
            doctrine=plan.doctrine,
            required_skills=required_skills,
            capability_gaps=capability_gaps,
        )

    # -- internal: helpers -----------------------------------------------------

    def _think(self, ctx: AgentContext, content: str, action: str = "", worker_id: str = "") -> None:
        ctx.thoughts.append(Thought(content=content, action=action, worker_id=worker_id))

    def _is_code_task(self, task: str) -> bool:
        lowered = task.lower()
        return any(kw in lowered for kw in (
            "code", "coding", "function", "class", "refactor", "bug", "fix",
            "implement", "test", "file", "module", "patch", "代码", "编码",
            "重构", "修复", "实现", "测试",
        ))

    def _extract_tags(self, task: str) -> list[str]:
        tags: list[str] = []
        lowered = task.lower()
        if any(kw in lowered for kw in ("code", "coding", "代码", "编码")):
            tags.append("code")
        if any(kw in lowered for kw in ("bug", "fix", "修复", "缺陷")):
            tags.append("bug")
        if any(kw in lowered for kw in ("refactor", "重构")):
            tags.append("refactor")
        if any(kw in lowered for kw in ("docs", "document", "文档")):
            tags.append("docs")
        if any(kw in lowered for kw in ("test", "测试")):
            tags.append("testing")
        return tags

    def _pick_fallback(self, subtask, profile, attempted, worker_metrics, per_kind_metrics):
        ranked = rank_workers(subtask, self.worker_specs, profile,
                              exclude=attempted, worker_metrics=worker_metrics,
                              per_kind_metrics=per_kind_metrics)
        return ranked[0] if ranked else None

    def _reroute(self, assignment, reason):
        return Assignment(subtask=assignment.subtask, worker_id=assignment.worker_id,
                          score=assignment.score,
                          rationale=[*assignment.rationale, reason])

    def _determine_status(self, outcomes):
        if not outcomes:
            return "empty", "No outcomes produced."
        fails = sum(1 for o in outcomes if o.review and o.review.decision == "fail")
        revises = sum(1 for o in outcomes if o.review and o.review.decision == "revise")
        if fails > 0:
            return "needs_revision", f"{fails} subtask(s) failed review."
        if revises > 0:
            return "partial", f"{revises} subtask(s) need revision."
        if any(o.result.status != "completed" for o in outcomes):
            return "partial", "Some subtasks did not complete."
        return "completed", "All subtasks passed review."

    def _build_report(self, task, profile_name, profile, status, leader_notes,
                      plan, planned_assignments, outcomes, performance,
                      leader_trace, synthesis_worker, synthesis_raw, started_total):
        performance["total_ms"] = int((perf_counter() - started_total) * 1000)
        return RunReport(
            task=task, mode=profile_name, profile=profile.name, status=status,
            leader_notes=leader_notes, plan=plan,
            planned_assignments=planned_assignments, outcomes=outcomes,
            performance=performance, leader_trace=leader_trace,
            synthesis_worker=synthesis_worker, synthesis_raw=synthesis_raw,
        )

    def _persist_report(self, report: RunReport) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.runs_dir / f"mvp_run_{timestamp}.json"
        save_json_file(path, report.to_dict())
        return path

    def _progress(self, tracker: ProgressTracker) -> float:
        c, t = tracker.snapshot()
        if t <= 0:
            return 0.9
        return 0.18 + (c / t) * 0.72

    def _emit(self, callback, event_type, **payload):
        if callback is None:
            return
        callback({"type": event_type, **payload})

    def _raise_if_cancelled(self, run_control):
        if run_control is not None:
            run_control.raise_if_cancelled()

    def _record_trace(self, trace, phase, decision, detail, worker_id="", task_id="", score=0.0):
        trace.append(LeaderTraceEntry(
            timestamp=datetime.now().isoformat(), phase=phase, decision=decision,
            detail=detail, worker_id=worker_id, task_id=task_id, score=score,
        ))

    def _worker_labels(self, spec: WorkerSpec) -> dict[str, str]:
        return describe_worker_platform(spec)

    def run(
        self,
        task: str,
        profile_name: str,
        plan_only: bool = False,
        event_callback: RunEventCallback | None = None,
        run_control: RunControl | None = None,
    ) -> RunReport:
        started_total = perf_counter()
        profile = self.get_profile(profile_name)
        ctx = AgentContext(task=task, profile=profile)

        self._think(ctx, f"Observing task: {task[:120]}", action="observe")
        ctx.recalled_tasks = self.memory.recall_similar_tasks(task, limit=5)
        ctx.recalled_pattern = self.memory.recall_pattern(task)
        if ctx.recalled_pattern and ctx.recalled_pattern.get("success"):
            self._think(ctx, f"Found successful past pattern: {ctx.recalled_pattern['subtask_kinds']}")

        if self._is_code_task(task):
            repo_hints = self.planner.repo_index.infer_files(task, limit=8)
            if repo_hints:
                ctx.code_context = self.code_intel.analyze_many(repo_hints[:5])
                symbols_found = sum(len(info.symbols) for info in ctx.code_context.values())
                self._think(ctx, f"Code analysis: {len(ctx.code_context)} files, {symbols_found} symbols")

        self._think(ctx, "Reasoning about task decomposition...", action="plan")
        plan = self.planner.create_plan(task, profile, run_control=run_control)
        if ctx.recalled_pattern:
            plan = self._enrich_plan_with_memory(plan, ctx.recalled_pattern)

        self._emit(
            event_callback,
            "run_started",
            progress=0.05,
            message=f"MVP Agent 正在分析任务：{task[:80]}",
            profile=profile.name,
            plan_only=plan_only,
        )

        performance: dict[str, Any] = {
            "planning_ms": 0,
            "execution_ms": 0,
            "review_ms": 0,
            "synthesis_ms": 0,
            "total_ms": 0,
            "assignments": [],
            "memory_recall_count": len(ctx.recalled_tasks),
        }
        leader_trace: list[LeaderTraceEntry] = []
        planned_assignments: list[Assignment] = []
        outcomes: list[AssignmentOutcome] = []
        status = "unknown"
        leader_notes = ""
        synthesis_worker = ""
        synthesis_raw = ""

        worker_metrics = summarize_worker_metrics(self.runs_dir, limit=80)
        per_kind_metrics = summarize_worker_metrics_by_kind(self.runs_dir, limit=30)
        mem_stats = self.memory.all_worker_stats()
        for worker_id, stats in mem_stats.items():
            if worker_id not in worker_metrics:
                worker_metrics[worker_id] = {}
            worker_metrics[worker_id].setdefault("avg_duration_ms", stats.get("avg_duration_ms", 0))
            mem_failure = 1.0 - stats.get("success_rate", 1.0)
            current_failure = worker_metrics[worker_id].get("failure_ratio", 0)
            worker_metrics[worker_id]["failure_ratio"] = (
                max(current_failure, mem_failure) if mem_failure > 0 else current_failure
            )

        for subtask in plan.subtasks:
            planned_assignments.append(
                route_subtask(
                    subtask,
                    self.worker_specs,
                    profile,
                    worker_metrics=worker_metrics,
                    per_kind_metrics=per_kind_metrics,
                )
            )
        self._record_trace(
            leader_trace,
            "planning",
            "plan_created",
            f"Agent planned {len(plan.subtasks)} subtasks: {plan.summary}",
        )

        performance["planning_ms"] = int((perf_counter() - started_total) * 1000)
        self._emit(
            event_callback,
            "planning_completed",
            progress=0.18 if not plan_only else 1.0,
            message=f"已生成 {len(planned_assignments)} 个子任务。",
            assignments=[
                {
                    "task_id": assignment.subtask.task_id,
                    "title": assignment.subtask.title,
                    "worker_id": assignment.worker_id,
                    "kind": assignment.subtask.kind,
                }
                for assignment in planned_assignments
            ],
        )

        if plan_only:
            return self._build_report(
                task,
                profile_name,
                profile,
                "planned",
                "Agent produced plan without execution.",
                plan,
                planned_assignments,
                outcomes,
                performance,
                leader_trace,
                "",
                "",
                started_total,
            )

        try:
            progress = ProgressTracker(len(planned_assignments) * 3)
            completed_outcomes: dict[str, AssignmentOutcome] = {}
            execution_started = perf_counter()
            assignment_order = {
                assignment.subtask.task_id: index
                for index, assignment in enumerate(planned_assignments, start=1)
            }
            batches = self._build_execution_batches(planned_assignments, profile)

            ctx.total_count = len(planned_assignments)
            for batch_index, batch in enumerate(batches, start=1):
                self._raise_if_cancelled(run_control)
                if len(batch) > 1:
                    self._emit(
                        event_callback,
                        "batch_started",
                        progress=self._progress(progress),
                        message=f"MVP Agent 正在并行推进第 {batch_index} 批，共 {len(batch)} 个文件级编码包。",
                        batch_index=batch_index,
                        batch_size=len(batch),
                        task_ids=[assignment.subtask.task_id for assignment in batch],
                    )

                for assignment in batch:
                    self._think(
                        ctx,
                        f"Delegating: {assignment.subtask.title} -> {assignment.worker_id}",
                        action="delegate",
                        worker_id=assignment.worker_id,
                    )

                batch_results = self._execute_batch(
                    batch=batch,
                    profile=profile,
                    event_callback=event_callback,
                    run_control=run_control,
                    progress=progress,
                    total_tasks=len(planned_assignments),
                    assignment_order=assignment_order,
                    worker_metrics=worker_metrics,
                    per_kind_metrics=per_kind_metrics,
                    completed_outcomes=completed_outcomes,
                )
                for outcome, perf_row in batch_results:
                    outcomes.append(outcome)
                    completed_outcomes[outcome.assignment.subtask.task_id] = outcome
                    performance["execution_ms"] += int(perf_row.get("execution_ms", 0))
                    performance["review_ms"] += int(perf_row.get("review_ms", 0))
                    performance["assignments"].append(perf_row)
                    self._reflect_and_learn(ctx, outcome)

            outcomes.sort(key=lambda item: assignment_order.get(item.assignment.subtask.task_id, 999))
            performance["assignments"].sort(
                key=lambda item: assignment_order.get(str(item.get("task_id", "")), 999)
            )
            performance["execution_ms"] = int((perf_counter() - execution_started) * 1000)

            self._think(ctx, "Synthesizing final response...", action="synthesize")
            synthesis_started = perf_counter()
            synthesis_worker, synthesis_raw = self._synthesize(task, profile, outcomes)
            performance["synthesis_ms"] = int((perf_counter() - synthesis_started) * 1000)

            status, leader_notes = self._determine_status(outcomes)
            if synthesis_raw:
                leader_notes = synthesis_raw
        except RunCancelled as exc:
            status = "cancelled"
            leader_notes = str(exc).strip() or "Task cancelled by user."
            self._emit(event_callback, "run_cancelled", progress=0.9, message=leader_notes)

        self._persist_learning(task, profile, plan, outcomes, status)
        report = self._build_report(
            task,
            profile_name,
            profile,
            status,
            leader_notes,
            plan,
            planned_assignments,
            outcomes,
            performance,
            leader_trace,
            synthesis_worker,
            synthesis_raw,
            started_total,
        )
        report.report_path = str(self._persist_report(report))
        self._emit(
            event_callback,
            "run_completed",
            progress=1.0,
            message=f"Agent complete: {status}",
            status=status,
            report_path=report.report_path,
        )
        return report

    def _execute_one(
        self,
        assignment,
        profile,
        event_callback,
        run_control,
        progress,
        task_index,
        total_tasks,
        worker_metrics,
        per_kind_metrics,
        upstream_results,
    ) -> tuple[AssignmentOutcome, dict[str, Any]]:
        subtask = assignment.subtask
        perf_row = {
            "task_id": subtask.task_id,
            "title": subtask.title,
            "execution_ms": 0,
            "review_ms": 0,
            "reroutes": 0,
            "worker_id": assignment.worker_id,
        }
        reroute_budget = 2
        current = assignment
        attempted: set[str] = set()
        previous_attempt = None

        while True:
            scope = self.planner.repo_index.describe_scope(subtask.target_files)
            self._emit(
                event_callback,
                "assignment_started",
                progress=self._progress(progress),
                message=f"正在执行子任务 {task_index}/{total_tasks}：{subtask.title}",
                worker_id=current.worker_id,
                task_id=subtask.task_id,
                target_files=subtask.target_files,
                scope=scope,
            )

            exec_start = perf_counter()
            try:
                current, payload = self._invoke_worker(
                    current,
                    profile,
                    event_callback,
                    run_control,
                    attempted,
                    worker_metrics,
                    per_kind_metrics,
                    upstream_results,
                    previous_attempt,
                )
            except RuntimeError as exc:
                result = WorkerResult(
                    worker_id="none",
                    status="error",
                    summary=str(exc),
                    deliverable="",
                    artifacts=[],
                    changes_made=[],
                    risks=[str(exc)],
                    recommended_next_steps=[],
                    confidence=0.0,
                    raw_response="",
                )
                review = ReviewResult(
                    reviewer_worker="",
                    decision="fail",
                    summary="All workers failed.",
                    findings=[str(exc)],
                    confidence=0.0,
                    raw_response="",
                )
                return AssignmentOutcome(assignment=current, result=result, review=review), perf_row

            perf_row["execution_ms"] += int((perf_counter() - exec_start) * 1000)
            perf_row["worker_id"] = current.worker_id
            attempted.add(current.worker_id)

            result = WorkerResult(
                worker_id=current.worker_id,
                status=str(payload.get("status") or "partial").strip().lower(),
                summary=str(payload.get("summary") or "").strip(),
                deliverable=str(payload.get("deliverable") or "").strip(),
                artifacts=normalize_str_list(payload.get("artifacts")),
                changes_made=normalize_str_list(payload.get("changes_made")),
                risks=normalize_str_list(payload.get("risks")),
                recommended_next_steps=normalize_str_list(payload.get("recommended_next_steps")),
                confidence=clamp_float(payload.get("confidence"), 0.0, 1.0, 0.5),
                raw_response=str(payload.get("_raw_response", "")),
            )
            progress.advance()
            self._emit(
                event_callback,
                "assignment_completed",
                progress=self._progress(progress),
                message=f"{subtask.title} 已完成执行，准备验收。",
                worker_id=current.worker_id,
                task_id=subtask.task_id,
                result_status=result.status,
                result_summary=result.summary or result.deliverable,
                target_files=subtask.target_files,
                scope=scope,
                changes_made=result.changes_made,
            )
            self._stream_worker_result(event_callback, subtask, result, progress)

            self._emit(
                event_callback,
                "review_started",
                progress=self._progress(progress),
                message=f"正在验收子任务：{subtask.title}",
                reviewer_worker=profile.reviewer_worker,
                task_id=subtask.task_id,
                target_files=subtask.target_files,
                scope=scope,
            )
            review_started = perf_counter()
            review = self.reviewer.review(subtask, result, profile, run_control=run_control)
            perf_row["review_ms"] += int((perf_counter() - review_started) * 1000)
            progress.advance()
            self._emit(
                event_callback,
                "review_completed",
                progress=self._progress(progress),
                message=f"{subtask.title} 验收结论：{review.decision}",
                reviewer_worker=review.reviewer_worker,
                task_id=subtask.task_id,
                decision=review.decision,
                summary=review.summary,
                target_files=subtask.target_files,
            )

            if result.status in self.FAILED_RESULT_STATUSES and reroute_budget > 0:
                fallback = self._pick_fallback(subtask, profile, attempted, worker_metrics, per_kind_metrics)
                if fallback:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current = self._reroute(fallback, f"execution {result.status}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": [f"Failed: {result.status}"],
                    }
                    self._emit(
                        event_callback,
                        "assignment_rerouted",
                        message=f"{subtask.title} 执行结果为 {result.status}，改派给 {current.worker_id}。",
                        task_id=subtask.task_id,
                        previous_worker=result.worker_id,
                        next_worker=current.worker_id,
                        reason=result.status,
                    )
                    continue

            if review.decision == "fail" and reroute_budget > 0:
                fallback = self._pick_fallback(subtask, profile, attempted, worker_metrics, per_kind_metrics)
                if fallback:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current = self._reroute(fallback, f"review fail by {review.reviewer_worker}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": review.findings,
                    }
                    self._emit(
                        event_callback,
                        "assignment_rerouted",
                        message=f"{subtask.title} 未通过验收，改派给 {current.worker_id} 继续处理。",
                        task_id=subtask.task_id,
                        previous_worker=result.worker_id,
                        next_worker=current.worker_id,
                        reason="review_fail",
                    )
                    continue

            return AssignmentOutcome(assignment=current, result=result, review=review), perf_row

    def _execute_batch(
        self,
        *,
        batch: list[Assignment],
        profile,
        event_callback,
        run_control,
        progress,
        total_tasks: int,
        assignment_order: dict[str, int],
        worker_metrics,
        per_kind_metrics,
        completed_outcomes,
    ) -> list[tuple[AssignmentOutcome, dict[str, Any]]]:
        upstream = completed_outcomes or {}

        def run_one(current_assignment: Assignment) -> tuple[AssignmentOutcome, dict[str, Any]]:
            return self._execute_one(
                current_assignment,
                profile,
                event_callback,
                run_control,
                progress,
                assignment_order.get(current_assignment.subtask.task_id, 1),
                total_tasks,
                worker_metrics,
                per_kind_metrics,
                upstream,
            )

        if len(batch) <= 1:
            return [run_one(batch[0])]

        results: list[tuple[AssignmentOutcome, dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=min(self._parallel_batch_limit(profile), len(batch))) as executor:
            future_map = {executor.submit(run_one, assignment): assignment for assignment in batch}
            for future in as_completed(future_map):
                results.append(future.result())
        results.sort(key=lambda item: assignment_order.get(item[0].assignment.subtask.task_id, 999))
        return results

    def _build_execution_batches(self, assignments: list[Assignment], profile) -> list[list[Assignment]]:
        pending = list(assignments)
        completed_ids: set[str] = set()
        batches: list[list[Assignment]] = []
        max_parallel = self._parallel_batch_limit(profile)

        while pending:
            ready = [
                assignment
                for assignment in pending
                if set(assignment.subtask.depends_on).issubset(completed_ids)
            ]
            if not ready:
                batch = [pending.pop(0)]
                batches.append(batch)
                completed_ids.update(item.subtask.task_id for item in batch)
                continue

            batch: list[Assignment] = []
            for candidate in ready:
                if len(batch) >= max_parallel:
                    break
                if self._can_parallelize_assignment(candidate, batch, profile):
                    batch.append(candidate)

            if not batch:
                batch = [ready[0]]

            batches.append(batch)
            batch_ids = {assignment.subtask.task_id for assignment in batch}
            pending = [assignment for assignment in pending if assignment.subtask.task_id not in batch_ids]
            completed_ids.update(batch_ids)

        return batches

    def _can_parallelize_assignment(self, candidate: Assignment, batch: list[Assignment], profile) -> bool:
        if not batch:
            return True

        batch_write_count = sum(1 for item in batch if item.subtask.needs_write_access)
        if candidate.subtask.needs_write_access and batch_write_count >= self._parallel_write_limit(profile):
            return False

        candidate_targets = set(candidate.subtask.target_files)
        candidate_deps = set(candidate.subtask.depends_on)
        for existing in batch:
            existing_targets = set(existing.subtask.target_files)
            if candidate_deps & {existing.subtask.task_id}:
                return False
            if set(existing.subtask.depends_on) & {candidate.subtask.task_id}:
                return False
            if candidate_targets and existing_targets and candidate_targets & existing_targets:
                return False

            if candidate.subtask.needs_write_access or existing.subtask.needs_write_access:
                if not candidate_targets or not existing_targets:
                    return False
                if candidate.subtask.parallel_group and existing.subtask.parallel_group:
                    if candidate.subtask.parallel_group != existing.subtask.parallel_group:
                        return False
                elif candidate.subtask.parallel_group != existing.subtask.parallel_group:
                    return False

        return True

    def _parallel_batch_limit(self, profile) -> int:
        if profile.name == "premium":
            return 3
        if profile.name == "balanced":
            return 2
        return 1

    def _parallel_write_limit(self, profile) -> int:
        return 2 if profile.name == "premium" else 1

    def _stream_worker_result(self, event_callback, subtask: Subtask, result: WorkerResult, progress: ProgressTracker) -> None:
        if not (subtask.target_files or result.changes_made or result.summary or result.deliverable):
            return
        chunks = build_code_stream_chunks(
            self.workspace_root,
            target_files=subtask.target_files,
            changes_made=result.changes_made,
            summary=result.summary,
            deliverable=result.deliverable,
        )
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            self._emit(
                event_callback,
                "assignment_stream",
                progress=self._progress(progress),
                message=f"{subtask.title} 代码回包 {index}/{total}",
                task_id=subtask.task_id,
                worker_id=result.worker_id,
                stream_text=chunk,
                chunk_index=index,
                chunk_total=total,
                target_files=subtask.target_files,
            )

    def memory_stats(self) -> dict[str, Any]:
        """Return a summary of the agent's memory store."""
        stats = self.memory.stats()
        payload = {
            "total_tasks_remembered": stats.total_tasks,
            "total_workers_tracked": stats.total_workers,
            "top_workers": stats.top_workers,
            "recent_patterns": stats.recent_patterns,
        }
        if hasattr(self.memory, "top_capability_gaps"):
            payload["top_capability_gaps"] = self.memory.top_capability_gaps()
        return payload

    def evolution_status(self) -> dict[str, Any]:
        top_gaps = self.memory.top_capability_gaps() if hasattr(self.memory, "top_capability_gaps") else []
        return {
            "skills_available": len(self.list_skills(limit=1000)),
            "core_skill_pack_enabled": self.skill_mesh is not None,
            "top_capability_gaps": top_gaps,
            "worker_count": len(self.worker_specs),
        }
