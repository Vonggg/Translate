from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from main import clean_scan_artifacts, scan_generated_artifact_paths


class ScanArtifactCleanupTests(unittest.TestCase):
    def test_only_scan_outputs_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir) / "records"
            record_dir.mkdir()
            cfg = SimpleNamespace(
                record_dir=record_dir,
                stage_record_dir=record_dir,
                output_scan_records_json="records.json",
                output_ids_json="ids.json",
                output_font_map_json="font_map.json",
                output_material_map_json="material_map.json",
                output_ref_map_json="ref_map.json",
                output_path_id_map_json="path_id_map.json",
                scan_state_path=record_dir / "scan_state.json",
                scan_cache_path=record_dir / "scan_cache",
                enable_ai_field_review=True,
                output_string_field_stats_json="string_field_stats.json",
                output_string_field_stats_tsv="string_field_stats.tsv",
                output_string_field_review_txt="string_field_review.txt",
            )
            for path in scan_generated_artifact_paths(cfg):
                if path.suffix:
                    path.write_text("scan", encoding="utf-8")
                else:
                    path.mkdir()
                    (path / "cache.json").write_text("cache", encoding="utf-8")

            file_id_map = record_dir / "file_id_map.json"
            trans = record_dir / "trans.json"
            file_id_map.write_text("export", encoding="utf-8")
            trans.write_text("translation", encoding="utf-8")

            self.assertEqual(clean_scan_artifacts(cfg), len(scan_generated_artifact_paths(cfg)))
            self.assertTrue(file_id_map.is_file())
            self.assertTrue(trans.is_file())
            self.assertFalse(any(path.exists() for path in scan_generated_artifact_paths(cfg)))


if __name__ == "__main__":
    unittest.main()
