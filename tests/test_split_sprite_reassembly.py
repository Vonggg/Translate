from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from PIL import Image

from support.image_restore import restore_split_sprites_to_import


class SplitSpriteReassemblyTests(unittest.TestCase):
    def test_only_edited_sprite_region_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input"
            edited_root = root / "edited"
            output_root = root / "ToImport"
            atlas_path = source_root / "bundle" / "Texture2D" / "atlas.png"
            atlas_path.parent.mkdir(parents=True)
            edited_root.mkdir()
            Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(atlas_path)
            Image.new("RGBA", (2, 1), (0, 255, 0, 255)).save(edited_root / "Changed_1.png")
            sprite_map = root / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": "Changed_1.png",
                                "texture_png": str(atlas_path),
                                "rect": {"x": 1, "y": 0, "width": 2, "height": 1},
                                "packing_rotation": 0,
                            },
                            {
                                "item_type": "sprite",
                                "flat_name": "Unchanged_2.png",
                                "texture_png": str(atlas_path),
                                "rect": {"x": 0, "y": 2, "width": 1, "height": 1},
                                "packing_rotation": 0,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = restore_split_sprites_to_import(
                edited_root, output_root, sprite_map, source_root
            )

            self.assertEqual(result, (1, 1, 0))
            with Image.open(output_root / atlas_path.relative_to(source_root)) as rebuilt:
                rebuilt = rebuilt.convert("RGBA")
                self.assertEqual(rebuilt.getpixel((1, 3)), (0, 255, 0, 255))
                self.assertEqual(rebuilt.getpixel((2, 3)), (0, 255, 0, 255))
                self.assertEqual(rebuilt.getpixel((0, 0)), (255, 0, 0, 255))

    def test_transparent_pixels_replace_original_and_rotation_is_reversed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input"
            edited_root = root / "edited"
            output_root = root / "ToImport"
            atlas_path = source_root / "atlas.png"
            source_root.mkdir()
            edited_root.mkdir()
            Image.new("RGBA", (3, 3), (255, 0, 0, 255)).save(atlas_path)
            changed = Image.new("RGBA", (1, 2), (0, 0, 0, 0))
            changed.putpixel((0, 1), (0, 0, 255, 255))
            changed.save(edited_root / "Rotated_3.png")
            sprite_map = root / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": "Rotated_3.png",
                                "texture_png": str(atlas_path),
                                "rect": {"x": 0, "y": 0, "width": 2, "height": 1},
                                "packing_rotation": 4,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                restore_split_sprites_to_import(
                    edited_root, output_root, sprite_map, source_root
                ),
                (1, 1, 0),
            )
            with Image.open(output_root / "atlas.png") as rebuilt:
                colors = [rebuilt.convert("RGBA").getpixel((x, 2)) for x in range(2)]
                self.assertIn((0, 0, 0, 0), colors)
                self.assertIn((0, 0, 255, 255), colors)

    def test_wrong_sprite_size_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input"
            edited_root = root / "edited"
            atlas_path = source_root / "atlas.png"
            source_root.mkdir()
            edited_root.mkdir()
            Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(atlas_path)
            Image.new("RGBA", (3, 3), (0, 255, 0, 255)).save(edited_root / "Wrong.png")
            sprite_map = root / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": "Wrong.png",
                                "texture_png": str(atlas_path),
                                "rect": {"x": 0, "y": 0, "width": 2, "height": 2},
                                "packing_rotation": 0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                result = restore_split_sprites_to_import(
                    edited_root, root / "ToImport", sprite_map, source_root
                )

            self.assertEqual(result, (0, 0, 1))
            self.assertIn(
                "\033[91m[图集回拼][跳过] Wrong.png 尺寸=3x3，应为=2x2\033[0m",
                output.getvalue(),
            )

    def test_ngui_sprite_uses_top_left_atlas_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input"
            edited_root = root / "edited"
            output_root = root / "ToImport"
            atlas_path = source_root / "atlas.png"
            source_root.mkdir()
            edited_root.mkdir()
            Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(atlas_path)
            Image.new("RGBA", (2, 1), (0, 255, 0, 255)).save(
                edited_root / "NGUI_10.png"
            )
            sprite_map = root / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "ngui_sprite",
                                "flat_name": "NGUI_10.png",
                                "texture_png": str(atlas_path),
                                "rect": {"x": 1, "y": 0, "width": 2, "height": 1},
                                "coordinate_origin": "top_left",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                restore_split_sprites_to_import(
                    edited_root, output_root, sprite_map, source_root
                ),
                (1, 1, 0),
            )
            with Image.open(output_root / "atlas.png") as rebuilt:
                rebuilt = rebuilt.convert("RGBA")
                self.assertEqual(rebuilt.getpixel((1, 0)), (0, 255, 0, 255))
                self.assertEqual(rebuilt.getpixel((2, 0)), (0, 255, 0, 255))
                self.assertEqual(rebuilt.getpixel((1, 3)), (255, 0, 0, 255))


if __name__ == "__main__":
    unittest.main()
