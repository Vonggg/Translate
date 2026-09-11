"""Find resource fields and metadata regions that are likely encoded data.

The scanner intentionally reports *candidates*, not confirmed encryption.  High
entropy also occurs in compressed media, hashes and random identifiers; the
output keeps the evidence needed for a later IL2CPP/dump.cs investigation.
"""

from __future__ import annotations

import base64
from collections import Counter
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable

from pipeline.shared import atomic_write_json


REPORT_FILENAME = "suspicious_encoded_data_report.json"
DEFAULT_WINDOW_SIZE = 512
DEFAULT_WINDOW_STEP = 256
HIGH_ENTROPY_THRESHOLD = 7.2
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_BASE64_BYTES_RE = re.compile(rb'"[A-Za-z0-9+/]{64,}={0,2}"')
_BINARY_FIELD_NAME_BYTES_RE = re.compile(
    rb'"[^"\\]*(?:blob|bytes?|data|cipher|encrypt(?:ed|ion)?)[^"\\]*"\s*:',
    re.IGNORECASE,
)
_METADATA_OFFSET_RE = re.compile(
    r"(?P<field>[A-Za-z_$][\w$]*)\s*/\*Metadata offset 0x(?P<offset>[0-9A-Fa-f]+)\*/"
)


def shannon_entropy(data: bytes) -> float:
    """Return Shannon entropy in bits per byte (0.0 through 8.0)."""

    if not data:
        return 0.0
    length = len(data)
    counts = Counter(data)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _format_offset(value: int) -> str:
    return f"0x{value:X}"


def _decode_text_encoding(value: str) -> tuple[str, bytes] | None:
    compact = value.strip()
    if len(compact) >= 64 and len(compact) % 4 == 0 and _BASE64_RE.fullmatch(compact):
        try:
            decoded = base64.b64decode(compact, validate=True)
        except ValueError:
            decoded = b""
        if len(decoded) >= 48:
            return "base64", decoded
    if len(compact) >= 96 and len(compact) % 2 == 0 and _HEX_RE.fullmatch(compact):
        try:
            decoded = bytes.fromhex(compact)
        except ValueError:
            decoded = b""
        if len(decoded) >= 48:
            return "hex", decoded
    return None


