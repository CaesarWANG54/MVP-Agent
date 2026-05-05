from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mvp.agent import MvpAgent
from mvp.audit import append_worker_call, flush_audit, summarize_worker_metrics
from mvp.framework_history import read_framework_timeline
from mvp.models import Assignment, RunProfile, Subtask, WorkerResult, WorkerSpec
from mvp.orchestrator import MVPLoader
from mvp.planner import Planner
from mvp.review import Reviewer
from mvp.router import route_subtask
from mvp.worker_platform import describe_worker_platform
from mvp.workers.claude_cli import ClaudeCliWorker
from mvp.workers.extension_bridge_agent import ExtensionBridgeAgentWorker
from mvp.workers.external_cli_agent import ExternalCliAgentWorker
from mvp.workers.openclaw_agent import OpenClawAgentWorker
from mvp.workers.registry import WorkerRegistry
from mvp.workers.service_mesh_agent import ServiceMeshAgentWorker
from mvp.workers.codex_cli import CodexCliWorker


class MVPWorkerAndHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_codex_worker_writes_audit_to_configured_runs_dir(self) -> None:
        spec = WorkerSpec(
            worker_id="codex_architect",
            worker_type="codex_cli",
            display_name="Codex 架构师",
            role="test",
            capabilities=["planning"],
            cost_tier=1,
            quality_tier=1,
            speed_tier=1,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            config={
                "model": "gpt-5.4",
                "persist_sessions": False,
                "read_sandbox": "read-only",
                "write_sandbox": "workspace-write",
            },
        )
        worker = CodexCliWorker(spec, self.root, self.runs_dir)
        completed = subprocess.CompletedProcess(
            args=["codex", "exec"],
            returncode=0,
            stdout='{"status":"completed","summary":"ok","deliverable":"done","artifacts":[],"changes_made":[],"risks":[],"recommended_next_steps":[],"confidence":0.9}',
            stderr="",
        )
        with mock.patch("mvp.workers.codex_cli.run_subprocess_capture", return_value=completed):
            text = worker.invoke_text("hello", allow_write=True)
        self.assertIn('"status":"completed"', text)
        audit_path = self.runs_dir / "worker_calls.jsonl"
        self.assertTrue(audit_path.exists())
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[-1]["access_mode"], "workspace-write")

    def test_claude_worker_extracts_result_and_records_permission_mode(self) -> None:
        spec = WorkerSpec(
            worker_id="claude_builder",
            worker_type="claude_cli",
            display_name="Claude",
            role="test",
            capabilities=["coding"],
            cost_tier=1,
            quality_tier=1,
            speed_tier=1,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            config={
                "model": "deepseek-v4-pro[1m]",
                "read_permission_mode": "plan",
                "write_permission_mode": "acceptEdits",
                "base_url": "https://api.deepseek.com/anthropic",
            },
        )
        worker = ClaudeCliWorker(spec, self.root, self.runs_dir)
        completed = subprocess.CompletedProcess(
            args=["claude", "-p"],
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": '{"status":"completed","summary":"ok"}'}),
            stderr="",
        )
        with mock.patch("mvp.workers.claude_cli.run_subprocess_capture", return_value=completed):
            text = worker.invoke_text("hello", allow_write=True)
        self.assertIn('"status":"completed"', text)
        audit_path = self.runs_dir / "worker_calls.jsonl"
        self.assertTrue(audit_path.exists())
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[-1]["access_mode"], "claude:acceptEdits")

    def test_claude_worker_quick_mode_uses_quick_effort(self) -> None:
        spec = WorkerSpec(
            worker_id="claude_strategist",
            worker_type="claude_cli",
            display_name="Claude",
            role="test",
            capabilities=["planning"],
            cost_tier=1,
            quality_tier=1,
            speed_tier=1,
            local_only=False,
            api_cost=True,
            supports_workspace_write=False,
            config={
                "model": "deepseek-v4-pro[1m]",
                "quick_effort": "low",
                "deep_effort": "medium",
                "quick_timeout": 12,
                "deep_timeout": 34,
                "read_permission_mode": "plan",
                "base_url": "https://api.deepseek.com/anthropic",
            },
        )
        worker = ClaudeCliWorker(spec, self.root, self.runs_dir)
        completed = subprocess.CompletedProcess(
            args=["claude", "-p"],
            returncode=0,
            stdout=json.dumps({"is_error": False, "result": '{"status":"completed","summary":"ok"}'}),
            stderr="",
        )
        calls: list[dict[str, object]] = []

        def fake_run(command, cwd=None, input_text=None, timeout=None, run_control=None, env=None):
            del cwd, input_text, run_control
            calls.append({"command": command, "timeout": timeout, "env": env})
            return completed

        with mock.patch("mvp.workers.claude_cli.run_subprocess_capture", side_effect=fake_run):
            text = worker.invoke_text_mode("hello", mode="quick")
        self.assertIn('"status":"completed"', text)
        self.assertEqual(calls[0]["timeout"], 22)
        self.assertEqual(calls[0]["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "low")

    def test_openclaw_worker_quick_ping_uses_local_state_files(self) -> None:
        openclaw_root = self.root / ".openclaw"
        agent_dir = openclaw_root / "agents" / "main" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        config_path = openclaw_root / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": {"primary": "openai-codex/gpt-5.4"}
                        },
                        "list": [
                            {
                                "id": "main",
                                "agentDir": str(agent_dir),
                                "model": "openai-codex/gpt-5.4",
                            }
                        ],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (agent_dir / "auth-profiles.json").write_text(
            json.dumps(
                {
                    "profiles": {
                        "openai-codex:default": {
                            "type": "oauth",
                            "provider": "openai-codex",
                            "expires": 4070908800000,
                        }
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (agent_dir / "auth-state.json").write_text(
            json.dumps(
                {
                    "lastGood": {
                        "openai-codex": "openai-codex:default"
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        spec = WorkerSpec(
            worker_id="openclaw_coder",
            worker_type="openclaw_agent",
            display_name="OpenClaw",
            role="test",
            capabilities=["coding"],
            cost_tier=1,
            quality_tier=1,
            speed_tier=1,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            config={"agent": "main"},
        )
        worker = OpenClawAgentWorker(spec, self.root, self.runs_dir)
        with mock.patch.object(worker, "_resolve_openclaw_config_path", return_value=config_path), \
             mock.patch.object(worker, "_agent_status", side_effect=AssertionError("CLI fallback should not run")):
            payload = worker.quick_ping()
        self.assertTrue(payload["ok"])
        self.assertIn("openai-codex/gpt-5.4", payload["summary"])
        self.assertIn("local state", payload["summary"])
        audit_path = self.runs_dir / "worker_calls.jsonl"
        self.assertFalse(audit_path.exists())
        flush_audit(self.runs_dir)
        self.assertTrue(audit_path.exists())
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rows[-1]["stage"], "quick_ping")

    def test_framework_timeline_includes_message_level_rows(self) -> None:
        report_path = self.runs_dir / "mvp_run_20260504_090000.json"
        report_path.write_text(
            json.dumps(
                {
                    "task": "测试时间线",
                    "profile": "balanced",
                    "status": "completed",
                    "leader_notes": "测试通过",
                    "plan": {"summary": "summary", "execution_strategy": "strategy"},
                    "planned_assignments": [],
                    "outcomes": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.runs_dir / "worker_calls.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-05-04T09:00:01+08:00",
                    "worker_id": "codex_architect",
                    "framework": "Codex",
                    "target": "gpt-5.4",
                    "stage": "invoke",
                    "status": "completed",
                    "allow_write": False,
                    "duration_ms": 1200,
                    "summary": "worker ok",
                    "access_mode": "read-only",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        openclaw_dir = self.root / "openclaw"
        openclaw_dir.mkdir(parents=True, exist_ok=True)
        openclaw_session = openclaw_dir / "session.jsonl"
        openclaw_session.write_text(
            "\n".join(
                [
                    json.dumps({"type": "session", "id": "oc-1", "timestamp": "2026-05-04T01:00:00Z"}, ensure_ascii=False),
                    json.dumps(
                        {
                            "type": "message",
                            "timestamp": "2026-05-04T01:00:01Z",
                            "message": {"role": "user", "content": [{"type": "text", "text": "OpenClaw 用户消息"}]},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "timestamp": "2026-05-04T01:00:02Z",
                            "message": {
                                "role": "assistant",
                                "provider": "openai-codex",
                                "model": "gpt-5.4",
                                "content": [{"type": "text", "text": "OpenClaw 助手回复"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            encoding="utf-8",
        )
        openclaw_store = openclaw_dir / "sessions.json"
        openclaw_store.write_text(
            json.dumps(
                {
                    "agent:main:main": {
                        "sessionId": "oc-1",
                        "updatedAt": 1777856402000,
                        "lastChannel": "webchat",
                        "sessionFile": str(openclaw_session),
                        "skillsSnapshot": {"skills": ["a", "b"]},
                        "origin": {"label": "openclaw-tui"},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.runs_dir / "openclaw_session_stores.json").write_text(
            json.dumps({"paths": [str(openclaw_store)]}, ensure_ascii=False),
            encoding="utf-8",
        )

        temp_home = self.root / "home"
        codex_session_dir = temp_home / ".codex" / "sessions" / "2026" / "05" / "04"
        codex_session_dir.mkdir(parents=True, exist_ok=True)
        codex_session = codex_session_dir / "rollout-2026-05-04T09-00-00-test.jsonl"
        codex_session.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T01:00:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": "codex-session-1",
                                "timestamp": "2026-05-04T01:00:00Z",
                                "cwd": str(self.root),
                                "cli_version": "0.120.0",
                                "model_provider": "openai",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T01:00:01Z",
                            "type": "event_msg",
                            "payload": {"type": "user_message", "message": "Codex 用户消息"},
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T01:00:02Z",
                            "type": "event_msg",
                            "payload": {"type": "agent_message", "message": "Codex 助手回复", "phase": "final"},
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            encoding="utf-8",
        )

        with mock.patch("mvp.framework_history.Path.home", return_value=temp_home):
            rows = read_framework_timeline(self.root, self.runs_dir, limit=20)

        sources = {str(row.get("source")) for row in rows}
        kinds = {str(row.get("kind")) for row in rows}
        self.assertIn("OpenClaw消息", sources)
        self.assertIn("Codex消息", sources)
        self.assertIn("MVP", sources)
        self.assertIn("framework_message", kinds)

    def test_latency_penalty_prefers_faster_worker(self) -> None:
        subtask = Subtask(
            task_id="S1",
            title="写一段产品说明",
            goal="生成只读说明文字",
            kind="design",
            difficulty=1,
            repeatability=4,
            risk=1,
            needs_write_access=False,
            preferred_capabilities=["design", "documentation"],
            acceptance_criteria=["文字完整"],
        )
        profile = RunProfile(
            name="premium",
            planner_worker="codex_architect",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )
        worker_specs = {
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design", "coding", "review", "architecture"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            ),
            "hermes_designer": WorkerSpec(
                worker_id="hermes_designer",
                worker_type="openclaw_agent",
                display_name="Hermes",
                role="",
                capabilities=["planning", "design", "requirements", "review", "coordination", "documentation", "summarization"],
                cost_tier=3,
                quality_tier=5,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
        }
        worker_metrics = {
            "codex_architect": {"avg_duration_ms": 240000.0, "failure_ratio": 0.0},
            "hermes_designer": {"avg_duration_ms": 24000.0, "failure_ratio": 0.0},
        }
        assignment = route_subtask(subtask, worker_specs, profile, worker_metrics=worker_metrics)
        self.assertEqual(assignment.worker_id, "hermes_designer")

    def test_premium_short_task_prefers_fast_planner_first(self) -> None:
        worker_specs = {
            "ollama_planner": WorkerSpec(
                worker_id="ollama_planner",
                worker_type="ollama_api",
                display_name="Ollama",
                role="",
                capabilities=["planning"],
                cost_tier=1,
                quality_tier=2,
                speed_tier=5,
                local_only=True,
                api_cost=False,
                supports_workspace_write=False,
            ),
            "hermes_designer": WorkerSpec(
                worker_id="hermes_designer",
                worker_type="openclaw_agent",
                display_name="Hermes",
                role="",
                capabilities=["planning", "design"],
                cost_tier=3,
                quality_tier=5,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            ),
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="premium",
            planner_worker="hermes_designer",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )
        self.assertEqual(planner._planner_candidates("写一段介绍", profile, False)[0], "ollama_planner")

    def test_external_cli_planner_uses_agent_cli_modes(self) -> None:
        worker_specs = {
            "goose_planner": WorkerSpec(
                worker_id="goose_planner",
                worker_type="external_cli_agent",
                display_name="Goose Planner",
                role="",
                capabilities=["planning", "design"],
                cost_tier=3,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
                platform={"mode_family": "agent_cli"},
                config={"binary": "goose", "invoke_command": ["goose", "run"]},
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="balanced",
            planner_worker="goose_planner",
            reviewer_worker="goose_planner",
            prefer_local=False,
            cost_weight=0.9,
            quality_weight=2.0,
            speed_weight=1.0,
            local_bonus=0.3,
            simple_repeatable_bonus=0.6,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.75,
            max_subtasks_hint=4,
        )
        self.assertEqual(planner._planner_modes("goose_planner", profile, False), ["quick"])

    def test_balanced_planner_tries_quick_then_deep_for_remote_worker(self) -> None:
        worker_specs = {
            "claude_strategist": WorkerSpec(
                worker_id="claude_strategist",
                worker_type="claude_cli",
                display_name="Claude Strategist",
                role="",
                capabilities=["planning", "design", "architecture"],
                cost_tier=4,
                quality_tier=4,
                speed_tier=2,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="premium",
            planner_worker="claude_strategist",
            reviewer_worker="claude_strategist",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )

        class FakePlannerWorker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke_json_mode(self, prompt, allow_write=False, run_control=None, mode="default"):
                del prompt, allow_write, run_control
                self.calls.append(mode)
                if mode == "quick":
                    raise RuntimeError("quick timeout")
                return {
                    "summary": "Plan ready",
                    "execution_strategy": "remote staged planning",
                    "subtasks": [
                        {
                            "task_id": "S1",
                            "title": "Plan",
                            "goal": "Map the work",
                            "kind": "planning",
                            "difficulty": 2,
                            "repeatability": 2,
                            "risk": 2,
                            "needs_write_access": False,
                            "preferred_capabilities": ["planning"],
                            "acceptance_criteria": ["Plan exists"],
                            "depends_on": [],
                            "target_files": [],
                            "validation_steps": [],
                            "parallel_group": "",
                            "notes": "",
                        }
                    ],
                }

        fake = FakePlannerWorker()
        registry.get = lambda worker_id: fake  # type: ignore[method-assign]
        plan = planner.create_plan("Plan a repository refactor.", profile)
        self.assertEqual(fake.calls[:2], ["quick", "deep"])
        self.assertEqual(plan.planner_worker, "claude_strategist")

    def test_short_readonly_task_uses_fast_path_plan(self) -> None:
        worker_specs = {
            "ollama_planner": WorkerSpec(
                worker_id="ollama_planner",
                worker_type="ollama_api",
                display_name="Ollama",
                role="",
                capabilities=["planning"],
                cost_tier=1,
                quality_tier=2,
                speed_tier=5,
                local_only=True,
                api_cost=False,
                supports_workspace_write=False,
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="cheap",
            planner_worker="ollama_planner",
            reviewer_worker="ollama_reviewer",
            prefer_local=True,
            cost_weight=2.8,
            quality_weight=1.0,
            speed_weight=1.2,
            local_bonus=2.4,
            simple_repeatable_bonus=3.0,
            escalate_difficulty=4,
            escalate_risk=4,
            latency_weight=1.8,
            max_subtasks_hint=2,
        )
        plan = planner.create_plan("请只用中文一句话确认已经优化完成。", profile)
        self.assertEqual(plan.planner_worker, "mvp_fast_path")
        self.assertEqual(len(plan.subtasks), 1)
        self.assertFalse(plan.subtasks[0].needs_write_access)

    def test_balanced_profile_prefers_remote_agent_planning_chain(self) -> None:
        subtask = Subtask(
            task_id="S1",
            title="规划认证模块重构",
            goal="拆分认证模块重构计划",
            kind="planning",
            difficulty=3,
            repeatability=2,
            risk=3,
            needs_write_access=False,
            preferred_capabilities=["planning", "architecture"],
            acceptance_criteria=["给出明确计划"],
        )
        profile = RunProfile(
            name="balanced",
            planner_worker="hermes_designer",
            reviewer_worker="claude_strategist",
            prefer_local=False,
            cost_weight=0.9,
            quality_weight=2.0,
            speed_weight=1.0,
            local_bonus=0.3,
            simple_repeatable_bonus=0.6,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.75,
            max_subtasks_hint=4,
        )
        worker_specs = {
            "hermes_designer": WorkerSpec(
                worker_id="hermes_designer",
                worker_type="openclaw_agent",
                display_name="Hermes",
                role="",
                capabilities=["planning", "design", "requirements", "architecture", "review", "coordination"],
                cost_tier=3,
                quality_tier=5,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "claude_strategist": WorkerSpec(
                worker_id="claude_strategist",
                worker_type="claude_cli",
                display_name="Claude Strategist",
                role="",
                capabilities=["planning", "design", "review", "documentation", "requirements", "architecture"],
                cost_tier=4,
                quality_tier=4,
                speed_tier=2,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "ollama_planner": WorkerSpec(
                worker_id="ollama_planner",
                worker_type="ollama_api",
                display_name="Ollama",
                role="",
                capabilities=["planning", "triage", "summarization"],
                cost_tier=1,
                quality_tier=2,
                speed_tier=5,
                local_only=True,
                api_cost=False,
                supports_workspace_write=False,
            ),
        }
        assignment = route_subtask(subtask, worker_specs, profile)
        self.assertEqual(assignment.worker_id, "hermes_designer")

    def test_balanced_code_review_prefers_claude_before_codex(self) -> None:
        worker_specs = {
            "claude_strategist": WorkerSpec(
                worker_id="claude_strategist",
                worker_type="claude_cli",
                display_name="Claude Strategist",
                role="",
                capabilities=["review", "architecture", "testing"],
                cost_tier=4,
                quality_tier=4,
                speed_tier=2,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "codex_auditor": WorkerSpec(
                worker_id="codex_auditor",
                worker_type="codex_cli",
                display_name="Codex Auditor",
                role="",
                capabilities=["review", "qa", "testing"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        reviewer = Reviewer(registry)
        profile = RunProfile(
            name="balanced",
            planner_worker="claude_strategist",
            reviewer_worker="claude_strategist",
            prefer_local=False,
            cost_weight=0.9,
            quality_weight=2.0,
            speed_weight=1.0,
            local_bonus=0.3,
            simple_repeatable_bonus=0.6,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.75,
            max_subtasks_hint=4,
        )
        subtask = Subtask(
            task_id="S1",
            title="Review auth patch",
            goal="Verify a code patch safely.",
            kind="testing",
            difficulty=3,
            repeatability=2,
            risk=3,
            needs_write_access=False,
            preferred_capabilities=["testing", "review"],
            acceptance_criteria=["State whether the patch is safe"],
            target_files=["src/auth.py"],
            validation_steps=["Review auth regression risk"],
        )
        result = WorkerResult(
            worker_id="openclaw_coder",
            status="completed",
            summary="Patched auth flow.",
            deliverable="Updated auth logic.",
            artifacts=[],
            changes_made=["src/auth.py"],
            risks=[],
            recommended_next_steps=[],
            confidence=0.8,
            raw_response="",
        )

        class FakeReviewer:
            def __init__(self, response=None) -> None:
                self.calls: list[str] = []
                self.response = response or {
                    "decision": "pass",
                    "summary": "Looks safe",
                    "findings": [],
                    "confidence": 0.8,
                }

            def invoke_json_mode(self, prompt, allow_write=False, run_control=None, mode="default"):
                del prompt, allow_write, run_control
                self.calls.append(mode)
                return self.response

        claude = FakeReviewer()
        codex = FakeReviewer()
        registry.get = lambda worker_id: {"claude_strategist": claude, "codex_auditor": codex}[worker_id]  # type: ignore[method-assign]
        review = reviewer.review(subtask, result, profile)
        self.assertEqual(review.reviewer_worker, "claude_strategist")
        self.assertEqual(claude.calls, ["quick"])
        self.assertEqual(codex.calls, [])

    def test_complex_code_task_prefers_codex_planner(self) -> None:
        worker_specs = {
            "ollama_planner": WorkerSpec(
                worker_id="ollama_planner",
                worker_type="ollama_api",
                display_name="Ollama",
                role="",
                capabilities=["planning"],
                cost_tier=1,
                quality_tier=2,
                speed_tier=5,
                local_only=True,
                api_cost=False,
                supports_workspace_write=False,
            ),
            "hermes_designer": WorkerSpec(
                worker_id="hermes_designer",
                worker_type="openclaw_agent",
                display_name="Hermes",
                role="",
                capabilities=["planning", "design", "architecture"],
                cost_tier=3,
                quality_tier=5,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design", "coding", "architecture"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            ),
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="premium",
            planner_worker="hermes_designer",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )
        task = "Refactor a multi-file authentication flow and add regression-safe validation for the repository."
        self.assertEqual(planner._planner_candidates(task, profile, True)[0], "codex_architect")

    def test_code_plan_is_upgraded_into_architecture_implementation_and_testing(self) -> None:
        worker_specs = {
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design", "coding", "architecture"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="premium",
            planner_worker="codex_architect",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )

        class FakePlannerWorker:
            def invoke_json(self, prompt, allow_write=False, run_control=None):
                del prompt, allow_write, run_control
                return {
                    "summary": "Implement auth fix",
                    "execution_strategy": "single coding task",
                    "subtasks": [
                        {
                            "task_id": "S1",
                            "title": "Implement auth fix",
                            "goal": "Patch the authentication flow in code.",
                            "kind": "coding",
                            "difficulty": 4,
                            "repeatability": 2,
                            "risk": 4,
                            "needs_write_access": True,
                            "preferred_capabilities": ["coding"],
                            "acceptance_criteria": ["Behavior is fixed"],
                            "target_files": ["src/auth.py", "tests/test_auth.py"],
                            "validation_steps": ["Run auth regression checks"],
                            "notes": "Touches auth core.",
                        }
                    ],
                }

        registry.get = lambda worker_id: FakePlannerWorker()  # type: ignore[method-assign]
        plan = planner.create_plan("Fix the authentication bug in the repository and verify it safely.", profile)
        self.assertEqual([subtask.kind for subtask in plan.subtasks], ["architecture", "coding", "testing"])
        self.assertFalse(plan.subtasks[0].needs_write_access)
        self.assertTrue(plan.subtasks[1].needs_write_access)
        self.assertEqual(plan.subtasks[2].depends_on, ["S2"])
        self.assertIn("src/auth.py", plan.subtasks[0].target_files)
        self.assertIn("Run auth regression checks", plan.subtasks[2].validation_steps)

    def test_code_plan_creates_parallel_file_scope_packages(self) -> None:
        src_dir = self.root / "src"
        tests_dir = self.root / "tests"
        src_dir.mkdir(parents=True, exist_ok=True)
        tests_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "auth.py").write_text("def login(user):\n    return user\n", encoding="utf-8")
        (src_dir / "session.py").write_text("from src.auth import login\n\n\ndef create_session(user):\n    return login(user)\n", encoding="utf-8")
        (tests_dir / "test_auth.py").write_text("from src.auth import login\n\n\ndef test_login():\n    assert login('a') == 'a'\n", encoding="utf-8")
        (tests_dir / "test_session.py").write_text("from src.session import create_session\n\n\ndef test_session():\n    assert create_session('a') == 'a'\n", encoding="utf-8")

        worker_specs = {
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design", "coding", "architecture"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        planner = Planner(registry, worker_specs)
        profile = RunProfile(
            name="premium",
            planner_worker="codex_architect",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )

        class FakePlannerWorker:
            def invoke_json(self, prompt, allow_write=False, run_control=None):
                del prompt, allow_write, run_control
                return {
                    "summary": "Implement auth and session changes",
                    "execution_strategy": "single coding task",
                    "subtasks": [
                        {
                            "task_id": "S1",
                            "title": "Implement auth and session changes",
                            "goal": "Update auth and session flows safely.",
                            "kind": "coding",
                            "difficulty": 4,
                            "repeatability": 2,
                            "risk": 4,
                            "needs_write_access": True,
                            "preferred_capabilities": ["coding"],
                            "acceptance_criteria": ["Behavior is fixed"],
                            "target_files": [
                                "src/auth.py",
                                "src/session.py",
                                "tests/test_auth.py",
                                "tests/test_session.py",
                            ],
                            "validation_steps": [],
                            "notes": "",
                        }
                    ],
                }

        registry.get = lambda worker_id: FakePlannerWorker()  # type: ignore[method-assign]
        plan = planner.create_plan("Update auth and session behavior and keep regression safety.", profile)
        parallel_subtasks = [subtask for subtask in plan.subtasks if subtask.parallel_group]
        self.assertGreaterEqual(len(parallel_subtasks), 2)
        self.assertTrue(all(subtask.target_files for subtask in parallel_subtasks))
        self.assertTrue(any("Repository scope:" in subtask.notes for subtask in parallel_subtasks))

    def test_agent_builds_parallel_batches_for_disjoint_code_packages(self) -> None:
        config_path = self.root / "mvp.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        agent = MvpAgent(str(config_path))
        profile = agent.get_profile("premium")
        batch_plan = [
            Assignment(
                subtask=Subtask(
                    task_id="S1",
                    title="Auth package",
                    goal="Update auth",
                    kind="coding",
                    difficulty=3,
                    repeatability=2,
                    risk=3,
                    needs_write_access=True,
                    preferred_capabilities=["coding"],
                    acceptance_criteria=["done"],
                    target_files=["src/auth.py"],
                    parallel_group="parallel:s2",
                ),
                worker_id="codex_architect",
                score=9.0,
                rationale=["coding"],
            ),
            Assignment(
                subtask=Subtask(
                    task_id="S2",
                    title="Session package",
                    goal="Update session",
                    kind="coding",
                    difficulty=3,
                    repeatability=2,
                    risk=3,
                    needs_write_access=True,
                    preferred_capabilities=["coding"],
                    acceptance_criteria=["done"],
                    target_files=["src/session.py"],
                    parallel_group="parallel:s2",
                ),
                worker_id="claude_builder",
                score=8.0,
                rationale=["coding"],
            ),
            Assignment(
                subtask=Subtask(
                    task_id="S3",
                    title="Verify",
                    goal="Verify behavior",
                    kind="testing",
                    difficulty=2,
                    repeatability=3,
                    risk=2,
                    needs_write_access=False,
                    preferred_capabilities=["testing"],
                    acceptance_criteria=["checked"],
                    depends_on=["S1", "S2"],
                    target_files=["tests/test_auth.py", "tests/test_session.py"],
                ),
                worker_id="codex_auditor",
                score=7.0,
                rationale=["testing"],
            ),
        ]
        batches = agent._build_execution_batches(batch_plan, profile)
        self.assertEqual(len(batches), 2)
        self.assertEqual({item.subtask.task_id for item in batches[0]}, {"S1", "S2"})
        self.assertEqual([item.subtask.task_id for item in batches[1]], ["S3"])

    def test_agent_ping_mode_uses_worker_specific_paths(self) -> None:
        config_path = self.root / "mvp.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        agent = MvpAgent(str(config_path))
        quick_calls: list[str] = []
        deep_calls: list[str] = []
        enabled_specs = agent.registry.all_enabled_specs()
        for spec in enabled_specs:
            worker = agent.registry.get(spec.worker_id)

            def quick(worker_id=spec.worker_id):
                quick_calls.append(worker_id)
                return {"ok": True, "summary": "quick ok", "deliverable": worker_id, "latency_ms": 7, "ping_mode": "quick"}

            def deep(worker_id=spec.worker_id):
                deep_calls.append(worker_id)
                return {"ok": True, "summary": "deep ok", "deliverable": worker_id, "latency_ms": 11, "ping_mode": "deep"}

            worker.quick_ping = quick  # type: ignore[method-assign]
            worker.deep_ping = deep  # type: ignore[method-assign]

        quick_rows = agent.quick_ping()
        deep_rows = agent.deep_ping()
        self.assertEqual(len(quick_rows), len(enabled_specs))
        self.assertEqual(len(deep_rows), len(enabled_specs))
        self.assertEqual(set(quick_calls), {spec.worker_id for spec in enabled_specs})
        self.assertEqual(set(deep_calls), {spec.worker_id for spec in enabled_specs})

    def test_architecture_task_prefers_architecture_specialist(self) -> None:
        subtask = Subtask(
            task_id="S1",
            title="Map migration architecture",
            goal="Identify safe code touch points for a migration.",
            kind="architecture",
            difficulty=4,
            repeatability=2,
            risk=4,
            needs_write_access=False,
            preferred_capabilities=["architecture", "planning"],
            acceptance_criteria=["Names impacted modules"],
            target_files=["src/migrate.py"],
        )
        profile = RunProfile(
            name="premium",
            planner_worker="codex_architect",
            reviewer_worker="codex_auditor",
            prefer_local=False,
            cost_weight=0.5,
            quality_weight=2.4,
            speed_weight=0.8,
            local_bonus=0.5,
            simple_repeatable_bonus=0.7,
            escalate_difficulty=3,
            escalate_risk=3,
            latency_weight=0.85,
            max_subtasks_hint=4,
        )
        worker_specs = {
            "openclaw_coder": WorkerSpec(
                worker_id="openclaw_coder",
                worker_type="openclaw_agent",
                display_name="OpenClaw",
                role="",
                capabilities=["coding", "review"],
                cost_tier=3,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
            ),
            "codex_architect": WorkerSpec(
                worker_id="codex_architect",
                worker_type="codex_cli",
                display_name="Codex",
                role="",
                capabilities=["planning", "design", "coding", "architecture"],
                cost_tier=4,
                quality_tier=5,
                speed_tier=1,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
            ),
        }
        assignment = route_subtask(subtask, worker_specs, profile)
        self.assertEqual(assignment.worker_id, "codex_architect")


    def test_worker_metrics_ignore_ping_rows(self) -> None:
        audit_path = self.runs_dir / "worker_calls.jsonl"
        audit_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T09:00:01+08:00",
                            "worker_id": "ollama_planner",
                            "stage": "quick_ping",
                            "status": "completed",
                            "duration_ms": 25,
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T09:00:02+08:00",
                            "worker_id": "ollama_planner",
                            "stage": "invoke",
                            "status": "completed",
                            "duration_ms": 2300,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        metrics = summarize_worker_metrics(self.runs_dir, limit=10)
        self.assertIn("ollama_planner", metrics)
        self.assertEqual(metrics["ollama_planner"]["avg_duration_ms"], 2300.0)

    def test_worker_metrics_include_invoke_mode_rows(self) -> None:
        audit_path = self.runs_dir / "worker_calls.jsonl"
        audit_path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T09:00:01+08:00",
                            "worker_id": "claude_strategist",
                            "stage": "invoke:quick",
                            "status": "failed",
                            "duration_ms": 1200,
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T09:00:02+08:00",
                            "worker_id": "claude_strategist",
                            "stage": "invoke:deep",
                            "status": "completed",
                            "duration_ms": 2400,
                        }
                    ),
                    json.dumps(
                        {
                            "timestamp": "2026-05-04T09:00:03+08:00",
                            "worker_id": "claude_strategist",
                            "stage": "quick_ping",
                            "status": "completed",
                            "duration_ms": 20,
                        }
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        metrics = summarize_worker_metrics(self.runs_dir, limit=10)
        self.assertIn("claude_strategist", metrics)
        self.assertEqual(metrics["claude_strategist"]["avg_duration_ms"], 1800.0)

    def test_ping_mode_uses_worker_specific_paths(self) -> None:
        config_path = self.root / "mvp.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loader = MVPLoader(str(config_path))
        quick_calls: list[str] = []
        deep_calls: list[str] = []
        enabled_specs = loader.registry.all_enabled_specs()
        for spec in enabled_specs:
            worker = loader.registry.get(spec.worker_id)

            def quick(worker_id=spec.worker_id):
                quick_calls.append(worker_id)
                return {"ok": True, "summary": "quick ok", "deliverable": worker_id, "latency_ms": 7, "ping_mode": "quick"}

            def deep(worker_id=spec.worker_id):
                deep_calls.append(worker_id)
                return {"ok": True, "summary": "deep ok", "deliverable": worker_id, "latency_ms": 11, "ping_mode": "deep"}

            worker.quick_ping = quick  # type: ignore[method-assign]
            worker.deep_ping = deep  # type: ignore[method-assign]

        quick_rows = loader.quick_ping()
        deep_rows = loader.deep_ping()
        self.assertEqual(len(quick_rows), len(enabled_specs))
        self.assertEqual(len(deep_rows), len(enabled_specs))
        self.assertEqual(set(quick_calls), {spec.worker_id for spec in enabled_specs})
        self.assertEqual(set(deep_calls), {spec.worker_id for spec in enabled_specs})
        self.assertTrue(all(row["ping_mode"] == "quick" for row in quick_rows))
        self.assertTrue(all(row["ping_mode"] == "deep" for row in deep_rows))

    def test_loader_quick_ping_flushes_buffered_audit_rows(self) -> None:
        config_path = self.root / "mvp.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loader = MVPLoader(str(config_path))
        enabled_specs = loader.registry.all_enabled_specs()
        for spec in enabled_specs:
            worker = loader.registry.get(spec.worker_id)

            def quick(worker_id=spec.worker_id):
                append_worker_call(
                    self.runs_dir,
                    {
                        "worker_id": worker_id,
                        "framework": "test",
                        "target": worker_id,
                        "stage": "quick_ping",
                        "status": "completed",
                        "allow_write": False,
                        "duration_ms": 1,
                        "summary": "quick ok",
                        "access_mode": "test",
                    },
                    buffered=True,
                )
                return {"ok": True, "summary": "quick ok", "deliverable": worker_id, "latency_ms": 1, "ping_mode": "quick"}

            worker.quick_ping = quick  # type: ignore[method-assign]

        loader.quick_ping()
        audit_path = self.runs_dir / "worker_calls.jsonl"
        self.assertTrue(audit_path.exists())
        rows = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(rows), len(enabled_specs))
        self.assertTrue(all(row["stage"] == "quick_ping" for row in rows))

    def test_loader_quick_ping_reuses_short_ttl_cache(self) -> None:
        config_path = self.root / "mvp.config.json"
        config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        loader = MVPLoader(str(config_path))
        counts: dict[str, int] = {}
        enabled_specs = loader.registry.all_enabled_specs()
        for spec in enabled_specs:
            worker = loader.registry.get(spec.worker_id)

            def quick(worker_id=spec.worker_id):
                counts[worker_id] = counts.get(worker_id, 0) + 1
                return {"ok": True, "summary": "quick ok", "deliverable": worker_id, "latency_ms": 1, "ping_mode": "quick"}

            worker.quick_ping = quick  # type: ignore[method-assign]

        first = loader.quick_ping()
        second = loader.quick_ping()
        self.assertEqual(len(first), len(enabled_specs))
        self.assertEqual(len(second), len(enabled_specs))
        self.assertEqual(counts, {spec.worker_id: 1 for spec in enabled_specs})

    def test_registry_supports_claude_cli_workers(self) -> None:
        worker_specs = {
            "claude_strategist": WorkerSpec(
                worker_id="claude_strategist",
                worker_type="claude_cli",
                display_name="Claude",
                role="",
                capabilities=["planning"],
                cost_tier=4,
                quality_tier=4,
                speed_tier=2,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
                config={"model": "deepseek-v4-pro[1m]"},
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        worker = registry.get("claude_strategist")
        self.assertIsInstance(worker, ClaudeCliWorker)

    def test_registry_supports_external_cli_workers(self) -> None:
        worker_specs = {
            "goose_planner": WorkerSpec(
                worker_id="goose_planner",
                worker_type="external_cli_agent",
                display_name="Goose Planner",
                role="",
                capabilities=["planning", "design"],
                cost_tier=3,
                quality_tier=3,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=False,
                platform={
                    "framework_label": "Goose",
                    "backend_label": "Goose CLI",
                    "target_label": "goose/planner",
                },
                config={
                    "binary": "goose",
                    "invoke_command": ["goose", "run", "--json", "--mode", "{mode}"],
                    "response_parser": "json_field",
                    "response_text_field": "result",
                },
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        worker = registry.get("goose_planner")
        self.assertIsInstance(worker, ExternalCliAgentWorker)

    def test_registry_supports_extension_bridge_workers(self) -> None:
        worker_specs = {
            "cline_bridge": WorkerSpec(
                worker_id="cline_bridge",
                worker_type="extension_bridge_agent",
                display_name="Cline Bridge",
                role="",
                capabilities=["planning", "coding"],
                cost_tier=3,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
                config={
                    "binary": "cline-bridge",
                    "bridge_command": ["cline-bridge", "run", "--mode", "{mode}"],
                },
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        worker = registry.get("cline_bridge")
        self.assertIsInstance(worker, ExtensionBridgeAgentWorker)

    def test_registry_supports_service_mesh_workers(self) -> None:
        worker_specs = {
            "crewai_mesh": WorkerSpec(
                worker_id="crewai_mesh",
                worker_type="service_mesh_agent",
                display_name="CrewAI Mesh",
                role="",
                capabilities=["planning", "coding"],
                cost_tier=3,
                quality_tier=4,
                speed_tier=3,
                local_only=False,
                api_cost=True,
                supports_workspace_write=True,
                config={
                    "base_url": "http://127.0.0.1:8787",
                    "invoke_path": "/invoke",
                },
            )
        }
        registry = WorkerRegistry(worker_specs, self.root, self.runs_dir)
        worker = registry.get("crewai_mesh")
        self.assertIsInstance(worker, ServiceMeshAgentWorker)

    def test_external_cli_worker_invokes_config_driven_command(self) -> None:
        spec = WorkerSpec(
            worker_id="goose_planner",
            worker_type="external_cli_agent",
            display_name="Goose Planner",
            role="test",
            capabilities=["planning"],
            cost_tier=2,
            quality_tier=3,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            platform={
                "framework_label": "Goose",
                "backend_label": "Goose CLI",
                "target_label": "goose/planner",
            },
            config={
                "binary": "goose",
                "invoke_command": ["goose", "run", "--mode", "{mode}", "--access", "{write_mode}"],
                "quick_ping_command": ["goose", "status", "--json"],
                "health_command": ["goose", "--version"],
                "response_parser": "json_field",
                "response_text_field": "result",
                "prompt_transport": "stdin",
            },
        )
        worker = ExternalCliAgentWorker(spec, self.root, self.runs_dir)
        completed = subprocess.CompletedProcess(
            args=["goose", "run"],
            returncode=0,
            stdout=json.dumps({"result": '{"status":"completed","summary":"ok"}'}),
            stderr="",
        )
        calls: list[dict[str, object]] = []

        def fake_run(command, cwd=None, input_text=None, timeout=None, run_control=None, env=None):
            del cwd, run_control, env
            calls.append({"command": command, "input_text": input_text, "timeout": timeout})
            return completed

        with mock.patch("mvp.workers.external_cli_agent.run_subprocess_capture", side_effect=fake_run):
            text = worker.invoke_text_mode("hello world", allow_write=True, mode="quick")
        self.assertIn('"status":"completed"', text)
        self.assertEqual(calls[0]["command"], ["goose", "run", "--mode", "quick", "--access", "write"])
        self.assertEqual(calls[0]["input_text"], "hello world")

    def test_extension_bridge_worker_uses_bridge_command(self) -> None:
        spec = WorkerSpec(
            worker_id="cline_bridge",
            worker_type="extension_bridge_agent",
            display_name="Cline Bridge",
            role="test",
            capabilities=["coding"],
            cost_tier=2,
            quality_tier=3,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            platform={
                "framework_label": "Cline",
                "backend_label": "IDE Extension Bridge",
                "target_label": "cline/default",
                "mode_family": "extension_bridge",
            },
            config={
                "binary": "cline-bridge",
                "bridge_command": ["cline-bridge", "run", "--mode", "{mode}", "--access", "{write_mode}"],
                "quick_ping_command": ["cline-bridge", "ping", "--quick"],
                "response_parser": "json_field",
                "response_text_field": "result",
                "prompt_transport": "stdin",
            },
        )
        worker = ExtensionBridgeAgentWorker(spec, self.root, self.runs_dir)
        completed = subprocess.CompletedProcess(
            args=["cline-bridge", "run"],
            returncode=0,
            stdout=json.dumps({"result": '{"status":"completed","summary":"bridge ok"}'}),
            stderr="",
        )
        calls: list[dict[str, object]] = []

        def fake_run(command, cwd=None, input_text=None, timeout=None, run_control=None, env=None):
            del cwd, run_control, env
            calls.append({"command": command, "input_text": input_text, "timeout": timeout})
            return completed

        with mock.patch("mvp.workers.external_cli_agent.run_subprocess_capture", side_effect=fake_run):
            text = worker.invoke_text_mode("bridge this task", allow_write=True, mode="deep")
        self.assertIn('"status":"completed"', text)
        self.assertEqual(calls[0]["command"], ["cline-bridge", "run", "--mode", "deep", "--access", "write"])
        self.assertEqual(calls[0]["input_text"], "bridge this task")

    def test_service_mesh_worker_uses_http_roundtrips(self) -> None:
        spec = WorkerSpec(
            worker_id="crewai_mesh",
            worker_type="service_mesh_agent",
            display_name="CrewAI Mesh",
            role="test",
            capabilities=["planning", "coding"],
            cost_tier=2,
            quality_tier=4,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            platform={
                "framework_label": "CrewAI",
                "backend_label": "Agent Service API",
                "mode_family": "service_mesh",
            },
            config={
                "base_url": "http://127.0.0.1:8787",
                "invoke_path": "/invoke",
                "health_path": "/health",
                "quick_ping_path": "/ping/quick",
                "deep_ping_path": "/ping/deep",
                "response_parser": "json_field",
                "response_text_field": "result",
                "request_template": {
                    "prompt": "{prompt}",
                    "mode": "{mode}",
                    "allow_write": "{allow_write}",
                    "workspace_root": "{workspace_root}",
                },
                "auth_env": "MVP_MESH_TOKEN",
            },
        )
        worker = ServiceMeshAgentWorker(spec, self.root, self.runs_dir)
        requests: list[dict[str, object]] = []

        class FakeResponse:
            def __init__(self, body: str, status: int = 200) -> None:
                self._body = body.encode("utf-8")
                self.status = status

            def read(self) -> bytes:
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                del exc_type, exc, tb
                return False

        def fake_urlopen(req, timeout=None):
            requests.append(
                {
                    "url": req.full_url,
                    "method": req.get_method(),
                    "body": req.data.decode("utf-8") if req.data else "",
                    "headers": dict(req.header_items()),
                    "timeout": timeout,
                }
            )
            if req.full_url.endswith("/health"):
                return FakeResponse("mesh healthy")
            if req.full_url.endswith("/ping/quick"):
                return FakeResponse(json.dumps({"result": "quick mesh ok"}))
            if req.full_url.endswith("/ping/deep"):
                return FakeResponse(json.dumps({"summary": "deep mesh ok", "deliverable": "mesh ready"}))
            if req.full_url.endswith("/invoke"):
                return FakeResponse(json.dumps({"result": '{"status":"completed","summary":"mesh ok","deliverable":"done"}'}))
            raise AssertionError(f"Unexpected URL: {req.full_url}")

        with mock.patch.dict("os.environ", {"MVP_MESH_TOKEN": "secret-token"}, clear=False):
            with mock.patch("mvp.workers.service_mesh_agent.urlrequest.urlopen", side_effect=fake_urlopen):
                health = worker.health_check()
                quick = worker.quick_ping()
                deep = worker.deep_ping()
                text = worker.invoke_text_mode("Mesh task", allow_write=True, mode="deep")

        self.assertTrue(health.ok)
        self.assertEqual(health.summary, "mesh healthy")
        self.assertTrue(quick["ok"])
        self.assertEqual(quick["summary"], "quick mesh ok")
        self.assertTrue(deep["ok"])
        self.assertEqual(deep["deliverable"], "mesh ready")
        self.assertIn('"status":"completed"', text)

        invoke_request = next(item for item in requests if str(item["url"]).endswith("/invoke"))
        invoke_payload = json.loads(str(invoke_request["body"]))
        self.assertEqual(invoke_request["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(invoke_payload["mode"], "deep")
        self.assertEqual(invoke_payload["allow_write"], "true")
        self.assertEqual(invoke_payload["workspace_root"], str(self.root))

    def test_describe_worker_platform_honors_external_platform_metadata(self) -> None:
        spec = WorkerSpec(
            worker_id="aider_builder",
            worker_type="external_cli_agent",
            display_name="Aider Builder",
            role="test",
            capabilities=["coding"],
            cost_tier=3,
            quality_tier=4,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            platform={
                "framework_label": "Aider",
                "backend_label": "Aider CLI",
                "target_label": "deepseek-coder",
                "security_label": "read=repo / write=repo",
                "security_detail": "Config-driven external agent test.",
                "mode_family": "agent_cli",
            },
            config={"binary": "aider", "invoke_command": ["aider", "--message", "{prompt}"]},
        )
        payload = describe_worker_platform(spec)
        self.assertEqual(payload["framework_label"], "Aider")
        self.assertEqual(payload["backend_label"], "Aider CLI")
        self.assertEqual(payload["target_label"], "deepseek-coder")
        self.assertEqual(payload["security_label"], "read=repo / write=repo")

    def test_describe_worker_platform_supports_new_transport_defaults(self) -> None:
        bridge_spec = WorkerSpec(
            worker_id="cline_bridge",
            worker_type="extension_bridge_agent",
            display_name="Cline Bridge",
            role="test",
            capabilities=["coding"],
            cost_tier=3,
            quality_tier=3,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=True,
            config={
                "binary": "cline-bridge",
                "bridge_command": ["cline-bridge", "run", "--mode", "{mode}"],
            },
        )
        mesh_spec = WorkerSpec(
            worker_id="crewai_mesh",
            worker_type="service_mesh_agent",
            display_name="CrewAI Mesh",
            role="test",
            capabilities=["planning"],
            cost_tier=3,
            quality_tier=3,
            speed_tier=3,
            local_only=False,
            api_cost=True,
            supports_workspace_write=False,
            config={
                "base_url": "http://127.0.0.1:8787",
                "invoke_path": "/invoke",
            },
        )
        bridge_payload = describe_worker_platform(bridge_spec)
        mesh_payload = describe_worker_platform(mesh_spec)
        self.assertEqual(bridge_payload["framework_label"], "Extension Bridge")
        self.assertEqual(bridge_payload["target_label"], "cline-bridge")
        self.assertEqual(mesh_payload["framework_label"], "Service Mesh")
        self.assertEqual(mesh_payload["target_label"], "http://127.0.0.1:8787")


if __name__ == "__main__":
    unittest.main()
