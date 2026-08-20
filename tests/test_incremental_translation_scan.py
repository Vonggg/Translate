from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline import translation
from support.config import load_config


class IncrementalTranslationScanTests(unittest.TestCase):
    def test_completed_ai_scan_is_reused_and_changed_file_is_rescanned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            record_root = root / "records"
            json_path = input_root / "bundle" / "MonoBehaviour" / "Panel_1.json"
            json_path.parent.mkdir(parents=True)
            json_path.write_text(
                json.dumps(
                    {
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 10},
                        "m_Text": "Hello player",
                    }
                ),
                encoding="utf-8",
            )
            cfg = replace(
                load_config(quiet=True),
                resource_input_root=input_root,
                record_dir=record_root,
                enable_ai_field_review=True,
                max_scan_workers=1,
            )

            translation.scan_and_record(cfg)
            records_path = record_root / cfg.output_scan_records_json
            first_records = json.loads(records_path.read_text(encoding="utf-8"))
            self.assertEqual([item["source_text"] for item in first_records], ["Hello player"])

            records_path.write_text("[]", encoding="utf-8")
            with patch.object(
                translation,
                "_scan_one_translation_json",
                side_effect=AssertionError("unchanged input must not be parsed again"),
            ):
                translation.scan_and_record(cfg)
            restored_records = json.loads(records_path.read_text(encoding="utf-8"))
            self.assertEqual([item["source_text"] for item in restored_records], ["Hello player"])

            json_path.write_text(
                json.dumps(
                    {
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 10},
                        "m_Text": "Changed player text",
                    }
                ),
                encoding="utf-8",
            )
            original_scan = translation._scan_one_translation_json
            with patch.object(translation, "_scan_one_translation_json", wraps=original_scan) as scanner:
                translation.scan_and_record(cfg)
            self.assertEqual(scanner.call_count, 1)
            changed_records = json.loads(records_path.read_text(encoding="utf-8"))
            self.assertEqual([item["source_text"] for item in changed_records], ["Changed player text"])


if __name__ == "__main__":
    unittest.main()
