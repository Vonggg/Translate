from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.translation import (
    _export_translated_files,
    _record_from_dict,
    _record_to_dict,
    _scan_one_translation_json,
)
from pipeline.shared import ScanRecord


class EmbeddedTextCsvTests(unittest.TestCase):
    def _cfg(self, root: Path) -> SimpleNamespace:
        return SimpleNamespace(
            resource_input_root=root / "input",
            translated_dump_dir=root / "output",
            ignore_text=[],
            font_keys=[],
            string_field_blacklist=["m_Script", "m_Name"],
            enable_ai_field_review=True,
            text_keys=[],
        )

    def test_textasset_csv_is_scanned_as_individual_source_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            json_path = cfg.resource_input_root / "bundle" / "TextAsset" / "Localization_1.json"
            json_path.parent.mkdir(parents=True)
            script = (
                "Key,Comments,EN,JA,KO\r\n"
                'weapon.one,,"Launches ink, over walls.",日本語,한국어\r\n'
                "weapon.two,,Second description,日本語,한국어"
            )
            json_path.write_text(json.dumps({"m_Name": "Localization", "m_Script": script}), encoding="utf-8")

            result = _scan_one_translation_json(cfg, json_path, {}, {}, {})

            self.assertIsNone(result["error"])
            self.assertEqual([record.source_text for record in result["records"]], ["Launches ink, over walls.", "Second description"])
            self.assertEqual({record.field for record in result["records"]}, {"m_Script.csv[].EN"})
            self.assertEqual(result["string_field_stats"]["m_Script.csv[].EN"]["count"], 2)
            self.assertEqual(result["records"][0].embedded_locator["row_key"], "weapon.one")

            restored = _record_from_dict(_record_to_dict(result["records"][0]))
            self.assertIsNotNone(restored)
            self.assertEqual(restored.embedded_locator, result["records"][0].embedded_locator)

    def test_export_rewrites_only_selected_csv_cells(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            json_path = cfg.resource_input_root / "bundle" / "TextAsset" / "Localization_1.json"
            json_path.parent.mkdir(parents=True)
            script = (
                "Key,Comments,EN,JA,KO\r\n"
                'weapon.one,,"Launches ink, over walls.",日本語,한국어\r\n'
                "weapon.two,,Second description,日本語2,한국어2"
            )
            json_path.write_text(json.dumps({"m_Name": "Localization", "m_Script": script}), encoding="utf-8")
            scan_result = _scan_one_translation_json(cfg, json_path, {}, {}, {})
            selected_record = scan_result["records"][0]

            _export_translated_files(
                cfg,
                {
                    "Launches ink, over walls.": "向墙后发射墨水。",
                    "Second description": "不应写回",
                },
                [json_path],
                [selected_record],
            )

            output_path = cfg.translated_dump_dir / json_path.relative_to(cfg.resource_input_root)
            output_data = json.loads(output_path.read_text(encoding="utf-8"))
            output_script = output_data["m_Script"]
            self.assertIn("\r\n", output_script)
            rows = list(csv.DictReader(io.StringIO(output_script, newline="")))
            self.assertEqual(rows[0]["EN"], "向墙后发射墨水。")
            self.assertEqual(rows[1]["EN"], "Second description")
            self.assertEqual(rows[0]["JA"], "日本語")
            self.assertEqual(rows[0]["KO"], "한국어")

    def test_export_handles_doubled_quotes_beyond_csv_sniffer_sample(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            json_path = cfg.resource_input_root / "bundle" / "TextAsset" / "Localization_1.json"
            json_path.parent.mkdir(parents=True)
            filler_rows = [
                f'filler.{index},,"Filler text {index}, {"x" * 80}",日本語,한국어'
                for index in range(400)
            ]
            script = "\r\n".join(
                [
                    "Key,Comments,EN,JA,KO",
                    "menu.start,,Start,開始,시작",
                    *filler_rows,
                    'vehicle.repair,,"Press the ""repair"" button.","「""修理""」",수리',
                ]
            )
            self.assertGreater(script.index('""repair""'), 32768)
            json_path.write_text(
                json.dumps({"m_Name": "Localization", "m_Script": script}, ensure_ascii=False),
                encoding="utf-8",
            )

            scan_result = _scan_one_translation_json(cfg, json_path, {}, {}, {})

            self.assertIsNone(scan_result["error"])
            repair_record = next(
                record
                for record in scan_result["records"]
                if record.embedded_locator["row_key"] == "vehicle.repair"
            )
            self.assertEqual(repair_record.source_text, 'Press the "repair" button.')

            translated_text = '点击 "修理" 按钮, 然后继续。'
            _export_translated_files(
                cfg,
                {repair_record.source_text: translated_text},
                [json_path],
                [repair_record],
            )

            output_path = cfg.translated_dump_dir / json_path.relative_to(cfg.resource_input_root)
            output = json.loads(output_path.read_text(encoding="utf-8"))
            rows = list(csv.DictReader(io.StringIO(output["m_Script"], newline="")))
            repair_row = next(row for row in rows if row["Key"] == "vehicle.repair")
            self.assertEqual(repair_row["EN"], translated_text)
            self.assertEqual(repair_row["JA"], '「"修理"」')
            self.assertEqual(repair_row["KO"], "수리")
            expected_columns = {"Key", "Comments", "EN", "JA", "KO"}
            self.assertTrue(
                all(set(row) == expected_columns and None not in row.values() for row in rows)
            )

    def test_ngui_csv_uses_unnamed_first_column_as_localization_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            json_path = cfg.resource_input_root / "bundle" / "TextAsset" / "Localization_1.json"
            json_path.parent.mkdir(parents=True)
            script = (
                ",Description,English,Chinese,AppTutti\r\n"
                "TEXT_OF_MAINTAKS,-,MAIN TASKS,主线任务,主线任务\r\n"
                "recievedAllAwards,-,All rewards received,已领取全部奖励,已领取全部奖励"
            )
            json_path.write_text(
                json.dumps({"m_Name": "Localization", "m_Script": script}, ensure_ascii=False),
                encoding="utf-8",
            )

            scan_result = _scan_one_translation_json(cfg, json_path, {}, {}, {})

            self.assertIsNone(scan_result["error"])
            self.assertEqual(
                [record.source_text for record in scan_result["records"]],
                ["MAIN TASKS", "All rewards received"],
            )
            self.assertEqual(scan_result["records"][0].embedded_locator["row_key"], "TEXT_OF_MAINTAKS")
            self.assertEqual(scan_result["records"][0].embedded_locator["key_column"], "")

            _export_translated_files(
                cfg,
                {"MAIN TASKS": "主线任务"},
                [json_path],
                [scan_result["records"][0]],
            )

            output_path = cfg.translated_dump_dir / json_path.relative_to(cfg.resource_input_root)
            output = json.loads(output_path.read_text(encoding="utf-8"))
            rows = list(csv.reader(io.StringIO(output["m_Script"], newline="")))
            self.assertEqual(rows[1][0], "TEXT_OF_MAINTAKS")
            self.assertEqual(rows[1][2], "主线任务")
            self.assertEqual(rows[2][2], "All rewards received")

    def test_export_clears_stale_text_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            stale_path = cfg.translated_dump_dir / "stale" / "DefaultInputActions_16.json"
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text("dangerous old overlay", encoding="utf-8")
            json_path = cfg.resource_input_root / "bundle" / "MonoBehaviour" / "Label_1.json"
            json_path.parent.mkdir(parents=True)
            json_path.write_text(json.dumps({"m_Text": "Start"}), encoding="utf-8")

            _export_translated_files(
                cfg,
                {"Start": "开始"},
                [json_path],
                [
                    ScanRecord(
                        file_path=str(json_path),
                        field="m_Text",
                        source_text="Start",
                    )
                ],
            )

            self.assertFalse(stale_path.exists())
            output_path = cfg.translated_dump_dir / json_path.relative_to(cfg.resource_input_root)
            self.assertTrue(output_path.is_file())

    def test_ai_field_selection_is_enforced_at_export_time(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cfg = self._cfg(root)
            json_path = cfg.resource_input_root / "bundle" / "MonoBehaviour" / "Mixed_1.json"
            json_path.parent.mkdir(parents=True)
            json_path.write_text(
                json.dumps(
                    {
                        "visibleTitle": "Garage",
                        "runtimeLookup": "Garage",
                        "items": {
                            "Array": [
                                {"caption": "Garage", "lookup": "Garage"}
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            selected_records = [
                ScanRecord(
                    file_path=str(json_path),
                    field="visibleTitle",
                    source_text="Garage",
                ),
                ScanRecord(
                    file_path=str(json_path),
                    field="items.Array[0].caption",
                    source_text="Garage",
                ),
            ]

            _export_translated_files(
                cfg,
                {"Garage": "车库"},
                [json_path],
                selected_records,
            )

            output_path = cfg.translated_dump_dir / json_path.relative_to(cfg.resource_input_root)
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual("车库", output["visibleTitle"])
            self.assertEqual("Garage", output["runtimeLookup"])
            self.assertEqual("车库", output["items"]["Array"][0]["caption"])
            self.assertEqual("Garage", output["items"]["Array"][0]["lookup"])


if __name__ == "__main__":
    unittest.main()
