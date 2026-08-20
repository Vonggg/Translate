from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import sys
import struct
import zlib
from pathlib import Path
from typing import Any, Iterable

import lz4.block

from support.config import PipelineConfig
from tools.catalog_bin_tool import (
    parse_catalog_file as parse_binary_catalog_file,
    repack_binary_catalog_from_legacy_output,
    write_legacy_output_view as write_binary_legacy_output_view,
)


CATALOG_FIELDS = (
    "m_KeyDataString",
    "m_BucketDataString",
    "m_EntryDataString",
    "m_ExtraDataString",
)

CRC_MISMATCH_RE = re.compile(
    r"CRC Mismatch\.\s+Provided\s+([0-9a-fA-F]+),\s+calculated\s+([0-9a-fA-F]+)\s+from data\.\s+"
    r"Will not load AssetBundle\s+'([^']+)'"
)


def _log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def _log_blue(message: str) -> None:
    print(f"\033[94m{message}\033[0m", flush=True)


def _log_orange(message: str) -> None:
    print(f"\033[38;5;208m{message}\033[0m", flush=True)


def printable_ascii_runs(data: bytes, min_len: int = 4) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for match in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, data):
        rows.append((match.start(), match.group(0).decode("ascii", errors="replace")))
    return rows


def utf16_views(data: bytes) -> Iterable[tuple[str, str]]:
    yield "utf16le_even", data.decode("utf-16le", errors="ignore")
    if len(data) > 1:
        yield "utf16le_odd", data[1:].decode("utf-16le", errors="ignore")


def printable_text_runs(text: str, min_len: int = 4) -> list[tuple[int, str]]:
    pattern = re.compile(rf"[\w\u4e00-\u9fff .:/\\!@#$%^&*()+=,\[\]{{}}<>|;'\"?-]{{{min_len},}}")
    return [(match.start(), match.group(0)) for match in pattern.finditer(text)]


def extract_json_objects(text: str) -> list[tuple[int, dict, str]]:
    rows: list[tuple[int, dict, str]] = []
    for match in re.finditer(r"\{[^{}]*\}", text):
        raw = match.group(0)
        if '"m_Hash"' not in raw and '"m_Crc"' not in raw and '"m_BundleName"' not in raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        rows.append((match.start(), parsed, raw))
    return rows


def parse_key_table(data: bytes) -> list[dict]:
    rows: list[dict] = []
    index = 0
    while index + 4 <= len(data):
        length = int.from_bytes(data[index:index + 4], "little", signed=False)
        if 1 <= length <= 512 and index + 4 + length <= len(data):
            raw = data[index + 4:index + 4 + length]
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                index += 1
                continue
            if all(char.isprintable() or char in "\t\r\n" for char in text):
                rows.append({"offset": index, "length": length, "text": text})
                index += 4 + length
                continue
        index += 1
    return rows


