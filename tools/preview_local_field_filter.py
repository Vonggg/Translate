from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline.runtime_field_policy import runtime_field_exclusion_reason


_FIELD_LINE_RE = re.compile(r"^field:\s*(.+)$", re.MULTILINE)
_BACKING_FIELD_RE = re.compile(r"<([^>]+)>k__BackingField")
_ARRAY_INDEX_RE = re.compile(r"\[\]")

_TRUSTED_VISIBLE_FIELDS = {
    "m_text",
    "m_Text",
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


@dataclass(frozen=True)
class ReviewBlock:
    field: str
    normalized_field: str
    sibling_schema: str
    samples: tuple[str, ...]
    text: str


def _metadata_value(block: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_samples(block: str) -> tuple[str, ...]:
    marker = re.search(r"^sample_values:.*$", block, re.MULTILINE)
    if marker is None:
        return ()
    result: list[str] = []
    for line in block[marker.end() :].splitlines():
        if line.startswith("- "):
            result.append(line[2:])
        elif line.strip():
            break
    return tuple(result)


def parse_review(path: Path) -> tuple[str, list[ReviewBlock]]:
    text = path.read_text(encoding="utf-8")
    first = re.search(r"^field:\s*", text, re.MULTILINE)
    if first is None:
        return text, []
    header = text[: first.start()]
    starts = [match.start() for match in _FIELD_LINE_RE.finditer(text)]
    starts.append(len(text))
    blocks: list[ReviewBlock] = []
    for index in range(len(starts) - 1):
        raw = text[starts[index] : starts[index + 1]].rstrip() + "\n"
        field = _metadata_value(raw, "field")
        normalized = _metadata_value(raw, "normalized_field") or field.split("@@context_", 1)[0]
        blocks.append(
            ReviewBlock(
                field=field,
                normalized_field=normalized,
                sibling_schema=_metadata_value(raw, "sibling_schema"),
                samples=_parse_samples(raw),
                text=raw,
            )
        )
    return header, blocks


def _raw_semantic_names(field_path: str) -> set[str]:
    names = {match.group(1) for match in _BACKING_FIELD_RE.finditer(field_path)}
    cleaned = _BACKING_FIELD_RE.sub(lambda match: match.group(1), field_path)
    names.update(part for part in re.split(r"[.\[\]]+", cleaned) if part and part != "Array")
    return {name.strip("_<> ").replace("-", "") for name in names if name.strip("_<> ")}


def _semantic_names(field_path: str) -> set[str]:
    names = _raw_semantic_names(field_path)
    return {name.strip("_<> ").replace("-", "").casefold() for name in names if name.strip("_<> ")}


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


def _all_samples_have_no_language(samples: tuple[str, ...]) -> bool:
    meaningful = [sample.strip() for sample in samples if sample.strip()]
    if not meaningful:
        return True
    return all(not any(character.isalpha() for character in sample) for sample in meaningful)


def _all_samples_are_machine_values(samples: tuple[str, ...]) -> bool:
    meaningful = [sample.strip() for sample in samples if sample.strip()]
    return bool(meaningful) and all(
        any(pattern.search(sample) for pattern in _MACHINE_VALUE_PATTERNS)
        for sample in meaningful
    )


def _runtime_policy_reason(field_path: str, samples: tuple[str, ...]) -> str | None:
    concrete_path = _ARRAY_INDEX_RE.sub("[0]", field_path)
    values = samples or ("",)
    for value in values:
        reason = runtime_field_exclusion_reason(None, concrete_path, value)
        if reason is not None:
            return reason
    return None


def classify(block: ReviewBlock) -> tuple[str, str]:
    field_path = block.normalized_field

    if field_path in _TRUSTED_VISIBLE_FIELDS:
        return "allow", "Unity Text/TMP 直接文本字段"
    if _VISIBLE_LOCALIZATION_RE.search(field_path):
        return "allow", "结构化本地化显示文本"
    if _LOCALIZED_VALUE_RE.search(field_path):
        return "allow", "明确本地化译文值字段"

    policy_reason = _runtime_policy_reason(field_path, block.samples)
    if policy_reason is not None:
        return "protect", policy_reason
    if _RUNTIME_PATH_RE.search(field_path):
        return "protect", "Unity/运行时元数据路径"

    semantic_names = _semantic_names(field_path)
    if semantic_names & _RUNTIME_SEMANTIC_NAMES:
        matched = sorted(semantic_names & _RUNTIME_SEMANTIC_NAMES)[0]
        return "protect", f"运行时语义字段名: {matched}"
    if _has_identifier_name(field_path):
        return "protect", "运行时 ID/GUID 字段"
    if any(name.endswith(_RUNTIME_SEMANTIC_SUFFIXES) for name in semantic_names):
        return "protect", "运行时 ID/GUID 字段"

    schema_lower = block.sibling_schema.casefold()
    if "task=string" in schema_lower and (
        "<analyticsid>k__backingfield=string" in schema_lower
        or "<vfxinfo>k__backingfield=object" in schema_lower
        or "_finishsoundinfos=object" in schema_lower
    ):
        return "protect", "任务/VFX/音效运行时配置"
    if "productname=string" in schema_lower and sum(
        marker in schema_lower
        for marker in ("idgoogleplay=string", "idamazon=string", "idios=string", "idmac=string", "idwindows=string")
    ) >= 2:
        return "protect", "IAP 商品查找配置"
    if "companyname=string" in schema_lower and sum(
        marker in schema_lower
        for marker in ("privacylink=string", "termslink=string", "companylogo=pptr")
    ) >= 2:
        return "protect", "SDK 合规配置"

    if any("DO NOT DELETE INFORMATION" in sample for sample in block.samples):
        return "protect", "工具/配置保留标记"
    if _all_samples_are_machine_values(block.samples):
        return "protect", "机器配置值"
    if _all_samples_have_no_language(block.samples):
        return "protect", "样本不含语言文本"

    return "unknown", "缺少足够的本地正反证据"


def _filtered_header(original: str) -> str:
    lines = []
    for line in original.rstrip().splitlines():
        if line.startswith("# 高召回规则:"):
            lines.append("# 安全规则: 只有存在明确玩家可见文本证据时才返回；不确定时不要返回。")
        else:
            lines.append(line)
    lines.insert(0, "# 本文件已完成本地三态过滤，只包含仍需 AI 判断的 unknown 字段。")
    return "\n".join(lines).rstrip() + "\n\n"


def build_preview(review_path: Path, output_path: Path, report_path: Path) -> dict[str, object]:
    header, blocks = parse_review(review_path)
    classified: list[dict[str, object]] = []
    unknown_blocks: list[ReviewBlock] = []
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for block in blocks:
        decision, reason = classify(block)
        counts[decision] += 1
        reasons[f"{decision}: {reason}"] += 1
        classified.append(
            {
                "field": block.field,
                "normalized_field": block.normalized_field,
                "decision": decision,
                "reason": reason,
                "samples": list(block.samples),
            }
        )
        if decision == "unknown":
            unknown_blocks.append(block)

    output_text = _filtered_header(header) + "\n".join(block.text.rstrip() for block in unknown_blocks) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")

    report: dict[str, object] = {
        "source": str(review_path),
        "output": str(output_path),
        "source_bytes": review_path.stat().st_size,
        "output_bytes": output_path.stat().st_size,
        "candidate_count_before": len(blocks),
        "candidate_count_after": len(unknown_blocks),
        "decision_counts": dict(sorted(counts.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "fields": classified,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview deterministic local filtering for AI string-field review.")
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = build_preview(args.review, args.output, args.report)
    print(json.dumps({key: report[key] for key in (
        "source_bytes",
        "output_bytes",
        "candidate_count_before",
        "candidate_count_after",
        "decision_counts",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
