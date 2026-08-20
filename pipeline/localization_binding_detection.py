from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


REPORT_FILENAME = "localization_binding_detection.json"
REPORT_SCHEMA_VERSION = 1

TEXT_FIELD_LEAVES = {"m_Text", "m_text", "mText", "_text"}
_IGNORED_KEY_FIELD_LEAVES = {
    "m_Name",
    "m_EditorClassIdentifier",
    "m_TagString",
    "m_AssetBundleName",
    "m_AssetBundleVariant",
    "mLocalizeTargetName",
}
_KEY_FIELD_HINT = re.compile(
    r"(?:^|_)(?:id|key|term|token|code)(?:$|_)",
    re.IGNORECASE,
)
_FORMAT_TOKEN = re.compile(r"\{(?:\d+|[A-Za-z_]\w*)(?:[^{}]*)\}")


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _field_leaf(field_path: str) -> str:
    leaf = field_path.rsplit(".", 1)[-1]
    return leaf.split("[", 1)[0]


def _walk_strings(node: Any, field_path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child_path = f"{field_path}.{key}" if field_path else str(key)
            if isinstance(value, str):
                yield child_path, value
            else:
                yield from _walk_strings(value, child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            child_path = f"{field_path}[{index}]"
            if isinstance(value, str):
                yield child_path, value
            else:
                yield from _walk_strings(value, child_path)


def _schema_shape(value: Any, depth: int = 0) -> Any:
    if isinstance(value, dict):
        if isinstance(value.get("m_PathID"), int) and "m_FileID" in value:
            return "PPtr"
        if depth >= 2:
            return "object"
        return {
            str(key): _schema_shape(child, depth + 1)
            for key, child in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, list):
        item_shapes: list[Any] = []
        seen: set[str] = set()
        for item in value[:16]:
            shape = _schema_shape(item, depth + 1)
            signature = _sha256_json(shape)
            if signature not in seen:
                seen.add(signature)
                item_shapes.append(shape)
        return {"array": item_shapes or ["empty"]}
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return type(value).__name__


def _extract_pptr(data: Any) -> tuple[int | None, int | None]:
    if not isinstance(data, dict):
        return None, None
    file_id = data.get("m_FileID")
    path_id = data.get("m_PathID")
    return (
        file_id if isinstance(file_id, int) else None,
        path_id if isinstance(path_id, int) else None,
    )


def extract_component_descriptor(
    data: Any,
    *,
    relative_file: str,
    asset: str,
    asset_path_id: int | None,
) -> dict[str, Any] | None:
    """Extract only the structural data needed for cross-component localization inference."""
    if not isinstance(data, dict):
        return None
    _game_object_file_id, game_object_path_id = _extract_pptr(data.get("m_GameObject"))
    if game_object_path_id is None or game_object_path_id == 0:
        return None
    script_file_id, script_path_id = _extract_pptr(data.get("m_Script"))
    all_strings = list(_walk_strings(data))
    strings = [
        {"field": field, "value": value}
        for field, value in all_strings
        if value.strip()
    ]
    text_fields = [
        item for item in strings
        if _field_leaf(str(item["field"])) in TEXT_FIELD_LEAVES
    ]
    schema_signature = _sha256_json(_schema_shape(data))
    return {
        "file": Path(relative_file).as_posix(),
        "asset": Path(asset).as_posix(),
        "asset_path_id": asset_path_id,
        "game_object_path_id": game_object_path_id,
        "script_file_id": script_file_id,
        "script_path_id": script_path_id,
        "schema_signature": schema_signature,
        "strings": strings,
        "string_fields": sorted({field for field, _value in all_strings}),
        "text_fields": text_fields,
    }


def localization_input_signature(resource_root: Path, json_files: Iterable[Path]) -> str:
    signatures: list[dict[str, Any]] = []
    for path in json_files:
        if path.parent.name.lower() != "monobehaviour":
            continue
        try:
            stat = path.stat()
            relative = path.relative_to(resource_root).as_posix()
        except (OSError, ValueError):
            continue
        signatures.append(
            {"path": relative, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        )
    return _sha256_json(sorted(signatures, key=lambda item: str(item["path"])))


def _looks_like_format_value(value: str) -> bool:
    stripped = value.strip()
    return bool(_FORMAT_TOKEN.search(stripped)) and len(stripped) <= 160


def _key_field_hint(field_path: str) -> bool:
    leaf = _field_leaf(field_path).strip().lower()
    compact = re.sub(r"[^a-z0-9]+", "_", leaf).strip("_")
    return bool(_KEY_FIELD_HINT.search(compact))


def _protection_entry(
    descriptor: dict[str, Any],
    field: str,
    value: str,
    reason: str,
    group_id: str,
) -> dict[str, Any]:
    return {
        "file": descriptor["file"],
        "field": field,
        "value": value,
        "reason": reason,
        "asset": descriptor["asset"],
        "game_object_path_id": descriptor["game_object_path_id"],
        "component_path_id": descriptor.get("asset_path_id"),
        "group": group_id,
    }


def infer_localization_bindings(
    descriptors: Iterable[dict[str, Any]],
    *,
    input_signature: str = "",
) -> dict[str, Any]:
    components = [item for item in descriptors if isinstance(item, dict)]
    by_game_object: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for component in components:
        asset = component.get("asset")
        game_object_path_id = component.get("game_object_path_id")
        if isinstance(asset, str) and isinstance(game_object_path_id, int):
            by_game_object[(asset, game_object_path_id)].append(component)

    groups: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for component in components:
        script_path_id = component.get("script_path_id")
        schema_signature = component.get("schema_signature")
        if not isinstance(script_path_id, int) or script_path_id == 0 or not isinstance(schema_signature, str):
            continue
        groups[(script_path_id, schema_signature)].append(component)

    protected: list[dict[str, Any]] = []
    detected_groups: list[dict[str, Any]] = []
    ambiguous_groups: list[dict[str, Any]] = []

    for (script_path_id, schema_signature), instances in groups.items():
        script_file_ids = sorted(
            {
                int(item["script_file_id"])
                for item in instances
                if isinstance(item.get("script_file_id"), int)
            }
        )
        group_id = f"{script_path_id}:{schema_signature[:12]}"
        linked_texts: dict[str, list[tuple[dict[str, Any], str, str]]] = {}
        linked_instance_count = 0
        for instance in instances:
            links: list[tuple[dict[str, Any], str, str]] = []
            game_object_key = (str(instance["asset"]), int(instance["game_object_path_id"]))
            for other in by_game_object.get(game_object_key, []):
                if other.get("file") == instance.get("file"):
                    continue
                for text_item in other.get("text_fields", []):
                    field = text_item.get("field")
                    value = text_item.get("value")
                    if isinstance(field, str) and isinstance(value, str) and value.strip():
                        links.append((other, field, value))
            if links:
                linked_instance_count += 1
            linked_texts[str(instance["file"])] = links

        field_stats: dict[str, dict[str, Any]] = {}
        for instance in instances:
            links = linked_texts.get(str(instance["file"]), [])
            linked_values = {value for _other, _field, value in links}
            for item in instance.get("strings", []):
                field = item.get("field")
                value = item.get("value")
                if not isinstance(field, str) or not isinstance(value, str) or not value.strip():
                    continue
                leaf = _field_leaf(field)
                if leaf in TEXT_FIELD_LEAVES or leaf in _IGNORED_KEY_FIELD_LEAVES:
                    continue
                stat = field_stats.setdefault(
                    field,
                    {
                        "field": field,
                        "present": 0,
                        "exact_text_matches": 0,
                        "format_values": 0,
                        "values": set(),
                    },
                )
                stat["present"] += 1
                stat["values"].add(value)
                if value in linked_values:
                    stat["exact_text_matches"] += 1
                if _looks_like_format_value(value):
                    stat["format_values"] += 1

        has_format_sibling = any(
            stat["present"] > 0 and stat["format_values"] / stat["present"] >= 0.5
            for stat in field_stats.values()
        )
        known_i2 = any(
            "mTerm" in instance.get("string_fields", [])
            and "mLocalizeTargetName" in instance.get("string_fields", [])
            for instance in instances
        )
        accepted_fields: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        for field, stat in field_stats.items():
            present = int(stat["present"])
            exact_matches = int(stat["exact_text_matches"])
            format_values = int(stat["format_values"])
            unique_values = len(stat["values"])
            exact_ratio = exact_matches / present if present else 0.0
            field_hint = _key_field_hint(field)
            candidate = {
                "field": field,
                "present": present,
                "unique_values": unique_values,
                "exact_text_matches": exact_matches,
                "exact_ratio": round(exact_ratio, 4),
                "field_name_hint": field_hint,
                "format_value_ratio": round(format_values / present, 4) if present else 0.0,
            }
            candidates.append(candidate)
            if format_values / present >= 0.5:
                continue
            if known_i2 and field in {"mTerm", "mTermSecondary"}:
                candidate["rule"] = "standard_i2_structure"
                accepted_fields.append(candidate)
                continue
            enough_links = linked_instance_count >= min(3, len(instances))
            evidence = (
                exact_matches >= 2
                and enough_links
                and (has_format_sibling or field_hint)
            ) or (
                exact_matches >= 1
                and linked_instance_count >= 3
                and has_format_sibling
                and field_hint
            )
            if evidence:
                candidate["rule"] = "script_schema_same_gameobject_text_relation"
                accepted_fields.append(candidate)

        if known_i2 and not any(item["field"] == "mTerm" for item in accepted_fields):
            accepted_fields.append(
                {
                    "field": "mTerm",
                    "present": 0,
                    "unique_values": 0,
                    "exact_text_matches": 0,
                    "exact_ratio": 0.0,
                    "field_name_hint": True,
                    "format_value_ratio": 0.0,
                    "rule": "standard_i2_structure",
                }
            )

        if not accepted_fields:
            if linked_instance_count and any(item["exact_text_matches"] for item in candidates):
                ambiguous_groups.append(
                    {
                        "group": group_id,
                        "script_file_ids": script_file_ids,
                        "script_path_id": script_path_id,
                        "schema_signature": schema_signature,
                        "instances": len(instances),
                        "linked_text_instances": linked_instance_count,
                        "candidates": sorted(
                            candidates,
                            key=lambda item: (-item["exact_text_matches"], item["field"]),
                        )[:8],
                    }
                )
            continue

        accepted_field_names = {item["field"] for item in accepted_fields}
        group_protection_start = len(protected)
        for instance in instances:
            key_values: set[str] = set()
            instance_strings = {
                str(item.get("field")): str(item.get("value"))
                for item in instance.get("strings", [])
                if isinstance(item.get("field"), str) and isinstance(item.get("value"), str)
            }
            for field in accepted_field_names:
                value = instance_strings.get(field, "")
                if value:
                    key_values.add(value)
                    protected.append(
                        _protection_entry(instance, field, value, "localization_key_field", group_id)
                    )

            for other, text_field, text_value in linked_texts.get(str(instance["file"]), []):
                if text_value in key_values:
                    protected.append(
                        _protection_entry(
                            other,
                            text_field,
                            text_value,
                            "text_equals_localization_key",
                            group_id,
                        )
                    )
                elif known_i2 and not instance_strings.get("mTerm", "").strip():
                    protected.append(
                        _protection_entry(
                            other,
                            text_field,
                            text_value,
                            "i2_auto_term_uses_text_value",
                            group_id,
                        )
                    )

        detected_groups.append(
            {
                "group": group_id,
                "script_file_ids": script_file_ids,
                "script_path_id": script_path_id,
                "schema_signature": schema_signature,
                "instances": len(instances),
                "linked_text_instances": linked_instance_count,
                "format_sibling_detected": has_format_sibling,
                "standard_i2": known_i2,
                "key_fields": accepted_fields,
                "protected_positions": len(protected) - group_protection_start,
                "sample_files": [str(item["file"]) for item in instances[:5]],
            }
        )

    recognized_by_script: dict[int, set[str]] = defaultdict(set)
    standard_i2_scripts: set[int] = set()
    detected_group_ids = {str(item["group"]) for item in detected_groups}
    for group in detected_groups:
        script_path_id = group.get("script_path_id")
        if not isinstance(script_path_id, int):
            continue
        for key_field in group.get("key_fields", []):
            field = key_field.get("field") if isinstance(key_field, dict) else None
            if isinstance(field, str):
                recognized_by_script[script_path_id].add(field)
        if group.get("standard_i2"):
            standard_i2_scripts.add(script_path_id)

    propagated_schema_instances = 0
    for component in components:
        script_path_id = component.get("script_path_id")
        schema_signature = component.get("schema_signature")
        if not isinstance(script_path_id, int) or not isinstance(schema_signature, str):
            continue
        fields = recognized_by_script.get(script_path_id)
        if not fields:
            continue
        group_id = f"{script_path_id}:{schema_signature[:12]}"
        if group_id in detected_group_ids:
            continue
        instance_strings = {
            str(item.get("field")): str(item.get("value"))
            for item in component.get("strings", [])
            if isinstance(item.get("field"), str) and isinstance(item.get("value"), str)
        }
        available_fields = fields.intersection(component.get("string_fields", []))
        if not available_fields:
            continue
        propagated_schema_instances += 1
        key_values: set[str] = set()
        for field in available_fields:
            value = instance_strings.get(field, "")
            if value:
                key_values.add(value)
                protected.append(
                    _protection_entry(
                        component,
                        field,
                        value,
                        "localization_key_field_propagated_by_script",
                        group_id,
                    )
                )
        game_object_key = (str(component["asset"]), int(component["game_object_path_id"]))
        for other in by_game_object.get(game_object_key, []):
            if other.get("file") == component.get("file"):
                continue
            for text_item in other.get("text_fields", []):
                text_field = text_item.get("field")
                text_value = text_item.get("value")
                if not isinstance(text_field, str) or not isinstance(text_value, str):
                    continue
                reason = ""
                if text_value in key_values:
                    reason = "text_equals_localization_key_propagated_by_script"
                elif (
                    script_path_id in standard_i2_scripts
                    and "mTerm" in available_fields
                    and not instance_strings.get("mTerm", "").strip()
                ):
                    reason = "i2_auto_term_uses_text_value"
                if reason:
                    protected.append(
                        _protection_entry(other, text_field, text_value, reason, group_id)
                    )

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
    for entry in protected:
        key = (str(entry["file"]), str(entry["field"]), str(entry["value"]))
        deduplicated.setdefault(key, entry)
    protected_entries = sorted(
        deduplicated.values(),
        key=lambda item: (str(item["file"]), str(item["field"]), str(item["value"])),
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "input_signature": input_signature,
        "summary": {
            "component_descriptors": len(components),
            "script_schema_groups": len(groups),
            "detected_localization_groups": len(detected_groups),
            "ambiguous_groups": len(ambiguous_groups),
            "propagated_schema_instances": propagated_schema_instances,
            "protected_positions": len(protected_entries),
            "protected_key_fields": sum(
                1
                for item in protected_entries
                if str(item["reason"]).startswith("localization_key_field")
            ),
            "protected_text_fields": sum(
                1
                for item in protected_entries
                if not str(item["reason"]).startswith("localization_key_field")
            ),
        },
        "detected_groups": detected_groups,
        "ambiguous_groups": ambiguous_groups,
        "protected_entries": protected_entries,
    }


def protection_index(report: Any) -> dict[str, set[tuple[str, str]]]:
    result: dict[str, set[tuple[str, str]]] = defaultdict(set)
    if not isinstance(report, dict):
        return {}
    for entry in report.get("protected_entries", []):
        if not isinstance(entry, dict):
            continue
        file = entry.get("file")
        field = entry.get("field")
        value = entry.get("value")
        if isinstance(file, str) and isinstance(field, str) and isinstance(value, str):
            result[Path(file).as_posix()].add((field, value))
    return dict(result)
