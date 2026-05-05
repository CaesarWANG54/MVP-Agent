from __future__ import annotations

import argparse
import json
import os
import re
import threading
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable
import tkinter as tk
from tkinter import BooleanVar, StringVar, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from PIL import ImageTk

from .config import DEFAULT_CONFIG_PATH
from .agent import MvpAgent
from .orchestrator import MVPLoader
from .pet_runtime import (
    MOODS,
    PetMood,
    PetPackage,
    animation_durations,
    discover_pet_packages,
    extract_animation_frames,
    load_pet_package,
    make_brand_icon,
    make_thumbnail,
    mood_from_event,
    mood_from_report_status,
)
from .utils import (
    RunCancelled, RunControl, detect_budget_mode, iso_now_local,
    load_json_file, safe_snippet, save_json_file,
)
from .workspace_snapshot import collect_workspace_snapshot


@dataclass(slots=True)
class QueueJob:
    job_id: str
    task: str
    mode: str
    plan_only: bool
    status: str = "queued"
    created_at: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = iso_now_local()


class CollapsiblePane(tk.Frame):
    """A reusable toggle-able section with header button and collapsible body."""

    def __init__(self, parent: tk.Misc, title: str = "", initially_open: bool = False, **kw: Any) -> None:
        super().__init__(parent, **kw)
        self._show = initially_open
        self._header_text = title
        self._header = tk.Button(
            self,
            text=f"{'▼' if self._show else '▶'} {title}",
            anchor="w",
            relief="flat",
            bd=0,
            padx=10,
            pady=4,
            command=self.toggle,
        )
        self._header.grid(row=0, column=0, sticky="ew")
        self._body = tk.Frame(self)
        self.grid_columnconfigure(0, weight=1)
        if self._show:
            self._body.grid(row=1, column=0, sticky="ew", padx=(14, 0))

    def toggle(self) -> None:
        self._show = not self._show
        self._header.configure(text=f"{'▼' if self._show else '▶'} {self._header_text}")
        if self._show:
            self._body.grid(row=1, column=0, sticky="ew", padx=(14, 0))
        else:
            self._body.grid_remove()

    @property
    def body(self) -> tk.Frame:
        return self._body


