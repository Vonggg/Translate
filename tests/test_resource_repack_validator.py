from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.resource_repack_validator import _merge_final_split


class ResourceRepackValidatorTests(unittest.TestCase):
    def _entry(self) -> dict[str, object]:
        return {
            "split_parts": [
                {"name": "large.bundle.split0", "size": 4},
                {"name": "large.bundle.split1", "size": 4},
                {"name": "large.bundle.split2", "size": 2},
            ]
        }

    def test_merge_accepts_fixed_chunk_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "large.bundle.split0").write_bytes(b"ABCD")
            (root / "large.bundle.split1").write_bytes(b"EFGH")
            (root / "large.bundle.split2").write_bytes(b"I")
            destination = root / "candidate" / "large.bundle"

            self.assertTrue(
                _merge_final_split(self._entry(), root / "large.bundle", destination)
            )
            self.assertEqual(destination.read_bytes(), b"ABCDEFGHI")

    def test_merge_rejects_evenly_rebalanced_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "large.bundle.split0").write_bytes(b"ABC")
            (root / "large.bundle.split1").write_bytes(b"DEF")
            (root / "large.bundle.split2").write_bytes(b"GHI")

            with self.assertRaisesRegex(RuntimeError, "split 边界错误"):
                _merge_final_split(
                    self._entry(),
                    root / "large.bundle",
                    root / "candidate" / "large.bundle",
                )


if __name__ == "__main__":
    unittest.main()
