from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

import resource_menu


class ImageImportAutoRestoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
