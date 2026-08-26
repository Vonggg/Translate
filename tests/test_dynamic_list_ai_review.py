from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_tools():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_dynamic_list_ai_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tools()


def _candidate(candidate_id: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "name": "ObfuscatedData",
        "source": r"bin\Data\data.unity3d",
        "bundle_entry": "sharedassets1.assets",
        "array_label": "rows.Array",
        "item_count": 3,
        "entry_kind": "inline",
        "field_signature": ["icon", "price", "title"],
        "sample_names": ["One", "Two", "Three"],
        "image_count": 3,
        "classification": {"score": 4},
    }


def _config(root: Path, *, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        root_dir=root,
        enable_ai_dynamic_list_review=enabled,
        ai_dynamic_list_transport="codex_cli",
        ai_dynamic_list_codex_model="gpt-5.3-codex-spark",
        ai_dynamic_list_codex_reasoning_effort="medium",
        ai_dynamic_list_base_url="https://api.deepseek.invalid",
        ai_dynamic_list_api_key="test-key",
        ai_dynamic_list_model="deepseek-test",
        ai_dynamic_list_timeout=30,
        ai_dynamic_list_proxy_http="",
        ai_dynamic_list_proxy_https="",
    )


class _HTTPResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class DynamicListAIReviewTests(unittest.TestCase):
    def test_codex_receives_opaque_candidate_id_and_maps_it_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_id = (
                str(root / "private" / "Store.json")
                + "|742|m_rewards.Array"
            )
            seen_user_content: list[str] = []

            def codex_request(**kwargs):
                seen_user_content.append(kwargs["user_content"])
                payload = json.loads(kwargs["user_content"])
                public_id = payload["candidates"][0]["candidate_id"]
                return (
                    {
                        "accepted": [{
                            "candidate_id": public_id,
                            "kind": "商店/商品",
                            "reason": "结构一致且含价格与图片",
                        }]
                    },
                    {},
                )

            with (
                patch.object(TOOLS, "load_config", return_value=_config(root)),
                patch.object(TOOLS, "codex_cli_available", return_value=True),
                patch.object(
                    TOOLS,
                    "request_structured_output",
                    side_effect=codex_request,
                ),
                patch.object(
                    TOOLS,
                    "DEFAULT_DYNAMIC_LIST_AI_REVIEW",
                    root / "dynamic_list_ai_review.json",
                ),
            ):
                accepted = TOOLS._request_dynamic_list_ai_review(
                    [_candidate(private_id)]
                )

            self.assertEqual(accepted, {private_id})
            self.assertEqual(len(seen_user_content), 1)
            sent = json.loads(seen_user_content[0])
            public_id = sent["candidates"][0]["candidate_id"]
            self.assertRegex(public_id, r"^candidate_[0-9a-f]{16}$")
            self.assertNotIn(private_id, seen_user_content[0])
            self.assertNotIn(str(root), seen_user_content[0])

    def test_codex_fails_once_then_deepseek_retries_four_times_and_filters_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private_id = str(root / "Store.json") + "|700|items.Array"
            http_calls: list[dict] = []

            class Session:
                trust_env = True

                def post(self, _url, **kwargs):
                    http_calls.append(kwargs)
                    if len(http_calls) < 4:
                        raise RuntimeError("temporary DeepSeek failure")
                    request_body = json.loads(
                        kwargs["json"]["messages"][-1]["content"]
                    )
                    public_id = request_body["candidates"][0]["candidate_id"]
                    content = json.dumps(
                        {
                            "accepted": [
                                {
                                    "candidate_id": public_id,
                                    "kind": "商店/商品",
                                    "reason": "valid",
                                },
                                {
                                    "candidate_id": "candidate_not_in_request",
                                    "kind": "商店/商品",
                                    "reason": "must be ignored",
                                },
                            ]
                        },
                        ensure_ascii=False,
                    )
                    return _HTTPResponse(
                        {"choices": [{"message": {"content": content}}]}
                    )

            with (
                patch.object(TOOLS, "load_config", return_value=_config(root)),
                patch.object(TOOLS, "codex_cli_available", return_value=True),
                patch.object(
                    TOOLS,
                    "request_structured_output",
                    side_effect=RuntimeError("Codex failed"),
                ) as codex_request,
                patch("requests.Session", return_value=Session()),
                patch.object(
                    TOOLS,
                    "DEFAULT_DYNAMIC_LIST_AI_REVIEW",
                    root / "dynamic_list_ai_review.json",
                ),
            ):
                accepted = TOOLS._request_dynamic_list_ai_review(
                    [_candidate(private_id)]
                )

            self.assertEqual(codex_request.call_count, 1)
            self.assertEqual(len(http_calls), 4)
            self.assertEqual(accepted, {private_id})

    def test_disabled_ai_does_not_touch_any_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(
                    TOOLS,
                    "load_config",
                    return_value=_config(root, enabled=False),
                ),
                patch.object(TOOLS, "_dynamic_list_ai_candidates") as candidates,
                patch.object(TOOLS, "_dynamic_list_ai_transport_chain") as chain,
                patch.object(TOOLS, "request_structured_output") as codex_request,
                patch("requests.Session") as http_session,
            ):
                accepted = TOOLS._request_dynamic_list_ai_review(
                    [_candidate("private-candidate")]
                )

            self.assertEqual(accepted, set())
            candidates.assert_not_called()
            chain.assert_not_called()
            codex_request.assert_not_called()
            http_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
