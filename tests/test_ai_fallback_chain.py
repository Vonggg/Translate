from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pipeline import translation
from pipeline.shared import ScanRecord
from support.config import load_config


class _Strategy:
    name = "test"
    batch_output_budget_chars = 32000
    output_safety_divisor = 12

    def build_batches(self, items):
        return [items]

    def estimate_output_chars(self, text):
        return len(text) + 16

    def system_prompt(self):
        return "translate"

    def user_content(self, batch, _index, _count):
        return json.dumps(
            {"items": [{"id": item_id, "text": text} for item_id, text in batch]},
            ensure_ascii=False,
        )

    def extra_payload(self):
        return {}

    def parse_response(self, content):
        data = json.loads(content)
        return {
            item["id"]: item["translation"]
            for item in data.get("items", [])
        }


class _HTTPResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class AIFallbackChainTests(unittest.TestCase):
    def _translation_config(self, record_dir: Path):
        return replace(
            load_config(),
            record_dir=record_dir,
            enable_ai_translation=True,
            ai_translation_transport="codex_cli",
            ai_translation_codex_model="codex-model",
            ai_translation_base_url="https://example.invalid",
            ai_translation_api_key="key",
            ai_translation_model="http-model",
        )

    def test_batch_falls_back_from_codex_to_http_after_equal_retry_budget(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._translation_config(Path(temp_dir))
            requested_http_ids = []

            class Session:
                trust_env = True

                def post(self, _url, **kwargs):
                    content = json.loads(kwargs["json"]["messages"][-1]["content"])
                    requested_http_ids.append([item["id"] for item in content["items"]])
                    return _HTTPResponse(
                        {
                            "choices": [{
                                "message": {
                                    "content": json.dumps(
                                        {"items": [{"id": 7, "translation": "开始"}]},
                                        ensure_ascii=False,
                                    )
                                },
                                "finish_reason": "stop",
                            }]
                        }
                    )

            with (
                mock.patch.object(translation, "codex_cli_available", return_value=True),
                mock.patch.object(
                    translation,
                    "request_structured_output",
                    side_effect=RuntimeError("codex failed"),
                ) as codex_request,
                mock.patch("requests.Session", return_value=Session()),
            ):
                result = translation._translate_ai_batch(
                    [(7, "Start")], cfg, _Strategy(), 1, 1
                )

            self.assertEqual(codex_request.call_count, 1)
            self.assertEqual(requested_http_ids, [[7]])
            self.assertEqual(result, {7: "开始"})

    def test_unresolved_ai_text_falls_back_to_baidu_provider(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir)
            cfg = self._translation_config(record_dir)
            record = ScanRecord(
                file_path="sample.json",
                field="m_Text",
                source_text="Play now",
                translated_text="",
                path_id=1,
                font_path_id=None,
            )

            class FailingSession:
                trust_env = True

                def post(self, *_args, **_kwargs):
                    raise RuntimeError("http failed")

            with (
                mock.patch.object(translation, "codex_cli_available", return_value=True),
                mock.patch.object(
                    translation,
                    "request_structured_output",
                    side_effect=RuntimeError("codex failed"),
                ) as codex_request,
                mock.patch("requests.Session", return_value=FailingSession()),
                mock.patch.object(translation, "get_strategy", return_value=_Strategy()),
                mock.patch.object(
                    translation,
                    "_translate_one_text_with_provider_retry",
                    return_value=("Play now", "立即开始"),
                ) as baidu_request,
            ):
                result = translation.build_translation_map([record], cfg)

            self.assertEqual(codex_request.call_count, 1)
            baidu_request.assert_called_once_with(cfg, "Play now", max_attempts=3)
            self.assertEqual(result["Play now"], "立即开始")

    def test_field_review_falls_back_from_codex_to_http_with_same_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            candidates_path = temp_path / "string_field_review.txt"
            candidates_path.write_text(
                "规则\n\nfield:m_Text\nsample_values:\n- Start\n",
                encoding="utf-8",
            )
            cfg = replace(
                load_config(),
                record_dir=temp_path,
                ai_field_review_transport="codex_cli",
                ai_field_review_codex_model="codex-model",
                ai_field_review_base_url="https://example.invalid",
                ai_field_review_api_key="key",
                ai_field_review_model="http-model",
            )
            calls = []

            def request_batch(*_args, transport=None, **_kwargs):
                calls.append(transport)
                if transport == "codex_cli":
                    raise RuntimeError("codex failed")
                return ["m_Text"]

            with (
                mock.patch.object(translation, "codex_cli_available", return_value=True),
                mock.patch.object(
                    translation,
                    "_split_ai_field_review_batches",
                    return_value=["field:m_Text\n", "field:m_Text\n"],
                ),
                mock.patch.object(
                    translation,
                    "_post_ai_field_review_batch",
                    side_effect=request_batch,
                ),
            ):
                fields = translation._request_ai_field_selection(cfg, candidates_path)

            self.assertEqual(calls, ["codex_cli", "http", "http"])
            self.assertEqual(fields, ["m_Text"])

    def test_open_circuit_skips_codex_for_later_translation_batches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cfg = self._translation_config(Path(temp_dir))
            circuit = {}
            requested_http_ids = []

            class Session:
                trust_env = True

                def post(self, _url, **kwargs):
                    content = json.loads(kwargs["json"]["messages"][-1]["content"])
                    items = content["items"]
                    requested_http_ids.append([item["id"] for item in items])
                    return _HTTPResponse(
                        {
                            "choices": [{
                                "message": {
                                    "content": json.dumps(
                                        {
                                            "items": [
                                                {"id": item["id"], "translation": f"译文{item['id']}"}
                                                for item in items
                                            ]
                                        },
                                        ensure_ascii=False,
                                    )
                                },
                                "finish_reason": "stop",
                            }]
                        }
                    )

            with (
                mock.patch.object(translation, "codex_cli_available", return_value=True),
                mock.patch.object(
                    translation,
                    "request_structured_output",
                    side_effect=RuntimeError("codex failed"),
                ) as codex_request,
                mock.patch("requests.Session", return_value=Session()),
            ):
                first = translation._translate_ai_batch(
                    [(1, "One")], cfg, _Strategy(), 1, 2, codex_circuit=circuit
                )
                second = translation._translate_ai_batch(
                    [(2, "Two")], cfg, _Strategy(), 2, 2, codex_circuit=circuit
                )

            self.assertEqual(codex_request.call_count, 1)
            self.assertEqual(requested_http_ids, [[1], [2]])
            self.assertEqual(first, {1: "译文1"})
            self.assertEqual(second, {2: "译文2"})


if __name__ == "__main__":
    unittest.main()
