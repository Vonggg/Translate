from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_GUID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_INPUT_CONTROL_PATH_RE = re.compile(r"^(?:<[^>]+>/|\*/\{)[^\r\n]+")
_PREFAB_LOOKUP_FIELD_RE = re.compile(
    r"^(?P<container>.+)\.Array\[(?P<index>\d+)\]\."
    r"(?P<leaf>Id|ID|id|_id|_name|_additionalInfo|DamageSound|DeathSound)$"
)
_SERIALIZED_CALLBACK_KEY_RE = re.compile(r"^call_\d+$", re.IGNORECASE)


_ALWAYS_RUNTIME_PATH_PATTERNS: tuple[tuple[str, str], ...] = (
    ("m_ActionMaps.*", "Unity Input System 动作表"),
    ("m_ControlSchemes.*", "Unity Input System 控制方案"),
    ("references.RefIds*.type.asm", "Unity SerializeReference 程序集"),
    ("references.RefIds*.type.ns", "Unity SerializeReference 命名空间"),
    ("references.RefIds*.type.class", "Unity SerializeReference 类型"),
    ("m_InternalIds.Array*", "Addressables InternalId"),
    ("m_ProviderIds.Array*", "Addressables ProviderId"),
    ("m_ResourceProviderData.Array*.m_Id", "Addressables ProviderId"),
    ("prefabs.Array*.type", "Prefab 运行时类型"),
    ("spriteInfoList.Array*.name", "TMP Sprite 运行时名称"),
    ("m_StyleList.Array*.m_OpeningDefinition", "TMP 样式标记"),
    ("m_StyleList.Array*.m_ClosingDefinition", "TMP 样式标记"),
    ("m_RenderingLayerNames.Array*", "Unity Rendering Layer 名称"),
    ("customRemoteConfig.*.valueName", "运行时配置键"),
    ("m_CreationSettings.characterSequence", "TMP 字符集配置"),
    ("m_CreationSettings.*FontAssetGUID", "TMP 字体 GUID"),
    ("m_CreationSettings.sourceFontFileGUID", "TMP 源字体 GUID"),
    ("m_CreationSettings.sourceFontFileName", "TMP 源字体文件名"),
    ("m_SourceFontFileGUID", "TMP 源字体 GUID"),
    ("m_SourceFontFilePath", "TMP 源字体路径"),
    ("m_FaceInfo.m_FamilyName", "TMP 字体元数据"),
    ("m_FaceInfo.m_StyleName", "TMP 字体元数据"),
    ("m_Version", "Unity/TMP 版本元数据"),
    ("settings.passTag", "Shader Pass 标签"),
    ("*m_RegexValue", "正则表达式配置"),
    ("*_textFormat", "运行时格式串"),
    ("*AdUnit", "广告位标识"),
    ("*AppId", "应用标识"),
    ("*AppKey", "应用密钥"),
    ("*ClientId", "客户端标识"),
    ("*GameKey", "游戏密钥"),
    ("*SecretKey", "私钥标识"),
    ("*Token", "访问令牌"),
    ("*SlotID", "广告位标识"),
    ("*AccessToken", "访问令牌"),
    ("*URL", "运行时 URL"),
    ("*Path", "运行时路径"),
    ("*_path", "运行时资源路径"),
    ("*KeystorePath", "签名文件路径"),
    ("*PersistentCalls.*", "UnityEvent 运行时回调"),
    ("m_Clips.Array*.m_DisplayName", "Timeline Clip 编辑器标识"),
    ("animationsNames.Array*", "运行时动画查找名称"),
)

_TYPE_METADATA_LEAVES = {
    "m_AssemblyName",
    "m_ClassName",
    "m_Namespace",
    "m_TargetAssemblyTypeName",
    "m_ObjectArgumentAssemblyTypeName",
    "assemblyName",
    "className",
    "namespaceName",
}

_IDENTIFIER_LEAVES = {
    "m_ActionId",
    "m_GUID",
    "m_Guid",
    "m_Id",
    "m_ID",
    "guid",
    "Guid",
    "GUID",
    "id",
}

_ADDRESSABLE_LEAVES = {
    "m_InternalId",
    "m_ProviderId",
    "InternalId",
    "ProviderId",
}

