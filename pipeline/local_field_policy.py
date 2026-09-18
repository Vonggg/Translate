from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from .runtime_field_policy import runtime_field_exclusion_reason


LocalFieldDecisionKind = Literal["allow", "protect", "unknown"]


@dataclass(frozen=True)
class LocalFieldDecision:
    decision: LocalFieldDecisionKind
    reason: str


_BACKING_FIELD_RE = re.compile(r"<([^>]+)>k__BackingField")
_ARRAY_INDEX_RE = re.compile(r"\[\]")

_TRUSTED_VISIBLE_FIELDS = {
    "m_text",
    "m_Text",
    # NGUI UILabel 的序列化显示文本。它不像 UGUI/TMP 使用下划线命名，
    # 但同样是直接呈现在界面上的内容，不能交给 unknown 字段筛选丢弃。
    "mText",
}

_TRUSTED_EMBEDDED_VISIBLE_LEAVES = {
    "qwesttitle",
    "tasktext",
    "replica",
    "actor",
}

_VISIBLE_LOCALIZATION_RE = re.compile(
    r"(?:^|\.)localizations\.Array\[\]\."
    r"(?:GDPRAcceptButton|GDPRDescription|GDPRHeader|GDPRPrivacyButton|GDPRTermsButton)$",
    re.IGNORECASE,
)

_LOCALIZED_VALUE_RE = re.compile(
    r"(?:^|\.)(?:m_Localized|localizedText|translatedText|translation)$",
    re.IGNORECASE,
)

_RUNTIME_PATH_RE = re.compile(
    r"(?:"
    r"m_fontInfo(?:\.|$)|m_FaceInfo(?:\.|$)|m_CreationSettings(?:\.|$)|"
    r"m_StyleList(?:\.|$)|spriteInfoList(?:\.|$)|"
    r"m_ExcludedPropertiesInInspector(?:\.|$)|"
    r"tagNames\.Array\[\]$|_names\.Array\[\]$|"
    r"data\.dataString$|InstrumentationSettings(?:\.|$)|"
    r"lightLayerName\d*$|meshName$"
    r")",
    re.IGNORECASE,
)

_RUNTIME_SEMANTIC_NAMES = {
    "actionid",
    "adunit",
    "analyticsid",
    "animationname",
    "animatorstate",
    "appkey",
    "appid",
    "assembly",
    "assemblyname",
    "assetguid",
    "assettype",
    "behaviourid",
    "brainid",
    "callback",
    "cachedassettype",
    "classname",
    "clientid",
    "collectsoundid",
    "collectvfxid",
    "code",
    "configid",
    "contractnames",
    "controllerreference",
    "controlpath",
    "desiredtag",
    "enterportalvfxid",
    "eventname",
    "fileid",
    "formulastring",
    "gamekey",
    "guid",
    "id",
    "itemid",
    "joystickname",
    "languageiso",
    "methodname",
    "musicids",
    "multiplayerbrainid",
    "namespace",
    "parentcontractnames",
    "poolname",
    "preloadbundlesgroupsids",
    "providerid",
    "regexvalue",
    "sceneid",
    "scenename",
    "secretkey",
    "serializabletype",
    "shadername",
    "skinid",
    "slotid",
    "soundid",
    "statconfigid",
    "statename",
    "subobjectname",
    "tagname",
    "token",
    "typename",
    "updatedbehaviourid",
    "updatedstatconfigid",
    "uuid",
    "vfxid",
    "vfxs",
}

_RUNTIME_SEMANTIC_SUFFIXES = (
    "guid",
    "uuid",
    "analyticsid",
    "soundid",
    "vfxid",
    "brainid",
    "configid",
    "statconfigid",
    "behaviourid",
    "skinid",
    "itemid",
    "actionid",
    "key",
    "url",
    "uri",
    "host",
    "type",
    "property",
    "propertyname",
    "sound",
    "audio",
    "vfx",
    "preset",
)

_MACHINE_VALUE_PATTERNS = (
    re.compile(r"^-----BEGIN (?:RSA )?(?:PRIVATE KEY|CERTIFICATE)-----"),
    re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE),
    re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$"),
    re.compile(r"^#[0-9a-fA-F]{3,8}$"),
    re.compile(r"^(?:NaN|[-+]?Infinity)$", re.IGNORECASE),
)


def _raw_semantic_names(field_path: str) -> set[str]:
    semantic_path = re.sub(r"\.Array\[\]$", "", field_path)
    leaf = semantic_path.rsplit(".", 1)[-1]
    names = {match.group(1) for match in _BACKING_FIELD_RE.finditer(leaf)}
    names.add(_BACKING_FIELD_RE.sub(lambda match: match.group(1), leaf))
    return {
        name.strip("_<> ")
        for name in names
        if name.strip("_<> ")
    }


def _semantic_names(field_path: str) -> set[str]:
    return {
        name.replace("-", "").casefold()
        for name in _raw_semantic_names(field_path)
    }


def _has_identifier_name(field_path: str) -> bool:
    for name in _raw_semantic_names(field_path):
        lowered = name.casefold()
        if re.search(r"(?:^|_)(?:id|ids|guid|guids|uuid|uuids)$", lowered):
            return True
        if re.search(r"(?:Id|IDs|ID|GUID|Guid|UUID|Uuid)s?$", name):
            return True
        if re.match(r"^id[A-Z]", name):
            return True
    return False


