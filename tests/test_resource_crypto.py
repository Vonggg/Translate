import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline.resource_crypto import (
    decrypt_staged, encrypt_results, load_candidates, settings_path, validate_bundle, xor_repeat,
)


@pytest.mark.parametrize("key", [b"x", b"different-key", bytes(range(256))])
def test_algorithm_reused_with_different_keys(key):
    data = bytes(range(256)) * 33
    encrypted = xor_repeat(data, key)
    assert encrypted == bytes(v ^ key[i % len(key)] for i, v in enumerate(data))
    assert xor_repeat(encrypted, key) == data


def test_auto_key_and_optional_configuration(tmp_path):
    game = tmp_path / "game"
    game.mkdir()
    (game / "AndroidManifest.xml").write_text('<manifest package="com.example.other"/>')
    candidates = load_candidates(game, tmp_path)
    assert bytes.fromhex(candidates[0]["key_hex"]) == hashlib.md5(b"com.example.other").hexdigest().upper().encode()
    path = settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"auto_detect": False, "profiles": [{"algorithm": "xor_repeat", "key_hex": "123456"}]}))
    assert load_candidates(game, tmp_path)[0]["key_hex"] == "123456"
    path.write_text('{"enabled": false}')
    assert load_candidates(game, tmp_path) == []


def test_unknown_and_plaintext_untouched(tmp_path):
    path = tmp_path / "bundle"
    profile = [{"algorithm": "xor_repeat", "key_hex": "123456"}]
    for data in (b"unknown resource" * 9, b"UnityFS\0plaintext", b"short"):
        path.write_bytes(data)
        assert decrypt_staged(path, profile) is None
        assert path.read_bytes() == data


def test_recognized_corruption_is_not_written(tmp_path):
    path = tmp_path / "bundle"
    encrypted = xor_repeat(b"UnityFS\0broken file", b"key")
    path.write_bytes(encrypted)
    with pytest.raises(ValueError):
        decrypt_staged(path, [{"algorithm": "xor_repeat", "key_hex": b"key".hex()}])
    assert path.read_bytes() == encrypted


def test_export_metadata_import_modified_payload(tmp_path):
    source, result = tmp_path / "source", tmp_path / "result"
    key = b"another-game-key"
    plain = b"UnityFS\0original"
    source.write_bytes(xor_repeat(plain, key))
    with patch("pipeline.resource_crypto.validate_bundle", return_value=3):
        metadata = decrypt_staged(source, [{"algorithm": "xor_repeat", "key_hex": key.hex()}])
        assert source.read_bytes() == plain
        assert metadata["plaintext_sha256"] == hashlib.sha256(plain).hexdigest()
        changed = b"UnityFS\0modified-longer-text"
        result.write_bytes(changed)
        entries = [{"staged_relative": "bundle", "resource_crypto": metadata}]
        assert encrypt_results(entries, {"bundle": result}) == 1
        assert xor_repeat(result.read_bytes(), key) == changed
        assert encrypt_results(entries, {}) == 0


def test_invalid_import_not_overwritten(tmp_path):
    path = tmp_path / "result"
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        encrypt_results([{"staged_relative": "bundle", "resource_crypto": {
            "algorithm": "xor_repeat", "key_hex": "12"}}], {"bundle": path})
    assert path.read_bytes() == b"invalid"


def test_staging_and_import_hooks_preserve_game_and_reuse_export_key(tmp_path):
    from dataclasses import replace
    from support.config import load_config
    from pipeline.resource_staging import (
        prepare_unified_resource_source, resource_source_map_path,
        restore_imported_resource_paths, encrypt_imported_resource_paths,
    )
    game = tmp_path / "projects/demo/game-name/game"
    android = game / "assets/aa/Android"
    android.mkdir(parents=True)
    (game / "assets/bin/Data").mkdir(parents=True)
    (game / "AndroidManifest.xml").write_text('<manifest package="com.example.demo"/>')
    key = hashlib.md5(b"com.example.demo").hexdigest().upper().encode()
    encrypted = xor_repeat(b"UnityFS\0test", key)
    original = android / "test.bundle"
    original.write_bytes(encrypted)
    cfg = replace(load_config(), root_dir=tmp_path / "tool", project_root_dir=tmp_path / "projects",
        project_name="demo", resource_source_subpath=Path("game-name/game/assets/bin/Data"),
        catalog_source_subpath=Path("game-name/game/assets/aa/catalog.json"),
        resource_staging_root=tmp_path / "staging")
    with patch("pipeline.resource_staging.inspect_and_download_catalog_resources", return_value=True), \
         patch("pipeline.resource_crypto.validate_bundle", return_value=1):
        staging = prepare_unified_resource_source(cfg)
        assert (staging / "aa/Android/test.bundle").read_bytes() == b"UnityFS\0test"
        state = json.loads(resource_source_map_path(cfg).read_text())
        assert state["entries"][0]["resource_crypto"]["key_hex"] == key.hex()
        raw = tmp_path / "raw"
        output = raw / "aa/Android/test.bundle"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"UnityFS\0changed")
        restored = restore_imported_resource_paths(cfg, tmp_path / "final", raw)
        settings = settings_path(cfg.workspace_root)
        settings.write_text('{"enabled":false}')
        assert encrypt_imported_resource_paths(cfg, restored) == 1
        assert xor_repeat(next(iter(restored.values())).read_bytes(), key) == b"UnityFS\0changed"
    assert original.read_bytes() == encrypted
