from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_tool_scripts():
    script_path = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tool_scripts_for_sprite_selection_test", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tool_scripts()


class SplitSpriteSelectionTests(unittest.TestCase):
    def test_missing_block_image_directory_requires_allpng_export(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            block_root = Path(temp_dir) / "AllPNG" / "屏蔽object"
            with (
                patch.object(TOOLS, "DEFAULT_BLOCK_IMAGE_ROOT", block_root),
                patch.object(TOOLS, "_selected_allpng_items", return_value=[]) as selector,
            ):
                TOOLS.run_block_objects_by_image()

            self.assertFalse(block_root.exists())
            selector.assert_not_called()

    def test_existing_block_image_directory_is_read_as_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            png_root = root / "PNG"
            block_root = root / "屏蔽object"
            png_root.mkdir()
            block_root.mkdir()
            (block_root / "Target.png").write_bytes(b"image contents are not read here")
            (block_root / "ignore.txt").write_text("ignore", encoding="utf-8")
            image_map = root / "_allpng_map.json"
            image_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "flat_name": "Target.png",
                                "original_relative_path": "bundle/Texture2D/Target.png",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(TOOLS, "DEFAULT_ALL_IMAGE_MAP", image_map),
                patch.object(TOOLS, "DEFAULT_ALL_IMAGE_PNG_ROOT", png_root),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_MAP", root / "missing-sprite-map.json"),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_PNG_ROOT", root / "missing-sprites"),
                patch.object(TOOLS, "DEFAULT_BLOCK_IMAGE_ROOT", block_root),
                patch.object(TOOLS, "prompt_input", return_value="2"),
            ):
                selected = TOOLS._selected_allpng_items()

            self.assertEqual([item["flat_name"] for item in selected], ["Target.png"])

    def test_cross_file_sprite_reference_matches_component_scope(self) -> None:
        sprite_key = ("manifest", "sharedassets1.assets")
        level_key = ("manifest", "level1")
        sprite_scope = {
            "scope_key": sprite_key,
            "source": "data.unity3d",
            "bundle_entry": "sharedassets1.assets",
            "items": {
                ("Sprite", 478): {
                    "data": {
                        "m_RD": {
                            "texture": {"m_FileID": 0, "m_PathID": 64}
                        }
                    }
                }
            },
        }
        level_scope = {
            "scope_key": level_key,
            "source": "data.unity3d",
            "bundle_entry": "level1",
            "items": {
                ("MonoBehaviour", 1116): {
                    "data": {
                        "m_Sprite": {"m_FileID": 2, "m_PathID": 478},
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 197},
                    }
                }
            },
        }
        sprite_scope["pointer_scopes"] = {0: sprite_scope}
        level_scope["pointer_scopes"] = {0: level_scope, 2: sprite_scope}
        scopes = {sprite_key: sprite_scope, level_key: level_scope}

        def all_entries(scopes_value, type_names, _path_ids):
            return [
                (scope_key, scope, type_name, path_id, entry)
                for scope_key, scope in scopes_value.items()
                for (type_name, path_id), entry in scope["items"].items()
                if type_name in type_names
            ]

        chain = [{"path_id": 197, "name": "NO ADS", "source_json": "go.json"}]
        with (
            patch.object(
                TOOLS,
                "_prefilter_reference_entries_across_scopes",
                side_effect=all_entries,
            ),
            patch.object(TOOLS, "_object_chain_direct", return_value=chain),
        ):
            matches = TOOLS._find_image_object_matches(
                scopes,
                texture_targets=set(),
                sprite_targets={(sprite_key, 478)},
            )

        self.assertEqual(len(matches), 1)
        self.assertIs(matches[0]["scope"], level_scope)
        self.assertEqual(matches[0]["component_path_id"], 1116)
        self.assertEqual(matches[0]["chain"], chain)

    def test_preview_loads_sprite_from_external_scope(self) -> None:
        sprite_scope = {"items": {}}
        component_data = {
            "m_Enabled": 1,
            "m_Sprite": {"m_FileID": 2, "m_PathID": 478},
        }
        level_scope = {
            "items": {("MonoBehaviour", 1116): {"data": component_data}},
        }
        level_scope["pointer_scopes"] = {0: level_scope, 2: sprite_scope}
        game_object_data = {
            "m_Component": {"Array": [{"component": {"m_FileID": 0, "m_PathID": 1116}}]}
        }
        image = object()
        with patch.object(TOOLS, "_preview_sprite_image", return_value=image) as loader:
            result = TOOLS._preview_component(level_scope, game_object_data)

        self.assertEqual(result, (component_data, image))
        loader.assert_called_once_with(sprite_scope, 478)

    def test_sprite_render_data_uses_packed_sprite_atlas_rect(self) -> None:
        render_key = {
            "first": {"data[0]": 1, "data[1]": 2, "data[2]": 3, "data[3]": 4},
            "second": 21300000,
        }
        packed_data = {
            "texture": {"m_FileID": 0, "m_PathID": 64},
            "textureRect": {"x": 10, "y": 20, "width": 30, "height": 40},
        }
        scope = {
            "items": {
                ("SpriteAtlas", 99): {
                    "data": {
                        "m_RenderDataMap": {
                            "Array": [{"first": render_key, "second": packed_data}]
                        }
                    }
                }
            }
        }
        sprite_data = {
            "m_RenderDataKey": render_key,
            "m_SpriteAtlas": {"m_FileID": 0, "m_PathID": 99},
            "m_RD": {"texture": {"m_FileID": 0, "m_PathID": 12}},
        }

        self.assertIs(TOOLS._sprite_render_data(sprite_data, scope), packed_data)
        self.assertEqual(TOOLS._sprite_texture_path_id(sprite_data, scope), 64)

    def test_preview_sprite_resolves_external_render_texture_scope(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            texture_path = Path(temp_dir) / "atlas.png"
            Image.new("RGBA", (8, 8), (20, 40, 60, 255)).save(texture_path)
            texture_scope = {
                "items": {
                    ("Texture2D", 64): {
                        "data": {},
                        "path": texture_path,
                    }
                }
            }
            sprite_scope = {
                "items": {
                    ("Sprite", 7): {
                        "data": {
                            "m_RD": {
                                "texture": {"m_FileID": 3, "m_PathID": 64},
                                "textureRect": {
                                    "x": 1, "y": 2, "width": 4, "height": 3,
                                },
                            }
                        }
                    }
                }
            }
            sprite_scope["pointer_scopes"] = {0: sprite_scope, 3: texture_scope}

            preview = TOOLS._preview_sprite_image(sprite_scope, 7)

            self.assertIsNotNone(preview)
            self.assertEqual(preview.size, (4, 3))

    def test_sprite_render_data_resolves_external_sprite_atlas_scope(self) -> None:
        render_key = {"first": {"data[0]": 9}, "second": 21300000}
        packed_data = {
            "texture": {"m_FileID": 0, "m_PathID": 64},
            "textureRect": {"x": 1, "y": 2, "width": 3, "height": 4},
        }
        atlas_scope = {
            "items": {
                ("SpriteAtlas", 99): {
                    "data": {"m_RenderDataMap": {"Array": [
                        {"first": render_key, "second": packed_data}
                    ]}}
                }
            }
        }
        sprite_scope = {"items": {}}
        sprite_scope["pointer_scopes"] = {0: sprite_scope, 2: atlas_scope}
        sprite_data = {
            "m_RenderDataKey": render_key,
            "m_SpriteAtlas": {"m_FileID": 2, "m_PathID": 99},
        }

        data, owner_scope = TOOLS._sprite_render_data_with_scope(
            sprite_data, sprite_scope
        )

        self.assertIs(data, packed_data)
        self.assertIs(owner_scope, atlas_scope)

    def test_split_sprite_can_be_selected_without_regular_allpng_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sprite_root = root / "Sprite"
            sprite_png_root = sprite_root / "PNG"
            sprite_png_root.mkdir(parents=True)
            sprite_map = sprite_root / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "sprite",
                                "flat_name": "GiftIcon_123.png",
                                "sprite_name": "GiftIcon",
                                "sprite_path_id": 123,
                                "texture_path_id": 456,
                                "source_resource": "aa/example.bundle",
                                "bundle_entry": "CAB-example",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(TOOLS, "DEFAULT_ALL_IMAGE_MAP", root / "missing.json"),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_ROOT", sprite_root),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_PNG_ROOT", sprite_png_root),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_MAP", sprite_map),
                patch.object(TOOLS, "DEFAULT_BLOCK_IMAGE_ROOT", root / "BlockImages"),
                patch.object(
                    TOOLS,
                    "prompt_input",
                    side_effect=["1", str(sprite_png_root / "GiftIcon_123.png")],
                ),
            ):
                selected = TOOLS._selected_allpng_items()

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["_selection_kind"], "sprite")
            self.assertEqual(selected[0]["sprite_path_id"], 123)

    def test_ngui_split_sprite_can_be_selected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            sprite_png_root = root / "Sprite" / "PNG"
            sprite_png_root.mkdir(parents=True)
            sprite_map = root / "Sprite" / "_allsprite_map.json"
            sprite_map.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "item_type": "ngui_sprite",
                                "flat_name": "Button_NGUI_10.png",
                                "sprite_name": "Button",
                                "atlas_path_id": 10,
                                "source_resource": "data.unity3d",
                                "bundle_entry": "sharedassets0.assets",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(TOOLS, "DEFAULT_ALL_IMAGE_MAP", root / "missing.json"),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_PNG_ROOT", sprite_png_root),
                patch.object(TOOLS, "DEFAULT_ALL_SPRITE_MAP", sprite_map),
                patch.object(TOOLS, "DEFAULT_BLOCK_IMAGE_ROOT", root / "BlockImages"),
                patch.object(
                    TOOLS,
                    "prompt_input",
                    side_effect=["1", "Button_NGUI_10.png"],
                ),
            ):
                selected = TOOLS._selected_allpng_items()

            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["_selection_kind"], "ngui_sprite")

    def test_ngui_atlas_and_sprite_name_match_uisprite(self) -> None:
        atlas_key = ("manifest", "sharedassets0.assets")
        level_key = ("manifest", "level1")
        atlas_scope = {
            "scope_key": atlas_key,
            "source": "data.unity3d",
            "bundle_entry": "sharedassets0.assets",
            "items": {},
        }
        level_scope = {
            "scope_key": level_key,
            "source": "data.unity3d",
            "bundle_entry": "level1",
            "items": {
                ("MonoBehaviour", 1116): {
                    "data": {
                        "mAtlas": {"m_FileID": 2, "m_PathID": 484},
                        "mSpriteName": "settings_main_icon",
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 197},
                    }
                }
            },
        }
        atlas_scope["pointer_scopes"] = {0: atlas_scope}
        level_scope["pointer_scopes"] = {0: level_scope, 2: atlas_scope}
        scopes = {atlas_key: atlas_scope, level_key: level_scope}

        def all_entries(scopes_value, type_names, _path_ids):
            return [
                (scope_key, scope, type_name, path_id, entry)
                for scope_key, scope in scopes_value.items()
                for (type_name, path_id), entry in scope["items"].items()
                if type_name in type_names
            ]

        chain = [{"path_id": 197, "name": "Settings", "source_json": "go.json"}]
        with (
            patch.object(
                TOOLS,
                "_prefilter_reference_entries_across_scopes",
                side_effect=all_entries,
            ),
            patch.object(TOOLS, "_object_chain_direct", return_value=chain),
        ):
            matches = TOOLS._find_image_object_matches(
                scopes,
                texture_targets=set(),
                sprite_targets=set(),
                ngui_sprite_targets={
                    (atlas_key, 484, "settings_main_icon")
                },
            )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["component_type"], "NGUI UISprite")
        self.assertEqual(matches[0]["ngui_atlas_path_id"], 484)
        self.assertEqual(matches[0]["ngui_sprite_name"], "settings_main_icon")

    def test_cross_file_texture_reference_matches_ngui_uitexture(self) -> None:
        texture_key = ("manifest", "texture.assets")
        level_key = ("manifest", "level1")
        texture_scope = {
            "scope_key": texture_key,
            "source": "texture.assets",
            "bundle_entry": "",
            "items": {("Texture2D", 1): {"data": {"m_Name": "vk"}}},
        }
        level_scope = {
            "scope_key": level_key,
            "source": "level1",
            "bundle_entry": "",
            "items": {
                ("MonoBehaviour", 366): {
                    "data": {
                        "mTexture": {"m_FileID": 3, "m_PathID": 1},
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 8},
                    }
                }
            },
        }
        texture_scope["pointer_scopes"] = {0: texture_scope}
        level_scope["pointer_scopes"] = {0: level_scope, 3: texture_scope}
        scopes = {texture_key: texture_scope, level_key: level_scope}

        def all_entries(scopes_value, type_names, _path_ids):
            return [
                (scope_key, scope, type_name, path_id, entry)
                for scope_key, scope in scopes_value.items()
                for (type_name, path_id), entry in scope["items"].items()
                if type_name in type_names
            ]

        chain = [{"path_id": 8, "name": "ButtonVkontakte", "source_json": "go.json"}]
        with (
            patch.object(
                TOOLS,
                "_prefilter_reference_entries_across_scopes",
                side_effect=all_entries,
            ),
            patch.object(TOOLS, "_object_chain_direct", return_value=chain),
        ):
            matches = TOOLS._find_image_object_matches(
                scopes,
                texture_targets={(texture_key, 1)},
                sprite_targets=set(),
            )

        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["component_type"], "NGUI UITexture")
        self.assertEqual(matches[0]["texture_path_id"], 1)
        self.assertEqual(matches[0]["chain"], chain)

    def test_preview_loads_ngui_uitexture_from_external_scope(self) -> None:
        texture_scope = {"items": {("Texture2D", 1): {"data": {}}}}
        component_data = {
            "m_Enabled": 1,
            "mTexture": {"m_FileID": 3, "m_PathID": 1},
        }
        level_scope = {
            "items": {("MonoBehaviour", 366): {"data": component_data}},
        }
        level_scope["pointer_scopes"] = {0: level_scope, 3: texture_scope}
        game_object_data = {
            "m_Component": {"Array": [{"component": {"m_FileID": 0, "m_PathID": 366}}]}
        }
        image = object()
        with patch.object(TOOLS, "_preview_texture_image", return_value=image) as loader:
            result = TOOLS._preview_component(level_scope, game_object_data)

        self.assertEqual(result, (component_data, image))
        loader.assert_called_once_with(texture_scope, 1)


if __name__ == "__main__":
    unittest.main()
