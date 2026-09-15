import json
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.resource_crypto_ai import discover_ai_candidates, SYSTEM_PROMPT


def config(tmp_path):
    return SimpleNamespace(workspace_root=tmp_path, catalog_source_path=tmp_path / "catalog.json",
                           enable_ai_translation=False)


def test_no_ai_skips_without_request_or_evidence_upload(tmp_path, capsys):
    cfg = config(tmp_path)
    with patch("pipeline.resource_crypto_ai.request_analysis", side_effect=AssertionError("network forbidden")):
        assert discover_ai_candidates(cfg, [tmp_path / "not_read.bundle"]) == []
    assert "未配置可用 AI" in capsys.readouterr().out


def test_prompt_is_analysis_not_translation():
    assert "不是翻译员" in SYSTEM_PROMPT
    assert "不可默认包名" in SYSTEM_PROMPT
    assert "needed_files" in SYSTEM_PROMPT


def test_ai_candidate_cached_but_not_executed(tmp_path):
    cfg = config(tmp_path)
    path = tmp_path / "encrypted.bundle"
    original = b"not standard" * 40
    path.write_bytes(original)
    response = {"classification": "encryption_evidence", "summary": "candidate only",
        "profiles": [{"algorithm": "xor_repeat", "key_hex": "123456", "evidence": "sample"},
                     {"algorithm": "run_code", "key_hex": "ff", "evidence": "ignore"}], "needed_files": []}
    with patch("pipeline.resource_crypto_ai.available_transports", return_value=["http"]), \
         patch("pipeline.resource_crypto_ai.request_analysis", return_value=response) as request:
        assert discover_ai_candidates(cfg, [path])[0]["key_hex"] == "123456"
        assert len(discover_ai_candidates(cfg, [path])) == 1
        assert request.call_count == 1
    assert path.read_bytes() == original


def test_ai_failure_does_not_block_or_leak_exception(tmp_path, capsys):
    path = tmp_path / "unknown.bundle"
    path.write_bytes(b"unknown" * 20)
    with patch("pipeline.resource_crypto_ai.available_transports", return_value=["http"]), \
         patch("pipeline.resource_crypto_ai.request_analysis", side_effect=RuntimeError("secret-token")):
        assert discover_ai_candidates(config(tmp_path), [path]) == []
    assert "secret-token" not in capsys.readouterr().out
