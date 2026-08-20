from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _load_tools():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_ai_menu_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tools()


class AiBatchMenuSelectionTests(unittest.TestCase):
    def test_selects_range_and_mixed_batch_numbers_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            requests = []
            for number in range(1, 16):
                path = records / f"ai_translation_request_batch_{number:03d}.json"
                path.write_text("{}", encoding="utf-8")
                requests.append(path)
            with (
                patch.object(TOOLS, "_default_ai_records_dir", return_value=records),
                patch.object(TOOLS, "_ai_response_state", return_value="未生成 response"),
                patch.object(TOOLS, "get_strategy", return_value=object()),
                patch.object(TOOLS, "load_config", return_value=object()),
                patch.object(TOOLS, "prompt_input", return_value="10-12,15"),
            ):
                selected = TOOLS._select_ai_batch_request_paths()

            self.assertEqual(
                [path.name for path in selected or []],
                [requests[index - 1].name for index in (10, 11, 12, 15)],
            )

    def test_manual_request_path_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            listed = records / "ai_translation_request_batch_001.json"
            listed.write_text("{}", encoding="utf-8")
            manual = records / "custom_request.json"
            with (
                patch.object(TOOLS, "_default_ai_records_dir", return_value=records),
                patch.object(TOOLS, "_ai_response_state", return_value="未生成 response"),
                patch.object(TOOLS, "get_strategy", return_value=object()),
                patch.object(TOOLS, "load_config", return_value=object()),
                patch.object(TOOLS, "prompt_input", return_value=str(manual)),
            ):
                selected = TOOLS._select_ai_batch_request_paths()

            self.assertEqual(selected, [manual])


if __name__ == "__main__":
    unittest.main()
