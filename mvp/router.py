from __future__ import annotations

from .models import Assignment, RunProfile, Subtask, WorkerSpec
from .skill_mesh import capabilities_for_skill_ids


def _profile_route_bias(spec: WorkerSpec, subtask: Subtask, profile: RunProfile) -> tuple[float, list[str]]:
    reasons: list[str] = []
    score = 0.0

    if spec.worker_id == profile.planner_worker and subtask.kind in {
        "planning", "design", "requirements", "architecture", "coordination", "research", "summarization",
    }:
        score += 4.0
        reasons.append("profile planner bias")

    if spec.worker_id == profile.reviewer_worker and subtask.kind in {
        "review", "testing", "qa", "security", "documentation",
    }:
        score += 3.6
        reasons.append("profile reviewer bias")

    if profile.name == "balanced":
        if spec.worker_id == "hermes_designer" and subtask.kind in {
            "planning", "design", "requirements", "architecture", "coordination",
        }:
            score += 3.0
            reasons.append("balanced hermes lead")
        if spec.worker_id == "claude_strategist" and subtask.kind in {
            "planning", "design", "review", "requirements", "architecture", "documentation",
        }:
            score += 3.0
            reasons.append("balanced claude strategy")
        if spec.worker_id == "claude_strategist" and subtask.kind in {"review", "testing", "qa"}:
            score += 5.0
            reasons.append("balanced claude review lead")
        if spec.worker_id == "claude_builder" and subtask.kind in {"coding", "testing", "documentation"}:
            score += 4.2
            reasons.append("balanced claude builder")
        if spec.worker_id == "openclaw_coder" and subtask.kind in {"coding", "testing", "research"}:
            score += 3.4
            reasons.append("balanced openclaw execution")
        if spec.worker_id == "ollama_planner" and subtask.kind in {
            "planning", "design", "requirements", "architecture", "coding",
        }:
            score -= 2.4
            reasons.append("balanced remote-first planning")
        if spec.worker_id == "ollama_reviewer" and subtask.kind in {"review", "testing", "qa"}:
            score -= 2.2
            reasons.append("balanced remote-first review")

    return score, reasons


