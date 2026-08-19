import os
import sys
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gui
import multi_grabbing


class TargetConfigTests(unittest.TestCase):
    def test_upsert_adds_and_updates_without_duplicates(self):
        first = {"lesson_id": "101", "course_name": "课程A",
                 "limit_count": 50, "enabled": True}
        updated = {"lesson_id": "101", "course_name": "课程A新版",
                   "limit_count": 60, "enabled": True}
        targets = gui.upsert_target([], first)
        targets = gui.upsert_target(targets, updated)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["limit_count"], 60)

    def test_multi_targets_satisfy_run_validation(self):
        cfg = dict(gui.DEFAULT_CONFIG)
        cfg["targets"] = [{"lesson_id": "101", "course_name": "课程A",
                           "limit_count": 50, "enabled": True}]
        _, errors = gui.validate_config(cfg, for_run=True)
        self.assertEqual(errors, [])

    def test_invalid_multi_capacity_is_rejected(self):
        cfg = dict(gui.DEFAULT_CONFIG)
        cfg["targets"] = [{"lesson_id": "101", "course_name": "课程A",
                           "limit_count": 0, "enabled": True}]
        _, errors = gui.validate_config(cfg, for_run=True)
        self.assertTrue(any("容量" in item for item in errors))


class ParallelLauncherTests(unittest.TestCase):
    def test_normalize_targets_deduplicates_and_ignores_disabled(self):
        cfg = {"targets": [
            {"lesson_id": "101", "course_name": "A", "limit_count": 50},
            {"lesson_id": "101", "course_name": "重复", "limit_count": 60},
            {"lesson_id": "202", "course_name": "B", "limit_count": 80,
             "enabled": False},
        ]}
        targets = multi_grabbing.normalize_targets(cfg)
        self.assertEqual([item["lesson_id"] for item in targets], ["101"])

    def test_each_worker_receives_its_own_course_and_capacity(self):
        target = {"lesson_id": "101", "course_name": "课程A", "limit_count": 50}
        command = multi_grabbing.worker_command("python", target, {"mode": "spam"})
        self.assertIn("101", command)
        self.assertIn("课程A", command)
        self.assertIn("50", command)
        self.assertIn("--limit", command)


if __name__ == "__main__":
    unittest.main()
