from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pipeline import tmp_pipeline


class TmpCharCollectionTests(unittest.TestCase):
    def test_includes_all_template_ttf_characters_including_chinese(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            (records / "game_chars.txt").write_text("译", encoding="utf-8")
            cfg = SimpleNamespace(
                root_dir=root,
                stage_record_dir=records,
                output_game_chars_txt="game_chars.txt",
                output_tmp_chars_txt="tmp_chars.txt",
                ttf_template_path=root / "template.ttf",
                include_old_sdf_template_chars=False,
            )
            supported = {ord("A"), ord("中"), ord("译")}
            with (
                patch.object(tmp_pipeline, "_supported_codepoints_from_ttf", return_value=supported),
                patch.object(tmp_pipeline, "_report_translation_chars_missing_from_ttf", return_value=""),
                patch.object(tmp_pipeline, "_supported_codepoints_from_tmp_json", return_value=None),
                patch.object(tmp_pipeline, "collect_tmp_chars_from_resource_input", return_value=""),
            ):
                output_path = tmp_pipeline.build_merged_tmp_chars(cfg)

            self.assertEqual("A中译", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
