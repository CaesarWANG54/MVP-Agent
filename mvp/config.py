from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import RunProfile, WorkerSpec
from .utils import deep_merge, load_json_file


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "mvp.config.json"
RUNS_DIR = REPO_ROOT / "mvp_runs"


def default_config() -> dict[str, Any]:
    return {
        "workspace_root": str(REPO_ROOT),
        "runs_dir": str(RUNS_DIR),
        "skills": {
            "enabled": True,
            "discover_roots": [
                str(REPO_ROOT / "skills"),
                str(Path.home() / ".codex" / "skills"),
                str(Path.home() / ".codex" / "plugins" / "cache" / "openai-curated"),
            ],
            "max_cards": 256,
        },
        "pet": {
            "enabled": True,
            "selected_pet_id": "mvp-agent-pet",
            "lock_to_default": True,
            "default_package_dir": str(REPO_ROOT / "mvp" / "assets" / "pets" / "mvp-agent-pet"),
            "discover_roots": [
                str(REPO_ROOT / "mvp" / "assets" / "pets"),
            ],
        },
        "memory": {
            "db_path": str(RUNS_DIR / "mvp_memory.db"),
            "max_history": 5000,
            "enabled": True,
        },
        "profiles": {
            "cheap": {
                "planner_worker": "ollama_planner",
                "reviewer_worker": "ollama_reviewer",
                "prefer_local": True,
                "cost_weight": 2.8,
                "quality_weight": 1.0,
                "speed_weight": 1.2,
                "local_bonus": 2.4,
                "simple_repeatable_bonus": 3.0,
                "escalate_difficulty": 4,
                "escalate_risk": 4,
                "latency_weight": 1.8,
                "max_subtasks_hint": 2,
            },
            "balanced": {
                "planner_worker": "claude_strategist",
                "reviewer_worker": "claude_strategist",
                "prefer_local": False,
                "cost_weight": 0.9,
                "quality_weight": 2.0,
                "speed_weight": 1.0,
                "local_bonus": 0.3,
                "simple_repeatable_bonus": 0.6,
                "escalate_difficulty": 3,
                "escalate_risk": 3,
                "latency_weight": 0.75,
                "max_subtasks_hint": 4,
            },
            "premium": {
                "planner_worker": "hermes_designer",
                "reviewer_worker": "codex_auditor",
                "prefer_local": False,
                "cost_weight": 0.5,
                "quality_weight": 2.4,
                "speed_weight": 0.8,
                "local_bonus": 0.5,
                "simple_repeatable_bonus": 0.7,
                "escalate_difficulty": 3,
                "escalate_risk": 3,
                "latency_weight": 0.85,
                "max_subtasks_hint": 4,
            },
        },
        "workers": [
            {
                "worker_id": "hermes_designer",
                "worker_type": "openclaw_agent",
                "display_name": "Hermes 设计师",
                "role": "负责设计、规格整理、验收标准与高层规划。",
                "capabilities": ["planning", "design", "requirements", "architecture", "review", "coordination", "documentation", "summarization"],
                "cost_tier": 3,
                "quality_tier": 5,
                "speed_tier": 3,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": False,
                "config": {
                    "agent": "hermes-wingman",
                    "thinking": "low",
                    "quick_thinking": "low",
                    "deep_thinking": "medium",
                    "timeout": 480,
                    "quick_timeout": 24,
                    "deep_timeout": 90,
                    "expected_model": "openai-codex/gpt-5.4",
                },
            },
            {
                "worker_id": "openclaw_coder",
                "worker_type": "openclaw_agent",
                "display_name": "OpenClaw 工程师",
                "role": "负责 OpenClaw 侧执行、编码与多技能调用。",
                "capabilities": ["coding", "planning", "testing", "review", "research", "documentation", "summarization"],
                "cost_tier": 3,
                "quality_tier": 4,
                "speed_tier": 3,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": True,
                "config": {
                    "agent": "main",
                    "thinking": "low",
                    "quick_thinking": "low",
                    "deep_thinking": "medium",
                    "timeout": 480,
                    "quick_timeout": 24,
                    "deep_timeout": 90,
                    "expected_model": "openai-codex/gpt-5.4",
                },
            },
            {
                "worker_id": "ollama_planner",
                "worker_type": "ollama_api",
                "display_name": "Ollama 规划器",
                "role": "负责低成本、本地优先的轻量拆解与归纳。",
                "capabilities": ["planning", "triage", "summarization", "documentation", "coordination"],
                "cost_tier": 1,
                "quality_tier": 2,
                "speed_tier": 5,
                "local_only": True,
                "api_cost": False,
                "supports_workspace_write": False,
                "config": {
                    "model": "qwen3.5:9b",
                    "temperature": 0.2,
                    "think": False,
                    "timeout": 240,
                    "quick_timeout": 20,
                    "quick_num_predict": 48,
                    "deep_think": False,
                    "deep_timeout": 45,
                    "deep_num_predict": 512,
                },
            },
            {
                "worker_id": "ollama_reviewer",
                "worker_type": "ollama_api",
                "display_name": "Ollama 审核器",
                "role": "负责首轮复查、QA 与重复性检查。",
                "capabilities": ["review", "qa", "testing", "triage", "summarization", "documentation"],
                "cost_tier": 1,
                "quality_tier": 3,
                "speed_tier": 5,
                "local_only": True,
                "api_cost": False,
                "supports_workspace_write": False,
                "config": {
                    "model": "qwen3.5:9b",
                    "temperature": 0.1,
                    "think": False,
                    "timeout": 240,
                    "quick_timeout": 20,
                    "quick_num_predict": 32,
                    "deep_think": False,
                    "deep_timeout": 45,
                    "deep_num_predict": 256,
                },
            },
            {
                "worker_id": "ollama_heavy_local",
                "worker_type": "ollama_api",
                "display_name": "Ollama 重型本地模型",
                "role": "负责更重一些的本地推理与代码理解。",
                "capabilities": ["coding", "testing", "review", "planning"],
                "cost_tier": 2,
                "quality_tier": 3,
                "speed_tier": 3,
                "local_only": True,
                "api_cost": False,
                "supports_workspace_write": False,
                "config": {
                    "model": "qwen3-coder:30b",
                    "temperature": 0.1,
                    "think": False,
                    "timeout": 420,
                    "quick_timeout": 25,
                    "quick_num_predict": 32,
                    "deep_think": False,
                    "deep_timeout": 60,
                    "deep_num_predict": 320,
                },
            },
            {
                "worker_id": "claude_strategist",
                "worker_type": "claude_cli",
                "display_name": "Claude Strategist",
                "role": "Handles Claude Code / DeepSeek planning, design review, documentation, and high-level analysis.",
                "capabilities": ["planning", "design", "review", "documentation", "summarization", "requirements", "architecture"],
                "cost_tier": 4,
                "quality_tier": 4,
                "speed_tier": 2,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": False,
                "config": {
                    "model": "deepseek-v4-pro[1m]",
                    "effort": "max",
                    "quick_effort": "low",
                    "deep_effort": "medium",
                    "timeout": 480,
                    "quick_timeout": 28,
                    "deep_timeout": 105,
                    "read_permission_mode": "plan",
                    "write_permission_mode": "plan",
                    "base_url": "https://api.deepseek.com/anthropic",
                    "subagent_model": "deepseek-v4-flash",
                },
            },
            {
                "worker_id": "claude_builder",
                "worker_type": "claude_cli",
                "display_name": "Claude Builder",
                "role": "Handles Claude Code / DeepSeek coding, edits, testing, and implementation-ready documentation.",
                "capabilities": ["coding", "testing", "review", "documentation", "summarization", "planning"],
                "cost_tier": 4,
                "quality_tier": 4,
                "speed_tier": 2,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": True,
                "config": {
                    "model": "deepseek-v4-pro[1m]",
                    "effort": "max",
                    "quick_effort": "low",
                    "deep_effort": "medium",
                    "timeout": 600,
                    "quick_timeout": 32,
                    "deep_timeout": 120,
                    "read_permission_mode": "plan",
                    "write_permission_mode": "acceptEdits",
                    "base_url": "https://api.deepseek.com/anthropic",
                    "subagent_model": "deepseek-v4-flash",
                },
            },
            {
                "worker_id": "codex_architect",
                "worker_type": "codex_cli",
                "display_name": "Codex 架构师",
                "role": "负责复杂任务、架构判断与高难执行。",
                "capabilities": ["planning", "design", "coding", "testing", "review", "architecture"],
                "cost_tier": 4,
                "quality_tier": 5,
                "speed_tier": 1,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": True,
                "config": {
                    "model": "gpt-5.4",
                    "timeout": 1200,
                    "quick_timeout": 90,
                    "deep_timeout": 180,
                    "persist_sessions": True,
                    "read_sandbox": "read-only",
                    "write_sandbox": "workspace-write",
                },
            },
            {
                "worker_id": "codex_auditor",
                "worker_type": "codex_cli",
                "display_name": "Codex 终审官",
                "role": "负责严格验收、终审与风险把关。",
                "capabilities": ["review", "qa", "testing", "architecture", "security"],
                "cost_tier": 4,
                "quality_tier": 5,
                "speed_tier": 1,
                "local_only": False,
                "api_cost": True,
                "supports_workspace_write": False,
                "config": {
                    "model": "gpt-5.4",
                    "timeout": 1200,
                    "quick_timeout": 90,
                    "deep_timeout": 180,
                    "persist_sessions": True,
                    "read_sandbox": "read-only",
                    "write_sandbox": "read-only",
                },
            },
        ],
    }


