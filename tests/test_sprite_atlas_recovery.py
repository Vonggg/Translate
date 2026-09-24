import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.sprite_atlas_recovery import _read_atlases, recover_sprite_atlas
from test_object_hierarchy_preview import TOOLS


class SpriteAtlasRecoveryTests(unittest.TestCase):
    def test_exact_file_scope_and_source_change_invalidate_metadata(self):
        key = ({"data[0]": 1, "data[1]": 2, "data[2]": 3, "data[3]": 4}, 21300000)
        obj = SimpleNamespace(type=SimpleNamespace(name="SpriteAtlas"), path_id=149,
                              assets_file=SimpleNamespace(name="sharedassets1.assets"),
                              read_typetree=lambda: {"m_RenderDataMap": [(key, {"texture": {"m_PathID": 8}})]})
        with tempfile.TemporaryDirectory() as tmp, patch("UnityPy.load", return_value=SimpleNamespace(objects=[obj])) as load:
            source = Path(tmp) / "data.unity3d"
            source.write_bytes(b"one")
            _read_atlases.cache_clear()
            data = recover_sprite_atlas(source, "sharedassets1.assets", 149)
            self.assertEqual(data["m_RenderDataMap"][0]["first"], {"first": key[0], "second": key[1]})
            self.assertIsNone(recover_sprite_atlas(source, "other.assets", 149))
            self.assertEqual(load.call_count, 1)
            source.write_bytes(b"changed")
            recover_sprite_atlas(source, "sharedassets1.assets", 149)
            self.assertEqual(load.call_count, 2)
        _read_atlases.cache_clear()

    def test_missing_exported_atlas_resolves_packed_data(self):
        key = {"first": {"data[0]": 7}, "second": 21300000}
        packed = {"texture": {"m_FileID": 0, "m_PathID": 9}, "textureRect": {"x": 24}}
        scope = {"items": {}}
        sprite = {"m_SpriteAtlas": {"m_FileID": 0, "m_PathID": 149},
                  "m_RenderDataKey": key, "m_RD": {"texture": {"m_PathID": 0}}}
        with patch.object(TOOLS, "_recover_preview_sprite_atlas", return_value={"m_RenderDataMap": [{"first": key, "second": packed}]}):
            actual, actual_scope = TOOLS._sprite_render_data_with_scope(sprite, scope)
        self.assertEqual(actual, packed)
        self.assertIs(actual_scope, scope)

    def test_sprite_world_size_uses_pixels_per_unit_and_trimmed_pivot(self):
        scope = {"items": {("Sprite", 3): {"data": {
            "m_PixelsToUnits": 100, "m_Rect": {"width": 400, "height": 300},
            "m_Pivot": {"x": .25, "y": .5},
            "m_RD": {"textureRectOffset": {"x": 20, "y": 10}},
        }, "path": Path("unused")}}}
        component = {"m_Sprite": {"m_FileID": 0, "m_PathID": 3}}
        rect = TOOLS._preview_sprite_renderer_rect(scope, component, SimpleNamespace(size=(360, 280)), (0, 0, 0, 0), (50, 50))
        self.assertEqual(rect, (-40, -70, 180, 140))
