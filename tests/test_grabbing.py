import os
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import grabbing


class StdCountParsingTests(unittest.TestCase):
    def test_mapping_keyed_by_lesson_id(self):
        self.assertEqual(grabbing._parse_std_count({"123": 17}, "123"), 17)

    def test_object_with_lesson_id_and_count(self):
        data = {"data": [{"lessonId": 123, "stdCount": 17}]}
        self.assertEqual(grabbing._parse_std_count(data, "123"), 17)

    def test_single_lesson_count_wrapper(self):
        self.assertEqual(grabbing._parse_std_count({"stdCount": "17"}, "123"), 17)

    def test_boolean_is_not_a_count(self):
        self.assertIsNone(grabbing._parse_std_count({"stdCount": True}, "123"))


class SafetyDecisionTests(unittest.TestCase):
    def test_automatic_mode_only_submits_during_active_hours(self):
        self.assertTrue(grabbing._should_submit("spam", True))
        self.assertTrue(grabbing._should_submit("grab", True))
        self.assertFalse(grabbing._should_submit("spam", False))
        self.assertFalse(grabbing._should_submit("monitor", True))

    def test_cross_midnight_active_hours(self):
        with patch.object(grabbing, "_now_minutes", return_value=23 * 60):
            self.assertTrue(grabbing._in_active_hours("6:30-1:00"))
        with patch.object(grabbing, "_now_minutes", return_value=3 * 60):
            self.assertFalse(grabbing._in_active_hours("6:30-1:00"))

    def test_invalid_active_hours_fails_closed(self):
        self.assertFalse(grabbing._in_active_hours("25:00-26:00"))
        self.assertFalse(grabbing._in_active_hours("not-a-time"))


class TargetLookupTests(unittest.TestCase):
    def test_course_name_lookup_returns_lesson(self):
        data = [{"id": 456, "nameZh": "计算机网络"}]
        target, _ = grabbing.find_target(data, course_name="计算机")
        self.assertEqual(grabbing._lesson_id(target), "456")

    def test_lesson_id_has_priority_over_earlier_name_match(self):
        data = [
            {"id": 111, "nameZh": "计算机网络"},
            {"id": 456, "nameZh": "计算机组成原理"},
        ]
        target, _ = grabbing.find_target(data, lesson_id="456", course_name="计算机")
        self.assertEqual(grabbing._lesson_id(target), "456")

    def test_course_name_does_not_use_teaching_audience(self):
        item = {
            "id": 456,
            "nameZh": "人工智能专业学生",
            "course": {"nameZh": "机器学习基础"},
        }
        self.assertEqual(grabbing._name(item), "机器学习基础")
        target, _ = grabbing.find_target([item], course_name="人工智能专业学生")
        self.assertIsNone(target)


class LessonDisplayTests(unittest.TestCase):
    def test_default_display_includes_every_lesson(self):
        items = [{"id": number, "nameZh": f"课程{number}"} for number in range(1, 26)]
        output = StringIO()
        with redirect_stdout(output):
            grabbing.print_lessons(items)
        text = output.getvalue()
        self.assertIn("共找到 25 门可选课程，显示 25 门", text)
        self.assertIn("课程25", text)

    def test_filter_matches_name_or_partial_lesson_id(self):
        items = [
            {"id": 123456, "nameZh": "计算机网络", "extra": {"campus": "东区"}},
            {"id": 987654, "nameZh": "大学物理"},
        ]
        self.assertEqual(len(grabbing.filter_lessons(items, "计算机")), 1)
        self.assertEqual(len(grabbing.filter_lessons(items, "3456")), 1)
        self.assertEqual(grabbing.filter_lessons(items, "不存在"), [])

    def test_fuzzy_filter_supports_abbreviation_and_typo(self):
        items = [
            {"id": 123456, "nameZh": "计算机网络"},
            {"id": 987654, "nameZh": "大学物理"},
        ]
        self.assertEqual(grabbing._lesson_id(grabbing.filter_lessons(items, "计网")[0]), "123456")
        self.assertEqual(grabbing._lesson_id(grabbing.filter_lessons(items, "计算机网路")[0]), "123456")

    def test_fuzzy_results_are_ranked_by_relevance(self):
        items = [
            {"id": 1, "nameZh": "高级计算机网络专题"},
            {"id": 2, "nameZh": "计算机网络"},
            {"id": 3, "nameZh": "计算机组成原理"},
        ]
        matches = grabbing.filter_lessons(items, "计算机网络")
        self.assertEqual(grabbing._lesson_id(matches[0]), "2")

    def test_full_details_include_all_raw_fields(self):
        items = [{"id": 123456, "nameZh": "计算机网络", "extra": {"campus": "东区"}}]
        output = StringIO()
        with redirect_stdout(output):
            grabbing.print_lesson_details(items, "计算机")
        text = output.getvalue()
        self.assertIn('"extra"', text)
        self.assertIn('"campus": "东区"', text)


if __name__ == "__main__":
    unittest.main()
