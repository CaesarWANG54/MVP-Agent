from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mvp.pet_runtime import extract_animation_frames, load_pet_package, make_brand_icon


ASSETS_DIR = REPO_ROOT / "mvp" / "assets"
PNG_PATH = ASSETS_DIR / "mvp_icon.png"
ICO_PATH = ASSETS_DIR / "mvp_icon.ico"
DEFAULT_PET_DIR = ASSETS_DIR / "pets" / "mvp-agent-pet"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate MVP Agent icon from a Codex pet package.")
    parser.add_argument("--pet-dir", default=str(DEFAULT_PET_DIR), help="Pet package directory containing pet.json and spritesheet.webp.")
    args = parser.parse_args(argv)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    package = load_pet_package(args.pet_dir)
    if package is None:
        raise SystemExit(f"Pet package not found or invalid: {args.pet_dir}")

    idle_frame = extract_animation_frames(package, "idle")[0]
    icon = make_brand_icon(idle_frame, size=256)
    icon.save(PNG_PATH)
    icon.save(
        ICO_PATH,
        sizes=[(256, 256), (128, 128), (96, 96), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    print(PNG_PATH)
    print(ICO_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
