from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import main


class FontModeSelectionTests(unittest.TestCase):
    def test_unity_licensing_exit_199_is_retried_automatically(self) -> None:
        cfg = SimpleNamespace()
        with (
            patch.object(main, "existing_tmp_chars_path", return_value=Path("tmp_chars.txt")),
            patch.object(main, "launch_unity_tmp_generator", side_effect=[199, 0]) as launch,
            patch.object(main.time, "sleep") as sleep,
        ):
            result = main._run_unity_tmp_generation(cfg)

        self.assertEqual(0, result)
        self.assertEqual(2, launch.call_count)
        sleep.assert_called_once_with(5)

    def test_ngui_only_skips_unity_generation(self) -> None:
        cfg = SimpleNamespace()
        with (
            patch.object(main, "load_font_pipeline_modes", return_value={"tmp_sdf": 0, "ngui": 3}),
            patch.object(main, "_run_unity_tmp_generation") as run_unity,
            patch.object(main, "generate_ngui_fonts") as generate_ngui,
        ):
            self.assertEqual(0, main._run_font_generation(cfg))
        run_unity.assert_not_called()
        generate_ngui.assert_called_once_with(cfg)

    def test_tmp_only_skips_ngui_generation(self) -> None:
        cfg = SimpleNamespace()
        with (
            patch.object(main, "load_font_pipeline_modes", return_value={"tmp_sdf": 2, "ngui": 0}),
            patch.object(main, "_run_unity_tmp_generation", return_value=0) as run_unity,
            patch.object(main, "generate_ngui_fonts") as generate_ngui,
        ):
            self.assertEqual(0, main._run_font_generation(cfg))
        run_unity.assert_called_once_with(cfg)
        generate_ngui.assert_not_called()

    def test_mixed_fonts_run_both_replacement_flows(self) -> None:
        cfg = SimpleNamespace()
        with (
            patch.object(main, "load_font_pipeline_modes", return_value={"tmp_sdf": 1, "ngui": 2}),
            patch.object(main, "prepare_generated_tmp_import_replacements") as prepare_tmp,
            patch.object(main, "prepare_generated_ngui_import_replacements") as prepare_ngui,
        ):
            main._prepare_generated_font_import_replacements(cfg)
        prepare_tmp.assert_called_once_with(cfg)
        prepare_ngui.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()
