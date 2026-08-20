from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


def load_tool_module():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_char_preview_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UnsupportedCharPreviewTests(unittest.TestCase):
    def test_target_character_is_red_without_marker_arrows(self) -> None:
        module = load_tool_module()

        contexts = module.format_char_contexts("价格₩19000", "₩")

        self.assertEqual(contexts, ["价格\033[91m₩\033[0m19000"])
        self.assertNotIn(">>>", contexts[0])
        self.assertNotIn("<<<", contexts[0])


if __name__ == "__main__":
    unittest.main()
