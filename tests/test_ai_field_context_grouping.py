from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pipeline.translation import (
    _merge_string_field_stats,
    _record_matches_ai_field_selection,
    _scan_one_translation_json,
    _write_string_field_stats,
)
from support.config import load_config


class AIFieldContextGroupingTests(unittest.TestCase):
    def _write_component(
        self,
        root: Path,
        name: str,
        script_path_id: int,
        caption: str,
        extra: dict,
    ) -> Path:
        path = root / "bin" / "Data" / "demo" / "MonoBehaviour" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "m_GameObject": {"m_FileID": 0, "m_PathID": script_path_id},
            "m_Script": {"m_FileID": 1, "m_PathID": script_path_id},
            "caption": caption,
            "mText": caption,
            **extra,
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_only_structurally_ambiguous_fields_are_split(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            input_root = base / "input"
            record_dir = base / "records"
            cfg = replace(
                load_config(quiet=True),
                resource_input_root=input_root,
                record_dir=record_dir,
                enable_ai_field_review=True,
                ignore_text=[],
            )
            paths = [
                self._write_component(
                    input_root,
                    "LoaderA.json",
                    100,
                    "Garage",
                    {"loading": {"m_FileID": 0, "m_PathID": 5}},
                ),
                self._write_component(
                    input_root,
                    "LoaderB.json",
                    100,
                    "BattleArena",
                    {"loading": {"m_FileID": 0, "m_PathID": 6}},
                ),
                self._write_component(
                    input_root,
                    "Label.json",
                    200,
                    "Difficulty Level",
                    {"description": "Choose a difficulty"},
                ),
            ]

            merged = {}
            records = []
            for path in paths:
                result = _scan_one_translation_json(cfg, path, {}, {}, {})
                self.assertIsNone(result["error"])
                records.extend(result["records"])
                _merge_string_field_stats(merged, result["string_field_stats"])
            _write_string_field_stats(cfg, merged)

            stats = json.loads(
                (record_dir / cfg.output_string_field_stats_json).read_text(encoding="utf-8")
            )
            self.assertTrue(stats["caption"]["context_sensitive"])
            self.assertEqual(2, len(stats["caption"]["contexts"]))
            self.assertFalse(stats["mText"]["context_sensitive"])

            caption_selectors = {
                context["script_type"]: context["selector"]
                for context in stats["caption"]["contexts"].values()
            }
            selected = {caption_selectors["m_Script(pathID=200)"]}
            caption_records = [record for record in records if record.field == "caption"]
            matches = [
                _record_matches_ai_field_selection(
                    cfg,
                    record,
                    selected,
                    {"caption"},
                    {},
                )
                for record in caption_records
            ]
            self.assertEqual([False, False, True], matches)

            review = (record_dir / cfg.output_string_field_review_txt).read_text(encoding="utf-8")
            self.assertEqual(2, review.count("field: caption@@context_"))
            self.assertEqual(1, sum(line == "field: mText" for line in review.splitlines()))
            self.assertIn("sibling_schema:", review)
            self.assertIn("file: bin", review)


if __name__ == "__main__":
    unittest.main()
