from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mvp.pet_runtime import (
    ATLAS_COLUMNS,
    ATLAS_ROWS,
    CELL_HEIGHT,
    CELL_WIDTH,
    extract_animation_frames,
    load_pet_package,
    make_thumbnail,
)

PET_DIR = REPO_ROOT / "mvp" / "assets" / "pets" / "mvp-agent-pet"
DEFAULT_SOURCE = Path(r"C:\Users\CaesarWang\Desktop\55db9687d75325d5cd592b0f43f0b2af.jpg")
DEFAULT_RUN_DIR = REPO_ROOT / "mvp_runs" / "pet_runs" / "mvp-trophy-hatch"
OUTPUT_SHEET = PET_DIR / "spritesheet.webp"
OUTPUT_SOURCE = PET_DIR / "reference_source.png"
OUTPUT_PREVIEW = PET_DIR / "preview.png"
OUTPUT_CONTACT_SHEET = PET_DIR / "contact_sheet.png"
OUTPUT_DEBUG = PET_DIR / "idle_frame_debug.png"
MANIFEST_PATH = PET_DIR / "pet.json"
MANIFEST = {
    "id": "mvp-agent-pet",
    "displayName": "MVP Trophy",
    "description": "A cheerful pixel trophy mascot for MVP Agent, used for planning, coding, review, success, and alert states.",
    "spritesheetPath": "spritesheet.webp",
}

ROW_SPECS: list[tuple[str, int]] = [
    ("idle", 6),
    ("running-right", 8),
    ("running-left", 8),
    ("waving", 4),
    ("jumping", 5),
    ("failed", 8),
    ("waiting", 6),
    ("running", 6),
    ("review", 6),
]


def write_manifest() -> None:
    MANIFEST_PATH.write_text(json.dumps(MANIFEST, indent=2) + "\n", encoding="utf-8")


def remove_light_background(image: Image.Image, threshold: int = 244) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    for y in range(rgba.height):
        for x in range(rgba.width):
            red, green, blue, alpha = pixels[x, y]
            if red >= threshold and green >= threshold and blue >= threshold:
                pixels[x, y] = (red, green, blue, 0)
    bbox = rgba.getbbox()
    return rgba.crop(bbox) if bbox else rgba


def fit_sprite(image: Image.Image, max_size: tuple[int, int] = (152, 164)) -> Image.Image:
    sprite = image.copy()
    sprite.thumbnail(max_size, Image.Resampling.LANCZOS)
    return sprite


def transform_sprite(
    sprite: Image.Image,
    *,
    dx: int = 0,
    dy: int = 0,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    rotation: float = 0.0,
    brighten: float = 1.0,
    sharpen: bool = False,
    mirror: bool = False,
) -> Image.Image:
    frame = ImageOps.mirror(sprite) if mirror else sprite.copy()
    width = max(1, int(frame.width * scale_x))
    height = max(1, int(frame.height * scale_y))
    frame = frame.resize((width, height), Image.Resampling.NEAREST)
    if rotation:
        frame = frame.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
    if brighten != 1.0:
        frame = ImageEnhance.Brightness(frame).enhance(brighten)
    if sharpen:
        frame = frame.filter(ImageFilter.SHARPEN)

    canvas = Image.new("RGBA", (CELL_WIDTH, CELL_HEIGHT), (0, 0, 0, 0))
    x = (CELL_WIDTH - frame.width) // 2 + dx
    y = (CELL_HEIGHT - frame.height) // 2 + dy
    shadow = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.ellipse(
        (frame.width * 0.18, frame.height * 0.8, frame.width * 0.82, frame.height * 0.96),
        fill=(0, 0, 0, 52),
    )
    canvas.alpha_composite(shadow, dest=(x, y + 8))
    canvas.alpha_composite(frame, dest=(x, y))
    return canvas


def add_sparkles(
    frame: Image.Image,
    positions: list[tuple[int, int]],
    color: tuple[int, int, int] = (255, 212, 86),
) -> Image.Image:
    canvas = frame.copy()
    draw = ImageDraw.Draw(canvas)
    for x, y in positions:
        draw.line((x - 4, y, x + 4, y), fill=color, width=2)
        draw.line((x, y - 4, x, y + 4), fill=color, width=2)
    return canvas