def score_worker_with_memory(
    spec: WorkerSpec,
    subtask: Subtask,
    profile: RunProfile,
    worker_metrics: dict[str, dict[str, float]] | None = None,
    per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
    memory_scores: dict[str, dict[str, float]] | None = None,
) -> tuple[float, list[str]]:
    """Score a worker incorporating long-term memory performance data.

    Layers:
      1. Base rule-based scoring (capabilities, quality, cost, etc.)
      2. Real-time metrics adjustment (latency, failure rates)
      3. Long-term memory adjustment (historical success by task kind)
    """
    reasons: list[str] = []

    # --- Layer 1: base competency score ---
    capability_overlap = len(set(spec.capabilities) & set(subtask.preferred_capabilities))
    score = capability_overlap * 4.0
    if capability_overlap:
        reasons.append(f"capabilities x{capability_overlap}")

    score += spec.quality_tier * profile.quality_weight
    reasons.append(f"quality {spec.quality_tier}")

    score += spec.speed_tier * profile.speed_weight
    reasons.append(f"speed {spec.speed_tier}")

    score -= spec.cost_tier * profile.cost_weight
    reasons.append(f"cost {spec.cost_tier}")

    if profile.prefer_local and spec.local_only:
        score += profile.local_bonus
        reasons.append("local")

    if subtask.repeatability >= 4 and subtask.difficulty <= 2 and spec.local_only:
        score += profile.simple_repeatable_bonus
        reasons.append("simple-repeatable-local")

    if subtask.difficulty >= profile.escalate_difficulty:
        score += spec.quality_tier * 1.2
        reasons.append("difficulty-escalation")

    if subtask.risk >= profile.escalate_risk:
        score += spec.quality_tier * 1.4
        reasons.append("risk-escalation")

    # Kind-specific bonuses
    kind_bonuses = {
        "coding": 2.5,
        "architecture": 3.0,
        "design": 2.5,
        "review": 2.5,
        "testing": 2.2,
        "qa": 2.2,
        "security": 3.0,
        "performance": 2.8,
        "coordination": 2.0,
        "documentation": 1.6,
    }
    if subtask.kind in kind_bonuses:
        bonus = kind_bonuses[subtask.kind]
        if subtask.kind in spec.capabilities:
            score += bonus
            reasons.append(f"{subtask.kind}-specialist")

    # Write access gate
    if subtask.needs_write_access:
        if spec.supports_workspace_write:
            score += 4.0
            reasons.append("write-capable")
            if subtask.kind == "coding":
                score += 2.5
                reasons.append("implementation-gate")
            if subtask.target_files:
                score += 1.5
                reasons.append("target-file-owner")
        else:
            score -= 100.0
            reasons.append("readonly")

    # File scope overlap
    hints = _file_scope_capabilities(subtask.target_files)
    overlap = len(set(spec.capabilities) & hints)
    if overlap:
        score += overlap * 1.4
        reasons.append(f"file-scope x{overlap}")
    scope_shape = _file_scope_shape(subtask.target_files)
    if scope_shape == "tests" and set(spec.capabilities) & {"testing", "qa", "review"}:
        score += 2.2
        reasons.append("test-scope bias")
    elif scope_shape == "docs" and set(spec.capabilities) & {"documentation", "summarization", "review"}:
        score += 2.0
        reasons.append("docs-scope bias")
    elif scope_shape == "config" and set(spec.capabilities) & {"ops", "planning", "review"}:
        score += 1.8
        reasons.append("config-scope bias")
    elif scope_shape == "mixed-code-tests" and set(spec.capabilities) & {"coding", "testing", "review"}:
        score += 2.4
        reasons.append("mixed-code-test bias")

    if subtask.parallel_group and spec.speed_tier >= 3:
        score += 0.9
        reasons.append("parallel-ready")

    required_skill_caps = capabilities_for_skill_ids(subtask.required_skills)
    if required_skill_caps:
        skill_overlap = len(required_skill_caps & set(spec.capabilities))
        if skill_overlap:
            skill_bonus = skill_overlap * 1.5
            score += skill_bonus
            reasons.append(f"skill-mesh x{skill_overlap}")
        elif subtask.required_skills:
            score -= 1.2
            reasons.append("skill-gap")

    if spec.api_cost and profile.name == "cheap":
        score -= 3.0
        reasons.append("cheap-api-penalty")

    profile_bias, profile_reasons = _profile_route_bias(spec, subtask, profile)
    if profile_bias:
        score += profile_bias
        reasons.extend(profile_reasons)

    # --- Layer 2: real-time metrics ---
    metrics = (worker_metrics or {}).get(spec.worker_id)
    if metrics:
        avg_lat = float(metrics.get("avg_duration_ms", 0) or 0)
        if avg_lat > 0:
            penalty = min(avg_lat / 15000.0, 10.0) * profile.latency_weight
            score -= penalty
            reasons.append(f"latency-penalty {penalty:.1f}")
        fail_ratio = float(metrics.get("failure_ratio", 0) or 0)
        if fail_ratio >= 0.25:
            score -= fail_ratio * 4.0
            reasons.append(f"failure-penalty {fail_ratio:.2f}")

    if per_kind_metrics and subtask.kind:
        ks = per_kind_metrics.get(spec.worker_id, {}).get(subtask.kind)
        if ks and ks.get("samples", 0) >= 2:
            pr = float(ks.get("pass_rate", 0) or 0)
            sr = float(ks.get("success_rate", 0) or 0)
            if pr >= 0.8:
                score += pr * 3.0
                reasons.append(f"kind-{subtask.kind}-pass-{pr:.0%}")
            elif pr < 0.4:
                score -= (1.0 - pr) * 3.0
                reasons.append(f"kind-{subtask.kind}-low-pass-{pr:.0%}")
            elif sr < 0.5:
                score -= (1.0 - sr) * 2.0
                reasons.append(f"kind-{subtask.kind}-low-success-{sr:.0%}")

    # --- Layer 3: long-term memory ---
    if memory_scores:
        ms = memory_scores.get(spec.worker_id)
        if ms:
            samples = float(ms.get("samples", 0) or 0)
            success_rate = float(ms.get("success_rate", 0) or 0)
            if samples >= 3:
                # Reward consistently good workers
                if success_rate >= 0.8:
                    score += success_rate * 4.0
                    reasons.append(f"memory-success-{success_rate:.0%}")
                # Penalize consistently bad workers
                elif success_rate < 0.4:
                    score -= (1.0 - success_rate) * 4.0
                    reasons.append(f"memory-failure-{success_rate:.0%}")
                # Small bonus for experience
                experience_bonus = min(samples / 50.0, 1.5)
                score += experience_bonus
                reasons.append(f"memory-experience-{int(samples)}calls")

            # Per-kind memory performance
            kind_key = subtask.kind
            kind_success = float(ms.get(f"kind_{kind_key}_success", 0) or 0)
            kind_samples = float(ms.get(f"kind_{kind_key}_samples", 0) or 0)
            if kind_samples >= 2:
                if kind_success >= 0.7:
                    score += kind_success * 5.0
                    reasons.append(f"memory-kind-{kind_key}-{kind_success:.0%}")
                elif kind_success < 0.3:
                    score -= (1.0 - kind_success) * 5.0
                    reasons.append(f"memory-kind-{kind_key}-low-{kind_success:.0%}")

    return score, reasons


