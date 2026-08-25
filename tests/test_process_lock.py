from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support.process_lock import interprocess_file_lock


class InterprocessFileLockTests(unittest.TestCase):
    def test_second_process_waits_until_lock_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            lock_path = root / "shared.lock"
            marker_path = root / "child-entered.txt"
            script = (
                "from pathlib import Path; import sys; "
                "from support.process_lock import interprocess_file_lock; "
                "lock=Path(sys.argv[1]); marker=Path(sys.argv[2]); "
                "ctx=interprocess_file_lock(lock, label='child', poll_seconds=0.05); "
                "ctx.__enter__(); marker.write_text('entered', encoding='utf-8'); ctx.__exit__(None,None,None)"
            )

            with interprocess_file_lock(lock_path, label="parent", poll_seconds=0.05):
                child = subprocess.Popen(
                    [sys.executable, "-c", script, str(lock_path), str(marker_path)],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                time.sleep(0.25)
                self.assertFalse(marker_path.exists())

            output, _ = child.communicate(timeout=10)
            self.assertEqual(child.returncode, 0, output)
            self.assertTrue(marker_path.is_file())


if __name__ == "__main__":
    unittest.main()
