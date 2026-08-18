import json
import os
import sys
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import gui


class ConfigValidationTests(unittest.TestCase):
    def test_default_config_is_valid_for_saving(self):
        _, errors = gui.validate_config(gui.DEFAULT_CONFIG)
        self.assertEqual(errors, [])

    def test_run_requires_target_and_capacity(self):
        _, errors = gui.validate_config(gui.DEFAULT_CONFIG, for_run=True)
        self.assertTrue(any("lessonId" in item for item in errors))
        self.assertTrue(any("课程容量" in item for item in errors))

    def test_course_name_and_capacity_allow_run(self):
        cfg = dict(gui.DEFAULT_CONFIG, target_course_name="计算机网络", limit_count="120")
        normalized, errors = gui.validate_config(cfg, for_run=True)
        self.assertEqual(errors, [])
        self.assertEqual(normalized["limit_count"], 120)

    def test_invalid_active_hours_is_rejected(self):
        cfg = dict(gui.DEFAULT_CONFIG, active_hours="25:00-26:00")
        _, errors = gui.validate_config(cfg)
        self.assertTrue(any("活跃时段" in item for item in errors))


class ConfigFileTests(unittest.TestCase):
    def test_config_round_trip_preserves_unicode_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "config.json")
            cfg = dict(gui.DEFAULT_CONFIG, target_course_name="计算机网络", future_option="保留")
            gui.save_config_file(cfg, path)
            loaded = gui.load_config_file(path)
            self.assertEqual(loaded["target_course_name"], "计算机网络")
            self.assertEqual(loaded["future_option"], "保留")
            with open(path, encoding="utf-8") as f:
                self.assertIsInstance(json.load(f), dict)


class CourseResultFormattingTests(unittest.TestCase):
    def test_summary_uses_nested_course_names_and_teacher(self):
        item = {
            "id": 181198,
            "nameZh": "人工智能专业学生",
            "code": "AI3002.03",
            "teachers": [{"nameZh": "冯文杰", "nameEn": "Feng Wen Jie"}],
            "course": {
                "nameZh": "人工智能与机器学习基础",
                "nameEn": "Foundations of Artificial Intelligence and Machine Learning",
                "code": "AI3002",
                "credits": 4.0,
            },
            "limitCount": 120,
        }
        summary = gui.course_summary(item)
        self.assertEqual(summary["lesson_id"], "181198")
        self.assertEqual(summary["name_zh"], "人工智能与机器学习基础")
        self.assertEqual(summary["audience"], "人工智能专业学生")
        self.assertEqual(summary["lesson_code"], "AI3002.03")
        self.assertEqual(summary["course_code"], "AI3002")
        self.assertEqual(summary["teachers"], "冯文杰")
        self.assertEqual(summary["credits"], "4.0")

    def test_nested_fields_are_flattened_for_readable_table(self):
        rows = gui.flatten_course_fields({
            "nameZh": "人工智能专业学生",
            "course": {"nameZh": "人工智能"},
            "teachers": [{"nameZh": "冯文杰"}],
        })
        self.assertIn(("授课对象（中文）", "人工智能专业学生"), rows)
        self.assertIn(("课程信息 / 中文课程名", "人工智能"), rows)
        self.assertIn(("教师 [1] / 教师中文名", "冯文杰"), rows)


if __name__ == "__main__":
    unittest.main()
