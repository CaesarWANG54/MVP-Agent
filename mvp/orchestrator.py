from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Any, Callable

from .audit import flush_audit, summarize_worker_metrics, summarize_worker_metrics_by_kind
from .config import build_profiles, build_worker_specs, load_config
from .framework_history import read_framework_timeline
from .models import Assignment, AssignmentOutcome, LeaderTraceEntry, RunProfile, RunReport, Subtask, TaskPlan, WorkerResult, WorkerSpec
from .planner import Planner
from .review import Reviewer
from .router import rank_workers, route_subtask
from .system_stats import collect_system_stats
from .utils import RunCancelled, RunControl, clamp_float, clamp_int, ensure_directory, load_json_file, normalize_str_list, save_json_file
from .worker_platform import describe_worker_platform
from .workers.registry import WorkerRegistry


RunEventCallback = Callable[[dict[str, Any]], None]


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


class MVPLoader:
    FAILED_RESULT_STATUSES = {"failed", "error", "blocked", "empty", "cancelled"}

    def __init__(self, config_path: str | None = None, memory_path: str | None = None) -> None:
        self.config = load_config(config_path)
        self.workspace_root = Path(self.config["workspace_root"]).resolve()
        self.runs_dir = ensure_directory(Path(self.config["runs_dir"]).resolve())
        self.worker_specs = build_worker_specs(self.config)
        self.profiles = build_profiles(self.config)
        self.registry = WorkerRegistry(self.worker_specs, self.workspace_root, self.runs_dir)
        self.planner = Planner(self.registry, self.worker_specs)
        self.reviewer = Reviewer(self.registry)
        self._health_cache: tuple[float, list[dict[str, object]]] | None = None
        self._health_cache_ttl_s = 45.0
        self._ping_cache: dict[str, tuple[float, list[dict[str, object]]]] = {}
        self._quick_ping_cache_ttl_s = 6.0
        # Optional long-term memory
        try:
            from .memory import MemoryStore
            mem_path = Path(memory_path) if memory_path else (self.runs_dir / "mvp_memory.db")
            self.memory: MemoryStore | None = MemoryStore(mem_path)
        except Exception:
            self.memory = None

    def get_profile(self, name: str) -> RunProfile:
        if name not in self.profiles:
            available = ", ".join(sorted(self.profiles))
            raise ValueError(f"Unknown profile '{name}'. Available: {available}")
        return self.profiles[name]

    def list_workers(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        metrics = self._worker_metrics()
        for spec in self.registry.all_enabled_specs():
            labels = self._worker_labels(spec)
            worker_metric = metrics.get(spec.worker_id, {})
            rows.append(
                {
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
                    "mode_label": "本地" if spec.local_only else "混合",
                    "write_label": "可写工作区" if spec.supports_workspace_write else "只读",
                    "security_label": labels["security_label"],
                    "security_detail": labels["security_detail"],
                    "avg_latency_ms": worker_metric.get("avg_duration_ms"),
                    "failure_ratio": worker_metric.get("failure_ratio"),
                    "completed_ratio": worker_metric.get("completed_ratio"),
                }
            )
        return rows

    def health(self, force_refresh: bool = False) -> list[dict[str, object]]:
        if self._health_cache and not force_refresh:
            cached_at, rows = self._health_cache
            if perf_counter() - cached_at <= self._health_cache_ttl_s:
                return [dict(row) for row in rows]

        specs = self.registry.all_enabled_specs()

        def collect(spec: WorkerSpec) -> dict[str, object]:
            health = self.registry.get(spec.worker_id).health_check()
            labels = self._worker_labels(spec)
            return {
                "worker_id": health.worker_id,
                "ok": health.ok,
                "summary": health.summary,
                "framework_label": labels["framework_label"],
                "backend_label": labels["backend_label"],
                "target_label": labels["target_label"],
            }

        with ThreadPoolExecutor(max_workers=min(8, max(1, len(specs)))) as executor:
            rows = list(executor.map(collect, specs))
        self._health_cache = (perf_counter(), rows)
        return [dict(row) for row in rows]

    def smoke_test(self) -> list[dict[str, object]]:
        prompt = (
            "请只返回一个 JSON 对象，不要输出 Markdown。"
            "字段必须包含：status, summary, deliverable, artifacts, changes_made, risks, recommended_next_steps, confidence。"
            '请使用这个结构：{"status":"completed","summary":"联调成功","deliverable":"worker reachable",'
            '"artifacts":[],"changes_made":[],"risks":[],"recommended_next_steps":[],"confidence":0.91}'
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
                    "summary": str(payload.get("summary") or "联调成功").strip(),
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

    def ping(self, mode: str = "quick") -> list[dict[str, object]]:
        ping_mode = mode.strip().lower()
        if ping_mode not in {"quick", "deep"}:
            raise ValueError("Ping mode must be 'quick' or 'deep'.")
        if ping_mode == "quick":
            cached = self._ping_cache.get("quick")
            if cached and perf_counter() - cached[0] <= self._quick_ping_cache_ttl_s:
                return [dict(row) for row in cached[1]]
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
            rows = list(executor.map(collect, specs))
        flush_audit(self.runs_dir)
        if ping_mode == "quick":
            self._ping_cache["quick"] = (perf_counter(), [dict(row) for row in rows])
        return rows

    def quick_ping(self) -> list[dict[str, object]]:
        return self.ping("quick")

    def deep_ping(self) -> list[dict[str, object]]:
        return self.ping("deep")

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
        started_total = perf_counter()
        profile = self.get_profile(profile_name)
        plan: TaskPlan | None = None
        planned_assignments: list[Assignment] = []
        outcomes: list[AssignmentOutcome] = []
        status = "unknown"
        leader_notes = ""
        synthesis_worker = ""
        synthesis_raw = ""
        leader_trace: list[LeaderTraceEntry] = []
        performance: dict[str, Any] = {
            "planning_ms": 0,
            "execution_ms": 0,
            "review_ms": 0,
            "synthesis_ms": 0,
            "total_ms": 0,
            "assignments": [],
            "worker_metrics_snapshot": self._worker_metrics(),
        }
        try:
            self._raise_if_cancelled(run_control)
            self._emit(
                event_callback,
                event_type="run_started",
                progress=0.02,
                message=f"MVP 已开始接管任务，当前模式：{profile.name}",
                profile=profile.name,
                plan_only=plan_only,
            )

            self._raise_if_cancelled(run_control)
            self._emit(
                event_callback,
                event_type="planning_started",
                progress=0.08,
                message="正在拆分任务并计算最合适的派工方案。",
            )
            planning_started = perf_counter()
            plan = self.planner.create_plan(task, profile, run_control=run_control)
            self._record_trace(leader_trace, "planning", "plan_created",
                               f"Planner {plan.planner_worker} produced {len(plan.subtasks)} subtasks: {plan.summary}",
                               worker_id=plan.planner_worker)
            worker_metrics = self._worker_metrics()
            per_kind_metrics = summarize_worker_metrics_by_kind(self.runs_dir, limit=30)
            performance["worker_metrics_snapshot"] = worker_metrics
            planned_assignments = [
                route_subtask(subtask, self.worker_specs, profile,
                              worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics)
                for subtask in plan.subtasks
            ]
            for assignment in planned_assignments:
                self._record_trace(leader_trace, "routing", "worker_assigned",
                                   f"{assignment.subtask.task_id} '{assignment.subtask.title}' -> {assignment.worker_id} "
                                   f"(score={assignment.score:.1f}, rationale: {', '.join(assignment.rationale[:4])})",
                                   worker_id=assignment.worker_id, task_id=assignment.subtask.task_id,
                                   score=assignment.score)
            performance["planning_ms"] = int((perf_counter() - planning_started) * 1000)
            self._emit(
                event_callback,
                event_type="planning_completed",
                progress=1.0 if plan_only else 0.18,
                message=f"已生成 {len(planned_assignments)} 个子任务，准备开始派工。",
                planner_worker=plan.planner_worker,
                plan_summary=plan.summary,
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

            if not plan_only:
                progress = ProgressTracker(len(planned_assignments) * 3)
                assignment_order = {
                    assignment.subtask.task_id: index
                    for index, assignment in enumerate(planned_assignments, start=1)
                }
                batches = self._build_execution_batches(planned_assignments, profile)
                completed_outcomes: dict[str, AssignmentOutcome] = {}
                self._record_trace(leader_trace, "execution", "batches_formed",
                                   f"{len(batches)} batches for {len(planned_assignments)} assignments")

                for batch_index, batch in enumerate(batches, start=1):
                    self._raise_if_cancelled(run_control)
                    if len(batch) > 1:
                        self._emit(
                            event_callback,
                            event_type="batch_started",
                            progress=self._progress_from_tracker(progress),
                            message=f"MVP 正在并行推进第 {batch_index} 批，共 {len(batch)} 个文件级子任务。",
                            batch_index=batch_index,
                            batch_size=len(batch),
                            task_ids=[assignment.subtask.task_id for assignment in batch],
                        )

                    batch_outcomes = self._execute_assignment_batch(
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
                    for outcome, assignment_performance in batch_outcomes:
                        outcomes.append(outcome)
                        completed_outcomes[outcome.assignment.subtask.task_id] = outcome
                        performance["execution_ms"] += int(assignment_performance.get("execution_ms", 0))
                        performance["review_ms"] += int(assignment_performance.get("review_ms", 0))
                        performance["assignments"].append(assignment_performance)

                outcomes.sort(key=lambda item: assignment_order.get(item.assignment.subtask.task_id, 999))
                performance["assignments"].sort(key=lambda item: assignment_order.get(str(item.get("task_id", "")), 999))

                if outcomes:
                    synthesis_started = perf_counter()
                    synthesis_worker, synthesis_raw = self._synthesize(task, profile, outcomes)
                    performance["synthesis_ms"] = int((perf_counter() - synthesis_started) * 1000)
                    self._record_trace(leader_trace, "consolidation", "synthesis_done",
                                       f"Synthesized by {synthesis_worker}", worker_id=synthesis_worker)
                else:
                    synthesis_worker = ""
                    synthesis_raw = ""

            status, leader_notes = self._summarize_run(plan_only=plan_only, outcomes=outcomes)
            if synthesis_raw:
                leader_notes = synthesis_raw
        except RunCancelled as exc:
            if plan is None:
                raise
            status = "cancelled"
            leader_notes = str(exc).strip() or "任务已被用户中断。"
            synthesis_worker = ""
            synthesis_raw = ""
            self._emit(
                event_callback,
                event_type="run_cancelled",
                progress=self._progress_from_counts(len(outcomes) * 2, max(len(planned_assignments) * 3, 1)),
                message=leader_notes,
            )

        if plan is None:
            raise RuntimeError("MVP 未能生成任务规划。")
        report = RunReport(
            task=task,
            mode=profile_name,
            profile=profile.name,
            status=status,
            leader_notes=leader_notes,
            plan=plan,
            planned_assignments=planned_assignments,
            outcomes=outcomes,
            performance=performance,
            leader_trace=leader_trace,
            synthesis_worker=synthesis_worker if synthesis_worker else "",
            synthesis_raw=synthesis_raw if synthesis_raw else "",
        )
        performance["total_ms"] = int((perf_counter() - started_total) * 1000)
        report.report_path = str(self._persist_report(report))

        # Persist to long-term memory for learning
        if self.memory:
            try:
                self.memory.remember_task(
                    task=task, profile=profile.name,
                    plan_json=plan.to_dict(),
                    outcomes_json=[o.to_dict() for o in outcomes],
                    status=status,
                    tags=self._extract_task_tags(task),
                )
                subtask_kinds = [s.kind for s in plan.subtasks]
                worker_ids = [o.assignment.worker_id for o in outcomes] if outcomes else []
                self.memory.learn_pattern(
                    task=task, subtask_kinds=subtask_kinds,
                    worker_assignments=worker_ids, success=(status == "completed"),
                )
            except Exception:
                pass

        self._emit(
            event_callback,
            event_type="run_completed",
            progress=1.0,
            message=f"任务完成，最终状态：{status}",
            status=status,
            leader_notes=leader_notes,
            report_path=report.report_path,
        )
        return report

    def _execute_assignment_cycle(
        self,
        assignment: Assignment,
        profile: RunProfile,
        event_callback: RunEventCallback | None,
        run_control: RunControl | None,
        progress: ProgressTracker,
        task_index: int,
        total_tasks: int,
        worker_metrics: dict[str, dict[str, float]] | None = None,
        per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
        upstream_results: dict[str, AssignmentOutcome] | None = None,
    ) -> tuple[AssignmentOutcome, dict[str, Any]]:
        attempted_workers: set[str] = set()
        reroute_budget = 2
        current_assignment = assignment
        previous_attempt: dict[str, object] | None = None
        perf_row: dict[str, Any] = {
            "task_id": assignment.subtask.task_id,
            "title": assignment.subtask.title,
            "execution_ms": 0,
            "review_ms": 0,
            "reroutes": 0,
            "worker_id": assignment.worker_id,
        }
        while True:
            subtask = current_assignment.subtask
            self._emit(
                event_callback,
                event_type="assignment_started",
                progress=self._progress_from_tracker(progress),
                message=f"正在执行子任务 {task_index}/{total_tasks}：{subtask.title}",
                worker_id=current_assignment.worker_id,
                task_id=subtask.task_id,
                kind=subtask.kind,
            )
            execution_started = perf_counter()
            current_assignment, result_payload = self._run_assignment_with_fallback(
                current_assignment,
                profile,
                event_callback=event_callback,
                run_control=run_control,
                exclude=attempted_workers,
                worker_metrics=worker_metrics,
                per_kind_metrics=per_kind_metrics,
                upstream_results=upstream_results,
                previous_attempt=previous_attempt,
            )
            perf_row["execution_ms"] += int((perf_counter() - execution_started) * 1000)
            perf_row["worker_id"] = current_assignment.worker_id
            attempted_workers.add(current_assignment.worker_id)
            result = WorkerResult(
                worker_id=current_assignment.worker_id,
                status=str(result_payload.get("status") or "partial").strip().lower(),
                summary=str(result_payload.get("summary") or "").strip(),
                deliverable=str(result_payload.get("deliverable") or "").strip(),
                artifacts=normalize_str_list(result_payload.get("artifacts")),
                changes_made=normalize_str_list(result_payload.get("changes_made")),
                risks=normalize_str_list(result_payload.get("risks")),
                recommended_next_steps=normalize_str_list(result_payload.get("recommended_next_steps")),
                confidence=clamp_float(result_payload.get("confidence"), 0.0, 1.0, 0.5),
                raw_response=str(result_payload.get("_raw_response", "")),
            )
            progress_value = self._progress_from_counts(*progress.advance())
            self._emit(
                event_callback,
                event_type="assignment_completed",
                progress=progress_value,
                message=f"{subtask.title} 已完成执行，正在准备验收。",
                worker_id=current_assignment.worker_id,
                task_id=subtask.task_id,
                result_status=result.status,
                result_summary=result.summary or result.deliverable,
            )
            if result.status in self.FAILED_RESULT_STATUSES and reroute_budget > 0:
                fallback = self._select_followup_assignment(
                    subtask, profile, attempted_workers,
                    worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics,
                )
                if fallback is not None:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current_assignment = self._annotate_reroute(fallback, f"execution status {result.status}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": [f"Execution failed with status: {result.status}"],
                    }
                    self._emit(
                        event_callback,
                        event_type="assignment_rerouted",
                        progress=progress_value,
                        message=f"{subtask.title} 执行结果为 {result.status}，已改派给 {current_assignment.worker_id}。",
                        task_id=subtask.task_id,
                        previous_worker=result.worker_id,
                        next_worker=current_assignment.worker_id,
                        reason=result.status,
                    )
                    continue

            self._emit(
                event_callback,
                event_type="review_started",
                progress=self._progress_from_tracker(progress),
                message=f"正在验收子任务：{subtask.title}",
                reviewer_worker=profile.reviewer_worker,
                task_id=subtask.task_id,
            )
            review_started = perf_counter()
            review = self.reviewer.review(subtask, result, profile, run_control=run_control)
            perf_row["review_ms"] += int((perf_counter() - review_started) * 1000)
            progress_value = self._progress_from_counts(*progress.advance())
            self._emit(
                event_callback,
                event_type="review_completed",
                progress=progress_value,
                message=f"{subtask.title} 验收结论：{review.decision}",
                reviewer_worker=review.reviewer_worker,
                task_id=subtask.task_id,
                decision=review.decision,
                summary=review.summary,
            )
            if review.decision == "fail" and reroute_budget > 0:
                fallback = self._select_followup_assignment(
                    subtask, profile, attempted_workers,
                    worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics,
                )
                if fallback is not None:
                    reroute_budget -= 1
                    perf_row["reroutes"] += 1
                    current_assignment = self._annotate_reroute(fallback, f"review fail by {review.reviewer_worker}")
                    previous_attempt = {
                        "worker_id": result.worker_id,
                        "result_summary": result.summary or result.deliverable,
                        "review_findings": review.findings,
                    }
                    self._emit(
                        event_callback,
                        event_type="assignment_rerouted",
                        progress=progress_value,
                        message=f"{subtask.title} 未通过验收，已改派给 {current_assignment.worker_id} 重新执行。",
                        task_id=subtask.task_id,
                        previous_worker=result.worker_id,
                        next_worker=current_assignment.worker_id,
                        reason="review_fail",
                    )
                    continue

            if review.decision == "fail" and reroute_budget <= 0:
                replan_outcomes = self._dynamic_replan(
                    subtask, profile, attempted_workers, result, review, worker_metrics, per_kind_metrics,
                )
                if replan_outcomes:
                    return replan_outcomes, perf_row

            return AssignmentOutcome(assignment=current_assignment, result=result, review=review), perf_row

    def _execute_assignment_batch(
        self,
        batch: list[Assignment],
        profile: RunProfile,
        event_callback: RunEventCallback | None,
        run_control: RunControl | None,
        progress: ProgressTracker,
        total_tasks: int,
        assignment_order: dict[str, int],
        worker_metrics: dict[str, dict[str, float]] | None = None,
        per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
        completed_outcomes: dict[str, AssignmentOutcome] | None = None,
    ) -> list[tuple[AssignmentOutcome, dict[str, Any]]]:
        upstream = completed_outcomes or {}

        def _run_one(assignment: Assignment) -> tuple[AssignmentOutcome, dict[str, Any]]:
            return self._execute_assignment_cycle(
                assignment=assignment,
                profile=profile,
                event_callback=event_callback,
                run_control=run_control,
                progress=progress,
                task_index=assignment_order.get(assignment.subtask.task_id, 1),
                total_tasks=total_tasks,
                worker_metrics=worker_metrics,
                per_kind_metrics=per_kind_metrics,
                upstream_results=upstream,
            )

        if len(batch) <= 1:
            return [_run_one(batch[0])]

        outcomes: list[tuple[AssignmentOutcome, dict[str, Any]]] = []
        max_workers = min(self._parallel_batch_limit(profile), len(batch))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(_run_one, assignment): assignment for assignment in batch}
            for future in as_completed(future_map):
                outcomes.append(future.result())

        outcomes.sort(key=lambda item: assignment_order.get(item[0].assignment.subtask.task_id, 999))
        return outcomes

    def _run_assignment_with_fallback(
        self,
        assignment: Assignment,
        profile: RunProfile,
        *,
        event_callback: RunEventCallback | None = None,
        run_control: RunControl | None = None,
        exclude: set[str] | None = None,
        worker_metrics: dict[str, dict[str, float]] | None = None,
        per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
        upstream_results: dict[str, AssignmentOutcome] | None = None,
        previous_attempt: dict[str, object] | None = None,
    ) -> tuple[Assignment, dict[str, Any]]:
        prompt = self._build_execution_prompt(assignment, upstream_results=upstream_results, previous_attempt=previous_attempt)
        ranked = rank_workers(
            assignment.subtask, self.worker_specs, profile, exclude=exclude,
            worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics,
        )
        last_error = ""
        for position, candidate in enumerate(ranked):
            self._raise_if_cancelled(run_control)
            worker = self.registry.get(candidate.worker_id)
            try:
                payload = worker.invoke_json(
                    prompt=prompt,
                    allow_write=candidate.subtask.needs_write_access,
                    run_control=run_control,
                )
                if candidate.worker_id != assignment.worker_id:
                    candidate = self._annotate_reroute(candidate, f"fallback after worker failure: {assignment.worker_id}")
                return candidate, payload
            except Exception as exc:
                last_error = f"{candidate.worker_id}: {exc}"
                next_worker = ranked[position + 1].worker_id if position + 1 < len(ranked) else ""
                if next_worker:
                    self._emit(
                        event_callback,
                        event_type="assignment_rerouted",
                        message=f"{candidate.worker_id} 执行失败，改派给 {next_worker}。",
                        task_id=assignment.subtask.task_id,
                        previous_worker=candidate.worker_id,
                        next_worker=next_worker,
                        reason=str(exc),
                    )

        raise RuntimeError(f"All execution fallbacks failed. Last error: {last_error}")

    def _build_execution_prompt(
        self, assignment,
        upstream_results: dict[str, AssignmentOutcome] | None = None,
        previous_attempt: dict[str, object] | None = None,
    ):
        from .prompting import build_execution_prompt

        upstream_dict: dict[str, dict[str, object]] | None = None
        if upstream_results:
            upstream_dict = {}
            for dep_id in assignment.subtask.depends_on:
                dep_outcome = upstream_results.get(dep_id)
                if dep_outcome:
                    upstream_dict[dep_id] = {
                        "status": dep_outcome.result.status,
                        "summary": dep_outcome.result.summary,
                        "deliverable": dep_outcome.result.deliverable,
                        "changes_made": dep_outcome.result.changes_made,
                    }

        return build_execution_prompt(
            assignment, mode="delegated",
            upstream_results=upstream_dict,
            previous_attempt=previous_attempt,
        )

    def _summarize_run(self, plan_only: bool, outcomes: list[AssignmentOutcome]) -> tuple[str, str]:
        if plan_only:
            return "planned", "MVP 已生成派工规划，当前没有执行子任务。"
        if not outcomes:
            return "empty", "本次任务没有产出可用结果。"

        review_decisions = [item.review.decision for item in outcomes if item.review is not None]
        if any(decision == "fail" for decision in review_decisions):
            return "needs_revision", "至少有一个子任务没有通过验收。"
        if any(decision == "revise" for decision in review_decisions):
            return "partial", "至少有一个子任务需要修订或加做一轮。"
        if any(item.result.status != "completed" for item in outcomes):
            return "partial", "至少有一个执行结果不是完成态。"
        return "completed", "所有子任务均已通过当前验收门。"

    def _persist_report(self, report: RunReport) -> Path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.runs_dir / f"mvp_run_{timestamp}.json"
        save_json_file(path, report.to_dict())
        return path

    def _worker_labels(self, spec: WorkerSpec) -> dict[str, str]:
        return describe_worker_platform(spec)

    def _build_execution_batches(self, assignments: list[Assignment], profile: RunProfile) -> list[list[Assignment]]:
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

    def _can_parallelize_assignment(self, candidate: Assignment, batch: list[Assignment], profile: RunProfile) -> bool:
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

                if candidate.worker_id == existing.worker_id and not candidate.subtask.needs_write_access:
                    return False

        return True

    def _parallel_batch_limit(self, profile: RunProfile) -> int:
        if profile.name == "premium":
            return 3
        if profile.name == "balanced":
            return 2
        return 1

    def _parallel_write_limit(self, profile: RunProfile) -> int:
        return 2 if profile.name == "premium" else 1

    def _emit(self, callback: RunEventCallback | None, event_type: str, **payload: Any) -> None:
        if callback is None:
            return
        callback({"type": event_type, **payload})

    def _progress_from_tracker(self, progress: ProgressTracker) -> float:
        return self._progress_from_counts(*progress.snapshot())

    def _progress_from_counts(self, completed_units: int, total_units: int) -> float:
        if total_units <= 0:
            return 0.9
        return 0.18 + (completed_units / total_units) * 0.72

    def _raise_if_cancelled(self, run_control: RunControl | None) -> None:
        if run_control is not None:
            run_control.raise_if_cancelled()

    def _select_followup_assignment(
        self,
        subtask,
        profile: RunProfile,
        attempted_workers: set[str],
        worker_metrics: dict[str, dict[str, float]] | None = None,
        per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
    ) -> Assignment | None:
        ranked = rank_workers(
            subtask, self.worker_specs, profile, exclude=attempted_workers,
            worker_metrics=worker_metrics, per_kind_metrics=per_kind_metrics,
        )
        return ranked[0] if ranked else None

    def _annotate_reroute(self, assignment: Assignment, reason: str) -> Assignment:
        return Assignment(
            subtask=assignment.subtask,
            worker_id=assignment.worker_id,
            score=assignment.score,
            rationale=[*assignment.rationale, reason],
        )

    def _worker_metrics(self) -> dict[str, dict[str, float]]:
        metrics = summarize_worker_metrics(self.runs_dir, limit=80)
        # Merge memory-based metrics
        if self.memory:
            mem_stats = self.memory.all_worker_stats()
            for wid, stats in mem_stats.items():
                if wid not in metrics:
                    metrics[wid] = {}
                metrics[wid].setdefault("avg_duration_ms", stats.get("avg_duration_ms", 0))
                mem_failure = 1.0 - stats.get("success_rate", 1.0)
                current = metrics[wid].get("failure_ratio", 0)
                metrics[wid]["failure_ratio"] = max(current, mem_failure)
        return metrics

    @staticmethod
    def _extract_task_tags(task: str) -> list[str]:
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

    def _synthesize(
        self, task: str, profile: RunProfile, outcomes: list[AssignmentOutcome],
    ) -> tuple[str, str]:
        from .prompting import build_synthesis_prompt

        outcome_dicts: list[dict[str, object]] = []
        for oc in outcomes:
            outcome_dicts.append({
                "title": oc.assignment.subtask.title,
                "worker_id": oc.assignment.worker_id,
                "status": oc.result.status,
                "summary": oc.result.summary,
                "deliverable": oc.result.deliverable,
                "review_decision": oc.review.decision if oc.review else "none",
            })
        prompt = build_synthesis_prompt(task, profile.name, outcome_dicts)
        candidates = ["ollama_planner", "ollama_reviewer", "hermes_designer"]
        for candidate in candidates:
            if candidate not in self.worker_specs or not self.worker_specs[candidate].enabled:
                continue
            try:
                worker = self.registry.get(candidate)
                payload = worker.invoke_json(prompt, allow_write=False)
                synthesis = str(payload.get("synthesis") or "").strip()
                if synthesis:
                    key_findings = normalize_str_list(payload.get("key_findings"))
                    gaps = normalize_str_list(payload.get("gaps"))
                    parts = [synthesis]
                    if key_findings:
                        parts.append("\n关键发现:\n" + "\n".join(f"- {f}" for f in key_findings))
                    if gaps:
                        parts.append("\n待补充:\n" + "\n".join(f"- {g}" for g in gaps))
                    return candidate, "\n\n".join(parts)
            except Exception:
                continue
        return "", ""

    def _dynamic_replan(
        self,
        failed_subtask: Subtask,
        profile: RunProfile,
        attempted_workers: set[str],
        last_result: WorkerResult,
        last_review,
        worker_metrics: dict[str, dict[str, float]] | None,
        per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None,
    ) -> AssignmentOutcome | None:
        from .prompting import build_replan_prompt

        roster = {wid: spec.capabilities for wid, spec in self.worker_specs.items() if spec.enabled}
        failure_reason = f"Review failed by {last_review.reviewer_worker}: {'; '.join(last_review.findings)}"
        previous_results: list[dict[str, object]] = [{
            "worker_id": last_result.worker_id,
            "status": last_result.status,
            "summary": last_result.summary,
            "deliverable": last_result.deliverable,
        }]
        prompt = build_replan_prompt(
            failed_subtask, failure_reason, last_review.findings, previous_results, roster,
        )
        candidates = ["codex_architect", "ollama_planner", "hermes_designer"]
        payload = None
        for candidate in candidates:
            if candidate not in self.worker_specs or not self.worker_specs[candidate].enabled:
                continue
            try:
                worker = self.registry.get(candidate)
                payload = worker.invoke_json(prompt, allow_write=False)
                break
            except Exception:
                continue
        if payload is None:
            return None

        raw_subtasks = payload.get("subtasks", [])
        if not isinstance(raw_subtasks, list) or len(raw_subtasks) < 1:
            return None

        new_subtasks: list[Subtask] = []
        base_depends = list(failed_subtask.depends_on)
        for i, raw in enumerate(raw_subtasks, start=1):
            if not isinstance(raw, dict):
                continue
            new_subtasks.append(Subtask(
                task_id=f"{failed_subtask.task_id}_R{i}",
                title=str(raw.get("title") or f"Replan {i}"),
                goal=str(raw.get("goal") or failed_subtask.goal).strip(),
                kind=str(raw.get("kind") or failed_subtask.kind).strip().lower(),
                difficulty=min(clamp_int(raw.get("difficulty"), 1, 5, 2), failed_subtask.difficulty),
                repeatability=clamp_int(raw.get("repeatability"), 1, 5, 2),
                risk=min(clamp_int(raw.get("risk"), 1, 5, 2), failed_subtask.risk),
                needs_write_access=bool(raw.get("needs_write_access", False)),
                preferred_capabilities=normalize_str_list(raw.get("preferred_capabilities")),
                acceptance_criteria=normalize_str_list(raw.get("acceptance_criteria")),
                notes=f"Replanned from failed subtask {failed_subtask.task_id}. {str(raw.get('notes', '')).strip()}",
                depends_on=base_depends,
                target_files=normalize_str_list(raw.get("target_files")) or failed_subtask.target_files,
                validation_steps=normalize_str_list(raw.get("validation_steps")),
            ))

        if not new_subtasks:
            return None

        new_assignments: list[Assignment] = []
        for subtask in new_subtasks:
            ranked = rank_workers(
                subtask, self.worker_specs, profile,
                exclude=attempted_workers,
                worker_metrics=worker_metrics,
                per_kind_metrics=per_kind_metrics,
            )
            if ranked:
                new_assignments.append(ranked[0])

        if not new_assignments:
            return None

        replan_note = (
            f"Original subtask '{failed_subtask.title}' decomposed into {len(new_assignments)} smaller tasks "
            f"after repeated failures. Workers attempted: {', '.join(sorted(attempted_workers))}."
        )
        merged = AssignmentOutcome(
            assignment=Assignment(
                subtask=failed_subtask,
                worker_id="mvp_dynamic_replan",
                score=0.0,
                rationale=[replan_note],
            ),
            result=WorkerResult(
                worker_id="mvp_dynamic_replan",
                status="replanned",
                summary=replan_note,
                deliverable=f"Decomposed into: {', '.join(a.subtask.task_id for a in new_assignments)}",
                artifacts=[],
                changes_made=[],
                risks=[],
                recommended_next_steps=[],
                confidence=0.5,
                raw_response="",
            ),
            review=None,
        )
        return merged

    @staticmethod
    def _record_trace(
        trace: list[LeaderTraceEntry], phase: str, decision: str, detail: str,
        worker_id: str = "", task_id: str = "", score: float = 0.0,
    ) -> None:
        trace.append(LeaderTraceEntry(
            timestamp=datetime.now().isoformat(),
            phase=phase,
            decision=decision,
            detail=detail,
            worker_id=worker_id,
            task_id=task_id,
            score=score,
        ))
