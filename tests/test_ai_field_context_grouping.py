from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline.translation import (
    _merge_string_field_stats,
    _record_matches_ai_field_selection,
    _scan_one_translation_json,
    _write_scan_artifacts,
    _write_string_field_stats,
    apply_ai_field_selection_to_records,
)
from support.config import load_config


class AIFieldContextGroupingTests(unittest.TestCase):
    def _prepare_local_policy_scan(
        self,
        base: Path,
        *,
        include_unknown: bool = True,
    ) -> tuple[object, Path]:
        input_root = base / "input"
        record_dir = base / "records"
        json_path = input_root / "bundle" / "MonoBehaviour" / "Panel.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "m_GameObject": {"m_FileID": 0, "m_PathID": 10},
            "m_Script": {"m_FileID": 1, "m_PathID": 20},
            "m_Text": "Start Game",
            "shopProducts": {
                "Array": [
                    {
                        "productName": "coins_100",
                        "idGooglePlay": "coins.android",
                        "idAmazon": "coins.amazon",
                        "idIos": "coins.ios",
                    }
                ]
            },
        }
        if include_unknown:
            data["caption"] = "Welcome challenger"
        json_path.write_text(json.dumps(data), encoding="utf-8")
        cfg = replace(
            load_config(quiet=True),
            resource_input_root=input_root,
            record_dir=record_dir,
            enable_ai_field_review=True,
            ignore_text=[],
        )
        result = _scan_one_translation_json(cfg, json_path, {}, {}, {})
        self.assertIsNone(result["error"])
        merged = {}
        _merge_string_field_stats(merged, result["string_field_stats"])
        _write_scan_artifacts(
            cfg,
            result["records"],
            {},
            {},
            {},
            {},
            merged,
        )
        return cfg, record_dir

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
            self.assertTrue(stats["mText"]["context_sensitive"])

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
            self.assertEqual(2, review.count("field: mText@@context_"))
            self.assertIn("sibling_schema:", review)
            self.assertIn("file: bin", review)

    def test_local_allow_is_merged_and_local_protect_cannot_be_reselected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, record_dir = self._prepare_local_policy_scan(Path(temp_dir))
            review = (record_dir / cfg.output_string_field_review_txt).read_text(encoding="utf-8")
            self.assertIn("field: caption", review)
            self.assertNotIn("field: m_Text", review)
            self.assertNotIn("field: shopProducts.Array[].productName", review)
            with (record_dir / cfg.output_string_field_review_txt).open(
                "a",
                encoding="utf-8",
            ) as stream:
                stream.write(
                    "\nfield: staleCaption\n"
                    "normalized_field: staleCaption\n"
                    "sample_values: sample_total=1, sample_shown=1\n"
                    "- Stale candidate\n"
                )

            with patch(
                "pipeline.translation._request_ai_field_selection",
                return_value=[
                    "caption",
                    "shopProducts.Array[].productName",
                    "staleCaption",
                ],
            ):
                apply_ai_field_selection_to_records(cfg)

            records = json.loads(
                (record_dir / cfg.output_scan_records_json).read_text(encoding="utf-8")
            )
            self.assertEqual(
                {"m_Text", "caption"},
                {item["field"] for item in records},
            )

    def test_empty_ai_selection_is_valid_and_keeps_only_local_allow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, record_dir = self._prepare_local_policy_scan(Path(temp_dir))
            with patch(
                "pipeline.translation._request_ai_field_selection",
                return_value=[],
            ):
                apply_ai_field_selection_to_records(cfg)

            records = json.loads(
                (record_dir / cfg.output_scan_records_json).read_text(encoding="utf-8")
            )
            self.assertEqual(["m_Text"], [item["field"] for item in records])

    def test_missing_stats_cannot_turn_header_only_review_into_delete_all(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, record_dir = self._prepare_local_policy_scan(
                Path(temp_dir),
                include_unknown=False,
            )
            (record_dir / cfg.output_string_field_stats_json).unlink()
            records_path = record_dir / cfg.output_scan_records_json
            before = records_path.read_text(encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "string_field_stats.json"):
                apply_ai_field_selection_to_records(cfg)

            self.assertEqual(before, records_path.read_text(encoding="utf-8"))

    def test_no_unknown_fields_skips_ai_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg, record_dir = self._prepare_local_policy_scan(
                Path(temp_dir),
                include_unknown=False,
            )
            with patch(
                "pipeline.translation._request_ai_field_selection"
            ) as request_mock:
                apply_ai_field_selection_to_records(cfg)

            request_mock.assert_not_called()
            records = json.loads(
                (record_dir / cfg.output_scan_records_json).read_text(encoding="utf-8")
            )
            self.assertEqual(["m_Text"], [item["field"] for item in records])


if __name__ == "__main__":
    unittest.main()
