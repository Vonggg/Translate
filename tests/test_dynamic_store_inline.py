from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_tools():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location(
        "tools_for_dynamic_store_inline_test", script
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tools()


def _entry(path: Path, data: dict) -> dict:
    return {"item": {}, "path": path, "data": data}


def _scope(source: Path, data: dict, extra_items: dict | None = None) -> dict:
    scope = {
        "scope_key": ("manifest", "sharedassets.assets"),
        "source": str(source.parent),
        "bundle_entry": "sharedassets.assets",
        "items": {
            ("MonoBehaviour", 100): _entry(source, data),
        },
    }
    if extra_items:
        scope["items"].update(extra_items)
    scope["pointer_scopes"] = {0: scope}
    return scope


def _inline_row(name: str, sprite_path_id: int, price: int) -> dict:
    return {
        "m_displayIcon": {"m_FileID": 0, "m_PathID": sprite_path_id},
        "m_itemName": name,
        "m_itemPrice": price,
        "m_wingsData": {"m_FileID": 0, "m_PathID": 0},
    }


class DynamicStoreInlineTests(unittest.TestCase):
    def test_trade_template_ids_resolve_named_sprites_and_both_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "trade" / "MonoBehaviour" / "NpcTradeProfile_1.json"
            source.parent.mkdir(parents=True)
            data = {
                "m_Name": "NpcTradeProfile",
                "SellItems": {
                    "Array": [
                        {
                            "ItemTemplateId": "stone_hatchet",
                            "BasePrice": 20,
                            "MaxStock": 1,
                        },
                        {
                            "ItemTemplateId": "stone_pickaxe",
                            "BasePrice": 20,
                            "MaxStock": 1,
                        },
                    ]
                },
                "BuyItems": {
                    "Array": [
                        {
                            "ItemTemplateId": "metal_hatchet",
                            "BasePrice": 40,
                            "MaxStock": 1,
                        }
                    ]
                },
            }
            source.write_text(json.dumps(data), encoding="utf-8")
            trade_scope = _scope(source, data)
            scopes = {trade_scope["scope_key"]: trade_scope}
            for index, sprite_name in enumerate(
                ("Item_Stone_Hachet", "Item_StonePickAxe", "Item_Metal_Hatchet"),
                start=1,
            ):
                sprite_scope = {
                    "scope_key": (f"manifest-{index}", ""),
                    "source": f"sprite-{index}",
                    "bundle_entry": "",
                    "items": {
                        ("Sprite", 1): _entry(
                            root / f"sprite-{index}" / "Sprite" / f"{sprite_name}_1.json",
                            {"m_Name": sprite_name},
                        )
                    },
                }
                sprite_scope["pointer_scopes"] = {0: sprite_scope}
                scopes[sprite_scope["scope_key"]] = sprite_scope

            configs = TOOLS._find_store_product_configs(scopes)

            by_array = {config["array_label"]: config for config in configs}
            self.assertEqual(set(by_array), {"SellItems.Array", "BuyItems.Array"})
            self.assertEqual(
                [product["name"] for product in by_array["SellItems.Array"]["products"]],
                ["stone_hatchet", "stone_pickaxe"],
            )
            self.assertEqual(
                [
                    product["image_name"]
                    for product in by_array["SellItems.Array"]["products"]
                ],
                ["Item_Stone_Hachet", "Item_StonePickAxe"],
            )
            self.assertEqual(
                by_array["BuyItems.Array"]["products"][0]["image_name"],
                "Item_Metal_Hatchet",
            )
            self.assertTrue(
                all(
                    product["image_kind"] == "sprite"
                    for config in configs
                    for product in config["products"]
                )
            )

    def test_detects_inline_struct_array_and_resolves_names_and_sprites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "MonoBehaviour" / "Wing_Shop_Data_100.json"
            source.parent.mkdir(parents=True)
            rows = [
                _inline_row("Blue Wings", 301, 100),
                _inline_row("Red Wings", 302, 200),
            ]
            data = {
                "m_Name": "Wing Shop Data",
                "m_wingShopItemData": {"Array": rows},
            }
            source.write_text(json.dumps(data), encoding="utf-8")
            scope = _scope(
                source,
                data,
                {
                    ("Sprite", 301): _entry(
                        source.parent.parent / "Sprite" / "Blue_301.json",
                        {"m_Name": "Blue Wing Icon"},
                    ),
                    ("Sprite", 302): _entry(
                        source.parent.parent / "Sprite" / "Red_302.json",
                        {"m_Name": "Red Wing Icon"},
                    ),
                },
            )

            configs = TOOLS._find_store_product_configs(
                {scope["scope_key"]: scope}
            )

            self.assertEqual(len(configs), 1)
            config = configs[0]
            self.assertEqual(config["array_path"], ["m_wingShopItemData", "Array"])
            self.assertEqual(
                [product["name"] for product in config["products"]],
                ["Blue Wings", "Red Wings"],
            )
            self.assertEqual(
                [product["sprite_path_id"] for product in config["products"]],
                [301, 302],
            )
            self.assertTrue(
                all(product["entry_kind"] == "inline" for product in config["products"])
            )
            self.assertNotEqual(
                config["products"][0]["pointer_key"],
                config["products"][1]["pointer_key"],
            )

    def test_blocks_only_selected_inline_index_and_restores_original_array(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "input"
            output_root = base / "output"
            record_path = base / "records" / "blocked_store_products.json"
            source = source_root / "sharedassets" / "MonoBehaviour" / "Shop_100.json"
            source.parent.mkdir(parents=True)
            rows = [
                _inline_row("First", 301, 100),
                _inline_row("Second", 302, 200),
                _inline_row("Third", 303, 300),
            ]
            original = {
                "m_Name": "Wing Shop Data",
                "keep_me": {"unchanged": True},
                "m_wingShopItemData": {"Array": rows},
            }
            source.write_text(json.dumps(original), encoding="utf-8")
            scope = _scope(
                source,
                original,
                {
                    ("Sprite", path_id): _entry(
                        source_root / "sharedassets" / "Sprite" / f"Icon_{path_id}.json",
                        {"m_Name": f"Icon {path_id}"},
                    )
                    for path_id in (301, 302, 303)
                },
            )
            config = TOOLS._find_store_product_configs(
                {scope["scope_key"]: scope}
            )[0]
            selected = config["products"][1]

            with (
                patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root),
                patch.object(TOOLS, "DEFAULT_OBJECT_TO_IMPORT_ROOT", output_root),
                patch.object(TOOLS, "DEFAULT_STORE_PRODUCT_BLOCK_RECORD", record_path),
            ):
                self.assertTrue(
                    TOOLS._set_store_product_blocked(config, selected, True)
                )
                target = output_root / source.relative_to(source_root)
                patched_data = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(patched_data["keep_me"], {"unchanged": True})
                self.assertEqual(
                    [
                        row["m_itemName"]
                        for row in patched_data["m_wingShopItemData"]["Array"]
                    ],
                    ["First", "Third"],
                )
                records = TOOLS._load_store_product_records()["items"]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]["original_index"], 1)
                self.assertEqual(records[0]["entry_kind"], "inline")

                self.assertTrue(
                    TOOLS._set_store_product_blocked(config, selected, False)
                )
                self.assertFalse(target.exists())
                self.assertEqual(TOOLS._load_store_product_records()["items"], [])

    def test_duplicate_pptr_removes_only_selected_occurrence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "input"
            output_root = base / "output"
            record_path = base / "records" / "blocked_store_products.json"
            source = source_root / "store" / "MonoBehaviour" / "Offers_100.json"
            source.parent.mkdir(parents=True)
            pointers = [
                {"m_FileID": 0, "m_PathID": 200},
                {"m_FileID": 0, "m_PathID": 200},
                {"m_FileID": 0, "m_PathID": 201},
            ]
            original = {
                "m_Name": "Store Offers",
                "_products": {"Array": pointers},
            }
            source.write_text(json.dumps(original), encoding="utf-8")
            scope = _scope(
                source,
                original,
                {
                    ("MonoBehaviour", 200): _entry(
                        source.parent / "Offer_200.json",
                        {"m_Name": "Repeated", "productId": "repeat", "price": 10},
                    ),
                    ("MonoBehaviour", 201): _entry(
                        source.parent / "Offer_201.json",
                        {"m_Name": "Last", "productId": "last", "price": 20},
                    ),
                },
            )
            config = TOOLS._find_store_product_configs(
                {scope["scope_key"]: scope}
            )[0]
            first, selected = config["products"][:2]
            self.assertEqual(first["legacy_pointer_key"], selected["legacy_pointer_key"])
            self.assertNotEqual(first["pointer_key"], selected["pointer_key"])

            with (
                patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root),
                patch.object(TOOLS, "DEFAULT_OBJECT_TO_IMPORT_ROOT", output_root),
                patch.object(TOOLS, "DEFAULT_STORE_PRODUCT_BLOCK_RECORD", record_path),
            ):
                self.assertTrue(
                    TOOLS._set_store_product_blocked(config, selected, True)
                )
                target = output_root / source.relative_to(source_root)
                patched_data = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(
                    [row["m_PathID"] for row in patched_data["_products"]["Array"]],
                    [200, 201],
                )
                self.assertTrue(
                    TOOLS._set_store_product_blocked(config, selected, False)
                )
                self.assertFalse(target.exists())

    def test_grid_render_expands_canvas_to_include_last_slot(self) -> None:
        from PIL import Image, ImageFont

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            preview = root / "preview.png"
            Image.new("RGBA", (100, 80), (28, 31, 38, 255)).save(preview)
            preview.with_suffix(".regions.json").write_text(
                json.dumps(
                    {
                        "tree_nodes": [
                            {
                                "path_id": 2,
                                "x": 10,
                                "y": 10,
                                "width": 80,
                                "height": 40,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            scope = {
                "scope_key": ("manifest", "level1"),
                "source": "level1",
                "bundle_entry": "level1",
                "items": {},
            }
            products = [
                {"pointer_key": f"inline:{index}:fake", "name": f"Item {index}"}
                for index in range(8)
            ]
            config = {"products": products, "config_key": "grid-test"}
            layout = {
                "scope": scope,
                "root_path_id": 1,
                "root_name": "ShopPopup",
                "root_source_json": "",
                "force_active_path_ids": [],
                "content_path_id": 2,
                "mode": "grid",
                "grid": {
                    "cell_size": (30.0, 30.0),
                    "spacing": (0.0, 0.0),
                    "padding": {
                        "m_Left": 0,
                        "m_Right": 0,
                        "m_Top": 0,
                        "m_Bottom": 0,
                    },
                    "child_alignment": 0,
                    "start_corner": 0,
                    "start_axis": 0,
                    "constraint": 1,
                    "constraint_count": 2,
                },
                "reference_size": (80.0, 40.0),
                "template_scope": None,
                "template_path_id": 0,
            }

            with (
                patch.object(
                    TOOLS,
                    "_create_object_hierarchy_preview",
                    return_value=preview,
                ),
                patch.object(TOOLS, "_store_product_preview_image", return_value=None),
                patch.object(TOOLS, "_store_blocked_keys", return_value=set()),
                patch.object(TOOLS, "_preview_font", return_value=ImageFont.load_default()),
            ):
                image, slots = TOOLS._render_store_layout(config, layout, {scope["scope_key"]: scope})

            try:
                required_width = math.ceil(
                    max(x + width for x, _y, width, _height in slots) + 24
                )
                required_height = math.ceil(
                    max(y + height for _x, y, _width, height in slots) + 24
                )
                self.assertEqual(len(slots), len(products))
                self.assertGreater(required_height, 80)
                self.assertGreaterEqual(image.width, required_width)
                self.assertGreaterEqual(image.height, required_height)
            finally:
                image.close()

    def test_equal_scene_slot_array_reuses_instantiated_positions(self) -> None:
        root = Path("input/level2")
        scope = {
            "scope_key": ("manifest", "level2"),
            "source": "bundle",
            "bundle_entry": "level2",
            "items": {
                ("GameObject", 10): _entry(
                    root / "GameObject/LuckySpinPopup_10.json",
                    {"m_Name": "LuckySpinPopup"},
                ),
                ("GameObject", 11): _entry(
                    root / "GameObject/RewardSlotA_11.json",
                    {"m_Name": "RewardSlotA"},
                ),
                ("GameObject", 12): _entry(
                    root / "GameObject/RewardSlotB_12.json",
                    {"m_Name": "RewardSlotB"},
                ),
            },
        }
        scope["pointer_scopes"] = {0: scope}
        config = {
            "array_path": ["m_rewards", "Array"],
            "products": [{"name": "A"}, {"name": "B"}],
        }
        data = {
            "m_rewards": {
                "Array": [
                    {"m_FileID": 0, "m_PathID": 11},
                    {"m_FileID": 0, "m_PathID": 12},
                ]
            }
        }
        chain = [{"path_id": 10, "name": "LuckySpinPopup"}]

        with patch.object(
            TOOLS, "_game_object_subtree_path_ids", return_value=[10, 11, 12]
        ):
            layout = TOOLS._component_instantiated_item_layout(
                config, scope, 99, 10, data, chain, 0, chain[0], 105
            )

        self.assertIsNotNone(layout)
        self.assertEqual(layout["mode"], "instantiated")
        self.assertEqual(layout["item_path_ids"], [11, 12])
        self.assertEqual(layout["reference_field"], "m_rewards.Array")

    def test_reward_info_type_resolves_lowercase_sprite_pointer(self) -> None:
        root = Path("input/sharedassets2")
        scope = {
            "scope_key": ("manifest", "sharedassets2"),
            "source": "bundle",
            "bundle_entry": "sharedassets2",
            "items": {
                ("MonoBehaviour", 200): _entry(
                    root / "MonoBehaviour/COIN_TYPE_200.json",
                    {
                        "m_Name": "COIN_TYPE",
                        "m_sprite": {"m_FileID": 0, "m_PathID": 300},
                    },
                ),
                ("Sprite", 300): _entry(
                    root / "Sprite/Coin_300.json", {"m_Name": "Coin"}
                ),
            },
        }
        scope["pointer_scopes"] = {0: scope}

        product = TOOLS._store_inline_product_descriptor(
            scope,
            {
                "m_infoType": {"m_FileID": 0, "m_PathID": 200},
                "m_amount": 150,
                "m_petId": 0,
                "m_chance": 0,
            },
            0,
        )

        self.assertEqual(product["image_kind"], "sprite")
        self.assertEqual(product["sprite_path_id"], 300)
        self.assertEqual(product["name"], "COIN_TYPE ×150")

    def test_pet_reward_uses_pet_catalog_icon_and_name(self) -> None:
        root = Path("input/sharedassets2")
        pet_rows = [
            {
                "m_itemName": "Cat",
                "m_displayIcon": {"m_FileID": 0, "m_PathID": 310},
            },
            {
                "m_itemName": "Wizard Owl",
                "m_displayIcon": {"m_FileID": 0, "m_PathID": 311},
            },
        ]
        scope = {
            "scope_key": ("manifest", "sharedassets2"),
            "source": "bundle",
            "bundle_entry": "sharedassets2",
            "items": {
                ("MonoBehaviour", 200): _entry(
                    root / "MonoBehaviour/PET_TYPE_200.json",
                    {
                        "m_Name": "PET_TYPE",
                        "m_sprite": {"m_FileID": 0, "m_PathID": 300},
                    },
                ),
                ("MonoBehaviour", 201): _entry(
                    root / "MonoBehaviour/Pet_Shop_Item_Data_201.json",
                    {
                        "m_Name": "Pet_Shop_Item_Data",
                        "m_petItems": {"Array": pet_rows},
                    },
                ),
                ("Sprite", 300): _entry(
                    root / "Sprite/GenericGem_300.json", {"m_Name": "gem"}
                ),
                ("Sprite", 310): _entry(
                    root / "Sprite/Cat_310.json", {"m_Name": "Cat Icon"}
                ),
                ("Sprite", 311): _entry(
                    root / "Sprite/Owl_311.json", {"m_Name": "Owl Icon"}
                ),
            },
        }
        scope["pointer_scopes"] = {0: scope}

        product = TOOLS._store_inline_product_descriptor(
            scope,
            {
                "m_infoType": {"m_FileID": 0, "m_PathID": 200},
                "m_amount": 1,
                "m_petId": 1,
                "m_chance": 5,
            },
            0,
        )

        self.assertEqual(product["sprite_path_id"], 311)
        self.assertEqual(product["image_name"], "Owl Icon")
        self.assertEqual(product["name"], "PET_TYPE ×1 (Wizard Owl)")


if __name__ == "__main__":
    unittest.main()
