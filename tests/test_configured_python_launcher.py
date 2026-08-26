from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_launcher():
    script = Path(__file__).resolve().parents[1] / "run_with_config_python.py"
    spec = importlib.util.spec_from_file_location("configured_python_launcher_for_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 run_with_config_python.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LAUNCHER = _load_launcher()


def _args(script: str, *script_args: str) -> argparse.Namespace:
    return argparse.Namespace(
        config=LAUNCHER.DEFAULT_CONFIG_PATH,
        channel_package_dir_name=None,
        script=script,
        script_args=list(script_args),
        print_python=False,
    )


class ConfiguredPythonLauncherTests(unittest.TestCase):
    def test_blank_python_configuration_uses_launcher_interpreter(self) -> None:
        self.assertEqual(
            LAUNCHER.resolve_python({"python_executable": ""}),
            Path(sys.executable),
        )

    def test_quick_config_always_uses_bootstrap_interpreter(self) -> None:
        completed = SimpleNamespace(returncode=0)
        with (
            patch.object(LAUNCHER, "parse_args", return_value=_args(str(LAUNCHER.QUICK_CONFIG_SCRIPT), "--help")),
            patch.object(LAUNCHER, "load_config", side_effect=AssertionError("不应读取配置")),
            patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(Path(command[0]), Path(sys.executable))
        self.assertEqual(Path(command[2]), LAUNCHER.QUICK_CONFIG_SCRIPT)
        self.assertEqual(command[3:5], ["--config", str(LAUNCHER.DEFAULT_CONFIG_PATH)])

    def test_regular_script_uses_configured_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "python.exe"
            configured.touch()
            completed = SimpleNamespace(returncode=0)
            with (
                patch.object(LAUNCHER, "parse_args", return_value=_args("main.py")),
                patch.object(LAUNCHER, "load_config", return_value={"python_executable": str(configured)}),
                patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
            ):
                result = LAUNCHER.main()

            self.assertEqual(result, 0)
            self.assertEqual(Path(run.call_args.args[0][0]), configured)
            self.assertEqual(
                run.call_args.kwargs["env"][LAUNCHER.ACTIVE_CONFIG_PATH_ENV],
                str(LAUNCHER.DEFAULT_CONFIG_PATH),
            )

    def test_external_same_named_script_does_not_bypass_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            external_script = root / "快速配置.py"
            configured = root / "configured-python.exe"
            external_script.touch()
            configured.touch()
            completed = SimpleNamespace(returncode=0)
            with (
                patch.object(LAUNCHER, "parse_args", return_value=_args(str(external_script))),
                patch.object(LAUNCHER, "load_config", return_value={"python_executable": str(configured)}),
                patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
            ):
                result = LAUNCHER.main()

            self.assertEqual(result, 0)
            self.assertEqual(Path(run.call_args.args[0][0]), configured)

    def test_missing_config_returns_repair_instructions(self) -> None:
        stderr = io.StringIO()
        with (
            patch.object(LAUNCHER, "parse_args", return_value=_args("main.py")),
            patch.object(LAUNCHER, "load_config", side_effect=FileNotFoundError("missing config")),
            patch("sys.stderr", stderr),
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 2)
        self.assertIn("选择 0", stderr.getvalue())

    def test_interactive_child_exit_returns_to_launcher_until_launcher_q(self) -> None:
        completed = SimpleNamespace(returncode=0)
        interactive_args = _args("")
        interactive_args.script = None
        with (
            patch.object(LAUNCHER, "parse_args", return_value=interactive_args),
            patch.object(LAUNCHER, "choose_script", side_effect=["resource_menu.py", None]) as choose,
            patch.object(LAUNCHER, "resolve_configured_python", return_value=Path(sys.executable)),
            patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 0)
        self.assertEqual(choose.call_count, 2)
        run.assert_called_once()

    def test_explicit_script_keeps_single_run_return_code(self) -> None:
        completed = SimpleNamespace(returncode=7)
        with (
            patch.object(LAUNCHER, "parse_args", return_value=_args("main.py")),
            patch.object(LAUNCHER, "resolve_configured_python", return_value=Path(sys.executable)),
            patch.object(LAUNCHER, "choose_script") as choose,
            patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 7)
        choose.assert_not_called()
        run.assert_called_once()

    def test_custom_config_is_resolved_and_forwarded_through_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "Game A.json"
            configured = Path(temporary) / "python.exe"
            config_path.write_text("{}", encoding="utf-8")
            configured.touch()
            args = _args("main.py")
            args.config = config_path
            completed = SimpleNamespace(returncode=0)
            with (
                patch.object(LAUNCHER, "parse_args", return_value=args),
                patch.object(LAUNCHER, "load_config", return_value={"python_executable": str(configured)}),
                patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
            ):
                result = LAUNCHER.main()

            self.assertEqual(result, 0)
            self.assertEqual(
                run.call_args.kwargs["env"][LAUNCHER.ACTIVE_CONFIG_PATH_ENV],
                str(config_path.resolve()),
            )

    def test_explicit_config_and_channel_are_forwarded_to_child_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "Game A.json"
            configured = Path(temporary) / "python.exe"
            config_path.write_text("{}", encoding="utf-8")
            configured.touch()
            args = _args("resource_menu.py")
            args.config = config_path
            args.channel_package_dir_name = "GAME_hongtu_L"
            completed = SimpleNamespace(returncode=0)
            with (
                patch.object(LAUNCHER, "parse_args", return_value=args),
                patch.object(LAUNCHER, "load_config", return_value={"python_executable": str(configured)}),
                patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
            ):
                result = LAUNCHER.main()

            self.assertEqual(result, 0)
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env[LAUNCHER.EXPLICIT_CONFIG_ENV], "1")
            self.assertEqual(
                child_env[LAUNCHER.CHANNEL_PACKAGE_DIR_NAME_ENV],
                "GAME_hongtu_L",
            )

    def test_channel_without_explicit_config_does_not_enable_explicit_mode(self) -> None:
        args = _args("resource_menu.py")
        args.config = None
        args.channel_package_dir_name = "GAME_hongtu_L"
        completed = SimpleNamespace(returncode=0)
        with (
            patch.dict(os.environ, {LAUNCHER.EXPLICIT_CONFIG_ENV: "0"}, clear=False),
            patch.object(LAUNCHER, "parse_args", return_value=args),
            patch.object(LAUNCHER, "resolve_configured_python", return_value=Path(sys.executable)),
            patch.object(LAUNCHER.subprocess, "run", return_value=completed) as run,
        ):
            result = LAUNCHER.main()

        self.assertEqual(result, 0)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env[LAUNCHER.EXPLICIT_CONFIG_ENV], "0")
        self.assertEqual(
            child_env[LAUNCHER.CHANNEL_PACKAGE_DIR_NAME_ENV],
            "GAME_hongtu_L",
        )


if __name__ == "__main__":
    unittest.main()