def _iter_json_values(value: Any, path: str = "$") -> Iterable[tuple[str, bytes, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).replace("\\", "\\\\").replace("'", "\\'")
            yield from _iter_json_values(child, f"{path}['{key_text}']")
        return
    if isinstance(value, list):
        if len(value) >= 64 and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
            yield path, bytes(value), "byte_array"
            return
        for index, child in enumerate(value):
            yield from _iter_json_values(child, f"{path}[{index}]")
        return
    if isinstance(value, str):
        decoded = _decode_text_encoding(value)
        if decoded is not None:
            encoding, data = decoded
            yield path, data, encoding


def scan_json_file(path: Path) -> list[dict[str, Any]]:
    """Return suspicious encoded payloads found inside one exported JSON file."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    matches: list[dict[str, Any]] = []
    for json_path, data, encoding in _iter_json_values(payload):
        entropy = shannon_entropy(data)
        if entropy < HIGH_ENTROPY_THRESHOLD:
            continue
        matches.append(
            {
                "kind": "encoded_json_field",
                "file": str(path),
                "json_path": json_path,
                "encoding_hint": encoding,
                "byte_length": len(data),
                "entropy": round(entropy, 4),
            }
        )
    return matches


def scan_entropy_segments(
    data: bytes,
    *,
    window_size: int = DEFAULT_WINDOW_SIZE,
    step: int = DEFAULT_WINDOW_STEP,
    threshold: float = HIGH_ENTROPY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Group adjacent high-entropy windows and retain their boundary evidence."""

    if len(data) < window_size:
        return []
    windows = [
        (offset, shannon_entropy(data[offset : offset + window_size]))
        for offset in range(0, len(data) - window_size + 1, step)
    ]
    groups: list[list[tuple[int, float]]] = []
    for window in windows:
        if window[1] < threshold:
            continue
        if groups and window[0] <= groups[-1][-1][0] + step:
            groups[-1].append(window)
        else:
            groups.append([window])

    result: list[dict[str, Any]] = []
    for group in groups:
        start = group[0][0]
        end = min(len(data), group[-1][0] + window_size)
        if end - start < window_size:
            continue
        previous_start = max(0, start - window_size)
        next_end = min(len(data), end + window_size)
        before = shannon_entropy(data[previous_start:start]) if start else 0.0
        after = shannon_entropy(data[end:next_end]) if end < len(data) else 0.0
        entropies = [item[1] for item in group]
        result.append(
            {
                "offset": start,
                "end_offset": end,
                "byte_length": end - start,
                "window_count": len(group),
                "average_entropy": round(sum(entropies) / len(entropies), 4),
                "peak_entropy": round(max(entropies), 4),
                "entropy_before": round(before, 4),
                "entropy_after": round(after, 4),
                "boundary_entropy_drop": round(
                    max(0.0, min(entropies) - max(before, after)), 4
                ),
            }
        )
    return result


def _private_implementation_fields(dump_cs_path: Path) -> list[dict[str, Any]]:
    if not dump_cs_path.is_file():
        return []
    try:
        lines = dump_cs_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    namespace = ""
    result: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("// Namespace: "):
            namespace = line.removeprefix("// Namespace: ").strip()
            continue
        if "PrivateImplementationDetails" not in namespace:
            continue
        match = _METADATA_OFFSET_RE.search(line)
        if match:
            result.append(
                {
                    "field": match.group("field"),
                    "metadata_offset": int(match.group("offset"), 16),
                    "namespace": namespace,
                }
            )
    return result


def _annotate_metadata_segments(
    segments: list[dict[str, Any]], fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    for segment in segments:
        start = int(segment["offset"])
        end = int(segment["end_offset"])
        nearest = min(fields, key=lambda field: abs(int(field["metadata_offset"]) - start), default=None)
        if nearest is None:
            continue
        offset = int(nearest["metadata_offset"])
        if start <= offset < end or abs(offset - start) <= DEFAULT_WINDOW_SIZE:
            segment["near_private_implementation_field"] = {
                **nearest,
                "metadata_offset": _format_offset(offset),
            }
    return segments


def _iter_monobehaviour_json_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.rglob("*.json") if path.parent.name.casefold() == "monobehaviour"
    )


def _may_contain_encoded_json_field(path: Path) -> bool:
    """Cheap prefilter so large Unity exports are not all JSON-decoded."""

    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return bool(
        _BASE64_BYTES_RE.search(raw) or _BINARY_FIELD_NAME_BYTES_RE.search(raw)
    )


def scan_project_suspicious_encoded_data(cfg: Any) -> tuple[Path, dict[str, Any]]:
    """Scan exported script fields and global metadata, then write a JSON report."""

    input_root = Path(cfg.resource_input_root)
    metadata_path = Path(cfg.resource_managed_root) / "Metadata" / "global-metadata.dat"
    dump_path = Path(getattr(cfg, "il2cpp_dump_cs_path", metadata_path.with_name("dump.cs")))
    records_dir = Path(cfg.stage_record_dir)
    records_dir.mkdir(parents=True, exist_ok=True)

    all_json_files = list(_iter_monobehaviour_json_files(input_root))
    json_files = [path for path in all_json_files if _may_contain_encoded_json_field(path)]
    json_matches: list[dict[str, Any]] = []
    for path in json_files:
        json_matches.extend(scan_json_file(path))

    metadata_segments: list[dict[str, Any]] = []
    metadata_error: str | None = None
    if metadata_path.is_file():
        try:
            metadata_segments = scan_entropy_segments(metadata_path.read_bytes())
            metadata_segments = _annotate_metadata_segments(
                metadata_segments, _private_implementation_fields(dump_path)
            )
        except OSError as exc:
            metadata_error = str(exc)

    report = {
        "schema_version": 1,
        "scope": "疑似编码/加密数据；仅供逆向定位，不代表已确认加密。",
        "heuristics": {
            "high_entropy_threshold_bits_per_byte": HIGH_ENTROPY_THRESHOLD,
            "metadata_window_size": DEFAULT_WINDOW_SIZE,
            "metadata_window_step": DEFAULT_WINDOW_STEP,
            "json_field_types": ["base64", "hex", "byte_array"],
        },
        "inputs": {
            "workspace_monobehaviour_root": str(input_root),
            "global_metadata": str(metadata_path),
            "dump_cs": str(dump_path),
        },
        "stats": {
            "monobehaviour_json_file_count": len(all_json_files),
            "json_files_selected_for_deep_scan": len(json_files),
            "encoded_json_field_count": len(json_matches),
            "high_entropy_metadata_segment_count": len(metadata_segments),
        },
        "encoded_json_fields": json_matches,
        "high_entropy_metadata_segments": metadata_segments,
    }
    if metadata_error:
        report["metadata_read_error"] = metadata_error
    output_path = records_dir / REPORT_FILENAME
    atomic_write_json(output_path, report)
    return output_path, report


__all__ = [
    "HIGH_ENTROPY_THRESHOLD",
    "REPORT_FILENAME",
    "scan_entropy_segments",
    "scan_json_file",
    "scan_project_suspicious_encoded_data",
    "shannon_entropy",
]
