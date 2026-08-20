from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.bitmap_font_detection import (
    UnsupportedBitmapFontError,
    detect_bitmap_fonts,
    detect_ngui_dynamic_ttf_labels,
    detect_tmp_sdf_font_assets,
    load_font_pipeline_modes,
    run_bitmap_font_detection,
)


class BitmapFontDetectionTests(unittest.TestCase):
    def test_detects_ngui_dynamic_ttf_label_and_reports_step_six_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            record_root = root / "records"
            label_path = input_root / "bundle" / "MonoBehaviour" / "UILabel_13.json"
            label_path.parent.mkdir(parents=True)
            label_path.write_text(
                json.dumps(
                    {
                        "mText": "Hello",
                        "mFontSize": 20,
                        "mFont": {"m_FileID": 0, "m_PathID": 0},
                        "mTrueTypeFont": {"m_FileID": 2, "m_PathID": 82},
                        "mFontStyle": 0,
                        "mAlignment": 0,
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(resource_input_root=input_root, stage_record_dir=record_root)

            summary = detect_ngui_dynamic_ttf_labels(
                input_root,
                exported_resources_only=True,
            )
            report_path = run_bitmap_font_detection(cfg, "dynamic-ngui")
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(1, summary["label_count"])
            self.assertEqual(1, summary["reference_count"])
            self.assertEqual(2, summary["references"][0]["file_id"])
            self.assertEqual(82, summary["references"][0]["path_id"])
            self.assertEqual(summary, report["ngui_dynamic_ttf"])
            self.assertEqual(0, report["supported_ngui_count"])
            self.assertEqual({"tmp_sdf": 0, "ngui": 0}, load_font_pipeline_modes(cfg))

    def test_detects_text_bmfont_descriptor_inside_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "m_Name": "font-data",
                "m_Script": (
                    'info face="Demo" size=32\n'
                    'common lineHeight=32 base=26 scaleW=256 scaleH=256 pages=1\n'
                    'char id=65 x=2 y=3 width=18 height=22 xoffset=0 yoffset=2 xadvance=19 page=0 chnl=15'
                ),
            }
            (root / "font.json").write_text(json.dumps(payload), encoding="utf-8")

            detections = detect_bitmap_fonts(root)

            self.assertEqual(1, len(detections))
            self.assertEqual("bmfont_descriptor", detections[0]["type"])
            self.assertEqual("confirmed", detections[0]["confidence"])

    def test_detects_ngui_serialized_bmfont(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "m_Name": "UIFont",
                "mFont": {
                    "mSize": 32,
                    "mBase": 26,
                    "mWidth": 512,
                    "mHeight": 512,
                    "mSpriteName": "font-region",
                    "mSaved": {
                        "Array": [
                            {
                                "index": 65,
                                "x": 2,
                                "y": 3,
                                "width": 18,
                                "height": 22,
                                "offsetX": 0,
                                "offsetY": 2,
                                "advance": 19,
                            }
                        ]
                    },
                },
            }
            (root / "ui-font.json").write_text(json.dumps(payload), encoding="utf-8")

            detections = detect_bitmap_fonts(root)

            self.assertEqual(1, len(detections))
            self.assertEqual("ngui_bitmap_font", detections[0]["type"])
            self.assertEqual("confirmed", detections[0]["confidence"])

    def test_does_not_mistake_tmp_font_for_ngui(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = {
                "m_Name": "TMP Font Asset",
                "m_GlyphTable": {"Array": [{"m_Index": 1, "m_GlyphRect": {"m_X": 0}}]},
                "m_CharacterTable": {"Array": [{"m_Unicode": 65, "m_GlyphIndex": 1}]},
                "m_AtlasWidth": 1024,
                "m_AtlasHeight": 1024,
            }
            (root / "tmp-font.json").write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual([], detect_bitmap_fonts(root))

    def test_detects_tmp_sdf_font_asset_structurally(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            font_path = root / "bundle" / "MonoBehaviour" / "tmp-font.json"
            font_path.parent.mkdir(parents=True)
            font_path.write_text(
                json.dumps(
                    {
                        "m_Name": "TMP Font Asset",
                        "m_GlyphTable": {"Array": []},
                        "m_CharacterTable": {"Array": []},
                        "m_FaceInfo": {"m_PointSize": 72},
                    }
                ),
                encoding="utf-8",
            )

            sources = detect_tmp_sdf_font_assets(root, exported_resources_only=True)

            self.assertEqual(["bundle/MonoBehaviour/tmp-font.json"], sources)

    def test_detects_binary_fnt_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "font.fnt").write_bytes(b"BMF\x03\x01\x00\x00\x00")

            detections = detect_bitmap_fonts(root)

            self.assertEqual(1, len(detections))
            self.assertEqual("bmfont_descriptor", detections[0]["type"])

    def test_confirmed_ngui_font_is_allowed_for_fresh_and_cached_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            record_root = root / "records"
            font_path = input_root / "bundle" / "MonoBehaviour" / "UIFont_1.json"
            font_path.parent.mkdir(parents=True)
            font_path.write_text(
                json.dumps(
                    {
                        "mFont": {
                            "mSize": 32,
                            "mBase": 26,
                            "mWidth": 512,
                            "mHeight": 512,
                            "mSaved": {
                                "Array": [
                                    {
                                        "index": 65,
                                        "x": 2,
                                        "y": 3,
                                        "width": 18,
                                        "height": 22,
                                        "advance": 19,
                                    }
                                ]
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(resource_input_root=input_root, stage_record_dir=record_root)

            report_path = run_bitmap_font_detection(cfg, "same-input", fail_on_confirmed=True)
            cached_report_path = run_bitmap_font_detection(cfg, "same-input", fail_on_confirmed=True)

            self.assertEqual(report_path, cached_report_path)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(1, report["supported_ngui_count"])
            self.assertEqual(0, report["tmp_sdf_count"])
            self.assertEqual(0, report["unsupported_confirmed_count"])

            modes = load_font_pipeline_modes(cfg)
            self.assertEqual({"tmp_sdf": 0, "ngui": 1}, modes)

    def test_confirmed_non_ngui_bmfont_stops_for_fresh_and_cached_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            record_root = root / "records"
            font_path = input_root / "bundle" / "TextAsset" / "font-data.json"
            font_path.parent.mkdir(parents=True)
            font_path.write_text(
                json.dumps(
                    {
                        "m_Name": "font-data",
                        "m_Script": (
                            'info face="Demo" size=32\n'
                            'common lineHeight=32 base=26 scaleW=256 scaleH=256 pages=1\n'
                            'char id=65 x=2 y=3 width=18 height=22 '
                            'xoffset=0 yoffset=2 xadvance=19 page=0 chnl=15'
                        ),
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(resource_input_root=input_root, stage_record_dir=record_root)

            with self.assertRaises(UnsupportedBitmapFontError) as fresh_error:
                run_bitmap_font_detection(cfg, "same-input", fail_on_confirmed=True)
            with self.assertRaises(UnsupportedBitmapFontError) as cached_error:
                run_bitmap_font_detection(cfg, "same-input", fail_on_confirmed=True)

            self.assertEqual(1, fresh_error.exception.confirmed_count)
            self.assertEqual(1, cached_error.exception.confirmed_count)


if __name__ == "__main__":
    unittest.main()
