from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main
from pipeline.bitmap_font_detection import UnsupportedBitmapFontError
from pipeline.translation import _manual_ai_field_selection


class FullPipelineFailureStopTests(unittest.TestCase):
    def test_script_zero_returns_failure_for_confirmed_bitmap_font(self) -> None:
        error = UnsupportedBitmapFontError(3, Path("bitmap_font_detection.json"))
        with patch("main.scan_and_record", side_effect=error):
            result = main._run_noninteractive_step(SimpleNamespace(), "0")

        self.assertEqual(result, 1)

    def test_noninteractive_ai_failure_never_waits_for_manual_input(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            "pipeline.translation.os.environ",
            {"TRANSLATE_NONINTERACTIVE_STEP": "1"},
        ), patch("builtins.input", side_effect=AssertionError("must not prompt")):
            with self.assertRaisesRegex(RuntimeError, "已自动停止后续流程"):
                _manual_ai_field_selection(
                    SimpleNamespace(),
                    Path(temp_dir) / "string_field_review.txt",
                    "Codex CLI 超时",
                    is_error=True,
                )

    def test_failed_isolated_step_prevents_later_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "main.subprocess.run",
            return_value=subprocess.CompletedProcess(["python"], 1),
        ) as run:
            result = main._run_steps_in_isolated_processes(
                SimpleNamespace(root_dir=Path(temp_dir)),
                ["0", "1", "2"],
                "测试全部执行",
            )

        self.assertEqual(result, 1)
        self.assertEqual(run.call_count, 1)
        child_env = run.call_args.kwargs["env"]
        self.assertEqual(child_env["TRANSLATE_NONINTERACTIVE_STEP"], "1")

    def test_failure_in_middle_step_prevents_remaining_steps(self) -> None:
        results = [
            subprocess.CompletedProcess(["python"], 0),
            subprocess.CompletedProcess(["python"], 9),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "main.subprocess.run",
            side_effect=results,
        ) as run:
            result = main._run_steps_in_isolated_processes(
                SimpleNamespace(root_dir=Path(temp_dir)),
                ["1", "2", "3"],
                "测试连续执行",
            )

        self.assertEqual(result, 1)
        self.assertEqual(run.call_count, 2)

    def test_any_step_exception_is_converted_to_failure_code(self) -> None:
        with patch("main.export_translated_files", side_effect=ValueError("broken output")):
            result = main._run_noninteractive_step(SimpleNamespace(), "4")

        self.assertEqual(result, 1)

    def test_keyboard_interrupt_is_converted_to_stop_code(self) -> None:
        with patch("main.build_ttf_replacements", side_effect=KeyboardInterrupt):
            result = main._run_noninteractive_step(SimpleNamespace(), "6")

        self.assertEqual(result, 130)


if __name__ == "__main__":
    unittest.main()
