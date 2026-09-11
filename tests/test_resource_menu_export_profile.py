from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import resource_menu


class ResourceMenuExportProfileTests(unittest.TestCase):
    def _select(self, raw: str) -> str | None:
        with patch.object(resource_menu, "prompt_input", return_value=raw):
            return resource_menu.prompt_export_profile()

    def test_single_profile(self) -> None:
        self.assertEqual(self._select("1"), "basic")

    def test_range_profiles(self) -> None:
        self.assertEqual(self._select("1-3"), "basic+objects+mesh")

    def test_comma_separated_profiles(self) -> None:
        self.assertEqual(self._select("1,3"), "basic+mesh")

    def test_all_profiles(self) -> None:
        self.assertEqual(self._select("a"), "all")

    def test_cancel(self) -> None:
        self.assertIsNone(self._select("q"))

    def test_cancel_export_returns_to_resource_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            cfg = SimpleNamespace(
                resource_input_root=workspace / "input",
                resource_managed_root=workspace / "Managed",
                import_overlay_dir=workspace / "ToImport",
                workspace_root=workspace,
                log_dir=workspace / "logs",
            )
            with (
                patch.object(resource_menu, "load_config", return_value=cfg),
                patch.object(resource_menu, "prompt_input", side_effect=["1", "q", "q"]) as prompt,
            ):
                result = resource_menu.main()

            self.assertEqual(result, 0)
            self.assertEqual(prompt.call_count, 3)

    def test_export_all_command_is_noninteractive(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            cfg = SimpleNamespace(
                resource_input_root=workspace / "input",
                resource_managed_root=workspace / "Managed",
                import_overlay_dir=workspace / "ToImport",
                workspace_root=workspace,
                log_dir=workspace / "logs",
            )
            with (
                patch.object(resource_menu, "load_config", return_value=cfg),
                patch.object(resource_menu.sys, "argv", ["resource_menu.py", "export-all"]),
                patch.object(resource_menu, "run_export_profile", return_value=0) as run_export,
                patch.object(resource_menu, "prompt_input") as prompt,
            ):
                result = resource_menu.main()

            self.assertEqual(result, 0)
            run_export.assert_called_once_with(cfg, "all")
            prompt.assert_not_called()

    def test_monobehaviour_summary_reports_safely_preserved_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_root = Path(temp_dir)
            manifest_dir = input_root / "bundle"
            manifest_dir.mkdir()
            (manifest_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "MonoBehaviourSummaries": [
                            {
                                "Total": 10,
                                "WithCustomFields": 6,
                                "BaseOnly": 1,
                                "Preserved": 3,
                                "Failed": 0,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                resource_menu.print_monobehaviour_export_summary(input_root)

            rendered = output.getvalue()
            self.assertIn("total=10", rendered)
            self.assertIn("preserved=3", rendered)
            self.assertIn("原始对象会被保留", rendered)


if __name__ == "__main__":
    unittest.main()
