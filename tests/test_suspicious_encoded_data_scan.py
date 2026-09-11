from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from support.suspicious_encoded_data_scan import (
    REPORT_FILENAME,
    scan_entropy_segments,
    scan_json_file,
    scan_project_suspicious_encoded_data,
    shannon_entropy,
)


def test_entropy_and_binary_segment_scan_find_isolated_high_entropy_data() -> None:
    low = b"A" * 1024
    encoded = bytes(range(256)) * 8
    segments = scan_entropy_segments(low + encoded + low)

    assert shannon_entropy(encoded) == 8.0
    segment = next(item for item in segments if item["offset"] <= 1024 < item["end_offset"])
    assert segment["peak_entropy"] >= 7.9
    assert segment["boundary_entropy_drop"] >= 6.0


def test_json_scan_reports_high_entropy_base64_and_byte_array(tmp_path: Path) -> None:
    path = tmp_path / "sample.json"
    payload = {
        "blob": base64.b64encode(bytes(range(256))).decode("ascii"),
        "bytes": list(bytes(range(256))),
        "normal": "Hello world",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    matches = scan_json_file(path)

    assert {item["encoding_hint"] for item in matches} == {"base64", "byte_array"}
    assert all(item["entropy"] >= 7.2 for item in matches)


def test_project_scan_annotates_private_implementation_metadata_field(tmp_path: Path) -> None:
    input_root = tmp_path / "input"
    mono = input_root / "bin" / "Data" / "MonoBehaviour"
    mono.mkdir(parents=True)
    (mono / "sample.json").write_text("{}", encoding="utf-8")
    managed = tmp_path / "managed"
    metadata_dir = managed / "Metadata"
    metadata_dir.mkdir(parents=True)
    (metadata_dir / "global-metadata.dat").write_bytes(b"A" * 1024 + bytes(range(256)) * 4)
    dump = tmp_path / "dump.cs"
    dump.write_text(
        "// Namespace: <PrivateImplementationDetails>{test}\n"
        "internal static a.a_ a_ /*Metadata offset 0x400*/;\n",
        encoding="utf-8",
    )
    records = tmp_path / "records"
    cfg = SimpleNamespace(
        resource_input_root=input_root,
        resource_managed_root=managed,
        il2cpp_dump_cs_path=dump,
        stage_record_dir=records,
    )

    output_path, report = scan_project_suspicious_encoded_data(cfg)

    assert output_path == records / REPORT_FILENAME
    assert output_path.is_file()
    assert report["stats"]["high_entropy_metadata_segment_count"] == 1
    field = report["high_entropy_metadata_segments"][0]["near_private_implementation_field"]
    assert field["field"] == "a_"
    assert field["metadata_offset"] == "0x400"
