from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import resource_menu


class ImageImportAutoRestoreTests(unittest.TestCase):
    def test_same_flat_name_full_texture_wins_over_split_sprite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "workspace" / "input"
            allpng_root = root / "workspace" / "AllPNG"
            edited_root = allpng_root / "修改后的图片目录"
            relative_texture = Path("bundle") / "Texture2D" / "Logo_2.png"
            source_texture = input_root / relative_texture
            source_texture.parent.mkdir(parents=True)
            edited_root.mkdir(parents=True)
            Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(source_texture)
            replacement = edited_root / "Logo_2.png"
            Image.new("RGBA", (4, 4), (0, 0, 0, 0)).save(replacement)
            (allpng_root / "_allpng_map.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "flat_name": replacement.name,
                                "original_relative_path": str(relative_texture),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sprite_root = allpng_root / "Sprite"
            sprite_root.mkdir()
            (sprite_root / "_allsprite_map.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": replacement.name,
                                "texture_png": str(source_texture),
                                "rect": {"x": 1, "y": 1, "width": 2, "height": 2},
                                "packing_rotation": 0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            to_import_root = root / "workspace" / "output" / "Image" / "ToImport"
            not_imported: list[Path] = []

            restored, patched, rebuilt, invalid = resource_menu.restore_edited_images_before_import(
                root / "workspace", input_root, to_import_root, not_imported
            )

            self.assertEqual((restored, patched, rebuilt, invalid), (1, 0, 0, 0))
            self.assertEqual(not_imported, [])
            with Image.open(to_import_root / relative_texture) as output:
                self.assertEqual(output.size, (4, 4))
                self.assertEqual(output.convert("RGBA").getbbox(), None)

    def test_building_image_overlay_restores_flat_edits_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "workspace" / "input"
            to_import_root = root / "workspace" / "output" / "Image" / "ToImport"
            allpng_root = root / "workspace" / "AllPNG"
            edited_root = allpng_root / "修改后的图片目录"
            edited_root.mkdir(parents=True)
            input_root.mkdir(parents=True)

            edited_image = edited_root / "button_1.png"
            Image.new("RGBA", (2, 2), (12, 34, 56, 255)).save(edited_image)
            relative_target = Path("bundle") / "Texture2D" / "button.png"
            (allpng_root / "_allpng_map.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "flat_name": edited_image.name,
                                "original_relative_path": str(relative_target),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cfg = SimpleNamespace(
                root_dir=root,
                resource_input_root=input_root,
                image_import_dir=to_import_root,
            )
            overlay_root = resource_menu.build_import_overlay(cfg, {"image"})

            self.assertIsNotNone(overlay_root)
            self.assertTrue((to_import_root / relative_target).is_file())
            self.assertTrue((overlay_root / relative_target).is_file())
            with Image.open(overlay_root / relative_target) as restored:
                self.assertEqual(restored.convert("RGBA").getpixel((0, 0)), (12, 34, 56, 255))

    def test_missing_map_stops_unknown_edited_image_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            edited_root = root / "workspace" / "AllPNG" / "修改后的图片目录"
            edited_root.mkdir(parents=True)
            Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(edited_root / "unknown.png")

            cfg = SimpleNamespace(
                root_dir=root,
                resource_input_root=root / "workspace" / "input",
                image_import_dir=root / "workspace" / "output" / "Image" / "ToImport",
            )

            self.assertIsNone(resource_menu.build_import_overlay(cfg, {"image"}))

    def test_failed_sprite_is_reported_at_import_end(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "workspace" / "input"
            allpng_root = root / "workspace" / "AllPNG"
            edited_root = allpng_root / "修改后的图片目录"
            texture_path = input_root / "bundle" / "Texture2D" / "atlas.png"
            texture_path.parent.mkdir(parents=True)
            edited_root.mkdir(parents=True)
            Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(texture_path)
            failed_image = edited_root / "Wrong_1.png"
            Image.new("RGBA", (3, 3), (0, 255, 0, 255)).save(failed_image)
            (allpng_root / "_allpng_map.json").write_text(
                json.dumps({"items": []}),
                encoding="utf-8",
            )
            sprite_root = allpng_root / "Sprite"
            sprite_root.mkdir()
            (sprite_root / "_allsprite_map.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": failed_image.name,
                                "texture_png": str(texture_path),
                                "rect": {"x": 0, "y": 0, "width": 2, "height": 2},
                                "packing_rotation": 0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                root_dir=root,
                resource_input_root=input_root,
                image_import_dir=root / "workspace" / "output" / "Image" / "ToImport",
            )
            not_imported: list[Path] = []

            resource_menu.build_import_overlay(cfg, {"image"}, not_imported)

            self.assertEqual(not_imported, [failed_image])
            output = io.StringIO()
            with redirect_stdout(output):
                resource_menu.print_not_imported_images(not_imported)
            self.assertIn("\033[91m[图片导入][未导入图片] 共 1 张:\033[0m", output.getvalue())
            self.assertIn(str(failed_image), output.getvalue())

    def test_empty_not_imported_report_is_explicit(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            resource_menu.print_not_imported_images([])
        self.assertIn("\033[92m[图片导入][未导入图片] 无。\033[0m", output.getvalue())


if __name__ == "__main__":
    unittest.main()
