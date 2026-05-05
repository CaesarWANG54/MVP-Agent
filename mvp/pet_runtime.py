from __future__ import annotations

from dataclasses import dataclass
import json
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from PIL import Image


CELL_WIDTH = 192
CELL_HEIGHT = 208
ATLAS_COLUMNS = 8
ATLAS_ROWS = 9


@dataclass(frozen=True, slots=True)
class PetAnimation:
    name: str
    row: int
    used_columns: int
    durations_ms: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PetPackage:
    pet_id: str
    display_name: str
    description: str
    package_dir: Path
    spritesheet_path: Path


@dataclass(frozen=True, slots=True)
class PetMood:
    key: str
    animation: str
    label: str
    accent: str
    note: str


ANIMATIONS: dict[str, PetAnimation] = {
    "idle": PetAnimation("idle", 0, 6, (280, 110, 110, 140, 140, 320)),
    "running-right": PetAnimation("running-right", 1, 8, (120, 120, 120, 120, 120, 120, 120, 220)),
    "running-left": PetAnimation("running-left", 2, 8, (120, 120, 120, 120, 120, 120, 120, 220)),
    "waving": PetAnimation("waving", 3, 4, (140, 140, 140, 280)),
    "jumping": PetAnimation("jumping", 4, 5, (140, 140, 140, 140, 280)),
    "failed": PetAnimation("failed", 5, 8, (140, 140, 140, 140, 140, 140, 140, 240)),
    "waiting": PetAnimation("waiting", 6, 6, (150, 150, 150, 150, 150, 260)),
    "running": PetAnimation("running", 7, 6, (120, 120, 120, 120, 120, 220)),
    "review": PetAnimation("review", 8, 6, (150, 150, 150, 150, 150, 280)),
}

MOODS: dict[str, PetMood] = {
    "idle": PetMood("idle", "waiting", "待命中", "#8C8279", "等待新的编排任务。"),
    "planning": PetMood("planning", "review", "规划中", "#B24C27", "正在理解需求并拆分任务。"),
    "working": PetMood("working", "running-right", "执行中", "#C41528", "正在协调多 Agent 推进工作。"),
    "reviewing": PetMood("reviewing", "review", "复核中", "#6F4B2A", "正在核对结果与验收标准。"),
    "success": PetMood("success", "waving", "已完成", "#1F7A56", "这一轮任务已经顺利收口。"),
    "error": PetMood("error", "failed", "需关注", "#AD2E24", "这一轮遇到了异常或阻塞。"),
    "cancelled": PetMood("cancelled", "waiting", "已取消", "#7C6D62", "当前任务已停止，等待下一步。"),
    "offline": PetMood("offline", "idle", "连接待确认", "#8C8279", "正在等待成员平台状态更新。"),
}


def discover_pet_packages(roots: Iterable[Path]) -> list[PetPackage]:
    discovered: dict[str, PetPackage] = {}
    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.exists() or not root_path.is_dir():
            continue
        for child in sorted(root_path.iterdir(), key=lambda item: item.name.lower()):
            if not child.is_dir():
                continue
            package = load_pet_package(child)
            if package is None:
                continue
            discovered[str(package.package_dir).lower()] = package
    return sorted(discovered.values(), key=lambda item: item.display_name.lower())


def load_pet_package(package_dir: str | Path) -> PetPackage | None:
    directory = Path(package_dir).expanduser().resolve()
    manifest_path = directory / "pet.json"
    if not manifest_path.exists():
        return None
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    spritesheet_rel = str(payload.get("spritesheetPath", "spritesheet.webp")).strip() or "spritesheet.webp"
    spritesheet_path = directory / spritesheet_rel
    if not spritesheet_path.exists():
        return None
    pet_id = str(payload.get("id") or directory.name).strip() or directory.name
    display_name = str(payload.get("displayName") or pet_id).strip() or pet_id
    description = str(payload.get("description") or "").strip()
    return PetPackage(
        pet_id=pet_id,
        display_name=display_name,
        description=description,
        package_dir=directory,
        spritesheet_path=spritesheet_path,
    )