_LEGACY_INPUT_NAME_LEAVES = {
    "horizontalAxisName",
    "verticalAxisName",
    "horizontalPanAxisName",
    "verticalPanAxisName",
    "scrollAxisName",
    "m_HorizontalAxis",
    "m_VerticalAxis",
    "m_SubmitButton",
    "m_CancelButton",
}

_RUNTIME_LOOKUP_LEAVES = {
    "desiredTag": "Unity Tag 查找名称",
    "jelasticKey": "后端/远程配置键",
    "SpecificVehicleName": "任务载具查找名称",
    "VehicleType": "任务载具类型",
    "PickupType": "任务拾取物类型",
    "TargetFaction": "任务阵营标识",
    "markVisualType": "任务标记类型",
    "MarksTypeNPC": "任务标记类型",
    "MarksTypePickUp": "任务标记类型",
    "DialogName": "对话运行时查找名称",
}

_SERIALIZED_JSON_METADATA_LEAVES = {"$id", "$ref", "$type"}

def _field_leaf(field_path: str) -> str:
    leaf = field_path.rsplit(".", 1)[-1]
    return leaf.split("[", 1)[0]


def _has_input_system_structure(data: Any) -> bool:
    return isinstance(data, dict) and (
        "m_ActionMaps" in data
        or "m_ControlSchemes" in data
    )


