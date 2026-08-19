import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import grabbing
import gui
import multi_grabbing


class StatusEventTests(unittest.TestCase):
    def test_count_lines_become_dashboard_events(self):
        target = {"lesson_id": "101", "course_name": "课程A"}
        self.assertEqual(multi_grabbing.event_from_line(target, "已满 49/50")[:3],
                         ("full", 49, 50))
        self.assertEqual(multi_grabbing.event_from_line(target, "有空位！48/50")[:3],
                         ("available", 48, 50))

    def test_completed_targets_are_skipped_by_launcher(self):
        cfg = {"targets": [
            {"lesson_id": "101", "course_name": "A", "limit_count": 50,
             "status": "completed"},
            {"lesson_id": "202", "course_name": "B", "limit_count": 60,
             "status": "pending"},
        ]}
        targets = multi_grabbing.normalize_targets(cfg)
        self.assertEqual([item["lesson_id"] for item in targets], ["202"])


class CompletionPersistenceTests(unittest.TestCase):
    def test_completed_state_is_written_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            data = {"targets": [
                {"lesson_id": "101", "course_name": "A", "limit_count": 50,
                 "enabled": True, "status": "pending"},
            ]}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f)
            with patch.object(grabbing, "CONFIG_PATH", path):
                self.assertTrue(multi_grabbing.mark_target_completed(
                    "101", "2026-08-19 12:00:00"))
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            self.assertEqual(saved["targets"][0]["status"], "completed")
            self.assertEqual(saved["targets"][0]["completed_at"],
                             "2026-08-19 12:00:00")

    def test_gui_preserves_completion_fields(self):
        targets = gui.normalize_gui_targets({"targets": [{
            "lesson_id": "101", "course_name": "A", "limit_count": 50,
            "status": "completed", "completed_at": "2026-08-19 12:00:00",
        }]})
        self.assertEqual(targets[0]["status"], "completed")
        self.assertEqual(targets[0]["completed_at"], "2026-08-19 12:00:00")

    def test_all_completed_targets_block_start(self):
        cfg = dict(gui.DEFAULT_CONFIG)
        cfg["targets"] = [{"lesson_id": "101", "course_name": "A",
                           "limit_count": 50, "enabled": True,
                           "status": "completed"}]
        _, errors = gui.validate_config(cfg, for_run=True)
        self.assertTrue(any("均已完成" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
