from __future__ import annotations

import unittest

from pipeline.translation import _should_log_file_progress


class FileLogThrottlingTests(unittest.TestCase):
    def test_small_jobs_keep_each_file_visible(self) -> None:
        self.assertTrue(all(_should_log_file_progress(i, 20) for i in range(1, 21)))

    def test_large_jobs_only_log_milestones_and_edges(self) -> None:
        visible = [
            index
            for index in range(1, 5001)
            if _should_log_file_progress(index, 5000)
        ]
        self.assertEqual(visible[:3], [1, 100, 200])
        self.assertEqual(visible[-2:], [4900, 5000])
        self.assertEqual(len(visible), 51)


if __name__ == "__main__":
    unittest.main()
