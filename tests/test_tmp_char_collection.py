from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pipeline import tmp_pipeline


class TmpCharCollectionTests(unittest.TestCase):
    def test_configured_extra_chars_are_deduplicated_and_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "game_chars.txt").write_text("译", encoding="utf-8")
            cfg = SimpleNamespace(root_dir=root, stage_record_dir=root,
                output_game_chars_txt="game_chars.txt", output_tmp_chars_txt="tmp_chars.txt",
                ttf_template_path=root / "template.ttf", include_old_sdf_template_chars=False,
                tmp_extra_chars="霰霰译缺\n")
            with (
                patch.object(tmp_pipeline, "_supported_codepoints_from_ttf", return_value={ord(c) for c in "A译霰中"}),
                patch.object(tmp_pipeline, "_report_translation_chars_missing_from_ttf", return_value=""),
                patch.object(tmp_pipeline, "_supported_codepoints_from_tmp_json", return_value=None),
                patch.object(tmp_pipeline, "collect_tmp_chars_from_resource_input", return_value="A"),
            ):
                output = tmp_pipeline.build_merged_tmp_chars(cfg)
            self.assertEqual(output.read_text(encoding="utf-8"), "A译霰")
            self.assertIn("缺", (root / "merged_chars_removed_unsupported_by_ttf.txt").read_text(encoding="utf-8"))

    def test_does_not_include_unused_template_ttf_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            (records / "game_chars.txt").write_text("译", encoding="utf-8")
            (records / "tmp_chars.txt").write_text("A中译旧", encoding="utf-8")
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
                patch.object(tmp_pipeline, "collect_tmp_chars_from_resource_input", return_value="A缺"),
            ):
                output_path = tmp_pipeline.build_merged_tmp_chars(cfg)

            self.assertEqual("A译", output_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
