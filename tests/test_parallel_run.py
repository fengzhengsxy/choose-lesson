import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import multi_grabbing


class FakeProcess:
    next_pid = 1000

    def __init__(self, successful=True):
        self.stdout = iter(["选课成功，退出。\n"] if successful else ["任务失败\n"])
        self.returncode = 0 if successful else 1
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode


class ParallelExecutionTests(unittest.TestCase):
    def test_all_workers_are_started_before_results_are_collected(self):
        targets = [
            {"lesson_id": "101", "course_name": "A", "limit_count": 50},
            {"lesson_id": "202", "course_name": "B", "limit_count": 60},
        ]
        created = []

        def create_process(*_args, **_kwargs):
            process = FakeProcess(successful=True)
            created.append(process)
            return process

        with patch.object(multi_grabbing.subprocess, "Popen", side_effect=create_process):
            code = multi_grabbing.run_parallel(targets, {"mode": "spam"}, "all")
        self.assertEqual(code, 0)
        self.assertEqual(len(created), 2)

    def test_failed_worker_does_not_report_all_success(self):
        targets = [
            {"lesson_id": "101", "course_name": "A", "limit_count": 50},
            {"lesson_id": "202", "course_name": "B", "limit_count": 60},
        ]
        outcomes = iter((True, False))
        with patch.object(
                multi_grabbing.subprocess, "Popen",
                side_effect=lambda *_args, **_kwargs: FakeProcess(next(outcomes))):
            code = multi_grabbing.run_parallel(targets, {"mode": "spam"}, "all")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
