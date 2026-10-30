from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "App Source"
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from config import DEFAULT_CONFIG, load_config  # noqa: E402
from mockup_mode import explicit_sides_from_path  # noqa: E402
from queue_manager import (  # noqa: E402
    build_queue,
    find_incomplete_variant_groups,
    load_queue,
    pending_jobs,
    save_queue,
)


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test")
    return path


class NamingRulesTests(unittest.TestCase):

    def test_side_parser_requires_an_explicit_final_marker(self) -> None:
        self.assertEqual(explicit_sides_from_path("shirt.bl.1S.png"), 1)
        self.assertEqual(explicit_sides_from_path("shirt.wh.2S.webp"), 2)
        self.assertIsNone(explicit_sides_from_path("shirt.png"))



class QueueMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        config_path = root / "config.json"
        config_path.write_text("{}\n", encoding="utf-8")
        base = load_config(config_path)
        self.config = replace(
            base,
            project_root=root,
            config_path=config_path,
            rerun_done=False,
        )
        self.config.ensure_folders()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def jobs(self):
        return build_queue(self.config, preserve_existing=False, save=False)



    def test_png_only_targets_tagged_psd_and_maps_all_smart_layers(self) -> None:
        for name in (
            "one.wh.png",
            "one.bl.png",
            "two.wh.f.png",
            "two.wh.b.png",
            "two.bl.f.png",
            "two.bl.b.png",
            "plain.png",
            "plain_two.f.png",
            "plain_two.b.png",
            "broken.f.png",
        ):
            touch(self.config.png_dir / name)

        touch(self.config.mockup_dir / "shirt.1S.psd")
        touch(self.config.mockup_dir / "shirt.2S.psd")
        touch(self.config.mockup_dir / "ignored.psd")
        touch(self.config.mockup_dir / "ignored.1S.png")

        jobs = self.jobs()
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(job["runner"] == "psd" for job in jobs))
        self.assertTrue(all(Path(job["mockup_path"]).suffix.casefold() == ".psd" for job in jobs))

        one_job = next(job for job in jobs if job["output_name"].startswith("one__"))
        one_slots = {item["slot"]: Path(item["path"]).name for item in one_job["psd_replacements"]}
        self.assertEqual(one_slots["dark"], "one.bl.png")
        self.assertEqual(one_slots["light"], "one.wh.png")

        two_job = next(job for job in jobs if job["output_name"].startswith("two__"))
        two_slots = {item["slot"]: Path(item["path"]).name for item in two_job["psd_replacements"]}
        self.assertEqual(two_slots["dark_front"], "two.bl.f.png")
        self.assertEqual(two_slots["dark_back"], "two.bl.b.png")
        self.assertEqual(two_slots["light_front"], "two.wh.f.png")
        self.assertEqual(two_slots["light_back"], "two.wh.b.png")
        self.assertFalse(any(job["output_name"].startswith("broken__") for job in jobs))

    def test_png_two_side_can_share_front_across_both_shirt_colours(self) -> None:
        for name in (
            "shared_front.f.png",
            "shared_front.b.bl.png",
            "shared_front.b.wh.png",
        ):
            touch(self.config.png_dir / name)
        touch(self.config.mockup_dir / "shirt.2S.psd")
        touch(self.config.mockup_dir / "ignored.1S.psd")

        jobs = self.jobs()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["runner"], "psd")
        self.assertEqual(jobs[0]["idea_sides"], 2)
        self.assertEqual(jobs[0]["mockup_sides"], 2)
        slots = {
            item["slot"]: Path(item["path"]).name
            for item in jobs[0]["psd_replacements"]
        }
        self.assertEqual(slots["dark_front"], "shared_front.f.png")
        self.assertEqual(slots["dark_back"], "shared_front.b.bl.png")
        self.assertEqual(slots["light_front"], "shared_front.f.png")
        self.assertEqual(slots["light_back"], "shared_front.b.wh.png")
        self.assertEqual(len(jobs[0]["artwork_paths"]), 3)
        self.assertFalse(find_incomplete_variant_groups(self.config))

    def test_png_two_side_can_share_back_across_both_shirt_colours(self) -> None:
        for name in (
            "shared_back.b.png",
            "shared_back.f.bl.png",
            "shared_back.f.wh.png",
        ):
            touch(self.config.png_dir / name)
        touch(self.config.mockup_dir / "shirt.2S.psd")

        jobs = self.jobs()

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["runner"], "psd")
        self.assertEqual(jobs[0]["idea_sides"], 2)
        self.assertEqual(jobs[0]["mockup_sides"], 2)
        slots = {
            item["slot"]: Path(item["path"]).name
            for item in jobs[0]["psd_replacements"]
        }
        self.assertEqual(slots["dark_front"], "shared_back.f.bl.png")
        self.assertEqual(slots["dark_back"], "shared_back.b.png")
        self.assertEqual(slots["light_front"], "shared_back.f.wh.png")
        self.assertEqual(slots["light_back"], "shared_back.b.png")
        self.assertEqual(len(jobs[0]["artwork_paths"]), 3)
        self.assertFalse(find_incomplete_variant_groups(self.config))

    def test_png_shared_side_pattern_stays_incomplete_without_both_colours(
        self,
    ) -> None:
        touch(self.config.png_dir / "incomplete.f.png")
        touch(self.config.png_dir / "incomplete.b.bl.png")
        touch(self.config.mockup_dir / "shirt.2S.psd")

        self.assertFalse(self.jobs())
        findings = find_incomplete_variant_groups(self.config)
        self.assertTrue(
            any(group.base_name == "incomplete" for group in findings)
        )

    def test_incomplete_color_and_side_groups_are_reported(self) -> None:
        touch(self.config.png_dir / "color.wh.png")
        touch(self.config.png_dir / "side.bl.f.png")

        groups = find_incomplete_variant_groups(self.config)
        findings = {
            (group.base_name, group.present, group.missing)
            for group in groups
        }
        self.assertIn(("color", ("wh",), ("bl",)), findings)
        self.assertIn(("side", ("f",), ("b",)), findings)



class PngIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config_path = self.root / "config.json"
        config_path.write_text("{}", encoding="utf-8")
        self.config = load_config(config_path)
        self.config.ensure_folders()

    def test_defaults_keep_only_supported_settings_and_create_only_png_folders(self) -> None:
        self.assertEqual(self.config.ui_theme, "png_modern")
        self.assertEqual(self.config.ui_color_preset, "soft_purple")
        self.assertEqual(self.config.generation_timeout_sec, 600)
        self.assertEqual((self.config.output_width_px, self.config.output_height_px, self.config.output_dpi), (2000, 2000, 72))
        self.assertTrue(self.config.remove_output_metadata)
        self.assertFalse(self.config.output_watermark_enabled)
        self.assertFalse(self.config.rerun_done)
        self.assertFalse(self.config.update_check_on_start)
        self.assertEqual(self.config.update_github_repo, "")
        self.assertEqual({path.name for path in self.root.iterdir() if path.is_dir()}, {"PNG", "Mockup", "Output", "logs"})
        self.assertFalse(any(key.startswith(("edge_", "gemini_", "source_", "rmbg_", "background_")) for key in DEFAULT_CONFIG))

    def test_old_workflow_flags_cannot_enable_removed_features(self) -> None:
        self.config.config_path.write_text(json.dumps({"source_idea_1s_enabled": True, "source_background_remove_enabled": True}), encoding="utf-8")
        config = load_config(self.config.config_path)
        touch(self.root / "Idea" / "old.png")
        touch(self.root / "PNG" / "design.png")
        touch(self.root / "Mockup" / "shirt.1S.psd")
        jobs = build_queue(config, save=False)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["source_kind"], "png")

    def test_queue_load_save_and_selection_reject_foreign_runners(self) -> None:
        png = {"id": "1", "runner": "psd", "source_kind": "png", "status": "pending"}
        foreign = [
            {"id": "2", "runner": "gemini", "source_kind": "idea", "status": "pending"},
            {"id": "3", "runner": "background_remove", "status": "pending"},
            {"id": "4", "runner": "psd", "source_kind": "idea", "status": "pending"},
        ]
        self.config.queue_path.write_text(json.dumps([png] + foreign), encoding="utf-8")
        self.assertEqual(load_queue(self.config), [png])
        self.assertEqual(pending_jobs(self.config, [png] + foreign), [png])
        save_queue(self.config, [png] + foreign)
        self.assertEqual(json.loads(self.config.queue_path.read_text(encoding="utf-8")), [png])

    def test_psd_state_preserves_completed_outputs_and_requeues_missing_export(self) -> None:
        from state import apply_state_to_queue, load_state, record_job, _psd_done_marker
        touch(self.config.png_dir / "design.png")
        touch(self.config.mockup_dir / "shirt.1S.psd")
        job = build_queue(self.config, save=False)[0]
        output = touch(self.config.output_dir / "mockup.png")
        marker = _psd_done_marker(self.config, job)
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps({"output_paths": [str(output)]}), encoding="utf-8")
        job["status"] = "done"
        record_job(self.config, {}, job)
        state = load_state(self.config)
        self.assertEqual(apply_state_to_queue(self.config, [job], state)[0]["status"], "done")
        output.unlink()
        self.assertEqual(apply_state_to_queue(self.config, [job], state)[0]["status"], "pending")

    def test_psb_mockups_follow_the_same_one_side_rule(self) -> None:
        touch(self.config.png_dir / "design.png")
        touch(self.config.mockup_dir / "shirt.1S.psb")
        jobs = build_queue(self.config, save=False)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["mockup_sides"], 1)
        self.assertEqual(jobs[0]["runner"], "psd")


if __name__ == "__main__":
    unittest.main()