def _resolve_dict_path(data: Any, field_path: str) -> Any:
    node = data
    for key in field_path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _is_unity_object_reference(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if isinstance(value.get("m_PathID"), int) and "m_FileID" in value:
        return True
    return isinstance(value.get("_path"), str)


def _is_prefab_lookup_identifier(data: Any, field_path: str) -> bool:
    """Detect identifiers stored beside a prefab reference in a serialized lookup table."""
    match = _PREFAB_LOOKUP_FIELD_RE.fullmatch(field_path)
    if match is None:
        return False

    container = _resolve_dict_path(data, match.group("container"))
    entries = container.get("Array") if isinstance(container, dict) else None
    index = int(match.group("index"))
    if not isinstance(entries, list) or index >= len(entries):
        return False
    entry = entries[index]
    if not isinstance(entry, dict):
        return False

    return any(
        "prefab" in str(key).lower() and _is_unity_object_reference(value)
        for key, value in entry.items()
    )


def _is_character_customization_identifier(data: Any, field_path: str) -> bool:
    """Recognize the verified CustomControl lookup schema, not arbitrary names.

    CharacterCustomization compares Customs[].name to a runtime character key
    and enables/disables customSets.gameObject. A display card with a name and
    a prefab/icon reference alone is NOT sufficient evidence for this rule.
    Match the schema rather than game name, PathID, or character vocabulary.
    """
    if not isinstance(data, dict) or not _is_unity_object_reference(data.get("m_Script")):
        return False
    match = re.fullmatch(r"Customs\.Array\[(\d+)\]\.name", field_path)
    if match is None:
        return False
    container = data.get("Customs")
    entries = container.get("Array") if isinstance(container, dict) else None
    if not isinstance(entries, list) or not entries or int(match[1]) >= len(entries):
        return False
    return all(
        isinstance(entry, dict)
        and set(entry) == {"name", "customSets"}
        and isinstance(entry["name"], str)
        and _is_unity_object_reference(entry["customSets"])
        for entry in entries
    )


def _is_menu_runtime_parameter(data: Any, field_path: str) -> bool:
    if not fnmatchcase(field_path, "Params.Array*") or not isinstance(data, dict):
        return False
    return (
        isinstance(data.get("PrefabPath"), str)
        and "MenuType" in data
        and isinstance(data.get("onClick"), dict)
    )


def _is_runtime_track_name(data: Any, field_path: str) -> bool:
    if not fnmatchcase(field_path, "tracks.Array*.trackName") or not isinstance(data, dict):
        return False
    tracks = data.get("tracks")
    entries = tracks.get("Array") if isinstance(tracks, dict) else None
    if not isinstance(entries, list):
        return False
    return any(
        isinstance(entry, dict) and _is_unity_object_reference(entry.get("clip"))
        for entry in entries
    )


def runtime_field_exclusion_reason(
    data: Any,
    field_path: str,
    value: str,
) -> str | None:
    """Return a high-confidence reason when a string is runtime metadata, not UI text."""
    for pattern, reason in _ALWAYS_RUNTIME_PATH_PATTERNS:
        if fnmatchcase(field_path, pattern):
            return reason

    leaf = _field_leaf(field_path)
    stripped = value.strip()

    if leaf in _SERIALIZED_JSON_METADATA_LEAVES:
        return "序列化 JSON 结构元数据"
    if (
        leaf == "Name"
        and field_path.startswith("m_Script.json.")
        and (".$content[]" in field_path or ".QwestTree[]" in field_path)
    ):
        return "任务内部查找名称"

    if leaf in _LEGACY_INPUT_NAME_LEAVES:
        return "Unity Legacy Input 轴/按钮名称"

    lookup_reason = _RUNTIME_LOOKUP_LEAVES.get(leaf)
    if lookup_reason is not None:
        return lookup_reason

    if _SERIALIZED_CALLBACK_KEY_RE.fullmatch(leaf):
        return "序列化脚本回调/命令名称"

    if _is_menu_runtime_parameter(data, field_path):
        return "菜单/Prefab 运行时参数"

    if _is_runtime_track_name(data, field_path):
        return "运行时音轨查找名称"

    if leaf == "MapName" and isinstance(data, dict) and "MyMap" in data:
        return "运行时地图查找名称"

    if leaf == "level" and isinstance(data, dict) and _is_unity_object_reference(data.get("loading")):
        return "Unity 场景加载名称"

    if _has_input_system_structure(data):
        if field_path.startswith("m_ActionMaps."):
            return "Unity Input System 动作表"
        if field_path.startswith("m_ControlSchemes."):
            return "Unity Input System 控制方案"
        if leaf == "m_ActionId":
            return "Unity Input System 动作引用"

    if _is_prefab_lookup_identifier(data, field_path):
        return "Prefab 查找表运行时标识"

    if _is_character_customization_identifier(data, field_path):
        return "角色模型定制查找表运行时标识"

    if leaf == "m_ActionId" and (_UUID_RE.fullmatch(stripped) or _HEX_GUID_RE.fullmatch(stripped)):
        return "Unity Input System 动作 GUID"

    if leaf in _IDENTIFIER_LEAVES and (
        _UUID_RE.fullmatch(stripped)
        or _HEX_GUID_RE.fullmatch(stripped)
    ):
        return "运行时 GUID/ID"

    if leaf in {"m_ControlPath", "m_Path", "path"} and _INPUT_CONTROL_PATH_RE.match(stripped):
        return "Unity Input System 控制路径"

    if leaf in _TYPE_METADATA_LEAVES and stripped:
        return "运行时类型/程序集元数据"

    if leaf in _ADDRESSABLE_LEAVES and stripped:
        return "Addressables 运行时定位标识"

    return None


def is_runtime_non_text_field(data: Any, field_path: str, value: str) -> bool:
    return runtime_field_exclusion_reason(data, field_path, value) is not None


def restore_protected_runtime_fields(
    original: Any,
    candidate: Any,
) -> tuple[Any, list[tuple[str, str]]]:
    """Restore protected strings in a translated JSON tree from its original copy.

    Translation normally skips these fields.  This second pass also protects imports
    made from stale or manually edited translation output generated before a runtime
    field rule existed.
    """

    restored: list[tuple[str, str]] = []
    root_data = original

    def walk(source: Any, current: Any, field_path: str) -> Any:
        if isinstance(source, str):
            reason = runtime_field_exclusion_reason(root_data, field_path, source)
            if reason is not None and current != source:
                restored.append((field_path, reason))
                return source
            return current

        if isinstance(source, dict) and isinstance(current, dict):
            for key, source_value in source.items():
                child_path = f"{field_path}.{key}" if field_path else str(key)
                if key in current:
                    current[key] = walk(source_value, current[key], child_path)
                elif isinstance(source_value, str):
                    reason = runtime_field_exclusion_reason(
                        root_data,
                        child_path,
                        source_value,
                    )
                    if reason is not None:
                        current[key] = source_value
                        restored.append((child_path, reason))
            return current

        if isinstance(source, list) and isinstance(current, list):
            for index in range(min(len(source), len(current))):
                child_path = f"{field_path}[{index}]"
                current[index] = walk(source[index], current[index], child_path)
            return current

        return current

    return walk(original, candidate, ""), restored