def mood_from_event(event_type: str, *, status: str = "", decision: str = "") -> PetMood:
    event = event_type.strip().lower()
    result_status = status.strip().lower()
    review_decision = decision.strip().lower()
    if event in {"run_started", "planning_started", "planning_completed"}:
        return MOODS["planning"]
    if event in {"assignment_started", "assignment_completed", "assignment_stream", "batch_started", "assignment_rerouted"}:
        return MOODS["working"]
    if event in {"review_started", "review_completed"}:
        if review_decision == "fail":
            return MOODS["error"]
        if review_decision == "revise":
            return MOODS["working"]
        return MOODS["reviewing"]
    if event == "run_completed":
        return mood_from_report_status(result_status or "completed")
    if event == "run_cancelled":
        return MOODS["cancelled"]
    return MOODS["idle"]


def mood_from_report_status(status: str) -> PetMood:
    normalized = status.strip().lower()
    if normalized in {"completed", "planned", "partial"}:
        return MOODS["success"]
    if normalized == "cancelled":
        return MOODS["cancelled"]
    if normalized in {"needs_revision", "empty", "error", "failed", "unknown"}:
        return MOODS["error"]
    return MOODS["idle"]


@lru_cache(maxsize=16)
def _load_sheet(path_str: str) -> Image.Image:
    return Image.open(path_str).convert("RGBA")


def extract_animation_frames(package: PetPackage, animation_name: str) -> list[Image.Image]:
    animation = ANIMATIONS.get(animation_name) or ANIMATIONS["idle"]
    sheet = _load_sheet(str(package.spritesheet_path))
    frames: list[Image.Image] = []
    for column in range(animation.used_columns):
        left = column * CELL_WIDTH
        top = animation.row * CELL_HEIGHT
        frame = sheet.crop((left, top, left + CELL_WIDTH, top + CELL_HEIGHT))
        frames.append(frame)
    return frames


def animation_durations(animation_name: str) -> tuple[int, ...]:
    animation = ANIMATIONS.get(animation_name) or ANIMATIONS["idle"]
    return animation.durations_ms


def make_thumbnail(frame: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_width, target_height = size
    bbox = frame.getbbox()
    source = frame.crop(bbox) if bbox else frame.copy()
    source.thumbnail((target_width - 8, target_height - 8), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    x = max((target_width - source.width) // 2, 0)
    y = max((target_height - source.height) // 2, 0)
    canvas.alpha_composite(source, dest=(x, y))
    return canvas


def make_brand_icon(frame: Image.Image, size: int = 256) -> Image.Image:
    base = Image.new("RGBA", (size, size), "#F3EEE7")
    from PIL import ImageDraw

    draw = ImageDraw.Draw(base)
    draw.rounded_rectangle((8, 8, size - 8, size - 8), radius=56, fill="#FFFDFC", outline="#DDCFC1", width=3)
    draw.rounded_rectangle((24, 24, size - 24, size - 24), radius=44, fill="#FBF8F4")
    draw.rounded_rectangle((44, 40, size - 44, 72), radius=16, fill="#F8EAEC")
    draw.rounded_rectangle((58, 50, size - 126, 58), radius=4, fill="#C41528")
    draw.rounded_rectangle((size - 112, 50, size - 58, 58), radius=4, fill="#D9CCC1")
    draw.ellipse((size - 76, 106, size - 38, 144), fill="#C41528")

    pet = make_thumbnail(frame, (size - 70, size - 78))
    shadow = Image.new("RGBA", pet.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.ellipse((26, pet.height - 26, pet.width - 26, pet.height - 8), fill="#E7DDD4")
    base.alpha_composite(shadow, dest=(35, 56))
    base.alpha_composite(pet, dest=((size - pet.width) // 2, 58))
    return base
