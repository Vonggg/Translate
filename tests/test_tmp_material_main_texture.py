import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pipeline.tmp_pipeline import prepare_generated_tmp_import_replacements
from support.config import load_config


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class TmpMaterialMainTextureTests(unittest.TestCase):
    def test_replaces_material_main_texture_when_it_differs_from_font_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            asset_root = input_root / "bundle" / "CAB-test"
            record_dir = root / "records"
            result_dir = root / "result"
            overlay_dir = root / "overlay"

            font_path = asset_root / "MonoBehaviour" / "NpcFont_100.json"
            font_atlas_path = asset_root / "Texture2D" / "NpcFontAtlas_200.png"
            material_path = asset_root / "Material" / "NpcFontMaterial_300.json"
            material_atlas_path = asset_root / "Texture2D" / "NpcMaterialAtlas_400.png"
            generated_json_path = root / "generated.json"
            generated_atlas_path = root / "generated.png"

            for path in (font_path, font_atlas_path, material_path, material_atlas_path):
                path.parent.mkdir(parents=True, exist_ok=True)

            font = {
                "m_Name": "NpcFont",
                "m_FaceInfo": {"m_FamilyName": "Npc Font"},
                "m_GlyphTable": {"Array": []},
                "m_CharacterTable": {"Array": []},
                "m_AtlasTextures": {"Array": [{"m_FileID": 0, "m_PathID": -200}]},
                "m_Material": {"m_FileID": 0, "m_PathID": -300},
            }
            generated_font = {
                "m_FaceInfo": {"m_FamilyName": "Chinese Font"},
                "m_GlyphTable": {"Array": [{"m_Index": 1}]},
                "m_CharacterTable": {"Array": [{"m_Unicode": 20013, "m_GlyphIndex": 1}]},
                "m_AtlasTextures": {"Array": [{"m_FileID": 0, "m_PathID": 999}]},
            }
            material = {
                "m_Name": "NpcFontMaterial",
                "m_SavedProperties": {
                    "m_TexEnvs": {
                        "Array": [
                            {
                                "first": "_MainTex",
                                "second": {
                                    "m_Texture": {"m_FileID": 0, "m_PathID": -400},
                                    "m_Scale": {"x": 1.0, "y": 1.0},
                                    "m_Offset": {"x": 0.0, "y": 0.0},
                                },
                            }
                        ]
                    },
                    "m_Floats": {"Array": []},
                },
            }

            font_path.write_text(json.dumps(font), encoding="utf-8")
            material_path.write_text(json.dumps(material), encoding="utf-8")
            generated_json_path.write_text(json.dumps(generated_font), encoding="utf-8")
            font_atlas_path.write_bytes(PNG_1X1)
            material_atlas_path.write_bytes(PNG_1X1)
            generated_atlas_path.write_bytes(PNG_1X1 + b"generated")

            items = [
                {
                    "PathId": 100,
                    "TypeName": "MonoBehaviour",
                    "RelativePath": "bundle/CAB-test/MonoBehaviour/NpcFont_100.json",
                    "BundleEntryName": "CAB-test",
                },
                {
                    "PathId": -200,
                    "TypeName": "Texture2D",
                    "RelativePath": "bundle/CAB-test/Texture2D/NpcFontAtlas_200.png",
                    "BundleEntryName": "CAB-test",
                },
                {
                    "PathId": -300,
                    "TypeName": "Material",
                    "RelativePath": "bundle/CAB-test/Material/NpcFontMaterial_300.json",
                    "BundleEntryName": "CAB-test",
                },
                {
                    "PathId": -400,
                    "TypeName": "Texture2D",
                    "RelativePath": "bundle/CAB-test/Texture2D/NpcMaterialAtlas_400.png",
                    "BundleEntryName": "CAB-test",
                },
            ]
            input_root.mkdir(parents=True, exist_ok=True)
            (input_root / "manifest.json").write_text(
                json.dumps({"Items": items}),
                encoding="utf-8",
            )

            cfg = replace(
                load_config(quiet=True),
                root_dir=root,
                resource_input_root=input_root,
                record_dir=record_dir,
                result_dir=result_dir,
                import_overlay_dir=overlay_dir,
                unity_font_project=root / "unity",
            )

            with mock.patch(
                "pipeline.translation.collect_translated_text_effect_material_sources",
                return_value={},
            ):
                summary = prepare_generated_tmp_import_replacements(
                    cfg,
                    generated_json_path,
                    generated_atlas_path,
                )

            expected = generated_atlas_path.read_bytes()
            self.assertEqual(summary["font_replacements"], 1)
            self.assertEqual(summary["texture_replacements"], 2)
            self.assertEqual(summary["material_main_texture_replacements"], 1)
            self.assertEqual(
                (overlay_dir / font_atlas_path.relative_to(input_root)).read_bytes(),
                expected,
            )
            self.assertEqual(
                (overlay_dir / material_atlas_path.relative_to(input_root)).read_bytes(),
                expected,
            )


if __name__ == "__main__":
    unittest.main()
