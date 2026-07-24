from __future__ import annotations

import json
import os
import re
import shutil
import copy
import threading
import time
from fnmatch import fnmatchcase
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .shared import ScanRecord, atomic_write_json, collect_json_files, read_json, unique_preserve_order, write_json
from .manifest_index import build_tmp_manifest_index, tmp_manifest_index_path
from .ai_translation_strategy import get_strategy
from support.config import PipelineConfig


AI_FIELD_REVIEW_MAX_BATCH_BYTES = 150 * 1024
AI_FIELD_REVIEW_MAX_SAMPLES = 6
AI_FIELD_REVIEW_MAX_SAMPLE_CHARS = 300


def _extract_path_id(data: Any) -> int | None:
    if isinstance(data, dict):
        game_object = data.get("m_GameObject")
        if isinstance(game_object, dict) and isinstance(game_object.get("m_PathID"), int):
            return game_object["m_PathID"]
    return None


def _extract_font_path_id(data: dict[str, Any], cfg: PipelineConfig) -> int | None:
    font_pid: int | None = None
    for key, value in data.items():
        if "font" in key.lower() and isinstance(value, dict) and isinstance(value.get("m_PathID"), int):
            return value["m_PathID"]
        if key.lower() == "m_fontdata" and isinstance(value, dict):
            inner = value.get("m_Font")
            if isinstance(inner, dict) and isinstance(inner.get("m_PathID"), int):
                return inner["m_PathID"]

    for key in cfg.font_keys:
        value = data.get(key)
        if isinstance(value, dict) and isinstance(value.get("m_PathID"), int):
            font_pid = value["m_PathID"]
            break
        if key == "m_FontData" and isinstance(value, dict):
            inner = value.get("m_Font")
            if isinstance(inner, dict) and isinstance(inner.get("m_PathID"), int):
                font_pid = inner["m_PathID"]
                break
    return font_pid


MATERIAL_REFERENCE_KEYS = {
    "m_Material",
    "m_sharedMaterial",
    "m_fontMaterial",
    "m_baseMaterial",
    "material",
}


def _extract_material_refs(data: dict[str, Any]) -> list[dict[str, Any]]:
    material_refs: list[dict[str, Any]] = []

    def add_ref(field_name: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        path_id = value.get("m_PathID")
        if not isinstance(path_id, int) or path_id == 0:
            return
        file_id = value.get("m_FileID")
        material_refs.append(
            {
                "field": field_name,
                "file_id": file_id if isinstance(file_id, int) else None,
                "path_id": path_id,
                "material_file": "",
            }
        )

    for key in MATERIAL_REFERENCE_KEYS:
        add_ref(key, data.get(key))
    for key in ("m_fontSharedMaterials", "m_fontMaterials"):
        values = data.get(key, {}).get("Array") if isinstance(data.get(key), dict) else None
        if not isinstance(values, list):
            continue
        for index, value in enumerate(values):
            add_ref(f"{key}.Array[{index}]", value)
    return material_refs


def _collect_refs(node: Any, this_path_id: int | None, ref_map: dict[int, list[int]]) -> None:
    if this_path_id is None:
        return
    if isinstance(node, dict):
        if isinstance(node.get("m_PathID"), int):
            pid = node["m_PathID"]
            if pid != this_path_id:
                ref_map.setdefault(this_path_id, []).append(pid)
        for value in node.values():
            _collect_refs(value, this_path_id, ref_map)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, this_path_id, ref_map)


def _scan_state_path(cfg: PipelineConfig) -> Path:
    return cfg.scan_state_path


def _scan_cache_path(cfg: PipelineConfig, json_path: Path) -> Path:
    relative = json_path.relative_to(cfg.resource_input_root)
    cache_name = f"{relative.name}.scan.json"
    return cfg.scan_cache_path / relative.parent / cache_name


def _scan_bucket_key(cfg: PipelineConfig, json_path: Path) -> str:
    return str(json_path.relative_to(cfg.resource_input_root))


def _bundle_key_for_json_path(cfg: PipelineConfig, json_path: Path) -> str:
    relative = json_path.relative_to(cfg.resource_input_root)
    parts = list(relative.parts)
    for index, part in enumerate(parts):
        if part in {"MonoBehaviour", "TextAsset", "Texture2D", "Material", "Font"}:
            return str(Path(*parts[:index]))
    return str(relative.parent)


def _extract_asset_path_id_from_json_path(json_path: Path) -> int | None:
    stem = json_path.stem
    for part in reversed(stem.split("_")):
        if part.isdigit():
            return int(part)
    return None


def _build_font_asset_index(cfg: PipelineConfig, json_files: list[Path]) -> dict[str, dict[int, Path]]:
    index: dict[str, dict[int, Path]] = {}
    for json_path in json_files:
        asset_path_id = _extract_asset_path_id_from_json_path(json_path)
        if asset_path_id is None:
            continue
        bundle_key = _bundle_key_for_json_path(cfg, json_path)
        index.setdefault(bundle_key, {})[asset_path_id] = json_path
    return index


def _build_path_id_map(cfg: PipelineConfig, json_files: list[Path]) -> dict[str, dict[str, str]]:
    path_id_map: dict[str, dict[str, str]] = {}
    for json_path in json_files:
        asset_path_id = _extract_asset_path_id_from_json_path(json_path)
        if asset_path_id is None:
            continue
        asset_key = _bundle_key_for_json_path(cfg, json_path)
        relative = str(json_path.relative_to(cfg.resource_input_root))
        path_id_map.setdefault(asset_key, {})[str(asset_path_id)] = relative
    return path_id_map


def _write_path_id_map(cfg: PipelineConfig, json_files: list[Path] | None = None) -> Path:
    if json_files is None:
        json_files = collect_json_files(cfg.resource_input_root)
    output_path = cfg.stage_record_dir / cfg.output_path_id_map_json
    write_json(output_path, _build_path_id_map(cfg, json_files))
    return output_path


def _load_file_id_map(cfg: PipelineConfig) -> dict[str, dict[str, str]]:
    path = cfg.stage_record_dir / cfg.output_file_id_map_json
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for asset_key, entry in data.items():
        if not isinstance(asset_key, str) or not isinstance(entry, dict):
            continue
        raw_file_ids = entry.get("file_ids")
        if not isinstance(raw_file_ids, dict):
            continue
        result[asset_key] = {
            str(file_id): target_asset
            for file_id, target_asset in raw_file_ids.items()
            if isinstance(target_asset, str) and target_asset
        }
    return result


def _resolve_font_asset_path(
    cfg: PipelineConfig,
    json_path: Path,
    font_path_id: int,
    font_asset_index: dict[str, dict[int, Path]],
) -> Path | None:
    bundle_key = _bundle_key_for_json_path(cfg, json_path)
    bundle_index = font_asset_index.get(bundle_key)
    if bundle_index and font_path_id in bundle_index:
        return bundle_index[font_path_id]
    for other_bundle_index in font_asset_index.values():
        if font_path_id in other_bundle_index:
            return other_bundle_index[font_path_id]
    return None


