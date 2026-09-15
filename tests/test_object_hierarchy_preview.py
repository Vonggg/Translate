from __future__ import annotations

import importlib.util
import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


def _load_tool_scripts():
    script_path = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tool_scripts_for_preview_test", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tool_scripts()


class ObjectHierarchyPreviewLayoutTests(unittest.TestCase):
    def test_layout_type_uses_external_script_not_numeric_path_id(self):
        script_scope = {"items": {("MonoScript", 720): {
            "data": {"m_ClassName": "VerticalLayoutGroup"}, "path": Path("unused")
        }}}
        scope = {"items": {}, "pointer_scopes": {1: script_scope}}
        data = {"m_Script": {"m_FileID": 1, "m_PathID": 720}}
        self.assertEqual(TOOLS._preview_script_class(scope, data), "VerticalLayoutGroup")
        # Same numeric ID in another file must not inherit the first type.
        scope["pointer_scopes"][2] = {"items": {("MonoScript", 720): {
            "data": {"m_ClassName": "HorizontalLayoutGroup"}, "path": Path("unused")
        }}}
        data["m_Script"]["m_FileID"] = 2
        self.assertEqual(TOOLS._preview_script_class(scope, data), "HorizontalLayoutGroup")

    def test_content_fitter_restores_zero_height_before_vertical_layout(self):
        def entry(data):
            return {"data": data, "path": Path("unused")}
        layout = {
            "m_Script": {"m_FileID": 0, "m_PathID": 720},
            "m_ChildAlignment": 1, "m_Spacing": 40,
            "m_ChildControlWidth": 0, "m_ChildControlHeight": 0,
            "m_ChildForceExpandWidth": 0, "m_ChildForceExpandHeight": 0,
            "m_Padding": {"m_Top": 55, "m_Bottom": 130},
        }
        parent = {"m_Component": {"Array": [
            {"component": {"m_PathID": 10}}, {"component": {"m_PathID": 11}}
        ]}}
        transform = {"m_Pivot": {"x": .5, "y": .5}, "m_Children": {"Array": [
            {"m_PathID": 21}, {"m_PathID": 22}, {"m_PathID": 23}
        ]}}
        scope = {"items": {
            ("MonoScript", 720): entry({"m_ClassName": "VerticalLayoutGroup"}),
            ("MonoBehaviour", 10): entry(layout),
            ("MonoBehaviour", 11): entry({"m_HorizontalFit": 0, "m_VerticalFit": 2}),
        }}
        children = []
        for i in (1, 2, 3):
            child = {"m_GameObject": {"m_PathID": i},
                     "m_SizeDelta": {"x": 520, "y": 140},
                     "m_AnchorMin": {"x": 0, "y": 0},
                     "m_AnchorMax": {"x": 0, "y": 0}}
            scope["items"][("RectTransform", 20 + i)] = entry(child)
            scope["items"][("GameObject", i)] = entry({"m_IsActive": i != 3})
            children.append((i, child))
        fitted = TOOLS._preview_content_fitted_rect(scope, parent, transform, (0, 0, 760, 0), (1, 1))
        self.assertEqual(fitted, (0, -252.5, 760, 505))
        rects, applied = TOOLS._preview_vertical_layout_child_rects(scope, parent, fitted, (1, 1), children)
        self.assertTrue(applied)
        self.assertEqual(rects[1][1] - rects[2][1], 180)
        self.assertNotIn(3, rects)

    def test_file_id_scope_falls_back_to_unique_bundle_entry(self) -> None:
        exact_scope = {"asset_key": "source/exact/bundle/CAB-exact"}
        external_scope = {"asset_key": "actual/owner/bundle/CAB-external"}
        by_asset_key = {
            "source/exact/bundle/cab-exact": exact_scope,
            "actual/owner/bundle/cab-external": external_scope,
        }
        by_bundle_entry = {"cab-external": [external_scope]}

        resolved, used_fallback = TOOLS._mapped_pointer_scope(
            by_asset_key,
            by_bundle_entry,
            r"wrong\source\bundle\CAB-external",
        )

        self.assertIs(resolved, external_scope)
        self.assertTrue(used_fallback)

    def test_file_id_scope_does_not_guess_ambiguous_bundle_entry(self) -> None:
        candidates = [{"asset_key": "one"}, {"asset_key": "two"}]

        resolved, used_fallback = TOOLS._mapped_pointer_scope(
            {},
            {"cab-duplicate": candidates},
            "missing/source/bundle/CAB-duplicate",
        )

        self.assertIsNone(resolved)
        self.assertFalse(used_fallback)

    def test_unified_restore_preview_zoom_steps_and_fit(self) -> None:
        self.assertEqual(TOOLS._restore_preview_step_zoom(1.0, 1), 1.25)
        self.assertEqual(TOOLS._restore_preview_step_zoom(1.0, -1), 0.8)
        self.assertEqual(TOOLS._restore_preview_step_zoom(4.0, 1), 4.0)
        self.assertEqual(TOOLS._restore_preview_step_zoom(0.1, -1), 0.1)
        self.assertEqual(
            TOOLS._restore_preview_fit_zoom((2000, 1000), (1000, 800)),
            0.5,
        )
        self.assertEqual(
            TOOLS._restore_preview_fit_zoom((400, 200), (1000, 800)),
            1.0,
        )

    def test_unified_restore_preview_exposes_zoom_controls(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )
        function_source = source[
            source.index("def _show_restore_blocked_window("):
            source.index("def run_restore_blocked_objects(")
        ]

        self.assertIn('text="100%"', function_source)
        self.assertIn('text="适应窗口"', function_source)
        self.assertIn('preview.bind("<MouseWheel>", on_preview_mouse_wheel)', function_source)
        self.assertIn('preview.bind("<ButtonPress-1>", start_preview_drag)', function_source)
        self.assertIn('preview.bind("<B1-Motion>", drag_preview)', function_source)
        self.assertIn('preview.scan_dragto(event.x, event.y, gain=1)', function_source)
        self.assertIn('orient="horizontal", command=preview.xview', function_source)
        self.assertIn('orient="vertical", command=preview.yview', function_source)

    def test_restore_preview_limits_three_parent_and_child_layers(self) -> None:
        item = {
            "hierarchy_chain": [
                {"path_id": path_id, "name": f"up-{index}"}
                for index, path_id in enumerate((10, 20, 30, 40, 50, 60))
            ],
            "hierarchy_descendants": [
                {"path_id": 100 + depth, "name": f"down-{depth}", "depth": depth}
                for depth in range(1, 6)
            ],
        }

        chain, descendants, allowed = TOOLS._limited_restore_hierarchy(item)

        self.assertEqual([node["path_id"] for node in chain], [10, 20, 30, 40])
        self.assertEqual([node["depth"] for node in descendants], [1, 2, 3])
        self.assertEqual(allowed, {10, 20, 30, 40, 101, 102, 103})

    def test_restore_preview_without_saved_children_only_keeps_parent_chain(self) -> None:
        chain, descendants, allowed = TOOLS._limited_restore_hierarchy(
            {
                "hierarchy_chain": [
                    {"path_id": path_id} for path_id in (1, 2, 3, 4, 5)
                ]
            }
        )

        self.assertEqual([node["path_id"] for node in chain], [1, 2, 3, 4])
        self.assertEqual(descendants, [])
        self.assertEqual(allowed, {1, 2, 3, 4})

    def test_game_object_name_search_prefers_exact_and_supports_wildcards(self) -> None:
        def entry(name: str, path_id: int) -> dict:
            return {
                "item": {"AssetName": name},
                "path": Path(f"GameObject/{name}_{path_id}.json"),
                "data": {"m_Name": name, "m_Component": {"Array": []}},
            }

        scopes = {
            ("manifest-level2", ""): {
                "scope_key": ("manifest-level2", ""),
                "source": r"bin\Data\level2",
                "bundle_entry": "",
                "asset_key": "bin/data/level2",
                "items": {
                    ("GameObject", 10): entry("ShopButton", 10),
                    ("GameObject", 11): entry("PremiumShopButton", 11),
                },
            },
            ("manifest-level3", "ui"): {
                "scope_key": ("manifest-level3", "ui"),
                "source": r"bin\Data\level3",
                "bundle_entry": "ui",
                "asset_key": "bin/data/level3",
                "items": {
                    ("GameObject", 20): entry("ShopButton", 20),
                },
            },
        }

        exact, exact_mode = TOOLS._find_game_object_name_candidates(
            scopes,
            "shopbutton",
        )
        wildcard, wildcard_mode = TOOLS._find_game_object_name_candidates(
            scopes,
            "*shop*",
        )
        filtered, _ = TOOLS._find_game_object_name_candidates(
            scopes,
            "ShopButton",
            "level3",
        )

        self.assertEqual(exact_mode, "exact")
        self.assertEqual([row["path_id"] for row in exact], [10, 20])
        self.assertEqual(wildcard_mode, "contains")
        self.assertEqual({row["path_id"] for row in wildcard}, {10, 11, 20})
        self.assertEqual([row["path_id"] for row in filtered], [20])
        match = TOOLS._game_object_name_match(exact[0])
        self.assertEqual(match["component_type"], "GameObject")
        self.assertEqual(match["chain"][0]["path_id"], 10)

    def test_preview_source_prioritizes_selected_chain_before_siblings(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("chain_child_by_parent = {", source)
        self.assertIn("preferred_child_id = chain_child_by_parent.get(object_id)", source)
        self.assertIn("traversal_child_nodes = list(serialized_child_nodes)", source)
        self.assertLess(
            source.index("traversal_child_nodes.sort("),
            source.index(
                "for child_object_id, child_transform in traversal_child_nodes:",
                source.index("traversal_child_nodes.sort("),
            ),
        )

    def test_preview_context_menu_blocks_and_unblocks_without_bottom_buttons(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('label="屏蔽此层级 Object"', source)
        self.assertIn('label="取消屏蔽此层级 Object"', source)
        self.assertIn("def block_preview_region(region: dict) -> None:", source)
        self.assertIn("def unblock_preview_region(region: dict) -> None:", source)
        self.assertIn('tree_records[object_id]["image_resources"]', source)
        self.assertIn("resource_query_label = _preview_resource_query_label(region)", source)
        self.assertIn("command=lambda item=region: show_resource_names(item)", source)
        self.assertNotIn('text="屏蔽此层级"', source)
        self.assertNotIn('text="取消屏蔽此层级"', source)

    def test_hierarchy_field_editor_stages_scalar_override_for_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "input"
            source = source_root / "bundle" / "MonoBehaviour" / "114_7_7.json"
            source.parent.mkdir(parents=True)
            source.write_text(
                json.dumps(
                    {
                        "m_Script": {"m_FileID": 1, "m_PathID": 2},
                        "mText": "50",
                        "mFontSize": 24,
                        "mColor": {"r": 1.0, "g": 0.5},
                    }
                ),
                encoding="utf-8",
            )
            import_root = root / "output" / "Object" / "ToImport"
            with patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root), patch.object(
                TOOLS, "DEFAULT_OBJECT_TO_IMPORT_ROOT", import_root
            ):
                target, old_value, new_value = TOOLS.write_object_component_field_override(
                    source, "mText", "2"
                )
                TOOLS.write_object_component_field_override(source, "mFontSize", "30")

            self.assertEqual((old_value, new_value), ("50", "2"))
            self.assertEqual(target, import_root / "bundle" / "MonoBehaviour" / "114_7_7.json")
            staged = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(staged["mText"], "2")
            self.assertEqual(staged["mFontSize"], 30)
            self.assertEqual(staged["m_Script"], {"m_FileID": 1, "m_PathID": 2})

    def test_hierarchy_field_editor_excludes_object_pointers(self) -> None:
        fields = dict(
            TOOLS._iter_editable_object_fields(
                {
                    "mText": "50",
                    "mScript": {"m_FileID": 1, "m_PathID": 2},
                    "mColor": {"r": 1.0},
                    "m_Items": {"Array": ["not editable"]},
                }
            )
        )
        self.assertEqual(fields, {"mText": "50", "mColor.r": 1.0})
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('label="修改此层级显示文本…"', source)
        self.assertIn("write_object_component_field_override", source)

    def test_preview_resource_query_reports_sprite_and_texture_names(self) -> None:
        scope = {
            "source": r"bin\Data\sharedassets0.assets",
            "bundle_entry": "ui.bundle",
            "items": {
                ("Sprite", 7): {
                    "item": {"AssetName": "Start Button Sprite"},
                    "path": Path("Sprite/Start Button Sprite_7.json"),
                    "data": {
                        "m_Name": "fallback sprite",
                        "m_RD": {"texture": {"m_FileID": 0, "m_PathID": 9}},
                    },
                },
                ("Texture2D", 9): {
                    "item": {"AssetName": "Main UI Atlas"},
                    "path": Path("Texture2D/Main UI Atlas_9.png"),
                    "data": {"m_Name": "fallback texture"},
                },
            },
        }

        resources = TOOLS._preview_sprite_resource_records(scope, 7)
        region = {"image_resources": resources}

        self.assertEqual(
            [(item["type"], item["name"], item["path_id"]) for item in resources],
            [
                ("Sprite", "Start Button Sprite", 7),
                ("Texture2D", "Main UI Atlas", 9),
            ],
        )
        self.assertEqual(
            TOOLS._preview_resource_query_label(region),
            "查询 Sprite / Texture2D 资源名字",
        )
        detail = TOOLS._preview_resource_name_text(region)
        self.assertIn("Sprite Unity 资源名: Start Button Sprite (PathID=7)", detail)
        self.assertIn("Texture2D Unity 资源名: Main UI Atlas (PathID=9)", detail)
        self.assertIn(r"来源资源: bin\Data\sharedassets0.assets", detail)
        self.assertIn(r"导出文件: Sprite\Start Button Sprite_7.json", detail)
        self.assertIn(r"导出文件: Texture2D\Main UI Atlas_9.png", detail)

    def test_preview_resource_dialog_is_copyable(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("def _show_copyable_text_dialog(parent, title: str, text: str)", source)
        self.assertIn('text="复制全部"', source)
        self.assertIn("_show_copyable_text_dialog(\n            window,", source)

    def test_preview_resource_query_is_hidden_without_image_resource(self) -> None:
        self.assertEqual(TOOLS._preview_resource_query_label({}), "")

    def test_preview_has_direct_parent_navigation_button(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "工具脚本.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("def return_to_parent_level() -> None:", source)
        self.assertIn('text="返回当前层级的上一层级"', source)
        self.assertIn("refresh_for_path_id(parent_path_id)", source)

    def test_preview_hierarchy_overlay_text_can_be_hidden(self) -> None:
        region = {"display_depth": 7, "name": "Deep Child"}

        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(region, visible=False),
            "",
        )
        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(region, visible=True),
            "层级 7: Deep Child",
        )
        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(
                region,
                visible=True,
                selected=True,
            ),
            "已选择层级 7: Deep Child",
        )
        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(
                region,
                visible=True,
                blocked=True,
            ),
            "已屏蔽 层级 7: Deep Child",
        )

    def test_preview_hierarchy_overlay_text_respects_live_depth_limit(self) -> None:
        current = {"path_id": 10, "display_depth": 0, "name": "Current"}
        child = {"path_id": 12, "display_depth": 2, "name": "Grandchild"}
        too_deep = {"path_id": 13, "display_depth": 3, "name": "Too Deep"}

        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(current, visible=True, max_depth=2),
            "层级 0: Current",
        )
        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(child, visible=True, max_depth=2),
            "层级 2: Grandchild",
        )
        self.assertEqual(
            TOOLS._preview_hierarchy_overlay_text(too_deep, visible=True, max_depth=2),
            "",
        )
        self.assertIsNone(TOOLS._preview_hierarchy_text_depth_limit("全部"))
        self.assertEqual(TOOLS._preview_hierarchy_text_depth_limit("2"), 2)
        self.assertEqual(TOOLS._preview_hierarchy_text_depth_limit("-3"), 0)
        self.assertTrue(
            TOOLS._preview_hierarchy_overlay_is_visible(
                child,
                visible=True,
                max_depth=2,
            )
        )
        self.assertFalse(
            TOOLS._preview_hierarchy_overlay_is_visible(
                too_deep,
                visible=True,
                max_depth=2,
            )
        )
        self.assertFalse(
            TOOLS._preview_hierarchy_overlay_is_visible(
                current,
                visible=False,
                max_depth=None,
            )
        )
        self.assertFalse(
            TOOLS._preview_hierarchy_overlay_is_visible(
                child,
                visible=True,
                max_depth=None,
                hidden_path_ids={int(child.get("path_id", 0) or 0)},
            )
        )

    def test_preview_tree_collapses_descendants_but_keeps_other_branches(self) -> None:
        nodes = [
            {"path_id": 1, "name": "Root", "chain": [{"path_id": 1}]},
            {
                "path_id": 2,
                "name": "Branch",
                "chain": [{"path_id": 1}, {"path_id": 2}],
            },
            {
                "path_id": 3,
                "name": "Hidden child",
                "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 3}],
            },
            {
                "path_id": 4,
                "name": "Sibling",
                "chain": [{"path_id": 1}, {"path_id": 4}],
            },
        ]

        visible = TOOLS._preview_visible_tree_nodes(nodes, {2})

        self.assertEqual([node["path_id"] for node in visible], [1, 2, 4])

    def test_preview_right_click_hit_test_lists_deepest_overlap_first(self) -> None:
        nodes = [
            {
                "path_id": 1,
                "display_depth": 0,
                "x": 0,
                "y": 0,
                "width": 200,
                "height": 200,
            },
            {
                "path_id": 2,
                "display_depth": 2,
                "x": 20,
                "y": 20,
                "width": 50,
                "height": 50,
            },
            {
                "path_id": 3,
                "display_depth": 1,
                "x": 10,
                "y": 10,
                "width": 100,
                "height": 100,
            },
        ]

        hits = TOOLS._preview_nodes_at_point(nodes, 30, 30)

        self.assertEqual([node["path_id"] for node in hits], [2, 3, 1])

    def test_terminal_parent_chain_marks_exact_blocked_resource_red(self) -> None:
        match = {
            "source": r"bin\Data\data.unity3d",
            "bundle_entry": "level6",
            "component_type": "MonoBehaviour",
            "component_path_id": 1822,
            "chain": [
                {
                    "path_id": 112,
                    "name": "Image",
                    "source_json": r"D:\workspace\level6\GameObject\Image_112.json",
                },
                {
                    "path_id": 106,
                    "name": "Title",
                    "source_json": r"D:\workspace\level6\GameObject\Title_106.json",
                },
            ],
        }
        records = {
            "items": [
                {
                    "source_resource": r"bin\Data\data.unity3d",
                    "bundle_entry": "level6",
                    "source_json": r"D:\workspace\level6\GameObject\Title_106.json",
                    "game_object_path_id": 106,
                    "game_object_name": "Title",
                }
            ]
        }
        output = io.StringIO()
        with patch.object(TOOLS, "_load_block_records", return_value=records):
            with redirect_stdout(output):
                TOOLS._print_object_match(1, match)

        text = output.getvalue()
        self.assertIn("0. Image (PathID=112)", text)
        self.assertIn("\033[91m1. [已屏蔽] Title (PathID=106)\033[0m", text)

    def test_preview_unblock_only_restores_exact_resource_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source1 = root / "input" / "bundle" / "level1" / "GameObject" / "NO ADS_197.json"
            source2 = root / "input" / "bundle" / "level2" / "GameObject" / "NO ADS_197.json"
            target1 = root / "output" / "level1" / "NO ADS_197.json"
            target2 = root / "output" / "level2" / "NO ADS_197.json"
            record_path = root / "blocked.json"
            source1.parent.mkdir(parents=True)
            source2.parent.mkdir(parents=True)
            target1.parent.mkdir(parents=True)
            target2.parent.mkdir(parents=True)
            source1.write_text(json.dumps({"m_IsActive": 1}), encoding="utf-8")
            source2.write_text(json.dumps({"m_IsActive": 1}), encoding="utf-8")
            target1.write_text(json.dumps({"m_IsActive": 0}), encoding="utf-8")
            target2.write_text(json.dumps({"m_IsActive": 0}), encoding="utf-8")
            records = {
                "items": [
                    {
                        "source_json": str(source1),
                        "replacement_json": str(target1),
                        "source_resource": r"bin\Data\data.unity3d",
                        "bundle_entry": "level1",
                        "game_object_path_id": 197,
                        "game_object_name": "NO ADS",
                        "original_m_IsActive": 1,
                    },
                    {
                        "source_json": str(source2),
                        "replacement_json": str(target2),
                        "source_resource": r"bin\Data\data.unity3d",
                        "bundle_entry": "level2",
                        "game_object_path_id": 197,
                        "game_object_name": "NO ADS",
                        "original_m_IsActive": 1,
                    },
                ]
            }
            record_path.write_text(json.dumps(records), encoding="utf-8")

            with patch.object(TOOLS, "DEFAULT_BLOCK_RECORD", record_path):
                restored = TOOLS._unblock_preview_node(
                    {"path_id": 197, "source_json": str(source1)},
                    r"bin\Data\data.unity3d",
                    "level1",
                )

            self.assertTrue(restored)
            self.assertEqual(json.loads(target1.read_text())["m_IsActive"], 1)
            self.assertEqual(json.loads(target2.read_text())["m_IsActive"], 0)
            remaining = json.loads(record_path.read_text())["items"]
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["bundle_entry"], "level2")

    def test_block_record_json_location_shows_path_inside_export_root(self) -> None:
        source_json = (
            TOOLS.DEFAULT_SOURCE_ROOT
            / "bin"
            / "Data"
            / "data"
            / "bundle"
            / "level2"
            / "GameObject"
            / "NO ADS_197.json"
        )
        self.assertEqual(
            TOOLS._block_record_json_location({"source_json": str(source_json)}),
            str(
                Path("bin")
                / "Data"
                / "data"
                / "bundle"
                / "level2"
                / "GameObject"
                / "NO ADS_197.json"
            ),
        )

    def test_blocked_path_id_is_scoped_by_bundle_entry(self) -> None:
        records = {
            "items": [
                {
                    "source_resource": r"bin\Data\data.unity3d",
                    "bundle_entry": "level1",
                    "source_json": r"D:\workspace\input\data\bundle\level1\GameObject\NO ADS_197.json",
                    "game_object_path_id": 197,
                    "game_object_name": "NO ADS",
                },
                {
                    "source_resource": "bin/Data/data.unity3d",
                    "bundle_entry": "level2",
                    "source_json": r"D:\workspace\input\data\bundle\level2\GameObject\NO ADS_197.json",
                    "game_object_path_id": 197,
                    "game_object_name": "NO ADS",
                },
            ]
        }

        self.assertEqual(
            TOOLS._blocked_path_ids_for_preview_scope(
                records,
                "bin/Data/data.unity3d",
                "level1",
            ),
            {197},
        )
        self.assertEqual(
            TOOLS._blocked_path_ids_for_preview_scope(
                records,
                r"bin\Data\data.unity3d",
                "level3",
            ),
            set(),
        )
        rows = TOOLS._blocked_object_rows(records)
        self.assertTrue(
            TOOLS._is_preview_node_blocked(
                {
                    "path_id": 197,
                    "source_json": r"D:\workspace\input\data\bundle\level1\GameObject\NO ADS_197.json",
                },
                rows,
                r"bin\Data\data.unity3d",
                "level1",
            )
        )
        self.assertFalse(
            TOOLS._is_preview_node_blocked(
                {
                    "path_id": 197,
                    "source_json": r"D:\workspace\input\data\bundle\level1\GameObject\Another Object_197.json",
                },
                rows,
                r"bin\Data\data.unity3d",
                "level1",
            )
        )
        self.assertTrue(
            TOOLS._is_preview_node_blocked(
                {"path_id": 197},
                rows,
                r"bin\Data\data.unity3d",
                "level2",
            )
        )

    def test_center_anchored_rect(self) -> None:
        transform = {
            "m_AnchorMin": {"x": 0.5, "y": 0.5},
            "m_AnchorMax": {"x": 0.5, "y": 0.5},
            "m_AnchoredPosition": {"x": 10, "y": 20},
            "m_SizeDelta": {"x": 100, "y": 40},
            "m_Pivot": {"x": 0.5, "y": 0.5},
        }
        self.assertEqual(
            TOOLS._rect_transform_child_rect(transform, (0, 0, 800, 600)),
            (360.0, 300.0, 100.0, 40.0),
        )

    def test_rect_transform_replaces_serialized_nan_with_field_defaults(self) -> None:
        transform = {
            "m_AnchorMin": {"x": "NaN", "y": "NaN"},
            "m_AnchorMax": {"x": "NaN", "y": "NaN"},
            "m_AnchoredPosition": {"x": "NaN", "y": "NaN"},
            "m_SizeDelta": {"x": 100, "y": 40},
            "m_Pivot": {"x": "NaN", "y": "NaN"},
        }

        rect = TOOLS._rect_transform_child_rect(transform, (0, 0, 800, 600))

        self.assertEqual(rect, (350.0, 280.0, 100.0, 40.0))
        self.assertTrue(all(math.isfinite(value) for value in rect))

    def test_stretched_rect_uses_anchor_span_and_negative_size_delta(self) -> None:
        transform = {
            "m_AnchorMin": {"x": 0, "y": 0},
            "m_AnchorMax": {"x": 1, "y": 1},
            "m_AnchoredPosition": {"x": 0, "y": 0},
            "m_SizeDelta": {"x": -20, "y": -40},
            "m_Pivot": {"x": 0.5, "y": 0.5},
        }
        self.assertEqual(
            TOOLS._rect_transform_child_rect(transform, (0, 0, 800, 600)),
            (10.0, 20.0, 780.0, 560.0),
        )

    def test_aspect_fitter_height_controls_width_before_child_layout(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {
                        "m_Enabled": 1,
                        "m_AspectMode": 2,
                        "m_AspectRatio": 2.0,
                    },
                    "path": Path("AspectRatioFitter.json"),
                }
            }
        }
        game_object = {
            "m_Component": {
                "Array": [{"component": {"m_FileID": 0, "m_PathID": 99}}]
            }
        }
        transform = {"m_Pivot": {"x": 0.0, "y": 0.5}}

        rect, applied = TOOLS._preview_aspect_fitted_rect(
            scope,
            game_object,
            transform,
            (10.0, 20.0, 1.0, 100.0),
        )

        self.assertTrue(applied)
        self.assertEqual(rect, (10.0, 20.0, 200.0, 100.0))

    def test_aspect_fitter_width_controls_height_preserves_pivot(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {
                        "m_Enabled": 1,
                        "m_AspectMode": 1,
                        "m_AspectRatio": 4.0,
                    },
                    "path": Path("AspectRatioFitter.json"),
                }
            }
        }
        game_object = {
            "m_Component": {
                "Array": [{"component": {"m_FileID": 0, "m_PathID": 99}}]
            }
        }
        transform = {"m_Pivot": {"x": 0.5, "y": 1.0}}

        rect, applied = TOOLS._preview_aspect_fitted_rect(
            scope,
            game_object,
            transform,
            (0.0, 10.0, 200.0, 1.0),
        )

        self.assertTrue(applied)
        self.assertEqual(rect, (0.0, -39.0, 200.0, 50.0))

    def test_horizontal_layout_group_positions_reverse_children(self) -> None:
        def entry(data: dict) -> dict:
            return {"data": data, "path": Path("fixture.json")}

        scope = {
            "items": {
                ("MonoBehaviour", 90): entry({
                    "m_Enabled": 1,
                    "m_Script": {
                        "m_FileID": 1,
                        "m_PathID": 664,
                    },
                    "m_Padding": {
                        "m_Left": 10,
                        "m_Right": 10,
                        "m_Top": 0,
                        "m_Bottom": 0,
                    },
                    "m_ChildAlignment": 4,
                    "m_Spacing": 5,
                    "m_ChildForceExpandWidth": 0,
                    "m_ChildForceExpandHeight": 0,
                    "m_ChildControlWidth": 0,
                    "m_ChildControlHeight": 0,
                    "m_ReverseArrangement": 1,
                }),
                ("GameObject", 1): entry({
                    "m_Name": "Parent",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 90}}
                    ]},
                }),
                ("GameObject", 2): entry({
                    "m_Name": "First",
                    "m_Component": {"Array": []},
                }),
                ("GameObject", 3): entry({
                    "m_Name": "Second",
                    "m_Component": {"Array": []},
                }),
            }
        }
        child_transform = lambda width: {
            "m_AnchorMin": {"x": 0, "y": 0.5},
            "m_AnchorMax": {"x": 0, "y": 0.5},
            "m_AnchoredPosition": {"x": 0, "y": 0},
            "m_SizeDelta": {"x": width, "y": 20},
            "m_Pivot": {"x": 0.5, "y": 0.5},
            "m_LocalScale": {"x": 1, "y": 1},
        }

        rects, applied = TOOLS._preview_horizontal_layout_child_rects(
            scope,
            scope["items"][("GameObject", 1)]["data"],
            (0.0, 0.0, 200.0, 40.0),
            (1.0, 1.0),
            [(2, child_transform(40)), (3, child_transform(60))],
        )

        self.assertTrue(applied)
        self.assertEqual(rects[3], (47.5, 10.0, 60.0, 20.0))
        self.assertEqual(rects[2], (112.5, 10.0, 40.0, 20.0))

    def test_vertical_layout_group_positions_current_unity_children_top_to_bottom(self) -> None:
        def entry(data: dict) -> dict:
            return {"data": data, "path": Path("fixture.json")}

        scope = {
            "items": {
                ("MonoBehaviour", 90): entry({
                    "m_Enabled": 1,
                    "m_Script": {"m_FileID": 1, "m_PathID": 1227},
                    "m_Padding": {
                        "m_Left": 0,
                        "m_Right": 0,
                        "m_Top": 10,
                        "m_Bottom": 10,
                    },
                    "m_ChildAlignment": 4,
                    "m_Spacing": 5,
                    "m_ChildForceExpandWidth": 0,
                    "m_ChildForceExpandHeight": 0,
                    "m_ChildControlWidth": 0,
                    "m_ChildControlHeight": 0,
                    "m_ReverseArrangement": 0,
                }),
                ("GameObject", 1): entry({
                    "m_Name": "Sidebar",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 90}}
                    ]},
                }),
                ("GameObject", 2): entry({
                    "m_Name": "Play",
                    "m_IsActive": True,
                    "m_Component": {"Array": []},
                }),
                ("GameObject", 3): entry({
                    "m_Name": "Login",
                    "m_IsActive": True,
                    "m_Component": {"Array": []},
                }),
                ("GameObject", 4): entry({
                    "m_Name": "Settings",
                    "m_IsActive": True,
                    "m_Component": {"Array": []},
                }),
            }
        }
        child_transform = {
            "m_AnchorMin": {"x": 0, "y": 0},
            "m_AnchorMax": {"x": 0, "y": 0},
            "m_AnchoredPosition": {"x": 0, "y": 0},
            "m_SizeDelta": {"x": 100, "y": 50},
            "m_Pivot": {"x": 0.5, "y": 0.5},
            "m_LocalScale": {"x": 1, "y": 1},
        }

        rects, applied = TOOLS._preview_vertical_layout_child_rects(
            scope,
            scope["items"][("GameObject", 1)]["data"],
            (0.0, 0.0, 200.0, 300.0),
            (1.0, 1.0),
            [(2, child_transform), (3, child_transform), (4, child_transform)],
        )

        self.assertTrue(applied)
        self.assertEqual(rects[2], (50.0, 180.0, 100.0, 50.0))
        self.assertEqual(rects[3], (50.0, 125.0, 100.0, 50.0))
        self.assertEqual(rects[4], (50.0, 70.0, 100.0, 50.0))

    def test_selected_chain_does_not_reorder_vertical_layout_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            def entry(data: dict, name: str) -> dict:
                return {"data": data, "path": root / name}

            scope = {
                "source": "fixture.bundle",
                "bundle_entry": "ui",
                "items": {
                    ("GameObject", 1): entry({
                        "m_Name": "Sidebar",
                        "m_IsActive": True,
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 10}},
                            {"component": {"m_FileID": 0, "m_PathID": 90}},
                        ]},
                    }, "Sidebar.json"),
                    ("RectTransform", 10): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 1},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                        "m_Children": {"Array": [
                            {"m_FileID": 0, "m_PathID": 20},
                            {"m_FileID": 0, "m_PathID": 30},
                            {"m_FileID": 0, "m_PathID": 40},
                        ]},
                        "m_Father": {"m_FileID": 0, "m_PathID": 0},
                        "m_AnchorMin": {"x": 0, "y": 0},
                        "m_AnchorMax": {"x": 0, "y": 0},
                        "m_AnchoredPosition": {"x": 0, "y": 0},
                        "m_SizeDelta": {"x": 600, "y": 400},
                        "m_Pivot": {"x": 0, "y": 0},
                    }, "Sidebar.RectTransform.json"),
                    ("MonoBehaviour", 90): entry({
                        "m_Enabled": 1,
                        "m_Script": {"m_FileID": 1, "m_PathID": 1227},
                        "m_Padding": {
                            "m_Left": 0,
                            "m_Right": 0,
                            "m_Top": 0,
                            "m_Bottom": 0,
                        },
                        "m_ChildAlignment": 4,
                        "m_Spacing": 0,
                        "m_ChildForceExpandWidth": 0,
                        "m_ChildForceExpandHeight": 1,
                        "m_ChildControlWidth": 0,
                        "m_ChildControlHeight": 0,
                        "m_ReverseArrangement": 0,
                    }, "VerticalLayoutGroup.json"),
                },
            }
            for object_id, transform_id, name in (
                (2, 20, "Play"),
                (3, 30, "Login"),
                (4, 40, "Settings"),
            ):
                scope["items"][("GameObject", object_id)] = entry({
                    "m_Name": name,
                    "m_IsActive": True,
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": transform_id}}
                    ]},
                }, f"{name}.json")
                scope["items"][("RectTransform", transform_id)] = entry({
                    "m_GameObject": {"m_FileID": 0, "m_PathID": object_id},
                    "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                    "m_Children": {"Array": []},
                    "m_Father": {"m_FileID": 0, "m_PathID": 10},
                    "m_AnchorMin": {"x": 0, "y": 0},
                    "m_AnchorMax": {"x": 0, "y": 0},
                    "m_AnchoredPosition": {"x": 0, "y": 0},
                    "m_SizeDelta": {"x": 500, "y": 82},
                    "m_Pivot": {"x": 0.5, "y": 0.5},
                }, f"{name}.RectTransform.json")
            scope["pointer_scopes"] = {0: scope}
            scopes = {("fixture", "ui"): scope}
            match = {
                "source": "fixture.bundle",
                "bundle_entry": "ui",
                "chain": [
                    {"name": "Settings", "path_id": 4},
                    {"name": "Sidebar", "path_id": 1},
                ],
            }

            with patch.object(TOOLS, "DEFAULT_OBJECT_PREVIEW_ROOT", root / "preview"):
                target = TOOLS._create_object_hierarchy_preview(match, 1, scopes)
                metadata = json.loads(
                    target.with_suffix(".regions.json").read_text(encoding="utf-8")
                )

            nodes = {int(node["path_id"]): node for node in metadata["tree_nodes"]}
            self.assertEqual(nodes[1]["children"], [2, 3, 4])
            self.assertEqual(nodes[1]["layout_note"], "vertical_layout_group")
            self.assertLess(nodes[2]["y"], nodes[3]["y"])
            self.assertLess(nodes[3]["y"], nodes[4]["y"])

    def test_vertical_runtime_state_layout_only_restores_cross_axis(self) -> None:
        def entry(data: dict) -> dict:
            return {"data": data, "path": Path("fixture.json")}

        scope = {
            "items": {
                ("MonoBehaviour", 90): entry({
                    "m_Enabled": 1,
                    "m_Script": {
                        "m_FileID": 1,
                        "m_PathID": -4621643977240678714,
                    },
                    "m_Padding": {
                        "m_Left": 0,
                        "m_Right": 0,
                        "m_Top": 0,
                        "m_Bottom": 0,
                    },
                    "m_ChildAlignment": 1,
                    "m_Spacing": 0,
                    "m_ChildForceExpandWidth": 1,
                    "m_ChildForceExpandHeight": 1,
                    "m_ChildControlWidth": 0,
                    "m_ChildControlHeight": 0,
                    "m_ReverseArrangement": 0,
                }),
                ("GameObject", 1): entry({
                    "m_Name": "Runtime states",
                    "m_Component": {"Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 90}}
                    ]},
                }),
                ("GameObject", 2): entry({
                    "m_Name": "State",
                    "m_Component": {"Array": []},
                }),
            }
        }
        child_transform = {
            "m_AnchorMin": {"x": 0, "y": 0},
            "m_AnchorMax": {"x": 0, "y": 0},
            "m_AnchoredPosition": {"x": 0, "y": -30},
            "m_SizeDelta": {"x": 80, "y": 20},
            "m_Pivot": {"x": 0.5, "y": 0.5},
            "m_LocalScale": {"x": 1, "y": 1},
        }

        rects, applied = TOOLS._preview_vertical_layout_cross_axis_rects(
            scope,
            scope["items"][("GameObject", 1)]["data"],
            (0.0, 0.0, 200.0, 1.0),
            (1.0, 1.0),
            [(2, child_transform)],
        )

        self.assertTrue(applied)
        self.assertEqual(rects[2], (60.0, -40.0, 80.0, 20.0))

    def test_ui_preview_root_ignores_uninitialized_all_zero_scale(self) -> None:
        transform = {"m_LocalScale": {"x": 0.0, "y": 0.0, "z": 0.0}}

        self.assertEqual(
            TOOLS._preview_root_world_scale(transform),
            (1.0, 1.0),
        )

    def test_ui_preview_root_preserves_nonzero_serialized_scale(self) -> None:
        transform = {"m_LocalScale": {"x": 0.5, "y": 2.0, "z": 1.0}}

        self.assertEqual(
            TOOLS._preview_root_world_scale(transform),
            (0.5, 2.0),
        )

    def test_preview_normalizes_zero_scale_on_intermediate_runtime_container(self) -> None:
        transform = {"m_LocalScale": {"x": 0.0, "y": 0.0, "z": 0.0}}

        self.assertEqual(
            TOOLS._preview_effective_local_scale(transform),
            (1.0, 1.0),
        )

    def test_rect_applies_child_local_scale_to_its_own_bounds(self) -> None:
        transform = {
            "m_AnchorMin": {"x": 0.5, "y": 0.5},
            "m_AnchorMax": {"x": 0.5, "y": 0.5},
            "m_AnchoredPosition": {"x": 10, "y": 20},
            "m_SizeDelta": {"x": 100, "y": 40},
            "m_Pivot": {"x": 0.5, "y": 0.5},
            "m_LocalScale": {"x": 0.5, "y": 0.5},
        }

        self.assertEqual(
            TOOLS._rect_transform_child_rect(transform, (0, 0, 800, 600)),
            (385.0, 310.0, 50.0, 20.0),
        )

    def test_offscreen_sibling_does_not_set_ui_detail_scale(self) -> None:
        self.assertAlmostEqual(
            TOOLS._preview_canvas_scale(1920, 1080, 6000, 1080),
            1600 / 1920,
        )

    def test_parent_rotation_moves_child_center_in_parent_frame(self) -> None:
        rotated = TOOLS._preview_rotate_rect_position(
            (9.0, -1.0, 2.0, 2.0),
            (0.0, 0.0),
            90.0,
        )

        for actual, expected in zip(rotated, (-1.0, 9.0, 2.0, 2.0)):
            self.assertAlmostEqual(actual, expected)

    def test_array_value_accepts_unity_array_wrapper(self) -> None:
        values = [{"m_PathID": 1}, {"m_PathID": 2}]
        self.assertIs(TOOLS._array_value({"Array": values}), values)
        self.assertEqual(TOOLS._array_value(None), [])

    def test_non_leaf_selection_displays_rebased_complete_subtree(self) -> None:
        nodes = {
            1: {"path_id": 1, "name": "Root", "children": [2, 3], "chain": [{"path_id": 1}]},
            2: {"path_id": 2, "name": "Left", "children": [], "chain": [{"path_id": 1}, {"path_id": 2}]},
            3: {"path_id": 3, "name": "Right", "children": [4], "chain": [{"path_id": 1}, {"path_id": 3}]},
            4: {"path_id": 4, "name": "Leaf", "children": [], "chain": [{"path_id": 1}, {"path_id": 3}, {"path_id": 4}]},
        }
        displayed, mode = TOOLS._preview_display_nodes(nodes, 1)
        self.assertEqual(mode, "subtree")
        self.assertEqual(
            [(node["name"], node["display_depth"]) for node in displayed],
            [("Root", 0), ("Left", 1), ("Right", 1), ("Leaf", 2)],
        )

    def test_leaf_selection_displays_its_rebased_chain(self) -> None:
        nodes = {
            1: {"path_id": 1, "name": "Root", "children": [2], "chain": [{"path_id": 1}]},
            2: {"path_id": 2, "name": "Parent", "children": [3], "chain": [{"path_id": 1}, {"path_id": 2}]},
            3: {"path_id": 3, "name": "Leaf", "children": [], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 3}]},
        }
        displayed, mode = TOOLS._preview_display_nodes(nodes, 3)
        self.assertEqual(mode, "chain")
        self.assertEqual(
            [(node["name"], node["display_depth"]) for node in displayed],
            [("Root", 0), ("Parent", 1), ("Leaf", 2)],
        )

    def test_current_tree_crop_box_rebases_to_its_union_bounds(self) -> None:
        nodes = [
            {"x": 228, "y": 325, "width": 82, "height": 82},
            {"x": 723, "y": 1000, "width": 78, "height": 103},
        ]
        self.assertEqual(
            TOOLS._preview_nodes_crop_box(nodes, 1030, 1600),
            (208, 305, 821, 1123),
        )

    def test_tree_crop_prefers_visible_content_over_full_canvas_container(self) -> None:
        nodes = [{
            "x": 0, "y": 0, "width": 1600, "height": 900,
            "content_x": 500, "content_y": 300,
            "content_width": 200, "content_height": 100,
        }]

        self.assertEqual(
            TOOLS._preview_nodes_crop_box(nodes, 1600, 900),
            (480, 280, 720, 420),
        )

    def test_leaf_chain_loads_visual_context_from_chain_root_subtree(self) -> None:
        nodes = {
            1: {"path_id": 1, "name": "Root", "children": [2, 9], "chain": [{"path_id": 1}]},
            2: {"path_id": 2, "name": "Parent", "children": [3], "chain": [{"path_id": 1}, {"path_id": 2}]},
            3: {"path_id": 3, "name": "Leaf", "children": [], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 3}]},
            9: {"path_id": 9, "name": "Background", "children": [], "chain": [{"path_id": 1}, {"path_id": 9}]},
        }
        displayed, mode = TOOLS._preview_display_nodes(nodes, 3)
        image_nodes = TOOLS._preview_image_nodes_for_display(nodes, displayed, mode)
        self.assertEqual([node["path_id"] for node in displayed], [1, 2, 3])
        self.assertEqual([node["path_id"] for node in image_nodes], [1, 2, 3, 9])

    def test_isolated_level_hides_peer_branches_at_and_below_selected_depth(self) -> None:
        nodes = {
            1: {"path_id": 1, "children": [2, 5], "chain": [{"path_id": 1}]},
            2: {"path_id": 2, "children": [3, 4], "chain": [{"path_id": 1}, {"path_id": 2}]},
            3: {"path_id": 3, "children": [6], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 3}]},
            4: {"path_id": 4, "children": [7], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 4}]},
            5: {"path_id": 5, "children": [8], "chain": [{"path_id": 1}, {"path_id": 5}]},
            6: {"path_id": 6, "children": [], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 3}, {"path_id": 6}]},
            7: {"path_id": 7, "children": [], "chain": [{"path_id": 1}, {"path_id": 2}, {"path_id": 4}, {"path_id": 7}]},
            8: {"path_id": 8, "children": [], "chain": [{"path_id": 1}, {"path_id": 5}, {"path_id": 8}]},
        }
        image_nodes = TOOLS._preview_subtree_nodes(nodes, 1)

        isolated = TOOLS._preview_image_nodes_for_isolated_level(nodes, image_nodes, 3)

        self.assertEqual([node["path_id"] for node in isolated], [1, 2, 3, 6, 5])

    def test_unresolved_component_data_does_not_crash_image_preview_check(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            scope = {
                "items": {
                    ("MonoBehaviour", 99): {
                        "data": None,
                        "path": Path(temp_dir) / "missing_component.json",
                    }
                }
            }
            game_object_data = {
                "m_Component": {
                    "Array": [
                        {"component": {"m_FileID": 0, "m_PathID": 99}}
                    ]
                }
            }

            self.assertFalse(
                TOOLS._has_unresolved_preview_sprite(scope, game_object_data)
            )

    def test_disabled_image_component_is_not_reported_as_unresolved(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {
                        "m_Enabled": 0,
                        "m_Sprite": {"m_FileID": 2, "m_PathID": 123},
                    }
                }
            }
        }
        game_object_data = {
            "m_Component": {
                "Array": [
                    {"component": {"m_FileID": 0, "m_PathID": 99}}
                ]
            }
        }

        self.assertFalse(
            TOOLS._has_unresolved_preview_sprite(scope, game_object_data)
        )

    def test_ngui_widget_bounds_use_size_scale_and_top_right_pivot(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {"mWidth": 100, "mHeight": 40, "mPivot": 2}
                }
            }
        }
        game_object_data = {
            "m_Component": {
                "Array": [{"component": {"m_FileID": 0, "m_PathID": 99}}]
            }
        }
        transform_data = {"m_LocalScale": {"x": 2.0, "y": 0.5}}

        rect = TOOLS._preview_ngui_widget_rect(
            scope,
            game_object_data,
            transform_data,
            (9.5, 19.5, 1.0, 1.0),
        )

        self.assertEqual(rect, (-190.0, 0.0, 200.0, 20.0))

    def test_ngui_uiroot_uses_manual_ui_reference_size(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {
                        "scalingStyle": 1,
                        "manualWidth": 512,
                        "manualHeight": 384,
                    }
                }
            }
        }
        game_object_data = {
            "m_Component": {"Array": [
                {"component": {"m_FileID": 0, "m_PathID": 99}}
            ]}
        }

        self.assertEqual(
            TOOLS._preview_ngui_root_reference_size(scope, game_object_data),
            (512.0, 384.0),
        )

    def test_ugui_canvas_scaler_uses_serialized_reference_resolution(self) -> None:
        scope = {
            "items": {
                ("MonoBehaviour", 99): {
                    "data": {
                        "m_UiScaleMode": 1,
                        "m_ReferenceResolution": {"x": 1280, "y": 720},
                    }
                }
            }
        }
        game_object_data = {
            "m_Component": {"Array": [
                {"component": {"m_FileID": 0, "m_PathID": 99}}
            ]}
        }

        self.assertEqual(
            TOOLS._preview_ui_root_reference_size(scope, game_object_data),
            (1280.0, 720.0),
        )

    def test_preview_renders_through_intermediate_zero_scale_canvas_and_saves_text_layers(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            texture_path = root / "atlas.png"
            Image.new("RGBA", (16, 12), (30, 120, 220, 255)).save(texture_path)

            def entry(data: dict, name: str) -> dict:
                return {"data": data, "path": root / name}

            scope = {
                "source": "fixture.bundle",
                "bundle_entry": "ui",
                "items": {
                    ("GameObject", 1): entry({
                        "m_Name": "UI",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 11}}
                        ]},
                    }, "UI.json"),
                    ("Transform", 11): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 1},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                        "m_Children": {"Array": [
                            {"m_FileID": 0, "m_PathID": 22}
                        ]},
                    }, "UI.Transform.json"),
                    ("GameObject", 2): entry({
                        "m_Name": "Canvas",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 22}},
                            {"component": {"m_FileID": 0, "m_PathID": 23}},
                        ]},
                    }, "Canvas.json"),
                    ("RectTransform", 22): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 2},
                        "m_LocalScale": {"x": 0, "y": 0, "z": 0},
                        "m_Children": {"Array": [
                            {"m_FileID": 0, "m_PathID": 33},
                            {"m_FileID": 0, "m_PathID": 44},
                            {"m_FileID": 0, "m_PathID": 55},
                        ]},
                        "m_Father": {"m_FileID": 0, "m_PathID": 11},
                        "m_AnchorMin": {"x": 0, "y": 0},
                        "m_AnchorMax": {"x": 0, "y": 0},
                        "m_AnchoredPosition": {"x": 0, "y": 0},
                        "m_SizeDelta": {"x": 0, "y": 0},
                        "m_Pivot": {"x": 0, "y": 0},
                    }, "Canvas.RectTransform.json"),
                    ("MonoBehaviour", 23): entry({
                        "m_UiScaleMode": 1,
                        "m_ReferenceResolution": {"x": 800, "y": 600},
                    }, "CanvasScaler.json"),
                    ("GameObject", 3): entry({
                        "m_Name": "Image",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 33}},
                            {"component": {"m_FileID": 0, "m_PathID": 34}},
                        ]},
                    }, "Image.json"),
                    ("RectTransform", 33): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 3},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                        "m_Children": {"Array": []},
                        "m_Father": {"m_FileID": 0, "m_PathID": 22},
                        "m_AnchorMin": {"x": 0.5, "y": 0.5},
                        "m_AnchorMax": {"x": 0.5, "y": 0.5},
                        "m_AnchoredPosition": {"x": 0, "y": 0},
                        "m_SizeDelta": {"x": 100, "y": 50},
                        "m_Pivot": {"x": 0.5, "y": 0.5},
                    }, "Image.RectTransform.json"),
                    ("MonoBehaviour", 34): entry({
                        "m_Enabled": 1,
                        "m_Sprite": {"m_FileID": 0, "m_PathID": 40},
                    }, "Image.Component.json"),
                    ("GameObject", 4): entry({
                        "m_Name": "Label",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 44}},
                            {"component": {"m_FileID": 0, "m_PathID": 45}},
                        ]},
                    }, "Label.json"),
                    ("RectTransform", 44): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 4},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                        "m_Children": {"Array": []},
                        "m_Father": {"m_FileID": 0, "m_PathID": 22},
                        "m_AnchorMin": {"x": 0.5, "y": 0.5},
                        "m_AnchorMax": {"x": 0.5, "y": 0.5},
                        "m_AnchoredPosition": {"x": 0, "y": -80},
                        "m_SizeDelta": {"x": 200, "y": 40},
                        "m_Pivot": {"x": 0.5, "y": 0.5},
                    }, "Label.RectTransform.json"),
                    ("MonoBehaviour", 45): entry({
                        "m_Enabled": 1,
                        "m_Text": "",
                        "m_text": "Hello TMP",
                        "m_fontSize": 20,
                        "m_fontColor": {"r": 1, "g": 1, "b": 1, "a": 1},
                        "m_enableAutoSizing": 1,
                        "m_fontSizeMin": 8,
                        "m_HorizontalAlignment": 2,
                        "m_VerticalAlignment": 512,
                    }, "Label.Component.json"),
                    ("GameObject", 5): entry({
                        "m_Name": "Runtime Fill",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 55}},
                            {"component": {"m_FileID": 0, "m_PathID": 56}},
                        ]},
                    }, "Fill.json"),
                    ("RectTransform", 55): entry({
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 5},
                        "m_LocalScale": {"x": 1, "y": 1, "z": 1},
                        "m_Children": {"Array": []},
                        "m_Father": {"m_FileID": 0, "m_PathID": 22},
                        "m_AnchorMin": {"x": 0, "y": 0},
                        "m_AnchorMax": {"x": 0, "y": 0},
                        "m_AnchoredPosition": {"x": 20, "y": 20},
                        "m_SizeDelta": {"x": 0, "y": 0},
                        "m_Pivot": {"x": 0.5, "y": 0.5},
                    }, "Fill.RectTransform.json"),
                    ("MonoBehaviour", 56): entry({
                        "m_Enabled": 1,
                        "m_Sprite": {"m_FileID": 0, "m_PathID": 40},
                    }, "Fill.Component.json"),
                    ("Sprite", 40): entry({
                        "m_Name": "Fixture Sprite",
                        "m_RD": {
                            "texture": {"m_FileID": 0, "m_PathID": 50},
                            "textureRect": {"x": 0, "y": 0, "width": 16, "height": 12},
                        },
                    }, "Sprite.json"),
                    ("Texture2D", 50): {
                        "data": {"m_Name": "Fixture Atlas"},
                        "path": texture_path,
                    },
                },
            }
            scope["pointer_scopes"] = {0: scope}
            scopes = {("fixture", "ui"): scope}
            match = {
                "source": "fixture.bundle",
                "bundle_entry": "ui",
                "chain": [{"name": "UI", "path_id": 1}],
            }

            with patch.object(TOOLS, "DEFAULT_OBJECT_PREVIEW_ROOT", root / "preview"):
                target = TOOLS._create_object_hierarchy_preview(match, 0, scopes)
                metadata = json.loads(
                    target.with_suffix(".regions.json").read_text(encoding="utf-8")
                )

            nodes = {int(node["path_id"]): node for node in metadata["tree_nodes"]}
            layer_ids = [int(layer["path_id"]) for layer in metadata["image_layers"]]
            self.assertGreater(nodes[2]["width"], 100)
            self.assertGreater(nodes[3]["width"], 50)
            self.assertGreater(nodes[5]["width"], 2)
            self.assertIn(4, layer_ids)
            self.assertEqual(nodes[4]["texts"], ["Hello TMP"])
            self.assertGreaterEqual(layer_ids.count(3), 1)

    def test_dynamic_preview_collects_full_transform_subtree(self) -> None:
        scope = {
            "items": {
                ("GameObject", 1): {
                    "data": {
                        "m_Name": "Panel",
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 11}}
                        ]},
                    }
                },
                ("Transform", 11): {
                    "data": {
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 1},
                        "m_Children": {"Array": [
                            {"m_FileID": 0, "m_PathID": 22}
                        ]},
                    }
                },
                ("GameObject", 2): {
                    "data": {
                        "m_Name": "InactiveTemplate",
                        "m_IsActive": False,
                        "m_Component": {"Array": [
                            {"component": {"m_FileID": 0, "m_PathID": 22}}
                        ]},
                    }
                },
                ("Transform", 22): {
                    "data": {
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 2},
                        "m_Children": {"Array": []},
                    }
                },
            }
        }

        self.assertEqual(
            TOOLS._game_object_subtree_path_ids(scope, 1),
            [1, 2],
        )

if __name__ == "__main__":
    unittest.main()
