from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.translation import _i2_bound_game_objects_for_translations, write_runtime_text_binding_report
from pipeline.shared import ScanRecord


class RuntimeTextBindingReportTests(unittest.TestCase):
    def test_i2_and_no_game_object_sources_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            record_dir = root / "records"
            i2_path = input_root / "MonoBehaviour" / "I2Languages_1.json"
            plain_path = input_root / "MonoBehaviour" / "RuntimeStrings_2.json"
            static_path = input_root / "MonoBehaviour" / "Text_3.json"
            for path in (i2_path, plain_path, static_path):
                path.parent.mkdir(parents=True, exist_ok=True)
            i2_path.write_text(
                json.dumps({"mSource": {"mTerms": {"Array": [{"Languages": {"Array": []}}]}}}),
                encoding="utf-8",
            )
            plain_path.write_text(json.dumps({"m_Name": "Runtime Strings"}), encoding="utf-8")
            static_path.write_text(json.dumps({"m_GameObject": {"m_PathID": 77}}), encoding="utf-8")
            cfg = SimpleNamespace(
                resource_input_root=input_root,
                stage_record_dir=record_dir,
                output_runtime_text_binding_report_json="runtime_text_binding_report.json",
            )
            report_path = write_runtime_text_binding_report(
                cfg,
                [
                    ScanRecord(str(i2_path), "mSource.mTerms.Array[0].Languages", "银行", path_id=0),
                    ScanRecord(str(plain_path), "title", "运行时标题", path_id=None),
                    ScanRecord(str(static_path), "m_Text", "静态文本", path_id=77),
                ],
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]["i2_language_tables"], 1)
            self.assertEqual(report["summary"]["runtime_or_scriptable_sources"], 1)
            self.assertEqual(report["summary"]["runtime_bound_record_count"], 2)
            self.assertNotIn("静态文本", [text for source in report["sources"] for text in source["source_texts"]])

    def test_i2_binding_is_matched_by_translated_term(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_root = Path(temp_dir) / "input"
            localize_path = input_root / "level0" / "MonoBehaviour" / "Localize_1.json"
            localize_path.parent.mkdir(parents=True)
            localize_path.write_text(
                json.dumps(
                    {
                        "m_GameObject": {"m_PathID": 153},
                        "mTerm": "BANK",
                        "mLocalizeTargetName": "I2.Loc.LocalizeTarget_TextMeshPro_UGUI",
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(resource_input_root=input_root)
            self.assertEqual(
                _i2_bound_game_objects_for_translations(cfg, {"BANK": "银行"}),
                {("level0", 153)},
            )
            self.assertEqual(
                _i2_bound_game_objects_for_translations(cfg, {"PRODUCTS": "产品"}),
                set(),
            )


if __name__ == "__main__":
    unittest.main()