def add_tears(frame: Image.Image, positions: list[tuple[int, int]]) -> Image.Image:
    canvas = frame.copy()
    draw = ImageDraw.Draw(canvas)
    for x, y in positions:
        draw.ellipse(
            (x - 3, y - 1, x + 3, y + 7),
            fill=(130, 200, 255, 220),
            outline=(86, 154, 222, 255),
        )
    return canvas


def pose_sequence(sprite: Image.Image, row_name: str, count: int) -> list[Image.Image]:
    pose_map: dict[str, list[dict[str, object]]] = {
        "idle": [
            dict(dy=2, scale_y=0.99),
            dict(dy=0, brighten=1.02),
            dict(dy=-2, scale_y=1.01, sharpen=True),
            dict(dy=-3, rotation=-1.0, brighten=1.03),
            dict(dy=-1, rotation=1.0, brighten=1.01),
            dict(dy=2, scale_y=0.99),
        ],
        "running-right": [
            dict(dx=-10, dy=4, rotation=-4.0),
            dict(dx=-6, dy=0, rotation=-1.5),
            dict(dx=-2, dy=-4, rotation=1.5, brighten=1.02),
            dict(dx=4, dy=-1, rotation=4.0, sharpen=True),
            dict(dx=10, dy=3, rotation=2.0),
            dict(dx=6, dy=-2, rotation=-1.0, brighten=1.03),
            dict(dx=1, dy=-4, rotation=0.0),
            dict(dx=-3, dy=1, rotation=-2.0),
        ],
        "running-left": [
            dict(dx=10, dy=4, rotation=4.0, mirror=True),
            dict(dx=6, dy=0, rotation=1.5, mirror=True),
            dict(dx=2, dy=-4, rotation=-1.5, brighten=1.02, mirror=True),
            dict(dx=-4, dy=-1, rotation=-4.0, sharpen=True, mirror=True),
            dict(dx=-10, dy=3, rotation=-2.0, mirror=True),
            dict(dx=-6, dy=-2, rotation=1.0, brighten=1.03, mirror=True),
            dict(dx=-1, dy=-4, rotation=0.0, mirror=True),
            dict(dx=3, dy=1, rotation=2.0, mirror=True),
        ],
        "waving": [
            dict(rotation=-2.0, dy=1, brighten=1.02),
            dict(rotation=0.0, dy=-1, brighten=1.04),
            dict(rotation=2.0, dy=-2, brighten=1.05),
            dict(rotation=-1.0, dy=0, brighten=1.02),
        ],
        "jumping": [
            dict(dy=4, scale_y=0.96, scale_x=1.03),
            dict(dy=-8, scale_y=1.02, brighten=1.04),
            dict(dy=-24, scale_y=1.07, scale_x=0.97, brighten=1.07),
            dict(dy=-10, scale_y=1.01, brighten=1.03),
            dict(dy=2, scale_y=0.98, scale_x=1.02),
        ],
        "failed": [
            dict(dy=0, rotation=0.0),
            dict(dy=2, rotation=2.0, brighten=0.98),
            dict(dy=6, rotation=4.5, brighten=0.95),
            dict(dy=10, rotation=7.0, brighten=0.92),
            dict(dy=14, rotation=9.0, brighten=0.90),
            dict(dy=16, rotation=7.5, brighten=0.91),
            dict(dy=18, rotation=5.0, brighten=0.93),
            dict(dy=18, rotation=2.5, brighten=0.95),
        ],
        "waiting": [
            dict(dx=0, dy=2, rotation=-1.0),
            dict(dx=-3, dy=0, rotation=-2.0, brighten=1.01),
            dict(dx=0, dy=-1, rotation=0.0),
            dict(dx=3, dy=0, rotation=2.0, brighten=1.01),
            dict(dx=0, dy=1, rotation=0.0),
            dict(dx=0, dy=2, rotation=-1.0),
        ],
        "running": [
            dict(dy=2, rotation=-2.0),
            dict(dy=-2, rotation=0.0, brighten=1.02),
            dict(dy=-4, rotation=2.0, brighten=1.03),
            dict(dy=-2, rotation=0.0, sharpen=True),
            dict(dy=1, rotation=-1.0),
            dict(dy=2, rotation=-2.0),
        ],
        "review": [
            dict(dx=-1, dy=1, rotation=-1.0),
            dict(dx=0, dy=-1, rotation=0.0, brighten=1.02),
            dict(dx=1, dy=-2, rotation=1.0, brighten=1.03),
            dict(dx=0, dy=-1, rotation=0.0),
            dict(dx=-1, dy=0, rotation=-1.0, brighten=1.01),
            dict(dx=0, dy=1, rotation=0.0),
        ],
    }
    frames: list[Image.Image] = []
    poses = pose_map[row_name]
    for index in range(count):
        frame = transform_sprite(sprite, **poses[index])
        if row_name in {"idle", "waving", "review"} and index in {1, 3}:
            frame = add_sparkles(frame, [(46, 40), (145, 44)])
        if row_name == "jumping" and index == 2:
            frame = add_sparkles(frame, [(52, 28), (140, 24)])
        if row_name == "failed" and index >= 4:
            frame = add_tears(frame, [(78, 96), (116, 101)])
        if row_name == "review" and index in {2, 4}:
            frame = add_sparkles(frame, [(58, 42)])
        frames.append(frame)
    return frames