def load_config(path: str | None = None) -> dict[str, Any]:
    config = default_config()
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if config_path.exists():
        config = deep_merge(config, load_json_file(config_path))
    return config


def build_worker_specs(config: dict[str, Any]) -> dict[str, WorkerSpec]:
    workers: dict[str, WorkerSpec] = {}
    for raw in config.get("workers", []):
        upgraded = dict(raw)
        upgraded["capabilities"] = _augment_worker_capabilities(
            str(raw.get("worker_id", "")),
            raw.get("capabilities", []),
        )
        spec = WorkerSpec(**upgraded)
        workers[spec.worker_id] = spec
    return workers


def build_profiles(config: dict[str, Any]) -> dict[str, RunProfile]:
    profiles: dict[str, RunProfile] = {}
    for name, raw in config.get("profiles", {}).items():
        profile = RunProfile(name=name, **raw)
        profiles[name] = profile
    return profiles


def _augment_worker_capabilities(worker_id: str, capabilities: Any) -> list[str]:
    existing = [str(item).strip() for item in (capabilities or []) if str(item).strip()]
    boosted = {
        "hermes_designer": ["coordination", "requirements", "security", "performance"],
        "openclaw_coder": ["qa", "coordination", "performance"],
        "ollama_planner": ["review", "requirements", "memory"],
        "ollama_reviewer": ["security", "performance", "coordination"],
        "ollama_heavy_local": ["performance", "architecture", "qa"],
        "claude_strategist": ["coordination", "security", "performance", "memory"],
        "claude_builder": ["coordination", "security", "performance", "qa"],
        "codex_architect": ["coordination", "security", "performance", "documentation", "memory"],
        "codex_auditor": ["coordination", "performance", "documentation", "memory"],
    }
    extras = boosted.get(worker_id, [])
    merged: list[str] = []
    seen: set[str] = set()
    for item in [*existing, *extras]:
        lowered = item.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        merged.append(item)
    return merged
