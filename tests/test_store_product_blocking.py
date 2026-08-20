from __future__ import annotations

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _load_tools():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_store_product_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tools()


def _entry(path: Path, data: dict) -> dict:
    return {"item": {}, "path": path, "data": data}


class StoreProductBlockingTests(unittest.TestCase):
    def test_shared_object_graph_cache_reuses_memory_and_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "object_graph_cache.pkl"
            snapshot = {"fingerprint": "same"}
            scopes = {("manifest", "entry"): {"items": {}}}
            textures = {"texture.png": (("manifest", "entry"), 9)}
            previous_state = TOOLS._OBJECT_GRAPH_CACHE_STATE
            try:
                TOOLS._OBJECT_GRAPH_CACHE_STATE = None
                with (
                    patch.object(TOOLS, "DEFAULT_OBJECT_GRAPH_CACHE", cache_path),
                    patch.object(TOOLS, "_object_manifest_snapshot", return_value=snapshot),
                    patch.object(
                        TOOLS,
                        "_build_object_graph",
                        return_value=(scopes, textures),
                    ) as build,
                ):
                    first = TOOLS._load_object_graph()
                    second = TOOLS._load_object_graph()
                    self.assertEqual(first, second)
                    build.assert_called_once_with()
                    self.assertTrue(cache_path.is_file())

                    TOOLS._OBJECT_GRAPH_CACHE_STATE = None
                    third = TOOLS._load_object_graph()
                    self.assertEqual(third, first)
                    build.assert_called_once_with()
            finally:
                TOOLS._OBJECT_GRAPH_CACHE_STATE = previous_state

    def test_dynamic_store_scan_result_is_reused(self) -> None:
        previous_state = TOOLS._OBJECT_GRAPH_CACHE_STATE
        scopes = {("manifest", "entry"): {"items": {}}}
        config = {
            "name": "Shop",
            "path_id": 10,
            "array_label": "_products.Array",
            "products": [],
            "source": "shop.assets",
            "bundle_entry": "",
        }
        try:
            TOOLS._OBJECT_GRAPH_CACHE_STATE = {
                "version": TOOLS.OBJECT_GRAPH_CACHE_VERSION,
                "snapshot": {},
                "scopes": scopes,
                "textures": {},
                "derived": {},
            }
            with (
                patch.object(TOOLS, "_load_object_graph", return_value=(scopes, {})),
                patch.object(TOOLS, "_find_store_product_configs", return_value=[config]) as scan,
                patch.object(TOOLS, "_write_object_graph_cache"),
                patch.object(TOOLS, "_load_store_product_records", return_value={"items": []}),
                patch.object(TOOLS, "prompt_input", return_value="b"),
            ):
                TOOLS.run_block_dynamic_store_products()
                TOOLS.run_block_dynamic_store_products()

            scan.assert_called_once_with(scopes)
        finally:
            TOOLS._OBJECT_GRAPH_CACHE_STATE = previous_state

    def test_mesh_and_object_name_queries_reuse_derived_cache(self) -> None:
        previous_state = TOOLS._OBJECT_GRAPH_CACHE_STATE
        scopes = {("manifest", "entry"): {"items": {}}}
        targets = {(('manifest', 'entry'), 42)}
        try:
            TOOLS._OBJECT_GRAPH_CACHE_STATE = {
                "version": TOOLS.OBJECT_GRAPH_CACHE_VERSION,
                "snapshot": {},
                "scopes": scopes,
                "textures": {},
                "derived": {},
            }
            with (
                patch.object(TOOLS, "_write_object_graph_cache"),
                patch.object(
                    TOOLS,
                    "_find_mesh_object_matches",
                    return_value=[{"component_path_id": 7}],
                ) as mesh_scan,
                patch.object(
                    TOOLS,
                    "_find_game_object_name_candidates",
                    return_value=([{"path_id": 8}], "exact"),
                ) as name_scan,
            ):
                first_mesh = TOOLS._cached_mesh_object_matches(scopes, "Mesh", targets)
                second_mesh = TOOLS._cached_mesh_object_matches(scopes, "Mesh", targets)
                first_name = TOOLS._cached_game_object_name_candidates(scopes, "Shop")
                second_name = TOOLS._cached_game_object_name_candidates(scopes, "Shop")

            self.assertEqual(first_mesh, second_mesh)
            self.assertEqual(first_name, second_name)
            mesh_scan.assert_called_once_with(scopes, targets)
            name_scan.assert_called_once_with(scopes, "Shop", "")
        finally:
            TOOLS._OBJECT_GRAPH_CACHE_STATE = previous_state

    def test_detects_runtime_store_shell_without_static_product_array(self) -> None:
        root = Path("input/level22")
        shared = {
            "scope_key": ("manifest", "sharedassets22.assets"),
            "source": r"bin\Data\data\bundle",
            "bundle_entry": "sharedassets22.assets",
            "items": {},
        }
        level = {
            "scope_key": ("manifest", "level22"),
            "source": r"bin\Data\data\bundle",
            "bundle_entry": "level22",
            "items": {},
        }
        level["pointer_scopes"] = {0: level, 5: shared}
        shared["pointer_scopes"] = {0: shared}
        level["items"] = {
            ("GameObject", 3): _entry(
                root / "GameObject/StoreScreen_3.json",
                {"m_Name": "StoreScreen"},
            ),
            ("MonoBehaviour", 85): _entry(
                root / "MonoBehaviour/114_85_85.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 3},
                    "_bundleTemplate": {"m_FileID": 5, "m_PathID": 170},
                    "_itemsHubTemplate": {"m_FileID": 5, "m_PathID": 120},
                    "_itemTemplate": {"m_FileID": 5, "m_PathID": 179},
                    "_noAds": {"m_FileID": 0, "m_PathID": 108},
                },
            ),
        }
        shared["items"] = {
            ("GameObject", 13): _entry(
                Path("input/sharedassets22.assets/GameObject/StoreScreenBundle_13.json"),
                {"m_Name": "StoreScreenBundle"},
            ),
            ("MonoBehaviour", 170): _entry(
                Path("input/sharedassets22.assets/MonoBehaviour/114_170_170.json"),
                {"m_GameObject": {"m_FileID": 0, "m_PathID": 13}},
            ),
        }

        shells = TOOLS._find_runtime_store_shells({level["scope_key"]: level, shared["scope_key"]: shared})

        self.assertEqual(len(shells), 1)
        self.assertEqual(shells[0]["name"], "StoreScreen")
        bundle = next(row for row in shells[0]["pointers"] if row["field"] == "_bundleTemplate")
        self.assertEqual(bundle["object_name"], "StoreScreenBundle")
        self.assertIn("_noAds", {row["field"] for row in shells[0]["pointers"]})

        output = io.StringIO()
        with redirect_stdout(output):
            TOOLS._print_runtime_store_shells(shells)
        message = output.getvalue()
        self.assertIn("[未找到数据表]", message)
        self.assertIn("静态数据表", message)
        self.assertIn("无法列出或选择具体条目", message)

    def test_real_layout_grid_uses_serialized_cell_spacing_and_alignment(self) -> None:
        slots = TOOLS._grid_layout_slots(
            6,
            (0.0, 0.0, 934.06, 1031.9),
            {
                "cell_size": (200.0, 200.0),
                "spacing": (70.0, 70.0),
                "padding": {"m_Left": 0, "m_Right": 0, "m_Top": 0, "m_Bottom": 25},
                "child_alignment": 4,
                "start_corner": 0,
                "start_axis": 0,
                "constraint": 0,
                "constraint_count": 2,
            },
            (934.06, 1031.9),
        )

        self.assertEqual(len(slots), 6)
        self.assertAlmostEqual(slots[1][0] - slots[0][0], 270.0)
        self.assertAlmostEqual(slots[3][1] - slots[0][1], 270.0)
        self.assertEqual(slots[0][2:], (200.0, 200.0))

    def test_layout_root_prefers_shop_prefab_over_store_panel(self) -> None:
        index, node, score = TOOLS._store_layout_root([
            {"path_id": 1, "name": "Store Panel 1"},
            {"path_id": 2, "name": "Market Panel"},
            {"path_id": 3, "name": "Shop Prefab"},
            {"path_id": 4, "name": "Canvas"},
        ])

        self.assertEqual(index, 2)
        self.assertEqual(node["path_id"], 3)
        self.assertGreater(score, 100)

    def test_instantiated_ngui_list_uses_common_parent_and_real_item_objects(self) -> None:
        root = Path("input/tasks")
        scope = {
            "scope_key": ("manifest", "tasks.assets"),
            "source": "tasks.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/Daily_10.json",
                {"m_GameObject": {"m_FileID": 0, "m_PathID": 100}},
            ),
            ("GameObject", 100): _entry(
                root / "GameObject/Daily_100.json", {"m_Name": "Daily"}
            ),
            ("GameObject", 150): _entry(
                root / "GameObject/Container_150.json",
                {
                    "m_Name": "DailyContainer",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 151}}
                    ]},
                },
            ),
            ("Transform", 151): _entry(
                root / "Transform/Container_151.json",
                {"m_GameObject": {"m_FileID": 0, "m_PathID": 150}},
            ),
            ("GameObject", 200): _entry(
                root / "GameObject/TaskA_200.json",
                {
                    "m_Name": "Task A",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 201}}
                    ]},
                },
            ),
            ("Transform", 201): _entry(
                root / "Transform/TaskA_201.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 200},
                    "m_Father": {"m_FileID": 0, "m_PathID": 151},
                },
            ),
            ("GameObject", 210): _entry(
                root / "GameObject/TaskB_210.json",
                {
                    "m_Name": "Task B",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 211}}
                    ]},
                },
            ),
            ("Transform", 211): _entry(
                root / "Transform/TaskB_211.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 210},
                    "m_Father": {"m_FileID": 0, "m_PathID": 151},
                },
            ),
        }
        config = {
            "scope": scope,
            "path_id": 10,
            "array_label": "_dailyTasks.Array",
            "products": [
                {"product_object_path_id": 200},
                {"product_object_path_id": 210},
            ],
        }

        layout = TOOLS._find_instantiated_list_layout(config)

        self.assertIsNotNone(layout)
        self.assertEqual(layout["mode"], "instantiated")
        self.assertEqual(layout["content_path_id"], 150)
        self.assertEqual(layout["item_path_ids"], [200, 210])

    def test_scans_standalone_products_and_resolves_sprite_and_prefab(self) -> None:
        root = Path("input/sharedassets1.assets")
        scope = {
            "scope_key": ("manifest", "sharedassets1.assets"),
            "source": r"bin\Data\data\bundle",
            "bundle_entry": "sharedassets1.assets",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 100): _entry(
                root / "MonoBehaviour/Store Skins_100.json",
                {"m_Name": "Store Skins", "_offers": {"Array": [
                    {"m_FileID": 0, "m_PathID": 200}
                ]}},
            ),
            ("MonoBehaviour", 200): _entry(
                root / "MonoBehaviour/Bullet1_200.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 0},
                    "m_Name": "Bullet1",
                    "_thumbnailImage": {"m_FileID": 0, "m_PathID": 300},
                    "_backSprite": {"m_FileID": 0, "m_PathID": 301},
                    "_prefab": {"m_FileID": 0, "m_PathID": 400},
                    "_rewardADS": 1,
                    "_rewardADSToShow": 2,
                },
            ),
            ("MonoBehaviour", 400): _entry(
                root / "MonoBehaviour/Product_400.json",
                {"m_GameObject": {"m_FileID": 0, "m_PathID": 500}},
            ),
            ("Sprite", 300): _entry(root / "Sprite/Bullet_300.json", {"m_Name": "Bullet"}),
            ("Sprite", 301): _entry(root / "Sprite/Back_301.json", {"m_Name": "Back"}),
            ("GameObject", 500): _entry(root / "GameObject/Product_500.json", {"m_Name": "Product View"}),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        product = configs[0]["products"][0]
        self.assertEqual(product["name"], "Bullet1")
        self.assertEqual(product["sprite_name"], "Bullet")
        self.assertEqual(product["sprite_field"], "_thumbnailImage")
        self.assertEqual(configs[0]["array_label"], "_offers.Array")
        self.assertEqual(product["prefab_name"], "Product View")
        self.assertEqual(product["reward_ads_to_show"], 2)

    def test_detects_product_array_without_image_when_commerce_fields_exist(self) -> None:
        root = Path("input/store")
        scope = {
            "scope_key": ("manifest", "store.assets"),
            "source": "store.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/Store_10.json",
                {
                    "m_Name": "Store",
                    "_products": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/CoinPack_20.json",
                {
                    "m_Name": "Coin Pack",
                    "productId": "coins.100",
                    "price": 2.99,
                    "currency": "USD",
                },
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["products"][0]["image_kind"], "")
        self.assertGreaterEqual(configs[0]["classification"]["score"], 7)

    def test_detects_task_list_without_store_or_image_fields(self) -> None:
        root = Path("input/tasks")
        scope = {
            "scope_key": ("manifest", "tasks.assets"),
            "source": "tasks.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/DailyTasks_10.json",
                {
                    "m_Name": "Daily Tasks",
                    "_tasks": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/Task_20.json",
                {
                    "m_Name": "Win one match",
                    "taskId": "daily.win",
                    "description": "Win one match",
                    "progress": 0,
                    "target": 1,
                },
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["classification"]["kind"], "任务/活动")
        self.assertEqual(configs[0]["products"][0]["image_kind"], "")

    def test_task_entry_resolves_image_through_referenced_uitexture_component(self) -> None:
        root = Path("input/tasks")
        scope = {
            "scope_key": ("manifest", "tasks.assets"),
            "source": "tasks.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/DailyTasks_10.json",
                {
                    "m_Name": "Daily Tasks",
                    "_dailyTasks": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/Task_20.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 21},
                    "_texture": {"m_FileID": 0, "m_PathID": 30},
                    "numQuest": 0,
                },
            ),
            ("GameObject", 21): _entry(
                root / "GameObject/DailyTask_21.json", {"m_Name": "Daily Task"}
            ),
            ("MonoBehaviour", 30): _entry(
                root / "MonoBehaviour/UITexture_30.json",
                {"mTexture": {"m_FileID": 0, "m_PathID": 40}},
            ),
            ("Texture2D", 40): _entry(
                root / "Texture2D/DailyTask_40.png", {"m_Name": "Daily Task Icon"}
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        product = configs[0]["products"][0]
        self.assertEqual(product["name"], "Daily Task")
        self.assertEqual(product["image_kind"], "texture")
        self.assertEqual(product["texture_path_id"], 40)
        self.assertEqual(product["image_field"], "_texture")

    def test_rejects_generic_items_array_with_only_an_image(self) -> None:
        root = Path("input/config")
        scope = {
            "scope_key": ("manifest", "config.assets"),
            "source": "config.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/Config_10.json",
                {
                    "m_Name": "Generic Config",
                    "_items": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/Item_20.json",
                {
                    "m_Name": "Decoration",
                    "_icon": {"m_FileID": 0, "m_PathID": 30},
                },
            ),
            ("Sprite", 30): _entry(
                root / "Sprite/Decoration_30.json", {"m_Name": "Decoration"}
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(configs, [])

    def test_resolves_ngui_atlas_product_image(self) -> None:
        root = Path("input/ngui-store")
        scope = {
            "scope_key": ("manifest", "ngui-store.assets"),
            "source": "ngui-store.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/Store_10.json",
                {
                    "m_Name": "Store",
                    "_products": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/Offer_20.json",
                {
                    "m_Name": "Starter Offer",
                    "productId": "starter",
                    "_iconAtlas": {"m_FileID": 0, "m_PathID": 40},
                    "_iconName": "starter_icon",
                },
            ),
            ("MonoBehaviour", 40): _entry(
                root / "MonoBehaviour/Atlas_40.json",
                {
                    "material": {"m_FileID": 0, "m_PathID": 50},
                    "mSprites": {"Array": [
                        {"name": "starter_icon", "x": 0, "y": 0, "width": 16, "height": 16}
                    ]},
                    "mReplacement": {"m_FileID": 0, "m_PathID": 0},
                },
            ),
            ("Material", 50): _entry(
                root / "Material/Atlas_50.json",
                {"m_SavedProperties": {"m_TexEnvs": {"Array": [
                    {
                        "first": "_MainTex",
                        "second": {"m_Texture": {"m_FileID": 0, "m_PathID": 60}},
                    }
                ]}}},
            ),
            ("Texture2D", 60): _entry(
                root / "Texture2D/Atlas_60.png", {"m_Name": "Atlas"}
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        product = configs[0]["products"][0]
        self.assertEqual(product["image_kind"], "ngui_sprite")
        self.assertEqual(product["ngui_atlas_path_id"], 40)
        self.assertEqual(product["ngui_sprite_name"], "starter_icon")

    def test_resolves_texture_image_from_product_prefab(self) -> None:
        root = Path("input/prefab-store")
        scope = {
            "scope_key": ("manifest", "prefab-store.assets"),
            "source": "prefab-store.assets",
            "bundle_entry": "",
            "items": {},
        }
        scope["pointer_scopes"] = {0: scope}
        scope["items"] = {
            ("MonoBehaviour", 10): _entry(
                root / "MonoBehaviour/Store_10.json",
                {
                    "m_Name": "Store",
                    "_products": {"Array": [{"m_FileID": 0, "m_PathID": 20}]},
                },
            ),
            ("MonoBehaviour", 20): _entry(
                root / "MonoBehaviour/Offer_20.json",
                {
                    "m_Name": "Texture Offer",
                    "productId": "texture.offer",
                    "_prefab": {"m_FileID": 0, "m_PathID": 100},
                },
            ),
            ("GameObject", 100): _entry(
                root / "GameObject/OfferView_100.json",
                {
                    "m_Name": "Offer View",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 101}}
                    ]},
                },
            ),
            ("MonoBehaviour", 101): _entry(
                root / "MonoBehaviour/UITexture_101.json",
                {
                    "m_GameObject": {"m_FileID": 0, "m_PathID": 100},
                    "mTexture": {"m_FileID": 0, "m_PathID": 102},
                },
            ),
            ("Texture2D", 102): _entry(
                root / "Texture2D/Offer_102.png", {"m_Name": "Offer Texture"}
            ),
        }

        configs = TOOLS._find_store_product_configs({scope["scope_key"]: scope})

        self.assertEqual(len(configs), 1)
        product = configs[0]["products"][0]
        self.assertEqual(product["image_kind"], "texture")
        self.assertEqual(product["texture_path_id"], 102)
        self.assertTrue(product["image_field"].startswith("prefab.100."))

    def test_block_and_restore_rebuilds_only_products_array(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "input"
            output_root = base / "output"
            record_path = base / "records/blocked_store_products.json"
            source = source_root / "bin/Data/store/MonoBehaviour/Store_100.json"
            source.parent.mkdir(parents=True)
            original = {
                "m_Name": "Store",
                "keep_me": 7,
                "_products": {"Array": [
                    {"m_FileID": 0, "m_PathID": 200},
                    {"m_FileID": 0, "m_PathID": 201},
                ]},
            }
            source.write_text(json.dumps(original), encoding="utf-8")
            config = {
                "config_key": TOOLS._store_product_config_key(source, 100),
                "source_json": str(source),
                "source": r"bin\Data\store",
                "bundle_entry": "",
                "name": "Store",
                "path_id": 100,
            }
            product = {
                "pointer_key": "0:200", "file_id": 0, "path_id": 200,
                "name": "Bullet1", "index": 0, "sprite_name": "Bullet",
                "prefab_name": "Product View",
            }
            with (
                patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root),
                patch.object(TOOLS, "DEFAULT_OBJECT_TO_IMPORT_ROOT", output_root),
                patch.object(TOOLS, "DEFAULT_STORE_PRODUCT_BLOCK_RECORD", record_path),
            ):
                self.assertTrue(TOOLS._set_store_product_blocked(config, product, True))
                target = output_root / source.relative_to(source_root)
                patched_data = json.loads(target.read_text(encoding="utf-8"))
                self.assertEqual(patched_data["keep_me"], 7)
                self.assertEqual(
                    [row["m_PathID"] for row in patched_data["_products"]["Array"]],
                    [201],
                )
                self.assertTrue(TOOLS._set_store_product_blocked(config, product, False))
                self.assertFalse(target.exists())
                records = json.loads(record_path.read_text(encoding="utf-8"))
                self.assertEqual(records["items"], [])

    def test_unified_restore_preserves_original_order_with_multiple_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source_root = base / "input"
            output_root = base / "output"
            record_path = base / "records/blocked_store_products.json"
            source = source_root / "store/MonoBehaviour/Offers_10.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps({"_offers": {"Array": [
                    {"m_FileID": 0, "m_PathID": path_id}
                    for path_id in (100, 101, 102, 103)
                ]}}),
                encoding="utf-8",
            )
            config = {
                "config_key": TOOLS._store_product_config_key(source, 10, ("_offers", "Array")),
                "source_json": str(source), "source": "store", "bundle_entry": "",
                "name": "Offers", "path_id": 10,
                "array_path": ["_offers", "Array"],
            }

            def product(index: int, path_id: int) -> dict:
                return {
                    "pointer_key": f"0:{path_id}", "file_id": 0,
                    "path_id": path_id, "name": chr(65 + index), "index": index,
                    "sprite_name": "", "sprite_path_id": 0, "prefab_name": "",
                }

            with (
                patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root),
                patch.object(TOOLS, "DEFAULT_OBJECT_TO_IMPORT_ROOT", output_root),
                patch.object(TOOLS, "DEFAULT_STORE_PRODUCT_BLOCK_RECORD", record_path),
            ):
                TOOLS._set_store_product_blocked(config, product(1, 101), True)
                TOOLS._set_store_product_blocked(config, product(2, 102), True)
                target = output_root / source.relative_to(source_root)

                def target_ids() -> list[int]:
                    data = json.loads(target.read_text(encoding="utf-8"))
                    return [row["m_PathID"] for row in data["_offers"]["Array"]]

                self.assertEqual(target_ids(), [100, 103])
                records = TOOLS._load_store_product_records()
                record_b = next(row for row in records["items"] if row["product_path_id"] == 101)
                self.assertTrue(TOOLS._restore_store_product_record(record_b))
                self.assertEqual(target_ids(), [100, 101, 103])
                remaining = TOOLS._load_store_product_records()["items"]
                self.assertEqual([row["product_path_id"] for row in remaining], [102])


if __name__ == "__main__":
    unittest.main()
