import base64
import json
import struct
from unittest.mock import patch

from pipeline.catalog_tools import _attach_json_catalog_internal_ids, patch_expanded_catalog_from_final_bundles
from pipeline.catalog_tools import _rebuild_extra_data_base64


def catalog_fixture():
    raw = json.dumps({"m_Hash": "", "m_Crc": 0, "m_BundleName": "unrelated_internal_name", "m_BundleSize": 8})
    extra = b"xx" + raw.encode("utf-16le")
    return {"m_InternalIds": ["{RuntimePath}/Android/actual.bundle"],
        "m_EntryDataString": {"原始base64": base64.b64encode(struct.pack("<i7i", 1, 0, 0, -1, 0, 0, 0, 0)).decode()},
        "m_ExtraDataString": {"原始base64": base64.b64encode(extra).decode(),
            "AssetBundleRequestOptions": [{"view": "utf16le_even", "char_offset": 1, "raw": raw, **json.loads(raw)}]}}


def test_offset_identity_handles_empty_hash_and_zero_crc(tmp_path):
    catalog = catalog_fixture()
    _attach_json_catalog_internal_ids(catalog)
    row = catalog["m_ExtraDataString"]["AssetBundleRequestOptions"][0]
    assert row["InternalId"].endswith("actual.bundle")
    source, final = tmp_path / "source", tmp_path / "final"
    source.mkdir()
    final.mkdir()
    (source / "actual.bundle").write_bytes(b"12345678")
    (final / "actual.bundle").write_bytes(b"longer modified")
    output = tmp_path / "Output.json"
    output.write_text(json.dumps(catalog_fixture()))
    with patch("pipeline.catalog_tools.calculate_unityfs_uncompressed_crc", return_value=1234):
        sizes, crcs, _ = patch_expanded_catalog_from_final_bundles(output, final, source_bundle_root=source, zero_crc=True)
    assert sizes == 1 and crcs == 0
    assert json.loads(output.read_text())["m_ExtraDataString"]["AssetBundleRequestOptions"][0]["m_BundleSize"] == 15


def test_wrong_fragment_not_associated():
    catalog = catalog_fixture()
    row = catalog["m_ExtraDataString"]["AssetBundleRequestOptions"][0]
    row["char_offset"] += 1
    _attach_json_catalog_internal_ids(catalog)
    assert "InternalId" not in row


def test_growing_size_relocates_following_entry_and_length_prefix():
    raws = ['{"m_Crc":0,"m_BundleSize":9}', '{"m_Crc":0,"m_BundleSize":8}']
    data = bytearray()
    offsets, rows = [], []
    for raw in raws:
        offsets.append(len(data))
        data.extend(b"object")
        data.extend(struct.pack("<i", len(raw.encode("utf-16le"))))
        position = len(data)
        rows.append({"view": "utf16le_even" if position % 2 == 0 else "utf16le_odd",
                     "char_offset": position // 2, "raw": raw, **json.loads(raw)})
        data.extend(raw.encode("utf-16le"))
    rows[0]["m_BundleSize"] = 1000
    extra = {"原始base64": base64.b64encode(data).decode(), "AssetBundleRequestOptions": rows}
    entries = struct.pack("<i", 3) + b"".join(
        struct.pack("<7i", index, 0, -1, 0, offset, 0, 0)
        for index, offset in enumerate([offsets[0], offsets[1], -1]))
    entry_field = {"原始base64": base64.b64encode(entries).decode()}
    rebuilt = base64.b64decode(_rebuild_extra_data_base64(extra, entry_field=entry_field))
    changed_entries = base64.b64decode(entry_field["原始base64"])
    assert len(rebuilt) == len(data) + 6
    assert struct.unpack_from("<i", changed_entries, 4 + 28 + 16)[0] == offsets[1] + 6
    assert struct.unpack_from("<i", changed_entries, 4 + 56 + 16)[0] == -1
    assert struct.unpack_from("<i", rebuilt, 6)[0] == len(raws[0].encode("utf-16le")) + 6
    assert rebuilt[offsets[1] + 6:] == data[offsets[1]:]
