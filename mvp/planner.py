from __future__ import annotations

from dataclasses import replace

from .code_intel import CodeIntel
from .models import RunProfile, Subtask, TaskPlan, WorkerSpec
from .skill_mesh import SkillMesh
from .worker_platform import worker_mode_family
from .prompting import build_planner_prompt
from .repo_index import RepositoryIndex
from .utils import RunControl, clamp_int, normalize_str_list
from .workers.registry import WorkerRegistry


CODE_INTENT_KEYWORDS = (
    "code",
    "coding",
    "source",
    "file",
    "function",
    "class",
    "module",
    "refactor",
    "fix",
    "bug",
    "feature",
    "patch",
    "test",
    "implement",
    "repo",
    "repository",
    "workspace",
    "rewrite",
    "optimize",
    "migrate",
    "configure",
    "setup",
    "init",
    "scaffold",
    "lint",
    "format",
    "clean",
    "代码",
    "编码",
    "源码",
    "文件",
    "函数",
    "类",
    "模块",
    "重构",
    "修复",
    "功能",
    "补丁",
    "测试",
    "实现",
    "仓库",
    "项目",
    "改写",
    "优化",
    "迁移",
    "配置",
    "初始化",
    "搭建",
    "清理",
    "格式化",
    "升级",
    "降级",
    "调整",
)

WRITE_INTENT_KEYWORDS = (
    "implement",
    "fix",
    "create",
    "edit",
    "refactor",
    "update",
    "patch",
    "build",
    "write code",
    "rewrite",
    "optimize",
    "migrate",
    "configure",
    "setup",
    "init",
    "scaffold",
    "修改",
    "修复",
    "创建",
    "新增",
    "删除",
    "重构",
    "实现",
    "更新",
    "改代码",
    "写代码",
    "改写",
    "优化代码",
    "迁移代码",
    "配置项",
    "初始化项目",
    "搭建",
)

READONLY_NEGATIONS = (
    "do not modify",
    "don't modify",
    "no file changes",
    "read-only",
    "不要修改",
    "不需要修改",
    "无需修改",
    "只读",
)


