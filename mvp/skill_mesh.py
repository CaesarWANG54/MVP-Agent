from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from pathlib import Path
from typing import Iterable

from .models import Subtask, WorkerSpec


IGNORE_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
}


@dataclass(frozen=True, slots=True)
class SkillCard:
    skill_id: str
    title: str
    summary: str
    source: str
    path: str
    tags: tuple[str, ...]
    capabilities: tuple[str, ...]
    internal: bool = False


@dataclass(frozen=True, slots=True)
class SkillProfile:
    required_skill_ids: tuple[str, ...] = ()
    recommended_skill_ids: tuple[str, ...] = ()
    doctrine: tuple[str, ...] = ()
    capability_gaps: tuple[str, ...] = ()
    subtask_skill_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    subtask_notes: dict[str, tuple[str, ...]] = field(default_factory=dict)


INTERNAL_SKILLS: tuple[SkillCard, ...] = (
    SkillCard(
        skill_id="mvp-core:leader-routing",
        title="Leader Routing",
        summary="Pick the right specialist, not just the first available worker.",
        source="mvp-core",
        path="internal://leader-routing",
        tags=("coordination", "routing", "delegation", "decision"),
        capabilities=("coordination", "planning", "review"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:repo-intel",
        title="Repository Intelligence",
        summary="Ground every coding task in likely files, symbols, and module boundaries.",
        source="mvp-core",
        path="internal://repo-intel",
        tags=("repository", "codebase", "routing", "architecture"),
        capabilities=("architecture", "coding", "review"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:architecture-gate",
        title="Architecture Gate",
        summary="Open complex work with impact mapping and implementation order before edits begin.",
        source="mvp-core",
        path="internal://architecture-gate",
        tags=("architecture", "planning", "requirements", "design"),
        capabilities=("architecture", "planning", "design"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:parallel-code-packaging",
        title="Parallel Code Packaging",
        summary="Split disjoint file scopes into parallel packages with explicit handoff boundaries.",
        source="mvp-core",
        path="internal://parallel-code-packaging",
        tags=("parallel", "coding", "packaging", "coordination"),
        capabilities=("coding", "architecture", "coordination"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:testing-regression-gate",
        title="Testing Regression Gate",
        summary="Every meaningful code change gets an explicit verification and regression pass.",
        source="mvp-core",
        path="internal://testing-regression-gate",
        tags=("testing", "qa", "regression", "verification"),
        capabilities=("testing", "qa", "review"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:security-guard",
        title="Security Guard",
        summary="Auth, secrets, permissions, and untrusted input require a dedicated security lens.",
        source="mvp-core",
        path="internal://security-guard",
        tags=("security", "auth", "permissions", "secrets", "review"),
        capabilities=("security", "review", "architecture"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:performance-guard",
        title="Performance Guard",
        summary="Hot paths, slow flows, and scaling changes require latency and runtime checks.",
        source="mvp-core",
        path="internal://performance-guard",
        tags=("performance", "latency", "optimization", "profiling"),
        capabilities=("performance", "testing", "review"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:docs-handoff",
        title="Docs Handoff",
        summary="Public behavior, APIs, config, and workflows should leave behind clear handoff docs.",
        source="mvp-core",
        path="internal://docs-handoff",
        tags=("documentation", "handoff", "api", "config"),
        capabilities=("documentation", "review", "summarization"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:cost-aware-routing",
        title="Cost-aware Routing",
        summary="Move repetitive low-risk work local, escalate only the hard parts.",
        source="mvp-core",
        path="internal://cost-aware-routing",
        tags=("cost", "routing", "local-first", "efficiency"),
        capabilities=("coordination", "planning", "triage"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:memory-feedback",
        title="Memory Feedback",
        summary="Learn from successes, failures, and capability gaps on every run.",
        source="mvp-core",
        path="internal://memory-feedback",
        tags=("memory", "learning", "evolution", "feedback"),
        capabilities=("coordination", "review", "planning"),
        internal=True,
    ),
    SkillCard(
        skill_id="mvp-core:failure-reroute",
        title="Failure Reroute",
        summary="When a specialist fails, reframe, re-route, and keep the task moving.",
        source="mvp-core",
        path="internal://failure-reroute",
        tags=("fallback", "reroute", "resilience", "coordination"),
        capabilities=("coordination", "review", "triage"),
        internal=True,
    ),
)


CORE_CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "coordination": ("coordination", "planning", "review"),
    "architecture": ("architecture", "planning", "design"),
    "coding": ("coding", "architecture", "testing"),
    "testing": ("testing", "qa", "review"),
    "review": ("review", "qa", "architecture"),
    "security": ("security", "review", "architecture"),
    "performance": ("performance", "testing", "review"),
    "documentation": ("documentation", "summarization", "review"),
    "research": ("research", "summarization"),
}

SECURITY_KEYWORDS = (
    "auth", "oauth", "jwt", "token", "secret", "credential", "password", "permission",
    "security", "secure", "vulnerability", "sanitize", "injection", "xss", "csrf",
    "access control", "apikey", "api key", "acl", "role", "rbac", "rls",
)
PERFORMANCE_KEYWORDS = (
    "performance", "perf", "latency", "slow", "optimize", "optimization", "memory",
    "cpu", "gpu", "throughput", "response time", "bottleneck", "cache", "scaling",
)
DOCS_KEYWORDS = (
    "readme", "docs", "document", "documentation", "api", "sdk", "cli", "config",
    "migration", "schema", "usage", "guide", "handoff",
)
CODE_KEYWORDS = (
    "code", "coding", "refactor", "fix", "bug", "implement", "module", "file", "repo",
    "repository", "class", "function", "test", "patch", "feature", "workspace",
)
CAPABILITY_SEMANTIC_ALIASES: dict[str, tuple[str, ...]] = {
    "security": (
        "login", "log in", "sign in", "signin", "session", "sessions", "access denied",
        "lose access", "authorization", "authorize", "identity", "permission denied",
        "credential", "credentials", "account takeover", "tenant isolation",
        "认证", "鉴权", "授权", "登录", "会话", "令牌", "权限", "越权", "访问控制", "密钥", "凭证",
    ),
    "performance": (
        "sluggish", "lag", "laggy", "slowdown", "under load", "high load", "heavy load",
        "hot path", "timing out", "timeout", "p95", "p99", "tail latency", "spike",
        "degraded", "throughput drop", "response degradation",
        "卡顿", "很慢", "变慢", "响应慢", "超时", "负载", "高并发", "吞吐下降", "延迟升高",
    ),
    "documentation": (
        "onboarding", "operator guide", "runbook", "playbook", "handover", "how to use",
        "integration guide", "migration note", "usage note",
        "使用说明", "接入说明", "操作手册", "交接文档", "迁移说明", "变更说明",
    ),
    "coding": (
        "middleware", "endpoint", "handler", "service", "controller", "repository layer",
        "bugfix", "fixup", "implementation", "code path",
        "中间件", "接口", "处理器", "服务层", "控制器", "代码路径", "修复代码", "实现逻辑",
    ),
    "testing": (
        "regression", "smoke test", "validate", "verification", "reproduce", "coverage",
        "回归", "冒烟", "验证", "复现", "覆盖率",
    ),
}
STRICT_DOMAIN_HINTS: dict[str, tuple[str, ...]] = {
    "github": (
        "github", "pull request", "review thread", "review comment", "requested changes",
        "pr", "issue", "actions", "workflow", "ci", "checks",
    ),
    "game-studio": (
        "game", "gameplay", "phaser", "sprite", "hud", "canvas", "webgl", "three", "r3f",
        "playtest", "level", "enemy",
    ),
    "canva": (
        "canva", "presentation", "slides", "deck", "poster", "social media", "thumbnail",
        "brand kit", "design",
    ),
    "test-android-apps": (
        "android", "adb", "apk", "emulator", "compose", "mobile", "logcat", "perfetto",
    ),
    "hugging-face": (
        "hugging face", "huggingface", "transformers", "dataset", "model", "space",
        "gradio", "trl", "lora", "sft", "dpo", "vision model",
    ),
    "supabase": (
        "supabase", "postgres", "postgresql", "sql", "database", "rls", "edge function",
        "realtime", "storage", "migration", "query", "table", "schema",
    ),
    "hatch-pet": (
        "pet", "sprite", "icon", "mascot", "avatar", "animation", "spritesheet",
    ),
    "openai-docs": (
        "openai", "gpt", "responses api", "api docs", "openai api", "model selection",
    ),
    ".system:imagegen": (
        "image", "icon", "logo", "sprite", "illustration", "photo", "background", "avatar",
        "图片", "图标", "形象", "插画", "动画", "宠物",
    ),
    ".system:skill-creator": (
        "skill", "skills", "create skill", "build skill", "agent skill", "workflow skill",
        "技能", "技能包", "封装 skill", "写 skill",
    ),
    ".system:skill-installer": (
        "install skill", "install skills", "add skill", "import skill",
        "安装 skill", "安装技能", "导入技能",
    ),
    ".system:openai-docs": (
        "openai", "gpt", "responses api", "assistants api", "official docs",
        "官方文档", "模型选择", "openai api",
    ),
    "windows-multi-agent-orchestrator": (
        "multi agent", "orchestrator", "orchestration", "agent mesh", "control plane",
        "多agent", "多智能体", "协调平台", "指挥平台", "总控",
    ),
}


def capabilities_for_skill_ids(skill_ids: Iterable[str]) -> set[str]:
    lookup = {card.skill_id: card for card in INTERNAL_SKILLS}
    inferred: set[str] = set()
    for skill_id in skill_ids:
        card = lookup.get(skill_id)
        if card is not None:
            inferred.update(card.capabilities)
            continue
        lowered = skill_id.lower()
        for capability, aliases in CORE_CAPABILITY_ALIASES.items():
            if capability in lowered or any(alias in lowered for alias in aliases):
                inferred.add(capability)
    expanded: set[str] = set()
    for capability in inferred:
        expanded.update(CORE_CAPABILITY_ALIASES.get(capability, (capability,)))
        expanded.add(capability)
    return expanded


class SkillMesh:
    def __init__(self, roots: Iterable[str | Path], max_cards: int = 256) -> None:
        self.roots = tuple(Path(root).expanduser() for root in roots)
        self.max_cards = max(32, max_cards)
        self._cache: list[SkillCard] | None = None

    def discover(self, force_refresh: bool = False) -> list[SkillCard]:
        if self._cache is not None and not force_refresh:
            return list(self._cache)
        cards = list(INTERNAL_SKILLS)
        for root in self.roots:
            cards.extend(self._discover_external(root))
            if len(cards) >= self.max_cards:
                break
        deduped: dict[str, SkillCard] = {}
        for card in cards:
            deduped.setdefault(card.skill_id.lower(), card)
        ordered = sorted(deduped.values(), key=lambda item: (not item.internal, item.skill_id.lower()))
        self._cache = ordered[: self.max_cards]
        return list(self._cache)

    def catalog_summary(self, limit: int = 20) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for card in self.discover()[:limit]:
            rows.append({
                "skill_id": card.skill_id,
                "title": card.title,
                "source": card.source,
                "summary": card.summary,
                "capabilities": list(card.capabilities),
                "tags": list(card.tags),
                "internal": card.internal,
                "path": card.path,
            })
        return rows

    def profile_task(
        self,
        task: str,
        subtasks: list[Subtask],
        worker_specs: dict[str, WorkerSpec],
    ) -> SkillProfile:
        cards = self.discover()
        demand = self._demanded_capabilities(task, subtasks)
        doctrine = self._build_doctrine(task, subtasks, demand)
        required_ids = self._select_required_internal_skills(demand, doctrine)
        recommended = self._select_external_skills(cards, demand, task, subtasks)
        gaps = self._capability_gaps(demand, worker_specs)
        subtask_skill_map: dict[str, tuple[str, ...]] = {}
        subtask_notes: dict[str, tuple[str, ...]] = {}
        for subtask in subtasks:
            skill_ids = list(required_ids)
            for capability in self._subtask_capabilities(subtask, task):
                skill_ids.extend(self._internal_skills_for_capability(capability))
            skill_ids.extend(self._recommended_external_for_subtask(cards, task, subtask))
            subtask_skill_map[subtask.task_id] = tuple(_dedupe(skill_ids))
            subtask_notes[subtask.task_id] = tuple(self._coordination_notes(subtask))
        return SkillProfile(
            required_skill_ids=tuple(_dedupe(required_ids)),
            recommended_skill_ids=tuple(_dedupe(recommended)),
            doctrine=tuple(doctrine),
            capability_gaps=tuple(gaps),
            subtask_skill_map=subtask_skill_map,
            subtask_notes=subtask_notes,
        )

    def _discover_external(self, root: Path) -> list[SkillCard]:
        if not root.exists() or not root.is_dir():
            return []
        cards: list[SkillCard] = []
        for current_root, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in IGNORE_DIRS]
            if "SKILL.md" not in filenames:
                continue
            skill_path = Path(current_root) / "SKILL.md"
            card = self._load_skill_card(root, skill_path)
            if card is not None:
                cards.append(card)
            if len(cards) >= self.max_cards:
                break
        return cards

    def _load_skill_card(self, root: Path, skill_path: Path) -> SkillCard | None:
        try:
            text = skill_path.read_text(encoding="utf-8")
        except Exception:
            return None
        relative_path = skill_path.parent.relative_to(root)
        relative = relative_path.as_posix()
        first_line = next((line.strip("# ").strip() for line in text.splitlines() if line.strip()), relative)
        summary = self._skill_summary(text)
        tags = tuple(sorted(_tokenize(f"{relative} {first_line} {summary}") - {"skill"}))
        capabilities = tuple(sorted(self._infer_capabilities_from_text(f"{relative} {first_line} {summary}")))
        skill_id = self._normalize_skill_id(relative_path)
        return SkillCard(
            skill_id=skill_id,
            title=first_line or skill_id,
            summary=summary,
            source=root.name or "skills",
            path=str(skill_path),
            tags=tags,
            capabilities=capabilities,
            internal=False,
        )

    def _normalize_skill_id(self, relative_path: Path) -> str:
        parts = [part for part in relative_path.parts if part]
        if "skills" in parts:
            skill_index = parts.index("skills")
            prefix = parts[:skill_index]
            suffix = parts[skill_index + 1 :]
            if prefix and len(prefix) >= 1 and suffix:
                plugin = prefix[0]
                return f"{plugin}:{':'.join(suffix)}"
            if suffix:
                return ":".join(suffix)
        return ":".join(parts)

    def _skill_summary(self, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        if not cleaned:
            return ""
        for sentence in re.split(r"(?<=[.!?。！？])\s+", cleaned):
            candidate = sentence.strip()
            if candidate and not candidate.startswith("#"):
                return candidate[:220]
        return cleaned[:220]

    def _semantic_capabilities_from_text(self, text: str) -> set[str]:
        lowered = text.lower()
        caps: set[str] = set()
        for capability, hints in CAPABILITY_SEMANTIC_ALIASES.items():
            for hint in hints:
                normalized = hint.lower().strip()
                if not normalized:
                    continue
                if normalized in lowered:
                    caps.add(capability)
                    break
        return caps

    def _task_haystack(self, task: str, subtasks: Iterable[Subtask]) -> str:
        parts = [task]
        for subtask in subtasks:
            parts.extend([
                subtask.title,
                subtask.goal,
                subtask.notes,
                " ".join(subtask.target_files),
                " ".join(subtask.acceptance_criteria),
            ])
        return " ".join(part for part in parts if part).lower()

    def _skill_domain(self, card: SkillCard) -> str:
        skill_id = card.skill_id.lower()
        if skill_id in STRICT_DOMAIN_HINTS:
            return skill_id
        head = skill_id.split(":", 1)[0]
        if head in STRICT_DOMAIN_HINTS:
            return head
        source = card.source.lower()
        if source in STRICT_DOMAIN_HINTS:
            return source
        return "generic"

    def _task_allows_domain(
        self,
        card: SkillCard,
        haystack: str,
        tokens: set[str],
    ) -> bool:
        domain = self._skill_domain(card)
        hints = STRICT_DOMAIN_HINTS.get(domain)
        if not hints:
            return True
        for hint in hints:
            normalized = hint.lower().strip()
            if not normalized:
                continue
            if " " in normalized or "-" in normalized:
                if normalized in haystack:
                    return True
                continue
            if normalized in tokens:
                return True
        return False

    def _demanded_capabilities(self, task: str, subtasks: list[Subtask]) -> set[str]:
        lowered = task.lower()
        haystack = self._task_haystack(task, subtasks)
        demand: set[str] = {"coordination", "review"}
        if len(subtasks) > 1:
            demand.add("planning")
        demand.update(self._semantic_capabilities_from_text(haystack))
        if any(self._is_codey(subtask) for subtask in subtasks) or any(keyword in lowered for keyword in CODE_KEYWORDS):
            demand.update({"architecture", "coding", "testing", "documentation"})
        if any(keyword in lowered for keyword in SECURITY_KEYWORDS):
            demand.add("security")
        if any(keyword in lowered for keyword in PERFORMANCE_KEYWORDS):
            demand.add("performance")
        if any(keyword in lowered for keyword in DOCS_KEYWORDS):
            demand.add("documentation")
        for subtask in subtasks:
            demand.update(self._subtask_capabilities(subtask, task))
        return demand

    def _subtask_capabilities(self, subtask: Subtask, task: str) -> set[str]:
        caps = set(subtask.preferred_capabilities)
        kind = subtask.kind
        if kind in CORE_CAPABILITY_ALIASES:
            caps.update(CORE_CAPABILITY_ALIASES[kind])
            caps.add(kind)
        if self._looks_sensitive(task, subtask):
            caps.add("security")
        if self._looks_perf_sensitive(task, subtask):
            caps.add("performance")
        if kind in {"coding", "architecture", "testing", "review"} and (
            "api" in task.lower() or "config" in task.lower() or "schema" in task.lower()
        ):
            caps.add("documentation")
        if subtask.parallel_group:
            caps.add("coordination")
        return caps

    def _build_doctrine(self, task: str, subtasks: list[Subtask], demand: set[str]) -> list[str]:
        doctrine = [
            "Leader-first routing before execution.",
            "Cost-aware local-first fallback for repetitive low-risk work.",
            "Memory feedback after every run.",
        ]
        if any(self._is_codey(subtask) for subtask in subtasks):
            doctrine.append("Architecture gate before risky code changes.")
            doctrine.append("Verification gate after implementation.")
        if any(subtask.parallel_group for subtask in subtasks):
            doctrine.append("Parallel file-scope packages must stay within ownership boundaries.")
        if "security" in demand:
            doctrine.append("Sensitive surfaces require a security guardrail review.")
        if "performance" in demand:
            doctrine.append("Performance-sensitive work requires latency or runtime validation.")
        if "documentation" in demand and any(self._is_codey(subtask) for subtask in subtasks):
            doctrine.append("User-visible or operator-visible changes should leave a handoff note.")
        return doctrine

    def _select_required_internal_skills(self, demand: set[str], doctrine: Iterable[str]) -> list[str]:
        skill_ids = [
            "mvp-core:leader-routing",
            "mvp-core:cost-aware-routing",
            "mvp-core:memory-feedback",
            "mvp-core:failure-reroute",
        ]
        if {"architecture", "coding", "testing"} & demand:
            skill_ids.extend([
                "mvp-core:repo-intel",
                "mvp-core:architecture-gate",
                "mvp-core:testing-regression-gate",
            ])
        if "security" in demand:
            skill_ids.append("mvp-core:security-guard")
        if "performance" in demand:
            skill_ids.append("mvp-core:performance-guard")
        if "documentation" in demand:
            skill_ids.append("mvp-core:docs-handoff")
        if any("Parallel" in item for item in doctrine):
            skill_ids.append("mvp-core:parallel-code-packaging")
        return _dedupe(skill_ids)

    def _select_external_skills(
        self,
        cards: list[SkillCard],
        demand: set[str],
        task: str,
        subtasks: list[Subtask],
    ) -> list[str]:
        if not cards:
            return []
        scored: list[tuple[float, str]] = []
        haystack = self._task_haystack(task, subtasks)
        tokens = _tokenize(haystack)
        high_signal = demand & {"security", "performance", "documentation"}
        critical_signal = demand & {"security", "performance"}
        for card in cards:
            if card.internal:
                continue
            if not self._task_allows_domain(card, haystack, tokens):
                continue
            score = 0.0
            overlap = demand & set(card.capabilities)
            token_overlap = len(tokens & set(card.tags))
            high_signal_overlap = len(high_signal & set(card.capabilities))
            critical_overlap = len(critical_signal & set(card.capabilities))
            if critical_signal and critical_overlap == 0:
                continue
            if high_signal and high_signal_overlap == 0 and token_overlap == 0:
                continue
            score += len(overlap) * 3.0
            score += token_overlap * 1.5
            score += high_signal_overlap * 2.0
            if overlap and card.capabilities:
                score += len(overlap) / len(card.capabilities)
            if any(self._is_codey(subtask) for subtask in subtasks) and "coding" in card.capabilities:
                score += 1.5
            if score >= 4.0:
                scored.append((score, card.skill_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [skill_id for _, skill_id in scored[:8]]

    def _recommended_external_for_subtask(
        self,
        cards: list[SkillCard],
        task: str,
        subtask: Subtask,
    ) -> list[str]:
        demand = self._subtask_capabilities(subtask, task)
        scored: list[tuple[float, str]] = []
        haystack = " ".join([
            task,
            subtask.title,
            subtask.goal,
            subtask.notes,
            " ".join(subtask.target_files),
            " ".join(subtask.acceptance_criteria),
        ]).lower()
        tokens = _tokenize(haystack)
        for card in cards:
            if card.internal:
                continue
            if not self._task_allows_domain(card, haystack, tokens):
                continue
            cap_overlap = len(demand & set(card.capabilities))
            token_overlap = len(tokens & set(card.tags))
            score = cap_overlap * 2.5
            score += token_overlap * 1.2
            if cap_overlap and card.capabilities:
                score += cap_overlap / len(card.capabilities)
            if score >= 4.0 and (cap_overlap > 0 or token_overlap > 1):
                scored.append((score, card.skill_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [skill_id for _, skill_id in scored[:4]]

    def _capability_gaps(self, demand: set[str], worker_specs: dict[str, WorkerSpec]) -> list[str]:
        covered: set[str] = set()
        for spec in worker_specs.values():
            if not spec.enabled:
                continue
            covered.update(spec.capabilities)
        gaps = []
        for capability in sorted(demand):
            if capability in {"security", "performance"}:
                if capability not in covered:
                    gaps.append(f"Missing strong {capability} coverage in the current worker mesh.")
                continue
            aliases = CORE_CAPABILITY_ALIASES.get(capability, (capability,))
            if capability in covered or any(alias in covered for alias in aliases):
                continue
            gaps.append(f"Missing strong {capability} coverage in the current worker mesh.")
        return gaps

    def _internal_skills_for_capability(self, capability: str) -> list[str]:
        result = []
        for card in INTERNAL_SKILLS:
            if capability in card.capabilities or capability in card.tags:
                result.append(card.skill_id)
        return result

    def _coordination_notes(self, subtask: Subtask) -> list[str]:
        notes: list[str] = []
        if subtask.kind in {"architecture", "design", "requirements"}:
            notes.append("Emit explicit boundaries, risks, and handoff notes for downstream workers.")
        if subtask.kind == "coding":
            notes.append("Stay inside the assigned file scope and call out interface changes early.")
        if subtask.parallel_group:
            notes.append("This subtask is part of a parallel package; avoid touching sibling scopes.")
        if subtask.kind in {"testing", "qa", "review"}:
            notes.append("Act as a gatekeeper: verify the output instead of re-implementing it.")
        if self._looks_sensitive("", subtask):
            notes.append("Check auth, secret handling, permissions, and unsafe input paths.")
        if self._looks_perf_sensitive("", subtask):
            notes.append("Check latency, runtime cost, and hot paths affected by this change.")
        return notes

    def _looks_sensitive(self, task: str, subtask: Subtask) -> bool:
        haystack = " ".join([task, subtask.title, subtask.goal, subtask.notes]).lower()
        return (
            any(keyword in haystack for keyword in SECURITY_KEYWORDS)
            or "security" in self._semantic_capabilities_from_text(haystack)
        )

    def _looks_perf_sensitive(self, task: str, subtask: Subtask) -> bool:
        haystack = " ".join([task, subtask.title, subtask.goal, subtask.notes]).lower()
        return (
            any(keyword in haystack for keyword in PERFORMANCE_KEYWORDS)
            or "performance" in self._semantic_capabilities_from_text(haystack)
        )

    def _is_codey(self, subtask: Subtask) -> bool:
        return subtask.kind in {"coding", "architecture", "testing", "review"} or subtask.needs_write_access

    def _infer_capabilities_from_text(self, text: str) -> set[str]:
        lowered = text.lower()
        caps: set[str] = set()
        caps.update(self._semantic_capabilities_from_text(text))
        for capability, aliases in CORE_CAPABILITY_ALIASES.items():
            if capability in lowered or any(alias in lowered for alias in aliases):
                caps.add(capability)
        if any(keyword in lowered for keyword in SECURITY_KEYWORDS):
            caps.add("security")
        if any(keyword in lowered for keyword in PERFORMANCE_KEYWORDS):
            caps.add("performance")
        if any(keyword in lowered for keyword in DOCS_KEYWORDS):
            caps.add("documentation")
        if any(keyword in lowered for keyword in CODE_KEYWORDS):
            caps.add("coding")
        return caps


def _dedupe(values: Iterable[str]) -> list[str]:
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


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Z0-9_:\-]{3,}", text.lower()))
