from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import support.config as config_module


_MISSING = object()


class ProjectWorkspaceTests(unittest.TestCase):
    def _load(self, root: Path, project_name: object = _MISSING, **updates: object):
        payload: dict[str, object] = {
            "project_root_dir": str(root / "projects"),
            "unity_exe": "auto",
        }
        if project_name is not _MISSING:
            payload["project_name"] = project_name
        payload.update(updates)
        config_path = root / "config.json"
        config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with (
            patch.object(config_module, "_default_home", return_value=root),
            patch.object(
                config_module,
                "_resolve_unity_exe",
                return_value=root / "Unity.exe",
            ),
        ):
            return config_module.load_config(config_path, quiet=True)

    def test_switching_only_project_name_switches_all_workspace_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()

            first = self._load(root, "  GameA  ")
            second = self._load(root, "GameB")

            self.assertEqual(first.project_name, "GameA")
            self.assertEqual(first.project_dir, root / "projects" / "GameA")
            self.assertEqual(first.workspace_root, root / "workspaceGameA")
            self.assertEqual(first.sample_root, root / "样本" / "workspaceGameA")
            self.assertEqual(second.workspace_root, root / "workspaceGameB")
            self.assertEqual(second.sample_root, root / "样本" / "workspaceGameB")

            expected_relatives = {
                "resource_input_root": "input",
                "resource_staging_root": "input_sources",
                "log_dir": "logs",
                "result_dir": "output",
                "record_dir": "records",
                "import_overlay_dir": "output/Font/SDF/ToImport",
                "image_import_dir": "output/Image/ToImport",
                "object_import_dir": "output/Object/ToImport",
                "ttf_old_dir": "output/Font/TTF/source",
                "ttf_new_dir": "output/Font/TTF/ToImport",
                "ngui_generated_dir": "output/Font/NGUI/generated",
                "ngui_import_dir": "output/Font/NGUI/ToImport",
            }
            for attribute, relative in expected_relatives.items():
                self.assertEqual(
                    getattr(first, attribute),
                    first.workspace_root / Path(relative),
                    attribute,
                )
                self.assertEqual(
                    getattr(second, attribute),
                    second.workspace_root / Path(relative),
                    attribute,
                )

    def test_workbench_automatic_worker_budget_applies_without_manual_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with patch.dict(
                os.environ,
                {
                    "TRANSLATE_AUTO_MAX_SCAN_WORKERS": "4",
                    "TRANSLATE_AUTO_MAX_EXPORT_WORKERS": "2",
                    "TRANSLATE_AUTO_MAX_TRANSLATE_WORKERS": "2",
                    "TRANSLATE_AUTO_MAX_IMPORT_WORKERS": "2",
                },
                clear=False,
            ):
                cfg = self._load(root, "GameA")

            self.assertEqual(4, cfg.max_scan_workers)
            self.assertEqual(2, cfg.max_export_workers)
            self.assertEqual(2, cfg.max_translate_workers)
            self.assertEqual(2, cfg.max_import_workers)

    def test_explicit_worker_limit_remains_stricter_than_automatic_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            with patch.dict(
                os.environ,
                {"TRANSLATE_AUTO_MAX_SCAN_WORKERS": "8"},
                clear=False,
            ):
                cfg = self._load(root, "GameA", max_scan_workers=1)

            self.assertEqual(1, cfg.max_scan_workers)

    def test_empty_project_name_keeps_legacy_workspace_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()

            for value in (_MISSING, None, "", "   "):
                with self.subTest(project_name=value):
                    cfg = self._load(root, value)

                    self.assertEqual(cfg.project_name, "")
                    self.assertEqual(cfg.project_dir, root / "projects")
                    self.assertEqual(cfg.workspace_root, root / "workspace")
                    self.assertEqual(cfg.sample_root, root / "样本" / "workspace")
                    self.assertEqual(cfg.resource_input_root, root / "workspace" / "input")
                    self.assertEqual(cfg.result_dir, root / "workspace" / "output")

    def test_absolute_and_non_workspace_custom_paths_are_not_rewritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            external_input = root / "external" / "input"

            cfg = self._load(
                root,
                "GameA",
                resource_input_root=str(external_input),
                result_dir="custom/output",
            )

            self.assertEqual(cfg.resource_input_root, external_input)
            self.assertEqual(cfg.result_dir, root / "custom" / "output")

    def test_environment_selects_config_for_the_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            selected = root / "configs" / "GameA.json"
            selected.parent.mkdir(parents=True)
            selected.write_text(
                json.dumps({"project_name": "GameA", "unity_exe": "auto"}),
                encoding="utf-8",
            )

            with (
                patch.dict(os.environ, {config_module.ACTIVE_CONFIG_PATH_ENV: str(selected)}),
                patch.object(config_module, "_default_home", return_value=root),
                patch.object(config_module, "_resolve_unity_exe", return_value=root / "Unity.exe"),
            ):
                cfg = config_module.load_config(quiet=True)

            self.assertEqual(cfg.project_name, "GameA")
            self.assertEqual(config_module.resolve_config_path(selected), selected)

    def test_explicit_config_path_overrides_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            env_config = root / "env.json"
            explicit_config = root / "explicit.json"
            env_config.write_text(json.dumps({"project_name": "Env"}), encoding="utf-8")
            explicit_config.write_text(json.dumps({"project_name": "Explicit"}), encoding="utf-8")

            with (
                patch.dict(os.environ, {config_module.ACTIVE_CONFIG_PATH_ENV: str(env_config)}),
                patch.object(config_module, "_default_home", return_value=root),
                patch.object(config_module, "_resolve_unity_exe", return_value=root / "Unity.exe"),
            ):
                cfg = config_module.load_config(explicit_config, quiet=True)

            self.assertEqual(cfg.project_name, "Explicit")


if __name__ == "__main__":
    unittest.main()