class MVPDesktopApp(tk.Tk):
    POLL_MS = 140
    AUTO_STATS_MS = 15000
    AUTO_WORKSPACE_MS = 20000
    PROFILE_LABELS = {"省 API": "cheap", "均衡": "balanced", "高质量": "premium"}
    PROFILE_NAMES = {value: key for key, value in PROFILE_LABELS.items()}
    STATUS_LABELS = {
        "planned": "已规划", "completed": "已完成", "partial": "部分完成",
        "needs_revision": "待修订", "empty": "无结果", "cancelled": "已取消",
        "unknown": "未知",
    }
    RESULT_LABELS = {
        "completed": "完成", "partial": "部分完成", "failed": "失败",
        "error": "异常", "cancelled": "已取消", "planned": "待执行",
        "blocked": "阻塞", "unknown": "未知",
    }
    REVIEW_LABELS = {"pass": "通过", "revise": "需修改", "fail": "未通过"}
    KIND_LABELS = {
        "planning": "规划", "design": "设计", "requirements": "需求",
        "coding": "编码", "review": "审核", "coordination": "协调",
        "research": "研究", "documentation": "文档", "testing": "测试",
        "ops": "运维", "triage": "分诊", "summarization": "总结",
        "qa": "质检", "architecture": "架构", "security": "安全",
        "performance": "性能",
    }
    CORE_SKILL_LABELS = {
        "mvp-core:leader-routing": "领导路由",
        "mvp-core:repo-intel": "仓库情报",
        "mvp-core:architecture-gate": "架构闸门",
        "mvp-core:parallel-code-packaging": "并行分包",
        "mvp-core:testing-regression-gate": "回归验收",
        "mvp-core:security-guard": "安全守门",
        "mvp-core:performance-guard": "性能守门",
        "mvp-core:docs-handoff": "文档交接",
        "mvp-core:cost-aware-routing": "成本感知路由",
        "mvp-core:memory-feedback": "记忆反馈",
        "mvp-core:failure-reroute": "失败改派工",
    }
    CAPABILITY_LABELS = {
        "architecture": "架构",
        "coding": "编码",
        "coordination": "协调",
        "documentation": "文档",
        "performance": "性能",
        "planning": "规划",
        "qa": "质检",
        "research": "研究",
        "review": "验收",
        "security": "安全",
        "testing": "测试",
    }
    OUTPUT_MAX_LINES = 800

    def __init__(self, config_path: str | None = None, autostart: bool = True) -> None:
        super().__init__()
        self.loader = MvpAgent(config_path)
        self.assets_dir = Path(__file__).resolve().parent / "assets"
        self._after_ids: set[str] = set()
        self._shutting_down = False
        self.colors = {
            "bg": "#F5F3F0", "panel": "#FAF9F7", "panel_alt": "#F0EDE8",
            "line": "#D8D0C8", "text": "#1F1A17", "muted": "#8C8279",
            "accent": "#C41528", "accent_dark": "#941020", "accent_soft": "#F8EAEC",
            "good": "#1F7A56", "bad": "#AD2E24", "input": "#FFFDFC",
        }
        self.fonts = {
            "base": "{Microsoft YaHei UI} 10",
            "bold": "{Microsoft YaHei UI} 10 bold",
            "title": "{Microsoft YaHei UI} 22 bold",
            "section": "{Microsoft YaHei UI} 11 bold",
            "small": "{Microsoft YaHei UI} 9",
            "mono": "{Cascadia Mono} 10",
        }
        self.profile_var = StringVar(value="均衡")
        self.hint_var = BooleanVar(value=True)
        self.status_var = StringVar(value="就绪")
        self.connection_var = StringVar(value="未检查")
        self.smoke_var = StringVar(value="未检测")
        self.progress_var = StringVar(value="等待任务")
        self.last_report_var = StringVar(value="")
        self.hardware_mode_var = StringVar(value="建议：均衡")
        self.queue_state_var = StringVar(value="队列：0 个待执行")
        self.header_worker_var = StringVar(value="成员：未检查")

        self.queue: Queue[tuple[str, object]] = Queue()
        self.job_running = False
        self.current_job_id: str | None = None
        self.current_run_control: RunControl | None = None
        self.job_counter = 0
        self.jobs: dict[str, QueueJob] = {}
        self.job_order: list[str] = []
        self.pending_job_ids: list[str] = []
        self.current_report_payload: dict[str, Any] | None = None

        self.assignment_rows: dict[str, dict[str, Any]] = {}
        self.history_rows: dict[str, dict[str, Any]] = {}
        self.worker_rows: dict[str, dict[str, Any]] = {}
        self.timeline_rows: dict[str, dict[str, Any]] = {}
        self.latest_health_rows: dict[str, dict[str, Any]] = {}
        self.latest_smoke_rows: dict[str, dict[str, Any]] = {}
        self.sidebar_expanded = True
        self.detail_panel_expanded = False
        self.sidebar_toggle_var = StringVar(value="收起状态栏")
        self.detail_toggle_var = StringVar(value="展开任务细节")
        self.sidebar_width = 390
        self.task_placeholder = "例如：分析当前仓库，拆分登录模块重构任务，并优先把可并行的编码工作分包。"
        self.task_placeholder_visible = False
        self.sidebar_section_state: dict[str, bool] = {}
        self.sidebar_section_titles: dict[str, str] = {}
        self.sidebar_section_bodies: dict[str, tk.Frame] = {}
        self.sidebar_section_vars: dict[str, StringVar] = {}
        self.output_embeds: list[tk.Widget] = []
        self._output_queue: list[dict[str, Any]] = []
        self._output_stream_active = False
        self._output_stream_state: dict[str, Any] | None = None
        self._ui_state_path = self.loader.runs_dir / "mvp_ui_state.json"
        self._load_ui_state()
        self.pet_config = (
            self.loader.config.get("pet", {})
            if isinstance(self.loader.config.get("pet"), dict)
            else {}
        )
        self.pet_packages: list[PetPackage] = []
        self.pet_package_map: dict[str, PetPackage] = {}
        self.pet_choice_map: dict[str, str] = {}
        self.current_pet_package: PetPackage | None = None
        self.current_pet_mood: PetMood = MOODS["idle"]
        self.pet_choice_var = StringVar(value="")
        self.pet_name_var = StringVar(value="MVP Agent")
        self.pet_status_var = StringVar(value=MOODS["idle"].label)
        self.pet_message_var = StringVar(value=MOODS["idle"].note)
        self.pet_description_var = StringVar(value="")
        self.pet_path_var = StringVar(value="")
        self.pet_preview_photo = None
        self.pet_header_photo = None
        self.pet_frame_cache: dict[tuple[str, str, str], list[ImageTk.PhotoImage]] = {}
        self.pet_animation_nonce = 0
        self.pet_animation_state = MOODS["idle"].animation
        self.pet_animation_frames: list[ImageTk.PhotoImage] = []
        self.pet_header_frames: list[ImageTk.PhotoImage] = []
        self.pet_animation_delays: tuple[int, ...] = ()
        self.pet_animation_index = 0
        self.pet_connectivity_ok = True
        self._discover_pet_packages()

        self.title("MVP Agent — Code Agent")
        self.geometry("1400x860")
        self.minsize(1100, 700)
        self.configure(bg=self.colors["bg"])
        self.option_add("*Font", self.fonts["base"])
        self._style()
        self._icons()
        self._build()
        self._select_pet_package(self.current_pet_package, reset_animation=True)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.profile_var.trace_add("write", lambda *_args: self._refresh_header())
        self.hint_var.trace_add("write", lambda *_args: self._refresh_header())
        self._refresh_header()
        self.task_text.focus_set()
        self._schedule_after(self.POLL_MS, self._poll_queue)
        if autostart:
            self._schedule_after(120, self.refresh_workspace_async)
            self._schedule_after(180, self.refresh_stats_async)
            self._schedule_after(320, self.refresh_history_async)
            self._schedule_after(2200, self.refresh_health_async)
            self._schedule_after(self.AUTO_STATS_MS, self._schedule_stats_refresh)
            self._schedule_after(self.AUTO_WORKSPACE_MS, self._schedule_workspace_refresh)

        # Ctrl+Enter shortcut
        self.bind_all("<Control-Return>", lambda _e: self.run_task_async())
        self.bind_all("<Control-Shift-Return>", lambda _e: self.plan_task_async())

    # ── style & icons ────────────────────────────────────────────────

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=self.colors["bg"], foreground=self.colors["text"])
        style.configure("Card.TFrame", background=self.colors["panel"])
        style.configure("Card.TLabelframe", background=self.colors["panel"],
                         bordercolor=self.colors["line"])
        style.configure("Card.TLabelframe.Label", background=self.colors["panel"],
                         foreground=self.colors["text"], font=self.fonts["section"])
        style.configure("Accent.TButton", background=self.colors["accent"],
                         foreground="white", borderwidth=0, padding=(18, 10),
                         font=self.fonts["bold"])
        style.map("Accent.TButton",
                   background=[("active", self.colors["accent_dark"]),
                               ("disabled", "#E0A0A8")])
        style.configure("Quiet.TButton", background=self.colors["panel_alt"],
                         foreground=self.colors["text"], bordercolor=self.colors["line"],
                         padding=(10, 6))
        style.map("Quiet.TButton",
                   background=[("active", "#EAE3D8")],
                   bordercolor=[("active", self.colors["accent"])])
        style.configure("Mini.Quiet.TButton", padding=(8, 3), font=self.fonts["small"])
        style.configure("Status.Treeview", background="white", fieldbackground="white",
                         foreground=self.colors["text"], bordercolor=self.colors["line"],
                         rowheight=28)
        style.configure("Status.Treeview.Heading", background="#F0EBE3",
                         foreground=self.colors["text"], relief="flat",
                         font=self.fonts["bold"])
        style.configure("Ferrari.Horizontal.TProgressbar",
                         troughcolor="#E8E0D8", bordercolor="#E8E0D8",
                         background=self.colors["accent"], lightcolor=self.colors["accent"],
                         darkcolor=self.colors["accent"], thickness=8)
        style.configure("MVP.TNotebook", background=self.colors["panel"],
                         borderwidth=0, tabmargins=(0, 8, 0, 0))
        style.configure("MVP.TNotebook.Tab", background="#F3ECE4",
                         foreground=self.colors["muted"], padding=(18, 9), borderwidth=0)
        style.map("MVP.TNotebook.Tab",
                   background=[("selected", self.colors["panel"])],
                   foreground=[("selected", self.colors["text"])])

    def _icons(self) -> None:
        self.brand_photo = None
        self._reload_brand_icon_assets()

    def _reload_brand_icon_assets(self) -> None:
        icon_png = self.assets_dir / "mvp_icon.png"
        icon_ico = self.assets_dir / "mvp_icon.ico"
        try:
            if icon_png.exists():
                photo = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, photo)
                self.brand_photo = photo.subsample(5, 5)
        except Exception:
            self.brand_photo = None
        try:
            if icon_ico.exists():
                self.iconbitmap(default=str(icon_ico))
        except Exception:
            pass
        if hasattr(self, "header_brand_image_label") and self.brand_photo is not None:
            self.header_brand_image_label.configure(image=self.brand_photo)

    def _pet_roots(self) -> list[Path]:
        roots: list[Path] = []
        for raw in self.pet_config.get("discover_roots", []):
            text = str(raw).strip()
            if text:
                roots.append(Path(text).expanduser())
        default_dir = str(self.pet_config.get("default_package_dir", "")).strip()
        if default_dir:
            roots.append(Path(default_dir).expanduser().parent)
        return roots

    def _pet_choice_label(self, package: PetPackage) -> str:
        return f"{package.display_name}  [{package.pet_id}]"

    def _discover_pet_packages(self) -> None:
        packages = discover_pet_packages(self._pet_roots())
        lock_to_default = bool(self.pet_config.get("lock_to_default", False))
        package_map: dict[str, PetPackage] = {}
        for package in packages:
            package_map[package.pet_id] = package
            package_map[str(package.package_dir).lower()] = package

        default_dir = str(self.pet_config.get("default_package_dir", "")).strip()
        if default_dir:
            default_package = load_pet_package(default_dir)
            if default_package is not None:
                package_map.setdefault(default_package.pet_id, default_package)
                package_map.setdefault(str(default_package.package_dir).lower(), default_package)
                if not any(item.pet_id == default_package.pet_id for item in packages):
                    packages.append(default_package)
                if lock_to_default:
                    packages = [item for item in packages if item.pet_id == default_package.pet_id]
                    package_map = {
                        default_package.pet_id: default_package,
                        str(default_package.package_dir).lower(): default_package,
                    }

        packages.sort(key=lambda item: item.display_name.lower())
        self.pet_packages = packages
        self.pet_package_map = package_map
        self.pet_choice_map = {
            self._pet_choice_label(package): package.pet_id for package in packages
        }

        selected_key = str(self.pet_config.get("selected_pet_id", "")).strip()
        current = self.current_pet_package
        package = None
        if current is not None:
            package = package_map.get(current.pet_id) or package_map.get(str(current.package_dir).lower())
        if package is None and selected_key:
            package = package_map.get(selected_key) or package_map.get(selected_key.lower())
        if package is None and packages:
            package = packages[0]
        self._select_pet_package(package, reset_animation=False)

    def _select_pet_package(self, package: PetPackage | None, *, reset_animation: bool = True) -> None:
        self.current_pet_package = package
        if package is None:
            self.pet_name_var.set("MVP Agent")
            self.pet_description_var.set("未发现可用宠物包。")
            self.pet_path_var.set("")
            self.pet_choice_var.set("")
            self._apply_pet_mood(MOODS["offline"], note="当前没有找到 Hatch Pet / Codex Pet 资源。", restart=False)
            return

        self.pet_name_var.set(package.display_name)
        self.pet_description_var.set(package.description or "已连接到 Codex Pet 包。")
        self.pet_path_var.set(str(package.package_dir))
        choice = self._pet_choice_label(package)
        self.pet_choice_var.set(choice)
        self._apply_pet_mood(self.current_pet_mood, note=self.pet_message_var.get() or self.current_pet_mood.note, restart=reset_animation)

    def _pet_frames_for(self, animation_name: str, slot: str) -> list[ImageTk.PhotoImage]:
        if self.current_pet_package is None:
            return []
        size = (136, 136) if slot == "preview" else (56, 56)
        cache_key = (str(self.current_pet_package.package_dir), animation_name, slot)
        cached = self.pet_frame_cache.get(cache_key)
        if cached is not None:
            return cached
        frames = [
            ImageTk.PhotoImage(make_thumbnail(frame, size))
            for frame in extract_animation_frames(self.current_pet_package, animation_name)
        ]
        self.pet_frame_cache[cache_key] = frames
        return frames

    def _apply_pet_mood(self, mood: PetMood, *, note: str | None = None, restart: bool = True) -> None:
        self.current_pet_mood = mood
        note_text = safe_snippet((note or mood.note).strip(), 140) or mood.note
        self.pet_status_var.set(mood.label)
        self.pet_message_var.set(note_text)
        if hasattr(self, "pet_status_badge"):
            self.pet_status_badge.configure(bg=mood.accent, fg="white")
        if hasattr(self, "pet_header_status_label"):
            self.pet_header_status_label.configure(fg=mood.accent)
        if hasattr(self, "pet_note_label"):
            self.pet_note_label.configure(fg=self.colors["muted"])
        self.pet_animation_state = mood.animation
        self._start_pet_animation(restart=restart)

    def _start_pet_animation(self, *, restart: bool = True) -> None:
        if self.current_pet_package is None:
            return
        self.pet_animation_frames = self._pet_frames_for(self.pet_animation_state, "preview")
        self.pet_header_frames = self._pet_frames_for(self.pet_animation_state, "header")
        self.pet_animation_delays = animation_durations(self.pet_animation_state)
        if not self.pet_animation_frames:
            return
        if restart or self.pet_animation_index >= len(self.pet_animation_frames):
            self.pet_animation_index = 0
        self.pet_animation_nonce += 1
        self._render_pet_frame(self.pet_animation_nonce)

    def _render_pet_frame(self, nonce: int) -> None:
        if nonce != self.pet_animation_nonce or self.current_pet_package is None:
            return
        if not self.pet_animation_frames:
            return
        index = self.pet_animation_index % len(self.pet_animation_frames)
        preview = self.pet_animation_frames[index]
        header = self.pet_header_frames[index % len(self.pet_header_frames)] if self.pet_header_frames else preview
        self.pet_preview_photo = preview
        self.pet_header_photo = header
        if hasattr(self, "pet_preview_label"):
            self.pet_preview_label.configure(image=preview, text="")
        if hasattr(self, "pet_header_preview_label"):
            self.pet_header_preview_label.configure(image=header, text="")
        delay = self.pet_animation_delays[min(index, len(self.pet_animation_delays) - 1)] if self.pet_animation_delays else 160
        self.pet_animation_index = (index + 1) % len(self.pet_animation_frames)
        self._schedule_after(max(90, int(delay * 0.78)), lambda current=nonce: self._render_pet_frame(current))

    def _refresh_pet_catalog(self) -> None:
        current_id = self.current_pet_package.pet_id if self.current_pet_package is not None else ""
        self.pet_config["selected_pet_id"] = current_id or self.pet_config.get("selected_pet_id", "")
        self._discover_pet_packages()
        self._refresh_pet_picker()

    def _refresh_pet_picker(self) -> None:
        if hasattr(self, "pet_choice_combo"):
            values = list(self.pet_choice_map.keys())
            self.pet_choice_combo.configure(values=values)
            self.pet_choice_combo.configure(state="disabled" if len(values) <= 1 else "readonly")

    def _on_pet_selected(self, _event: object = None) -> None:
        selected = self.pet_choice_var.get().strip()
        pet_id = self.pet_choice_map.get(selected, "")
        if not pet_id:
            return
        package = self.pet_package_map.get(pet_id)
        if package is None:
            return
        self.pet_config["selected_pet_id"] = package.pet_id
        self._select_pet_package(package, reset_animation=True)

    def _open_current_pet_folder(self) -> None:
        if self.current_pet_package is None:
            messagebox.showinfo("没有宠物包", "当前没有可打开的宠物包。")
            return
        if hasattr(os, "startfile"):
            os.startfile(str(self.current_pet_package.package_dir))

    def _sync_icon_from_pet(self, show_message: bool = False) -> None:
        if self.current_pet_package is None:
            if show_message:
                messagebox.showinfo("无法同步图标", "当前还没有可用的宠物包。")
            return
        frame = extract_animation_frames(self.current_pet_package, "idle")[0]
        icon = make_brand_icon(frame, size=256)
        png_path = self.assets_dir / "mvp_icon.png"
        ico_path = self.assets_dir / "mvp_icon.ico"
        icon.save(png_path)
        icon.save(
            ico_path,
            sizes=[(256, 256), (128, 128), (96, 96), (64, 64), (48, 48), (32, 32), (16, 16)],
        )
        self._reload_brand_icon_assets()
        if show_message:
            messagebox.showinfo("图标已同步", f"已根据 {self.current_pet_package.display_name} 更新 MVP Agent 图标。")

    def _schedule_after(self, delay_ms: int, callback: Callable[[], None]) -> str | None:
        if self._shutting_down or not self.winfo_exists():
            return None
        ticket: dict[str, str | None] = {"id": None}

        def wrapped() -> None:
            after_id = ticket["id"]
            if after_id is not None:
                self._after_ids.discard(after_id)
            if self._shutting_down or not self.winfo_exists():
                return
            callback()

        after_id = self.after(delay_ms, wrapped)
        ticket["id"] = after_id
        self._after_ids.add(after_id)
        return after_id

    def destroy(self) -> None:
        if self._shutting_down:
            return
        self._save_ui_state()
        self._shutting_down = True
        if self.current_run_control is not None:
            try:
                self.current_run_control.request_cancel(
                    "GUI is shutting down; stopping the current task safely.")
            except Exception:
                pass
        for after_id in list(self._after_ids):
            try:
                self.after_cancel(after_id)
            except Exception:
                pass
            self._after_ids.discard(after_id)
        super().destroy()

    def _load_ui_state(self) -> None:
        defaults = {
            "pet": True,
            "summary": True,
            "control": True,
            "queue": True,
            "workers": False,
            "system": False,
            "history": False,
        }
        try:
            payload = load_json_file(self._ui_state_path)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        sections = payload.get("sidebar_sections", {})
        if not isinstance(sections, dict):
            sections = {}
        self.sidebar_section_state = defaults | {
            str(key): bool(value) for key, value in sections.items()
        }
        self.sidebar_width = max(
            280, min(560, int(payload.get("sidebar_width", self.sidebar_width))))
        self.sidebar_expanded = bool(payload.get("sidebar_expanded", self.sidebar_expanded))
        self.detail_panel_expanded = bool(
            payload.get("detail_panel_expanded", self.detail_panel_expanded))

    def _save_ui_state(self) -> None:
        payload = {
            "sidebar_width": self.sidebar_width,
            "sidebar_expanded": self.sidebar_expanded,
            "detail_panel_expanded": self.detail_panel_expanded,
            "sidebar_sections": self.sidebar_section_state,
        }
        try:
            save_json_file(self._ui_state_path, payload)
        except Exception:
            pass

    # ── layout ───────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Row 0: accent bar
        tk.Frame(self, bg=self.colors["accent"], height=3).grid(
            row=0, column=0, sticky="ew")

        # Row 1: header
        self._build_header(row=1)

        # Row 2: main area (input + output)
        self._build_main_area(row=2)

        # Row 3: footer
        self._build_footer(row=3)

    def _build_header(self, row: int) -> None:
        bar = tk.Frame(self, bg=self.colors["panel"], padx=20, pady=10,
                       highlightthickness=1, highlightbackground=self.colors["line"])
        bar.grid(row=row, column=0, sticky="ew", padx=14, pady=(10, 0))
        bar.grid_columnconfigure(1, weight=1)

        # Brand
        brand = tk.Frame(bar, bg=self.colors["panel"])
        brand.grid(row=0, column=0, sticky="w")
        if self.brand_photo is not None:
            self.header_brand_image_label = tk.Label(brand, image=self.brand_photo, bg=self.colors["panel"])
            self.header_brand_image_label.grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 10))
        tk.Label(brand, text="MVP Agent", bg=self.colors["panel"],
                 fg=self.colors["text"], font=self.fonts["title"]).grid(
            row=0, column=1, sticky="w")
        tk.Label(brand, text="Code Agent", bg=self.colors["panel"],
                 fg=self.colors["muted"], font=self.fonts["small"]).grid(
            row=1, column=1, sticky="w")
        controls = tk.Frame(bar, bg=self.colors["panel"])
        controls.grid(row=0, column=1, sticky="e")
        ttk.Button(
            controls,
            textvariable=self.sidebar_toggle_var,
            style="Quiet.TButton",
            command=self._toggle_sidebar,
        ).pack(side="right")
        pet_summary = tk.Frame(controls, bg=self.colors["panel"])
        pet_summary.pack(side="right", padx=(0, 12))
        self.pet_header_preview_label = tk.Label(
            pet_summary,
            width=56,
            height=56,
            bg=self.colors["panel"],
            anchor="center",
        )
        self.pet_header_preview_label.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.pet_header_status_label = tk.Label(
            pet_summary,
            textvariable=self.pet_status_var,
            bg=self.colors["panel"],
            fg=self.current_pet_mood.accent,
            font=self.fonts["bold"],
        )
        self.pet_header_status_label.grid(row=0, column=1, sticky="w")
        tk.Label(
            pet_summary,
            textvariable=self.pet_name_var,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=self.fonts["small"],
        ).grid(row=1, column=1, sticky="w", pady=(2, 0))
        status_block = tk.Frame(controls, bg=self.colors["panel"])
        status_block.pack(side="right", padx=(0, 10))
        tk.Label(status_block, textvariable=self.status_var,
                 bg=self.colors["panel"], fg=self.colors["text"],
                 font=self.fonts["bold"]).pack(anchor="e")
        tk.Label(status_block, textvariable=self.progress_var,
                 bg=self.colors["panel"], fg=self.colors["muted"],
                 font=self.fonts["small"]).pack(anchor="e", pady=(2, 0))

    def _header_chip(self, parent: tk.Frame, label: str,
                     var: StringVar, col: int) -> None:
        f = tk.Frame(parent, bg=self.colors["panel_alt"], padx=8, pady=3)
        f.grid(row=0, column=col, padx=(0, 6))
        tk.Label(f, text=label, bg=self.colors["panel_alt"],
                 fg=self.colors["muted"], font=self.fonts["small"]).grid(
            row=0, column=0, sticky="w")
        tk.Label(f, textvariable=var, bg=self.colors["panel_alt"],
                 fg=self.colors["text"], font=self.fonts["bold"]).grid(
            row=1, column=0, sticky="w")

    def _build_main_area(self, row: int) -> None:
        self.main_area = tk.Frame(self, bg=self.colors["bg"])
        self.main_area.grid(row=row, column=0, sticky="nsew", padx=14, pady=(8, 0))
        self.main_area.grid_columnconfigure(0, weight=0, minsize=self.sidebar_width)
        self.main_area.grid_columnconfigure(1, weight=0, minsize=10)
        self.main_area.grid_columnconfigure(2, weight=1)
        self.main_area.grid_rowconfigure(0, weight=1)

        self.sidebar_shell = tk.Frame(self.main_area, bg=self.colors["bg"], width=self.sidebar_width)
        self.sidebar_shell.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        self.sidebar_shell.grid_propagate(False)
        self.sidebar_shell.grid_rowconfigure(0, weight=1)
        self.sidebar_shell.grid_columnconfigure(0, weight=1)

        self.sidebar_canvas = tk.Canvas(
            self.sidebar_shell,
            bg=self.colors["bg"],
            highlightthickness=0,
            bd=0,
            width=390,
        )
        self.sidebar_canvas.grid(row=0, column=0, sticky="nsew")
        self.sidebar_scroll = ttk.Scrollbar(
            self.sidebar_shell, orient="vertical", command=self.sidebar_canvas.yview)
        self.sidebar_scroll.grid(row=0, column=1, sticky="ns")
        self.sidebar_canvas.configure(yscrollcommand=self.sidebar_scroll.set)
        self.sidebar_content = tk.Frame(self.sidebar_canvas, bg=self.colors["bg"])
        self.sidebar_window = self.sidebar_canvas.create_window(
            (0, 0), window=self.sidebar_content, anchor="nw")
        self.sidebar_content.bind(
            "<Configure>",
            lambda _e: self.sidebar_canvas.configure(
                scrollregion=self.sidebar_canvas.bbox("all")))
        self.sidebar_canvas.bind(
            "<Configure>",
            lambda e: self.sidebar_canvas.itemconfigure(
                self.sidebar_window, width=max(e.width - 4, 320)))
        self._bind_canvas_wheel(self.sidebar_canvas)

        self.sidebar_dragger = tk.Frame(
            self.main_area,
            bg="#E7DDD4",
            width=10,
            cursor="sb_h_double_arrow",
            highlightthickness=0,
        )
        self.sidebar_dragger.grid(row=0, column=1, sticky="ns", padx=(0, 12))
        self.sidebar_dragger.bind("<Button-1>", self._begin_sidebar_drag)
        self.sidebar_dragger.bind("<B1-Motion>", self._drag_sidebar)
        self.sidebar_dragger.bind("<Enter>", lambda _e: self.sidebar_dragger.configure(bg="#D7C7B8"))
        self.sidebar_dragger.bind("<Leave>", lambda _e: self.sidebar_dragger.configure(bg="#E7DDD4"))

        self.right_shell = tk.Frame(self.main_area, bg=self.colors["bg"])
        self.right_shell.grid(row=0, column=2, sticky="nsew")
        self.right_shell.grid_columnconfigure(0, weight=1)
        self.right_shell.grid_rowconfigure(0, weight=1)
        self.right_shell.grid_rowconfigure(1, weight=0)
        self.right_shell.grid_rowconfigure(2, weight=0)

        self._build_sidebar_layout(self.sidebar_content)
        self._build_qa_layout(self.right_shell)
        self._set_sidebar_expanded(self.sidebar_expanded)

    def _toggle_sidebar(self) -> None:
        self._set_sidebar_expanded(not self.sidebar_expanded)

    def _set_sidebar_expanded(self, expanded: bool) -> None:
        self.sidebar_expanded = expanded
        if expanded:
            self.sidebar_shell.grid()
            self.sidebar_dragger.grid()
            self._set_sidebar_width(self.sidebar_width)
            self.sidebar_toggle_var.set("收起状态栏")
        else:
            self.sidebar_shell.grid_remove()
            self.sidebar_dragger.grid_remove()
            self.main_area.grid_columnconfigure(0, minsize=0, weight=0)
            self.main_area.grid_columnconfigure(1, minsize=0, weight=0)
            self.sidebar_toggle_var.set("展开状态栏")
        self._save_ui_state()

    def _begin_sidebar_drag(self, event: tk.Event) -> None:
        del event
        self._sidebar_drag_origin = self.winfo_pointerx()
        self._sidebar_drag_width = self.sidebar_width

    def _drag_sidebar(self, event: tk.Event) -> None:
        if not self.sidebar_expanded:
            return
        origin = getattr(self, "_sidebar_drag_origin", self.winfo_pointerx())
        base_width = getattr(self, "_sidebar_drag_width", self.sidebar_width)
        delta = event.x_root - origin
        self._set_sidebar_width(base_width + delta)

    def _set_sidebar_width(self, width: int) -> None:
        self.sidebar_width = max(280, min(560, int(width)))
        if hasattr(self, "sidebar_shell"):
            self.sidebar_shell.configure(width=self.sidebar_width)
        if hasattr(self, "sidebar_canvas"):
            self.sidebar_canvas.configure(width=self.sidebar_width)
        if hasattr(self, "main_area"):
            self.main_area.grid_columnconfigure(0, minsize=self.sidebar_width, weight=0)
            self.main_area.grid_columnconfigure(1, minsize=10, weight=0)
        self._save_ui_state()

    def _build_input_area(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=self.colors["panel"], padx=16, pady=14,
                        highlightthickness=1,
                        highlightbackground=self.colors["line"])
        card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        # Label
        tk.Label(card, text="把编码任务交给 MVP Agent", bg=self.colors["panel"],
                 fg=self.colors["text"], font=self.fonts["section"]).grid(
            row=0, column=0, sticky="w")
        tk.Label(card, text="描述目标、仓库范围、约束与期望交付。MVP Agent 会先理解、再拆解、再派工、再验收。",
                 bg=self.colors["panel"], fg=self.colors["muted"],
                 font=self.fonts["small"]).grid(
            row=1, column=0, sticky="w", pady=(2, 8))

        # Input text box
        self.task_text = ScrolledText(
            card, height=3, wrap="word", relief="flat", bd=1,
            padx=14, pady=12, background=self.colors["input"],
            foreground=self.colors["text"],
            insertbackground=self.colors["text"])
        self.task_text.grid(row=2, column=0, sticky="ew")
        self.task_text.configure(
            highlightthickness=1, highlightbackground=self.colors["line"],
            highlightcolor=self.colors["accent"])
        self.task_text.bind("<KeyRelease>",
                            lambda _e: self.after_idle(self._refresh_header))

        # Action buttons + profile row
        actions = tk.Frame(card, bg=self.colors["panel"])
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        actions.grid_columnconfigure(0, weight=1)

        primary = tk.Frame(actions, bg=self.colors["panel"])
        primary.grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(
            primary, text="开始编码", style="Accent.TButton",
            command=self.run_task_async)
        self.run_button.pack(side="left")
        self.plan_button = ttk.Button(
            primary, text="只做规划", style="Quiet.TButton",
            command=self.plan_task_async)
        self.plan_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(
            primary, text="停止运行", style="Quiet.TButton",
            command=self.cancel_current_task)
        self.cancel_button.pack(side="left", padx=(8, 0))

        secondary = tk.Frame(actions, bg=self.colors["panel"])
        secondary.grid(row=0, column=1, sticky="e")
        self.quick_ping_button = ttk.Button(
            secondary, text="快速检查", style="Quiet.TButton",
            command=self.run_quick_ping_async)
        self.quick_ping_button.pack(side="left", padx=(0, 4))
        self.deep_ping_button = ttk.Button(
            secondary, text="深度检查", style="Quiet.TButton",
            command=self.run_deep_ping_async)
        self.deep_ping_button.pack(side="left", padx=(0, 4))
        self.refresh_button = ttk.Button(
            secondary, text="刷新状态", style="Quiet.TButton",
            command=self.refresh_health_async)
        self.refresh_button.pack(side="left")

        # Profile row
        prof = tk.Frame(card, bg=self.colors["panel"])
        prof.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        tk.Label(prof, text="运行策略", bg=self.colors["panel"],
                 fg=self.colors["muted"], font=self.fonts["small"]).pack(side="left")
        ttk.Combobox(prof, textvariable=self.profile_var,
                      values=list(self.PROFILE_LABELS.keys()),
                      state="readonly", width=8).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(prof, text="自动识别预算偏好",
                         variable=self.hint_var).pack(side="left", padx=(12, 0))

        # Queue preview
        self.queue_tree = ttk.Treeview(
            card, columns=("state", "mode", "kind", "task"),
            show="headings", style="Status.Treeview", height=4)
        for key, title, width in (
            ("state", "状态", 80), ("mode", "策略", 72),
            ("kind", "类型", 60), ("task", "任务摘要", 520),
        ):
            self.queue_tree.heading(key, text=title)
            self.queue_tree.column(key, width=width,
                                    anchor="center" if key != "task" else "w")
        self.queue_tree.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_selected)
        qtools = tk.Frame(card, bg=self.colors["panel"])
        qtools.grid(row=6, column=0, sticky="ew", pady=(4, 0))
        self.queue_remove_button = ttk.Button(
            qtools, text="移除待执行", style="Quiet.TButton",
            command=self.remove_selected_queue_job)
        self.queue_remove_button.pack(side="left")
        self.queue_detail = self._readonly(card, 4)
        self.queue_detail.grid(row=7, column=0, sticky="ew", pady=(6, 0))
        self._set_text(self.queue_detail, "当前没有运行中的编码任务。")

    def _build_output_area(self, parent: tk.Frame) -> None:
        card = tk.Frame(parent, bg=self.colors["panel"], padx=16, pady=14,
                        highlightthickness=1,
                        highlightbackground=self.colors["line"])
        card.grid(row=1, column=0, sticky="nsew")
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=3)
        card.grid_rowconfigure(1, weight=2)

        # Output text (streaming)
        out_frame = ttk.LabelFrame(card, text="输出", style="Card.TLabelframe",
                                    padding=8)
        out_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        out_frame.grid_columnconfigure(0, weight=1)
        out_frame.grid_rowconfigure(0, weight=1)
        self.output_text = ScrolledText(
            out_frame, wrap="word", relief="flat", bd=0,
            padx=12, pady=10, background=self.colors["input"],
            foreground=self.colors["text"],
            insertbackground=self.colors["text"],
            font=self.fonts["base"])
        self.output_text.grid(row=0, column=0, sticky="nsew")
        self.output_text.configure(state="disabled")
        self.live_stream = self.output_text
        # Configure tags for color-coded output
        self.output_text.tag_configure(
            "user", foreground=self.colors["accent"],
            font=self.fonts["bold"])
        self.output_text.tag_configure(
            "leader", foreground=self.colors["text"],
            font=self.fonts["bold"])
        self.output_text.tag_configure(
            "system", foreground=self.colors["muted"])
        self.output_text.tag_configure(
            "error", foreground=self.colors["bad"], font=self.fonts["bold"])
        self.output_text.tag_configure(
            "summary", foreground=self.colors["good"],
            font=self.fonts["bold"])
        self.output_text.tag_configure(
            "milestone", foreground=self.colors["accent_dark"],
            font=self.fonts["bold"])

        # Detail sections (collapsible)
        detail_frame = ttk.Frame(card, style="Card.TFrame")
        detail_frame.grid(row=1, column=0, sticky="nsew")
        detail_frame.grid_columnconfigure(0, weight=1)
        detail_frame.grid_rowconfigure(0, weight=1)

        detail_book = ttk.Notebook(detail_frame, style="MVP.TNotebook")
        detail_book.grid(row=0, column=0, sticky="nsew")

        # Tab 1: Plan & Execution
        exec_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(exec_tab, text="规划与执行")
        exec_tab.grid_columnconfigure(0, weight=1)
        exec_tab.grid_rowconfigure(0, weight=1)
        exec_tab.grid_rowconfigure(1, weight=2)
        self.assign_tree = ttk.Treeview(
            exec_tab,
            columns=("task", "kind", "worker", "result", "review"),
            show="headings", style="Status.Treeview")
        for key, title, width in (
            ("task", "子任务", 260), ("kind", "类型", 80),
            ("worker", "执行成员", 160), ("result", "结果", 90),
            ("review", "验收", 90),
        ):
            self.assign_tree.heading(key, text=title)
            self.assign_tree.column(key, width=width,
                                     anchor="center" if key != "task" else "w")
        self.assign_tree.grid(row=0, column=0, sticky="nsew")
        self.assign_tree.bind("<<TreeviewSelect>>",
                               self._on_assignment_selected)
        self.assign_detail = self._readonly(exec_tab, 10)
        self.assign_detail.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        # Tab 2: Raw JSON
        raw_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(raw_tab, text="原始 JSON")
        raw_tab.grid_columnconfigure(0, weight=1)
        raw_tab.grid_rowconfigure(0, weight=1)
        self.raw_json = self._readonly(raw_tab, 24)
        self.raw_json.grid(row=0, column=0, sticky="nsew")

        # Tab 3: System Status
        sys_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(sys_tab, text="系统状态")
        sys_tab.grid_columnconfigure(0, weight=1)
        sys_tab.grid_rowconfigure(0, weight=1)
        sys_tab.grid_rowconfigure(1, weight=2)
        sys_inner = ttk.Notebook(sys_tab, style="MVP.TNotebook")
        sys_inner.grid(row=0, column=0, sticky="nsew", rowspan=2)

        # Workers sub-tab
        workers_tab = ttk.Frame(sys_inner, style="Card.TFrame", padding=6)
        sys_inner.add(workers_tab, text="成员")
        workers_tab.grid_columnconfigure(0, weight=1)
        workers_tab.grid_rowconfigure(0, weight=1)
        workers_tab.grid_rowconfigure(1, weight=2)
        self.worker_tree = ttk.Treeview(
            workers_tab,
            columns=("worker", "framework", "target", "state"),
            show="headings", style="Status.Treeview")
        for key, title, width in (
            ("worker", "成员", 160), ("framework", "框架", 90),
            ("target", "模型 / 目标", 180), ("state", "状态", 80),
        ):
            self.worker_tree.heading(key, text=title)
            self.worker_tree.column(key, width=width,
                                     anchor="center" if key != "worker" else "w")
        self.worker_tree.grid(row=0, column=0, sticky="nsew")
        self.worker_tree.bind("<<TreeviewSelect>>", self._on_worker_selected)
        self.worker_detail = self._readonly(workers_tab, 11)
        self.worker_detail.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        # Hardware sub-tab
        hw_tab = ttk.Frame(sys_inner, style="Card.TFrame", padding=6)
        sys_inner.add(hw_tab, text="硬件与工作区")
        hw_tab.grid_columnconfigure(0, weight=1)
        hw_tab.grid_rowconfigure(0, weight=0)
        hw_tab.grid_rowconfigure(1, weight=1)
        hw_tab.grid_rowconfigure(2, weight=1)
        hw_actions = ttk.Frame(hw_tab, style="Card.TFrame")
        hw_actions.grid(row=0, column=0, sticky="ew")
        ttk.Button(hw_actions, text="刷新硬件", style="Quiet.TButton",
                    command=self.refresh_stats_async).pack(side="left")
        ttk.Button(hw_actions, text="刷新工作区", style="Quiet.TButton",
                    command=self.refresh_workspace_async).pack(
            side="left", padx=(8, 0))
        tk.Label(hw_actions, textvariable=self.hardware_mode_var,
                 bg=self.colors["panel"], fg=self.colors["text"]).pack(
            side="left", padx=(16, 0))
        self.hardware_text = self._readonly(hw_tab, 8)
        self.hardware_text.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        ws_frame = ttk.LabelFrame(hw_tab, text="工作区视图",
                                   style="Card.TLabelframe", padding=6)
        ws_frame.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        ws_frame.grid_columnconfigure(0, weight=1)
        ws_frame.grid_rowconfigure(0, weight=1)
        ws_book = ttk.Notebook(ws_frame, style="MVP.TNotebook")
        ws_book.grid(row=0, column=0, sticky="nsew")
        ft_tab = ttk.Frame(ws_book, style="Card.TFrame", padding=6)
        diff_tab = ttk.Frame(ws_book, style="Card.TFrame", padding=6)
        ws_book.add(ft_tab, text="文件树")
        ws_book.add(diff_tab, text="Diff")
        ft_tab.grid_columnconfigure(0, weight=1)
        ft_tab.grid_rowconfigure(0, weight=1)
        diff_tab.grid_columnconfigure(0, weight=1)
        diff_tab.grid_rowconfigure(0, weight=1)
        self.home_file_tree = self._readonly(ft_tab, 14)
        self.home_file_tree.grid(row=0, column=0, sticky="nsew")
        self.home_diff = self._readonly(diff_tab, 14)
        self.home_diff.grid(row=0, column=0, sticky="nsew")
        self.home_workspace_preview = self.home_file_tree
        self._set_text(self.home_file_tree, f"工作区：{self.loader.workspace_root}\n\n等待工作区快照。")
        self._set_text(self.home_diff, "等待工作区 diff。")

        # History sub-tab
        hist_tab = ttk.Frame(sys_inner, style="Card.TFrame", padding=6)
        sys_inner.add(hist_tab, text="运行历史")
        hist_tab.grid_columnconfigure(0, weight=1)
        hist_tab.grid_rowconfigure(0, weight=0)
        hist_tab.grid_rowconfigure(1, weight=2)
        hist_tab.grid_rowconfigure(2, weight=1)
        hist_actions = ttk.Frame(hist_tab, style="Card.TFrame")
        hist_actions.grid(row=0, column=0, sticky="ew")
        ttk.Button(hist_actions, text="刷新", style="Quiet.TButton",
                    command=self.refresh_history_async).pack(side="left")
        ttk.Button(hist_actions, text="打开源文件", style="Quiet.TButton",
                    command=self.open_selected_history_source).pack(
            side="left", padx=(8, 0))
        self.history_tree = ttk.Treeview(
            hist_tab,
            columns=("time", "source", "title", "subtitle"),
            show="headings", style="Status.Treeview")
        for key, title, width in (
            ("time", "时间", 140), ("source", "来源", 100),
            ("title", "主题", 240), ("subtitle", "补充", 260),
        ):
            self.history_tree.heading(key, text=title)
            self.history_tree.column(key, width=width,
                                      anchor="center"
                                      if key in {"time", "source"} else "w")
        self.history_tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_selected)
        self.history_tree.bind("<Double-1>",
                                self._load_selected_history_into_dashboard)
        self.history_detail = self._readonly(hist_tab, 8)
        self.history_detail.grid(row=2, column=0, sticky="nsew", pady=(6, 0))

        # Summary panel
        summary_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(summary_tab, text="总览")
        summary_tab.grid_columnconfigure(0, weight=1)
        summary_tab.grid_rowconfigure(0, weight=2)
        summary_tab.grid_rowconfigure(1, weight=1)
        self.home_summary = self._readonly(summary_tab, 16)
        self.home_summary.grid(row=0, column=0, sticky="nsew")
        self._set_text(self.home_summary, "等待编码任务。")
        self.team_summary = self.home_summary
        self.home_architecture = self._readonly(summary_tab, 6)
        self.home_architecture.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._set_text(
            self.home_architecture,
            "Planner -> Router -> Worker Mesh -> Reviewer -> Reroute\n"
            "面向代码仓库的 code agent 路由链已就绪。",
        )
        timeline_box = ttk.LabelFrame(summary_tab, text="子任务时间线", style="Card.TLabelframe", padding=6)
        timeline_box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        timeline_box.grid_columnconfigure(0, weight=1)
        timeline_box.grid_rowconfigure(0, weight=1)
        timeline_box.grid_rowconfigure(1, weight=1)
        self.timeline_tree = ttk.Treeview(
            timeline_box,
            columns=("time", "phase", "task", "worker"),
            show="headings",
            style="Status.Treeview",
            height=5,
        )
        for key, title, width in (
            ("time", "时间", 90),
            ("phase", "阶段", 110),
            ("task", "任务", 240),
            ("worker", "成员", 140),
        ):
            self.timeline_tree.heading(key, text=title)
            self.timeline_tree.column(key, width=width, anchor="center" if key in {"time", "phase"} else "w")
        self.timeline_tree.grid(row=0, column=0, sticky="nsew")
        self.timeline_tree.bind("<<TreeviewSelect>>", self._on_timeline_selected)
        self.timeline_detail = self._readonly(timeline_box, 5)
        self.timeline_detail.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._set_text(self.timeline_detail, "当前没有子任务时间线。")

    def _sidebar_section_label(self, key: str) -> str:
        title = self.sidebar_section_titles.get(key, key)
        marker = "[-]" if self.sidebar_section_state.get(key, True) else "[+]"
        return f"{marker} {title}"

    def _apply_sidebar_section_state(self, key: str) -> None:
        body = self.sidebar_section_bodies.get(key)
        if body is None:
            return
        if self.sidebar_section_state.get(key, True):
            body.grid()
        else:
            body.grid_remove()
        var = self.sidebar_section_vars.get(key)
        if var is not None:
            var.set(self._sidebar_section_label(key))

    def _toggle_sidebar_section(self, key: str) -> None:
        self.sidebar_section_state[key] = not self.sidebar_section_state.get(key, True)
        self._apply_sidebar_section_state(key)
        self._save_ui_state()

    def _build_sidebar_section(
        self,
        parent: tk.Frame,
        key: str,
        title: str,
        row: int,
        subtitle: str = "",
    ) -> tuple[tk.Frame, int]:
        self.sidebar_section_titles[key] = title
        shell = tk.Frame(
            parent, bg=self.colors["panel"], padx=14, pady=12,
            highlightthickness=1, highlightbackground=self.colors["line"])
        shell.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        shell.grid_columnconfigure(0, weight=1)

        header = tk.Frame(shell, bg=self.colors["panel"])
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_var = StringVar(value=self._sidebar_section_label(key))
        self.sidebar_section_vars[key] = title_var
        ttk.Button(
            header,
            textvariable=title_var,
            style="Quiet.TButton",
            command=lambda current=key: self._toggle_sidebar_section(current),
        ).grid(row=0, column=0, sticky="w")

        body = tk.Frame(shell, bg=self.colors["panel"])
        body.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        body.grid_columnconfigure(0, weight=1)
        self.sidebar_section_bodies[key] = body

        next_row = 0
        if subtitle:
            tk.Label(
                body,
                text=subtitle,
                bg=self.colors["panel"],
                fg=self.colors["muted"],
                font=self.fonts["small"],
            ).grid(row=0, column=0, sticky="w", pady=(0, 8))
            next_row = 1

        self._apply_sidebar_section_state(key)
        return body, next_row

    def _build_sidebar_layout(self, parent: tk.Frame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        pet_body, row = self._build_sidebar_section(
            parent,
            "pet",
            "MVP Pet",
            0,
            "宠物会跟随规划、执行、复核与异常状态切换动作。",
        )
        pet_card = tk.Frame(
            pet_body,
            bg="#FFF7EA",
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#E6C982",
        )
        pet_card.grid(row=row, column=0, sticky="ew")
        pet_card.grid_columnconfigure(1, weight=1)
        self.pet_preview_label = tk.Label(
            pet_card,
            bg="#FFF7EA",
            width=136,
            height=136,
            anchor="center",
            text="PET",
            fg=self.colors["muted"],
            font=self.fonts["bold"],
        )
        self.pet_preview_label.grid(row=0, column=0, rowspan=4, padx=(0, 10), sticky="nw")
        tk.Label(
            pet_card,
            textvariable=self.pet_name_var,
            bg="#FFF7EA",
            fg=self.colors["text"],
            font=self.fonts["section"],
        ).grid(row=0, column=1, sticky="w")
        self.pet_status_badge = tk.Label(
            pet_card,
            textvariable=self.pet_status_var,
            bg=self.current_pet_mood.accent,
            fg="white",
            padx=10,
            pady=3,
            font=self.fonts["small"],
        )
        self.pet_status_badge.grid(row=1, column=1, sticky="w", pady=(6, 0))
        self.pet_note_label = tk.Label(
            pet_card,
            textvariable=self.pet_message_var,
            bg="#FFF7EA",
            fg=self.colors["muted"],
            justify="left",
            wraplength=180,
            font=self.fonts["small"],
        )
        self.pet_note_label.grid(row=2, column=1, sticky="w", pady=(8, 0))
        tk.Label(
            pet_card,
            textvariable=self.pet_description_var,
            bg="#FFF7EA",
            fg=self.colors["text"],
            justify="left",
            wraplength=180,
            font=self.fonts["small"],
        ).grid(row=3, column=1, sticky="w", pady=(8, 0))
        pet_tools = tk.Frame(pet_body, bg=self.colors["panel"])
        pet_tools.grid(row=row + 1, column=0, sticky="ew", pady=(8, 0))
        self.pet_choice_combo = ttk.Combobox(
            pet_tools,
            textvariable=self.pet_choice_var,
            state="readonly",
            width=28,
        )
        self.pet_choice_combo.pack(side="left", fill="x", expand=True)
        self.pet_choice_combo.bind("<<ComboboxSelected>>", self._on_pet_selected)
        ttk.Button(
            pet_tools,
            text="刷新",
            style="Quiet.TButton",
            command=self._refresh_pet_catalog,
        ).pack(side="left", padx=(8, 0))
        pet_actions = tk.Frame(pet_body, bg=self.colors["panel"])
        pet_actions.grid(row=row + 2, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(
            pet_actions,
            text="打开宠物包",
            style="Quiet.TButton",
            command=self._open_current_pet_folder,
        ).pack(side="left")
        ttk.Button(
            pet_actions,
            text="同步图标",
            style="Quiet.TButton",
            command=lambda: self._sync_icon_from_pet(show_message=True),
        ).pack(side="left", padx=(8, 0))
        self._refresh_pet_picker()

        summary_body, row = self._build_sidebar_section(
            parent,
            "summary",
            "状态线程栏",
            1,
            "运行状态、队列、成员和历史都收在左边。",
        )
        self.home_summary = self._readonly(summary_body, 8)
        self.home_summary.grid(row=row, column=0, sticky="ew")
        self.team_summary = self.home_summary
        self._set_text(self.home_summary, "等待编码任务。")
        self.home_architecture = self._readonly(summary_body, 4)
        self.home_architecture.grid(row=row + 1, column=0, sticky="ew", pady=(8, 0))
        self._set_text(
            self.home_architecture,
            "Planner -> Router -> Worker Mesh -> Reviewer -> Reroute\n"
            "代码任务会先判断作用域，再决定是否并行分包。",
        )
        self.home_skill_mesh = self._readonly(summary_body, 11)
        self.home_skill_mesh.grid(row=row + 2, column=0, sticky="ew", pady=(8, 0))
        self._set_text(
            self.home_skill_mesh,
            "Skill Mesh 将在这里显示本轮核心 skills、协作原则、公开候选与能力缺口。",
        )

        control_body, row = self._build_sidebar_section(parent, "control", "运行控制", 2)
        profile_row = tk.Frame(control_body, bg=self.colors["panel"])
        profile_row.grid(row=row, column=0, sticky="ew")
        tk.Label(profile_row, text="策略", bg=self.colors["panel"],
                 fg=self.colors["muted"], font=self.fonts["small"]).pack(side="left")
        ttk.Combobox(
            profile_row, textvariable=self.profile_var,
            values=list(self.PROFILE_LABELS.keys()),
            state="readonly", width=8).pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            profile_row, text="自动预算", variable=self.hint_var).pack(side="left", padx=(12, 0))

        action_row = tk.Frame(control_body, bg=self.colors["panel"])
        action_row.grid(row=row + 1, column=0, sticky="ew", pady=(10, 0))
        self.quick_ping_button = ttk.Button(
            action_row, text="快速检查", style="Quiet.TButton",
            command=self.run_quick_ping_async)
        self.quick_ping_button.pack(side="left")
        self.deep_ping_button = ttk.Button(
            action_row, text="深度检查", style="Quiet.TButton",
            command=self.run_deep_ping_async)
        self.deep_ping_button.pack(side="left", padx=(8, 0))
        self.refresh_button = ttk.Button(
            action_row, text="刷新状态", style="Quiet.TButton",
            command=self.refresh_health_async)
        self.refresh_button.pack(side="left", padx=(8, 0))

        self.progressbar = ttk.Progressbar(
            control_body, maximum=100, value=0, mode="determinate",
            style="Ferrari.Horizontal.TProgressbar")
        self.progressbar.grid(row=row + 2, column=0, sticky="ew", pady=(12, 0))
        tk.Label(
            control_body, textvariable=self.progress_var, bg=self.colors["panel"],
            fg=self.colors["muted"], font=self.fonts["small"]).grid(
            row=row + 3, column=0, sticky="w", pady=(6, 0))

        chip_wrap = tk.Frame(control_body, bg=self.colors["panel"])
        chip_wrap.grid(row=row + 4, column=0, sticky="ew", pady=(10, 0))
        for index, (label, var) in enumerate((
            ("运行", self.status_var),
            ("成员", self.header_worker_var),
            ("联调", self.smoke_var),
            ("队列", self.queue_state_var),
        )):
            self._header_chip(chip_wrap, label, var, index)

        queue_body, row = self._build_sidebar_section(parent, "queue", "任务队列", 3)
        self.queue_tree = ttk.Treeview(
            queue_body, columns=("state", "mode", "kind", "task"),
            show="headings", style="Status.Treeview", height=5)
        for key, title, width in (
            ("state", "状态", 70), ("mode", "策略", 70),
            ("kind", "类型", 56), ("task", "任务", 210),
        ):
            self.queue_tree.heading(key, text=title)
            self.queue_tree.column(
                key, width=width, anchor="center" if key != "task" else "w")
        self.queue_tree.grid(row=row, column=0, sticky="ew")
        self.queue_tree.bind("<<TreeviewSelect>>", self._on_queue_selected)
        qtools = tk.Frame(queue_body, bg=self.colors["panel"])
        qtools.grid(row=row + 1, column=0, sticky="ew", pady=(6, 0))
        self.queue_remove_button = ttk.Button(
            qtools, text="移除待执行", style="Quiet.TButton",
            command=self.remove_selected_queue_job)
        self.queue_remove_button.pack(side="left")
        self.queue_detail = self._readonly(queue_body, 4)
        self.queue_detail.grid(row=row + 2, column=0, sticky="ew", pady=(6, 0))
        self._set_text(self.queue_detail, "当前没有运行中的编码任务。")

        worker_body, row = self._build_sidebar_section(parent, "workers", "成员状态", 4)
        self.worker_tree = ttk.Treeview(
            worker_body,
            columns=("worker", "framework", "target", "state"),
            show="headings", style="Status.Treeview", height=5)
        for key, title, width in (
            ("worker", "成员", 126), ("framework", "框架", 72),
            ("target", "模型 / 目标", 150), ("state", "状态", 72),
        ):
            self.worker_tree.heading(key, text=title)
            self.worker_tree.column(
                key, width=width, anchor="center" if key != "worker" else "w")
        self.worker_tree.grid(row=row, column=0, sticky="ew")
        self.worker_tree.bind("<<TreeviewSelect>>", self._on_worker_selected)
        self.worker_detail = self._readonly(worker_body, 6)
        self.worker_detail.grid(row=row + 1, column=0, sticky="ew", pady=(6, 0))

        system_body, row = self._build_sidebar_section(parent, "system", "系统与工作区", 5)
        sys_actions = tk.Frame(system_body, bg=self.colors["panel"])
        sys_actions.grid(row=row, column=0, sticky="ew")
        ttk.Button(sys_actions, text="刷新硬件", style="Quiet.TButton",
                   command=self.refresh_stats_async).pack(side="left")
        ttk.Button(sys_actions, text="刷新工作区", style="Quiet.TButton",
                   command=self.refresh_workspace_async).pack(side="left", padx=(8, 0))
        tk.Label(sys_actions, textvariable=self.hardware_mode_var,
                 bg=self.colors["panel"], fg=self.colors["text"],
                 font=self.fonts["small"]).pack(side="left", padx=(12, 0))
        self.hardware_text = self._readonly(system_body, 7)
        self.hardware_text.grid(row=row + 1, column=0, sticky="ew", pady=(8, 0))

        ws_book = ttk.Notebook(system_body, style="MVP.TNotebook")
        ws_book.grid(row=row + 2, column=0, sticky="ew", pady=(8, 0))
        ft_tab = ttk.Frame(ws_book, style="Card.TFrame", padding=6)
        diff_tab = ttk.Frame(ws_book, style="Card.TFrame", padding=6)
        ws_book.add(ft_tab, text="文件树")
        ws_book.add(diff_tab, text="Diff")
        ft_tab.grid_columnconfigure(0, weight=1)
        ft_tab.grid_rowconfigure(0, weight=1)
        diff_tab.grid_columnconfigure(0, weight=1)
        diff_tab.grid_rowconfigure(0, weight=1)
        self.home_file_tree = self._readonly(ft_tab, 10)
        self.home_file_tree.grid(row=0, column=0, sticky="nsew")
        self.home_diff = self._readonly(diff_tab, 10)
        self.home_diff.grid(row=0, column=0, sticky="nsew")
        self.home_workspace_preview = self.home_file_tree
        self._set_text(self.home_file_tree, f"工作区：{self.loader.workspace_root}\n\n等待工作区快照。")
        self._set_text(self.home_diff, "等待工作区 diff。")

        history_body, row = self._build_sidebar_section(parent, "history", "框架历史", 6)
        hist_actions = tk.Frame(history_body, bg=self.colors["panel"])
        hist_actions.grid(row=row, column=0, sticky="ew")
        ttk.Button(hist_actions, text="刷新", style="Quiet.TButton",
                   command=self.refresh_history_async).pack(side="left")
        ttk.Button(hist_actions, text="打开源文件", style="Quiet.TButton",
                   command=self.open_selected_history_source).pack(side="left", padx=(8, 0))
        self.history_tree = ttk.Treeview(
            history_body,
            columns=("time", "source", "title", "subtitle"),
            show="headings", style="Status.Treeview", height=6)
        for key, title, width in (
            ("time", "时间", 128), ("source", "来源", 84),
            ("title", "主题", 168), ("subtitle", "补充", 170),
        ):
            self.history_tree.heading(key, text=title)
            self.history_tree.column(
                key, width=width, anchor="center" if key in {"time", "source"} else "w")
        self.history_tree.grid(row=row + 1, column=0, sticky="ew", pady=(6, 0))
        self.history_tree.bind("<<TreeviewSelect>>", self._on_history_selected)
        self.history_tree.bind("<Double-1>", self._load_selected_history_into_dashboard)
        self.history_detail = self._readonly(history_body, 6)
        self.history_detail.grid(row=row + 2, column=0, sticky="ew", pady=(6, 0))

    def _build_qa_layout(self, parent: tk.Frame) -> None:
        self.chat_card = tk.Frame(
            parent, bg=self.colors["panel"], padx=16, pady=14,
            highlightthickness=1, highlightbackground=self.colors["line"])
        self.chat_card.grid(row=0, column=0, sticky="nsew")
        self.chat_card.grid_columnconfigure(0, weight=1)
        self.chat_card.grid_rowconfigure(1, weight=1)

        title_row = tk.Frame(self.chat_card, bg=self.colors["panel"])
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.grid_columnconfigure(0, weight=1)
        tk.Label(
            title_row,
            text="MVP Agent 会话",
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=self.fonts["section"],
        ).grid(row=0, column=0, sticky="w")
        tk.Label(
            title_row,
            text="对话、代码回包与编排反馈",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=self.fonts["small"],
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Button(
            title_row,
            textvariable=self.detail_toggle_var,
            style="Quiet.TButton",
            command=self._toggle_detail_panel,
        ).grid(row=0, column=1, rowspan=2, sticky="e")

        conversation_surface = tk.Frame(
            self.chat_card,
            bg="#F8F5F1",
            highlightthickness=1,
            highlightbackground=self.colors["line"],
            padx=10,
            pady=10,
        )
        conversation_surface.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        conversation_surface.grid_columnconfigure(0, weight=1)
        conversation_surface.grid_rowconfigure(0, weight=1)

        self.output_text = ScrolledText(
            conversation_surface,
            wrap="word",
            relief="flat",
            bd=0,
            padx=8,
            pady=8,
            background="#F8F5F1",
            foreground=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground="#E7DDD4",
            font=self.fonts["base"],
        )
        self.output_text.grid(row=0, column=0, sticky="nsew")
        self.output_text.configure(state="disabled")
        self.output_text.bind(
            "<Configure>", lambda _e: self.after_idle(self._refresh_chat_tags))
        self.live_stream = self.output_text
        self._refresh_chat_tags()

        self.detail_card = tk.Frame(
            parent, bg=self.colors["panel"], padx=16, pady=12,
            highlightthickness=1, highlightbackground=self.colors["line"])
        self.detail_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.detail_card.grid_columnconfigure(0, weight=1)
        self.detail_body = tk.Frame(self.detail_card, bg=self.colors["panel"])
        self.detail_body.grid(row=0, column=0, sticky="ew")
        self.detail_body.grid_columnconfigure(0, weight=1)

        detail_book = ttk.Notebook(self.detail_body, style="MVP.TNotebook")
        detail_book.grid(row=0, column=0, sticky="ew")

        exec_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(exec_tab, text="任务详情")
        exec_tab.grid_columnconfigure(0, weight=1)
        exec_tab.grid_rowconfigure(0, weight=1)
        exec_tab.grid_rowconfigure(1, weight=1)
        self.assign_tree = ttk.Treeview(
            exec_tab,
            columns=("task", "kind", "worker", "result", "review"),
            show="headings", style="Status.Treeview", height=7)
        for key, title, width in (
            ("task", "子任务", 260), ("kind", "类型", 86),
            ("worker", "执行成员", 156), ("result", "结果", 90),
            ("review", "验收", 90),
        ):
            self.assign_tree.heading(key, text=title)
            self.assign_tree.column(key, width=width, anchor="center" if key != "task" else "w")
        self.assign_tree.grid(row=0, column=0, sticky="ew")
        self.assign_tree.bind("<<TreeviewSelect>>", self._on_assignment_selected)
        self.assign_detail = self._readonly(exec_tab, 8)
        self.assign_detail.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        timeline_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(timeline_tab, text="子任务时间线")
        timeline_tab.grid_columnconfigure(0, weight=1)
        timeline_tab.grid_rowconfigure(0, weight=1)
        timeline_tab.grid_rowconfigure(1, weight=1)
        self.timeline_tree = ttk.Treeview(
            timeline_tab,
            columns=("time", "phase", "task", "worker"),
            show="headings", style="Status.Treeview", height=7)
        for key, title, width in (
            ("time", "时间", 90),
            ("phase", "阶段", 110),
            ("task", "任务", 270),
            ("worker", "成员", 150),
        ):
            self.timeline_tree.heading(key, text=title)
            self.timeline_tree.column(key, width=width, anchor="center" if key in {"time", "phase"} else "w")
        self.timeline_tree.grid(row=0, column=0, sticky="ew")
        self.timeline_tree.bind("<<TreeviewSelect>>", self._on_timeline_selected)
        self.timeline_detail = self._readonly(timeline_tab, 7)
        self.timeline_detail.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self._set_text(self.timeline_detail, "当前没有子任务时间线。")

        raw_tab = ttk.Frame(detail_book, style="Card.TFrame", padding=8)
        detail_book.add(raw_tab, text="原始 JSON")
        raw_tab.grid_columnconfigure(0, weight=1)
        raw_tab.grid_rowconfigure(0, weight=1)
        self.raw_json = self._readonly(raw_tab, 18)
        self.raw_json.grid(row=0, column=0, sticky="nsew")

        self.composer_card = tk.Frame(
            parent, bg=self.colors["panel"], padx=16, pady=14,
            highlightthickness=1, highlightbackground=self.colors["line"])
        self.composer_card.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self.composer_card.grid_columnconfigure(0, weight=1)
        tk.Label(self.composer_card, text="输入编码目标", bg=self.colors["panel"],
                 fg=self.colors["text"], font=self.fonts["section"]).grid(
            row=0, column=0, sticky="w")
        tk.Label(
            self.composer_card,
            text="直接描述目标、范围、约束或贴上待修改文件。",
            bg=self.colors["panel"], fg=self.colors["muted"],
            font=self.fonts["small"]).grid(row=1, column=0, sticky="w", pady=(2, 8))
        self.task_text = ScrolledText(
            self.composer_card, height=4, wrap="word", relief="flat", bd=1,
            padx=14, pady=12, background=self.colors["input"],
            foreground=self.colors["text"],
            insertbackground=self.colors["text"])
        self.task_text.grid(row=2, column=0, sticky="ew")
        self.task_text.configure(
            highlightthickness=1, highlightbackground=self.colors["line"],
            highlightcolor=self.colors["accent"])
        self.task_text.bind("<KeyPress>", self._on_task_key_press)
        self.task_text.bind("<Button-1>", self._on_task_click)
        self.task_text.bind("<FocusOut>", self._on_task_focus_out)
        self.task_text.bind("<KeyRelease>", lambda _e: self.after_idle(self._refresh_header))
        self._show_task_placeholder()

        action_row = tk.Frame(self.composer_card, bg=self.colors["panel"])
        action_row.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        action_row.grid_columnconfigure(0, weight=1)
        primary = tk.Frame(action_row, bg=self.colors["panel"])
        primary.grid(row=0, column=0, sticky="w")
        self.run_button = ttk.Button(
            primary, text="开始编码", style="Accent.TButton",
            command=self.run_task_async)
        self.run_button.pack(side="left")
        self.plan_button = ttk.Button(
            primary, text="只做规划", style="Quiet.TButton",
            command=self.plan_task_async)
        self.plan_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(
            primary, text="停止运行", style="Quiet.TButton",
            command=self.cancel_current_task)
        self.cancel_button.pack(side="left", padx=(8, 0))

        composer_status = tk.Frame(action_row, bg=self.colors["panel"])
        composer_status.grid(row=0, column=1, sticky="e")
        tk.Label(
            composer_status,
            textvariable=self.queue_state_var,
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=self.fonts["small"],
        ).pack(anchor="e")
        tk.Label(
            composer_status,
            textvariable=self.header_worker_var,
            bg=self.colors["panel"],
            fg=self.colors["text"],
            font=self.fonts["small"],
        ).pack(anchor="e", pady=(2, 0))
        tk.Label(
            self.composer_card,
            text="快捷键：Ctrl+Enter 开始编码    Ctrl+Shift+Enter 只做规划",
            bg=self.colors["panel"],
            fg=self.colors["muted"],
            font=self.fonts["small"],
        ).grid(row=4, column=0, sticky="e", pady=(8, 0))

        self._set_detail_panel_expanded(self.detail_panel_expanded)

    def _toggle_detail_panel(self) -> None:
        self._set_detail_panel_expanded(not self.detail_panel_expanded)

    def _set_detail_panel_expanded(self, expanded: bool) -> None:
        self.detail_panel_expanded = expanded
        if expanded:
            self.detail_body.grid()
            self.detail_toggle_var.set("收起任务细节")
        else:
            self.detail_body.grid_remove()
            self.detail_toggle_var.set("展开任务细节")
        self._save_ui_state()

    def _show_task_placeholder(self) -> None:
        if not hasattr(self, "task_text"):
            return
        self.task_placeholder_visible = True
        self.task_text.configure(foreground=self.colors["muted"])
        self.task_text.delete("1.0", "end")
        self.task_text.insert("1.0", self.task_placeholder)
        self.task_text.mark_set("insert", "1.0")

    def _clear_task_placeholder(self) -> None:
        if not hasattr(self, "task_text") or not self.task_placeholder_visible:
            return
        self.task_placeholder_visible = False
        self.task_text.configure(foreground=self.colors["text"])
        self.task_text.delete("1.0", "end")

    def _on_task_click(self, _event: tk.Event) -> None:
        self._clear_task_placeholder()

    def _on_task_key_press(self, event: tk.Event) -> None:
        navigation_keys = {
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
        }
        if self.task_placeholder_visible and event.keysym not in navigation_keys:
            self._clear_task_placeholder()
        self.after_idle(self._refresh_header)

    def _on_task_focus_out(self, _event: tk.Event) -> None:
        self.after_idle(self._restore_placeholder_if_empty)

    def _restore_placeholder_if_empty(self) -> None:
        if not hasattr(self, "task_text"):
            return
        content = self.task_text.get("1.0", "end").strip()
        if not content:
            self._show_task_placeholder()

    def _get_task_value(self) -> str:
        if not hasattr(self, "task_text"):
            return ""
        content = self.task_text.get("1.0", "end").strip()
        if self.task_placeholder_visible:
            if not content or content == self.task_placeholder:
                return ""
            self.task_placeholder_visible = False
            self.task_text.configure(foreground=self.colors["text"])
        return content

    def _refresh_chat_tags(self) -> None:
        if not hasattr(self, "output_text"):
            return
        width = max(self.output_text.winfo_width(), 720)
        user_left = max(int(width * 0.26), 180)
        assistant_right = max(int(width * 0.2), 140)
        meta_common = {
            "spacing1": 10,
            "spacing3": 2,
            "font": self.fonts["small"],
        }
        bubble_common = {
            "spacing1": 0,
            "spacing3": 14,
            "relief": "flat",
            "font": self.fonts["base"],
        }
        self.output_text.tag_configure(
            "user_meta",
            foreground=self.colors["muted"],
            justify="right",
            lmargin1=user_left,
            lmargin2=user_left,
            rmargin=20,
            **meta_common,
        )
        self.output_text.tag_configure(
            "user_bubble",
            background="#F8EAEC",
            foreground=self.colors["text"],
            lmargin1=user_left,
            lmargin2=user_left,
            rmargin=20,
            **bubble_common,
        )
        self.output_text.tag_configure(
            "leader_meta",
            foreground=self.colors["muted"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **meta_common,
        )
        self.output_text.tag_configure(
            "leader_bubble",
            background="#F0EAE3",
            foreground=self.colors["text"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **bubble_common,
        )
        self.output_text.tag_configure(
            "system_meta",
            foreground=self.colors["muted"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **meta_common,
        )
        self.output_text.tag_configure(
            "system_bubble",
            background="#F7F1EA",
            foreground=self.colors["muted"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **bubble_common,
        )
        self.output_text.tag_configure(
            "milestone_meta",
            foreground=self.colors["accent_dark"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **meta_common,
        )
        self.output_text.tag_configure(
            "milestone_bubble",
            background="#F8EAEC",
            foreground=self.colors["accent_dark"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **bubble_common,
        )
        self.output_text.tag_configure(
            "summary_meta",
            foreground=self.colors["good"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **meta_common,
        )
        self.output_text.tag_configure(
            "summary_bubble",
            background="#ECF5F0",
            foreground=self.colors["good"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **bubble_common,
        )
        self.output_text.tag_configure(
            "error_meta",
            foreground=self.colors["bad"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **meta_common,
        )
        self.output_text.tag_configure(
            "error_bubble",
            background="#FAECEB",
            foreground=self.colors["bad"],
            lmargin1=18,
            lmargin2=18,
            rmargin=assistant_right,
            **bubble_common,
        )
        code_common = {
            "font": self.fonts["mono"],
            "spacing1": 3,
            "spacing3": 10,
            "tabs": ("1c",),
        }
        for style, left, right in (
            ("user", user_left, 20),
            ("leader", 18, assistant_right),
            ("system", 18, assistant_right),
            ("milestone", 18, assistant_right),
            ("summary", 18, assistant_right),
            ("error", 18, assistant_right),
        ):
            self.output_text.tag_configure(
                f"{style}_code_lang",
                foreground=self.colors["muted"],
                font=self.fonts["small"],
                lmargin1=left,
                lmargin2=left,
                rmargin=right,
                spacing1=6,
                spacing3=0,
            )
            self.output_text.tag_configure(
                f"{style}_code",
                background="#1F1A17",
                foreground="#F6F1EB",
                lmargin1=left,
                lmargin2=left,
                rmargin=right,
                **code_common,
            )

    def _build_footer(self, row: int) -> None:
        bar = tk.Frame(self, bg=self.colors["bg"], padx=16, pady=6)
        bar.grid(row=row, column=0, sticky="ew")
        bar.grid_columnconfigure(0, weight=1)
        tk.Label(bar,
                 text=f"工作区：{self.loader.workspace_root}",
                 bg=self.colors["bg"], fg=self.colors["muted"],
                 font=self.fonts["small"]).grid(row=0, column=0, sticky="w")
        tk.Label(bar,
                 text=f"报告目录：{self.loader.runs_dir}",
                 bg=self.colors["bg"], fg=self.colors["muted"],
                 font=self.fonts["small"]).grid(row=0, column=1, sticky="e")

    # ── helper widgets ───────────────────────────────────────────────

    def _readonly(self, parent: tk.Misc, height: int) -> ScrolledText:
        box = ScrolledText(
            parent, height=height, wrap="word", relief="flat", bd=1,
            padx=10, pady=8, background=self.colors["panel_alt"],
            foreground=self.colors["text"],
            insertbackground=self.colors["text"])
        box.configure(highlightthickness=1,
                       highlightbackground=self.colors["line"],
                       highlightcolor=self.colors["accent"])
        box.configure(state="disabled")
        return box

    def _set_text(self, widget: ScrolledText, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _bind_canvas_wheel(self, canvas: tk.Canvas) -> None:
        def on_wheel(event: object) -> None:
            delta = 0
            if getattr(event, "delta", 0):
                delta = -int(getattr(event, "delta") / 120)
            elif getattr(event, "num", 0) == 4:
                delta = -1
            elif getattr(event, "num", 0) == 5:
                delta = 1
            if delta:
                canvas.yview_scroll(delta, "units")

        def bind(_e: object = None) -> None:
            self.bind_all("<MouseWheel>", on_wheel)
            self.bind_all("<Button-4>", on_wheel)
            self.bind_all("<Button-5>", on_wheel)

        def unbind(_e: object = None) -> None:
            self.unbind_all("<MouseWheel>")
            self.unbind_all("<Button-4>")
            self.unbind_all("<Button-5>")

        canvas.bind("<Enter>", bind)
        canvas.bind("<Leave>", unbind)

    # ── output methods ────────────────────────────────────────────────

    def _append_output(self, role: str, message: str) -> None:
        text = message.strip()
        if not text:
            return
        timestamp = iso_now_local().split("T")[-1][:8]
        labels = {
            "user": "你",
            "leader": "MVP Agent",
            "system": "系统",
            "error": "错误",
            "milestone": "阶段节点",
            "summary": "代码回包",
        }
        style_map = {
            "user": "user",
            "leader": "leader",
            "system": "system",
            "error": "error",
            "milestone": "milestone",
            "summary": "summary",
        }
        style = style_map.get(role, "system")
        entry = {
            "label": labels.get(role, "系统"),
            "timestamp": timestamp,
            "style": style,
            "text": text,
        }
        if self._should_stream_output(role, text):
            self._output_queue.append(entry)
            self._process_output_queue()
            return
        self._render_output_entry(entry)

    def _should_stream_output(self, role: str, text: str) -> bool:
        try:
            viewable = self.winfo_viewable()
        except Exception:
            viewable = False
        if not viewable:
            return False
        return bool(self._output_stream_active or self._output_queue or (role != "user" and len(text) >= 24))

    def _process_output_queue(self) -> None:
        if self._output_stream_active or not self._output_queue:
            return
        entry = self._output_queue.pop(0)
        self._output_stream_active = True
        self._output_stream_state = {
            "entry": entry,
            "segments": self._split_output_segments(str(entry["text"])),
            "segment_index": 0,
            "offset": 0,
            "header_done": False,
        }
        self._render_output_stream_step()

    def _render_output_entry(self, entry: dict[str, Any]) -> None:
        self.output_text.configure(state="normal")
        self._render_output_header(str(entry["label"]), str(entry["timestamp"]), str(entry["style"]))
        self._render_output_segments_now(
            self._split_output_segments(str(entry["text"])),
            str(entry["style"]),
        )
        self.output_text.see("end")
        self.output_text.configure(state="disabled")
        self._trim_output()

    def _render_output_header(self, label: str, timestamp: str, style: str) -> None:
        meta_tag = f"{style}_meta"
        if self.output_text.index("end-1c") != "1.0":
            self.output_text.insert("end", "\n")
        self.output_text.insert("end", f"{label}  {timestamp}\n", meta_tag)

    def _split_output_segments(self, text: str) -> list[dict[str, str]]:
        pattern = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<code>.*?)```", re.DOTALL)
        segments: list[dict[str, str]] = []
        cursor = 0
        for match in pattern.finditer(text):
            prefix = text[cursor:match.start()]
            if prefix:
                segments.append({"type": "text", "text": prefix})
            segments.append(
                {
                    "type": "code",
                    "language": match.group("lang").strip(),
                    "code": match.group("code").rstrip("\n"),
                }
            )
            cursor = match.end()
        suffix = text[cursor:]
        if suffix:
            segments.append({"type": "text", "text": suffix})
        return segments or [{"type": "text", "text": text}]

    def _render_output_segments_now(
        self,
        segments: list[dict[str, str]],
        style: str,
    ) -> None:
        for segment in segments:
            if segment.get("type") == "code":
                self._insert_code_segment(
                    style,
                    segment.get("language", ""),
                    segment.get("code", ""),
                )
            else:
                self.output_text.insert("end", segment.get("text", ""), f"{style}_bubble")
        self.output_text.insert("end", "\n", f"{style}_bubble")

    def _render_output_stream_step(self) -> None:
        state = self._output_stream_state
        if not state:
            self._output_stream_active = False
            return
        entry = state["entry"]
        style = str(entry["style"])
        if not state["header_done"]:
            self.output_text.configure(state="normal")
            self._render_output_header(str(entry["label"]), str(entry["timestamp"]), style)
            state["header_done"] = True
            self.output_text.configure(state="disabled")

        segments: list[dict[str, str]] = state["segments"]
        segment_index = int(state["segment_index"])
        if segment_index >= len(segments):
            self.output_text.configure(state="normal")
            self.output_text.insert("end", "\n", f"{style}_bubble")
            self.output_text.see("end")
            self.output_text.configure(state="disabled")
            self._trim_output()
            self._output_stream_state = None
            self._output_stream_active = False
            self._process_output_queue()
            return

        segment = segments[segment_index]
        self.output_text.configure(state="normal")
        if segment.get("type") == "code":
            self._insert_code_segment(
                style,
                segment.get("language", ""),
                segment.get("code", ""),
            )
            state["segment_index"] = segment_index + 1
            state["offset"] = 0
            delay = 26
        else:
            content = segment.get("text", "")
            offset = int(state["offset"])
            chunk = content[offset: offset + 22]
            self.output_text.insert("end", chunk, f"{style}_bubble")
            state["offset"] = offset + len(chunk)
            if state["offset"] >= len(content):
                state["segment_index"] = segment_index + 1
                state["offset"] = 0
            delay = 12 if chunk and chunk[-1] not in {".", "。", "!", "！", "?", "？", "\n"} else 22
        self.output_text.see("end")
        self.output_text.configure(state="disabled")
        self._schedule_after(delay, self._render_output_stream_step)

    def _insert_code_segment(self, style: str, language: str, code: str) -> None:
        bubble_tag = f"{style}_bubble"
        if language or code:
            self.output_text.insert("end", "\n", bubble_tag)
        if language:
            self.output_text.insert("end", f"{language}\n", f"{style}_code_lang")
        if code:
            self._insert_copy_button(code)
            self.output_text.insert("end", "\n")
            self.output_text.insert("end", f"{code}\n", f"{style}_code")

    def _insert_copy_button(self, code: str) -> None:
        button = ttk.Button(
            self.output_text,
            text="复制代码",
            style="Mini.Quiet.TButton",
            command=lambda current=code: self._copy_code_block(current),
        )
        self.output_embeds.append(button)
        self.output_text.window_create("end", window=button)

    def _copy_code_block(self, code: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(code)
            self.update_idletasks()
            self.progress_var.set("已复制代码块到剪贴板")
        except Exception:
            self.progress_var.set("复制代码块失败")

    def _trim_output(self) -> None:
        lines = int(self.output_text.index("end-1c").split(".")[0])
        if lines > self.OUTPUT_MAX_LINES:
            self.output_text.configure(state="normal")
            self.output_text.delete("1.0", f"{lines - self.OUTPUT_MAX_LINES + 100}.0")
            self.output_text.configure(state="disabled")

    def _append_live_stream(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type", "")).strip()
        message = str(payload.get("message", "")).strip()
        if not message:
            return
        phase = self._phase_label(event_type)
        task_label = str(payload.get("task_id") or "").strip()
        worker_label = str(payload.get("worker_id")
                           or payload.get("reviewer_worker") or "").strip()
        parts = [phase]
        if task_label:
            parts.append(task_label)
        if worker_label:
            parts.append(worker_label)
        role = "milestone" if event_type in {
            "planning_completed", "run_completed", "batch_started",
            "assignment_completed", "review_completed"} else "system"
        self._append_output(role, " · ".join(parts) + f" · {message}")

    def _record_timeline_event(self, payload: dict[str, Any]) -> None:
        event_type = str(payload.get("type", "")).strip()
        if not event_type:
            return
        message = str(payload.get("message", "")).strip()
        timestamp = iso_now_local().split("T")[-1][:8]
        task_label = str(payload.get("task_id") or payload.get("title") or payload.get("plan_summary") or "-").strip()
        worker_label = str(payload.get("worker_id") or payload.get("reviewer_worker") or payload.get("next_worker") or "-").strip()
        iid = f"tl-{len(self.timeline_rows):03d}"
        row = {
            "timestamp": timestamp,
            "phase": self._phase_label(event_type),
            "task": task_label,
            "worker": worker_label,
            "message": message,
            "payload": payload,
        }
        self.timeline_rows[iid] = row
        if hasattr(self, "timeline_tree"):
            self.timeline_tree.insert(
                "",
                "end",
                iid=iid,
                values=(timestamp, row["phase"], safe_snippet(task_label, 34), safe_snippet(worker_label, 20)),
            )
            self.timeline_tree.yview_moveto(1.0)
            if len(self.timeline_rows) == 1:
                self.timeline_tree.selection_set(iid)
                self._on_timeline_selected()

    def _reset_live_views(self) -> None:
        self.timeline_rows.clear()
        if hasattr(self, "timeline_tree"):
            for iid in self.timeline_tree.get_children():
                self.timeline_tree.delete(iid)
        if hasattr(self, "timeline_detail"):
            self._set_text(self.timeline_detail, "当前没有子任务时间线。")

    def _phase_label(self, event_type: str) -> str:
        mapping = {
            "run_started": "开始接管",
            "planning_started": "开始规划",
            "planning_completed": "规划完成",
            "batch_started": "并行批次",
            "assignment_started": "开始执行",
            "assignment_completed": "执行完成",
            "assignment_rerouted": "改派工",
            "review_started": "开始验收",
            "review_completed": "验收完成",
            "run_completed": "运行完成",
            "run_cancelled": "已取消",
        }
        return mapping.get(event_type, event_type or "事件")

    def _on_timeline_selected(self, _event=None) -> None:
        if not hasattr(self, "timeline_tree"):
            return
        selection = self.timeline_tree.selection()
        if not selection:
            return
        row = self.timeline_rows.get(selection[0], {})
        if not row:
            return
        self._set_text(
            self.timeline_detail,
            "\n".join(
                [
                    f"时间：{row.get('timestamp', '-')}",
                    f"阶段：{row.get('phase', '-')}",
                    f"任务：{row.get('task', '-')}",
                    f"成员：{row.get('worker', '-')}",
                    "",
                    f"消息：{row.get('message', '-')}",
                ]
            ),
        )

    # ── control plane refresh ────────────────────────────────────────

    def _refresh_header(self) -> None:
        explicit_mode = self.PROFILE_LABELS.get(self.profile_var.get(),
                                                 "balanced")
        effective_mode = self._effective_profile_name()
        if hasattr(self, "progress_var"):
            pass  # updated elsewhere

    def _refresh_control_plane_panels(self) -> None:
        """Now only refreshes header chips; detail panels updated on demand."""
        pass

    def _set_summary_text(self, text: str) -> None:
        self._set_text(self.home_summary, text)

    def _format_skill_label(self, skill_id: str) -> str:
        normalized = str(skill_id).strip()
        if not normalized:
            return "-"
        label = self.CORE_SKILL_LABELS.get(normalized)
        if label:
            return f"{label} · {normalized}"
        return normalized

    def _format_capability_gap(self, gap: str) -> str:
        normalized = str(gap).strip()
        if not normalized:
            return "-"
        match = re.search(r"Missing strong ([a-zA-Z_]+) coverage", normalized)
        if not match:
            return normalized
        capability = match.group(1).strip().lower()
        capability_label = self.CAPABILITY_LABELS.get(capability, capability)
        return f"当前成员网缺少较强的{capability_label}能力覆盖。"

    def _extract_plan_skill_mesh(
        self,
        plan: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        required: list[str] = []
        for item in plan.get("required_skills", []):
            if isinstance(item, str) and item.strip():
                required.append(item.strip())
        subtasks = plan.get("subtasks", [])
        if not isinstance(subtasks, list):
            subtasks = []
        if not required:
            for subtask in subtasks:
                if not isinstance(subtask, dict):
                    continue
                for skill_id in subtask.get("required_skills", []):
                    if isinstance(skill_id, str) and skill_id.startswith("mvp-core:"):
                        required.append(skill_id)
        core_skills: list[str] = []
        external_counter: Counter[str] = Counter()
        for skill_id in required:
            if skill_id.startswith("mvp-core:"):
                core_skills.append(skill_id)
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                continue
            for skill_id in subtask.get("required_skills", []):
                if not isinstance(skill_id, str):
                    continue
                normalized = skill_id.strip()
                if not normalized or normalized.startswith("mvp-core:"):
                    continue
                external_counter[normalized] += 1
        doctrine = [
            item.strip()
            for item in plan.get("doctrine", [])
            if isinstance(item, str) and item.strip()
        ]
        gaps = [
            self._format_capability_gap(item)
            for item in plan.get("capability_gaps", [])
            if isinstance(item, str) and item.strip()
        ]
        external_skills = [skill_id for skill_id, _ in external_counter.most_common(6)]
        return (
            list(dict.fromkeys(core_skills)),
            list(dict.fromkeys(doctrine)),
            external_skills,
            list(dict.fromkeys(gaps)),
        )

    def _build_skill_mesh_text(self, plan: dict[str, Any] | None) -> str:
        if not isinstance(plan, dict) or not plan:
            return "等待任务后显示本轮核心 skills、协作原则、公开候选与能力缺口。"
        core_skills, doctrine, external_skills, gaps = self._extract_plan_skill_mesh(plan)
        lines = [f"核心 skills（{len(core_skills)}）"]
        if core_skills:
            lines.extend(f"• {self._format_skill_label(skill_id)}" for skill_id in core_skills[:6])
        else:
            lines.append("• 当前计划还没有显式声明核心 skills。")
        lines.append("")
        lines.append("协作原则")
        if doctrine:
            lines.extend(f"• {item}" for item in doctrine[:4])
        else:
            lines.append("• 当前计划未声明额外的协作原则。")
        lines.append("")
        lines.append("公开候选 skills")
        if external_skills:
            lines.extend(f"• {skill_id}" for skill_id in external_skills)
        else:
            lines.append("• 当前没有高相关公开 skills 需要补充。")
        lines.append("")
        lines.append("能力缺口")
        if gaps:
            lines.extend(f"• {gap}" for gap in gaps[:4])
        else:
            lines.append("• 当前成员网已覆盖本轮关键能力。")
        return "\n".join(lines)

    def _apply_skill_mesh_summary(self, plan: dict[str, Any] | None) -> None:
        if hasattr(self, "home_skill_mesh"):
            self._set_text(self.home_skill_mesh, self._build_skill_mesh_text(plan))

    def _worker_display_name(self, worker_id: str) -> str:
        spec = self.loader.worker_specs.get(worker_id)
        return spec.display_name if spec is not None else worker_id

    def _effective_profile_name(self) -> str:
        explicit_mode = self.PROFILE_LABELS.get(self.profile_var.get(),
                                                 "balanced")
        task = self._get_task_value()
        effective_mode = explicit_mode
        if self.hint_var.get() and task:
            hinted = detect_budget_mode(task, None)
            if hinted != "balanced" or explicit_mode == "balanced":
                effective_mode = hinted
        return effective_mode

    # ── background task dispatching ───────────────────────────────────

    def _bg(self, name: str, func: Callable[[], object],
            result_type: str) -> None:
        def runner() -> None:
            try:
                self.queue.put((result_type, func()))
            except Exception as exc:
                self.queue.put(
                    ("error", {"title": f"{name} 失败",
                                "message": str(exc),
                                "trace": traceback.format_exc()}))

        threading.Thread(target=runner, daemon=True).start()

    def refresh_history_async(self) -> None:
        self._bg("历史刷新",
                 lambda: self.loader.framework_timeline(limit=80), "history")

    def refresh_workspace_async(self) -> None:
        self._bg("工作区快照",
                 lambda: collect_workspace_snapshot(self.loader.workspace_root),
                 "workspace")

    def refresh_health_async(self) -> None:
        self._bg("状态刷新", self.loader.health, "health")

    def run_smoke_test_async(self) -> None:
        self._bg("深度联调", self.loader.smoke_test, "smoke")

    def refresh_stats_async(self) -> None:
        self._bg("硬件监控", self.loader.system_snapshot, "stats")

    def run_quick_ping_async(self) -> None:
        self._bg("快速联调", self.loader.quick_ping, "ping")

    def run_deep_ping_async(self) -> None:
        self._bg("深度联调", self.loader.deep_ping, "ping")

    def run_task_async(self) -> None:
        self._start_run(False)

    def plan_task_async(self) -> None:
        self._start_run(True)

    # ── job lifecycle ─────────────────────────────────────────────────

    def _start_run(self, plan_only: bool) -> None:
        task = self._get_task_value()
        if not task:
            messagebox.showinfo("缺少任务", "请先输入任务。")
            return
        explicit_mode = self.PROFILE_LABELS.get(self.profile_var.get(),
                                                 "balanced")
        mode = explicit_mode
        if self.hint_var.get():
            hinted = detect_budget_mode(task, None)
            if hinted != "balanced" or explicit_mode == "balanced":
                mode = hinted
        self._append_output("user", task)
        job = self._create_job(task=task, mode=mode, plan_only=plan_only)
        self.pending_job_ids.append(job.job_id)
        self._render_job_queue()
        if self.job_running:
            self._append_output(
                "system",
                f"任务已加入队列：{job.job_id}。当前编码任务结束后会自动接力。")
            return
        self._launch_next_job()

    def _launch_next_job(self) -> None:
        if self.job_running or not self.pending_job_ids:
            self._sync_controls()
            return
        job_id = self.pending_job_ids.pop(0)
        job = self.jobs[job_id]
        job.status = "running"
        self.current_job_id = job_id
        self.current_run_control = RunControl()
        self.job_running = True
        self.status_var.set("运行中")
        self.progressbar.configure(value=0)
        self.progress_var.set("MVP 正在接管任务")
        self._apply_pet_mood(MOODS["planning"], note="收到新任务，正在梳理目标与分工。")
        self._reset_live_views()
        self._apply_skill_mesh_summary(None)
        if hasattr(self, "home_skill_mesh"):
            self._set_text(
                self.home_skill_mesh,
                "Skill Mesh 正在分析本轮任务的核心 skills、公开候选与能力缺口。",
            )
        self._render_job_queue()
        self._sync_controls()
        self._append_output(
            "leader",
            f"开始处理任务 {job.job_id}：{safe_snippet(job.task, 90)}")

        def runner() -> None:
            try:
                report = self.loader.run(
                    task=job.task,
                    profile_name=job.mode,
                    plan_only=job.plan_only,
                    run_control=self.current_run_control,
                    event_callback=lambda event: self.queue.put(
                        ("run_event", {"job_id": job.job_id, "event": event})),
                )
                self.queue.put(
                    ("run_report",
                     {"job_id": job.job_id, "report": report.to_dict()}))
            except RunCancelled as exc:
                self.queue.put(
                    ("run_cancelled",
                     {"job_id": job.job_id, "message": str(exc)}))
            except Exception as exc:
                self.queue.put(
                    ("error",
                     {"job_id": job.job_id, "title": "任务执行失败",
                      "message": str(exc),
                      "trace": traceback.format_exc()}))
            finally:
                self.queue.put(("run_finished", {"job_id": job.job_id}))

        threading.Thread(target=runner, daemon=True).start()

    def _create_job(self, task: str, mode: str, plan_only: bool) -> QueueJob:
        self.job_counter += 1
        job_id = f"J{self.job_counter:03d}"
        job = QueueJob(job_id=job_id, task=task, mode=mode,
                        plan_only=plan_only)
        self.jobs[job_id] = job
        self.job_order.append(job_id)
        return job

    def _sync_controls(self) -> None:
        self.run_button.configure(state="normal")
        self.plan_button.configure(state="normal")
        self.refresh_button.configure(state="normal")
        self.cancel_button.configure(
            state="normal" if self.job_running and self.current_job_id
            else "disabled")
        self.quick_ping_button.configure(
            state="disabled" if self.job_running else "normal")
        self.deep_ping_button.configure(
            state="disabled" if self.job_running else "normal")
        selected = self.queue_tree.selection()
        removable = False
        if selected:
            job = self.jobs.get(selected[0])
            removable = bool(job and job.status == "queued")
        self.queue_remove_button.configure(
            state="normal" if removable else "disabled")

    def cancel_current_task(self) -> None:
        if (not self.job_running or not self.current_job_id
                or self.current_run_control is None):
            messagebox.showinfo("没有运行中的任务", "当前没有可取消的任务。")
            return
        job = self.jobs[self.current_job_id]
        job.status = "cancelled"
        job.note = "用户在 GUI 中请求取消。"
        self.current_run_control.request_cancel(
            "收到取消请求。MVP 正在停止当前派工并保留已完成进度。")
        self.status_var.set("取消中")
        self.progress_var.set("已收到取消请求，正在等待当前成员安全停下。")
        self._append_output("system", f"正在中断任务 {job.job_id}。")
        self._render_job_queue()

    def remove_selected_queue_job(self) -> None:
        selection = self.queue_tree.selection()
        if not selection:
            messagebox.showinfo("没有选中项", "请先选中一个待执行任务。")
            return
        job_id = selection[0]
        job = self.jobs.get(job_id)
        if not job or job.status != "queued":
            messagebox.showinfo("无法移除", "只能移除尚未开始的待执行任务。")
            return
        self.pending_job_ids = [
            item for item in self.pending_job_ids if item != job_id]
        job.status = "cancelled"
        job.note = "已从队列移除。"
        self._render_job_queue()
        self._set_text(self.queue_detail,
                        f"{job.job_id} 已从待执行队列移除。")

    # ── event poll loop ───────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                name, payload = self.queue.get_nowait()
                if name == "run_event":
                    self._run_event(payload)
                elif name == "run_report":
                    self._apply_report(payload)
                elif name == "run_cancelled":
                    self._apply_cancelled(payload)
                elif name == "run_finished":
                    self._finish_job(payload)
                elif name == "health":
                    self._apply_health(payload)
                elif name == "ping":
                    self._apply_ping(payload)
                elif name == "smoke":
                    self._apply_ping(payload)
                elif name == "history":
                    self._apply_history(payload)
                elif name == "workspace":
                    self._apply_workspace(payload)
                elif name == "stats":
                    self._apply_stats(payload)
                elif name == "error":
                    self._error(payload)
        except Empty:
            pass
        if self.winfo_exists():
            self._schedule_after(self.POLL_MS, self._poll_queue)

    # ── event handlers ────────────────────────────────────────────────

    def _run_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", ""))
        if job_id and job_id != self.current_job_id:
            return
        payload = payload.get("event", payload)
        if not isinstance(payload, dict):
            return
        if "progress" in payload:
            progress = max(0.0, min(100.0,
                           float(payload.get("progress", 0.0) or 0.0) * 100.0))
            self.progressbar.configure(value=progress)
        self.progress_var.set(
            str(payload.get("message", "MVP 正在推进")).strip())
        event_type = str(payload.get("type", ""))
        self._apply_pet_mood(
            mood_from_event(
                event_type,
                status=str(payload.get("status", "")),
                decision=str(payload.get("decision", "")),
            ),
            note=str(payload.get("message", "")).strip() or None,
            restart=event_type in {"planning_started", "assignment_started", "review_started", "run_completed", "run_cancelled"},
        )
        self._append_live_stream(payload)
        self._record_timeline_event(payload)
        if event_type == "planning_completed":
            self._render_plan(payload)
        if event_type in {
            "planning_started", "planning_completed", "batch_started",
            "assignment_started", "assignment_completed",
            "assignment_rerouted", "review_started", "review_completed",
            "run_completed", "run_cancelled",
        }:
            role = ("leader"
                    if event_type in {"planning_completed", "run_completed"}
                    else "system")
            self._append_output(role,
                                str(payload.get("message", event_type)))

    def _apply_report(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", ""))
        report_payload = payload.get("report", payload)
        if job_id and job_id in self.jobs:
            job = self.jobs[job_id]
            status = str(report_payload.get("status", "unknown"))
            job.status = status
            job.note = str(report_payload.get("leader_notes", "")).strip()
        if job_id and job_id != self.current_job_id:
            return
        if not isinstance(report_payload, dict):
            return
        self.current_report_payload = report_payload
        status = str(report_payload.get("status", "unknown"))
        report_path = str(report_payload.get("report_path", "")).strip()
        self.status_var.set(self.STATUS_LABELS.get(status, status))
        self._apply_pet_mood(
            mood_from_report_status(status),
            note=str(report_payload.get("leader_notes", "")).strip() or str(report_payload.get("plan", {}).get("summary", "")).strip() or None,
        )
        self.last_report_var.set(
            f"最近报告：{Path(report_path).name if report_path else '未保存'}")
        self.progressbar.configure(value=100)
        self.progress_var.set(
            "本轮任务已完成" if status != "cancelled" else "本轮任务已取消")
        plan = (report_payload.get("plan", {})
                if isinstance(report_payload.get("plan"), dict) else {})
        core_skills, _doctrine, _external_skills, gaps = self._extract_plan_skill_mesh(plan)
        summary = "\n".join([
            f"模式：{self.PROFILE_NAMES.get(str(report_payload.get('profile', 'balanced')), str(report_payload.get('profile', 'balanced')))}",
            f"状态：{self.STATUS_LABELS.get(status, status)}",
            f"计划摘要：{plan.get('summary', '-')}",
            f"执行策略：{plan.get('execution_strategy', '-')}",
            f"Skill Mesh：{len(core_skills)} 个核心 skills，{len(gaps)} 个能力缺口",
            f"规划耗时：{self._perf_value(report_payload, 'planning_ms')}",
            f"执行耗时：{self._perf_value(report_payload, 'execution_ms')}",
            f"验收耗时：{self._perf_value(report_payload, 'review_ms')}",
            f"总耗时：{self._perf_value(report_payload, 'total_ms')}",
            "",
            f"领导结论：{report_payload.get('leader_notes', '-')}",
        ])
        self._set_summary_text(summary)
        self._apply_skill_mesh_summary(plan)
        self._set_text(
            self.raw_json,
            json.dumps(report_payload, ensure_ascii=False, indent=2))
        self._render_report_assignments(report_payload)
        note = str(report_payload.get("leader_notes", "")).strip()
        if note:
            self._append_output("summary", note)
        self._render_job_queue()
        self.refresh_history_async()
        self.refresh_workspace_async()

    def _apply_health(self, payload: object) -> None:
        if isinstance(payload, list):
            self.latest_health_rows = {
                str(row.get("worker_id")): row
                for row in payload
                if isinstance(row, dict) and row.get("worker_id")}
            ok_count = sum(
                1 for row in payload
                if isinstance(row, dict) and row.get("ok"))
            self.connection_var.set(f"{ok_count}/{len(payload)} 可用")
            self.header_worker_var.set(f"{ok_count}/{len(payload)}")
            self.pet_connectivity_ok = ok_count > 0
            if not self.job_running:
                mood = self.current_pet_mood if self.pet_connectivity_ok else MOODS["offline"]
                if self.pet_connectivity_ok and self.current_pet_mood.key == "offline":
                    mood = MOODS["idle"]
                self._apply_pet_mood(mood, note=self.pet_message_var.get() or mood.note, restart=False)
            self._render_workers()

    def _apply_ping(self, payload: object) -> None:
        if isinstance(payload, list):
            self.latest_smoke_rows = {
                str(row.get("worker_id")): row
                for row in payload
                if isinstance(row, dict) and row.get("worker_id")}
            ok_count = sum(
                1 for row in payload
                if isinstance(row, dict) and row.get("ok"))
            first_mode = (str(payload[0].get("ping_mode", "quick"))
                          if payload and isinstance(payload[0], dict)
                          else "quick")
            label = "快测" if first_mode == "quick" else "深测"
            self.smoke_var.set(f"{label} {ok_count}/{len(payload)}")
            self.pet_connectivity_ok = ok_count > 0
            if not self.job_running and not self.pet_connectivity_ok:
                self._apply_pet_mood(MOODS["offline"], note="成员联调还未全部就绪。", restart=False)
            self._render_workers()

    def _apply_history(self, payload: object) -> None:
        if not isinstance(payload, list):
            return
        self.history_rows.clear()
        for iid in self.history_tree.get_children():
            self.history_tree.delete(iid)
        for idx, row in enumerate(payload):
            if not isinstance(row, dict):
                continue
            iid = f"h-{idx}"
            self.history_rows[iid] = row
            self.history_tree.insert(
                "", "end", iid=iid,
                values=(row.get("timestamp", "-"),
                        row.get("source", "-"),
                        row.get("title", "-"),
                        row.get("subtitle", "-")))

    def _apply_workspace(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        tree_text = (str(payload.get("tree", "")).strip()
                     or "没有可显示的文件树。")
        diff_text = (str(payload.get("diff", "")).strip()
                     or "没有可显示的工作区 diff。")
        if hasattr(self, "home_file_tree"):
            self._set_text(self.home_file_tree, tree_text)
        if hasattr(self, "home_diff"):
            self._set_text(self.home_diff, diff_text)

    def _apply_stats(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        memory = (payload.get("memory", {})
                  if isinstance(payload.get("memory"), dict) else {})
        disk = (payload.get("disk", {})
                if isinstance(payload.get("disk"), dict) else {})
        warnings = (payload.get("warnings", [])
                    if isinstance(payload.get("warnings"), list) else [])
        gpu_rows = (payload.get("gpu", [])
                    if isinstance(payload.get("gpu"), list) else [])
        cpu_percent = payload.get("cpu_percent")
        self.hardware_mode_var.set(
            f"建议档位：{self.PROFILE_NAMES.get(str(payload.get('recommended_mode', 'balanced')), '均衡')}")
        lines = [
            "内存",
            f"总量：{memory.get('total_gb', '-')} GB",
            f"已用：{memory.get('used_gb', '-')} GB ({memory.get('used_percent', '-')}%)",
            f"空闲：{memory.get('free_gb', '-')} GB",
            "", "磁盘",
            f"位置：{disk.get('path', '-')}",
            f"总量：{disk.get('total_gb', '-')} GB",
            f"已用：{disk.get('used_gb', '-')} GB ({disk.get('used_percent', '-')}%)",
            f"空闲：{disk.get('free_gb', '-')} GB",
            "", "CPU / GPU",
            f"CPU 占用：{cpu_percent if cpu_percent is not None else '-'}%",
        ]
        if gpu_rows:
            for idx, row in enumerate(gpu_rows, start=1):
                lines.extend([
                    f"GPU {idx}：{row.get('name', '-')}",
                    f"  利用率：{row.get('utilization_percent', '-')}%",
                    f"  显存：{row.get('memory_used_mb', '-')} / {row.get('memory_total_mb', '-')} MB ({row.get('memory_percent', '-')}%)",
                    f"  温度：{row.get('temperature_c', '-')} °C"])
        else:
            lines.append("GPU：未检测到可读状态")
        lines.extend(["", "告警"])
        lines.extend(
            [f"- {item}" for item in warnings]
            if warnings else ["当前未发现明显硬件压力。"])
        if hasattr(self, "hardware_text"):
            self._set_text(self.hardware_text, "\n".join(lines))

    def _apply_cancelled(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", ""))
        if job_id and job_id in self.jobs:
            job = self.jobs[job_id]
            job.status = "cancelled"
            job.note = str(payload.get("message", "任务已取消")).strip()
        if job_id and job_id != self.current_job_id:
            self._render_job_queue()
            return
        self.status_var.set("已取消")
        self.progress_var.set(
            str(payload.get("message", "任务已取消")).strip())
        self._apply_pet_mood(MOODS["cancelled"], note=self.progress_var.get())
        self._append_output("system", self.progress_var.get())
        self._render_job_queue()
        self.refresh_workspace_async()

    def _finish_job(self, payload: object) -> None:
        if isinstance(payload, dict):
            job_id = str(payload.get("job_id", ""))
        else:
            job_id = ""
        if (job_id and job_id in self.jobs
                and self.jobs[job_id].status == "running"):
            self.jobs[job_id].status = "completed"
        if job_id and job_id != self.current_job_id:
            self._render_job_queue()
            return
        self.job_running = False
        self.current_job_id = None
        self.current_run_control = None
        if not self.pending_job_ids and self.current_pet_mood.key == "planning":
            self._apply_pet_mood(MOODS["idle"], note="等待新的编码任务。", restart=False)
        self._render_job_queue()
        self._sync_controls()
        self.refresh_workspace_async()
        self._launch_next_job()

    def _error(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        job_id = str(payload.get("job_id", ""))
        if job_id and job_id in self.jobs:
            self.jobs[job_id].status = "error"
            self.jobs[job_id].note = str(payload.get("message", "未知错误"))
        title = str(payload.get("title", "MVP 错误"))
        message = str(payload.get("message", "未知错误"))
        self._append_output("error", f"{title}：{message}")
        self._set_summary_text(f"{title}\n\n{message}")
        self._set_text(self.raw_json,
                        str(payload.get("trace", "")) or message)
        self.status_var.set("异常")
        self.progress_var.set("本轮任务出现错误")
        self._apply_pet_mood(MOODS["error"], note=message)
        self._render_job_queue()
        self.refresh_workspace_async()
        messagebox.showerror(title, message)

    # ── rendering ─────────────────────────────────────────────────────

    def _render_job_queue(self) -> None:
        for iid in self.queue_tree.get_children():
            self.queue_tree.delete(iid)
        active_ids: list[str] = []
        if self.current_job_id and self.current_job_id in self.jobs:
            active_ids.append(self.current_job_id)
        active_ids.extend(
            job_id for job_id in self.pending_job_ids if job_id in self.jobs)
        finished_ids = [
            job_id for job_id in self.job_order
            if job_id not in active_ids][-4:]
        for job_id in [*active_ids, *finished_ids]:
            job = self.jobs[job_id]
            self.queue_tree.insert(
                "", "end", iid=job_id,
                values=(
                    self.STATUS_LABELS.get(job.status, job.status),
                    self.PROFILE_NAMES.get(job.mode, job.mode),
                    "规划" if job.plan_only else "执行",
                    safe_snippet(job.task, 88),
                ))
        self.queue_state_var.set(f"队列：{len(self.pending_job_ids)} 个待执行")
        if not self.current_job_id:
            self._set_text(self.queue_detail, "当前没有运行中的编码任务。")
        self._sync_controls()

    def _render_workers(self) -> None:
        rows = self.loader.list_workers()
        self.worker_rows = {str(row["worker_id"]): row for row in rows}
        for iid in self.worker_tree.get_children():
            self.worker_tree.delete(iid)
        for row in rows:
            worker_id = str(row["worker_id"])
            health = self.latest_health_rows.get(worker_id, {})
            smoke = self.latest_smoke_rows.get(worker_id, {})
            state = "待检查"
            if health:
                state = "可用" if health.get("ok") else "异常"
            if smoke:
                ping_mode = str(smoke.get("ping_mode", "quick"))
                prefix = "快测" if ping_mode == "quick" else "深测"
                state = f"{prefix}通过" if smoke.get("ok") else f"{prefix}失败"
            self.worker_tree.insert(
                "", "end", iid=worker_id,
                values=(row.get("display_name", worker_id),
                        row.get("framework_label", "-"),
                        row.get("target_label", "-"), state))

    def _render_plan(self, payload: dict[str, Any]) -> None:
        rows = payload.get("assignments", [])
        if not isinstance(rows, list):
            return
        self.assignment_rows.clear()
        for iid in self.assign_tree.get_children():
            self.assign_tree.delete(iid)
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            iid = f"plan-{idx}"
            self.assignment_rows[iid] = row
            self.assign_tree.insert(
                "", "end", iid=iid,
                values=(row.get("title", row.get("task_id", iid)),
                        self.KIND_LABELS.get(
                            str(row.get("kind", "coordination")),
                            str(row.get("kind", "-"))),
                        row.get("worker_id", "-"),
                        "待执行", "-"))
        self._set_text(self.assign_detail,
                        "已完成规划。选择一个子任务可以查看派工去向。")

    def _render_report_assignments(self, payload: dict[str, Any]) -> None:
        self.assignment_rows.clear()
        for iid in self.assign_tree.get_children():
            self.assign_tree.delete(iid)
        outcomes = payload.get("outcomes", [])
        if isinstance(outcomes, list) and outcomes:
            perf_rows = self._performance_rows(payload)
            for idx, raw in enumerate(outcomes):
                if not isinstance(raw, dict):
                    continue
                assignment = (raw.get("assignment", {})
                              if isinstance(raw.get("assignment"), dict)
                              else {})
                subtask = (assignment.get("subtask", {})
                           if isinstance(assignment.get("subtask"), dict)
                           else {})
                result = (raw.get("result", {})
                          if isinstance(raw.get("result"), dict) else {})
                review = (raw.get("review", {})
                          if isinstance(raw.get("review"), dict) else {})
                iid = f"outcome-{idx}"
                perf = perf_rows.get(
                    str(subtask.get("task_id", "")), {})
                self.assignment_rows[iid] = {
                    "subtask": subtask, "assignment": assignment,
                    "result": result, "review": review,
                    "performance": perf}
                self.assign_tree.insert(
                    "", "end", iid=iid,
                    values=(subtask.get("title",
                                        subtask.get("task_id", iid)),
                            self.KIND_LABELS.get(
                                str(subtask.get("kind", "coordination")),
                                str(subtask.get("kind", "-"))),
                            assignment.get("worker_id", "-"),
                            self.RESULT_LABELS.get(
                                str(result.get("status", "unknown")),
                                str(result.get("status", "-"))),
                            self.REVIEW_LABELS.get(
                                str(review.get("decision", "-")),
                                str(review.get("decision", "-")))))
            self._set_text(
                self.assign_detail,
                "选择一个子任务可以查看目标、结果和验收意见。")
            return

        planned_assignments = payload.get("planned_assignments", [])
        if isinstance(planned_assignments, list):
            for idx, assignment in enumerate(planned_assignments):
                if not isinstance(assignment, dict):
                    continue
                subtask = (assignment.get("subtask", {})
                           if isinstance(assignment.get("subtask"), dict)
                           else {})
                iid = f"planned-{idx}"
                self.assignment_rows[iid] = {
                    "subtask": subtask, "assignment": assignment}
                self.assign_tree.insert(
                    "", "end", iid=iid,
                    values=(subtask.get("title",
                                        subtask.get("task_id", iid)),
                            self.KIND_LABELS.get(
                                str(subtask.get("kind", "coordination")),
                                str(subtask.get("kind", "-"))),
                            assignment.get("worker_id", "-"),
                            "待执行", "-"))
            self._set_text(
                self.assign_detail,
                "当前是仅规划模式。选择一个子任务可以查看派工方案。")

    # ── tree selection handlers ───────────────────────────────────────

    def _on_queue_selected(self, _event: object = None) -> None:
        selection = self.queue_tree.selection()
        if not selection:
            self._sync_controls()
            return
        job = self.jobs.get(selection[0])
        if not job:
            self._sync_controls()
            return
        lines = [
            f"编号：{job.job_id}",
            f"状态：{self.STATUS_LABELS.get(job.status, job.status)}",
            f"档位：{self.PROFILE_NAMES.get(job.mode, job.mode)}",
            f"类型：{'仅规划' if job.plan_only else '执行'}",
            f"创建时间：{job.created_at}",
            "", "任务内容", job.task,
        ]
        if job.note:
            lines.extend(["", "备注", job.note])
        self._set_text(self.queue_detail, "\n".join(lines))
        self._sync_controls()

    def _on_assignment_selected(self, _event: object = None) -> None:
        selection = self.assign_tree.selection()
        if not selection:
            return
        payload = self.assignment_rows.get(selection[0], {})
        subtask = (payload.get("subtask", {})
                   if isinstance(payload.get("subtask"), dict) else {})
        assignment = (payload.get("assignment", {})
                      if isinstance(payload.get("assignment"), dict) else {})
        result = (payload.get("result", {})
                  if isinstance(payload.get("result"), dict) else {})
        review = (payload.get("review", {})
                  if isinstance(payload.get("review"), dict) else {})
        perf = (payload.get("performance", {})
                if isinstance(payload.get("performance"), dict) else {})
        lines = [
            f"子任务：{subtask.get('title', payload.get('title', '-'))}",
            f"编号：{subtask.get('task_id', payload.get('task_id', '-'))}",
            f"类型：{self.KIND_LABELS.get(str(subtask.get('kind', payload.get('kind', 'coordination'))), str(subtask.get('kind', payload.get('kind', '-'))))}",
            f"执行成员：{assignment.get('worker_id', payload.get('worker_id', '-'))}",
        ]
        if subtask:
            lines.extend([
                f"目标：{subtask.get('goal', '-')}",
                f"需要写入工作区：{'是' if subtask.get('needs_write_access') else '否'}",
                "", "验收标准"])
            criteria = subtask.get("acceptance_criteria", [])
            lines.extend(
                [f"- {item}" for item in criteria]
                if isinstance(criteria, list) and criteria
                else ["- 暂无"])
            depends_on = subtask.get("depends_on", [])
            target_files = subtask.get("target_files", [])
            validation_steps = subtask.get("validation_steps", [])
            if isinstance(depends_on, list) and depends_on:
                lines.extend(["", "前置依赖"])
                lines.extend([f"- {item}" for item in depends_on])
            if isinstance(target_files, list) and target_files:
                lines.extend(["", "目标文件 / 模块"])
                lines.extend([f"- {item}" for item in target_files])
            if isinstance(validation_steps, list) and validation_steps:
                lines.extend(["", "验证步骤"])
                lines.extend([f"- {item}" for item in validation_steps])
        if result:
            lines.extend([
                "", "执行结果",
                f"状态：{self.RESULT_LABELS.get(str(result.get('status', 'unknown')), str(result.get('status', '-')))}",
                f"摘要：{result.get('summary', '-')}",
                f"交付：{result.get('deliverable', '-')}"])
        if review:
            lines.extend([
                "", "验收结论",
                f"结果：{self.REVIEW_LABELS.get(str(review.get('decision', '-')), str(review.get('decision', '-')))}",
                f"摘要：{review.get('summary', '-')}"])
            findings = review.get("findings", [])
            if isinstance(findings, list) and findings:
                lines.extend([f"- {item}" for item in findings])
        if perf:
            lines.extend([
                "", "性能",
                f"执行耗时：{perf.get('execution_ms', '-')} ms",
                f"验收耗时：{perf.get('review_ms', '-')} ms",
                f"改派次数：{perf.get('reroutes', 0)}"])
        self._set_text(self.assign_detail, "\n".join(lines))

    def _on_history_selected(self, _event: object = None) -> None:
        selection = self.history_tree.selection()
        if selection:
            row = self.history_rows.get(selection[0], {})
            self._set_text(
                self.history_detail,
                "\n".join([
                    f"来源：{row.get('source', '-')}",
                    f"时间：{row.get('timestamp', '-')}",
                    f"主题：{row.get('title', '-')}",
                    f"补充：{row.get('subtitle', '-')}",
                    "",
                    str(row.get("detail", ""))]).strip())

    def _load_selected_history_into_dashboard(
            self, _event: object = None) -> None:
        selection = self.history_tree.selection()
        if selection:
            row = self.history_rows.get(selection[0], {})
            if row.get("kind") == "mvp_report" and row.get("report_path"):
                self._apply_report(
                    self.loader.load_report_payload(
                        str(row["report_path"])))

    def _on_worker_selected(self, _event: object = None) -> None:
        selection = self.worker_tree.selection()
        if not selection:
            return
        worker_id = selection[0]
        row = self.worker_rows.get(worker_id, {})
        health = self.latest_health_rows.get(worker_id, {})
        smoke = self.latest_smoke_rows.get(worker_id, {})
        lines = [
            f"成员：{row.get('display_name', worker_id)}",
            f"框架：{row.get('framework_label', '-')}",
            f"后端：{row.get('backend_label', '-')}",
            f"目标：{row.get('target_label', '-')}",
            f"角色：{row.get('role', '-')}",
            f"工作模式：{row.get('mode_label', '-')}",
            f"工作区写入：{row.get('write_label', '-')}",
            f"消耗 API：{'是' if row.get('api_cost') else '否'}",
            f"访问权限：{row.get('security_label', '-')}",
            f"近期平均耗时：{str(int(row.get('avg_latency_ms'))) + ' ms' if isinstance(row.get('avg_latency_ms'), (int, float)) else '暂无'}",
            f"近期完成率：{str(round(float(row.get('completed_ratio', 0.0)) * 100, 1)) + '%' if isinstance(row.get('completed_ratio'), (int, float)) else '暂无'}",
            "", "权限说明",
            str(row.get("security_detail", "-")),
            "", "能力标签",
            ", ".join(row.get("capabilities", []))
            if isinstance(row.get("capabilities"), list) else "-",
            "", "连接检查",
            f"状态：{'可用' if health.get('ok') else '异常' if health else '未检查'}",
            f"说明：{health.get('summary', '尚未执行连接检查。')}",
        ]
        if smoke:
            ping_mode = ("快速联调"
                         if str(smoke.get("ping_mode", "quick")) == "quick"
                         else "深度联调")
            lines.extend([
                "", ping_mode,
                f"状态：{'可调用' if smoke.get('ok') else '调用失败'}",
                f"说明：{smoke.get('summary', '-')}",
                f"耗时：{smoke.get('latency_ms', '-')} ms"])
            if smoke.get("deliverable"):
                lines.append(f"返回：{smoke.get('deliverable')}")
        self._set_text(self.worker_detail, "\n".join(lines))

    def open_selected_history_source(self) -> None:
        selection = self.history_tree.selection()
        if not selection:
            messagebox.showinfo("没有选中项",
                                 "请先在框架历史中选中一条记录。")
            return
        row = self.history_rows.get(selection[0], {})
        candidate = row.get("report_path") or row.get("source_path")
        if not candidate:
            messagebox.showinfo("无法打开",
                                 "这条记录没有可直接打开的源文件。")
            return
        path = Path(str(candidate))
        if not path.exists():
            messagebox.showinfo("文件不存在", f"未找到：\n{path}")
            return
        if hasattr(os, "startfile"):
            os.startfile(str(path))

    # ── performance helpers ───────────────────────────────────────────

    def _performance_rows(self, payload: dict[str, Any]) \
            -> dict[str, dict[str, Any]]:
        performance = (payload.get("performance", {})
                       if isinstance(payload.get("performance"), dict)
                       else {})
        rows = (performance.get("assignments", [])
                if isinstance(performance.get("assignments"), list) else [])
        return {
            str(row.get("task_id")): row
            for row in rows
            if isinstance(row, dict) and row.get("task_id")}

    def _perf_value(self, payload: dict[str, Any], key: str) -> str:
        performance = (payload.get("performance", {})
                       if isinstance(payload.get("performance"), dict)
                       else {})
        value = performance.get(key)
        if isinstance(value, (int, float)):
            return f"{int(value)} ms"
        return "-"

    # ── scheduled refreshes ──────────────────────────────────────────

    def _schedule_stats_refresh(self) -> None:
        if self.winfo_exists():
            self.refresh_stats_async()
            self._schedule_after(self.AUTO_STATS_MS, self._schedule_stats_refresh)

    def _schedule_workspace_refresh(self) -> None:
        if self.winfo_exists():
            self.refresh_workspace_async()
            self._schedule_after(self.AUTO_WORKSPACE_MS,
                                 self._schedule_workspace_refresh)


# ── entry point ──────────────────────────────────────────────────────

def _mvp_agent_append_live_stream(self: "MVPDesktopApp", payload: dict[str, Any]) -> None:
    event_type = str(payload.get("type", "")).strip()
    message = str(payload.get("message", "")).strip()
    if not message:
        return
    phase = self._phase_label(event_type)
    task_label = str(payload.get("task_id") or "").strip()
    worker_label = str(payload.get("worker_id") or payload.get("reviewer_worker") or "").strip()
    parts = [phase]
    if task_label:
        parts.append(task_label)
    if worker_label:
        parts.append(worker_label)
    if event_type == "assignment_stream":
        stream_text = str(payload.get("stream_text", "")).strip()
        combined = " 路 ".join(parts) + f" 路 {message}"
        if stream_text:
            combined = f"{combined}\n{stream_text}"
        self._append_output("summary", combined)
        return
    role = "milestone" if event_type in {
        "planning_completed", "run_completed", "batch_started",
        "assignment_completed", "review_completed",
    } else "system"
    self._append_output(role, " 路 ".join(parts) + f" 路 {message}")


def _mvp_agent_phase_label(self: "MVPDesktopApp", event_type: str) -> str:
    mapping = {
        "run_started": "开始接管",
        "planning_started": "开始规划",
        "planning_completed": "规划完成",
        "batch_started": "并行批次",
        "assignment_started": "开始执行",
        "assignment_completed": "执行完成",
        "assignment_stream": "代码回包",
        "assignment_rerouted": "改派工",
        "review_started": "开始验收",
        "review_completed": "验收完成",
        "run_completed": "运行完成",
        "run_cancelled": "已取消",
    }
    return mapping.get(event_type, event_type or "事件")


def _mvp_agent_on_timeline_selected(self: "MVPDesktopApp", _event=None) -> None:
    if not hasattr(self, "timeline_tree"):
        return
    selection = self.timeline_tree.selection()
    if not selection:
        return
    row = self.timeline_rows.get(selection[0], {})
    if not row:
        return
    payload = row.get("payload", {}) if isinstance(row.get("payload"), dict) else {}
    lines = [
        f"时间：{row.get('timestamp', '-')}",
        f"阶段：{row.get('phase', '-')}",
        f"任务：{row.get('task', '-')}",
        f"成员：{row.get('worker', '-')}",
        "",
        f"消息：{row.get('message', '-')}",
    ]
    target_files = payload.get("target_files", [])
    if isinstance(target_files, list) and target_files:
        lines.extend(["", "目标文件"])
        lines.extend([f"- {item}" for item in target_files])
    stream_text = str(payload.get("stream_text", "")).strip()
    if stream_text:
        lines.extend(["", "代码回包", stream_text])
    self._set_text(self.timeline_detail, "\n".join(lines))


MVPDesktopApp._append_live_stream = _mvp_agent_append_live_stream
MVPDesktopApp._phase_label = _mvp_agent_phase_label
MVPDesktopApp._on_timeline_selected = _mvp_agent_on_timeline_selected


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mvp.app", description="启动 MVP Agent 中文桌面 code agent。")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="mvp.config.json 的路径")
    parser.add_argument("--self-test", action="store_true",
                        help="仅创建一次窗口并立即退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            app = MVPDesktopApp(args.config, autostart=False)
            app.update_idletasks()
            app.destroy()
            print("MVP Agent desktop self-test passed.")
            return 0
        app = MVPDesktopApp(args.config)
        app.mainloop()
        return 0
    except Exception as exc:
        log_path = Path(os.environ.get("TEMP", ".")) / "mvp_gui_error.log"
        trace = traceback.format_exc()
        log_path.write_text(trace, encoding="utf-8")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "MVP Agent 启动失败",
                f"{exc}\n\n错误日志已写入：\n{log_path}")
            root.destroy()
        except Exception:
            pass
        print(trace)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