def _has_runtime_semantic_suffix(field_path: str) -> bool:
    """Match suffixes only at a naming boundary (avoids e.g. monkey -> key)."""
    for raw_name in _raw_semantic_names(field_path):
        for suffix in _RUNTIME_SEMANTIC_SUFFIXES:
            if len(raw_name) < len(suffix):
                continue
            tail = raw_name[-len(suffix) :]
            if tail.casefold() != suffix:
                continue
            if len(raw_name) == len(suffix):
                return True
            suffix_start = len(raw_name) - len(suffix)
            if raw_name[suffix_start - 1] in "_-" or raw_name[suffix_start].isupper():
                return True
    return False


def _all_samples_have_no_language(samples: tuple[str, ...]) -> bool:
    meaningful = [sample.strip() for sample in samples if sample.strip()]
    return bool(meaningful) and all(
        not any(character.isalpha() for character in sample)
        for sample in meaningful
    )


def _all_samples_are_machine_values(samples: tuple[str, ...]) -> bool:
    meaningful = [sample.strip() for sample in samples if sample.strip()]
    return bool(meaningful) and all(
        any(pattern.search(sample) for pattern in _MACHINE_VALUE_PATTERNS)
        for sample in meaningful
    )


def _runtime_policy_reason(field_path: str, samples: tuple[str, ...]) -> str | None:
    concrete_path = _ARRAY_INDEX_RE.sub("[0]", field_path)
    for value in samples or ("",):
        reason = runtime_field_exclusion_reason(None, concrete_path, value)
        if reason is not None:
            return reason
    return None


def _schema_text(sibling_schema: str | Iterable[str]) -> str:
    if isinstance(sibling_schema, str):
        return sibling_schema
    return ", ".join(str(item) for item in sibling_schema)


def classify_local_string_field(
    field_path: str,
    sample_values: Iterable[str] = (),
    sibling_schema: str | Iterable[str] = (),
) -> LocalFieldDecision:
    """Classify a string-field context using only deterministic local evidence."""
    samples = tuple(value for value in sample_values if isinstance(value, str))

    if field_path in _TRUSTED_VISIBLE_FIELDS:
        return LocalFieldDecision("allow", "Unity Text/TMP/NGUI 直接文本字段")
    if (
        field_path.startswith("m_Script.json.")
        and _semantic_names(field_path) & _TRUSTED_EMBEDDED_VISIBLE_LEAVES
    ):
        return LocalFieldDecision("allow", "内嵌任务/对话显示文本字段")
    if _VISIBLE_LOCALIZATION_RE.search(field_path):
        return LocalFieldDecision("allow", "结构化本地化显示文本")
    if _LOCALIZED_VALUE_RE.search(field_path):
        return LocalFieldDecision("allow", "明确本地化译文值字段")

    policy_reason = _runtime_policy_reason(field_path, samples)
    if policy_reason is not None:
        return LocalFieldDecision("protect", policy_reason)
    if _RUNTIME_PATH_RE.search(field_path):
        return LocalFieldDecision("protect", "Unity/运行时元数据路径")

    semantic_names = _semantic_names(field_path)
    matched_names = semantic_names & _RUNTIME_SEMANTIC_NAMES
    if matched_names:
        return LocalFieldDecision(
            "protect",
            f"运行时语义字段名: {sorted(matched_names)[0]}",
        )
    if _has_identifier_name(field_path):
        return LocalFieldDecision("protect", "运行时 ID/GUID 字段")
    if _has_runtime_semantic_suffix(field_path):
        return LocalFieldDecision("protect", "运行时语义后缀字段")

    schema_lower = _schema_text(sibling_schema).casefold()
    if "task=string" in schema_lower and (
        "<analyticsid>k__backingfield=string" in schema_lower
        or "<vfxinfo>k__backingfield=object" in schema_lower
        or "_finishsoundinfos=object" in schema_lower
    ):
        return LocalFieldDecision("protect", "任务/VFX/音效运行时配置")
    if "productname=string" in schema_lower and sum(
        marker in schema_lower
        for marker in (
            "idgoogleplay=string",
            "idamazon=string",
            "idios=string",
            "idmac=string",
            "idwindows=string",
        )
    ) >= 2:
        return LocalFieldDecision("protect", "IAP 商品查找配置")
    if "companyname=string" in schema_lower and sum(
        marker in schema_lower
        for marker in (
            "privacylink=string",
            "termslink=string",
            "companylogo=pptr",
        )
    ) >= 2:
        return LocalFieldDecision("protect", "SDK 合规配置")

    if any("DO NOT DELETE INFORMATION" in sample for sample in samples):
        return LocalFieldDecision("protect", "工具/配置保留标记")
    if _all_samples_are_machine_values(samples):
        return LocalFieldDecision("protect", "机器配置值")
    if _all_samples_have_no_language(samples):
        return LocalFieldDecision("protect", "样本不含语言文本")

    return LocalFieldDecision("unknown", "缺少足够的本地正反证据")