def rank_workers_with_memory(
    subtask: Subtask,
    worker_specs: dict[str, WorkerSpec],
    profile: RunProfile,
    exclude: set[str] | None = None,
    worker_metrics: dict[str, dict[str, float]] | None = None,
    per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
    memory_scores: dict[str, dict[str, float]] | None = None,
) -> list[Assignment]:
    exclude = exclude or set()
    ranked: list[Assignment] = []
    for wid, spec in worker_specs.items():
        if not spec.enabled or wid in exclude:
            continue
        score, reasons = score_worker_with_memory(
            spec, subtask, profile,
            worker_metrics=worker_metrics,
            per_kind_metrics=per_kind_metrics,
            memory_scores=memory_scores,
        )
        ranked.append(Assignment(subtask=subtask, worker_id=wid, score=score, rationale=reasons))
    ranked.sort(key=lambda a: a.score, reverse=True)
    return ranked


def _file_scope_capabilities(target_files: list[str]) -> set[str]:
    hints: set[str] = set()
    for raw_path in target_files:
        path = raw_path.lower().replace("\\", "/")
        if path.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".kt", ".cs")):
            hints.update({"coding", "architecture", "testing"})
        if path.startswith(("tests/", "test/")) or path.endswith("_test.py") or "/tests/" in f"/{path}":
            hints.update({"testing", "qa", "review", "coding"})
        if path.startswith("docs/") or path.endswith((".md", ".rst", ".txt")):
            hints.update({"documentation", "summarization", "review"})
        if path.endswith((".json", ".toml", ".yaml", ".yml", ".ini")):
            hints.update({"ops", "review", "planning"})
    return hints


def _file_scope_shape(target_files: list[str]) -> str:
    if not target_files:
        return "generic"
    paths = [item.lower().replace("\\", "/") for item in target_files]
    if all(path.startswith(("tests/", "test/")) or "/tests/" in f"/{path}" or path.endswith("_test.py") for path in paths):
        return "tests"
    if all(path.startswith("docs/") or path.endswith((".md", ".rst", ".txt")) for path in paths):
        return "docs"
    if all(path.endswith((".json", ".toml", ".yaml", ".yml", ".ini")) for path in paths):
        return "config"
    if any(path.startswith(("tests/", "test/")) or "/tests/" in f"/{path}" or path.endswith("_test.py") for path in paths):
        return "mixed-code-tests"
    return "code"


