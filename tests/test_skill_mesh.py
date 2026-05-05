from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mvp.memory import MemoryStore
from mvp.models import RunProfile, Subtask, TaskPlan, WorkerSpec
from mvp.planner import Planner
from mvp.skill_mesh import SkillMesh, capabilities_for_skill_ids
from mvp.workers.registry import WorkerRegistry


class SkillMeshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.skills_root = self.root / "skills"
        self.plugin_root = self.root / "openai-curated"
        (self.skills_root / "security-audit").mkdir(parents=True, exist_ok=True)
        (self.skills_root / "security-audit" / "SKILL.md").write_text(
            "# Security Audit\nReview secrets, auth, and unsafe input handling for code changes.\n",
            encoding="utf-8",
        )
        (self.skills_root / "perf-profiler").mkdir(parents=True, exist_ok=True)
        (self.skills_root / "perf-profiler" / "SKILL.md").write_text(
            "# Perf Profiler\nAnalyze latency, hot paths, and performance bottlenecks in runtime flows.\n",
            encoding="utf-8",
        )
        (self.skills_root / "hatch-pet").mkdir(parents=True, exist_ok=True)
        (self.skills_root / "hatch-pet" / "SKILL.md").write_text(
            "# Hatch Pet\nBuild animated mascot sprites, icons, and pet status visuals for desktop agents.\n",
            encoding="utf-8",
        )
        (self.plugin_root / "game-studio" / "cache" / "skills" / "web-game-foundations").mkdir(parents=True, exist_ok=True)
        (self.plugin_root / "game-studio" / "cache" / "skills" / "web-game-foundations" / "SKILL.md").write_text(
            "# Web Game Foundations\nPlan browser game architecture and optimize frame-time performance for gameplay loops.\n",
            encoding="utf-8",
        )
        (self.plugin_root / "test-android-apps" / "cache" / "skills" / "android-performance").mkdir(parents=True, exist_ok=True)
        (self.plugin_root / "test-android-apps" / "cache" / "skills" / "android-performance" / "SKILL.md").write_text(
            "# Android Performance\nProfile mobile latency, frame pacing, and runtime bottlenecks on Android builds.\n",
            encoding="utf-8",
        )
        (self.plugin_root / "github" / "cache" / "skills" / "gh-address-comments").mkdir(parents=True, exist_ok=True)
        (self.plugin_root / "github" / "cache" / "skills" / "gh-address-comments" / "SKILL.md").write_text(
            "# Address PR Comments\nInspect GitHub pull request review threads and resolve requested changes.\n",
            encoding="utf-8",
        )
        (self.plugin_root / ".system" / "cache" / "skills" / "skill-creator").mkdir(parents=True, exist_ok=True)
        (self.plugin_root / ".system" / "cache" / "skills" / "skill-creator" / "SKILL.md").write_text(
            "# Skill Creator\nCreate and package a reusable Codex skill for a workflow or domain.\n",
            encoding="utf-8",
        )
        (self.plugin_root / ".system" / "cache" / "skills" / "openai-docs").mkdir(parents=True, exist_ok=True)
        (self.plugin_root / ".system" / "cache" / "skills" / "openai-docs" / "SKILL.md").write_text(
            "# OpenAI Docs\nRead official OpenAI API docs and model guidance.\n",
            encoding="utf-8",
        )
        (self.skills_root / "windows-multi-agent-orchestrator").mkdir(parents=True, exist_ok=True)
        (self.skills_root / "windows-multi-agent-orchestrator" / "SKILL.md").write_text(
            "# Windows Multi Agent Orchestrator\nCoordinate multiple agent frameworks through a Windows control plane.\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_skill_mesh_discovers_external_skills(self) -> None:
        mesh = SkillMesh([self.skills_root, self.plugin_root], max_cards=64)
        cards = mesh.discover()
        skill_ids = {card.skill_id for card in cards}
        self.assertIn("security-audit", skill_ids)
        self.assertIn("perf-profiler", skill_ids)
        self.assertIn("mvp-core:leader-routing", skill_ids)
        self.assertIn("game-studio:web-game-foundations", skill_ids)

    def test_profile_task_recommends_security_and_performance_skills(self) -> None:
        mesh = SkillMesh([self.skills_root, self.plugin_root], max_cards=64)
        subtasks = [
            Subtask(
                task_id="S1",
                title="Patch auth middleware",
                goal="Fix auth token handling and reduce latency in the API middleware.",
                kind="coding",
                difficulty=4,
                repeatability=2,
                risk=4,
                needs_write_access=True,
                preferred_capabilities=["coding"],
                acceptance_criteria=["Ship the fix safely."],
                target_files=["app/auth.py"],
            )
        ]
        worker_specs = {
            "builder": WorkerSpec(
                worker_id="builder",
                worker_type="codex_cli",
                display_name="Builder",
                role="coding",
                capabilities=["coding", "testing", "review"],
                cost_tier=2,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            )
        }
        profile = mesh.profile_task(
            "Fix auth token leak in the API middleware and optimize latency.",
            subtasks,
            worker_specs,
        )
        self.assertIn("mvp-core:security-guard", profile.required_skill_ids)
        self.assertIn("mvp-core:performance-guard", profile.required_skill_ids)
        self.assertTrue(any("security" in skill_id for skill_id in profile.recommended_skill_ids))
        self.assertTrue(any("perf" in skill_id for skill_id in profile.recommended_skill_ids))
        self.assertTrue(any("security" in gap.lower() for gap in profile.capability_gaps))
        self.assertTrue(any("performance" in gap.lower() for gap in profile.capability_gaps))
        self.assertFalse(any(skill_id.startswith("game-studio:") for skill_id in profile.recommended_skill_ids))
        self.assertFalse(any(skill_id.startswith("test-android-apps:") for skill_id in profile.recommended_skill_ids))
        self.assertFalse(any(skill_id == "hatch-pet" for skill_id in profile.recommended_skill_ids))
        self.assertFalse(any(skill_id.startswith(".system:") for skill_id in profile.recommended_skill_ids))
        self.assertFalse(any(skill_id == "windows-multi-agent-orchestrator" for skill_id in profile.recommended_skill_ids))

    def test_semantic_aliases_detect_security_and_performance_from_natural_language(self) -> None:
        mesh = SkillMesh([self.skills_root, self.plugin_root], max_cards=64)
        subtasks = [
            Subtask(
                task_id="S1",
                title="Stabilize login flow",
                goal="Users lose access because sessions expire early and responses feel sluggish under load.",
                kind="coding",
                difficulty=4,
                repeatability=2,
                risk=4,
                needs_write_access=True,
                preferred_capabilities=["coding"],
                acceptance_criteria=["Session handling is stable and responses recover under load."],
                target_files=["app/session.py", "app/api.py"],
            )
        ]
        worker_specs = {
            "builder": WorkerSpec(
                worker_id="builder",
                worker_type="codex_cli",
                display_name="Builder",
                role="coding",
                capabilities=["coding", "testing", "review"],
                cost_tier=2,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            )
        }
        profile = mesh.profile_task(
            "Login sessions expire early, users lose access, and API responses feel sluggish under load.",
            subtasks,
            worker_specs,
        )
        self.assertIn("mvp-core:security-guard", profile.required_skill_ids)
        self.assertIn("mvp-core:performance-guard", profile.required_skill_ids)
        self.assertTrue(any("security" in skill_id for skill_id in profile.recommended_skill_ids))
        self.assertTrue(any("perf" in skill_id for skill_id in profile.recommended_skill_ids))
        self.assertFalse(any(skill_id.startswith(".system:") for skill_id in profile.recommended_skill_ids))

    def test_capabilities_for_skill_ids_expands_core_skill_bias(self) -> None:
        caps = capabilities_for_skill_ids([
            "mvp-core:security-guard",
            "mvp-core:performance-guard",
        ])
        self.assertIn("security", caps)
        self.assertIn("review", caps)
        self.assertIn("performance", caps)
        self.assertIn("testing", caps)

    def test_planner_augments_plan_with_skill_guardrails(self) -> None:
        worker_specs = {
            "planner": WorkerSpec(
                worker_id="planner",
                worker_type="ollama_api",
                display_name="Planner",
                role="planning",
                capabilities=["planning", "coordination"],
                cost_tier=1,
                quality_tier=2,
                speed_tier=4,
                local_only=True,
                api_cost=False,
                supports_workspace_write=False,
            ),
            "builder": WorkerSpec(
                worker_id="builder",
                worker_type="codex_cli",
                display_name="Builder",
                role="coding",
                capabilities=["coding", "testing", "review"],
                cost_tier=3,
                quality_tier=4,
                speed_tier=2,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            ),
        }
        registry = WorkerRegistry(worker_specs, self.root, self.root / "runs")
        planner = Planner(registry, worker_specs, skill_mesh=SkillMesh([self.skills_root], max_cards=32))
        plan = TaskPlan(
            summary="Fix auth and latency issue",
            execution_strategy="implement directly",
            subtasks=[
                Subtask(
                    task_id="S1",
                    title="Implement middleware patch",
                    goal="Fix auth token handling and optimize latency in API middleware.",
                    kind="coding",
                    difficulty=4,
                    repeatability=2,
                    risk=4,
                    needs_write_access=True,
                    preferred_capabilities=["coding"],
                    acceptance_criteria=["Patch is implemented."],
                    target_files=["app/auth.py", "app/api.py"],
                )
            ],
            planner_worker="planner",
            raw_response="{}",
        )
        upgraded = planner._augment_plan_with_skills(
            "Fix auth token leak in the API middleware and optimize latency.",
            plan,
        )
        titles = {item.title for item in upgraded.subtasks}
        self.assertIn("Run security guardrail review", titles)
        self.assertIn("Run performance and efficiency review", titles)
        self.assertIn("Write concise handoff notes", titles)
        self.assertIn("mvp-core:security-guard", upgraded.required_skills)
        self.assertTrue(upgraded.doctrine)


class MemorySkillLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = MemoryStore(self.root / "memory.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_memory_store_remembers_skill_profiles_and_gap_counts(self) -> None:
        self.store.remember_skill_profile(
            task="Fix auth token leak",
            required_skills=["mvp-core:security-guard"],
            capability_gaps=["Missing strong security coverage in the current worker mesh."],
            success=True,
        )
        self.store.remember_skill_profile(
            task="Fix auth token leak again",
            required_skills=["mvp-core:security-guard"],
            capability_gaps=["Missing strong security coverage in the current worker mesh."],
            success=False,
        )
        recalled = self.store.recall_skill_profile("Fix auth token leak")
        self.assertIsNotNone(recalled)
        assert recalled is not None
        self.assertIn("mvp-core:security-guard", recalled["required_skills"])
        gaps = self.store.top_capability_gaps()
        self.assertEqual(gaps[0]["count"], 2)
        self.assertIn("security", gaps[0]["gap"].lower())


if __name__ == "__main__":
    unittest.main()
