from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_quick_config():
    script = Path(__file__).resolve().parents[1] / "快速配置.py"
    spec = importlib.util.spec_from_file_location("quick_config_for_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载快速配置.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


QUICK = _load_quick_config()


class QuickConfigTests(unittest.TestCase):
    def test_default_template_points_directly_to_config_json(self) -> None:
        with patch.object(sys, "argv", ["快速配置.py"]):
            args = QUICK._parse_args()

        self.assertEqual(args.config.resolve(), QUICK.DEFAULT_CONFIG_PATH.resolve())
        self.assertEqual(args.template.resolve(), QUICK.DEFAULT_CONFIG_PATH.resolve())

    def test_missing_default_config_explains_manual_rename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "config.json"
            with patch.object(QUICK, "DEFAULT_CONFIG_PATH", missing):
                with self.assertRaisesRegex(FileNotFoundError, "重命名为 config.json"):
                    QUICK._read_json(missing)

    def test_choose_default_python_preserves_valid_configured_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "configured-python.exe"
            configured.touch()
            with (
                patch.object(QUICK, "_probe_python", return_value=(True, "ok")) as probe,
                patch.object(QUICK, "_python_environment_candidates", return_value=[]),
            ):
                result = QUICK.choose_default_python({"python_executable": str(configured)})

            self.assertEqual(Path(result), configured.resolve())
            probe.assert_called_once_with(configured.resolve())

    def test_choose_default_python_skips_broken_config_and_finds_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            broken = root / "broken-python.exe"
            discovered = root / ".venv/Scripts/python.exe"
            broken.touch()
            discovered.parent.mkdir(parents=True)
            discovered.touch()

            def probe(path: Path) -> tuple[bool, str]:
                return (path == discovered.resolve(), "ok" if path == discovered.resolve() else "broken")

            with (
                patch.object(QUICK, "_probe_python", side_effect=probe),
                patch.object(QUICK, "_python_environment_candidates", return_value=[discovered]),
            ):
                result = QUICK.choose_default_python(
                    {"python_executable": str(broken)},
                    root=root,
                )

            self.assertEqual(Path(result), discovered.resolve())

    def test_explicit_python_has_priority_and_is_validated_later(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            explicit = root / "custom-python.exe"
            with patch.object(QUICK, "_probe_python") as probe:
                result = QUICK.choose_default_python({}, str(explicit), root=root)

            self.assertEqual(Path(result), explicit.resolve())
            probe.assert_not_called()

    def test_python_probe_reports_missing_project_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "python.exe"
            executable.touch()
            payload = {
                "version": [3, 11, 0],
                "errors": {"spookyhash": "ModuleNotFoundError"},
            }
            completed = SimpleNamespace(
                returncode=0,
                stdout=QUICK.PYTHON_PROBE_PREFIX + json.dumps(payload) + "\n",
                stderr="",
            )
            with patch.object(QUICK.subprocess, "run", return_value=completed):
                ok, detail = QUICK._probe_python(executable)

            self.assertFalse(ok)
            self.assertIn("spookyhash", detail)

    def test_python_environment_prompt_allows_empty_value(self) -> None:
        with patch("builtins.input", return_value=""):
            result = QUICK._prompt_python_environment()

        self.assertEqual(result, "")

    def test_python_environment_directory_resolves_scripts_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".venv"
            executable = environment / "Scripts" / "python.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()

            with (
                patch("builtins.input", return_value=str(environment)),
                patch.object(QUICK, "_probe_python", return_value=(True, "ok")),
            ):
                result = QUICK._prompt_python_environment()

            self.assertEqual(Path(result), executable.resolve())

    def test_blank_python_configuration_is_valid(self) -> None:
        with patch.object(QUICK, "_probe_python") as probe:
            checks = QUICK.validate_config({"python_executable": ""})

        python_check = next(
            check for check in checks if check.label == "虚拟 Python 环境"
        )
        self.assertTrue(python_check.ok)
        self.assertIn("启动器当前 Python", python_check.detail)
        probe.assert_not_called()

    def test_config_validation_does_not_probe_fixed_resource_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            checks = QUICK.validate_config(
                {
                    "project_root_dir": temporary,
                    "project_name": "",
                    "python_executable": "",
                    "resource_source_subpath": "missing/Data",
                    "resource_managed_subpath": "missing/Managed",
                    "catalog_source_subpath": "missing/catalog.json",
                    "stringliteral_json_subpath": "missing/stringliteral.json",
                }
            )

        labels = {check.label for check in checks}
        self.assertNotIn("assets/bin/Data", labels)
        self.assertNotIn("Managed DLL 目录", labels)
        self.assertNotIn("Addressables catalog", labels)
        self.assertNotIn("IL2CPP stringliteral", labels)

    def test_sanitize_template_removes_machine_values_and_secrets(self) -> None:
        source = {
            "project_root_dir": "D:/private/projects",
            "project_name": "secret-game",
            "python_executable": "D:/private/python.exe",
            "unity_exe": "D:/private/Unity.exe",
            "ttf_template_path": "D:/private/font.ttf",
            "baidu_appid": "appid",
            "baidu_appkey": "secret",
            "ai_translation_api_key": "secret",
            "ai_field_review_api_key": "secret",
            "google_proxy_http": "http://127.0.0.1:1080",
            "custom_setting": 7,
        }

        result = QUICK.sanitize_template(source)

        self.assertEqual(result["project_root_dir"], "")
        self.assertEqual(result["project_name"], "")
        self.assertEqual(result["python_executable"], "")
        self.assertEqual(result["unity_exe"], "auto")
        self.assertEqual(result["ttf_template_path"], "templates/fzkt.ttf")
        self.assertFalse(result["enable_ai_translation"])
        self.assertFalse(result["enable_ai_field_review"])
        self.assertEqual(result["ai_translation_transport"], "codex_cli")
        self.assertEqual(result["ai_translation_codex_model"], "gpt-5.3-codex-spark")
        self.assertEqual(result["ai_translation_codex_reasoning_effort"], "low")
        self.assertEqual(result["ai_translation_model"], "")
        self.assertFalse(result["include_old_sdf_template_chars"])
        self.assertTrue(result["protect_i2_tmp_fonts_from_replacement"])
        self.assertTrue(all(not result[field] for field in QUICK.SECRET_FIELDS))
        self.assertEqual(result["google_proxy_http"], "")
        self.assertEqual(result["custom_setting"], 7)

    def test_empty_project_name_uses_project_root_as_project_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary).resolve()

            result = QUICK._configured_project_dir(
                {
                    "project_root_dir": str(project),
                    "project_name": "",
                }
            )

            self.assertEqual(result, project)

    def test_project_layout_uses_fixed_relative_paths_without_scanning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "demo"

            layout = QUICK.detect_project_layout(
                project,
                {"resource_source_subpath": "should/not/be/used"},
                assume_yes=False,
            )

            self.assertEqual(layout.resource_source_subpath, "game-name/game/assets/bin/Data")
            self.assertEqual(layout.resource_managed_subpath, "game-name/game/assets/bin/Data/Managed")
            self.assertEqual(layout.catalog_source_subpath, "game-name/game/assets/aa/catalog.json")
            self.assertEqual(layout.stringliteral_json_subpath, "game-name/bak/64/stringliteral.json")

    def test_write_config_creates_timestamped_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"old": 1}), encoding="utf-8")

            backup = QUICK.write_config_with_backup(path, {"new": 2})

            self.assertIsNotNone(backup)
            self.assertTrue(backup.is_file())
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), {"old": 1})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"new": 2})
            self.assertNotIn(b"\r\n", path.read_bytes())

    def test_core_updates_store_complete_project_dir_with_empty_name(self) -> None:
        layout = QUICK.ProjectLayout("a/Data", "a/Data/Managed", "a/catalog.json", "a/strings.json")
        project = Path("D:/projects/demo")

        result = QUICK.build_core_updates(
            project,
            layout,
            python_executable="python.exe",
            unity_exe="auto",
        )

        self.assertEqual(result["project_name"], "")
        self.assertTrue(
            result["project_root_dir"].replace("\\", "/").endswith("/projects/demo")
        )
        self.assertEqual(result["resource_source_subpath"], "a/Data")
        self.assertEqual(result["ttf_template_path"], "templates/fzkt.ttf")

    def test_translation_prompt_configures_fixed_codex_model_and_effort(self) -> None:
        config = {"enable_ai_translation": False}

        with patch("builtins.input", side_effect=["1", "medium"]):
            QUICK._prompt_translation(config)

        self.assertTrue(config["enable_ai_translation"])
        self.assertEqual(config["ai_translation_transport"], "codex_cli")
        self.assertEqual(config["ai_translation_codex_model"], "gpt-5.3-codex-spark")
        self.assertEqual(config["ai_translation_codex_reasoning_effort"], "medium")

    def test_translation_prompt_configures_custom_http_api(self) -> None:
        config = {"enable_ai_translation": False}

        with (
            patch("builtins.input", side_effect=["2", "https://example.test/v1", "custom-model"]),
            patch.object(QUICK.getpass, "getpass", return_value="secret"),
        ):
            QUICK._prompt_translation(config)

        self.assertTrue(config["enable_ai_translation"])
        self.assertEqual(config["ai_translation_transport"], "http")
        self.assertEqual(config["ai_translation_base_url"], "https://example.test/v1")
        self.assertEqual(config["ai_translation_model"], "custom-model")
        self.assertEqual(config["ai_translation_api_key"], "secret")


if __name__ == "__main__":
    unittest.main()
