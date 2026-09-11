from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
import json
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

    def test_dynamic_batch_patches_dynamic_translation_cache(self) -> None:
        request = Path("ai_stringliteral_translation_request_batch_001.json")
        self.assertEqual(
            TOOLS._translation_cache_path_for_ai_request(request).name,
            "stringliteral_trans.json",
        )
        self.assertEqual(
            TOOLS._translation_cache_path_for_ai_request(
                Path("ai_translation_request_batch_001.json")
            ).name,
            "trans.json",
        )

    def test_auto_clean_removes_missing_chars_from_static_and_dynamic_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "translation_chars_missing_from_ttf.txt"
            static = root / "trans.json"
            dynamic = root / "stringliteral_trans.json"
            missing.write_text("★坏", encoding="utf-8")
            static.write_text(
                json.dumps({"A": "好★文本", "B": "正常"}, ensure_ascii=False),
                encoding="utf-8",
            )
            dynamic.write_text(
                json.dumps({"C": "坏字符", "D": "坏"}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = TOOLS.run_clean_all_unsupported_ttf_chars(
                missing,
                [static, dynamic],
            )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(static.read_text(encoding="utf-8"))["A"], "好文本")
            dynamic_data = json.loads(dynamic.read_text(encoding="utf-8"))
            self.assertEqual(dynamic_data["C"], "字符")
            self.assertEqual(dynamic_data["D"], "D")

    def test_auto_retry_skips_complete_responses_and_retries_failed_batches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = root / "ai_translation_request_batch_001.json"
            failed = root / "ai_stringliteral_translation_request_batch_002.json"
            complete.write_text("{}", encoding="utf-8")
            failed.write_text("{}", encoding="utf-8")

            def response_state(request_path, _response_path, _strategy):
                return "可解析: 1 条" if request_path == complete.resolve() else "未生成 response"

            with (
                patch.object(TOOLS, "load_config", return_value=object()),
                patch.object(TOOLS, "get_strategy", return_value=object()),
                patch.object(TOOLS, "_ai_response_state", side_effect=response_state),
                patch.object(TOOLS, "run_ai_translation_batch_tool", return_value=0) as run_batch,
            ):
                result = TOOLS.run_retry_failed_ai_batches([complete, failed])

            self.assertEqual(result, 0)
            run_batch.assert_called_once()
            self.assertEqual(run_batch.call_args.args[1], failed.resolve())
            self.assertEqual(
                run_batch.call_args.kwargs["trans_path"].name,
                "stringliteral_trans.json",
            )


if __name__ == "__main__":
    unittest.main()