def score_worker(
    spec: WorkerSpec,
    subtask: Subtask,
    profile: RunProfile,
    worker_metrics: dict[str, dict[str, float]] | None = None,
    per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
) -> tuple[float, list[str]]:
    reasons: list[str] = []
    capability_overlap = len(set(spec.capabilities) & set(subtask.preferred_capabilities))
    score = capability_overlap * 4.0
    if capability_overlap:
        reasons.append(f"capability overlap x{capability_overlap}")

    score += spec.quality_tier * profile.quality_weight
    reasons.append(f"quality {spec.quality_tier}")

    score += spec.speed_tier * profile.speed_weight
    reasons.append(f"speed {spec.speed_tier}")

    score -= spec.cost_tier * profile.cost_weight
    reasons.append(f"cost penalty {spec.cost_tier}")

    if profile.prefer_local and spec.local_only:
        score += profile.local_bonus
        reasons.append("local bonus")

    if subtask.repeatability >= 4 and subtask.difficulty <= 2 and spec.local_only:
        score += profile.simple_repeatable_bonus
        reasons.append("repeatable-simple local bonus")

    if subtask.difficulty >= profile.escalate_difficulty:
        score += spec.quality_tier * 1.2
        reasons.append("difficulty escalation")

    if subtask.risk >= profile.escalate_risk:
        score += spec.quality_tier * 1.4
        reasons.append("risk escalation")

    if subtask.kind == "coding" and "coding" in spec.capabilities:
        score += 2.5
        reasons.append("coding specialist")

    if subtask.kind == "architecture" and "architecture" in spec.capabilities:
        score += 3.0
        reasons.append("architecture specialist")

    if subtask.kind == "design" and "design" in spec.capabilities:
        score += 2.5
        reasons.append("design specialist")

    if subtask.kind == "review" and "review" in spec.capabilities:
        score += 2.5
        reasons.append("review specialist")

    if subtask.kind in {"testing", "qa"} and set(spec.capabilities) & {"testing", "qa", "review", "coding"}:
        score += 2.2
        reasons.append("verification specialist")

    if subtask.needs_write_access:
        if spec.supports_workspace_write:
            score += 4.0
            reasons.append("can write current workspace")
        else:
            score -= 100.0
            reasons.append("cannot write current workspace")

    if subtask.kind == "coding" and subtask.needs_write_access and spec.supports_workspace_write:
        score += 2.5
        reasons.append("implementation gate")

    if subtask.kind == "coding" and subtask.target_files and spec.supports_workspace_write:
        score += 1.5
        reasons.append("target-file ownership fit")

    if subtask.kind == "architecture" and subtask.risk >= 3:
        score += spec.quality_tier * 0.8
        reasons.append("high-risk architecture bias")

    if subtask.kind in {"testing", "qa"} and subtask.validation_steps:
        score += spec.quality_tier * 0.5
        reasons.append("validation-aware reviewer")

    file_scope_hints = _file_scope_capabilities(subtask.target_files)
    file_scope_overlap = len(set(spec.capabilities) & file_scope_hints)
    if file_scope_overlap:
        score += file_scope_overlap * 1.4
        reasons.append(f"file-scope fit x{file_scope_overlap}")
    scope_shape = _file_scope_shape(subtask.target_files)
    if scope_shape == "tests" and set(spec.capabilities) & {"testing", "qa", "review"}:
        score += 2.2
        reasons.append("test-scope bias")
    elif scope_shape == "docs" and set(spec.capabilities) & {"documentation", "summarization", "review"}:
        score += 2.0
        reasons.append("docs-scope bias")
    elif scope_shape == "config" and set(spec.capabilities) & {"ops", "planning", "review"}:
        score += 1.8
        reasons.append("config-scope bias")
    elif scope_shape == "mixed-code-tests" and set(spec.capabilities) & {"coding", "testing", "review"}:
        score += 2.4
        reasons.append("mixed-code-test bias")

    if subtask.parallel_group and spec.speed_tier >= 3:
        score += 0.9
        reasons.append("parallel-package speed bias")

    if spec.api_cost and profile.name == "cheap":
        score -= 3.0
        reasons.append("cheap mode api penalty")

    profile_bias, profile_reasons = _profile_route_bias(spec, subtask, profile)
    if profile_bias:
        score += profile_bias
        reasons.extend(profile_reasons)

    metrics = (worker_metrics or {}).get(spec.worker_id)
    if metrics:
        avg_duration_ms = float(metrics.get("avg_duration_ms", 0.0) or 0.0)
        if avg_duration_ms > 0:
            latency_penalty = min(avg_duration_ms / 15000.0, 10.0) * profile.latency_weight
            score -= latency_penalty
            reasons.append(f"latency penalty {latency_penalty:.1f}")
        failure_ratio = float(metrics.get("failure_ratio", 0.0) or 0.0)
        if failure_ratio >= 0.25:
            score -= failure_ratio * 4.0
            reasons.append(f"failure penalty {failure_ratio:.2f}")

    if per_kind_metrics and subtask.kind:
        kind_stats = per_kind_metrics.get(spec.worker_id, {}).get(subtask.kind)
        if kind_stats and kind_stats.get("samples", 0) >= 2:
            pass_rate = float(kind_stats.get("pass_rate", 0.0) or 0.0)
            success_rate = float(kind_stats.get("success_rate", 0.0) or 0.0)
            if pass_rate >= 0.8:
                score += pass_rate * 3.0
                reasons.append(f"kind '{subtask.kind}' pass rate {pass_rate:.0%}")
            elif pass_rate < 0.4:
                score -= (1.0 - pass_rate) * 3.0
                reasons.append(f"kind '{subtask.kind}' low pass rate {pass_rate:.0%}")
            elif success_rate < 0.5:
                score -= (1.0 - success_rate) * 2.0
                reasons.append(f"kind '{subtask.kind}' low success rate {success_rate:.0%}")

    return score, reasons


def rank_workers(
    subtask: Subtask,
    worker_specs: dict[str, WorkerSpec],
    profile: RunProfile,
    exclude: set[str] | None = None,
    worker_metrics: dict[str, dict[str, float]] | None = None,
    per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
) -> list[Assignment]:
    exclude = exclude or set()
    ranked: list[Assignment] = []
    for worker_id, spec in worker_specs.items():
        if not spec.enabled or worker_id in exclude:
            continue
        score, reasons = score_worker(
            spec, subtask, profile,
            worker_metrics=worker_metrics,
            per_kind_metrics=per_kind_metrics,
        )
        ranked.append(Assignment(subtask=subtask, worker_id=worker_id, score=score, rationale=reasons))

    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def route_subtask(
    subtask: Subtask,
    worker_specs: dict[str, WorkerSpec],
    profile: RunProfile,
    worker_metrics: dict[str, dict[str, float]] | None = None,
    per_kind_metrics: dict[str, dict[str, dict[str, float]]] | None = None,
) -> Assignment:
    ranked = rank_workers(
        subtask, worker_specs, profile,
        worker_metrics=worker_metrics,
        per_kind_metrics=per_kind_metrics,
    )
    if not ranked:
        raise RuntimeError("No enabled workers are available for routing.")
    return ranked[0]