def decode_catalog_field(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def build_field_base_info(value: str, data: bytes) -> dict:
    return {
        "原始base64字符数": len(value),
        "解码后字节数": len(data),
        "原始base64": value,
    }


def expand_catalog_object(catalog: dict) -> tuple[dict, list[str]]:
    expanded = json.loads(json.dumps(catalog, ensure_ascii=False))
    report: list[str] = []

    decoded: dict[str, bytes] = {}
    expanded_fields: dict[str, dict] = {}
    already_expanded = [field for field in CATALOG_FIELDS if isinstance(expanded.get(field), dict)]
    if len(already_expanded) == len(CATALOG_FIELDS):
        report.append("四个字段已经是展开对象，无需重复处理。")
        return expanded, report
    if already_expanded:
        raise RuntimeError(f"部分字段已经展开，部分字段仍未展开，已拒绝处理: {already_expanded}")

    internal_ids = expanded.get("m_InternalIds")
    if isinstance(internal_ids, list):
        report.append(f"m_InternalIds: {len(internal_ids)}")

    for field in CATALOG_FIELDS:
        value = expanded.get(field)
        if not isinstance(value, str):
            raise RuntimeError(f"{field} 缺失或不是 base64 字符串")
        data = decode_catalog_field(value)
        decoded[field] = data
        field_info = build_field_base_info(value, data)
        expanded_fields[field] = field_info
        report.append(f"{field}: base64_chars={len(value)} bytes={len(data)}")

        ascii_runs = printable_ascii_runs(data)
        if ascii_runs:
            field_info["ASCII可见字符串"] = [{"offset": offset, "text": text} for offset, text in ascii_runs]

        utf16_expanded: dict[str, list[dict]] = {}
        for view_name, text in utf16_views(data):
            runs = printable_text_runs(text)
            if runs:
                utf16_expanded[view_name] = [{"offset": offset, "text": text_value} for offset, text_value in runs]
        if utf16_expanded:
            field_info["UTF16可见字符串"] = utf16_expanded

    key_data = decoded.get("m_KeyDataString")
    if key_data is not None:
        key_rows = parse_key_table(key_data)
        expanded_fields["m_KeyDataString"]["长度前缀字符串表"] = key_rows
        report.append(f"m_KeyDataString length-prefixed readable strings: {len(key_rows)}")

    extra_data = decoded.get("m_ExtraDataString")
    if extra_data is not None:
        bundle_blocks: list[dict] = []
        for view_name, text in utf16_views(extra_data):
            for char_offset, parsed, raw in extract_json_objects(text):
                bundle_blocks.append({
                    "view": view_name,
                    "char_offset": char_offset,
                    "raw": raw,
                    **parsed,
                })

        unique_blocks: list[dict] = []
        seen: set[tuple] = set()
        for row in bundle_blocks:
            key = (row.get("m_Hash"), row.get("m_Crc"), row.get("m_BundleName"))
            if key in seen:
                continue
            seen.add(key)
            unique_blocks.append(row)
        expanded_fields["m_ExtraDataString"]["AssetBundleRequestOptions"] = unique_blocks
        report.append(f"m_ExtraDataString AssetBundleRequestOptions blocks: {len(unique_blocks)}")

    for field in CATALOG_FIELDS:
        expanded[field] = expanded_fields[field]
    return expanded, report


def parse_catalog_to_output(cfg: PipelineConfig, source_path: Path | None = None, output_dir: Path | None = None) -> tuple[Path, Path]:
    source = (source_path or cfg.catalog_source_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"catalog 文件不存在: {source}")

    out_dir = (output_dir or (cfg.result_dir / "catalog")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() == ".bin":
        raw_path = out_dir / "catalog_bin_raw.json"
        expanded_path = out_dir / "Output.json"
        report_path = out_dir / "catalog_parse_report.txt"

        def report_progress(done: int, total: int, locations: int) -> None:
            print(
                f"[catalog.bin] 解析 key: {done}/{total}，"
                f"唯一 location={locations}",
                flush=True,
            )

        parsed = parse_binary_catalog_file(
            source,
            raw_path,
            progress=report_progress,
        )
        write_binary_legacy_output_view(parsed, expanded_path)
        summary = parsed["summary"]
        hash_info = parsed["catalog_hash"]
        lines = [
            f"Source: {source}",
            "Format: Unity Addressables binary catalog",
            f"Raw output: {raw_path}",
            f"Compatible output: {expanded_path}",
            f"Keys: {summary['key_count']}",
            f"Locations: {summary['unique_location_count']}",
            f"AssetBundle locations: {summary['asset_bundle_location_count']}",
            f"Catalog hash recorded: {hash_info['recorded']}",
            f"Catalog hash algorithm: {hash_info['algorithm']}",
            f"Catalog hash calculated: {hash_info['calculated']}",
            f"Catalog hash candidates: {hash_info['calculated_by_algorithm']}",
            f"Catalog hash matches: {hash_info['matches']}",
        ]
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if hash_info["matches"] is False:
            candidates = hash_info["calculated_by_algorithm"]
            raise RuntimeError(
                "catalog.hash 校验失败: "
                f"记录={hash_info['recorded']} "
                f"MD5={candidates['md5']} "
                f"SpookyHash128={candidates['spookyhash128']}"
            )
        if hash_info["matches"] is True:
            _log_green(
                f"[catalog.bin] catalog.hash 算法: {hash_info['algorithm']}"
            )
        _log_green(
            f"[catalog.bin] 已生成统一 Output.json: {expanded_path}，"
            f"Bundle={summary['asset_bundle_location_count']}"
        )
        return raw_path, expanded_path

    catalog = json.loads(source.read_text(encoding="utf-8-sig"))
    formatted_path = out_dir / "catalog.json"
    expanded_path = out_dir / "Output.json"
    report_path = out_dir / "catalog_parse_report.txt"

    formatted_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=4), encoding="utf-8")
    expanded, report = expand_catalog_object(catalog)
    expanded_path.write_text(json.dumps(expanded, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"Source: {source}",
        f"Formatted catalog: {formatted_path}",
        f"Expanded output: {expanded_path}",
        "",
        *report,
    ]
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return formatted_path, expanded_path


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rebuild_request_option_raw(row: dict) -> str:
    raw = row.get("raw")
    if isinstance(raw, str) and raw:
        try:
            ordered = json.loads(raw)
        except json.JSONDecodeError:
            ordered = {}
    else:
        ordered = {}

    runtime_keys = [
        "m_Hash",
        "m_Crc",
        "m_Timeout",
        "m_ChunkedTransfer",
        "m_RedirectLimit",
        "m_RetryCount",
        "m_BundleName",
        "m_AssetLoadMode",
        "m_BundleSize",
        "m_UseCrcForCachedBundles",
        "m_UseUWRForLocalBundles",
        "m_ClearOtherCachedVersionsWhenLoaded",
    ]
    for key in runtime_keys:
        if key in row:
            ordered[key] = row[key]
    return _json_compact(ordered)


def _pad_request_option_raw_to_length(raw: str, expected_length: int) -> str:
    if len(raw) == expected_length:
        return raw
    if len(raw) > expected_length:
        raise RuntimeError(f"AssetBundleRequestOptions 回打后长度变长: old_len={expected_length} new_len={len(raw)}")
    padding = " " * (expected_length - len(raw))
    marker = '"m_Crc":'
    index = raw.find(marker)
    if index >= 0:
        insert_at = index + len(marker)
        return raw[:insert_at] + padding + raw[insert_at:]
    return raw[:-1] + padding + raw[-1:]


def _rebuild_extra_data_base64(field_obj: dict, crc_overflow: str = "zero") -> str:
    original_base64 = field_obj.get("原始base64")
    if not isinstance(original_base64, str) or not original_base64:
        raise RuntimeError("m_ExtraDataString 缺少 原始base64，无法回打")

    data = decode_catalog_field(original_base64)
    output = bytearray(data)
    options = field_obj.get("AssetBundleRequestOptions")
    if not isinstance(options, list):
        return original_base64

    replacements: list[tuple[int, bytes, bytes, dict]] = []
    for row in options:
        if not isinstance(row, dict):
            continue
        view = row.get("view")
        char_offset = row.get("char_offset")
        raw = row.get("raw")
        if not isinstance(char_offset, int) or not isinstance(raw, str):
            continue
        if view == "utf16le_even":
            byte_offset = char_offset * 2
        elif view == "utf16le_odd":
            byte_offset = 1 + char_offset * 2
        else:
            continue
        new_raw_unpadded = _rebuild_request_option_raw(row)
        if len(new_raw_unpadded) > len(raw) and "m_Crc" in row:
            if crc_overflow != "zero":
                raise RuntimeError(
                    "AssetBundleRequestOptions 回打后长度变长: "
                    f"hash={row.get('m_Hash')} old_len={len(raw)} new_len={len(new_raw_unpadded)}"
                )
            original_crc = row.get("m_Crc")
            row["m_Crc"] = 0
            new_raw_unpadded = _rebuild_request_option_raw(row)
            print(
                "[catalog][提示] CRC 十进制长度变长，已将该条 m_Crc 回打为 0 以跳过校验: "
                f"hash={row.get('m_Hash')} real_crc={original_crc}"
            )
        new_raw = _pad_request_option_raw_to_length(new_raw_unpadded, len(raw))
        old_bytes = raw.encode("utf-16le")
        new_bytes = new_raw.encode("utf-16le")
        if len(new_bytes) != len(old_bytes):
            raise RuntimeError(
                "AssetBundleRequestOptions 回打后长度变化，当前保守回打已拒绝: "
                f"hash={row.get('m_Hash')} old_len={len(raw)} new_len={len(new_raw)}"
            )
        if bytes(output[byte_offset:byte_offset + len(old_bytes)]) != old_bytes:
            raise RuntimeError(
                "AssetBundleRequestOptions 原始片段定位失败: "
                f"hash={row.get('m_Hash')} view={view} char_offset={char_offset}"
            )
        replacements.append((byte_offset, old_bytes, new_bytes, row))

    for byte_offset, old_bytes, new_bytes, _row in sorted(replacements, key=lambda item: item[0], reverse=True):
        output[byte_offset:byte_offset + len(old_bytes)] = new_bytes

    return base64.b64encode(bytes(output)).decode("ascii")


def repack_expanded_catalog(
    output_json_path: Path,
    destination_path: Path | None = None,
    crc_overflow: str = "zero",
) -> Path:
    source = output_json_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Output.json 不存在: {source}")
    catalog = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(catalog, dict):
        raise RuntimeError(f"Output.json 不是 JSON 对象: {source}")

    for field in CATALOG_FIELDS:
        value = catalog.get(field)
        if isinstance(value, str):
            continue
        if not isinstance(value, dict):
            raise RuntimeError(f"{field} 不是展开对象或 base64 字符串，无法回打")
        if field == "m_ExtraDataString":
            catalog[field] = _rebuild_extra_data_base64(value, crc_overflow=crc_overflow)
        else:
            original_base64 = value.get("原始base64")
            if not isinstance(original_base64, str) or not original_base64:
                raise RuntimeError(f"{field} 缺少 原始base64，无法回打")
            catalog[field] = original_base64

    destination = destination_path or source.with_name("catalog.repacked.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_json_compact(catalog), encoding="utf-8")
    return destination


def collect_crc_mismatches_from_logs(log_paths: Iterable[Path]) -> dict[str, int]:
    crc_by_name: dict[str, int] = {}
    for log_path in log_paths:
        if not log_path.is_file():
            continue
        text = log_path.read_text(encoding="utf-8-sig", errors="ignore")
        for match in CRC_MISMATCH_RE.finditer(text):
            calculated_hex = match.group(2)
            bundle_name = match.group(3)
            crc_by_name[bundle_name] = int(calculated_hex, 16)
    return crc_by_name


def _bundle_lookup_key(path: Path) -> str:
    return path.name.replace("\\", "/")


def collect_final_bundle_files(bundle_root: Path) -> dict[str, Path]:
    if not bundle_root.is_dir():
        return {}
    files: dict[str, Path] = {}
    for path in bundle_root.rglob("*.bundle"):
        if path.is_file():
            files[_bundle_lookup_key(path)] = path
    return files


def _read_null_string(data: bytes, offset: int) -> tuple[str, int]:
    end = data.index(0, offset)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _align16(value: int) -> int:
    return (value + 15) & ~15


def _decode_unity_lzma(data: bytes, decompressed_size: int | None = None) -> bytes:
    import lzma

    if len(data) < 5:
        raise RuntimeError("LZMA block is too short")
    prop = data[0]
    lc = prop % 9
    remainder = prop // 9
    lp = remainder % 5
    pb = remainder // 5
    dictionary_size = int.from_bytes(data[1:5], "little", signed=False)
    decompressor = lzma.LZMADecompressor(
        format=lzma.FORMAT_RAW,
        filters=[{
            "id": lzma.FILTER_LZMA1,
            "dict_size": dictionary_size,
            "lc": lc,
            "lp": lp,
            "pb": pb,
        }],
    )
    if decompressed_size is None:
        return decompressor.decompress(data[5:])
    return decompressor.decompress(data[5:], max_length=decompressed_size)


def _decode_unityfs_block(data: bytes, compression: int, decompressed_size: int) -> bytes:
    if compression == 0:
        return data
    if compression == 1:
        return _decode_unity_lzma(data, decompressed_size)
    if compression in (2, 3):
        return lz4.block.decompress(data, uncompressed_size=decompressed_size)
    raise RuntimeError(f"Unsupported UnityFS compression: {compression}")


def calculate_unityfs_uncompressed_crc(bundle_path: Path) -> int:
    data = bundle_path.read_bytes()
    offset = 0
    signature, offset = _read_null_string(data, offset)
    if signature != "UnityFS":
        raise RuntimeError(f"Unsupported bundle signature: {signature}")

    version = struct.unpack_from(">I", data, offset)[0]
    offset += 4
    _unity_version, offset = _read_null_string(data, offset)
    _revision, offset = _read_null_string(data, offset)

    if version >= 6:
        _total_size = struct.unpack_from(">Q", data, offset)[0]
        offset += 8
        compressed_info_size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        decompressed_info_size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        flags = struct.unpack_from(">I", data, offset)[0]
        offset += 4
    else:
        raise RuntimeError(f"Unsupported UnityFS version: {version}")

    info_offset = _align16(offset) if version >= 7 else offset
    if flags & 0x80:
        info_offset = len(data) - compressed_info_size

    info_compression = flags & 0x3F
    compressed_info = data[info_offset:info_offset + compressed_info_size]
    info = _decode_unityfs_block(compressed_info, info_compression, decompressed_info_size)

    info_cursor = 16
    block_count = struct.unpack_from(">I", info, info_cursor)[0]
    info_cursor += 4
    blocks: list[tuple[int, int, int]] = []
    for _index in range(block_count):
        decompressed_size = struct.unpack_from(">I", info, info_cursor)[0]
        compressed_size = struct.unpack_from(">I", info, info_cursor + 4)[0]
        block_flags = struct.unpack_from(">H", info, info_cursor + 8)[0]
        info_cursor += 10
        blocks.append((decompressed_size, compressed_size, block_flags))

    data_offset = info_offset + compressed_info_size
    if not (flags & 0x80) and (flags & 0x200):
        data_offset = _align16(data_offset)

    crc = 0
    cursor = data_offset
    for decompressed_size, compressed_size, block_flags in blocks:
        compressed_block = data[cursor:cursor + compressed_size]
        cursor += compressed_size
        block_data = _decode_unityfs_block(compressed_block, block_flags & 0x3F, decompressed_size)
        crc = zlib.crc32(block_data, crc)
    return crc & 0xFFFFFFFF


def calculate_final_bundle_crcs_manually(bundle_root: Path) -> dict[str, int]:
    crc_by_name: dict[str, int] = {}
    if not bundle_root.is_dir():
        return crc_by_name
    failed = 0
    for bundle_path in sorted(bundle_root.rglob("*.bundle")):
        try:
            crc_by_name[bundle_path.name] = calculate_unityfs_uncompressed_crc(bundle_path)
        except Exception as exc:
            failed += 1
            print(f"[catalog] 手动 CRC 计算失败: {bundle_path} ({exc})")
    print(f"[catalog] 手动 UnityFS CRC 计算完成: 成功={len(crc_by_name)}, 失败={failed}")
    return crc_by_name


def _source_catalog_android_root(cfg: PipelineConfig) -> Path:
    return cfg.catalog_source_path.parent / "Android"


def _sample_root(cfg: PipelineConfig) -> Path:
    return cfg.root_dir / "样本"


def _copy_if_exists(source: Path, destination: Path) -> None:
    if not source.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def save_catalog_crc_sample(
    cfg: PipelineConfig,
    output_dir: Path,
    source_bundle_root: Path,
    final_bundle_root: Path,
    failures: list[dict],
) -> Path:
    sample_dir = _sample_root(cfg) / "catalog_crc"
    if sample_dir.exists():
        shutil.rmtree(sample_dir)
    sample_dir.mkdir(parents=True, exist_ok=True)

    catalog_source = cfg.catalog_source_path
    _copy_if_exists(catalog_source, sample_dir / catalog_source.name)
    if catalog_source.suffix.lower() == ".bin":
        _copy_if_exists(catalog_source.with_suffix(".hash"), sample_dir / "catalog.hash")
    _copy_if_exists(output_dir / "Output.json", sample_dir / "Output.json")
    report_path = sample_dir / "crc_self_check_report.json"
    report_path.write_text(json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")

    for failure in failures[:20]:
        relative_path = str(failure.get("relative_path", "")).replace("/", "\\")
        if not relative_path:
            continue
        _copy_if_exists(source_bundle_root / relative_path, sample_dir / "source_bundle" / relative_path)
        _copy_if_exists(final_bundle_root / relative_path, sample_dir / "final_bundle" / relative_path)

    print(f"[catalog][样本] CRC 自校验失败样本已保存: {sample_dir}")
    return sample_dir


def validate_catalog_crc_algorithm(
    cfg: PipelineConfig,
    expanded_catalog_path: Path,
    source_bundle_root: Path,
    final_bundle_root: Path,
    output_dir: Path,
) -> bool:
    catalog = json.loads(expanded_catalog_path.read_text(encoding="utf-8-sig"))
    extra = catalog.get("m_ExtraDataString") if isinstance(catalog, dict) else None
    options = extra.get("AssetBundleRequestOptions") if isinstance(extra, dict) else None
    if not isinstance(options, list):
        raise RuntimeError("Output.json 中没有 AssetBundleRequestOptions，无法做 CRC 自校验")

    final_bundle_files = collect_final_bundle_files(final_bundle_root)
    if not final_bundle_files:
        _log_green(
            f"[catalog] 本次没有修改 bundle 文件，无需修改 catalog，已正常跳过: "
            f"{final_bundle_root}"
        )
        return False

    checked = 0
    passed = 0
    skipped_zero_crc = 0
    failures: list[dict] = []
    handled_final_paths: set[Path] = set()
    for row in options:
        if not isinstance(row, dict):
            continue
        hash_value = str(row.get("m_Hash", ""))
        expected_crc = row.get("m_Crc")
        if not hash_value or not isinstance(expected_crc, int):
            continue

        matched_final: Path | None = None
        for _bundle_name, bundle_path in final_bundle_files.items():
            if hash_value in bundle_path.name:
                matched_final = bundle_path
                break
        if matched_final is None:
            continue
        handled_final_paths.add(matched_final.resolve())
        if expected_crc == 0:
            skipped_zero_crc += 1
            continue

        try:
            relative_path = matched_final.relative_to(final_bundle_root)
        except ValueError:
            relative_path = Path(matched_final.name)
        source_bundle_path = source_bundle_root / relative_path
        if not source_bundle_path.is_file():
            failures.append({
                "reason": "source_bundle_missing",
                "hash": hash_value,
                "expected_crc": expected_crc,
                "relative_path": relative_path.as_posix(),
                "source_bundle": str(source_bundle_path),
                "final_bundle": str(matched_final),
            })
            continue

        checked += 1
        try:
            actual_crc = calculate_unityfs_uncompressed_crc(source_bundle_path)
        except Exception as exc:
            failures.append({
                "reason": "source_crc_calculate_failed",
                "hash": hash_value,
                "expected_crc": expected_crc,
                "relative_path": relative_path.as_posix(),
                "source_bundle": str(source_bundle_path),
                "error": str(exc),
            })
            continue
        if actual_crc == expected_crc:
            passed += 1
            continue
        failures.append({
            "reason": "source_crc_mismatch",
            "hash": hash_value,
            "expected_crc": expected_crc,
            "actual_crc": actual_crc,
            "expected_hex": f"0x{expected_crc:08x}",
            "actual_hex": f"0x{actual_crc:08x}",
            "relative_path": relative_path.as_posix(),
            "source_bundle": str(source_bundle_path),
            "final_bundle": str(matched_final),
        })

    option_by_crc_size: dict[tuple[int, int], list[dict]] = {}
    option_by_name_size: dict[tuple[str, int], list[dict]] = {}
    for row in options:
        if not isinstance(row, dict):
            continue
        row_crc = row.get("m_Crc")
        row_size = row.get("m_BundleSize")
        if isinstance(row_crc, int) and row_crc != 0 and isinstance(row_size, int):
            option_by_crc_size.setdefault((row_crc, row_size), []).append(row)
        # m_Crc=0 is a valid Addressables setting (CRC checking disabled).
        # Keep those rows indexed by their actual bundle name and size so the
        # preflight can still identify them without requiring a CRC value.
        if isinstance(row_size, int):
            primary_key = row.get("PrimaryKey")
            internal_id = row.get("InternalId")
            names: set[str] = set()
            for value in (primary_key, internal_id):
                if isinstance(value, str) and value:
                    names.add(Path(value.replace("\\", "/")).name)
            for bundle_name in names:
                option_by_name_size.setdefault((bundle_name.lower(), row_size), []).append(row)

    source_bundles_by_name: dict[str, list[Path]] = {}
    if source_bundle_root.is_dir():
        for source_bundle in sorted(source_bundle_root.rglob("*.bundle")):
            source_bundles_by_name.setdefault(source_bundle.name, []).append(source_bundle)

    for matched_final in sorted(final_bundle_files.values(), key=lambda path: path.as_posix().lower()):
        if matched_final.resolve() in handled_final_paths:
            continue
        handled_final_paths.add(matched_final.resolve())
        source_candidates = source_bundles_by_name.get(matched_final.name, [])
        if len(source_candidates) != 1:
            failures.append({
                "reason": "source_bundle_name_missing" if not source_candidates else "source_bundle_name_ambiguous",
                "bundle_name": matched_final.name,
                "source_candidates": [str(path) for path in source_candidates],
                "final_bundle": str(matched_final),
            })
            continue

        source_bundle_path = source_candidates[0]
        try:
            relative_path = matched_final.relative_to(final_bundle_root)
        except ValueError:
            relative_path = Path(matched_final.name)
        checked += 1
        try:
            actual_crc = calculate_unityfs_uncompressed_crc(source_bundle_path)
        except Exception as exc:
            failures.append({
                "reason": "source_crc_calculate_failed",
                "bundle_name": matched_final.name,
                "relative_path": relative_path.as_posix(),
                "source_bundle": str(source_bundle_path),
                "error": str(exc),
            })
            continue

        source_size = source_bundle_path.stat().st_size
        candidates = option_by_crc_size.get((actual_crc, source_size), [])
        if len(candidates) == 1:
            passed += 1
            continue
        if not candidates:
            # A zero m_Crc means the catalog intentionally disables CRC
            # validation.  Require an unambiguous filename + source-size match
            # instead of treating the missing non-zero CRC as corruption.
            name_candidates = option_by_name_size.get((matched_final.name.lower(), source_size), [])
            if len(name_candidates) == 1 and int(name_candidates[0].get("m_Crc", 0) or 0) == 0:
                passed += 1
                continue
        failures.append({
            "reason": "catalog_crc_size_not_found" if not candidates else "catalog_crc_size_ambiguous",
            "bundle_name": matched_final.name,
            "actual_crc": actual_crc,
            "actual_hex": f"0x{actual_crc:08x}",
            "source_size": source_size,
            "candidate_count": len(candidates),
            "relative_path": relative_path.as_posix(),
            "source_bundle": str(source_bundle_path),
            "final_bundle": str(matched_final),
        })

    if checked == 0 and skipped_zero_crc == 0:
        failures.append({
            "reason": "no_source_bundle_checked",
            "source_bundle_root": str(source_bundle_root),
            "final_bundle_root": str(final_bundle_root),
        })

    if failures:
        print(f"[catalog][停止] CRC 算法自校验未通过: checked={checked}, passed={passed}, failures={len(failures)}")
        for failure in failures[:10]:
            print(f"[catalog][停止] {failure}")
        if cfg.enable_sample_collection:
            save_catalog_crc_sample(
                cfg,
                output_dir,
                source_bundle_root,
                final_bundle_root,
                failures,
            )
        else:
            print("[catalog][样本] 自动保存已关闭，可通过 enable_sample_collection 启用。")
        return False

    if checked == 0 and skipped_zero_crc > 0:
        _log_green(
            f"[catalog] 匹配到的 bundle 条目均为 m_Crc=0，CRC 校验已禁用，"
            f"无需验证计算算法: skipped={skipped_zero_crc}"
        )
    else:
        _log_green(
            f"[catalog] CRC 算法自校验通过: checked={checked}, passed={passed}, "
            f"skipped_zero_crc={skipped_zero_crc}"
        )
    return True


def export_bundle_crc_with_unity(cfg: PipelineConfig, bundle_root: Path, output_path: Path) -> dict[str, int]:
    launcher = cfg.unity_font_project / "Tools" / "export_bundle_crc.py"
    if not launcher.is_file():
        print(f"[catalog] Unity CRC launcher 不存在，跳过: {launcher}")
        return {}
    if not cfg.unity_exe.is_file():
        print(f"[catalog] Unity 可执行文件不存在，跳过 CRC 自动计算: {cfg.unity_exe}")
        return {}
    if not bundle_root.is_dir():
        print(f"[catalog] 最终 bundle 目录不存在，跳过 CRC 自动计算: {bundle_root}")
        return {}

    command = [
        sys.executable,
        str(launcher),
        "--bundle-root",
        str(bundle_root),
        "--output",
        str(output_path),
        "--unity-exe",
        str(cfg.unity_exe),
        "--project-root",
        str(cfg.unity_font_project),
    ]
    print("[catalog] 正在调用 Unity 计算最终 bundle CRC...", flush=True)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"[catalog] Unity CRC 计算失败，返回码: {result.returncode}")
        return {}
    if not output_path.is_file():
        print(f"[catalog] Unity CRC 输出不存在: {output_path}")
        return {}

    payload = json.loads(output_path.read_text(encoding="utf-8-sig"))
    bundles = payload.get("bundles") if isinstance(payload, dict) else None
    if not isinstance(bundles, list):
        print(f"[catalog] Unity CRC 输出结构异常: {output_path}")
        return {}

    crc_by_name: dict[str, int] = {}
    failed = 0
    for row in bundles:
        if not isinstance(row, dict):
            continue
        if row.get("success") is True:
            name = str(row.get("name", ""))
            crc = row.get("crc")
            if name and isinstance(crc, int):
                crc_by_name[name] = crc
        else:
            failed += 1
    print(f"[catalog] Unity CRC 计算完成: 成功={len(crc_by_name)}, 失败={failed}")
    return crc_by_name


def patch_expanded_catalog_from_final_bundles(
    output_json_path: Path,
    bundle_root: Path,
    log_paths: Iterable[Path] = (),
    crc_by_bundle_name: dict[str, int] | None = None,
    source_bundle_root: Path | None = None,
    update_size: bool = True,
    update_crc: bool = True,
    zero_crc: bool = False,
) -> tuple[int, int, Path]:
    source = output_json_path.resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Output.json 不存在: {source}")
    catalog = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(catalog, dict):
        raise RuntimeError(f"Output.json 不是 JSON 对象: {source}")

    extra = catalog.get("m_ExtraDataString")
    if not isinstance(extra, dict):
        raise RuntimeError("Output.json 中 m_ExtraDataString 不是展开对象，请先解析 catalog")
    options = extra.get("AssetBundleRequestOptions")
    if not isinstance(options, list):
        raise RuntimeError("m_ExtraDataString 中没有 AssetBundleRequestOptions")

    bundle_files = collect_final_bundle_files(bundle_root)
    option_by_crc_size: dict[tuple[int, int], list[dict]] = {}
    for row in options:
        if not isinstance(row, dict):
            continue
        row_crc = row.get("m_Crc")
        row_size = row.get("m_BundleSize")
        if isinstance(row_crc, int) and isinstance(row_size, int):
            option_by_crc_size.setdefault((row_crc, row_size), []).append(row)

    crc_map = dict(crc_by_bundle_name or {})
    if update_crc:
        for name, crc in collect_crc_mismatches_from_logs(log_paths).items():
            crc_map.setdefault(name, crc)
    size_updates = 0
    crc_updates = 0
    matched_rows: set[int] = set()

    if source_bundle_root is not None and source_bundle_root.is_dir():
        source_bundles_by_name: dict[str, list[Path]] = {}
        for source_bundle in sorted(source_bundle_root.rglob("*.bundle")):
            source_bundles_by_name.setdefault(source_bundle.name, []).append(source_bundle)

        name_matched = 0
        name_ambiguous = 0
        name_missing = 0
        for matched_path in sorted(bundle_files.values(), key=lambda path: path.as_posix().lower()):
            source_candidates = source_bundles_by_name.get(matched_path.name, [])
            if not source_candidates:
                name_missing += 1
                continue
            if len(source_candidates) != 1:
                name_ambiguous += 1
                continue
            source_bundle = source_candidates[0]
            try:
                source_crc = calculate_unityfs_uncompressed_crc(source_bundle)
            except Exception:
                name_missing += 1
                continue
            source_size = source_bundle.stat().st_size
            candidates = option_by_crc_size.get((source_crc, source_size), [])
            if len(candidates) != 1:
                if candidates:
                    name_ambiguous += 1
                else:
                    name_missing += 1
                continue

            row = candidates[0]
            matched_rows.add(id(row))
            name_matched += 1
            if update_size:
                new_size = matched_path.stat().st_size
                if row.get("m_BundleSize") != new_size:
                    row["m_BundleSize"] = new_size
                    size_updates += 1

            if update_crc:
                new_crc = 0 if zero_crc else crc_map.get(matched_path.name)
                if new_crc is not None and row.get("m_Crc") != new_crc:
                    row["m_Crc"] = new_crc
                    crc_updates += 1

        print(
            "[catalog] 按新 bundle 文件名反查 catalog: "
            f"命中={name_matched}, 未命中={name_missing}, 歧义={name_ambiguous}"
        )

    for row in options:
        if not isinstance(row, dict):
            continue
        if id(row) in matched_rows:
            continue
        hash_value = str(row.get("m_Hash", ""))
        matched_path: Path | None = None
        for bundle_name, bundle_path in bundle_files.items():
            if hash_value and hash_value in bundle_name:
                matched_path = bundle_path
                break
        if matched_path is None:
            continue

        if update_size:
            new_size = matched_path.stat().st_size
            if row.get("m_BundleSize") != new_size:
                row["m_BundleSize"] = new_size
                size_updates += 1

        if update_crc:
            short_name = matched_path.name
            new_crc = 0 if zero_crc else crc_map.get(short_name)
            if new_crc is not None and row.get("m_Crc") != new_crc:
                row["m_Crc"] = new_crc
                crc_updates += 1

    backup_path = source.with_suffix(source.suffix + ".bak_before_catalog_auto_patch")
    if not backup_path.exists():
        shutil.copy2(source, backup_path)
    source.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    return size_updates, crc_updates, backup_path


def auto_patch_and_repack_catalog_after_import(
    cfg: PipelineConfig,
    final_result_root: Path,
    log_paths: Iterable[Path] = (),
    source_bundle_root: Path | None = None,
) -> Path | None:
    if not cfg.catalog_source_path.is_file():
        _log_green(f"[catalog] 未找到 catalog，无需自动修正，已正常跳过: {cfg.catalog_source_path}")
        return None

    bundle_root = final_result_root / "Bundle" / "Android"
    if not collect_final_bundle_files(bundle_root):
        _log_green(
            f"[catalog] 本次没有修改 bundle 文件，无需修改 catalog，已正常跳过: "
            f"{bundle_root}"
        )
        return None

    output_dir = cfg.result_dir / "catalog"
    expanded_path = output_dir / "Output.json"
    if expanded_path.is_file():
        _log_green(f"[catalog] 复用导出前已展开的 Output.json: {expanded_path}")
    else:
        print(f"[catalog][提示] 未找到导出前的 Output.json，兜底解析: {cfg.catalog_source_path}")
        _formatted_path, expanded_path = parse_catalog_to_output(cfg, cfg.catalog_source_path, output_dir)

    source_bundle_root = source_bundle_root or _source_catalog_android_root(cfg)
    if not validate_catalog_crc_algorithm(cfg, expanded_path, source_bundle_root, bundle_root, output_dir):
        print("[catalog][停止] 未修改 catalog。请先确认 CRC 算法或样本。")
        return None

    manual_crc = {}
    print("[catalog] 已启用 CRC 跳过模式: 命中的 bundle 条目将统一回填 m_Crc=0")
    size_updates, crc_updates, backup_path = patch_expanded_catalog_from_final_bundles(
        expanded_path,
        bundle_root,
        log_paths=log_paths,
        crc_by_bundle_name=manual_crc,
        source_bundle_root=source_bundle_root,
        zero_crc=True,
    )
    _log_green(f"[catalog] 已按最终 bundle 修正 Output.json: size={size_updates}, crc置0={crc_updates}")
    _log_green(f"[catalog] Output.json 自动修正前备份: {backup_path}")

    catalog_source = cfg.catalog_source_path
    remote_report_path = (
        cfg.root_dir
        / "workspace"
        / "resource_state"
        / "addressables_remote_resources.json"
    )
    localized_internal_id_count = 0
    source_catalog_modified = False
    if remote_report_path.is_file():
        try:
            remote_report = json.loads(remote_report_path.read_text(encoding="utf-8-sig"))
            localized_rows = remote_report.get("localized_internal_ids")
            if isinstance(localized_rows, list):
                localized_internal_id_count = len(localized_rows)
            else:
                localized_internal_id_count = int(
                    remote_report.get("localized_internal_id_count", 0) or 0
                )
            source_catalog_modified = bool(remote_report.get("source_catalog_modified"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            localized_internal_id_count = 0
            source_catalog_modified = False
    sync_source_catalog = source_catalog_modified or localized_internal_id_count > 0

    if catalog_source.suffix.lower() == ".bin":
        final_catalog_path = final_result_root / "Bundle" / "catalog.bin"
        final_hash_path = final_result_root / "Bundle" / "catalog.hash"
        repack_result = repack_binary_catalog_from_legacy_output(
            catalog_source,
            expanded_path,
            final_catalog_path,
            final_hash_path,
        )
        _log_green(
            "[catalog.bin] 已按 Output.json 回写: "
            f"InternalId={repack_result['internal_id_updates']}，"
            f"Bundle元数据={repack_result['bundle_option_updates']}"
        )
        _log_green(
            f"[catalog.bin] 已生成 catalog.hash: "
            f"{repack_result['catalog_hash']}"
        )
        if sync_source_catalog:
            shutil.copy2(final_catalog_path, catalog_source)
            shutil.copy2(final_hash_path, catalog_source.with_suffix(".hash"))
            _log_orange(
                f"[源文件已修改] catalog 中有 {localized_internal_id_count} 个远程路径已本地化，"
                f"已用最终 catalog.bin 覆盖源文件: {catalog_source}"
            )
            _log_orange(
                f"[源文件已修改] 已同步覆盖 catalog.hash: "
                f"{catalog_source.with_suffix('.hash')}"
            )
        else:
            _log_blue(
                f"[catalog.bin] 本次没有远程路径被本地化，原项目 catalog 未修改；"
                f"请手动替换: {final_catalog_path} 和 {final_hash_path}"
            )
        return final_catalog_path

    final_catalog_path = final_result_root / "Bundle" / "catalog.json"
    repacked_path = repack_expanded_catalog(expanded_path, final_catalog_path)
    _log_green(f"[catalog] 已回打 catalog 并输出到: {repacked_path}")
    if sync_source_catalog:
        shutil.copy2(repacked_path, catalog_source)
        _log_orange(
            f"[源文件已修改] catalog 中有 {localized_internal_id_count} 个远程路径已本地化，"
            f"已用最终 catalog 覆盖源文件: {catalog_source}"
        )
    else:
        _log_blue(
            f"[catalog] 本次没有远程路径被本地化，原项目 catalog 未修改；"
            f"请手动替换: {repacked_path}"
        )
    return repacked_path
