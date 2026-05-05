from __future__ import annotations

import gc
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from mvp.app import MVPDesktopApp, main as app_main
from mvp.utils import RunCancelled


class MVPGuiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.config_path = self.root / "mvp.config.json"
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(
                {
                    "workspace_root": str(self.root),
                    "runs_dir": str(self.runs_dir),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        gc.collect()
        try:
            self.temp_dir.cleanup()
        except PermissionError:
            shutil.rmtree(self.root, ignore_errors=True)

    def test_plan_only_report_renders_assignments(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            payload = {
                "task": "测试任务",
                "profile": "balanced",
                "status": "planned",
                "leader_notes": "仅规划",
                "report_path": "",
                "plan": {
                    "summary": "拆分一项任务",
                    "execution_strategy": "先规划后执行",
                },
                "planned_assignments": [
                    {
                        "worker_id": "ollama_planner",
                        "score": 12.3,
                        "rationale": ["local bonus"],
                        "subtask": {
                            "task_id": "S1",
                            "title": "规划子任务",
                            "goal": "完成拆分",
                            "kind": "planning",
                            "difficulty": 2,
                            "repeatability": 4,
                            "risk": 1,
                            "needs_write_access": False,
                            "preferred_capabilities": ["planning"],
                            "acceptance_criteria": ["有清晰路由"],
                            "notes": "",
                        },
                    }
                ],
                "outcomes": [],
            }
            app._apply_report(payload)
            self.assertEqual(len(app.assign_tree.get_children()), 1)
        finally:
            app.destroy()

    def test_control_plane_panels_render_workspace_and_architecture(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            workspace_text = app.home_workspace_preview.get("1.0", "end")
            architecture_text = app.home_architecture.get("1.0", "end")
            self.assertIn("工作区：", workspace_text)
            self.assertIn("Planner -> Router -> Worker Mesh -> Reviewer -> Reroute", architecture_text)
        finally:
            app.destroy()

    def test_workspace_panels_accept_tree_and_diff_snapshot(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app._apply_workspace({"tree": "repo/\n├─ mvp/", "diff": "分支：main\n\n工作区干净，没有未提交改动。"})
            self.assertIn("repo/", app.home_file_tree.get("1.0", "end"))
            self.assertIn("工作区干净", app.home_diff.get("1.0", "end"))
        finally:
            app.destroy()

    def test_report_updates_home_and_team_summaries(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            payload = {
                "task": "测试任务",
                "profile": "balanced",
                "status": "completed",
                "leader_notes": "全部通过",
                "report_path": "",
                "plan": {
                    "summary": "拆分后执行",
                    "execution_strategy": "先规划后执行",
                },
                "planned_assignments": [],
                "outcomes": [],
                "performance": {
                    "planning_ms": 10,
                    "execution_ms": 20,
                    "review_ms": 30,
                    "total_ms": 60,
                },
            }
            app._apply_report(payload)
            home_text = app.home_summary.get("1.0", "end")
            team_text = app.team_summary.get("1.0", "end")
            self.assertIn("模式：均衡", home_text)
            self.assertIn("领导结论：全部通过", home_text)
            self.assertIn("模式：均衡", team_text)
            self.assertIn("领导结论：全部通过", team_text)
        finally:
            app.destroy()

    def test_report_renders_skill_mesh_panel(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            payload = {
                "task": "修复认证令牌泄漏并优化接口延迟",
                "profile": "balanced",
                "status": "completed",
                "leader_notes": "skill mesh 已完成装配",
                "report_path": "",
                "plan": {
                    "summary": "先修复认证，再补性能验收",
                    "execution_strategy": "先规划后执行",
                    "required_skills": [
                        "mvp-core:leader-routing",
                        "mvp-core:security-guard",
                        "mvp-core:performance-guard",
                    ],
                    "doctrine": [
                        "Leader-first routing before execution.",
                        "Sensitive surfaces require a security guardrail review.",
                    ],
                    "capability_gaps": [
                        "Missing strong security coverage in the current worker mesh.",
                    ],
                    "subtasks": [
                        {
                            "task_id": "S1",
                            "title": "修复认证中间件",
                            "goal": "修复 token 泄漏并降低延迟",
                            "kind": "coding",
                            "required_skills": [
                                "mvp-core:security-guard",
                                "security-audit",
                            ],
                        }
                    ],
                },
                "planned_assignments": [],
                "outcomes": [],
                "performance": {
                    "planning_ms": 10,
                    "execution_ms": 20,
                    "review_ms": 30,
                    "total_ms": 60,
                },
            }
            app._apply_report(payload)
            mesh_text = app.home_skill_mesh.get("1.0", "end")
            self.assertIn("核心 skills", mesh_text)
            self.assertIn("mvp-core:security-guard", mesh_text)
            self.assertIn("security-audit", mesh_text)
            self.assertIn("安全能力覆盖", mesh_text)
        finally:
            app.destroy()

    def test_run_event_updates_live_stream_and_timeline(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app.current_job_id = "J001"
            payload = {
                "job_id": "J001",
                "event": {
                    "type": "assignment_started",
                    "message": "正在执行子任务 1/2：实现主界面",
                    "task_id": "S1",
                    "worker_id": "codex_architect",
                    "progress": 0.5,
                },
            }
            app._run_event(payload)
            self.assertEqual(len(app.timeline_tree.get_children()), 1)
            self.assertIn("正在执行子任务", app.live_stream.get("1.0", "end"))
            self.assertIn("开始执行", app.timeline_detail.get("1.0", "end"))
        finally:
            app.destroy()

    def test_assignment_stream_event_renders_code_payload(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app.current_job_id = "J002"
            payload = {
                "job_id": "J002",
                "event": {
                    "type": "assignment_stream",
                    "message": "认证模块代码回包 1/1",
                    "task_id": "S2",
                    "worker_id": "codex_architect",
                    "stream_text": "文件预览：src/auth.py\n```python\nprint('ok')\n```",
                    "target_files": ["src/auth.py"],
                    "progress": 0.65,
                },
            }
            app._run_event(payload)
            self.assertIn("代码回包", app.live_stream.get("1.0", "end"))
            self.assertIn("src/auth.py", app.timeline_detail.get("1.0", "end"))
            self.assertIn("print('ok')", app.timeline_detail.get("1.0", "end"))
        finally:
            app.destroy()

    def test_output_code_block_gets_highlight_tags(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app._append_output("leader", "已生成代码：\n```python\nprint('ok')\n```")
            self.assertIn("print('ok')", app.output_text.get("1.0", "end"))
            self.assertTrue(app.output_text.tag_ranges("leader_code"))
            self.assertTrue(app.output_text.tag_ranges("leader_code_lang"))
            self.assertGreaterEqual(len(app.output_embeds), 1)
        finally:
            app.destroy()

    def test_gui_self_test_entrypoint(self) -> None:
        self.assertEqual(app_main(["--config", str(self.config_path), "--self-test"]), 0)

    def test_queue_and_cancel_flow(self) -> None:
        class FakeReport:
            def __init__(self, task: str, plan_only: bool) -> None:
                self.task = task
                self.plan_only = plan_only

            def to_dict(self) -> dict[str, object]:
                return {
                    "task": self.task,
                    "profile": "balanced",
                    "status": "planned" if self.plan_only else "completed",
                    "leader_notes": "fake done",
                    "report_path": "",
                    "plan": {"summary": "fake summary", "execution_strategy": "fake strategy"},
                    "planned_assignments": [],
                    "outcomes": [],
                }

        app = MVPDesktopApp(str(self.config_path), autostart=False)
        calls: list[tuple[str, bool]] = []

        def fake_run(task: str, profile_name: str, plan_only: bool = False, run_control=None, event_callback=None):
            del profile_name
            calls.append((task, plan_only))
            if event_callback:
                event_callback({"type": "planning_completed", "progress": 0.2, "message": "fake planning", "assignments": []})
            if "取消" in task:
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    if run_control is not None and run_control.is_cancelled():
                        raise RunCancelled("测试取消成功")
                    time.sleep(0.02)
                raise AssertionError("cancel was not requested in time")
            time.sleep(0.05)
            return FakeReport(task, plan_only)

        try:
            app.loader.run = fake_run  # type: ignore[method-assign]

            app.task_text.delete("1.0", "end")
            app.task_text.insert("1.0", "第一个任务")
            app.run_task_async()
            app.task_text.delete("1.0", "end")
            app.task_text.insert("1.0", "第二个任务")
            app.plan_task_async()

            deadline = time.time() + 3.0
            while time.time() < deadline and (app.job_running or app.pending_job_ids):
                app._poll_queue()
                app.update_idletasks()
                time.sleep(0.03)

            self.assertEqual(calls[:2], [("第一个任务", False), ("第二个任务", True)])
            self.assertFalse(app.job_running)
            self.assertFalse(app.pending_job_ids)

            app.task_text.delete("1.0", "end")
            app.task_text.insert("1.0", "取消测试任务")
            app.run_task_async()
            cancel_deadline = time.time() + 0.5
            while time.time() < cancel_deadline and not app.job_running:
                app._poll_queue()
                time.sleep(0.02)
            app.cancel_current_task()

            deadline = time.time() + 3.0
            while time.time() < deadline and app.job_running:
                app._poll_queue()
                app.update_idletasks()
                time.sleep(0.03)

            cancelled_jobs = [job for job in app.jobs.values() if "取消测试任务" in job.task]
            self.assertTrue(cancelled_jobs)
            self.assertEqual(cancelled_jobs[-1].status, "cancelled")
        finally:
            app.destroy()

    def test_ping_buttons_dispatch_modes(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        calls: list[tuple[str, str]] = []

        def fake_bg(name, func, result_type):
            calls.append((name, result_type))
            func()

        try:
            app.loader.quick_ping = lambda: []  # type: ignore[method-assign]
            app.loader.deep_ping = lambda: []  # type: ignore[method-assign]
            app._bg = fake_bg  # type: ignore[method-assign]
            app.run_quick_ping_async()
            app.run_deep_ping_async()
            self.assertEqual(calls[0][1], "ping")
            self.assertEqual(calls[1][1], "ping")
        finally:
            app.destroy()

    def test_sidebar_toggle_hides_and_restores_status_bar(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app.update_idletasks()
            self.assertTrue(app.sidebar_expanded)
            self.assertEqual(app.sidebar_shell.winfo_manager(), "grid")

            app._toggle_sidebar()
            app.update_idletasks()
            self.assertFalse(app.sidebar_expanded)
            self.assertEqual(app.sidebar_shell.winfo_manager(), "")
            self.assertIn("展开", app.sidebar_toggle_var.get())

            app._toggle_sidebar()
            app.update_idletasks()
            self.assertTrue(app.sidebar_expanded)
            self.assertEqual(app.sidebar_shell.winfo_manager(), "grid")
            self.assertIn("收起", app.sidebar_toggle_var.get())
        finally:
            app.destroy()

    def test_input_panel_stays_fixed_below_conversation(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            self.assertEqual(int(app.chat_card.grid_info()["row"]), 0)
            self.assertEqual(int(app.detail_card.grid_info()["row"]), 1)
            self.assertEqual(int(app.composer_card.grid_info()["row"]), 2)
        finally:
            app.destroy()

    def test_task_placeholder_and_reader_ignore_placeholder_text(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            self.assertTrue(app.task_placeholder_visible)
            self.assertEqual(app._get_task_value(), "")
            self.assertIn("例如：", app.task_text.get("1.0", "end"))

            app._clear_task_placeholder()
            app.task_text.insert("1.0", "重构认证模块")
            self.assertEqual(app._get_task_value(), "重构认证模块")

            app.task_text.delete("1.0", "end")
            app._restore_placeholder_if_empty()
            self.assertTrue(app.task_placeholder_visible)
            self.assertEqual(app._get_task_value(), "")
        finally:
            app.destroy()

    def test_sidebar_width_can_be_resized(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app._set_sidebar_width(520)
            app.update_idletasks()
            self.assertEqual(app.sidebar_width, 520)
            self.assertEqual(int(app.sidebar_shell.cget("width")), 520)

            app._set_sidebar_width(120)
            self.assertEqual(app.sidebar_width, 280)
        finally:
            app.destroy()

    def test_pet_panel_loads_and_lists_available_pet(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            self.assertIsNotNone(app.current_pet_package)
            self.assertTrue(app.pet_name_var.get())
            self.assertEqual(len(app.pet_choice_combo.cget("values")), 1)
            self.assertEqual(str(app.pet_choice_combo.cget("state")), "disabled")
            self.assertIn("MVP", app.pet_name_var.get())
        finally:
            app.destroy()

    def test_pet_status_reacts_to_run_events(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            app.current_job_id = "J-PET"
            app._run_event({"job_id": "J-PET", "event": {"type": "planning_started", "message": "开始规划任务"}})
            self.assertIn("规划", app.pet_status_var.get())

            app._run_event({"job_id": "J-PET", "event": {"type": "assignment_started", "message": "开始执行子任务"}})
            self.assertIn("执行", app.pet_status_var.get())

            app._apply_report({"job_id": "J-PET", "report": {"status": "completed", "leader_notes": "任务已经完成", "plan": {}, "profile": "balanced", "outcomes": []}})
            self.assertIn("完成", app.pet_status_var.get())
            self.assertIn("任务已经完成", app.pet_message_var.get())
        finally:
            app.destroy()

    def test_sidebar_section_state_persists_across_sessions(self) -> None:
        app = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            self.assertTrue(app.sidebar_section_state["queue"])
            app._toggle_sidebar_section("queue")
        finally:
            app.destroy()

        reopened = MVPDesktopApp(str(self.config_path), autostart=False)
        try:
            self.assertFalse(reopened.sidebar_section_state["queue"])
            self.assertEqual(reopened.sidebar_section_bodies["queue"].winfo_manager(), "")
        finally:
            reopened.destroy()


if __name__ == "__main__":
    unittest.main()
