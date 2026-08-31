from __future__ import annotations

import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from tools.resource_repack_validator import _merge_final_split, _prepare_candidates


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

    def test_prepare_candidates_extracts_only_modified_obb_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input_sources"
            final_root = root / "FinalResult"
            candidate_root = root / "candidate"
            prefix = Path("obb") / "game" / "main.1.obb.contents"
            changed_relative = prefix / "aa" / "Android" / "changed.bundle"
            unchanged_relative = prefix / "aa" / "Android" / "unchanged.bundle"
            for relative, content in (
                (changed_relative, b"old changed"),
                (unchanged_relative, b"same"),
            ):
                path = source_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)

            final_obb = final_root / "obb" / "game" / "main.1.obb"
            final_obb.parent.mkdir(parents=True)
            with zipfile.ZipFile(final_obb, "w") as archive:
                archive.writestr("assets/aa/Android/changed.bundle", b"new changed")
                archive.writestr("assets/aa/Android/unchanged.bundle", b"same")

            state = {
                "staging_root": str(source_root),
                "entries": [
                    {
                        "origin_kind": "obb",
                        "staged_relative": str(changed_relative),
                        "container_relative_assets_obb": "game/main.1.obb",
                        "archive_entry": "assets/aa/Android/changed.bundle",
                        "original_crc32": zlib.crc32(b"old changed") & 0xFFFFFFFF,
                        "original_file_size": len(b"old changed"),
                    },
                    {
                        "origin_kind": "obb",
                        "staged_relative": str(unchanged_relative),
                        "container_relative_assets_obb": "game/main.1.obb",
                        "archive_entry": "assets/aa/Android/unchanged.bundle",
                        "original_crc32": zlib.crc32(b"same") & 0xFFFFFFFF,
                        "original_file_size": len(b"same"),
                    },
                ],
            }

            returned_source, prepared = _prepare_candidates(
                state,
                final_root,
                candidate_root,
            )

            self.assertEqual(returned_source, source_root)
            self.assertEqual(prepared, 1)
            self.assertEqual(
                (candidate_root / changed_relative).read_bytes(),
                b"new changed",
            )
            self.assertFalse((candidate_root / unchanged_relative).exists())

    def test_prepare_candidates_merges_new_obb_split_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input_sources"
            final_root = root / "FinalResult"
            candidate_root = root / "candidate"
            prefix = Path("obb") / "game" / "main.1.obb.contents"
            staged_relative = prefix / "aa" / "Android" / "large.bundle"
            original = source_root / staged_relative
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b"ABCDEF")

            final_obb = final_root / "obb" / "game" / "main.1.obb"
            final_obb.parent.mkdir(parents=True)
            with zipfile.ZipFile(final_obb, "w") as archive:
                archive.writestr("assets/aa/Android/large.bundle.split0", b"ABCD")
                archive.writestr("assets/aa/Android/large.bundle.split1", b"EFGH")
                archive.writestr("assets/aa/Android/large.bundle.split2", b"IJK")

            state = {
                "staging_root": str(source_root),
                "entries": [
                    {
                        "origin_kind": "obb",
                        "staged_relative": str(staged_relative),
                        "container_relative_assets_obb": "game/main.1.obb",
                        "split_parts": [
                            {
                                "name": "large.bundle.split0",
                                "archive_entry": "assets/aa/Android/large.bundle.split0",
                                "size": 4,
                                "original_crc32": zlib.crc32(b"ABCD") & 0xFFFFFFFF,
                                "original_file_size": 4,
                            },
                            {
                                "name": "large.bundle.split1",
                                "archive_entry": "assets/aa/Android/large.bundle.split1",
                                "size": 2,
                                "original_crc32": zlib.crc32(b"EF") & 0xFFFFFFFF,
                                "original_file_size": 2,
                            },
                        ],
                    }
                ],
            }

            _source, prepared = _prepare_candidates(
                state,
                final_root,
                candidate_root,
            )

            self.assertEqual(prepared, 1)
            self.assertEqual(
                (candidate_root / staged_relative).read_bytes(),
                b"ABCDEFGHIJK",
            )


if __name__ == "__main__":
    unittest.main()
