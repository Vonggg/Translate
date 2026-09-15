import json
import os
from pathlib import Path

import pipeline.il2cpp_display_usage as m


def test_content_identity_ignores_timestamp_but_detects_same_size_edit(tmp_path):
    source = tmp_path / "binary"
    source.write_bytes(b"abcd")
    stat = source.stat()
    original = m._content_fingerprint([source], {"rule": 1})
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns + 100000))
    assert m._content_fingerprint([source], {"rule": 1}) == original
    source.write_bytes(b"abce")
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert m._content_fingerprint([source], {"rule": 1}) != original
    assert m._content_fingerprint([source], {"rule": 2}) != original


def test_cache_integrity_and_atomic_publication(tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    result = {"stats": {}, "exact_literals": [], "derived_influence": [], "unresolved": []}
    m._write_analysis_cache(cache, "valid", result)
    assert m._read_analysis_cache(cache, "valid")[0] == result
    assert m._read_analysis_cache(cache, "changed")[0] is None
    old = cache.read_bytes()
    def fail(*args):
        raise OSError("interrupted publish")
    monkeypatch.setattr(m.os, "replace", fail)
    import pytest
    with pytest.raises(OSError):
        m._write_analysis_cache(cache, "new", result)
    assert cache.read_bytes() == old
    assert list(tmp_path.glob("*.tmp")) == []
    damaged = json.loads(old)
    damaged["result"]["exact_literals"].append({"value": "not verified"})
    cache.write_text(json.dumps(damaged), encoding="utf-8")
    assert m._read_analysis_cache(cache, "valid")[0] is None
    cache.write_text("{broken", encoding="utf-8")
    assert m._read_analysis_cache(cache, "valid")[0] is None


def test_script_metadata_shared_payload_matches_standalone(tmp_path, monkeypatch):
    source = tmp_path / "script.json"
    source.write_text(json.dumps({"ScriptMethod": [], "Addresses": [],
        "ScriptString": [{"Address": 32, "Value": "Hello"}],
        "ScriptMetadata": [{"Address": 48, "Name": "Game.Type_TypeInfo"}],
        "ScriptMetadataMethod": [{"Address": 64, "MethodAddress": 80,
            "Name": "Method$UnityEngine.Component.GetComponentInChildren<UnityEngine.UI.Text>()"}]}))
    parsers = [m._load_script, m._load_script_metadata_type_names,
               m._load_script_metadata_method_targets, m._load_script_metadata_component_factory_types]
    expected = [parser(source) for parser in parsers]
    payload = m._read_script_payload(source)
    def fail(*args, **kwargs):
        raise AssertionError("shared payload must not reread disk")
    monkeypatch.setattr(Path, "read_text", fail)
    assert [parser(source, payload=payload) for parser in parsers] == expected


def test_public_entry_reuses_cache_and_invalidates_content_and_options(tmp_path, monkeypatch):
    import elftools.elf.elffile
    class FakeElf:
        def __init__(self, stream): pass
        def __getitem__(self, key): return "EM_AARCH64"
        def iter_sections(self):
            class Section(dict):
                def data(self): return bytes.fromhex("c0035fd6")
            return iter([Section(sh_flags=6, sh_addr=4096, sh_size=4, sh_type="SHT_PROGBITS")])
    monkeypatch.setattr(elftools.elf.elffile, "ELFFile", FakeElf)
    monkeypatch.setattr(m, "_collect_relative_slots", lambda elf: {})
    calls = []
    def analyze(**kwargs):
        calls.append(kwargs)
        return {"stats": {}, "exact_literals": [], "derived_influence": [], "unresolved": []}
    monkeypatch.setattr(m, "analyze_arm64_display_usage", analyze)
    binary, script, literal = [tmp_path / name for name in ("lib.so", "script.json", "literal.json")]
    binary.write_bytes(b"aaaa")
    script.write_text('{"ScriptMethod": [], "Addresses": []}')
    literal.write_text('[{"address":"0x10", "value":"Hello"}]')
    kwargs = dict(libil2cpp_path=binary, script_json_path=script,
                  stringliteral_json_path=literal, cache_path=tmp_path / "cache.json")
    assert not m.analyze_il2cpp_display_usage(**kwargs)["stats"]["cache_hit"]
    assert m.analyze_il2cpp_display_usage(**kwargs)["stats"]["cache_hit"]
    assert len(calls) == 1
    # Translation content/character set is not a native-analysis dependency.
    (tmp_path / "trans.json").write_text('{"Hello":"你好"}', encoding="utf-8")
    assert m.analyze_il2cpp_display_usage(**kwargs)["stats"]["cache_hit"]
    (tmp_path / "trans.json").write_text('{"Hello":"您好呀"}', encoding="utf-8")
    assert m.analyze_il2cpp_display_usage(**kwargs)["stats"]["cache_hit"]
    assert len(calls) == 1
    stat = binary.stat()
    binary.write_bytes(b"bbbb")
    os.utime(binary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert not m.analyze_il2cpp_display_usage(**kwargs)["stats"]["cache_hit"]
    assert not m.analyze_il2cpp_display_usage(**kwargs, max_wrapper_depth=5)["stats"]["cache_hit"]
    assert not m.analyze_il2cpp_display_usage(**kwargs, exclude_literal_addresses=[16])["stats"]["cache_hit"]
    assert len(calls) == 4