def build_legacy_spritesheet(source_path: Path) -> Image.Image:
    cleaned = remove_light_background(Image.open(source_path))
    sprite = fit_sprite(cleaned)
    cleaned.save(OUTPUT_SOURCE)
    atlas = Image.new(
        "RGBA",
        (ATLAS_COLUMNS * CELL_WIDTH, ATLAS_ROWS * CELL_HEIGHT),
        (0, 0, 0, 0),
    )
    for row_index, (row_name, used_columns) in enumerate(ROW_SPECS):
        frames = pose_sequence(sprite, row_name, used_columns)
        for column_index, frame in enumerate(frames):
            atlas.alpha_composite(frame, dest=(column_index * CELL_WIDTH, row_index * CELL_HEIGHT))
    return atlas


def save_preview_assets() -> None:
    package = load_pet_package(PET_DIR)
    if package is None:
        raise SystemExit(f"Pet package not found after sync: {PET_DIR}")
    idle_frame = extract_animation_frames(package, "idle")[0]
    preview = Image.new("RGBA", (420, 420), (0, 0, 0, 0))
    preview.alpha_composite(make_thumbnail(idle_frame, (360, 360)), dest=(30, 30))
    preview.save(OUTPUT_PREVIEW)
    idle_frame.save(OUTPUT_DEBUG)


def sync_from_hatch_run(run_dir: Path) -> None:
    final_sheet = run_dir / "final" / "spritesheet.webp"
    if not final_sheet.exists():
        raise SystemExit(f"Finalized Hatch Pet spritesheet not found: {final_sheet}")

    PET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(final_sheet, OUTPUT_SHEET)
    write_manifest()

    canonical_base = run_dir / "references" / "canonical-base.png"
    if canonical_base.exists():
        shutil.copy2(canonical_base, OUTPUT_SOURCE)
    elif DEFAULT_SOURCE.exists():
        shutil.copy2(DEFAULT_SOURCE, OUTPUT_SOURCE)

    contact_sheet = run_dir / "qa" / "contact-sheet.png"
    if contact_sheet.exists():
        shutil.copy2(contact_sheet, OUTPUT_CONTACT_SHEET)

    save_preview_assets()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or sync the MVP Agent trophy pet package.")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="Path to the trophy mascot source image.")
    parser.add_argument("--run-dir", default=str(DEFAULT_RUN_DIR), help="Finalized Hatch Pet run directory to sync from when available.")
    parser.add_argument("--force-legacy", action="store_true", help="Ignore the Hatch Pet run and rebuild the legacy deterministic spritesheet from the source image.")
    args = parser.parse_args(argv)

    PET_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = Path(args.run_dir).expanduser().resolve()
    if not args.force_legacy and (run_dir / "final" / "spritesheet.webp").exists():
        sync_from_hatch_run(run_dir)
        print(OUTPUT_SHEET)
        print(MANIFEST_PATH)
        if OUTPUT_CONTACT_SHEET.exists():
            print(OUTPUT_CONTACT_SHEET)
        print(OUTPUT_PREVIEW)
        return 0

    source_path = Path(args.source).expanduser().resolve()
    if not source_path.exists():
        raise SystemExit(f"Source image not found: {source_path}")

    write_manifest()
    atlas = build_legacy_spritesheet(source_path)
    atlas.save(OUTPUT_SHEET, lossless=True, quality=100, method=6)
    save_preview_assets()
    print(OUTPUT_SHEET)
    print(MANIFEST_PATH)
    print(OUTPUT_SOURCE)
    print(OUTPUT_PREVIEW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
