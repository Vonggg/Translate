from __future__ import annotations

import argparse
import importlib.util
import io
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
    return argparse.Namespace(script=script, script_args=list(script_args), print_python=False)


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


if __name__ == "__main__":
    unittest.main()