def _resolve_input_json_path(cfg: PipelineConfig, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return (cfg.resource_input_root / path).resolve()


def _record_to_dict(record: ScanRecord) -> dict[str, Any]:
    return {
        "file_path": record.file_path,
        "field": record.field,
        "source_text": record.source_text,
        "translated_text": record.translated_text,
        "path_id": record.path_id,
        "font_path_id": record.font_path_id,
    }


def _record_from_dict(data: Any) -> ScanRecord | None:
    if not isinstance(data, dict):
        return None
    file_path = data.get("file_path")
    field = data.get("field")
    source_text = data.get("source_text")
    if not isinstance(file_path, str) or not isinstance(field, str) or not isinstance(source_text, str):
        return None
    translated_text = data.get("translated_text", "")
    path_id = data.get("path_id")
    font_path_id = data.get("font_path_id")
    if path_id is not None and not isinstance(path_id, int):
        path_id = None
    if font_path_id is not None and not isinstance(font_path_id, int):
        font_path_id = None
    if not isinstance(translated_text, str):
        translated_text = ""
    return ScanRecord(
        file_path=file_path,
        field=field,
        source_text=source_text,
        translated_text=translated_text,
        path_id=path_id,
        font_path_id=font_path_id,
    )


def _merge_string_list_map(target: dict[int, list[str]], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, values in source.items():
        try:
            int_key = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        target.setdefault(int_key, []).extend([value for value in values if isinstance(value, str)])


def _merge_scan_ids_map(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    if isinstance(source.get("texts"), list):
        texts = [value for value in source.get("texts", []) if isinstance(value, str)]
        if texts:
            target.setdefault("texts", []).extend(texts)
    if isinstance(source.get("font_texts"), dict):
        font_texts = target.setdefault("font_texts", {})
        _merge_string_list_map(font_texts, source.get("font_texts"))
        return

    legacy_font_texts: dict[int, list[str]] = {}
    _merge_string_list_map(legacy_font_texts, source)
    if legacy_font_texts:
        font_texts = target.setdefault("font_texts", {})
        for path_id, texts in legacy_font_texts.items():
            font_texts.setdefault(str(path_id), []).extend(texts)


def _merge_int_map(target: dict[int, int], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        try:
            int_key = int(key)
        except (TypeError, ValueError):
            continue
        if isinstance(value, int):
            target[int_key] = value


def _merge_ref_map(target: dict[int, list[int]], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, values in source.items():
        try:
            int_key = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        cleaned_values = [value for value in values if isinstance(value, int)]
        if cleaned_values:
            target.setdefault(int_key, []).extend(cleaned_values)


def _add_reverse_ref_map(
    target: dict[str, dict[str, list[dict[str, Any]]]],
    cfg: PipelineConfig,
    json_path: Path,
    source_refs: dict[int, list[int]],
) -> None:
    if not source_refs:
        return
    asset_key = _bundle_key_for_json_path(cfg, json_path)
    source_file = str(json_path.relative_to(cfg.resource_input_root))
    bucket = target.setdefault(asset_key, {})
    for source_path_id, referenced_path_ids in source_refs.items():
        if not isinstance(source_path_id, int) or source_path_id == 0:
            continue
        for referenced_path_id in referenced_path_ids:
            if not isinstance(referenced_path_id, int) or referenced_path_id == 0:
                continue
            entry = {
                "path_id": source_path_id,
                "file": source_file,
            }
            entries = bucket.setdefault(str(referenced_path_id), [])
            if entry not in entries:
                entries.append(entry)


def _is_reverse_ref_map_format(ref_map: Any) -> bool:
    if not isinstance(ref_map, dict):
        return False
    saw_entry = False
    for bucket in ref_map.values():
        if not isinstance(bucket, dict):
            return False
        for entries in bucket.values():
            if not isinstance(entries, list):
                return False
            for entry in entries:
                saw_entry = True
                if not isinstance(entry, dict):
                    return False
                if not isinstance(entry.get("path_id"), int) or not isinstance(entry.get("file"), str):
                    return False
    return saw_entry or not ref_map


def _convert_forward_ref_map_artifact(
    cfg: PipelineConfig,
    forward_ref_map: Any,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    reverse_ref_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if not isinstance(forward_ref_map, dict):
        return reverse_ref_map
    for source_file, source_refs in forward_ref_map.items():
        if not isinstance(source_file, str) or not isinstance(source_refs, dict):
            continue
        json_path = Path(source_file)
        if not json_path.is_absolute():
            json_path = cfg.resource_input_root / json_path
        source_path_id = _extract_asset_path_id_from_json_path(json_path)
        if source_path_id is None:
            continue
        referenced_path_ids: list[int] = []
        for values in source_refs.values():
            if isinstance(values, list):
                referenced_path_ids.extend(value for value in values if isinstance(value, int))
        _add_reverse_ref_map(reverse_ref_map, cfg, json_path, {source_path_id: referenced_path_ids})
    return reverse_ref_map


def _merge_font_usage_entry(
    target: dict[str, dict[str, Any]],
    key: str,
    font_file: str,
    font_path_id: int | None,
    used_by: list[str],
) -> None:
    bucket = target.setdefault(
        key,
        {
            "font_file": font_file,
            "font_path_id": font_path_id,
            "used_by": [],
        },
    )
    if isinstance(font_file, str) and font_file:
        bucket["font_file"] = font_file
    if isinstance(font_path_id, int):
        bucket["font_path_id"] = font_path_id
    bucket.setdefault("used_by", [])
    for item in used_by:
        if isinstance(item, str) and item not in bucket["used_by"]:
            bucket["used_by"].append(item)


def _merge_material_file_entry(
    target: dict[str, dict[str, Any]],
    file_key: str,
    entry: dict[str, Any],
) -> None:
    materials = entry.get("materials")
    if not isinstance(materials, list) or not materials:
        return
    bucket = target.setdefault(
        file_key,
        {
            "file": entry.get("file", file_key),
            "asset": entry.get("asset", ""),
            "path_id": entry.get("path_id"),
            "game_object_path_id": entry.get("game_object_path_id"),
            "texts": [],
            "materials": [],
        },
    )
    for key in ("file", "asset", "path_id", "game_object_path_id"):
        if entry.get(key) not in (None, ""):
            bucket[key] = entry[key]
    bucket.setdefault("texts", [])
    for text in entry.get("texts", []):
        if isinstance(text, str) and text not in bucket["texts"]:
            bucket["texts"].append(text)
    bucket.setdefault("materials", [])
    for material in materials:
        if isinstance(material, dict) and material not in bucket["materials"]:
            bucket["materials"].append(material)


def _merge_material_file_map(
    target: dict[str, dict[str, Any]],
    source: Any,
) -> None:
    if not isinstance(source, dict):
        return
    for file_key, entry in source.items():
        if isinstance(file_key, str) and isinstance(entry, dict):
            _merge_material_file_entry(target, file_key, entry)


def _field_leaf(field_path: str) -> str:
    leaf = field_path.rsplit(".", 1)[-1]
    if "[" in leaf:
        leaf = leaf.split("[", 1)[0]
    return leaf


def _normalize_field_path(field_path: str) -> str:
    return re.sub(r"\[\d+\]", "[]", field_path)


def _is_blacklisted_string_field(cfg: PipelineConfig, field_path: str) -> bool:
    leaf = _field_leaf(field_path)
    for item in cfg.string_field_blacklist:
        if not isinstance(item, str) or not item:
            continue
        if item == field_path or item == leaf or field_path.endswith(f".{item}"):
            return True
        if any(char in item for char in "*?[]") and (fnmatchcase(field_path, item) or fnmatchcase(leaf, item)):
            return True
    return False


def _is_configured_text_field(cfg: PipelineConfig, field_path: str) -> bool:
    leaf = _field_leaf(field_path)
    return leaf in cfg.text_keys or field_path in cfg.text_keys


def _is_ai_selected_text_field(cfg: PipelineConfig, field_path: str, selected_fields: set[str] | None = None) -> bool:
    fields = selected_fields or set()
    normalized = _normalize_field_path(field_path)
    return field_path in fields or normalized in fields


def _iter_reference_types(data: Any) -> Iterable[dict[str, Any]]:
    if not isinstance(data, dict):
        return
    references = data.get("references")
    if not isinstance(references, dict):
        return
    ref_ids = references.get("RefIds", [])
    if isinstance(ref_ids, dict):
        array = ref_ids.get("Array", [])
    elif isinstance(ref_ids, list):
        array = ref_ids
    else:
        return
    if not isinstance(array, list):
        return
    for item in array:
        if not isinstance(item, dict):
            continue
        type_info = item.get("type")
        if isinstance(type_info, dict):
            yield type_info


def _is_unity_or_sdk_assembly(assembly: str) -> bool:
    normalized = assembly.strip()
    if not normalized:
        return False
    prefixes = (
        "Unity",
        "UnityEngine",
        "UnityEditor",
        "TMPro",
        "TextMeshPro",
        "Google",
        "Firebase",
        "Facebook",
        "AppsFlyer",
        "Adjust",
        "IronSource",
        "MaxSdk",
        "Yodo",
        "ByteBrew",
        "Tenjin",
    )
    return normalized.startswith(prefixes)


def _is_unity_or_sdk_namespace(namespace: str) -> bool:
    normalized = namespace.strip()
    if not normalized:
        return False
    prefixes = (
        "UnityEngine.",
        "UnityEditor.",
        "TMPro",
        "Google.",
        "Firebase.",
        "Facebook.",
        "AppsFlyer",
        "Adjust",
        "IronSource",
    )
    return normalized.startswith(prefixes)


def _is_localization_string_table_json(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    table_data = data.get("m_TableData")
    if not isinstance(table_data, dict):
        return False
    rows = table_data.get("Array")
    if not isinstance(rows, list):
        return False
    return any(isinstance(row, dict) and "m_Localized" in row for row in rows)


def _is_i2_language_table_json(data: Any) -> bool:
    """Return true only for I2's term/language table, not ordinary I2 components."""
    if not isinstance(data, dict):
        return False
    source = data.get("mSource")
    if not isinstance(source, dict) or not isinstance(source.get("mTerms"), dict):
        return False
    terms = source["mTerms"].get("Array")
    return isinstance(terms, list) and any(isinstance(term, dict) and "Languages" in term for term in terms)


def _runtime_text_binding_report_path(cfg: PipelineConfig) -> Path:
    return cfg.stage_record_dir / cfg.output_runtime_text_binding_report_json


def _runtime_text_binding_kind(data: Any, record: ScanRecord) -> str | None:
    if _is_i2_language_table_json(data):
        return "i2_language_table"
    if _is_localization_string_table_json(data):
        return "unity_localization_string_table"
    if not isinstance(record.path_id, int) or record.path_id <= 0:
        return "runtime_or_scriptable_text"
    return None


def write_runtime_text_binding_report(cfg: PipelineConfig, records: list[ScanRecord]) -> Path:
    """Record text sources that cannot be reliably linked to a static Text/TMP object."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    data_cache: dict[str, Any] = {}
    for record in records:
        source_path = str(record.file_path)
        if source_path not in data_cache:
            try:
                data_cache[source_path] = read_json(_resolve_input_json_path(cfg, source_path))
            except Exception:
                data_cache[source_path] = None
        kind = _runtime_text_binding_kind(data_cache[source_path], record)
        if kind is None:
            continue
        entry = grouped.setdefault(
            (kind, source_path),
            {
                "kind": kind,
                "file_path": source_path,
                "record_count": 0,
                "source_texts": [],
            },
        )
        entry["record_count"] += 1
        if record.source_text not in entry["source_texts"]:
            entry["source_texts"].append(record.source_text)

    sources = list(grouped.values())
    for source in sources:
        source["text_count"] = len(source["source_texts"])
        source["source_text_samples"] = source["source_texts"][:6]
    sources.sort(key=lambda item: (str(item["kind"]), str(item["file_path"])))
    summary = {
        "i2_language_tables": sum(item["kind"] == "i2_language_table" for item in sources),
        "unity_localization_string_tables": sum(item["kind"] == "unity_localization_string_table" for item in sources),
        "runtime_or_scriptable_sources": sum(item["kind"] == "runtime_or_scriptable_text" for item in sources),
        "runtime_bound_record_count": sum(int(item["record_count"]) for item in sources),
    }
    report_path = _runtime_text_binding_report_path(cfg)
    write_json(report_path, {"summary": summary, "sources": sources})
    _log(
        "[导出] 运行时文本绑定报告已写入: "
        f"{report_path} (I2={summary['i2_language_tables']}, "
        f"Localization={summary['unity_localization_string_tables']}, "
        f"其他运行时来源={summary['runtime_or_scriptable_sources']})"
    )
    return report_path


def _is_unity_or_sdk_owned_json(data: Any, json_path: Path) -> bool:
    path_text = json_path.as_posix().lower()
    unity_path_markers = (
        "globalgamemanagers",
        "unity default resources",
        "localization-locales",
        "localization-assets-shared",
        "addressables",
    )
    if any(marker in path_text for marker in unity_path_markers):
        return True
    for type_info in _iter_reference_types(data):
        assembly = str(type_info.get("asm", ""))
        namespace = str(type_info.get("ns", ""))
        if _is_unity_or_sdk_assembly(assembly) or _is_unity_or_sdk_namespace(namespace):
            return True
    return False


def _should_record_text_from_asset(data: Any, json_path: Path, field_path: str) -> bool:
    if _is_localization_string_table_json(data):
        return fnmatchcase(field_path, "m_TableData.Array*.m_Localized")
    if _is_unity_or_sdk_owned_json(data, json_path):
        return False
    return True


def _add_string_field_stat(
    stats: dict[str, dict[str, Any]],
    cfg: PipelineConfig,
    json_path: Path,
    field_path: str,
    value: str,
) -> None:
    leaf = _field_leaf(field_path)
    normalized_field = _normalize_field_path(field_path)
    bucket = stats.setdefault(
        normalized_field,
        {
            "field": normalized_field,
            "normalized_field": normalized_field,
            "leaf_key": leaf,
            "count": 0,
            "files": [],
            "full_fields": [],
            "sample_values": [],
            "current_text_key": leaf in cfg.text_keys or normalized_field in cfg.text_keys,
            "blacklisted": _is_blacklisted_string_field(cfg, field_path),
        },
    )
    bucket["count"] = int(bucket.get("count", 0)) + 1
    relative = str(json_path.relative_to(cfg.resource_input_root))
    files = bucket.setdefault("files", [])
    if relative not in files and len(files) < 20:
        files.append(relative)
    full_fields = bucket.setdefault("full_fields", [])
    if field_path not in full_fields and len(full_fields) < 30:
        full_fields.append(field_path)
    samples = bucket.setdefault("sample_values", [])
    if value not in samples:
        samples.append(value)


def _merge_string_field_stats(target: dict[str, dict[str, Any]], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for field_path, entry in source.items():
        if not isinstance(field_path, str) or not isinstance(entry, dict):
            continue
        normalized_field = _normalize_field_path(str(entry.get("normalized_field") or field_path))
        bucket = target.setdefault(
            normalized_field,
            {
                "field": normalized_field,
                "normalized_field": normalized_field,
                "leaf_key": entry.get("leaf_key", _field_leaf(normalized_field)),
                "count": 0,
                "files": [],
                "full_fields": [],
                "sample_values": [],
                "current_text_key": bool(entry.get("current_text_key", False)),
                "blacklisted": bool(entry.get("blacklisted", False)),
            },
        )
        bucket["count"] = int(bucket.get("count", 0)) + int(entry.get("count", 0))
        bucket["current_text_key"] = bool(bucket.get("current_text_key")) or bool(entry.get("current_text_key"))
        bucket["blacklisted"] = bool(bucket.get("blacklisted")) or bool(entry.get("blacklisted"))
        for key, limit in (("files", 20), ("full_fields", 30), ("sample_values", 0)):
            values = bucket.setdefault(key, [])
            for value in entry.get(key, []):
                if isinstance(value, str) and value not in values and (limit <= 0 or len(values) < limit):
                    values.append(value)


def _write_string_field_stats(cfg: PipelineConfig, stats: dict[str, dict[str, Any]]) -> None:
    ordered = OrderedDict(
        (field, stats[field])
        for field in sorted(
            stats,
            key=lambda key: (
                bool(stats[key].get("blacklisted", False)),
                not bool(stats[key].get("current_text_key", False)),
                -int(stats[key].get("count", 0)),
                key,
            ),
        )
    )
    write_json(cfg.stage_record_dir / cfg.output_string_field_stats_json, ordered)
    review_lines = [
        "# 请帮我判断这些 Unity JSON 字符串字段是否像会显示给玩家的文本字段。",
        "# 判断依据: 字段名、该字段文本出现次数 count、该字段拥有的 sample_values 文本样本。",
        "# sample_values 规则: 如果样本超过 6 条，只显示前 6 条；单条样本过长会截断；sample_total 表示原本记录的样本总数，sample_shown 表示当前显示条数。",
        "# 字段路径规则: Array[数字] 已归一化为 Array[]，请按输入中的 normalized field 原样返回。",
        "# 返回规则: 只直接给出可能需要翻译的 normalized field 字段路径，用英文逗号 ',' 隔开；不要返回 leaf_key；不要解释，不要编号，不要换行。",
        "# 示例: m_TableData.Array[].m_Localized,rant.Array[].speech",
        "",
    ]
    for field, entry in ordered.items():
        if bool(entry.get("blacklisted", False)) or _is_blacklisted_string_field(cfg, field):
            continue
        sample_values = [str(value) for value in entry.get("sample_values", []) if isinstance(value, str)]
        shown_samples = sample_values[:AI_FIELD_REVIEW_MAX_SAMPLES]
        leaf_key = str(entry.get("leaf_key", ""))
        count = int(entry.get("count", 0))
        review_lines.append(f"field: {field}")
        review_lines.append(f"leaf_key: {leaf_key}")
        review_lines.append(f"count: {count}")
        review_lines.append(
            f"sample_values: sample_total={len(sample_values)}, "
            f"sample_shown={len(shown_samples)}"
        )
        for sample in shown_samples:
            safe_sample = sample.replace("\t", " ").replace("\n", "\\n").replace("\r", "\\r")
            if len(safe_sample) > AI_FIELD_REVIEW_MAX_SAMPLE_CHARS:
                safe_sample = safe_sample[:AI_FIELD_REVIEW_MAX_SAMPLE_CHARS] + "...[truncated]"
            review_lines.append(f"- {safe_sample}")
        review_lines.append("")
    (cfg.stage_record_dir / cfg.output_string_field_review_txt).write_text("\n".join(review_lines), encoding="utf-8")
    rows = ["field\tleaf_key\tcount\tcurrent_text_key\tblacklisted\tsample_values\tfull_fields\tfiles"]
    for entry in ordered.values():
        sample_values = " | ".join(str(value).replace("\t", " ").replace("\n", "\\n") for value in entry.get("sample_values", []))
        full_fields = " | ".join(str(value).replace("\t", " ") for value in entry.get("full_fields", []))
        files = " | ".join(str(value).replace("\t", " ") for value in entry.get("files", []))
        rows.append(
            "\t".join(
                [
                    str(entry.get("field", "")),
                    str(entry.get("leaf_key", "")),
                    str(entry.get("count", 0)),
                    str(bool(entry.get("current_text_key", False))),
                    str(bool(entry.get("blacklisted", False))),
                    sample_values,
                    full_fields,
                    files,
                ]
            )
        )
    (cfg.stage_record_dir / cfg.output_string_field_stats_tsv).write_text("\n".join(rows), encoding="utf-8")


def _build_file_material_entry(
    cfg: PipelineConfig,
    json_path: Path,
    data: dict[str, Any],
    path_id_map: dict[str, dict[str, str]] | None = None,
    file_id_map: dict[str, dict[str, str]] | None = None,
    texts: list[str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    material_refs = _extract_material_refs(data)
    if not material_refs:
        return None
    asset_key = _bundle_key_for_json_path(cfg, json_path)
    relative = str(json_path.relative_to(cfg.resource_input_root))
    this_path_id = _extract_asset_path_id_from_json_path(json_path)
    for material_ref in material_refs:
        file_id = material_ref.get("file_id")
        path_id = material_ref.get("path_id")
        target_asset = asset_key
        if isinstance(file_id, int) and file_id != 0:
            target_asset = (file_id_map or {}).get(asset_key, {}).get(str(file_id), "")
        material_ref["asset"] = target_asset
        if path_id_map is not None and target_asset and isinstance(path_id, int):
            resolved_file = path_id_map.get(target_asset, {}).get(str(path_id))
            if resolved_file:
                material_ref["material_file"] = resolved_file
    return (
        relative,
        {
            "file": relative,
            "asset": asset_key,
            "path_id": this_path_id,
            "game_object_path_id": _extract_path_id(data),
            "texts": texts or [],
            "materials": material_refs,
        },
    )


def _load_scan_state(cfg: PipelineConfig) -> dict[str, Any] | None:
    state_path = _scan_state_path(cfg)
    if not state_path.is_file():
        return None
    try:
        state = read_json(state_path)
    except Exception:
        return None
    return state if isinstance(state, dict) else None


def _write_scan_state(cfg: PipelineConfig, json_path: Path, state: str) -> None:
    atomic_write_json(_scan_state_path(cfg), {"state": state, "last_file": str(json_path)})


def _clear_scan_cache(cfg: PipelineConfig) -> None:
    if cfg.scan_cache_path.exists():
        shutil.rmtree(cfg.scan_cache_path)


def _preload_scan_cache(
    cfg: PipelineConfig,
    json_files: list[Path],
    start_index: int,
    records: list[ScanRecord],
    ids_map: dict[str, dict[str, Any]],
    font_map: dict[str, dict[str, Any]],
    material_map: dict[str, dict[str, Any]],
    ref_map: dict[str, dict[str, list[dict[str, Any]]]],
    string_field_stats: dict[str, dict[str, Any]],
) -> None:
    for json_path in json_files[:start_index]:
        cache_path = _scan_cache_path(cfg, json_path)
        if not cache_path.is_file():
            continue
        try:
            cached = read_json(cache_path)
        except Exception:
            continue
        if not isinstance(cached, dict):
            continue
        records_data = cached.get("records", [])
        if isinstance(records_data, list):
            for item in records_data:
                record = _record_from_dict(item)
                if record is not None:
                    records.append(record)
        bucket_key = _scan_bucket_key(cfg, json_path)
        flat_ref: dict[int, list[int]] = {}
        _merge_ref_map(flat_ref, cached.get("ref_map"))
        _merge_scan_ids_map(ids_map.setdefault(bucket_key, {}), cached.get("ids_map"))
        _merge_string_field_stats(string_field_stats, cached.get("string_field_stats"))
        if flat_ref:
            _add_reverse_ref_map(ref_map, cfg, json_path, flat_ref)
        cached_font_map = cached.get("font_map")
        if isinstance(cached_font_map, dict):
            for key, value in cached_font_map.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                used_by = value.get("used_by")
                _merge_font_usage_entry(
                    font_map,
                    key,
                    value.get("font_file", key) if isinstance(value.get("font_file"), str) else key,
                    value.get("font_path_id") if isinstance(value.get("font_path_id"), int) else None,
                    used_by if isinstance(used_by, list) else [],
                )
        cached_material_map = cached.get("material_map")
        _merge_material_file_map(material_map, cached_material_map)


def _write_scan_cache(
    cfg: PipelineConfig,
    json_path: Path,
    records: list[ScanRecord],
    ids_map: dict[str, Any],
    font_map: dict[str, dict[str, Any]],
    material_map: dict[str, dict[str, Any]],
    ref_map: dict[int, list[int]],
    string_field_stats: dict[str, dict[str, Any]],
) -> None:
    cache_path = _scan_cache_path(cfg, json_path)
    payload = {
        "json_path": str(json_path),
        "records": [_record_to_dict(record) for record in records],
        "ids_map": ids_map,
        "font_map": font_map,
        "material_map": material_map,
        "ref_map": ref_map,
        "string_field_stats": string_field_stats,
    }
    atomic_write_json(cache_path, payload)


def _write_scan_artifacts(
    cfg: PipelineConfig,
    records: list[ScanRecord],
    ids_map: dict[str, dict[str, Any]],
    font_map: dict[str, dict[str, Any]],
    material_map: dict[str, dict[str, Any]],
    ref_map: dict[str, dict[str, list[dict[str, Any]]]],
    string_field_stats: dict[str, dict[str, Any]] | None = None,
) -> None:
    cfg.stage_record_dir.mkdir(parents=True, exist_ok=True)
    write_json(cfg.stage_record_dir / cfg.output_scan_records_json, [_record_to_dict(record) for record in records])
    write_json(cfg.stage_record_dir / cfg.output_ids_json, ids_map)
    write_json(cfg.stage_record_dir / cfg.output_font_map_json, font_map)
    write_json(cfg.stage_record_dir / cfg.output_material_map_json, material_map)
    write_json(cfg.stage_record_dir / cfg.output_ref_map_json, ref_map)
    if string_field_stats is not None:
        _write_string_field_stats(cfg, string_field_stats)


def _resolve_scan_resume_index(json_files: list[Path], state: dict[str, Any] | None) -> int:
    if not state:
        return 0
    if state.get("state") == "completed":
        return len(json_files)
    last_file = state.get("last_file")
    if not isinstance(last_file, str) or not last_file:
        return 0
    last_path = Path(last_file).resolve()
    for index, json_path in enumerate(json_files):
        if json_path.resolve() == last_path:
            return index
    return -1


def _scan_one_translation_json(
    cfg: PipelineConfig,
    json_path: Path,
    font_asset_index: dict[str, dict[int, Path]],
    path_id_map: dict[str, dict[str, str]],
    file_id_map: dict[str, dict[str, str]],
) -> dict[str, Any]:
    bucket_key = _scan_bucket_key(cfg, json_path)
    file_records: list[ScanRecord] = []
    file_ids_map: dict[str, Any] = {"texts": [], "font_texts": {}}
    file_font_map: dict[str, dict[str, Any]] = {}
    file_material_map: dict[str, dict[str, Any]] = {}
    file_ref_map: dict[int, list[int]] = {}
    file_string_field_stats: dict[str, dict[str, Any]] = {}
    try:
        data = read_json(json_path)
    except Exception as exc:
        return {
            "json_path": json_path,
            "bucket_key": bucket_key,
            "records": file_records,
            "ids_map": file_ids_map,
            "font_map": file_font_map,
            "material_map": file_material_map,
            "ref_map": file_ref_map,
            "string_field_stats": file_string_field_stats,
            "error": str(exc),
        }

    game_object_path_id = _extract_path_id(data)
    this_path_id = _extract_asset_path_id_from_json_path(json_path)
    font_pid: int | None = None
    resolved_font_path: Path | None = None
    if isinstance(data, dict) and game_object_path_id is not None:
        font_pid = _extract_font_path_id(data, cfg)
        if font_pid is not None:
            resolved_font_path = _resolve_font_asset_path(cfg, json_path, font_pid, font_asset_index)
            font_key = str(resolved_font_path) if resolved_font_path is not None else f"{_bundle_key_for_json_path(cfg, json_path)}#{font_pid}"
            file_font_map.setdefault(
                font_key,
                {
                    "font_file": str(resolved_font_path) if resolved_font_path is not None else font_key,
                    "font_path_id": font_pid,
                    "used_by": [],
                },
            )
        _collect_refs(data, this_path_id, file_ref_map)

    def add_text_record(field: str, value: str) -> None:
        if value in cfg.ignore_text or len(value.strip()) <= 1:
            return
        if not _should_record_text_from_asset(data, json_path, field):
            return
        file_records.append(
            ScanRecord(
                file_path=str(json_path),
                field=field,
                source_text=value,
                path_id=game_object_path_id,
                font_path_id=font_pid,
            )
        )
        file_ids_map.setdefault("texts", []).append(value)

    def walk(node: Any, field_path: str = "", in_text_field: bool = False) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{field_path}.{key}" if field_path else str(key)
                blacklisted = _is_blacklisted_string_field(cfg, child_path)
                if isinstance(value, str):
                    if blacklisted:
                        continue
                    _add_string_field_stat(file_string_field_stats, cfg, json_path, child_path, value)
                    if cfg.enable_ai_field_review or in_text_field or _is_configured_text_field(cfg, child_path):
                        add_text_record(child_path, value)
                else:
                    if blacklisted:
                        continue
                    walk(value, child_path, (not cfg.enable_ai_field_review) and (in_text_field or _is_configured_text_field(cfg, child_path)))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                child_path = f"{field_path}[{index}]"
                blacklisted = _is_blacklisted_string_field(cfg, child_path)
                if blacklisted:
                    continue
                if isinstance(item, str):
                    _add_string_field_stat(file_string_field_stats, cfg, json_path, child_path, item)
                    if cfg.enable_ai_field_review or in_text_field:
                        add_text_record(child_path, item)
                else:
                    walk(item, child_path, in_text_field)

    walk(data)

    if isinstance(data, dict) and isinstance(data.get("m_fontAsset"), dict):
        path_id = data["m_fontAsset"].get("m_PathID")
        text = data.get("m_text", "")
        if isinstance(path_id, int):
            file_ids_map.setdefault("font_texts", {}).setdefault(str(path_id), []).append(text)
    if font_pid is not None:
        font_key = str(resolved_font_path) if resolved_font_path is not None else f"{_bundle_key_for_json_path(cfg, json_path)}#{font_pid}"
        _merge_font_usage_entry(
            file_font_map,
            font_key,
            str(resolved_font_path) if resolved_font_path is not None else font_key,
            font_pid,
            [str(json_path)],
        )
    if isinstance(data, dict):
        file_texts = unique_preserve_order(record.source_text for record in file_records)
        material_entry = _build_file_material_entry(cfg, json_path, data, path_id_map, file_id_map, file_texts)
        if material_entry is not None:
            file_key, entry = material_entry
            _merge_material_file_entry(file_material_map, file_key, entry)

    return {
        "json_path": json_path,
        "bucket_key": bucket_key,
        "records": file_records,
        "ids_map": file_ids_map,
        "font_map": file_font_map,
        "material_map": file_material_map,
        "ref_map": file_ref_map,
        "string_field_stats": file_string_field_stats,
        "error": None,
    }


def _scan_worker_count(total_files: int, cfg: PipelineConfig) -> int:
    cpu_count = os.cpu_count() or 4
    if total_files < 200:
        auto_count = 1
    else:
        auto_count = max(2, min(12, cpu_count * 2))
    if cfg.max_scan_workers > 0:
        return max(1, min(auto_count, cfg.max_scan_workers))
    return auto_count


def scan_translation_inputs(cfg: PipelineConfig) -> tuple[list[ScanRecord], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]]:
    records: list[ScanRecord] = []
    ids_map: dict[str, dict[str, Any]] = {}
    font_map: dict[str, dict[str, Any]] = {}
    material_map: dict[str, dict[str, Any]] = {}
    ref_map: dict[str, dict[str, list[dict[str, Any]]]] = {}
    string_field_stats: dict[str, dict[str, Any]] = {}

    json_files = collect_json_files(cfg.resource_input_root)
    _log(f"[扫描] 共发现 {len(json_files)} 个 JSON 文件")
    font_asset_index = _build_font_asset_index(cfg, json_files)
    path_id_map = _build_path_id_map(cfg, json_files)
    file_id_map = _load_file_id_map(cfg)
    if file_id_map:
        _log(f"[扫描] 已读取 FileID 资源映射: {len(file_id_map)} 个资源文件")
    else:
        _log("[扫描] 未找到 FileID 资源映射，外部 file_id 材质引用可能无法精确解析")
    state = _load_scan_state(cfg)
    if state is None:
        _log("[扫描] 未发现状态文件，将从头开始")
    if state is None and cfg.scan_cache_path.exists():
        shutil.rmtree(cfg.scan_cache_path)

    start_index = _resolve_scan_resume_index(json_files, state)
    if start_index < 0:
        _log("[扫描] 记录文件无效，已重新从头开始")
        _clear_scan_cache(cfg)
        start_index = 0
        state = None
    elif state is not None:
        last_file = state.get("last_file")
        scan_state = state.get("state", "unknown")
        _log(f"[扫描] 读取到状态: state={scan_state}, last_file={last_file}")
        _log(f"[扫描] 断点位置: {start_index}/{len(json_files)}")

    _preload_scan_cache(cfg, json_files, start_index, records, ids_map, font_map, material_map, ref_map, string_field_stats)
    if start_index > 0:
        _log(f"[扫描] 已预载前 {start_index} 个文件的缓存")

    remaining_files = json_files[start_index:]
    total_remaining = len(remaining_files)
    worker_count = _scan_worker_count(total_remaining, cfg)
    _log(f"[扫描] 并发扫描线程数: {worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for offset, result in enumerate(
            executor.map(lambda path: _scan_one_translation_json(cfg, path, font_asset_index, path_id_map, file_id_map), remaining_files),
            start=1,
        ):
            json_path = result["json_path"]
            bucket_key = result["bucket_key"]
            _write_scan_state(cfg, json_path, "interrupted")
            if offset == 1 or offset % 200 == 0 or offset == total_remaining:
                _log(f"[扫描] {offset}/{total_remaining} {json_path}")
            if result["error"]:
                _log(f"[跳过] 读取失败: {json_path} ({result['error']})")
                continue

            file_records = result["records"]
            file_ids_map = result["ids_map"]
            file_font_map = result["font_map"]
            file_material_map = result["material_map"]
            file_ref_map = result["ref_map"]
            file_string_field_stats = result["string_field_stats"]
            records.extend(file_records)
            if file_records:
                _merge_scan_ids_map(ids_map.setdefault(bucket_key, {}), file_ids_map)
            _merge_string_field_stats(string_field_stats, file_string_field_stats)
            if file_ref_map:
                _add_reverse_ref_map(ref_map, cfg, json_path, file_ref_map)

            for key, value in file_font_map.items():
                _merge_font_usage_entry(
                    font_map,
                    key,
                    value.get("font_file", key) if isinstance(value.get("font_file"), str) else key,
                    value.get("font_path_id") if isinstance(value.get("font_path_id"), int) else None,
                    value.get("used_by", []) if isinstance(value.get("used_by"), list) else [],
                )

            _merge_material_file_map(material_map, file_material_map)

            _write_scan_cache(cfg, json_path, file_records, file_ids_map, file_font_map, file_material_map, file_ref_map, file_string_field_stats)
    if json_files:
        _write_scan_state(cfg, json_files[-1], "completed")
        _log(f"[扫描] 已完成，状态已写入 completed: {json_files[-1]}")
    else:
        _write_scan_state(cfg, cfg.resource_input_root, "completed")
        _log("[扫描] 没有可扫描文件，状态已写入 completed")

    _write_scan_artifacts(
        cfg,
        records,
        ids_map,
        font_map,
        material_map,
        ref_map,
        string_field_stats if cfg.enable_ai_field_review else None,
    )
    if cfg.enable_ai_field_review:
        _log(
            f"[扫描] AI 字段判断已启用，字符串字段统计已写入: {cfg.stage_record_dir / cfg.output_string_field_stats_json} / "
            f"{cfg.stage_record_dir / cfg.output_string_field_stats_tsv}"
        )
    path_id_map_path = _write_path_id_map(cfg, json_files)
    _log(f"[扫描] PathID 文件索引已写入: {path_id_map_path}")
    return records, ids_map, font_map, ref_map


def _material_map_path(cfg: PipelineConfig) -> Path:
    return cfg.stage_record_dir / cfg.output_material_map_json


def _load_material_map(cfg: PipelineConfig) -> dict[str, dict[str, Any]] | None:
    path = _material_map_path(cfg)
    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except Exception:
        return None
    result: dict[str, dict[str, Any]] = {}
    _merge_material_file_map(result, data)
    return result


def _scan_one_material_json(
    cfg: PipelineConfig,
    json_path: Path,
    path_id_map: dict[str, dict[str, str]],
    file_id_map: dict[str, dict[str, str]],
    texts_by_file: dict[str, list[str]] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    try:
        data = read_json(json_path)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    relative = str(json_path.relative_to(cfg.resource_input_root))
    return _build_file_material_entry(cfg, json_path, data, path_id_map, file_id_map, (texts_by_file or {}).get(relative, []))


def _material_texts_from_scan_records(cfg: PipelineConfig) -> dict[str, list[str]]:
    scan_artifacts = _load_scan_artifacts(cfg)
    if scan_artifacts is None:
        return {}
    records, _ids_map, _font_map, _ref_map = scan_artifacts
    texts_by_file: dict[str, list[str]] = {}
    for record in records:
        try:
            relative = str(_resolve_input_json_path(cfg, record.file_path).relative_to(cfg.resource_input_root))
        except ValueError:
            continue
        bucket = texts_by_file.setdefault(relative, [])
        if record.source_text not in bucket:
            bucket.append(record.source_text)
    return texts_by_file


def rebuild_material_map(cfg: PipelineConfig) -> dict[str, dict[str, Any]]:
    json_files = collect_json_files(cfg.resource_input_root)
    path_id_map = _build_path_id_map(cfg, json_files)
    file_id_map = _load_file_id_map(cfg)
    texts_by_file = _material_texts_from_scan_records(cfg)
    material_map: dict[str, dict[str, Any]] = {}
    worker_count = _scan_worker_count(len(json_files), cfg)
    _log(f"[扫描] 开始补充生成 material_map.json，JSON 文件数: {len(json_files)}，线程数: {worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for index, result in enumerate(
            executor.map(lambda path: _scan_one_material_json(cfg, path, path_id_map, file_id_map, texts_by_file), json_files),
            start=1,
        ):
            if index == 1 or index % 2000 == 0 or index == len(json_files):
                _log(f"[扫描] material_map 进度: {index}/{len(json_files)}")
            if result is None:
                continue
            file_key, entry = result
            _merge_material_file_entry(material_map, file_key, entry)
    cfg.stage_record_dir.mkdir(parents=True, exist_ok=True)
    write_json(_material_map_path(cfg), material_map)
    _log(f"[扫描] 材质使用表已写入: {_material_map_path(cfg)}，记录文件数: {len(material_map)}")
    return material_map


def _translate_baidu(text: str, cfg: PipelineConfig) -> str:
    from hashlib import md5
    import random
    import requests

    if not cfg.baidu_appid or not cfg.baidu_appkey:
        raise RuntimeError("Baidu translation is selected but appid/appkey are not configured.")

    from_lang = "en"
    to_lang = "zh"
    endpoint = "http://api.fanyi.baidu.com"
    url = endpoint + "/api/trans/vip/translate"

    salt = random.randint(32768, 65536)
    sign = md5((cfg.baidu_appid + text + str(salt) + cfg.baidu_appkey).encode("utf-8")).hexdigest()
    payload = {"appid": cfg.baidu_appid, "q": text, "from": from_lang, "to": to_lang, "salt": salt, "sign": sign}
    response = requests.post(url, params=payload, headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=60)
    response.raise_for_status()
    result = response.json()
    if result.get("error_msg") == "INVALID QUERY":
        return ""
    if result.get("error_code") or result.get("error_msg"):
        raise RuntimeError(f"Baidu translation error: {result.get('error_code', '')} {result.get('error_msg', '')}".strip())
    trans_result = result.get("trans_result")
    if not isinstance(trans_result, list):
        raise RuntimeError(f"Baidu translation response missing trans_result: {result}")
    return "\n".join(item["dst"] for item in trans_result if isinstance(item, dict) and isinstance(item.get("dst"), str))


def _translate_google(text: str, cfg: PipelineConfig) -> str:
    import html
    import re
    from urllib import parse

    import requests

    proxies = {"http": cfg.google_proxy_http, "https": cfg.google_proxy_https}
    query = parse.quote(text)
    url = f"https://translate.google.com/m?q={query}&tl=zh-CN&sl=auto"
    response = requests.get(url, proxies=proxies, timeout=60)
    matches = re.findall(r'(?s)class="(?:t0|result-container)">(.*?)<', response.text)
    return html.unescape(matches[0]) if matches else ""


def _translate_ai(text: str, cfg: PipelineConfig) -> str:
    import requests

    base_url = cfg.ai_translation_base_url.strip().rstrip("/")
    api_key = cfg.ai_translation_api_key.strip()
    model = cfg.ai_translation_model.strip()
    if not (base_url and api_key and model):
        raise RuntimeError("AI translation is enabled but base_url/api_key/model is not configured.")

    proxies = {
        key: value
        for key, value in {
            "http": cfg.ai_translation_proxy_http.strip(),
            "https": cfg.ai_translation_proxy_https.strip(),
        }.items()
        if value
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是游戏逆向汉化翻译助手。用户输入是从游戏资源中导出的文本，"
                    "目标是制作简体中文汉化，不是只翻译英文；任何语言都要翻译成简体中文。"
                    "繁体中文必须转换为简体中文，不能因为已经是中文就原样保留。"
                    "如果原始文本本身含有中文，译文中的所有中文字符也必须是简体中文，不得夹杂繁体字。"
                    "语言名称也要汉化，例如 Español 译为西班牙语、Français 译为法语、日本語译为日语、한국어译为韩语。"
                    "如果不同语言文本表达的是同一句话或同一个 UI 含义，要翻译成一致的简体中文说法。"
                    "保持游戏 UI 文本自然简洁。"
                    "保留换行、占位符、数字、货币符号、格式控制符和富文本标签。"
                    "只输出译文，不要解释。"
                ),
            },
            {"role": "user", "content": text},
        ],
        "temperature": 0,
    }
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        proxies=proxies or None,
        timeout=cfg.ai_translation_timeout,
    )
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return str(content).strip()


def _ai_translation_request_configured(cfg: PipelineConfig) -> bool:
    return bool(
        cfg.enable_ai_translation
        and cfg.ai_translation_base_url.strip()
        and cfg.ai_translation_api_key.strip()
        and cfg.ai_translation_model.strip()
    )


def _translate_ai_batch(
    batch: list[tuple[int, str]],
    cfg: PipelineConfig,
    strategy: Any,
    batch_index: int,
    batch_count: int,
) -> dict[int, str]:
    import requests

    base_url = cfg.ai_translation_base_url.strip().rstrip("/")
    api_key = cfg.ai_translation_api_key.strip()
    model = cfg.ai_translation_model.strip()
    if not (base_url and api_key and model):
        raise RuntimeError("AI translation is enabled but base_url/api_key/model is not configured.")

    user_content = strategy.user_content(batch, batch_index, batch_count)
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": strategy.system_prompt(),
            },
            {"role": "user", "content": user_content},
        ],
    }
    payload.update(strategy.extra_payload())
    cfg.stage_record_dir.mkdir(parents=True, exist_ok=True)
    request_name = "ai_translation_request.json" if batch_count == 1 else f"ai_translation_request_batch_{batch_index:03d}.json"
    request_path = cfg.stage_record_dir / request_name
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[翻译] AI 请求内容已写入: {request_path}")
    proxies = {
        key: value
        for key, value in {
            "http": cfg.ai_translation_proxy_http.strip(),
            "https": cfg.ai_translation_proxy_https.strip(),
        }.items()
        if value
    }
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        proxies=proxies or None,
        timeout=cfg.ai_translation_timeout,
    )
    _log(f"[翻译] AI 批量接口已响应: batch={batch_index}/{batch_count}, HTTP {response.status_code}")
    response.raise_for_status()
    data = response.json()
    response_name = "ai_translation_response.json" if batch_count == 1 else f"ai_translation_response_batch_{batch_index:03d}.json"
    response_path = cfg.stage_record_dir / response_name
    response_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"[翻译] AI 返回内容已写入: {response_path}")
    usage = data.get("usage", {})
    if isinstance(usage, dict):
        hit = usage.get("prompt_cache_hit_tokens")
        miss = usage.get("prompt_cache_miss_tokens")
        if hit is not None or miss is not None:
            _log(f"[翻译] AI 缓存命中: batch={batch_index}/{batch_count}, hit={hit}, miss={miss}")
    content = str(data["choices"][0]["message"]["content"])
    return strategy.parse_response(content)


def _translate_configured_provider(text: str, cfg: PipelineConfig) -> str:
    if cfg.translate_provider == "google":
        return _translate_google(text, cfg)
    return _translate_baidu(text, cfg)


def translate_text(text: str, cfg: PipelineConfig) -> str:
    mode = cfg.translation_mode.strip().lower()
    if mode in {"copy", "none", "原文"}:
        return text
    if mode and mode != "translate":
        raise RuntimeError(f"Unknown translation_mode: {cfg.translation_mode}")
    if cfg.enable_ai_translation:
        try:
            translated = _translate_ai(text, cfg)
            if translated:
                return translated
            raise RuntimeError("AI translation returned empty text.")
        except Exception as exc:
            _log(f"[翻译] AI 翻译失败，回落到 {cfg.translate_provider}: {_format_log_text(text)} ({exc})")
    return _translate_configured_provider(text, cfg)


def _translate_worker_count(total_texts: int, cfg: PipelineConfig) -> int:
    if total_texts < 20:
        auto_count = 1
    else:
        auto_count = 4
    if cfg.max_translate_workers > 0:
        return max(1, min(auto_count, cfg.max_translate_workers))
    return auto_count


def _translate_one_text(cfg: PipelineConfig, source_text: str) -> tuple[str, str]:
    translated = translate_text(source_text, cfg)
    return source_text, translated or source_text


def _translate_one_text_with_provider(cfg: PipelineConfig, source_text: str) -> tuple[str, str]:
    translated = _translate_configured_provider(source_text, cfg)
    return source_text, translated or source_text


def _translate_one_text_with_provider_retry(cfg: PipelineConfig, source_text: str, max_attempts: int = 3) -> tuple[str, str]:
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            translated = _translate_configured_provider(source_text, cfg)
            return source_text, translated or source_text
        except Exception as exc:
            last_exc = exc
            _log(
                f"[翻译] {cfg.translate_provider} 请求失败，重试 {attempt}/{max_attempts}: "
                f"{_format_log_text(source_text)} ({exc})"
            )
    raise RuntimeError(f"{cfg.translate_provider} 翻译连续失败 {max_attempts} 次: {last_exc}")


def _ordered_translations(source_texts: list[str], translations: dict[str, str]) -> OrderedDict[str, str]:
    return OrderedDict((text, translations[text]) for text in source_texts if text in translations)


def _format_log_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _log(message: str) -> None:
    print(message, flush=True)


def _log_blue(message: str) -> None:
    print(f"\033[94m{message}\033[0m", flush=True)


def _log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def _log_dark_green(message: str) -> None:
    print(f"\033[32m{message}\033[0m", flush=True)


def _log_field_list(title: str, fields: list[str] | tuple[str, ...]) -> None:
    _log(f"{title}: {len(fields)} 个")
    if not fields:
        return
    width = max(2, len(str(len(fields))))
    for index, field in enumerate(fields, start=1):
        _log(f"  {index:>{width}}. {field}")


def _start_wait_logger(prefix: str, interval_seconds: int = 30) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def run() -> None:
        start = time.monotonic()
        while not stop_event.wait(interval_seconds):
            elapsed = int(time.monotonic() - start)
            _log(f"{prefix} 仍在等待 AI 响应... 已等待 {elapsed} 秒")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return stop_event, thread


def build_translation_map(records: list[ScanRecord], cfg: PipelineConfig) -> OrderedDict[str, str]:
    source_texts = unique_preserve_order(record.source_text for record in records)
    cache_path = cfg.stage_record_dir / cfg.output_trans_json
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    translations: OrderedDict[str, str] = OrderedDict((text, "") for text in source_texts)
    atomic_write_json(cache_path, dict(translations))
    _log(f"[翻译] 已依据 records.json 重新生成空 trans.json 任务表: {cache_path}，键数={len(translations)}")

    failed_fallbacks: OrderedDict[str, str] = OrderedDict()
    pending_items: list[tuple[int, str]] = list(enumerate(source_texts))
    _log(f"[翻译] 待翻译去重文本数: {len(source_texts)}")

    if cfg.enable_ai_translation and pending_items:
        if _ai_translation_request_configured(cfg):
            strategy = get_strategy(cfg)
            batches = strategy.build_batches(pending_items)
            _log(
                f"[翻译] AI 分批翻译已启用: strategy={getattr(strategy, 'name', 'custom')}，"
                f"待发送 trans key 数={len(pending_items)}，批次={len(batches)}，"
                f"单批预计输出预算={getattr(strategy, 'batch_output_budget_chars', 'unknown')}"
            )
            ai_translated_ids: set[int] = set()
            for batch_index, batch in enumerate(batches, start=1):
                batch_size = len(strategy.user_content(batch, batch_index, len(batches)))
                estimated_output = sum(strategy.estimate_output_chars(text) for _index, text in batch)
                _log_green(
                    f"[翻译] 开始 AI batch={batch_index}/{len(batches)}，"
                    f"条目={len(batch)}，输入字符={batch_size}，预计输出={estimated_output}"
                )
                wait_stop, wait_thread = _start_wait_logger(f"[翻译] AI batch={batch_index}/{len(batches)}")
                try:
                    batch_result = _translate_ai_batch(batch, cfg, strategy, batch_index, len(batches))
                except Exception as exc:
                    _log_dark_green(
                        f"[翻译] AI batch={batch_index}/{len(batches)} "
                        f"失败，将本批回落到 {cfg.translate_provider}: {exc}"
                    )
                    continue
                finally:
                    wait_stop.set()
                    wait_thread.join(timeout=1)
                for item_index, source_text in batch:
                    translated = batch_result.get(item_index)
                    if not translated:
                        continue
                    translations[source_text] = translated
                    ai_translated_ids.add(item_index)
                ordered_cache = _ordered_translations(source_texts, translations)
                atomic_write_json(cache_path, dict(ordered_cache))
                _log_blue(
                    f"[翻译] AI batch={batch_index}/{len(batches)} 完成，"
                    f"返回={len(batch_result)}，累计AI成功={len(ai_translated_ids)}"
                )
            if ai_translated_ids:
                pending_items = [(index, text) for index, text in pending_items if index not in ai_translated_ids]
                _log(f"[翻译] AI 批量翻译成功: {len(ai_translated_ids)} 条；剩余回落请求: {len(pending_items)} 条")
        else:
            _log(f"[翻译] AI 翻译已启用但配置不完整，将直接回落到 {cfg.translate_provider}。")

    _log(f"[翻译] 待回落逐条请求文本数: {len(pending_items)}，每条最多重试 3 次")
    for completed, (index, source_text) in enumerate(pending_items, start=1):
        try:
            _, translated = _translate_one_text_with_provider_retry(cfg, source_text, max_attempts=3)
        except Exception as exc:
            failed_fallbacks[source_text] = str(exc)
            _log(
                f"[翻译] {cfg.translate_provider} 连续失败，已中断翻译: "
                f"({index + 1}/{len(source_texts)}) {_format_log_text(source_text)} ({exc})"
            )
            raise
        translations[source_text] = translated
        ordered_cache = _ordered_translations(source_texts, translations)
        atomic_write_json(cache_path, dict(ordered_cache))
        _log(
            f"[翻译] 回落 {completed}/{len(pending_items)} "
            f"({index + 1}/{len(source_texts)}) {_format_log_text(source_text)} -> {_format_log_text(translated)}"
        )

    translations = _ordered_translations(source_texts, translations)

    if failed_fallbacks:
        _log(f"[翻译] 有 {len(failed_fallbacks)} 条请求失败，已使用原文兜底。")

    _log(f"[翻译] 翻译缓存已更新: {cache_path}")
    return translations


def apply_translations_to_json(
    node: Any,
    cfg: PipelineConfig,
    translations: dict[str, str],
    in_text_field: bool = False,
    field_path: str = "",
) -> Any:
    if isinstance(node, str):
        if _is_blacklisted_string_field(cfg, field_path):
            return node
        return translations.get(node, node) if (cfg.enable_ai_field_review or in_text_field) else node
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{field_path}.{key}" if field_path else str(key)
            blacklisted = _is_blacklisted_string_field(cfg, child_path)
            is_text_field = in_text_field or _is_configured_text_field(cfg, child_path)
            if isinstance(value, str) and not blacklisted and (cfg.enable_ai_field_review or is_text_field) and value in translations:
                node[key] = translations[value]
            else:
                node[key] = apply_translations_to_json(
                    value,
                    cfg,
                    translations,
                    (not cfg.enable_ai_field_review) and (is_text_field and not blacklisted),
                    child_path,
                )
        return node
    if isinstance(node, list):
        result = []
        for index, item in enumerate(node):
            child_path = f"{field_path}[{index}]"
            if isinstance(item, str):
                if _is_blacklisted_string_field(cfg, child_path):
                    result.append(item)
                else:
                    result.append(translations.get(item, item) if (cfg.enable_ai_field_review or in_text_field) else item)
            else:
                result.append(apply_translations_to_json(item, cfg, translations, in_text_field, child_path))
        return result
    return node


def _zero_color_alpha(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    changed = False
    for key in ("a", "m_A", "alpha"):
        if key in value and value[key] != 0:
            value[key] = 0
            changed = True
    return changed


def _zero_vector(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    changed = False
    for key in ("x", "y", "z", "w", "m_X", "m_Y", "m_Z", "m_W"):
        if key in value and value[key] != 0:
            value[key] = 0
            changed = True
    return changed


def _disable_shadow_or_outline_component(data: Any) -> int:
    if not isinstance(data, dict):
        return 0
    if "m_EffectColor" not in data and "m_EffectDistance" not in data:
        return 0

    changes = 0
    if data.get("m_Enabled") != 0:
        data["m_Enabled"] = 0
        changes += 1
    if _zero_color_alpha(data.get("m_EffectColor")):
        changes += 1
    if _zero_vector(data.get("m_EffectDistance")):
        changes += 1
    if data.get("m_UseGraphicAlpha") not in (None, 0):
        data["m_UseGraphicAlpha"] = 0
        changes += 1
    return changes


MATERIAL_EFFECT_FLOAT_ZERO_KEYS = {
    "_OutlineWidth",
    "_OutlineSoftness",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_UnderlayDilate",
    "_UnderlaySoftness",
    "_UnderlayOffset",
    "_GlowOffset",
    "_GlowInner",
    "_GlowOuter",
    "_GlowPower",
}

MATERIAL_EFFECT_COLOR_ALPHA_ZERO_KEYS = {
    "_OutlineColor",
    "_UnderlayColor",
    "_GlowColor",
}

# Generic scene shaders also commonly expose _OutlineWidth/_OutlineColor.  A
# material must have several TMP SDF-specific properties before we ever alter it.
TMP_SDF_MATERIAL_MARKERS = {
    "_FaceColor",
    "_FaceDilate",
    "_GradientScale",
    "_ScaleRatioA",
    "_ScaleRatioB",
    "_ScaleRatioC",
    "_TextureWidth",
    "_TextureHeight",
    "_WeightNormal",
    "_WeightBold",
}


def _material_property_names(node: Any, names: set[str]) -> None:
    if isinstance(node, dict):
        pair_name = node.get("first")
        if not isinstance(pair_name, str):
            pair_name = node.get("name") if isinstance(node.get("name"), str) else None
        if isinstance(pair_name, str):
            names.add(pair_name)
        for key, value in node.items():
            if isinstance(key, str):
                names.add(key)
            _material_property_names(value, names)
    elif isinstance(node, list):
        for item in node:
            _material_property_names(item, names)


def _is_tmp_sdf_material_json(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    names: set[str] = set()
    _material_property_names(data, names)
    markers = TMP_SDF_MATERIAL_MARKERS.intersection(names)
    return "_FaceColor" in markers and len(markers) >= 3


def _zero_number(value: Any) -> tuple[Any, bool]:
    if isinstance(value, bool):
        return value, False
    if isinstance(value, (int, float)) and value != 0:
        return 0, True
    return value, False


def _disable_material_property_value(property_name: str, value: Any) -> tuple[Any, int]:
    if property_name in MATERIAL_EFFECT_FLOAT_ZERO_KEYS:
        new_value, changed = _zero_number(value)
        return new_value, 1 if changed else 0
    if property_name in MATERIAL_EFFECT_COLOR_ALPHA_ZERO_KEYS and isinstance(value, dict):
        return value, 1 if _zero_color_alpha(value) else 0
    return value, 0


def _disable_material_effect_properties(node: Any) -> int:
    changes = 0
    if isinstance(node, dict):
        pair_name = node.get("first")
        if not isinstance(pair_name, str):
            pair_name = node.get("name") if isinstance(node.get("name"), str) else None
        if isinstance(pair_name, str):
            for value_key in ("second", "value", "m_Value"):
                if value_key not in node:
                    continue
                node[value_key], changed = _disable_material_property_value(pair_name, node[value_key])
                changes += changed

        for key, value in list(node.items()):
            if isinstance(key, str):
                node[key], changed = _disable_material_property_value(key, value)
                changes += changed
            changes += _disable_material_effect_properties(node[key])
    elif isinstance(node, list):
        for item in node:
            changes += _disable_material_effect_properties(item)
    return changes


def _load_material_usage_map(cfg: PipelineConfig) -> dict[str, dict[str, Any]]:
    material_map = _load_material_map(cfg)
    if material_map is None:
        _log("[阴影描边] material_map.json 不存在，补充生成一次")
        material_map = rebuild_material_map(cfg)
    return material_map


def _build_unique_material_path_id_index(
    path_id_map: dict[str, dict[str, str]],
) -> dict[int, str]:
    candidates: dict[int, set[str]] = {}
    for bucket in path_id_map.values():
        for path_id, relative in bucket.items():
            if not isinstance(relative, str) or "\\Material\\" not in relative:
                continue
            try:
                numeric_path_id = int(path_id)
            except ValueError:
                continue
            candidates.setdefault(numeric_path_id, set()).add(relative)
    return {
        path_id: next(iter(paths))
        for path_id, paths in candidates.items()
        if len(paths) == 1
    }


def _material_paths_for_translated_records(
    cfg: PipelineConfig,
    records: list[ScanRecord],
    translations: dict[str, str],
    material_map: dict[str, dict[str, Any]],
    path_id_map: dict[str, dict[str, str]],
    file_id_map: dict[str, dict[str, str]],
) -> dict[str, list[dict[str, Any]]]:
    unique_materials = _build_unique_material_path_id_index(path_id_map)
    material_sources: dict[str, list[dict[str, Any]]] = {}
    translated_files: set[str] = set()
    for record in records:
        if record.source_text not in translations:
            continue
        try:
            translated_files.add(str(_resolve_input_json_path(cfg, record.file_path).relative_to(cfg.resource_input_root)))
        except ValueError:
            continue

    for text_file in translated_files:
        entry = material_map.get(text_file)
        if not isinstance(entry, dict):
            continue
        asset_key = entry.get("asset") if isinstance(entry.get("asset"), str) else ""
        materials = entry.get("materials")
        if not isinstance(materials, list):
            continue
        for material in materials:
            if not isinstance(material, dict):
                continue
            relative = material.get("material_file") if isinstance(material.get("material_file"), str) else ""
            path_id = material.get("path_id")
            file_id = material.get("file_id")
            target_asset = material.get("asset") if isinstance(material.get("asset"), str) else ""
            if not target_asset and isinstance(file_id, int) and file_id != 0:
                target_asset = file_id_map.get(asset_key, {}).get(str(file_id), "")
            if not relative and file_id in (None, 0) and isinstance(path_id, int):
                relative = path_id_map.get(asset_key, {}).get(str(path_id), "")
            if not relative and target_asset and isinstance(path_id, int):
                relative = path_id_map.get(target_asset, {}).get(str(path_id), "")
            if not relative and isinstance(path_id, int):
                relative = unique_materials.get(path_id, "")
            if not relative:
                continue
            material_sources.setdefault(relative, []).append(
                {
                    "text_file": text_file,
                    "field": material.get("field", ""),
                    "file_id": file_id,
                    "path_id": path_id,
                }
            )
    return material_sources


def _all_text_effect_material_paths(cfg: PipelineConfig) -> dict[str, list[dict[str, Any]]]:
    """Find every TMP SDF Material that has outline, underlay, or glow properties."""
    effect_names = MATERIAL_EFFECT_FLOAT_ZERO_KEYS | MATERIAL_EFFECT_COLOR_ALPHA_ZERO_KEYS
    material_sources: dict[str, list[dict[str, Any]]] = {}
    for json_path in collect_json_files(cfg.resource_input_root):
        if json_path.parent.name.lower() != "material":
            continue
        try:
            raw_text = json_path.read_text(encoding="utf-8-sig", errors="ignore")
            data = json.loads(raw_text)
        except Exception:
            continue
        if not any(name in raw_text for name in effect_names) or not _is_tmp_sdf_material_json(data):
            continue
        relative = str(json_path.relative_to(cfg.resource_input_root))
        material_sources[relative] = [{"text_file": "<runtime-binding fallback>", "field": "", "file_id": None, "path_id": None}]
    return material_sources


def _remove_stale_global_material_overlays(cfg: PipelineConfig, remove_tmp: bool = False) -> int:
    """Remove old all-material fallback outputs; optionally retain valid TMP overlays."""
    record_path = cfg.stage_record_dir / cfg.output_disabled_effect_components_json
    if not record_path.is_file():
        return 0
    try:
        record_data = read_json(record_path)
    except Exception:
        return 0
    materials = record_data.get("materials") if isinstance(record_data, dict) else None
    if not isinstance(materials, list):
        return 0
    removed = 0
    for item in materials:
        if not isinstance(item, dict):
            continue
        used_by = item.get("used_by")
        if not isinstance(used_by, list) or not any(
            isinstance(source, dict) and source.get("text_file") == "<runtime-binding fallback>"
            for source in used_by
        ):
            continue
        relative = item.get("file")
        output_file = item.get("output_file")
        if not isinstance(relative, str) or not isinstance(output_file, str):
            continue
        try:
            source_data = read_json(_resolve_input_json_path(cfg, relative))
        except Exception:
            continue
        if not remove_tmp and _is_tmp_sdf_material_json(source_data):
            continue
        output_path = cfg.stage_dir / output_file
        if output_path.is_file():
            output_path.unlink()
            removed += 1
    if removed:
        detail = "全部旧全量材质覆盖层" if remove_tmp else "旧版全量工具误写入的非 TMP 材质"
        _log_blue(f"[材质阴影描边] 已清理{detail}: {removed} 个")
    return removed


def _load_runtime_binding_sources_for_translations(
    cfg: PipelineConfig,
    translations: dict[str, str],
) -> list[dict[str, Any]]:
    report_path = _runtime_text_binding_report_path(cfg)
    if not report_path.is_file():
        return []
    try:
        report = read_json(report_path)
    except Exception:
        return []
    sources = report.get("sources") if isinstance(report, dict) else None
    if not isinstance(sources, list):
        return []
    return [
        item
        for item in sources
        if isinstance(item, dict)
        and isinstance(item.get("source_texts"), list)
        and any(isinstance(text, str) and text in translations for text in item["source_texts"])
    ]


def _i2_bound_game_objects_for_translations(
    cfg: PipelineConfig,
    translations: dict[str, str],
) -> set[tuple[str, int]]:
    """Find GameObjects whose I2 Localize term is present in trans.json."""
    result: set[tuple[str, int]] = set()
    for json_path in collect_json_files(cfg.resource_input_root):
        try:
            raw_text = json_path.read_text(encoding="utf-8-sig", errors="ignore")
        except OSError:
            continue
        if '"mTerm"' not in raw_text or '"mLocalizeTargetName"' not in raw_text:
            continue
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        term = data.get("mTerm")
        target_name = data.get("mLocalizeTargetName")
        game_object = data.get("m_GameObject")
        if (
            not isinstance(term, str)
            or term not in translations
            or not isinstance(target_name, str)
            or "Text" not in target_name
            or not isinstance(game_object, dict)
            or not isinstance(game_object.get("m_PathID"), int)
            or game_object["m_PathID"] <= 0
        ):
            continue
        result.add((_bundle_key_for_json_path(cfg, json_path), game_object["m_PathID"]))
    return result


def _load_path_id_map(cfg: PipelineConfig) -> dict[str, dict[str, str]]:
    path = cfg.stage_record_dir / cfg.output_path_id_map_json
    if not path.is_file():
        _log("[阴影描边] path_id_map.json 不存在，补充生成一次")
        _write_path_id_map(cfg)
    try:
        data = read_json(path)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for asset_key, bucket in data.items():
        if not isinstance(asset_key, str) or not isinstance(bucket, dict):
            continue
        result[asset_key] = {
            str(path_id): relative
            for path_id, relative in bucket.items()
            if isinstance(relative, str)
        }
    return result


def write_translation_outputs(
    cfg: PipelineConfig,
    records: list[ScanRecord],
    translations: dict[str, str],
    ids_map: dict[str, dict[str, Any]],
    font_map: dict[str, dict[str, Any]],
    ref_map: dict[str, Any],
) -> None:
    cfg.stage_record_dir.mkdir(parents=True, exist_ok=True)
    write_json(cfg.stage_record_dir / cfg.output_scan_records_json, [_record_to_dict(record) for record in records])
    write_json(cfg.stage_record_dir / cfg.output_trans_json, dict(translations))
    write_json(cfg.stage_record_dir / cfg.output_ids_json, ids_map)
    write_json(cfg.stage_record_dir / cfg.output_font_map_json, font_map)
    write_json(cfg.stage_record_dir / cfg.output_ref_map_json, ref_map)
    rebuild_game_text_outputs(cfg, translations)
    mapping_rows = ["source\ttranslated\tfile\tfield\tpath_id\tfont_path_id"]
    for record in records:
        mapping_rows.append(
            "\t".join(
                [
                    record.source_text.replace("\t", " ").replace("\n", "\\n"),
                    translations.get(record.source_text, record.source_text).replace("\t", " ").replace("\n", "\\n"),
                    record.file_path,
                    record.field,
                    "" if record.path_id is None else str(record.path_id),
                    "" if record.font_path_id is None else str(record.font_path_id),
                ]
            )
    )
    (cfg.stage_record_dir / cfg.output_mapping_tsv).write_text("\n".join(mapping_rows), encoding="utf-8")


def rebuild_game_text_outputs(
    cfg: PipelineConfig,
    translations: dict[str, str] | OrderedDict[str, str] | None = None,
) -> None:
    trans_path = cfg.stage_record_dir / cfg.output_trans_json
    game_txt_path = cfg.stage_record_dir / cfg.output_game_txt
    game_chars_path = cfg.stage_record_dir / cfg.output_game_chars_txt
    loaded_from_file = translations is None

    if translations is None:
        _log(f"[重建文本] 读取 trans.json: {trans_path}")
        translations = _load_translation_dict(cfg)
        if translations is None:
            raise FileNotFoundError(f"trans.json not found: {trans_path}")
    else:
        _log("[重建文本] 使用内存中的翻译结果生成 game.txt/game_chars.txt")

    total = len(translations)
    empty_values = sum(1 for value in translations.values() if not str(value))
    ordered_values = unique_preserve_order(translations.values())
    char_text = "".join(unique_preserve_order("".join(translations.keys()) + "".join(translations.values())))

    _log(
        f"[重建文本] trans 条目={total}，game.txt 去重行={len(ordered_values)}，"
        f"空译文={empty_values}，字符数={len(char_text)}"
    )
    game_txt_path.parent.mkdir(parents=True, exist_ok=True)
    game_txt_path.write_text("\n".join(ordered_values), encoding="utf-8")
    _log(f"[重建文本] 已写入 game.txt: {game_txt_path}")
    game_chars_path.write_text(char_text, encoding="utf-8")
    _log(f"[重建文本] 已写入 game_chars.txt: {game_chars_path}")
    if loaded_from_file:
        _log("[重建文本] 完成: 已从 trans.json 重建 game.txt 和 game_chars.txt")


def _load_completed_translation_artifacts(cfg: PipelineConfig) -> tuple[OrderedDict[str, str], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]] | None:
    trans_path = cfg.stage_record_dir / cfg.output_trans_json
    ids_path = cfg.stage_record_dir / cfg.output_ids_json
    font_map_path = cfg.stage_record_dir / cfg.output_font_map_json
    ref_map_path = cfg.stage_record_dir / cfg.output_ref_map_json
    if not (trans_path.is_file() and ids_path.is_file() and font_map_path.is_file() and ref_map_path.is_file()):
        return None
    try:
        cached_translations = read_json(trans_path)
        cached_ids = read_json(ids_path)
        cached_font_map = read_json(font_map_path)
        cached_ref_map = read_json(ref_map_path)
    except Exception:
        return None
    if not isinstance(cached_translations, dict):
        return None
    translations = OrderedDict(
        (str(key), str(value))
        for key, value in cached_translations.items()
        if isinstance(key, str) and isinstance(value, str)
    )
    ids_map = cached_ids if isinstance(cached_ids, dict) else {}
    font_map = cached_font_map if isinstance(cached_font_map, dict) else {}
    ref_map = cached_ref_map if isinstance(cached_ref_map, dict) else {}
    return translations, ids_map, font_map, ref_map


def _load_scan_artifacts(cfg: PipelineConfig) -> tuple[list[ScanRecord], dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any]] | None:
    records_path = cfg.stage_record_dir / cfg.output_scan_records_json
    ids_path = cfg.stage_record_dir / cfg.output_ids_json
    font_map_path = cfg.stage_record_dir / cfg.output_font_map_json
    ref_map_path = cfg.stage_record_dir / cfg.output_ref_map_json
    if not (records_path.is_file() and ids_path.is_file() and font_map_path.is_file() and ref_map_path.is_file()):
        return None
    try:
        raw_records = read_json(records_path)
        cached_ids = read_json(ids_path)
        cached_font_map = read_json(font_map_path)
        cached_ref_map = read_json(ref_map_path)
    except Exception:
        return None
    if not isinstance(raw_records, list):
        return None
    records = [record for item in raw_records if (record := _record_from_dict(item)) is not None]
    ids_map = cached_ids if isinstance(cached_ids, dict) else {}
    font_map = cached_font_map if isinstance(cached_font_map, dict) else {}
    ref_map = cached_ref_map if isinstance(cached_ref_map, dict) else {}
    return records, ids_map, font_map, ref_map


def _load_translation_dict(cfg: PipelineConfig) -> OrderedDict[str, str] | None:
    trans_path = cfg.stage_record_dir / cfg.output_trans_json
    if not trans_path.is_file():
        return None
    try:
        cached_translations = read_json(trans_path)
    except Exception:
        return None
    if not isinstance(cached_translations, dict):
        return None
    return OrderedDict(
        (str(key), str(value))
        for key, value in cached_translations.items()
        if isinstance(key, str) and isinstance(value, str)
    )


def _parse_ai_field_response(raw: str) -> list[str]:
    fields: list[str] = []
    for part in raw.replace("\n", ",").replace("，", ",").split(","):
        field = _normalize_field_path(part.strip().strip("\"'`"))
        if field and field not in fields:
            fields.append(field)
    return fields


def _manual_ai_field_selection(cfg: PipelineConfig, candidates_path: Path, reason: str = "") -> list[str]:
    if reason:
        print(f"[AI字段] {reason}")
    print(f"[AI字段] 请把字段候选文件交给 AI 判断: {candidates_path}")
    raw = input("\033[38;5;208m[AI字段] 粘贴 AI 返回的字段名，使用英文逗号分隔: \033[0m").strip()
    return _parse_ai_field_response(raw)


def _split_ai_field_review_batches(prompt: str, max_bytes: int = AI_FIELD_REVIEW_MAX_BATCH_BYTES) -> list[str]:
    lines = prompt.splitlines()
    header: list[str] = []
    blocks: list[list[str]] = []
    current_block: list[str] | None = None

    for line in lines:
        if line.startswith("field:"):
            if current_block is not None:
                blocks.append(current_block)
            current_block = [line]
            continue
        if current_block is None:
            header.append(line)
        else:
            current_block.append(line)
    if current_block is not None:
        blocks.append(current_block)

    if not blocks:
        return [prompt]

    header_text = "\n".join(header).strip() + "\n\n"
    batches: list[str] = []
    current_text = header_text
    current_has_block = False

    for block in blocks:
        block_text = "\n".join(block).rstrip() + "\n\n"
        candidate_text = current_text + block_text
        if current_has_block and len(candidate_text.encode("utf-8")) > max_bytes:
            batches.append(current_text.rstrip() + "\n")
            current_text = header_text + block_text
            current_has_block = True
        else:
            current_text = candidate_text
            current_has_block = True

    if current_has_block:
        batches.append(current_text.rstrip() + "\n")
    return batches


def _post_ai_field_review_batch(
    cfg: PipelineConfig,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    batch_index: int,
    batch_count: int,
) -> list[str]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 Unity 游戏汉化字段筛选助手。"
                    "只判断字段名是否可能是会显示给玩家的文本字段。"
                    "输入中的 Array[数字] 已归一化为 Array[]。"
                    "只返回输入中出现过的 normalized field 字段路径，不要返回 leaf_key 或短字段名。"
                    "用英文逗号分隔；不要解释，不要编号，不要换行。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    import requests

    proxies = {
        key: value
        for key, value in {
            "http": cfg.ai_field_review_proxy_http.strip(),
            "https": cfg.ai_field_review_proxy_https.strip(),
        }.items()
        if value
    }
    session = requests.Session()
    session.trust_env = False
    response = session.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        proxies=proxies or None,
        timeout=cfg.ai_field_review_timeout,
    )
    if response.status_code == 200:
        _log_green(f"[AI字段] AI 接口已响应: batch={batch_index}/{batch_count}, HTTP {response.status_code}")
    else:
        _log(f"[AI字段] AI 接口已响应: batch={batch_index}/{batch_count}, HTTP {response.status_code}")
    response.raise_for_status()
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_ai_field_response(str(content))


def _request_ai_field_selection(cfg: PipelineConfig, candidates_path: Path) -> list[str]:
    base_url = cfg.ai_field_review_base_url.strip().rstrip("/")
    api_key = cfg.ai_field_review_api_key.strip()
    model = cfg.ai_field_review_model.strip()
    if not (base_url and api_key and model):
        return _manual_ai_field_selection(cfg, candidates_path, "未配置 AI 接口 base_url/api_key/model，将使用人工判断。")
    if not candidates_path.is_file():
        raise FileNotFoundError(f"字段候选文件不存在: {candidates_path}")

    prompt = candidates_path.read_text(encoding="utf-8")
    candidate_lines = [
        line
        for line in prompt.splitlines()
        if line.strip().startswith("field:")
    ]
    print(
        f"[AI字段] 自动请求 AI 字段判断: model={model}, base_url={base_url}, "
        f"候选字段={len(candidate_lines)}, 文件大小={candidates_path.stat().st_size} bytes",
        flush=True,
    )
    batches = _split_ai_field_review_batches(prompt)
    if len(batches) > 1:
        print(
            f"[AI字段] 字段候选将分批发送: {len(batches)} 批，"
            f"每批上限={AI_FIELD_REVIEW_MAX_BATCH_BYTES} bytes；同一 field 不拆分。",
            flush=True,
        )
    try:
        fields: list[str] = []
        for index, batch_prompt in enumerate(batches, start=1):
            batch_size = len(batch_prompt.encode("utf-8"))
            batch_field_count = sum(
                1 for line in batch_prompt.splitlines()
                if line.strip().startswith("field:")
            )
            _log(
                f"[AI字段] 开始发送 batch={index}/{len(batches)}，"
                f"字段={batch_field_count}，大小={batch_size} bytes"
            )
            batch_fields = _post_ai_field_review_batch(
                cfg,
                base_url,
                api_key,
                model,
                batch_prompt,
                index,
                len(batches),
            )
            _log(
                f"[AI字段] AI batch={index}/{len(batches)} 返回字段数: {len(batch_fields)}，"
                f"发送大小={batch_size} bytes"
            )
            for field in batch_fields:
                if field not in fields:
                    fields.append(field)
        if not fields:
            return _manual_ai_field_selection(cfg, candidates_path, "AI 接口返回了空字段列表，将使用人工判断。")
        _log_field_list("[AI字段] AI 自动返回字段", fields)
        return fields
    except Exception as exc:
        return _manual_ai_field_selection(cfg, candidates_path, f"AI 接口访问失败，将使用人工判断: {exc}")


def apply_ai_field_selection_to_records(cfg: PipelineConfig) -> None:
    if not cfg.enable_ai_field_review:
        print("[AI字段] config.json 未启用 enable_ai_field_review，本步骤不执行。")
        return
    scan_artifacts = _load_scan_artifacts(cfg)
    if scan_artifacts is None:
        raise FileNotFoundError("未找到完整扫描记录，请先执行菜单 0。")

    candidates_path = cfg.stage_record_dir / cfg.output_string_field_review_txt
    if not candidates_path.is_file():
        raise FileNotFoundError(
            f"字段候选文件不存在: {candidates_path}\n"
            "当 enable_ai_field_review=true 时，需要先运行脚本 0 扫描导出的 JSON，"
            "脚本 0 才会生成 string_field_review.txt。"
        )
    selected_fields = _request_ai_field_selection(cfg, candidates_path)
    if not selected_fields:
        raise ValueError("AI 字段列表为空，未修改 records.json。")

    selected_set = set(selected_fields)
    records, _ids_map, font_map, ref_map = scan_artifacts
    record_fields = unique_preserve_order(record.field for record in records)
    matched_fields = [field for field in selected_fields if any(_is_ai_selected_text_field(cfg, record_field, {field}) for record_field in record_fields)]
    missing_fields = [field for field in selected_fields if field not in matched_fields]
    kept_records = [record for record in records if _is_ai_selected_text_field(cfg, record.field, selected_set)]
    removed_count = len(records) - len(kept_records)
    _log(
        f"[AI字段] records 字段统计: 记录数={len(records)}，唯一字段={len(record_fields)}，"
        f"AI返回字段={len(selected_fields)}，命中字段={len(matched_fields)}，未命中字段={len(missing_fields)}"
    )
    if missing_fields:
        _log_field_list("[AI字段] AI 返回但 records 中未命中的字段", missing_fields)
    if selected_fields and len(matched_fields) < len(selected_fields) and len(record_fields) <= len(matched_fields):
        raise RuntimeError(
            "AI 返回字段与 records.json 明显不匹配。当前 records 很可能来自旧白名单扫描。"
            "请重新执行菜单 0，并选择清空旧记录后再执行菜单 1。"
        )
    ids_map: dict[str, dict[str, Any]] = {}
    for record in kept_records:
        try:
            bucket_key = _scan_bucket_key(cfg, _resolve_input_json_path(cfg, record.file_path))
        except ValueError:
            bucket_key = str(record.file_path)
        ids_map.setdefault(bucket_key, {}).setdefault("texts", []).append(record.source_text)

    material_map = _load_material_map(cfg) or {}
    _write_scan_artifacts(cfg, kept_records, ids_map, font_map, material_map, ref_map)
    _log(
        f"[AI字段] 已按字段过滤 records.json: 原记录={len(records)}，"
        f"保留={len(kept_records)}，删除={removed_count}"
    )
    _log_field_list("[AI字段] 本次保留字段", selected_fields)


def _load_ids_target_paths(cfg: PipelineConfig) -> list[Path]:
    ids_path = cfg.stage_record_dir / cfg.output_ids_json
    if not ids_path.is_file():
        return []
    try:
        cached_ids = read_json(ids_path)
    except Exception:
        return []
    if not isinstance(cached_ids, dict):
        return []
    target_paths: list[Path] = []
    for key in cached_ids.keys():
        if not isinstance(key, str) or not key:
            continue
        target_paths.append(_resolve_input_json_path(cfg, key))
    return target_paths


def _target_paths_from_records_and_translations(
    cfg: PipelineConfig,
    records: list[ScanRecord],
    translations: dict[str, str],
) -> list[Path]:
    seen: set[Path] = set()
    target_paths: list[Path] = []
    for record in records:
        if record.source_text not in translations:
            continue
        path = Path(record.file_path)
        if path in seen:
            continue
        seen.add(path)
        target_paths.append(path)
    return target_paths


def _is_localization_shared_data_json(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("m_Entries"), dict)
        and "m_TableCollectionName" in data
        and "m_TableCollectionNameGuidString" in data
        and "m_KeyGenerator" in data
        and "m_TableData" not in data
    )


def _export_translated_files(cfg: PipelineConfig, final_translations: dict[str, str], target_paths: list[Path] | None = None) -> None:
    cfg.translated_dump_dir.mkdir(parents=True, exist_ok=True)
    json_files = target_paths if target_paths is not None else collect_json_files(cfg.resource_input_root)
    _log(f"[导出] 开始套用 trans.json 到 {cfg.translated_dump_dir}，文件数: {len(json_files)}")
    written_count = 0
    for index, json_path in enumerate(json_files, start=1):
        try:
            data = read_json(json_path)
        except Exception:
            _log(f"[导出] 读取失败，已跳过: {json_path}")
            continue
        if _is_localization_shared_data_json(data):
            _log(f"[导出][跳过] Localization Shared Data 不导出翻译: {json_path.relative_to(cfg.resource_input_root)}")
            relative = json_path.relative_to(cfg.resource_input_root)
            stale_output = cfg.translated_dump_dir / relative
            if stale_output.exists():
                stale_output.unlink()
                _log(f"[导出][清理] 已删除旧的 Shared Data 待导入文件: {stale_output}")
            continue
        original = copy.deepcopy(data)
        translated = apply_translations_to_json(data, cfg, final_translations)
        if translated == original:
            _log(f"[导出] {index}/{len(json_files)} 无变化，跳过: {json_path.relative_to(cfg.resource_input_root)}")
            continue
        relative = json_path.relative_to(cfg.resource_input_root)
        output_path = cfg.translated_dump_dir / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8")
        written_count += 1
        _log(f"[导出] {index}/{len(json_files)} {relative}")
    _log(f"[导出] 实际写出待替换 JSON: {written_count}/{len(json_files)}")


def disable_translated_text_effect_components(
    cfg: PipelineConfig,
    force_all_text_effect_materials: bool = False,
    material_only: bool = False,
) -> None:
    translations = _load_translation_dict(cfg)
    scan_artifacts = _load_scan_artifacts(cfg)
    if translations is None or scan_artifacts is None:
        raise FileNotFoundError("需要先生成 records.json / trans.json / ref_map.json，再执行阴影描边组件屏蔽。")

    _remove_stale_global_material_overlays(
        cfg,
        remove_tmp=not force_all_text_effect_materials,
    )
    records, _ids_map, _font_map, ref_map = scan_artifacts
    if not _is_reverse_ref_map_format(ref_map):
        raise ValueError("ref_map.json 不是被引用表格式，请先重新执行脚本 0。")
    path_id_map = _load_path_id_map(cfg)
    if not path_id_map:
        raise FileNotFoundError("path_id_map.json 为空或读取失败，请先重新执行脚本 0。")
    material_map = _load_material_usage_map(cfg)
    file_id_map = _load_file_id_map(cfg)

    runtime_sources = _load_runtime_binding_sources_for_translations(cfg, dict(translations))
    clean_all_text_effect_materials = force_all_text_effect_materials
    if force_all_text_effect_materials:
        _log("\033[94m[材质阴影描边] 已由工具脚本强制启用全部 TMP 效果材质清理。\033[0m")
    elif runtime_sources:
        kinds = sorted({str(item.get("kind", "unknown")) for item in runtime_sources})
        _log_blue(
            "[阴影描边][运行时绑定] 检测到 "
            f"{len(runtime_sources)} 个运行时文本来源（{', '.join(kinds)}）。"
            "脚本 5 只处理可精确定位的 I2 绑定，不再自动全量修改 TMP 材质。"
        )

    component_paths: set[Path] = set()
    translated_game_objects: set[tuple[str, int]] = set()
    i2_bound_game_objects = (
        set()
        if material_only
        else _i2_bound_game_objects_for_translations(cfg, dict(translations))
    )

    def add_game_object_components(key: tuple[str, int]) -> None:
        if key in translated_game_objects:
            return
        translated_game_objects.add(key)
        asset_key, game_object_path_id = key
        entries = ref_map.get(asset_key, {}).get(str(game_object_path_id), [])
        if not isinstance(entries, list):
            return
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            source_path_id = entry.get("path_id")
            relative = None
            if isinstance(source_path_id, int):
                relative = path_id_map.get(asset_key, {}).get(str(source_path_id))
            if not isinstance(relative, str):
                relative = entry.get("file") if isinstance(entry.get("file"), str) else None
            if not relative:
                continue
            component_paths.add(_resolve_input_json_path(cfg, relative))

    component_records = [] if material_only else records
    for record in component_records:
        if record.source_text not in translations or not isinstance(record.path_id, int) or record.path_id <= 0:
            continue
        json_path = _resolve_input_json_path(cfg, record.file_path)
        add_game_object_components((_bundle_key_for_json_path(cfg, json_path), record.path_id))
    for key in i2_bound_game_objects:
        add_game_object_components(key)

    _log(
        f"[阴影描边] 已翻译文本 GameObject: {len(translated_game_objects)} 个；"
        f"其中 I2 精确绑定: {len(i2_bound_game_objects)} 个；"
        f"候选同物体组件: {len(component_paths)} 个"
    )

    if clean_all_text_effect_materials:
        material_sources = _all_text_effect_material_paths(cfg)
        _log(
            "\033[94m[材质阴影描边][运行时绑定] 已启用全部 TMP 效果材质清理："
            f"候选材质={len(material_sources)}。\033[0m"
        )
    else:
        material_records = list(records)
        if component_paths and translations:
            marker_text = next(iter(translations))
            material_records.extend(
                ScanRecord(
                    file_path=str(path),
                    field="<i2-bound-component>",
                    source_text=marker_text,
                )
                for path in component_paths
            )
        material_sources = _material_paths_for_translated_records(
            cfg,
            material_records,
            dict(translations),
            material_map,
            path_id_map,
            file_id_map,
        )
    material_paths = {_resolve_input_json_path(cfg, relative) for relative in material_sources}
    _log(f"[阴影描边] 候选文本材质: {len(material_paths)} 个")

    matched_count = 0
    changed_count = 0
    disabled_fields_total = 0
    skipped_without_effect = 0
    disabled_records: list[dict[str, Any]] = []
    material_matched_count = 0
    material_changed_count = 0
    material_disabled_fields_total = 0
    material_skipped_without_effect = 0
    material_skipped_non_tmp = 0
    disabled_material_records: list[dict[str, Any]] = []
    total_components = len(component_paths)
    for index, json_path in enumerate(sorted(component_paths), start=1):
        if index == 1 or index % 2000 == 0 or index == total_components:
            _log(
                f"[阴影描边] 扫描进度: {index}/{total_components}；"
                f"命中: {matched_count}；本轮写出: {changed_count}；跳过无效果字段: {skipped_without_effect}"
            )
        if not json_path.is_file():
            continue
        relative = json_path.relative_to(cfg.resource_input_root)
        asset_key = _bundle_key_for_json_path(cfg, json_path)
        component_path_id = _extract_asset_path_id_from_json_path(json_path)
        game_object_path_id: int | None = None
        output_path = cfg.translated_dump_dir / relative
        source_path = json_path
        try:
            raw_text = source_path.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            _log(f"[阴影描边] 读取失败，已跳过: {source_path}")
            continue
        if "m_EffectColor" not in raw_text and "m_EffectDistance" not in raw_text:
            skipped_without_effect += 1
            continue
        try:
            data = json.loads(raw_text)
        except Exception:
            _log(f"[阴影描边] 读取失败，已跳过: {source_path}")
            continue
        changes = _disable_shadow_or_outline_component(data)
        matched_count += 1
        game_object = data.get("m_GameObject") if isinstance(data, dict) else None
        if isinstance(game_object, dict) and isinstance(game_object.get("m_PathID"), int):
            game_object_path_id = game_object["m_PathID"]
        disabled_records.append(
            {
                "asset": asset_key,
                "game_object_path_id": game_object_path_id,
                "component_path_id": component_path_id,
                "file": str(relative),
                "output_file": str(output_path.relative_to(cfg.stage_dir)),
                "changed_this_run": changes > 0,
                "changed_fields": changes,
                "already_disabled": changes == 0,
            }
        )
        if changes <= 0:
            continue
        disabled_fields_total += changes
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed_count += 1
        if changed_count <= 30 or changed_count % 500 == 0:
            _log(f"[阴影描边] {index}/{len(component_paths)} 已屏蔽: {relative}，修改字段: {changes}")

    total_materials = len(material_paths)
    for index, json_path in enumerate(sorted(material_paths), start=1):
        if index == 1 or index % 500 == 0 or index == total_materials:
            _log(
                f"[材质阴影描边] 扫描进度: {index}/{total_materials}；"
                f"命中: {material_matched_count}；本轮写出: {material_changed_count}；跳过无效果参数: {material_skipped_without_effect}"
            )
        if not json_path.is_file():
            continue
        relative = json_path.relative_to(cfg.resource_input_root)
        output_path = cfg.translated_dump_dir / relative
        # Material overlays are generated only by this pass, so always rebuild
        # from input instead of perpetuating an older all-material cleanup.
        source_path = json_path
        try:
            raw_text = source_path.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            _log(f"[材质阴影描边] 读取失败，已跳过: {source_path}")
            continue
        if not any(name in raw_text for name in MATERIAL_EFFECT_FLOAT_ZERO_KEYS | MATERIAL_EFFECT_COLOR_ALPHA_ZERO_KEYS):
            material_skipped_without_effect += 1
            continue
        try:
            data = json.loads(raw_text)
        except Exception:
            _log(f"[材质阴影描边] 读取失败，已跳过: {source_path}")
            continue
        if not _is_tmp_sdf_material_json(data):
            material_skipped_non_tmp += 1
            continue
        changes = _disable_material_effect_properties(data)
        material_matched_count += 1
        source_entries = material_sources.get(str(relative), [])
        disabled_material_records.append(
            {
                "file": str(relative),
                "output_file": str(output_path.relative_to(cfg.stage_dir)),
                "changed_this_run": changes > 0,
                "changed_fields": changes,
                "already_disabled": changes == 0,
                "used_by": source_entries,
            }
        )
        if changes <= 0:
            continue
        material_disabled_fields_total += changes
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        material_changed_count += 1
        if material_changed_count <= 30 or material_changed_count % 200 == 0:
            _log(f"[材质阴影描边] {index}/{len(material_paths)} 已屏蔽: {relative}，修改字段: {changes}")

    record_path = cfg.stage_record_dir / cfg.output_disabled_effect_components_json
    write_json(record_path, {"components": disabled_records, "materials": disabled_material_records})
    tsv_path = record_path.with_suffix(".tsv")
    tsv_rows = [
        "kind\tasset\tgame_object_path_id\tcomponent_path_id\tfile\toutput_file\tchanged_this_run\tchanged_fields\talready_disabled\tused_by"
    ]
    for item in disabled_records:
        tsv_rows.append(
            "\t".join(
                [
                    "component",
                    str(item.get("asset", "")),
                    "" if item.get("game_object_path_id") is None else str(item.get("game_object_path_id")),
                    "" if item.get("component_path_id") is None else str(item.get("component_path_id")),
                    str(item.get("file", "")),
                    str(item.get("output_file", "")),
                    str(item.get("changed_this_run", False)),
                    str(item.get("changed_fields", 0)),
                    str(item.get("already_disabled", False)),
                    "",
                ]
            )
        )
    for item in disabled_material_records:
        used_by = "; ".join(
            f"{source.get('text_file', '')}:{source.get('field', '')}:fileid={source.get('file_id', '')}:pathid={source.get('path_id', '')}"
            for source in item.get("used_by", [])
            if isinstance(source, dict)
        )
        tsv_rows.append(
            "\t".join(
                [
                    "material",
                    "",
                    "",
                    "",
                    str(item.get("file", "")),
                    str(item.get("output_file", "")),
                    str(item.get("changed_this_run", False)),
                    str(item.get("changed_fields", 0)),
                    str(item.get("already_disabled", False)),
                    used_by.replace("\t", " ").replace("\n", " "),
                ]
            )
        )
    tsv_path.write_text("\n".join(tsv_rows), encoding="utf-8")

    _log(
        f"[阴影描边] 命中 Shadow/Outline 组件: {matched_count} 个；"
        f"写出屏蔽 JSON: {changed_count} 个；修改字段: {disabled_fields_total} 处；"
        f"跳过无效果字段组件: {skipped_without_effect} 个"
    )
    _log(
        f"[材质阴影描边] 命中材质: {material_matched_count} 个；"
        f"写出屏蔽 JSON: {material_changed_count} 个；修改字段: {material_disabled_fields_total} 处；"
        f"跳过无效果参数材质: {material_skipped_without_effect} 个；"
        f"跳过非 TMP 材质: {material_skipped_non_tmp} 个"
    )
    _log(f"[阴影描边] 屏蔽组件记录: {record_path}")
    _log(f"[阴影描边] 屏蔽组件 TSV: {tsv_path}")


def translate_and_record(cfg: PipelineConfig) -> None:
    scan_and_record(cfg)
    translate_from_scan_records(cfg)


def scan_and_record(cfg: PipelineConfig) -> None:
    state = _load_scan_state(cfg)
    if state is not None and state.get("state") == "completed":
        if cfg.enable_ai_field_review:
            _log("[扫描] AI 字段判断已启用，将重新扫描生成黑名单模式 records 和 AI 字段统计")
            state = None
            _clear_scan_cache(cfg)
            state_path = _scan_state_path(cfg)
            if state_path.is_file():
                state_path.unlink()
        else:
            scan_artifacts = _load_scan_artifacts(cfg)
            if scan_artifacts is not None:
                _records, _ids_map, _font_map, ref_map = scan_artifacts
                if not _is_reverse_ref_map_format(ref_map):
                    _log("[扫描] ref_map.json 是旧的正向引用格式，将重新生成被引用表")
                    ref_map = _convert_forward_ref_map_artifact(cfg, ref_map)
                    material_map = _load_material_map(cfg) or rebuild_material_map(cfg)
                    _write_scan_artifacts(cfg, _records, _ids_map, _font_map, material_map, ref_map)
                    _log(f"[扫描] 被引用表已写入: {cfg.stage_record_dir / cfg.output_ref_map_json}")
                    path_id_map_path = _write_path_id_map(cfg)
                    _log(f"[扫描] PathID 文件索引已写入: {path_id_map_path}")
                    return
                else:
                    path_id_map_path = cfg.stage_record_dir / cfg.output_path_id_map_json
                    if not path_id_map_path.is_file():
                        _log("[扫描] PathID 文件索引不存在，补充生成一次")
                        _write_path_id_map(cfg)
                        _log(f"[扫描] PathID 文件索引已写入: {path_id_map_path}")
                    if not tmp_manifest_index_path(cfg).is_file():
                        _log("[扫描] TMP manifest 索引不存在，补充生成一次")
                        manifest_index = build_tmp_manifest_index(cfg)
                        _log(
                            f"[扫描] TMP manifest 索引已写入: {tmp_manifest_index_path(cfg)} "
                            f"(manifest={manifest_index.get('manifest_count')}, item={manifest_index.get('item_count')})"
                        )
                    if not _material_map_path(cfg).is_file():
                        _log("[扫描] material_map.json 不存在，补充生成文件中心的材质使用表")
                        rebuild_material_map(cfg)
                    _log("[扫描] 状态已完成，直接复用已保存的扫描记录")
                    return
            else:
                _log("[扫描] 状态已完成，但扫描记录不完整，将重新扫描")

    _log(f"[扫描] 输入目录: {cfg.resource_input_root}")
    records, ids_map, font_map, ref_map = scan_translation_inputs(cfg)
    _log(f"[扫描] 完成，命中文本记录数: {len(records)}")
    manifest_index = build_tmp_manifest_index(cfg)
    _log(
        f"[扫描] TMP manifest 索引已写入: {tmp_manifest_index_path(cfg)} "
        f"(manifest={manifest_index.get('manifest_count')}, item={manifest_index.get('item_count')})"
    )
    _log("[完成] 扫描和记录文件写入已结束")


def translate_from_scan_records(cfg: PipelineConfig) -> None:
    scan_artifacts = _load_scan_artifacts(cfg)
    if scan_artifacts is None:
        raise FileNotFoundError("未找到完整扫描记录，请先执行菜单 0。")
    records, ids_map, font_map, ref_map = scan_artifacts
    _log(f"[翻译] 已读取扫描记录，命中文本记录数: {len(records)}")
    _log("[翻译] 接下来进入去重、缓存恢复和实际翻译阶段")
    translations = build_translation_map(records, cfg)
    _log(f"[记录] 翻译阶段结束，当前 trans.json 条目数: {len(translations)}")
    write_translation_outputs(cfg, records, translations, ids_map, font_map, ref_map)
    _log(f"[记录] 已写入 trans.json / ids.json / font_map.json / ref_map.json / game.txt / game_chars.txt / mapping.tsv")
    _log("[完成] 扫描、翻译和记录文件写入已结束")


def export_translated_files(cfg: PipelineConfig) -> None:
    state = _load_scan_state(cfg)
    if state is None:
        _log("[导出] 未找到扫描状态，仍会尝试使用现有 records.json / trans.json")
    translations = _load_translation_dict(cfg)
    scan_artifacts = _load_scan_artifacts(cfg)
    if translations is None or scan_artifacts is None:
        raise FileNotFoundError("需要先生成 records.json 和 trans.json，再执行导出翻译后的待替换 JSON。")
    records, _ids_map, _font_map, _ref_map = scan_artifacts
    target_paths = _target_paths_from_records_and_translations(cfg, records, dict(translations))
    if not target_paths:
        raise FileNotFoundError("trans.json 没有匹配到 records.json 中的任何文本，未找到需要导出的文件。")
    _log(f"[导出] 读取 records.json + trans.json 作为导出目标，候选文件: {len(target_paths)}")
    write_runtime_text_binding_report(
        cfg,
        [record for record in records if record.source_text in translations],
    )
    _export_translated_files(cfg, dict(translations), target_paths)
    _log("[完成] 翻译后的待替换 JSON 导出已结束")


def translate_and_export(cfg: PipelineConfig) -> None:
    translate_and_record(cfg)
    _log("[导出] 接下来把 trans.json 实际套用到导出的资源副本")
    translations = _load_translation_dict(cfg)
    scan_artifacts = _load_scan_artifacts(cfg)
    if translations is None or scan_artifacts is None:
        raise FileNotFoundError("需要先生成 records.json 和 trans.json，再执行导出翻译后的待替换 JSON。")
    records, _ids_map, _font_map, _ref_map = scan_artifacts
    target_paths = _target_paths_from_records_and_translations(cfg, records, dict(translations))
    if not target_paths:
        raise FileNotFoundError("trans.json 没有匹配到 records.json 中的任何文本，未找到需要导出的文件。")
    _log(f"[导出] 读取 records.json + trans.json 作为导出目标，候选文件: {len(target_paths)}")
    write_runtime_text_binding_report(
        cfg,
        [record for record in records if record.source_text in translations],
    )
    _export_translated_files(cfg, dict(translations), target_paths)
    _log("[完成] 扫描、翻译和实际文件导出已结束")
