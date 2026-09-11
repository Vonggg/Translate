from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from pipeline.manifest_index import TMP_MANIFEST_INDEX_SCHEMA_VERSION
from pipeline.ngui_font import (
    GENERATION_MANIFEST_NAME,
    REMOVED_UNSUPPORTED_CHARS_NAME,
    REMOVED_UNSUPPORTED_DETAILS_NAME,
    _translated_characters,
    generate_ngui_fonts,
    prepare_generated_ngui_import_replacements,
)


class NguiFontPipelineTests(unittest.TestCase):
    def _fixture(self, root: Path) -> SimpleNamespace:
        input_root = root / "input"
        records = root / "records"
        generated = root / "output" / "Font" / "NGUI" / "generated"
        import_dir = root / "output" / "Font" / "NGUI" / "ToImport"
        manifest_dir = input_root / "bin" / "Data" / "demo"
        asset_dir = manifest_dir / "bundle" / "shared.assets"
        texture_path = asset_dir / "Texture2D" / "MainAtlas_10.png"
        material_path = asset_dir / "Material" / "MainAtlas_20.json"
        atlas_path = asset_dir / "MonoBehaviour" / "MainAtlas_30.json"
        font_path = asset_dir / "MonoBehaviour" / "TestFont_40.json"
        for path in (texture_path, material_path, atlas_path, font_path):
            path.parent.mkdir(parents=True, exist_ok=True)
        records.mkdir(parents=True)

        original = Image.new("RGBA", (128, 128), (20, 30, 40, 255))
        draw = ImageDraw.Draw(original)
        draw.rectangle((0, 0, 63, 63), fill=(255, 255, 255, 255))
        draw.rectangle((64, 0, 95, 31), fill=(200, 10, 20, 255))
        original.save(texture_path)

        material_path.write_text(
            json.dumps(
                {
                    "m_SavedProperties": {
                        "m_TexEnvs": {
                            "Array": [
                                {
                                    "first": "_MainTex",
                                    "second": {"m_Texture": {"m_FileID": 0, "m_PathID": 10}},
                                }
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        atlas_path.write_text(
            json.dumps(
                {
                    "m_Name": "MainAtlas",
                    "material": {"m_FileID": 0, "m_PathID": 20},
                    "mSprites": {
                        "Array": [
                            {
                                "name": "TestFont",
                                "x": 16,
                                "y": 8,
                                "width": 64,
                                "height": 64,
                                "borderLeft": 0,
                                "borderRight": 0,
                                "borderTop": 0,
                                "borderBottom": 0,
                                "paddingLeft": 0,
                                "paddingRight": 0,
                                "paddingTop": 0,
                                "paddingBottom": 0,
                            },
                            {
                                "name": "Button",
                                "x": 64,
                                "y": 0,
                                "width": 32,
                                "height": 32,
                                "borderLeft": 1,
                                "borderRight": 1,
                                "borderTop": 1,
                                "borderBottom": 1,
                                "paddingLeft": 0,
                                "paddingRight": 0,
                                "paddingTop": 0,
                                "paddingBottom": 0,
                            },
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        font_path.write_text(
            json.dumps(
                {
                    "m_Name": "TestFont",
                    "mAtlas": {"m_FileID": 0, "m_PathID": 30},
                    "mUVRect": {"x": 0.125, "y": 0.4375, "width": 0.5, "height": 0.5},
                    "mFont": {
                        "mSize": 32,
                        "mBase": 0,
                        "mWidth": 64,
                        "mHeight": 64,
                        "mSpriteName": "TestFont",
                        "mSaved": {
                            "Array": [
                                {
                                    "index": 65,
                                    "x": 2,
                                    "y": 3,
                                    "width": 12,
                                    "height": 20,
                                    "offsetX": 0,
                                    "offsetY": 2,
                                    "advance": 13,
                                    "channel": 15,
                                    "kerning": {"Array": []},
                                }
                            ]
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        items = []
        for path_id, type_name, relative_path in (
            (10, "Texture2D", "bundle/shared.assets/Texture2D/MainAtlas_10.png"),
            (20, "Material", "bundle/shared.assets/Material/MainAtlas_20.json"),
            (30, "MonoBehaviour", "bundle/shared.assets/MonoBehaviour/MainAtlas_30.json"),
            (40, "MonoBehaviour", "bundle/shared.assets/MonoBehaviour/TestFont_40.json"),
        ):
            items.append(
                {
                    "manifest_path": str(manifest_dir / "manifest.json"),
                    "manifest_dir": str(manifest_dir),
                    "item": {
                        "PathId": path_id,
                        "TypeName": type_name,
                        "RelativePath": relative_path,
                        "BundleEntryName": "shared.assets",
                    },
                }
            )
        (records / "tmp_manifest_index.json").write_text(
            json.dumps(
                {
                    "schema_version": TMP_MANIFEST_INDEX_SCHEMA_VERSION,
                    "resource_input_root": str(input_root),
                    "manifest_count": 1,
                    "item_count": len(items),
                    "items": items,
                }
            ),
            encoding="utf-8",
        )
        asset_key = r"bin\Data\demo\bundle\shared.assets"
        (records / "file_id_map.json").write_text(
            json.dumps({asset_key: {"file_ids": {"0": asset_key}}}),
            encoding="utf-8",
        )
        (records / "bitmap_font_detection.json").write_text(
            json.dumps(
                {
                    "schema_version": 3,
                    "detections": [
                        {
                            "type": "ngui_bitmap_font",
                            "confidence": "confirmed",
                            "source_file": "bin/Data/demo/bundle/shared.assets/MonoBehaviour/TestFont_40.json",
                            "field_path": "mFont",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (records / "trans.json").write_text(json.dumps({"hello": "你好"}), encoding="utf-8")
        (records / "tmp_chars.txt").write_text("你好", encoding="utf-8")

        return SimpleNamespace(
            resource_input_root=input_root,
            stage_record_dir=records,
            output_file_id_map_json="file_id_map.json",
            output_trans_json="trans.json",
            ttf_template_path=Path(__file__).resolve().parents[1] / "templates" / "fzkt.ttf",
            ngui_generated_dir=generated,
            ngui_import_dir=import_dir,
            ngui_max_atlas_size=256,
            ngui_glyph_padding=2,
            ngui_bold_stroke_width=1,
        )

    def test_generates_l_shaped_ngui_atlas_and_prepares_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            original_texture = (
                cfg.resource_input_root
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets"
                / "Texture2D" / "MainAtlas_10.png"
            )
            original_pixels = Image.open(original_texture).convert("RGBA").tobytes()

            manifest = generate_ngui_fonts(cfg)

            self.assertEqual("generated", manifest["status"])
            generated_texture = cfg.ngui_generated_dir / original_texture.relative_to(cfg.resource_input_root)
            with Image.open(generated_texture) as generated_image:
                generated_rgba = generated_image.convert("RGBA")
                self.assertEqual((256, 256), generated_rgba.size)
                self.assertEqual(
                    original_pixels,
                    generated_rgba.crop((0, 0, 128, 128)).tobytes(),
                )

            generated_font_path = (
                cfg.ngui_generated_dir
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets"
                / "MonoBehaviour" / "TestFont_40.json"
            )
            generated_font = json.loads(generated_font_path.read_text(encoding="utf-8"))
            self.assertEqual(256, generated_font["mFont"]["mWidth"])
            self.assertEqual(256, generated_font["mFont"]["mHeight"])
            self.assertEqual({"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}, generated_font["mUVRect"])
            glyphs = {glyph["index"]: glyph for glyph in generated_font["mFont"]["mSaved"]["Array"]}
            self.assertNotEqual((18, 11), (glyphs[65]["x"], glyphs[65]["y"]))
            self.assertNotEqual(
                (12, 20, 0, 2, 13),
                tuple(glyphs[65][key] for key in ("width", "height", "offsetX", "offsetY", "advance")),
            )
            for char in "A你好":
                glyph = glyphs[ord(char)]
                self.assertTrue(glyph["x"] >= 128 or glyph["y"] >= 128)

            group = manifest["groups"][0]
            self.assertEqual("regenerate_all_source_and_translated_glyphs", manifest["generation_mode"])
            self.assertEqual(3, group["generated_glyph_bitmap_count"])
            self.assertEqual(1, group["fonts"][0]["source_glyph_count"])
            self.assertEqual(3, group["fonts"][0]["generated_glyph_count"])

            generated_atlas_path = generated_font_path.with_name("MainAtlas_30.json")
            generated_atlas = json.loads(generated_atlas_path.read_text(encoding="utf-8"))
            sprites = {sprite["name"]: sprite for sprite in generated_atlas["mSprites"]["Array"]}
            self.assertEqual((64, 0, 32, 32), tuple(sprites["Button"][key] for key in ("x", "y", "width", "height")))
            page_name = generated_font["mFont"]["mSpriteName"]
            self.assertEqual((0, 0, 256, 256), tuple(sprites[page_name][key] for key in ("x", "y", "width", "height")))

            counts = prepare_generated_ngui_import_replacements(cfg)
            self.assertEqual({"font": 1, "atlas": 1, "texture": 1}, counts)
            self.assertTrue((cfg.ngui_import_dir / original_texture.relative_to(cfg.resource_input_root)).is_file())
            self.assertTrue((cfg.ngui_generated_dir / GENERATION_MANIFEST_NAME).is_file())

    def test_ngui_uses_tmp_chars_not_earlier_game_chars(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            (cfg.stage_record_dir / "game_chars.txt").write_text("旧字符", encoding="utf-8")
            (cfg.stage_record_dir / "tmp_chars.txt").write_text("最终", encoding="utf-8")

            self.assertEqual(_translated_characters(cfg), ["最", "终"])

    def test_shared_atlas_fonts_reuse_one_generated_glyph_bitmap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            mono_dir = (
                cfg.resource_input_root
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets" / "MonoBehaviour"
            )
            first_font_path = mono_dir / "TestFont_40.json"
            second_font_path = mono_dir / "SecondFont_41.json"
            second_font = json.loads(first_font_path.read_text(encoding="utf-8"))
            second_font["m_Name"] = "SecondFont"
            second_font["mFont"]["mSpriteName"] = "SecondFont"
            second_font_path.write_text(json.dumps(second_font), encoding="utf-8")

            atlas_path = mono_dir / "MainAtlas_30.json"
            atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
            second_sprite = dict(atlas["mSprites"]["Array"][0])
            second_sprite.update({"name": "SecondFont", "x": 0, "y": 64})
            atlas["mSprites"]["Array"].append(second_sprite)
            atlas_path.write_text(json.dumps(atlas), encoding="utf-8")

            index_path = cfg.stage_record_dir / "tmp_manifest_index.json"
            index = json.loads(index_path.read_text(encoding="utf-8"))
            index["items"].append(
                {
                    "manifest_path": index["items"][0]["manifest_path"],
                    "manifest_dir": index["items"][0]["manifest_dir"],
                    "item": {
                        "PathId": 41,
                        "TypeName": "MonoBehaviour",
                        "RelativePath": "bundle/shared.assets/MonoBehaviour/SecondFont_41.json",
                        "BundleEntryName": "shared.assets",
                    },
                }
            )
            index["item_count"] = len(index["items"])
            index_path.write_text(json.dumps(index), encoding="utf-8")

            report_path = cfg.stage_record_dir / "bitmap_font_detection.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["detections"].append(
                {
                    "type": "ngui_bitmap_font",
                    "confidence": "confirmed",
                    "source_file": "bin/Data/demo/bundle/shared.assets/MonoBehaviour/SecondFont_41.json",
                    "field_path": "mFont",
                }
            )
            report_path.write_text(json.dumps(report), encoding="utf-8")

            manifest = generate_ngui_fonts(cfg)

            self.assertEqual(2, manifest["font_count"])
            self.assertEqual(3, manifest["groups"][0]["generated_glyph_bitmap_count"])
            generated_fonts = []
            for name in ("TestFont_40.json", "SecondFont_41.json"):
                path = cfg.ngui_generated_dir / first_font_path.relative_to(cfg.resource_input_root).with_name(name)
                generated_fonts.append(json.loads(path.read_text(encoding="utf-8")))
            for char in "A你好":
                positions = []
                for font in generated_fonts:
                    glyph = next(item for item in font["mFont"]["mSaved"]["Array"] if item["index"] == ord(char))
                    positions.append((glyph["x"], glyph["y"], glyph["width"], glyph["height"]))
                self.assertEqual(positions[0], positions[1])

    def test_trimmed_source_font_sprite_is_replaced_by_untrimmed_generated_page(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            atlas_path = (
                cfg.resource_input_root
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets"
                / "MonoBehaviour" / "MainAtlas_30.json"
            )
            atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
            source_sprite = atlas["mSprites"]["Array"][0]
            source_sprite.update(
                {
                    "width": 63,
                    "height": 63,
                    "paddingRight": 1,
                    "paddingTop": 1,
                }
            )
            atlas_path.write_text(json.dumps(atlas), encoding="utf-8")

            generate_ngui_fonts(cfg)

            generated_atlas = json.loads(
                (cfg.ngui_generated_dir / atlas_path.relative_to(cfg.resource_input_root)).read_text(
                    encoding="utf-8"
                )
            )
            generated_font_path = (
                cfg.ngui_generated_dir
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets"
                / "MonoBehaviour" / "TestFont_40.json"
            )
            generated_font = json.loads(generated_font_path.read_text(encoding="utf-8"))
            page_name = generated_font["mFont"]["mSpriteName"]
            sprites = {sprite["name"]: sprite for sprite in generated_atlas["mSprites"]["Array"]}

            self.assertEqual(1, sprites["TestFont"]["paddingRight"])
            self.assertEqual(1, sprites["TestFont"]["paddingTop"])
            generated_page = sprites[page_name]
            self.assertEqual((0, 0, 256, 256), tuple(
                generated_page[key] for key in ("x", "y", "width", "height")
            ))
            for field in (
                "paddingLeft", "paddingRight", "paddingTop", "paddingBottom",
                "borderLeft", "borderRight", "borderTop", "borderBottom",
            ):
                self.assertEqual(0, generated_page[field])

    def test_unsupported_source_glyph_is_reported_and_removed_like_sdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            font_path = (
                cfg.resource_input_root
                / "bin" / "Data" / "demo" / "bundle" / "shared.assets"
                / "MonoBehaviour" / "TestFont_40.json"
            )
            font = json.loads(font_path.read_text(encoding="utf-8"))
            unsupported = dict(font["mFont"]["mSaved"]["Array"][0])
            unsupported["index"] = 0x10FFFF
            font["mFont"]["mSaved"]["Array"].append(unsupported)
            font_path.write_text(json.dumps(font), encoding="utf-8")

            manifest = generate_ngui_fonts(cfg)

            self.assertEqual(1, manifest["removed_unsupported_source_character_count"])
            self.assertEqual([0x10FFFF], manifest["removed_unsupported_source_codepoints"])
            generated_font = json.loads(
                (cfg.ngui_generated_dir / font_path.relative_to(cfg.resource_input_root)).read_text(
                    encoding="utf-8"
                )
            )
            generated_codes = {item["index"] for item in generated_font["mFont"]["mSaved"]["Array"]}
            self.assertNotIn(0x10FFFF, generated_codes)
            self.assertTrue((cfg.stage_record_dir / REMOVED_UNSUPPORTED_CHARS_NAME).is_file())
            detail = (cfg.stage_record_dir / REMOVED_UNSUPPORTED_DETAILS_NAME).read_text(encoding="utf-8")
            self.assertIn("U+10FFFF", detail)
            self.assertIn("TestFont_40.json", detail)

    def test_unsupported_translated_glyph_still_stops_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._fixture(Path(temp_dir))
            (cfg.stage_record_dir / cfg.output_trans_json).write_text(
                json.dumps({"hello": "😀"}),
                encoding="utf-8",
            )
            (cfg.stage_record_dir / "tmp_chars.txt").write_text("😀", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "译文字符"):
                generate_ngui_fonts(cfg)


if __name__ == "__main__":
    unittest.main()