class Planner:
    def __init__(
        self,
        registry: WorkerRegistry,
        worker_specs: dict[str, WorkerSpec],
        skill_mesh: SkillMesh | None = None,
    ) -> None:
        self.registry = registry
        self.worker_specs = worker_specs
        self.skill_mesh = skill_mesh
        self.repo_index = RepositoryIndex(registry.workspace_root)
        self.code_intel = CodeIntel(registry.workspace_root)

    def create_plan(self, task: str, profile: RunProfile, run_control: RunControl | None = None) -> TaskPlan:
        fast_path = self._fast_path_plan(task, profile)
        if fast_path is not None:
            return fast_path

        roster = {worker_id: spec.capabilities for worker_id, spec in self.worker_specs.items() if spec.enabled}
        code_task = self._is_code_heavy_task(task)
        repo_hints = self.repo_index.infer_files(task, limit=8)
        prompt = build_planner_prompt(
            task=task,
            mode=profile.name,
            roster=roster,
            max_subtasks_hint=profile.max_subtasks_hint,
            code_task=code_task,
            repo_hints=repo_hints,
        )

        payload = None
        planner_used = profile.planner_worker
        last_error = ""
        for candidate in self._planner_candidates(task, profile, code_task):
            if candidate not in self.worker_specs or not self.worker_specs[candidate].enabled:
                continue
            planner_worker = self.registry.get(candidate)
            candidate_prompt = self._planner_prompt_for_candidate(
                candidate,
                base_prompt=prompt,
                task=task,
                profile=profile,
                code_task=code_task,
                repo_hints=repo_hints,
            )
            for mode in self._planner_modes(candidate, profile, code_task):
                try:
                    payload = self._invoke_json_mode(
                        planner_worker,
                        candidate_prompt,
                        allow_write=False,
                        run_control=run_control,
                        mode=mode,
                    )
                    payload = self._normalize_planner_output(payload, task)
                    self._validate_planner_output(payload)
                    planner_used = candidate
                    break
                except Exception as exc:
                    last_error = f"{candidate}[{mode}]: {exc}"
                    if self._can_use_local_recovery(candidate, profile, mode):
                        payload = self._deterministic_local_payload(task, code_task, repo_hints)
                        planner_used = candidate
                        break
            if payload is not None:
                break

        if payload is None:
            raise RuntimeError(f"All planner fallbacks failed. Last error: {last_error}")

        subtasks = self._parse_subtasks(payload.get("subtasks", []), task)
        if not subtasks:
            subtasks = [self._fallback_subtask(task)]

        plan = TaskPlan(
            summary=str(payload.get("summary") or task).strip(),
            execution_strategy=str(payload.get("execution_strategy") or "Route each subtask to the best matched worker.").strip(),
            subtasks=subtasks,
            planner_worker=planner_used,
            raw_response=str(payload.get("_raw_response", "")),
        )
        plan = self._upgrade_code_plan(task, plan, profile)
        return self._augment_plan_with_skills(task, plan)

    @staticmethod
    def _validate_planner_output(payload: dict[str, object]) -> None:
        if not isinstance(payload.get("subtasks"), list):
            raise ValueError("Planner output missing 'subtasks' array")
        if not payload.get("summary"):
            raise ValueError("Planner output missing 'summary'")
        for i, st in enumerate(payload["subtasks"]):
            if not isinstance(st, dict):
                raise ValueError(f"Subtask {i} is not a dict")
            if not st.get("task_id") or not st.get("title"):
                raise ValueError(f"Subtask {i} missing task_id or title")
            kind = str(st.get("kind", "")).strip().lower()
            if kind not in {
                "architecture", "design", "coding", "testing", "review",
                "research", "documentation", "ops", "coordination",
                "planning", "requirements", "qa", "summarization", "triage",
                "security", "performance",
            }:
                raise ValueError(f"Subtask {i} has unknown kind: {kind}")

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

    def _planner_modes(self, worker_id: str, profile: RunProfile, code_task: bool) -> list[str]:
        spec = self.worker_specs.get(worker_id)
        if spec is None:
            return ["default"]
        mode_family = worker_mode_family(spec)
        if mode_family in {"agent_cli", "extension_bridge", "service_mesh"}:
            if profile.name == "balanced":
                return ["quick"]
            return ["quick", "deep"] if profile.name == "premium" else ["quick"]
        if mode_family == "local_llm":
            return ["quick", "deep"] if code_task or profile.name != "cheap" else ["quick"]
        return ["default"]

    def _normalize_planner_output(self, payload: dict[str, object], task: str) -> dict[str, object]:
        normalized: dict[str, object] = {
            "summary": str(payload.get("summary") or task).strip(),
            "execution_strategy": str(
                payload.get("execution_strategy") or "Route each subtask to the best matched worker."
            ).strip(),
            "subtasks": [],
        }
        raw_subtasks = payload.get("subtasks")
        if not isinstance(raw_subtasks, list):
            return normalized

        subtasks: list[dict[str, object]] = []
        for index, raw in enumerate(raw_subtasks, start=1):
            if not isinstance(raw, dict):
                continue
            kind = self._normalize_planner_kind(raw.get("kind"))
            title = str(raw.get("title") or raw.get("name") or f"Subtask {index}").strip()
            goal = str(raw.get("goal") or raw.get("objective") or title or task).strip()
            task_id = str(raw.get("task_id") or raw.get("id") or f"S{index}").strip() or f"S{index}"
            preferred = normalize_str_list(raw.get("preferred_capabilities")) or self._default_capabilities_for_kind(kind)
            acceptance = (
                normalize_str_list(raw.get("acceptance_criteria"))
                or normalize_str_list(raw.get("success_criteria"))
                or ["Return a useful, verifiable outcome."]
            )
            subtasks.append(
                {
                    "task_id": task_id,
                    "title": title,
                    "goal": goal,
                    "kind": kind,
                    "difficulty": clamp_int(raw.get("difficulty"), 1, 5, 2),
                    "repeatability": clamp_int(raw.get("repeatability"), 1, 5, 2),
                    "risk": clamp_int(raw.get("risk"), 1, 5, 2),
                    "needs_write_access": bool(raw.get("needs_write_access", False)),
                    "preferred_capabilities": preferred,
                    "acceptance_criteria": acceptance,
                    "depends_on": normalize_str_list(raw.get("depends_on")),
                    "target_files": normalize_str_list(raw.get("target_files")),
                    "validation_steps": normalize_str_list(raw.get("validation_steps")),
                    "parallel_group": str(raw.get("parallel_group") or "").strip(),
                    "required_skills": normalize_str_list(raw.get("required_skills")),
                    "coordination_notes": normalize_str_list(raw.get("coordination_notes")),
                    "notes": str(raw.get("notes") or "").strip(),
                }
            )
        normalized["subtasks"] = subtasks
        return normalized

    def _normalize_planner_kind(self, value: object) -> str:
        kind = str(value or "coordination").strip().lower()
        known = {
            "architecture", "design", "coding", "testing", "review",
            "research", "documentation", "ops", "coordination",
            "planning", "requirements", "qa", "summarization", "triage",
            "security", "performance",
        }
        if kind in known:
            return kind
        if any(token in kind for token in ("implement", "build", "develop", "refactor", "patch")):
            return "coding"
        if any(token in kind for token in ("test", "verify", "validation", "regression", "check")):
            return "testing"
        if any(token in kind for token in ("arch", "impact", "scan")):
            return "architecture"
        if any(token in kind for token in ("design", "spec", "requirement")):
            return "design"
        if any(token in kind for token in ("review", "audit")):
            return "review"
        if any(token in kind for token in ("perf", "latency", "optimiz")):
            return "performance"
        if any(token in kind for token in ("research", "analysis", "analyze")):
            return "research"
        if any(token in kind for token in ("doc", "writeup", "summary")):
            return "documentation"
        return "coordination"

    def _default_capabilities_for_kind(self, kind: str) -> list[str]:
        if kind == "architecture":
            return ["architecture", "planning", "design", "review"]
        if kind == "design":
            return ["design", "planning", "requirements"]
        if kind == "coding":
            return ["coding", "testing", "review"]
        if kind in {"testing", "qa"}:
            return ["testing", "qa", "review"]
        if kind == "review":
            return ["review", "qa", "architecture"]
        if kind == "security":
            return ["security", "review", "architecture"]
        if kind == "performance":
            return ["performance", "testing", "review"]
        if kind == "documentation":
            return ["documentation", "summarization"]
        if kind == "research":
            return ["research", "summarization"]
        return ["planning"]

    def _planner_prompt_for_candidate(
        self,
        worker_id: str,
        *,
        base_prompt: str,
        task: str,
        profile: RunProfile,
        code_task: bool,
        repo_hints: list[str],
    ) -> str:
        spec = self.worker_specs.get(worker_id)
        if spec is None or worker_mode_family(spec) != "local_llm":
            return base_prompt
        return self._compact_planner_prompt(task, profile, code_task, repo_hints)

    def _compact_planner_prompt(
        self,
        task: str,
        profile: RunProfile,
        code_task: bool,
        repo_hints: list[str],
    ) -> str:
        max_subtasks = min(profile.max_subtasks_hint, 3)
        kinds = "architecture, design, coding, testing, review, research, documentation, coordination, planning, requirements, qa, summarization, triage, security, performance"
        hint_text = ", ".join(repo_hints[:4]) if repo_hints else "none"
        code_rules = (
            "- For code tasks, prefer architecture -> coding -> testing.\n"
            "- Keep target_files short and realistic.\n"
            "- Keep every string concise.\n"
        ) if code_task else ""
        return f"""
Return JSON only. Keep it compact.

Rules:
- Create 1 to {max_subtasks} subtasks.
- Use kinds only from: {kinds}.
- Keep summary, titles, goals, notes, and criteria short.
- Each subtask must include: task_id, title, goal, kind, difficulty, repeatability, risk, needs_write_access, preferred_capabilities, acceptance_criteria, depends_on, target_files, validation_steps, parallel_group, notes.
{code_rules}
Likely files: {hint_text}

JSON shape:
{{
  "summary": "short summary",
  "execution_strategy": "short strategy",
  "subtasks": [
    {{
      "task_id": "S1",
      "title": "short title",
      "goal": "short goal",
      "kind": "planning",
      "difficulty": 2,
      "repeatability": 2,
      "risk": 2,
      "needs_write_access": false,
      "preferred_capabilities": ["planning"],
      "acceptance_criteria": ["one clear check"],
      "depends_on": [],
      "target_files": [],
      "validation_steps": [],
      "parallel_group": "",
      "notes": ""
    }}
  ]
}}

        Task:
{task}
""".strip()

    def _can_use_local_recovery(self, worker_id: str, profile: RunProfile, mode: str) -> bool:
        spec = self.worker_specs.get(worker_id)
        if spec is None or worker_mode_family(spec) != "local_llm":
            return False
        if profile.name == "premium":
            return False
        return mode == "deep"

    def _deterministic_local_payload(self, task: str, code_task: bool, repo_hints: list[str]) -> dict[str, object]:
        if code_task or self._has_write_intent(task):
            targets = repo_hints[:4]
            return {
                "summary": task.strip(),
                "execution_strategy": "Local fallback: architecture -> coding -> testing.",
                "subtasks": [
                    {
                        "task_id": "S1",
                        "title": "Map affected code paths",
                        "goal": f"Identify the main files, symbols, and risks for: {task.strip()}",
                        "kind": "architecture",
                        "difficulty": 2,
                        "repeatability": 2,
                        "risk": 2,
                        "needs_write_access": False,
                        "preferred_capabilities": ["architecture", "planning", "review"],
                        "acceptance_criteria": ["List the main files and risks."],
                        "depends_on": [],
                        "target_files": targets,
                        "validation_steps": [],
                        "parallel_group": "",
                        "notes": "Deterministic local planner fallback.",
                    },
                    {
                        "task_id": "S2",
                        "title": "Implement the requested refactor",
                        "goal": task.strip(),
                        "kind": "coding",
                        "difficulty": 3,
                        "repeatability": 2,
                        "risk": 3,
                        "needs_write_access": True,
                        "preferred_capabilities": ["coding", "testing", "review"],
                        "acceptance_criteria": ["Apply the requested change in the relevant files."],
                        "depends_on": ["S1"],
                        "target_files": targets,
                        "validation_steps": [],
                        "parallel_group": "",
                        "notes": "Deterministic local planner fallback.",
                    },
                    {
                        "task_id": "S3",
                        "title": "Verify regression coverage",
                        "goal": f"Verify the result and regression surface for: {task.strip()}",
                        "kind": "testing",
                        "difficulty": 2,
                        "repeatability": 3,
                        "risk": 2,
                        "needs_write_access": False,
                        "preferred_capabilities": ["testing", "qa", "review"],
                        "acceptance_criteria": ["State whether the change is verified and what remains unverified."],
                        "depends_on": ["S2"],
                        "target_files": targets,
                        "validation_steps": ["Review or run targeted regression checks for the changed flow."],
                        "parallel_group": "",
                        "notes": "Deterministic local planner fallback.",
                    },
                ],
            }
        return {
            "summary": task.strip(),
            "execution_strategy": "Local fallback: single concise read-only response.",
            "subtasks": [
                {
                    "task_id": "S1",
                    "title": "Produce the requested response",
                    "goal": task.strip(),
                    "kind": "coordination",
                    "difficulty": 1,
                    "repeatability": 3,
                    "risk": 1,
                    "needs_write_access": False,
                    "preferred_capabilities": ["summarization", "documentation"],
                    "acceptance_criteria": ["Return a direct, truthful response."],
                    "depends_on": [],
                    "target_files": [],
                    "validation_steps": [],
                    "parallel_group": "",
                    "notes": "Deterministic local planner fallback.",
                }
            ],
        }

    def _planner_candidates(self, task: str, profile: RunProfile, code_task: bool) -> list[str]:
        compact_task = " ".join(task.split())
        def dedupe(items: list[str]) -> list[str]:
            seen: set[str] = set()
            ordered: list[str] = []
            for item in items:
                if item in seen:
                    continue
                seen.add(item)
                ordered.append(item)
            return ordered
        if profile.name == "balanced":
            if code_task and self._should_escalate_code_planning(compact_task, profile):
                return dedupe([
                    profile.planner_worker,
                    "ollama_planner",
                    "codex_architect",
                    "hermes_designer",
                ])
            return dedupe([
                profile.planner_worker,
                "ollama_planner",
                "hermes_designer",
                "codex_architect",
            ])
        if code_task and self._should_escalate_code_planning(compact_task, profile):
            return dedupe([
                "codex_architect",
                profile.planner_worker,
                "hermes_designer",
                "ollama_planner",
            ])
        if profile.name != "premium" or len(compact_task) <= 160:
            return dedupe([
                "ollama_planner",
                profile.planner_worker,
                "hermes_designer",
                "codex_architect",
            ])
        return dedupe([
            profile.planner_worker,
            "ollama_planner",
            "hermes_designer",
            "codex_architect",
        ])

    def _should_escalate_code_planning(self, compact_task: str, profile: RunProfile) -> bool:
        lowered = compact_task.lower()
        complexity_hints = (
            "architecture",
            "refactor",
            "multi-file",
            "cross-file",
            "regression",
            "migration",
            "bug",
            "feature",
            "架构",
            "重构",
            "跨文件",
            "回归",
            "迁移",
            "缺陷",
            "功能",
        )
        return profile.name == "premium" or len(compact_task) > 120 or any(hint in lowered for hint in complexity_hints)

    def _parse_subtasks(self, raw_subtasks: object, task: str) -> list[Subtask]:
        subtasks: list[Subtask] = []
        if not isinstance(raw_subtasks, list):
            return subtasks
        for index, raw in enumerate(raw_subtasks, start=1):
            if not isinstance(raw, dict):
                continue
            subtasks.append(
                Subtask(
                    task_id=str(raw.get("task_id") or f"S{index}"),
                    title=str(raw.get("title") or f"Subtask {index}"),
                    goal=str(raw.get("goal") or task).strip(),
                    kind=str(raw.get("kind") or "coordination").strip().lower(),
                    difficulty=clamp_int(raw.get("difficulty"), 1, 5, 2),
                    repeatability=clamp_int(raw.get("repeatability"), 1, 5, 2),
                    risk=clamp_int(raw.get("risk"), 1, 5, 2),
                    needs_write_access=bool(raw.get("needs_write_access", False)),
                    preferred_capabilities=normalize_str_list(raw.get("preferred_capabilities")),
                    acceptance_criteria=normalize_str_list(raw.get("acceptance_criteria")),
                    notes=str(raw.get("notes") or "").strip(),
                    depends_on=normalize_str_list(raw.get("depends_on")),
                    target_files=normalize_str_list(raw.get("target_files")),
                    validation_steps=normalize_str_list(raw.get("validation_steps")),
                    parallel_group=str(raw.get("parallel_group") or "").strip(),
                    required_skills=normalize_str_list(raw.get("required_skills")),
                    coordination_notes=normalize_str_list(raw.get("coordination_notes")),
                )
            )
        return subtasks

    def _fallback_subtask(self, task: str) -> Subtask:
        return Subtask(
            task_id="S1",
            title="Fallback single subtask",
            goal=task,
            kind="coordination",
            difficulty=3,
            repeatability=2,
            risk=2,
            needs_write_access=False,
            preferred_capabilities=["planning"],
            acceptance_criteria=["Return a useful outcome for the user task."],
            notes="Planner returned no subtasks, so MVP created a fallback task.",
        )

    def _upgrade_code_plan(self, task: str, plan: TaskPlan, profile: RunProfile | None = None) -> TaskPlan:
        if not self._is_code_heavy_task(task) and not any(subtask.kind in {"coding", "architecture", "testing"} or subtask.needs_write_access for subtask in plan.subtasks):
            return plan

        subtasks = self._enrich_subtasks_with_repo_targets(task, list(plan.subtasks))
        has_architecture = any(subtask.kind in {"architecture", "design", "requirements"} for subtask in subtasks)
        implementation_tasks = [subtask for subtask in subtasks if self._is_implementation_subtask(subtask)]

        if implementation_tasks:
            first_impl_index = min(subtasks.index(item) for item in implementation_tasks)
            last_impl_index = max(subtasks.index(item) for item in implementation_tasks)
            has_pre_impl_phase = any(not item.needs_write_access for item in subtasks[:first_impl_index])
            has_post_impl_phase = any(not item.needs_write_access for item in subtasks[last_impl_index + 1 :])
        else:
            has_pre_impl_phase = False
            has_post_impl_phase = False

        if implementation_tasks and (not has_architecture and not has_pre_impl_phase):
            subtasks = self._inject_architecture_subtask(task, subtasks, implementation_tasks)
            has_pre_impl_phase = True

        if not implementation_tasks and self._has_write_intent(task):
            subtasks.append(self._make_implementation_subtask(task, depends_on=[subtasks[-1].task_id] if subtasks else []))
            implementation_tasks = [subtask for subtask in subtasks if self._is_implementation_subtask(subtask)]
            has_post_impl_phase = False

        subtasks = self._enrich_subtasks_with_repo_targets(task, subtasks)
        subtasks = self._attach_repo_context(subtasks)
        max_groups = {"cheap": 2, "balanced": 3, "premium": 4}.get(
            profile.name if profile else "balanced", 3)
        subtasks = self._split_parallel_code_packages(task, subtasks, max_groups=max_groups)
        implementation_tasks = [subtask for subtask in subtasks if self._is_implementation_subtask(subtask)]
        has_testing = any(subtask.kind in {"testing", "qa"} and not subtask.needs_write_access for subtask in subtasks)
        high_complexity = any(subtask.difficulty >= 3 or subtask.risk >= 3 for subtask in implementation_tasks) or len(subtasks) <= 2

        if implementation_tasks and ((high_complexity and not has_post_impl_phase) or (not has_testing and not has_post_impl_phase)):
            subtasks = self._inject_testing_subtask(task, subtasks, implementation_tasks)

        subtasks = self._enrich_subtasks_with_repo_targets(task, subtasks)
        subtasks = self._attach_repo_context(subtasks)
        subtasks = self._renumber_subtasks(subtasks)
        return replace(
            plan,
            subtasks=subtasks,
            execution_strategy=self._upgrade_execution_strategy(plan.execution_strategy, subtasks),
        )

    def _inject_architecture_subtask(self, task: str, subtasks: list[Subtask], implementation_tasks: list[Subtask]) -> list[Subtask]:
        first_impl_index = min(subtasks.index(item) for item in implementation_tasks)
        shared_targets = self._collect_target_files(subtasks)
        architecture = Subtask(
            task_id="ARCH",
            title="Map code touch points and implementation order",
            goal=f"Identify the safest implementation path, impacted modules, and edge cases for: {task.strip()}",
            kind="architecture",
            difficulty=max(2, min(4, implementation_tasks[0].difficulty)),
            repeatability=2,
            risk=max(2, implementation_tasks[0].risk),
            needs_write_access=False,
            preferred_capabilities=["architecture", "planning", "design", "review"],
            acceptance_criteria=[
                "Name the modules or files that are most likely to change.",
                "Call out the implementation order and main regression risks.",
            ],
            notes="Architecture gate inserted by MVP to improve code-task delegation quality.",
            target_files=shared_targets,
        )
        if not any(item.title == architecture.title for item in subtasks):
            subtasks.insert(first_impl_index, architecture)
        return subtasks

    def _inject_testing_subtask(self, task: str, subtasks: list[Subtask], implementation_tasks: list[Subtask]) -> list[Subtask]:
        dependency_titles = [item.task_id for item in implementation_tasks]
        validation_steps = self._collect_validation_steps(subtasks) or [
            "Check whether the reported change satisfies the requested behavior.",
            "List any test or verification gap that still remains.",
        ]
        testing = Subtask(
            task_id="VERIFY",
            title="Verify implementation outcome and regression surface",
            goal=f"Validate the code-task outcome for: {task.strip()}",
            kind="testing",
            difficulty=max(2, min(4, max(item.difficulty for item in implementation_tasks))),
            repeatability=3,
            risk=max(2, max(item.risk for item in implementation_tasks)),
            needs_write_access=False,
            preferred_capabilities=["testing", "qa", "review", "coding"],
            acceptance_criteria=[
                "State whether the implementation appears to satisfy the requested change.",
                "Call out any missing verification or likely regression area.",
            ],
            notes="Verification gate inserted by MVP after implementation.",
            depends_on=dependency_titles,
            target_files=self._collect_target_files(subtasks),
            validation_steps=validation_steps,
        )
        if not any(item.title == testing.title for item in subtasks):
            subtasks.append(testing)
        return subtasks

    def _make_implementation_subtask(self, task: str, depends_on: list[str]) -> Subtask:
        return Subtask(
            task_id="IMPL",
            title="Implement the requested code change",
            goal=task.strip(),
            kind="coding",
            difficulty=3,
            repeatability=2,
            risk=3,
            needs_write_access=True,
            preferred_capabilities=["coding", "architecture", "review"],
            acceptance_criteria=[
                "Apply the code change requested by the user.",
                "Describe the concrete files or modules touched.",
            ],
            notes="Implementation task synthesized by MVP because the planner returned code intent without a write-capable step.",
            depends_on=depends_on,
        )

    def _upgrade_execution_strategy(self, original: str, subtasks: list[Subtask]) -> str:
        if not any(subtask.kind in {"architecture", "coding", "testing"} for subtask in subtasks):
            return original
        parallel_groups = {subtask.parallel_group for subtask in subtasks if subtask.parallel_group}
        if parallel_groups:
            return (
                "Code-first orchestration: architecture gate -> parallel file-scope implementation packages -> "
                "verification, with repository-aware routing on each package."
            )
        return (
            "Code-first orchestration: architecture gate -> implementation -> verification, "
            "with each phase routed independently for higher coding quality and clearer accountability."
        )

    def _renumber_subtasks(self, subtasks: list[Subtask]) -> list[Subtask]:
        mapping = {subtask.task_id: f"S{index}" for index, subtask in enumerate(subtasks, start=1)}
        renumbered: list[Subtask] = []
        for index, subtask in enumerate(subtasks, start=1):
            renumbered.append(
                replace(
                    subtask,
                    task_id=f"S{index}",
                    depends_on=[mapping.get(item, item) for item in subtask.depends_on],
                )
            )
        return renumbered

    def _enrich_subtasks_with_repo_targets(self, task: str, subtasks: list[Subtask]) -> list[Subtask]:
        enriched: list[Subtask] = []
        for subtask in subtasks:
            raw_existing_targets = list(subtask.target_files)
            existing_targets = self.repo_index.normalize_paths(raw_existing_targets) or raw_existing_targets
            should_infer = self._should_infer_repo_targets(task, subtask, existing_targets)
            if should_infer:
                inferred_targets = self.repo_index.infer_files(
                    self._repo_query_for_subtask(task, subtask),
                    existing_paths=existing_targets,
                    limit=6,
                )
            else:
                inferred_targets = existing_targets

            if inferred_targets != subtask.target_files:
                enriched.append(replace(subtask, target_files=inferred_targets))
            else:
                enriched.append(subtask)
        return enriched

    def _split_parallel_code_packages(self, task: str, subtasks: list[Subtask], max_groups: int = 3) -> list[Subtask]:
        packaged: list[Subtask] = []
        for subtask in subtasks:
            if not self._is_implementation_subtask(subtask):
                packaged.append(subtask)
                continue

            clusters = self._dependency_aware_clusters(subtask.target_files, max_groups=max_groups)
            # Don't split if too few files and same top-level module
            if len(clusters) <= 1:
                packaged.append(subtask)
                continue
            if len(subtask.target_files) < 3 and len(
                    {f.split("/", 1)[0] for f in subtask.target_files}) <= 1:
                packaged.append(subtask)
                continue

            group_id = f"parallel:{subtask.task_id.lower()}"
            for index, cluster in enumerate(clusters, start=1):
                cluster_kind = self._cluster_kind(subtask, cluster)
                capabilities = list(subtask.preferred_capabilities)
                for capability in self._cluster_capabilities(cluster_kind):
                    if capability not in capabilities:
                        capabilities.append(capability)
                packaged.append(
                    replace(
                        subtask,
                        task_id=f"{subtask.task_id}_P{index}",
                        title=self._parallel_title(subtask.title, cluster, index),
                        goal=f"{subtask.goal.strip()} Focus scope: {self.repo_index.describe_scope(cluster)}.",
                        kind=cluster_kind,
                        preferred_capabilities=capabilities,
                        target_files=cluster,
                        validation_steps=self._validation_steps_for_cluster(subtask, cluster),
                        parallel_group=group_id,
                        notes=(
                            f"{subtask.notes} "
                            f"Repository-aware package {index} created by MVP for parallel-ready file-scope execution."
                        ).strip(),
                    )
                )
        return packaged

    def _collect_target_files(self, subtasks: list[Subtask]) -> list[str]:
        merged: list[str] = []
        for subtask in subtasks:
            for item in subtask.target_files:
                if item not in merged:
                    merged.append(item)
        return merged

    def _collect_validation_steps(self, subtasks: list[Subtask]) -> list[str]:
        merged: list[str] = []
        for subtask in subtasks:
            for item in subtask.validation_steps:
                if item not in merged:
                    merged.append(item)
        return merged

    def _is_implementation_subtask(self, subtask: Subtask) -> bool:
        return subtask.kind == "coding" or subtask.needs_write_access

    def _should_infer_repo_targets(self, task: str, subtask: Subtask, existing_targets: list[str]) -> bool:
        if existing_targets:
            return True
        if self._is_implementation_subtask(subtask):
            return True
        if subtask.kind in {"architecture", "testing", "qa", "documentation", "review"} and self._is_code_heavy_task(task):
            return True
        return False

    def _repo_query_for_subtask(self, task: str, subtask: Subtask) -> str:
        parts = [
            task,
            subtask.title,
            subtask.goal,
            subtask.notes,
            " ".join(subtask.acceptance_criteria),
            " ".join(subtask.validation_steps),
            " ".join(subtask.target_files),
        ]
        return " ".join(part.strip() for part in parts if part and part.strip())

    def _cluster_kind(self, subtask: Subtask, cluster: list[str]) -> str:
        if cluster and all("/tests/" in f"/{item}" or item.startswith("tests/") or item.startswith("test/") or item.endswith("_test.py") for item in cluster):
            return "testing"
        if cluster and all(item.endswith((".md", ".rst", ".txt")) or item.startswith("docs/") for item in cluster):
            return "documentation"
        return subtask.kind

    def _cluster_capabilities(self, kind: str) -> list[str]:
        if kind == "testing":
            return ["testing", "qa", "review", "coding"]
        if kind == "documentation":
            return ["documentation", "review", "summarization"]
        if kind == "architecture":
            return ["architecture", "planning", "review"]
        return ["coding", "architecture", "review"]

    def _parallel_title(self, title: str, cluster: list[str], index: int) -> str:
        scope = self.repo_index.describe_scope(cluster)
        return f"{title} [{index}: {scope}]"

    def _dependency_aware_clusters(self, paths: list[str], max_groups: int) -> list[list[str]]:
        normalized = self.repo_index.normalize_paths(paths)
        if len(normalized) <= 1:
            return [normalized] if normalized else []

        edges = self.code_intel.build_dep_graph(normalized)
        if not edges:
            return self.repo_index.cluster_files(normalized, max_groups=max_groups)

        adjacency: dict[str, set[str]] = {path: set() for path in normalized}
        for edge in edges:
            if edge.source in adjacency and edge.target in adjacency:
                adjacency[edge.source].add(edge.target)
                adjacency[edge.target].add(edge.source)

        components: list[list[str]] = []
        visited: set[str] = set()
        for path in normalized:
            if path in visited:
                continue
            stack = [path]
            group: list[str] = []
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                group.append(current)
                stack.extend(sorted(adjacency.get(current, set()) - visited))
            components.append(sorted(group, key=normalized.index))

        if len(components) <= 1:
            return self.repo_index.cluster_files(normalized, max_groups=max_groups)
        if len(components) > max_groups:
            flattened: list[str] = []
            kept = components[: max_groups - 1]
            for group in components[max_groups - 1 :]:
                flattened.extend(group)
            return [*kept, flattened]
        return components

    def _attach_repo_context(self, subtasks: list[Subtask]) -> list[Subtask]:
        enriched: list[Subtask] = []
        for subtask in subtasks:
            target_files = self.repo_index.normalize_paths(subtask.target_files) or list(subtask.target_files)
            notes = subtask.notes.strip()
            validation_steps = list(subtask.validation_steps)

            if target_files:
                scope = self.repo_index.describe_scope(target_files)
                scope_line = f"Repository scope: {scope}."
                if scope_line not in notes:
                    notes = f"{notes} {scope_line}".strip()

                symbol_line = self._symbol_context_line(target_files)
                if symbol_line and symbol_line not in notes:
                    notes = f"{notes} {symbol_line}".strip()

                for step in self._infer_validation_steps(subtask.kind, target_files):
                    if step not in validation_steps:
                        validation_steps.append(step)

            enriched.append(
                replace(
                    subtask,
                    target_files=target_files,
                    validation_steps=validation_steps,
                    notes=notes,
                )
            )
        return enriched

    def _symbol_context_line(self, target_files: list[str]) -> str:
        intel = self.code_intel.analyze_many(target_files[:4])
        symbols: list[str] = []
        for info in intel.values():
            for symbol in info.symbols[:3]:
                if symbol.kind in {"class", "function", "method"}:
                    symbols.append(symbol.name)
            if len(symbols) >= 5:
                break
        if not symbols:
            return ""
        return "Key symbols: " + ", ".join(symbols[:5]) + "."

    def _infer_validation_steps(self, kind: str, target_files: list[str]) -> list[str]:
        steps: list[str] = []
        test_files = [
            path for path in target_files
            if path.startswith(("tests/", "test/")) or "/tests/" in f"/{path}" or path.endswith("_test.py")
        ]
        if test_files:
            listed = ", ".join(test_files[:4])
            steps.append(f"Review or run targeted tests for: {listed}.")

        if kind in {"coding", "testing", "qa"}:
            source_files = [
                path for path in target_files
                if path.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs"))
                and path not in test_files
            ]
            if source_files:
                steps.append(
                    "Confirm the changed code path and the verification path stay aligned for: "
                    + ", ".join(source_files[:4])
                    + "."
                )
        return steps

    def _validation_steps_for_cluster(self, subtask: Subtask, cluster: list[str]) -> list[str]:
        merged = list(subtask.validation_steps)
        for step in self._infer_validation_steps(subtask.kind, cluster):
            if step not in merged:
                merged.append(step)
        return merged

    def _is_code_heavy_task(self, task: str) -> bool:
        lowered = task.lower()
        return any(keyword in lowered for keyword in CODE_INTENT_KEYWORDS)

    def _has_write_intent(self, task: str) -> bool:
        lowered = task.lower()
        sanitized = lowered
        for negation in READONLY_NEGATIONS:
            sanitized = sanitized.replace(negation.lower(), "")
        return any(keyword in sanitized for keyword in WRITE_INTENT_KEYWORDS)

    def _fast_path_plan(self, task: str, profile: RunProfile) -> TaskPlan | None:
        if profile.name != "cheap":
            return None
        compact_task = " ".join(task.split())
        if len(compact_task) > 160:
            return None
        if self._has_write_intent(compact_task):
            return None

        lowered = compact_task.lower()
        kind = "coordination"
        preferred_capabilities = ["summarization"]
        title = "Fast response task"
        goal = compact_task
        if any(keyword in lowered for keyword in ("confirm", "one sentence", "summary", "reply", "确认", "一句话", "总结", "概括", "回复")):
            kind = "summarization"
            preferred_capabilities = ["summarization", "documentation"]
            title = "Generate concise answer"
        elif any(keyword in lowered for keyword in ("introduce", "explain", "describe", "介绍", "解释", "说明")):
            kind = "documentation"
            preferred_capabilities = ["documentation", "summarization"]
            title = "Generate explanation"
        elif any(keyword in lowered for keyword in ("check", "review", "assess", "look at", "检查", "审查", "评估", "看看")):
            kind = "review"
            preferred_capabilities = ["review", "qa"]
            title = "Quick review"
        elif any(keyword in lowered for keyword in ("research", "investigate", "analyze", "研究", "调研", "分析")):
            kind = "research"
            preferred_capabilities = ["research", "summarization"]
            title = "Quick analysis"

        return TaskPlan(
            summary=compact_task,
            execution_strategy="Fast path: single read-only subtask without remote planning.",
            subtasks=[
                Subtask(
                    task_id="S1",
                    title=title,
                    goal=goal,
                    kind=kind,
                    difficulty=1,
                    repeatability=4,
                    risk=1,
                    needs_write_access=False,
                    preferred_capabilities=preferred_capabilities,
                    acceptance_criteria=["Return a direct, truthful, read-only response that matches the user request."],
                    notes="Fast-path plan generated locally to reduce coordination overhead.",
                )
            ],
            planner_worker="mvp_fast_path",
            raw_response="local fast-path planner",
        )

    def _augment_plan_with_skills(self, task: str, plan: TaskPlan) -> TaskPlan:
        if self.skill_mesh is None:
            return plan
        profile = self.skill_mesh.profile_task(task, list(plan.subtasks), self.worker_specs)
        subtasks = [self._apply_skill_annotations(subtask, profile) for subtask in plan.subtasks]
        subtasks = self._inject_skill_guardrails(task, subtasks, profile)
        if subtasks != plan.subtasks:
            subtasks = self._renumber_subtasks(subtasks)
        return replace(
            plan,
            subtasks=subtasks,
            doctrine=list(profile.doctrine),
            required_skills=list(profile.required_skill_ids),
            capability_gaps=list(profile.capability_gaps),
        )

    def _apply_skill_annotations(self, subtask: Subtask, profile) -> Subtask:
        required = list(subtask.required_skills)
        required.extend(profile.subtask_skill_map.get(subtask.task_id, ()))
        coordination_notes = list(subtask.coordination_notes)
        coordination_notes.extend(profile.subtask_notes.get(subtask.task_id, ()))
        extra_caps = self._capabilities_from_required_skills(required)
        preferred = list(subtask.preferred_capabilities)
        for capability in extra_caps:
            if capability not in preferred:
                preferred.append(capability)
        note_blob = subtask.notes.strip()
        if coordination_notes:
            coord_line = "Coordination: " + " ".join(coordination_notes)
            if coord_line not in note_blob:
                note_blob = f"{note_blob} {coord_line}".strip()
        return replace(
            subtask,
            preferred_capabilities=preferred,
            required_skills=self._dedupe_list(required),
            coordination_notes=self._dedupe_list(coordination_notes),
            notes=note_blob,
        )

    def _inject_skill_guardrails(self, task: str, subtasks: list[Subtask], profile) -> list[Subtask]:
        upgraded = list(subtasks)
        required = set(profile.required_skill_ids)
        if "mvp-core:security-guard" in required and not any(
            item.kind == "security" or item.title == "Run security guardrail review" for item in upgraded
        ):
            upgraded.append(
                Subtask(
                    task_id="SECURITY",
                    title="Run security guardrail review",
                    goal=f"Review auth, secrets, permissions, and untrusted-input risks for: {task.strip()}",
                    kind="security",
                    difficulty=3,
                    repeatability=3,
                    risk=4,
                    needs_write_access=False,
                    preferred_capabilities=["security", "review", "architecture"],
                    acceptance_criteria=[
                        "Call out any security-sensitive surface or confirm none were introduced.",
                        "List the specific files or flows that deserve security attention.",
                    ],
                    depends_on=[item.task_id for item in upgraded if item.needs_write_access],
                    target_files=self._collect_target_files(upgraded),
                    validation_steps=["Check auth, permissions, secret handling, and unsafe input paths."],
                    required_skills=["mvp-core:security-guard"],
                    coordination_notes=["Use this as a guardrail review, not a rewrite pass."],
                    notes="Security gate inserted by MVP skill mesh.",
                )
            )
        if "mvp-core:performance-guard" in required and not any(
            item.title == "Run performance and efficiency review" for item in upgraded
        ):
            upgraded.append(
                Subtask(
                    task_id="PERF",
                    title="Run performance and efficiency review",
                    goal=f"Check latency, runtime cost, and efficiency impact for: {task.strip()}",
                    kind="testing",
                    difficulty=3,
                    repeatability=3,
                    risk=3,
                    needs_write_access=False,
                    preferred_capabilities=["performance", "testing", "review"],
                    acceptance_criteria=[
                        "Identify likely hot paths or confirm the task is not performance sensitive.",
                        "State the main runtime or latency concern, if any.",
                    ],
                    depends_on=[item.task_id for item in upgraded if item.needs_write_access],
                    target_files=self._collect_target_files(upgraded),
                    validation_steps=["Check hot paths, loops, queries, memory pressure, and latency-sensitive flows."],
                    required_skills=["mvp-core:performance-guard"],
                    coordination_notes=["Use this as an efficiency gate before final sign-off."],
                    notes="Performance gate inserted by MVP skill mesh.",
                )
            )
        if "mvp-core:docs-handoff" in required and not any(item.kind == "documentation" for item in upgraded):
            upgraded.append(
                Subtask(
                    task_id="DOCS",
                    title="Write concise handoff notes",
                    goal=f"Capture the user-visible or operator-visible impact of: {task.strip()}",
                    kind="documentation",
                    difficulty=2,
                    repeatability=4,
                    risk=2,
                    needs_write_access=False,
                    preferred_capabilities=["documentation", "summarization", "review"],
                    acceptance_criteria=[
                        "Summarize what changed, what to verify, and what remains risky.",
                    ],
                    depends_on=[item.task_id for item in upgraded if item.needs_write_access],
                    target_files=self._collect_target_files(upgraded),
                    validation_steps=["Call out API, CLI, config, migration, or README impact where relevant."],
                    required_skills=["mvp-core:docs-handoff"],
                    coordination_notes=["Leave a clean operator/developer handoff summary."],
                    notes="Documentation handoff inserted by MVP skill mesh.",
                )
            )
        if any(item.parallel_group for item in upgraded) and not any(item.kind == "coordination" for item in upgraded):
            upgraded.append(
                Subtask(
                    task_id="SYNC",
                    title="Reconcile parallel packages",
                    goal=f"Merge the parallel workstream findings for: {task.strip()}",
                    kind="coordination",
                    difficulty=2,
                    repeatability=3,
                    risk=3,
                    needs_write_access=False,
                    preferred_capabilities=["coordination", "review", "planning"],
                    acceptance_criteria=[
                        "State whether the parallel packages fit together cleanly.",
                        "Call out interface or ownership conflicts before final review.",
                    ],
                    depends_on=[item.task_id for item in upgraded if item.parallel_group],
                    target_files=self._collect_target_files(upgraded),
                    validation_steps=["Check cross-package interfaces, shared assumptions, and handoff completeness."],
                    required_skills=["mvp-core:parallel-code-packaging", "mvp-core:leader-routing"],
                    coordination_notes=["Use this checkpoint to reconcile sibling package outputs."],
                    notes="Coordination checkpoint inserted by MVP skill mesh.",
                )
            )
        return upgraded

    def _capabilities_from_required_skills(self, required_skills: list[str]) -> list[str]:
        if not required_skills:
            return []
        from .skill_mesh import capabilities_for_skill_ids

        return sorted(capabilities_for_skill_ids(required_skills))

    @staticmethod
    def _dedupe_list(values: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for value in values:
            normalized = str(value).strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            ordered.append(normalized)
        return ordered
