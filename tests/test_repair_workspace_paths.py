from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.repair_workspace_paths import repair_all


class RepairWorkspacePathsTests(unittest.TestCase):
    def test_dry_run_and_apply_use_containing_workspace_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "workspace甲"
            second = root / "workspace乙"
            first_map = first / "AllPNG" / "_allpng_map.json"
            second_state = second / "records" / "scan_state.json"
            first_map.parent.mkdir(parents=True)
            second_state.parent.mkdir(parents=True)

            stale_first = str(root / "workspace" / "input" / "image.png")
            correct_second = str(second / "input" / "item.json")
            first_map.write_text(f'{{"path": "{stale_first.replace(chr(92), chr(92) * 2)}"}}', encoding="utf-8")
            second_state.write_text(
                f'{{"path": "{correct_second.replace(chr(92), chr(92) * 2)}"}}',
                encoding="utf-8",
            )
            stale_cache = first / "records" / "object_graph_cache.pkl"
            stale_cache.parent.mkdir(parents=True, exist_ok=True)
            stale_cache.write_bytes(f"cache:{root / 'workspace' / 'input'}".encode("utf-8"))
            source_file = root / "game" / "data.unity3d"
            source_file.parent.mkdir()
            source_file.write_bytes(b"source data")
            resource_map = first / "resource_state" / "resource_source_map.json"
            resource_map.parent.mkdir(parents=True, exist_ok=True)
            resource_map.write_text(
                "{\n"
                f'  "staging_root": "{str(first / "input_sources").replace(chr(92), chr(92) * 2)}",\n'
                '  "entries": [\n'
                "    {\n"
                '      "staged_relative": "bin\\\\Data\\\\data.unity3d",\n'
                f'      "source_path": "{str(source_file).replace(chr(92), chr(92) * 2)}"\n'
                "    }\n"
                "  ]\n"
                "}",
                encoding="utf-8",
            )

            preview = repair_all(root, apply=False)
            self.assertEqual(preview.changed_files, 1)
            self.assertEqual(preview.stale_caches, 1)
            self.assertEqual(preview.restored_files, 1)
            self.assertIn("workspace\\\\input", first_map.read_text(encoding="utf-8"))
            self.assertTrue(stale_cache.exists())
            self.assertFalse((first / "input_sources" / "bin" / "Data" / "data.unity3d").exists())

            applied = repair_all(root, apply=True)
            self.assertEqual(applied.changed_files, 1)
            self.assertIn("workspace甲\\\\input", first_map.read_text(encoding="utf-8"))
            self.assertIn("workspace乙\\\\input", second_state.read_text(encoding="utf-8"))
            self.assertFalse(stale_cache.exists())
            restored_file = first / "input_sources" / "bin" / "Data" / "data.unity3d"
            self.assertEqual(restored_file.read_bytes(), b"source data")

            repeated = repair_all(root, apply=True)
            self.assertEqual(repeated.changed_files, 0)
            self.assertEqual(repeated.stale_caches, 0)
            self.assertEqual(repeated.restored_files, 0)


if __name__ == "__main__":
    unittest.main()
