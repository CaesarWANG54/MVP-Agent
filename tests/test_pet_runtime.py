from __future__ import annotations

import unittest
from pathlib import Path

from mvp.pet_runtime import (
    MOODS,
    animation_durations,
    discover_pet_packages,
    extract_animation_frames,
    load_pet_package,
    mood_from_event,
    mood_from_report_status,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
PET_DIR = REPO_ROOT / "mvp" / "assets" / "pets" / "mvp-agent-pet"


class PetRuntimeTests(unittest.TestCase):
    def test_default_pet_package_loads(self) -> None:
        package = load_pet_package(PET_DIR)
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(package.pet_id, "mvp-agent-pet")
        self.assertTrue(package.spritesheet_path.exists())

    def test_discovery_finds_repo_pet(self) -> None:
        packages = discover_pet_packages([PET_DIR.parent])
        pet_ids = {package.pet_id for package in packages}
        self.assertIn("mvp-agent-pet", pet_ids)

    def test_extract_animation_frames_uses_contract_counts(self) -> None:
        package = load_pet_package(PET_DIR)
        assert package is not None
        self.assertEqual(len(extract_animation_frames(package, "idle")), 6)
        self.assertEqual(len(extract_animation_frames(package, "running-right")), 8)
        self.assertEqual(len(animation_durations("review")), 6)

    def test_mood_mapping_covers_core_run_states(self) -> None:
        self.assertEqual(mood_from_event("planning_started").key, "planning")
        self.assertEqual(mood_from_event("assignment_started").key, "working")
        self.assertEqual(mood_from_event("review_completed", decision="pass").key, "reviewing")
        self.assertEqual(mood_from_event("run_cancelled").key, "cancelled")
        self.assertEqual(mood_from_report_status("completed").key, "success")
        self.assertEqual(mood_from_report_status("needs_revision").key, "error")
        self.assertEqual(MOODS["offline"].label, "连接待确认")


if __name__ == "__main__":
    unittest.main()
