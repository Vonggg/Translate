from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools import ai_translation_batch_tool as batch_tool
from pipeline import translation
from pipeline.shared import ScanRecord
from support.config import load_config


class _FakeStrategy:
    name = "fake"
    batch_output_budget_chars = 32000

    def build_batches(self, items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
        return [items]

    def estimate_output_chars(self, text: str) -> int:
        return len(text) + 64

    def system_prompt(self) -> str:
        return "translate"

    def user_content(self, batch: list[tuple[int, str]], _index: int, _count: int) -> str:
        return json.dumps(
            {"items": [{"id": item_id, "text": text} for item_id, text in batch]},
            ensure_ascii=False,
        )

    def extra_payload(self) -> dict[str, object]:
        return {}

    def parse_response(self, content: str) -> dict[int, str]:
        data = json.loads(content)
        return {item["id"]: item["translation"] for item in data["items"]}


def _response(items: list[dict[str, object]]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {"content": json.dumps({"items": items}, ensure_ascii=False)},
                "finish_reason": "stop",
            }
        ],
        "usage": {"total_tokens": 10},
    }


class AITranslationBatchToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.request_path = self.tmp_path / "ai_translation_request_batch_002.json"
        self.response_path = self.tmp_path / "ai_translation_response_batch_002.json"
        self.cfg = SimpleNamespace(
            ai_translation_base_url="https://example.invalid",
            ai_translation_api_key="key",
            ai_translation_model="test-model",
            ai_translation_proxy_http="",
            ai_translation_proxy_https="",
            ai_translation_timeout=10,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_request(self, items: list[dict[str, object]]) -> None:
        self.request_path.write_text(
            json.dumps(
                {
                    "model": "test-model",
                    "messages": [
                        {
                            "role": "user",
                            "content": json.dumps({"items": items}),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_resend_retries_only_missing_ids_and_archives_every_response(self) -> None:
        self.write_request(
            [{"id": 556, "text": "Continue"}, {"id": 557, "text": "Restart"}]
        )
        strategy = _FakeStrategy()
        requested_ids: list[list[int]] = []
        responses = iter(
            [
                _response([{"id": 556, "translation": "继续"}]),
                _response([{"id": 557, "translation": "重新开始"}]),
            ]
        )

        def fake_post(payload, *_args, **_kwargs):
            content = json.loads(payload["messages"][-1]["content"])
            requested_ids.append([item["id"] for item in content["items"]])
            return next(responses)

        with (
            mock.patch.object(batch_tool, "load_config", return_value=self.cfg),
            mock.patch.object(batch_tool, "get_strategy", return_value=strategy),
            mock.patch.object(batch_tool, "post_ai_payload", side_effect=fake_post),
        ):
            batch_tool.resend_batch(self.request_path, self.response_path)

        self.assertEqual(requested_ids, [[556, 557], [557]])
        self.assertEqual(
            len(list(self.tmp_path.glob("ai_translation_response_batch_002_raw_*.json"))),
            2,
        )
        combined = json.loads(self.response_path.read_text(encoding="utf-8"))
        content = json.loads(combined["choices"][0]["message"]["content"])
        self.assertEqual(
            content["items"],
            [
                {"id": 556, "translation": "继续"},
                {"id": 557, "translation": "重新开始"},
            ],
        )

    def test_service_error_is_archived_before_validation_failure(self) -> None:
        self.write_request([{"id": 557, "text": "Restart"}])
        with (
            mock.patch.object(batch_tool, "load_config", return_value=self.cfg),
            mock.patch.object(batch_tool, "get_strategy", return_value=_FakeStrategy()),
            mock.patch.object(
                batch_tool,
                "post_ai_payload",
                return_value={"error": {"message": "queue timeout"}},
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "queue timeout"):
                batch_tool.resend_batch(self.request_path, self.response_path)

        raw_paths = list(self.tmp_path.glob("ai_translation_response_batch_002_raw_*.json"))
        self.assertEqual(len(raw_paths), 4)
        raw_data = json.loads(raw_paths[0].read_text(encoding="utf-8"))
        self.assertEqual(raw_data["error"]["message"], "queue timeout")
        self.assertFalse(self.response_path.exists())

    def test_codex_resend_falls_back_to_configured_http_ai(self) -> None:
        self.write_request([{"id": 557, "text": "Restart"}])
        circuit_file = self.tmp_path / "codex-circuit.json"
        cfg = SimpleNamespace(
            ai_translation_transport="codex_cli",
            ai_translation_codex_model="codex-model",
            ai_translation_codex_reasoning_effort="low",
            ai_translation_base_url="https://example.invalid",
            ai_translation_api_key="key",
            ai_translation_model="http-model",
            ai_translation_proxy_http="",
            ai_translation_proxy_https="",
            ai_translation_timeout=10,
            root_dir=self.tmp_path,
        )
        with (
            mock.patch.object(batch_tool, "load_config", return_value=cfg),
            mock.patch.object(batch_tool, "get_strategy", return_value=_FakeStrategy()),
            mock.patch(
                "pipeline.translation.codex_cli_available",
                return_value=True,
            ),
            mock.patch.object(
                batch_tool,
                "request_structured_output",
                side_effect=RuntimeError("codex failed"),
            ) as codex_request,
            mock.patch.object(
                batch_tool,
                "post_ai_payload",
                return_value=_response([{"id": 557, "translation": "重新开始"}]),
            ) as http_request,
        ):
            batch_tool.resend_batch(
                self.request_path,
                self.response_path,
                circuit_file,
            )
            batch_tool.resend_batch(
                self.request_path,
                self.response_path,
                circuit_file,
            )

        self.assertEqual(codex_request.call_count, 1)
        self.assertEqual(http_request.call_count, 2)
        self.assertTrue(circuit_file.is_file())
        self.assertTrue(self.response_path.is_file())


class MainAITranslationBatchTests(unittest.TestCase):
    def test_main_batch_retries_only_missing_ids_and_writes_complete_response(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir)
            cfg = SimpleNamespace(
                ai_translation_base_url="https://example.invalid",
                ai_translation_api_key="key",
                ai_translation_model="test-model",
                ai_translation_proxy_http="",
                ai_translation_proxy_https="",
                ai_translation_timeout=10,
                stage_record_dir=record_dir,
            )
            requested_ids: list[list[int]] = []
            responses = iter(
                [
                    _response([{"id": 556, "translation": "继续"}]),
                    _response([{"id": 557, "translation": "重新开始"}]),
                ]
            )

            class FakeHTTPResponse:
                status_code = 200

                def __init__(self, data):
                    self.data = data

                def raise_for_status(self) -> None:
                    return None

                def json(self):
                    return self.data

            class FakeSession:
                trust_env = True

                def post(self, _url, **kwargs):
                    content = json.loads(kwargs["json"]["messages"][-1]["content"])
                    requested_ids.append([item["id"] for item in content["items"]])
                    return FakeHTTPResponse(next(responses))

            with mock.patch("requests.Session", return_value=FakeSession()):
                result = translation._translate_ai_batch(
                    [(556, "Continue"), (557, "Restart")],
                    cfg,
                    _FakeStrategy(),
                    batch_index=2,
                    batch_count=2,
                )

            self.assertEqual(requested_ids, [[556, 557], [557]])
            self.assertEqual(result, {556: "继续", 557: "重新开始"})
            raw_paths = list(
                record_dir.glob("ai_translation_response_batch_002_raw_*.json")
            )
            self.assertEqual(len(raw_paths), 2)
            response_path = record_dir / "ai_translation_response_batch_002.json"
            response_data = json.loads(response_path.read_text(encoding="utf-8"))
            response_items = json.loads(
                response_data["choices"][0]["message"]["content"]
            )["items"]
            self.assertEqual(
                response_items,
                [
                    {"id": 556, "translation": "继续"},
                    {"id": 557, "translation": "重新开始"},
                ],
            )

    def test_main_translation_resumes_existing_trans_with_stable_batch_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir) / "records"
            record_dir.mkdir(parents=True)
            cfg = replace(
                load_config(),
                record_dir=record_dir,
                enable_ai_translation=True,
                ai_translation_base_url="https://example.invalid",
                ai_translation_api_key="key",
                ai_translation_model="test-model",
            )
            texts = ["First text", "Second text", "Third text", "Fourth text"]
            (record_dir / cfg.output_trans_json).write_text(
                json.dumps(
                    {
                        texts[0]: "第一段",
                        texts[1]: "第二段",
                        texts[2]: "",
                        texts[3]: "",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            records = [
                ScanRecord(
                    file_path=f"{index}.json",
                    field="m_Text",
                    source_text=text,
                    translated_text="",
                    path_id=index,
                    font_path_id=None,
                )
                for index, text in enumerate(texts)
            ]

            class TwoItemBatchStrategy(_FakeStrategy):
                output_safety_divisor = 12

                def build_batches(self, items):
                    return [items[index:index + 2] for index in range(0, len(items), 2)]

            calls: list[tuple[list[int], int, int]] = []

            def fake_translate_ai_batch(
                batch,
                _cfg,
                _strategy,
                batch_index,
                batch_count,
                **_kwargs,
            ):
                calls.append(([item_id for item_id, _text in batch], batch_index, batch_count))
                return {item_id: f"译文 {item_id}" for item_id, _text in batch}

            with (
                mock.patch.object(translation, "get_strategy", return_value=TwoItemBatchStrategy()),
                mock.patch.object(
                    translation,
                    "_translate_ai_batch",
                    side_effect=fake_translate_ai_batch,
                ),
                mock.patch.object(
                    translation,
                    "_translate_one_text_with_provider_retry",
                ) as fallback_mock,
            ):
                result = translation.build_translation_map(records, cfg)

            self.assertEqual(calls, [([2, 3], 2, 2)])
            self.assertEqual(result[texts[0]], "第一段")
            self.assertEqual(result[texts[1]], "第二段")
            self.assertEqual(result[texts[2]], "译文 2")
            self.assertEqual(result[texts[3]], "译文 3")
            fallback_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
