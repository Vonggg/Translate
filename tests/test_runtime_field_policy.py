from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import resource_menu

from pipeline.runtime_field_policy import (
    restore_protected_runtime_fields,
    runtime_field_exclusion_reason,
)
from pipeline.translation import _scan_one_translation_json, apply_translations_to_json
from support.config import load_config


class RuntimeFieldPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.input_actions = {
            "m_ActionMaps": {
                "Array": [
                    {
                        "m_Name": "Player",
                        "m_Actions": {
                            "Array": [
                                {
                                    "m_Name": "Move",
                                    "m_ExpectedControlType": "Button",
                                }
                            ]
                        },
                        "m_Bindings": {
                            "Array": [
                                {
                                    "m_Action": "Move",
                                    "m_Path": "<Keyboard>/w",
                                }
                            ]
                        },
                    }
                ]
            },
            "m_Label": "Move",
        }

    def test_input_system_paths_are_excluded_before_ai(self) -> None:
        cases = {
            "m_ActionMaps.Array[0].m_Actions.Array[0].m_Name": "Move",
            "m_ActionMaps.Array[0].m_Actions.Array[0].m_ExpectedControlType": "Button",
            "m_ActionMaps.Array[0].m_Bindings.Array[0].m_Action": "Move",
            "m_ActionMaps.Array[0].m_Bindings.Array[0].m_Path": "<Keyboard>/w",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.assertIsNotNone(
                    runtime_field_exclusion_reason(self.input_actions, field, value)
                )

    def test_same_text_outside_runtime_structure_is_not_excluded(self) -> None:
        self.assertIsNone(
            runtime_field_exclusion_reason(
                self.input_actions,
                "m_Label",
                "Move",
            )
        )

    def test_translation_writer_preserves_runtime_fields(self) -> None:
        translated = apply_translations_to_json(
            self.input_actions,
            load_config(),
            {"Move": "移动", "Button": "按钮"},
        )
        action_map = translated["m_ActionMaps"]["Array"][0]
        self.assertEqual(action_map["m_Actions"]["Array"][0]["m_Name"], "Move")
        self.assertEqual(
            action_map["m_Actions"]["Array"][0]["m_ExpectedControlType"],
            "Button",
        )
        self.assertEqual(action_map["m_Bindings"]["Array"][0]["m_Action"], "Move")
        self.assertEqual(translated["m_Label"], "移动")

    def test_runtime_text_format_is_preserved(self) -> None:
        data = {
            "_textFormat": r"mm\:ss",
            "m_text": "Time left",
        }
        translated = apply_translations_to_json(
            data,
            load_config(),
            {
                r"mm\:ss": "mm:ss",
                "Time left": "剩余时间",
            },
        )

        self.assertEqual(r"mm\:ss", translated["_textFormat"])
        self.assertEqual("剩余时间", translated["m_text"])
        self.assertEqual(
            "运行时格式串",
            runtime_field_exclusion_reason(data, "_textFormat", r"mm\:ss"),
        )

    def test_import_guard_restores_stale_runtime_translation(self) -> None:
        original = {
            "_textFormat": r"mm\:ss",
            "m_text": "Time left",
            "nested": {"m_RegexValue": r"^room_[0-9]+$"},
        }
        stale_output = {
            "_textFormat": "mm:ss",
            "m_text": "剩余时间",
            "nested": {"m_RegexValue": "房间"},
        }

        repaired, restored = restore_protected_runtime_fields(original, stale_output)

        self.assertEqual(r"mm\:ss", repaired["_textFormat"])
        self.assertEqual(r"^room_[0-9]+$", repaired["nested"]["m_RegexValue"])
        self.assertEqual("剩余时间", repaired["m_text"])
        self.assertEqual(
            {
                ("_textFormat", "运行时格式串"),
                ("nested.m_RegexValue", "正则表达式配置"),
            },
            set(restored),
        )

    def test_import_overlay_repairs_runtime_fields_but_keeps_visible_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            input_root = workspace / "input"
            text_root = workspace / "output" / "Text"
            relative = Path("bundle") / "MonoBehaviour" / "asset.json"
            original_path = input_root / relative
            translated_path = text_root / relative
            original_path.parent.mkdir(parents=True)
            translated_path.parent.mkdir(parents=True)
            original_path.write_text(
                json.dumps(
                    {"_textFormat": r"mm\:ss", "m_text": "Time left"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            translated_path.write_text(
                json.dumps(
                    {"_textFormat": "mm:ss", "m_text": "剩余时间"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                workspace_root=workspace,
                root_dir=Path(temp_dir),
                resource_input_root=input_root,
                stage_dir=workspace / "output",
            )

            overlay_root = resource_menu.build_import_overlay(cfg, {"text"})

            self.assertIsNotNone(overlay_root)
            repaired = json.loads((overlay_root / relative).read_text(encoding="utf-8"))
            self.assertEqual(r"mm\:ss", repaired["_textFormat"])
            self.assertEqual("剩余时间", repaired["m_text"])

    def test_manual_object_edit_wins_after_all_static_text_postprocessing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "workspace"
            input_root = workspace / "input"
            text_root = workspace / "output" / "Text"
            object_root = workspace / "output" / "Object" / "ToImport"
            relative = Path("bin") / "Data" / "level2" / "MonoBehaviour" / "114_7_7.json"

            for root, payload in (
                (input_root, {"_textFormat": r"mm\:ss", "m_Text": "ACHIEVEMENTS"}),
                (text_root, {"_textFormat": "错误格式", "m_Text": "自动汉化"}),
                (object_root, {"_textFormat": "手动格式", "m_Text": "看广告获取10钻石"}),
            ):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            cfg = SimpleNamespace(
                workspace_root=workspace,
                root_dir=Path(temp_dir),
                resource_input_root=input_root,
                stage_dir=workspace / "output",
                object_import_dir=object_root,
            )

            overlay_root = resource_menu.build_import_overlay(cfg, {"text", "object"})

            self.assertIsNotNone(overlay_root)
            merged = json.loads((overlay_root / relative).read_text(encoding="utf-8"))
            self.assertEqual("手动格式", merged["_textFormat"])
            self.assertEqual("看广告获取10钻石", merged["m_Text"])

    def test_legacy_input_and_startup_lookup_names_are_preserved(self) -> None:
        cases = [
            ({"horizontalAxisName": "Horizontal"}, "horizontalAxisName", "Horizontal"),
            ({"m_SubmitButton": "Submit"}, "m_SubmitButton", "Submit"),
            ({"desiredTag": "HeroPoint"}, "desiredTag", "HeroPoint"),
            ({"jelasticKey": "TermsOfService"}, "jelasticKey", "TermsOfService"),
            ({"call_1": "Meow"}, "call_1", "Meow"),
            ({"MapName": "Builder", "MyMap": 1}, "MapName", "Builder"),
            (
                {"loading": {"m_FileID": 0, "m_PathID": 0}, "level": "Garage"},
                "level",
                "Garage",
            ),
        ]
        for data, field, value in cases:
            with self.subTest(field=field):
                self.assertIsNotNone(runtime_field_exclusion_reason(data, field, value))
                translated = apply_translations_to_json(data, load_config(), {value: "不应写入"})
                self.assertEqual(value, translated[field])

    def test_timeline_audio_animation_and_menu_parameter_paths_are_preserved(self) -> None:
        data = {
            "m_Clips": {"Array": [{"m_DisplayName": "Active"}]},
            "tracks": {
                "Array": [
                    {
                        "trackName": "DefaultMenu",
                        "clip": {"m_FileID": 0, "m_PathID": 123},
                    }
                ]
            },
            "animationsNames": {"Array": ["fly"]},
            "Params": {"Array": ["BattleRoyal"]},
            "PrefabPath": "UI/Menu/ShopPlayer",
            "MenuType": 0,
            "onClick": {"Array": []},
            "m_Label": "Active",
        }
        translated = apply_translations_to_json(
            data,
            load_config(),
            {
                "Active": "激活",
                "DefaultMenu": "默认菜单",
                "fly": "飞行",
                "BattleRoyal": "大逃杀",
            },
        )
        self.assertEqual("Active", translated["m_Clips"]["Array"][0]["m_DisplayName"])
        self.assertEqual("DefaultMenu", translated["tracks"]["Array"][0]["trackName"])
        self.assertEqual("fly", translated["animationsNames"]["Array"][0])
        self.assertEqual("BattleRoyal", translated["Params"]["Array"][0])
        self.assertEqual("激活", translated["m_Label"])

    def test_generic_params_and_tracks_are_not_excluded_without_runtime_structure(self) -> None:
        data = {
            "Params": {"Array": ["Visible parameter"]},
            "tracks": {"Array": [{"trackName": "Visible track"}]},
        }
        self.assertIsNone(
            runtime_field_exclusion_reason(data, "Params.Array[0]", "Visible parameter")
        )
        self.assertIsNone(
            runtime_field_exclusion_reason(data, "tracks.Array[0].trackName", "Visible track")
        )

    def test_scanner_excludes_runtime_fields_before_ai_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_root = Path(temp_dir) / "input"
            json_path = input_root / "bundle" / "MonoBehaviour" / "DefaultInputActions_1.json"
            json_path.parent.mkdir(parents=True)
            json_path.write_text(json.dumps(self.input_actions), encoding="utf-8")
            cfg = SimpleNamespace(
                resource_input_root=input_root,
                ignore_text=[],
                font_keys=[],
                string_field_blacklist=["m_Script", "m_Name"],
                enable_ai_field_review=True,
                text_keys=[],
            )

            result = _scan_one_translation_json(cfg, json_path, {}, {}, {})

            self.assertIsNone(result["error"])
            self.assertEqual(
                [(record.field, record.source_text) for record in result["records"]],
                [("m_Label", "Move")],
            )
            self.assertEqual(set(result["string_field_stats"]), {"m_Label"})
            self.assertGreater(sum(result["runtime_exclusions"].values()), 0)

    def test_prefab_mapping_identifiers_are_excluded_by_structure(self) -> None:
        skin_config = {
            "m_Name": "UnrelatedConfigName",
            "entries": {
                "Array": [
                    {
                        "Id": "Hostage_nud_fem_1",
                        "_name": "Hostage",
                        "_additionalInfo": "hostage_variant",
                        "DamageSound": "EnemyDamage",
                        "Prefab": {"_path": "Assets/Characters/Hostage.prefab"},
                    },
                    {
                        "Id": "Zombie Armored",
                        "_name": "Armored",
                        "_defaultPrefab": {"m_FileID": 0, "m_PathID": -123},
                    },
                    {
                        "Id": "EnemyDummy",
                        "DeathSound": "EnemyDeath",
                        "Prefab": {"_path": ""},
                    },
                ]
            },
        }
        cases = {
            "entries.Array[0].Id": "Hostage_nud_fem_1",
            "entries.Array[0]._name": "Hostage",
            "entries.Array[0]._additionalInfo": "hostage_variant",
            "entries.Array[0].DamageSound": "EnemyDamage",
            "entries.Array[1].Id": "Zombie Armored",
            "entries.Array[2].Id": "EnemyDummy",
            "entries.Array[2].DeathSound": "EnemyDeath",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                self.assertEqual(
                    runtime_field_exclusion_reason(skin_config, field, value),
                    "Prefab 查找表运行时标识",
                )

    def test_prefab_mapping_rule_does_not_globally_block_names(self) -> None:
        ordinary_data = {
            "items": {
                "Array": [
                    {"_name": "Visible item name", "description": "Visible description"}
                ]
            }
        }
        self.assertIsNone(
            runtime_field_exclusion_reason(
                ordinary_data,
                "items.Array[0]._name",
                "Visible item name",
            )
        )

    def test_prefab_mapping_fields_are_preserved_when_writing(self) -> None:
        skin_config = {
            "skins": {
                "Array": [
                    {
                        "_name": "Zombie Armored",
                        "_additionalInfo": "armored_zombie",
                        "_skinPrefab": {"m_FileID": 0, "m_PathID": 42},
                        "label": "Armored enemy",
                    }
                ]
            }
        }
        translated = apply_translations_to_json(
            skin_config,
            load_config(),
            {
                "Zombie Armored": "装甲僵尸",
                "armored_zombie": "装甲僵尸标识",
                "Armored enemy": "装甲敌人",
            },
        )
        entry = translated["skins"]["Array"][0]
        self.assertEqual(entry["_name"], "Zombie Armored")
        self.assertEqual(entry["_additionalInfo"], "armored_zombie")
        self.assertEqual(entry["label"], "装甲敌人")

    def test_serialized_resource_path_is_excluded(self) -> None:
        self.assertEqual(
            runtime_field_exclusion_reason(
                {},
                "skins.Array[0].Prefab._path",
                "Assets/Characters/Hero.prefab",
            ),
            "运行时资源路径",
        )

    def test_prefab_lookup_identifiers_are_excluded_by_structure(self) -> None:
        skin_lookup = {
            "_skins": {
                "Array": [
                    {
                        "_name": "default",
                        "_additionalInfo": "player",
                        "_defaultPrefab": {"m_FileID": 0, "m_PathID": 123},
                    }
                ]
            },
            "_simpleSkins": {
                "Array": [
                    {
                        "Id": "Zombie Armored",
                        "Prefab": {"_path": "Assets/Characters/Zombie Armored.prefab"},
                    },
                    {
                        "Id": "EnemyDummy",
                        "Prefab": {"_path": ""},
                    }
                ]
            },
            "m_Label": "Zombie Armored",
        }

        for field, value in {
            "_skins.Array[0]._name": "default",
            "_skins.Array[0]._additionalInfo": "player",
            "_simpleSkins.Array[0].Id": "Zombie Armored",
            "_simpleSkins.Array[1].Id": "EnemyDummy",
        }.items():
            with self.subTest(field=field):
                self.assertEqual(
                    runtime_field_exclusion_reason(skin_lookup, field, value),
                    "Prefab 查找表运行时标识",
                )

        self.assertIsNone(
            runtime_field_exclusion_reason(skin_lookup, "m_Label", "Zombie Armored")
        )

    def test_prefab_like_field_without_reference_is_not_excluded(self) -> None:
        display_cards = {
            "cards": {
                "Array": [
                    {
                        "_name": "Starter Pack",
                        "prefabCaption": "Preview",
                    }
                ]
            }
        }
        self.assertIsNone(
            runtime_field_exclusion_reason(
                display_cards,
                "cards.Array[0]._name",
                "Starter Pack",
            )
        )

    def test_translation_writer_preserves_prefab_lookup_identifiers(self) -> None:
        skin_lookup = {
            "_skins": {
                "Array": [
                    {
                        "_name": "default",
                        "_additionalInfo": "player",
                        "_defaultPrefab": {"m_FileID": 0, "m_PathID": -123},
                    }
                ]
            },
            "m_Label": "default",
        }
        translated = apply_translations_to_json(
            skin_lookup,
            load_config(),
            {"default": "默认", "player": "玩家"},
        )
        self.assertEqual(translated["_skins"]["Array"][0]["_name"], "default")
        self.assertEqual(
            translated["_skins"]["Array"][0]["_additionalInfo"],
            "player",
        )
        self.assertEqual(translated["m_Label"], "默认")


if __name__ == "__main__":
    unittest.main()
