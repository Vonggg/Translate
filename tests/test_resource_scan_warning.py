"""CLI integration checks; build UnityResourceCLI before running these tests."""
import json
import subprocess
import struct
from pathlib import Path

import pytest


CLI = Path(__file__).resolve().parents[1] / "AssetPipeline_CLI/UnityResourceCLI/bin/Debug/net8.0/UnityResourceCLI.dll"
pytestmark = pytest.mark.skipif(not CLI.exists(), reason="Build UnityResourceCLI first")


def run_export(tmp_path, files, workers=1):
    source = tmp_path / "source"
    for name, data in files.items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    work = tmp_path / "work"
    result = subprocess.run(
        ["dotnet", str(CLI), "export", "--source", str(source), "--work", str(work),
         "--export-workers", str(workers)], capture_output=True, encoding="utf-8", errors="replace",
    )
    report = json.loads((work / "resource_scan_report.json").read_text(encoding="utf-8-sig"))
    return result, report


def test_nonstandard_bundles_warn_but_sidecars_do_not(tmp_path):
    result, report = run_export(tmp_path, {
        "aa/Android/encrypted.bundle": b"not a standard bundle",
        "aa/Android/no_extension": b"unknown",
        "assetpack/pack/content": b"unknown",
        "aa/catalog.json": b"{}",
        "aa/Android/data.resS": b"raw stream",
        "aa/Android/catalog.hash": b"hash",
    })
    assert result.returncode == 0
    assert report["SuspectedEncryptionCount"] == 3
    assert report["ParseFailedCount"] == 0
    assert len(report["Files"]) == 6
    assert "导出不完整" in result.stdout


@pytest.mark.parametrize("workers", [1, 2])
def test_parse_failure_remains_failure_and_is_reported(tmp_path, workers):
    invalid_bundle = (b"UnityFS\0" + struct.pack(">I", 6) + b"5.x.x\0" + b"2020.3.0f1\0"
                      + struct.pack(">QIII", 100, 16, 16, 63) + bytes(64))
    result, report = run_export(tmp_path, {
        "bad.bundle": invalid_bundle,
        "bad2.bundle": invalid_bundle,
    }, workers)
    assert result.returncode != 0
    assert report["ParseFailedCount"] >= 1
    assert report["SuspectedEncryptionCount"] == 0
    assert "导出不完整" in result.stdout


def test_normal_sidecars_do_not_warn(tmp_path):
    result, report = run_export(tmp_path, {"catalog.json": b"{}", "stream.resS": b"raw"})
    assert result.returncode == 0
    assert report["SuspectedEncryptionCount"] == 0
    assert "导出不完整" not in result.stdout
