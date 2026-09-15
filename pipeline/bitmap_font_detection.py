from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable

from .shared import atomic_write_json, collect_json_files
from support.config import PipelineConfig


REPORT_FILENAME = "bitmap_font_detection.json"
REPORT_SCHEMA_VERSION = 6
BITMAP_FONT_JSON_TYPES = {"monobehaviour", "textasset", "font"}
NGUI_BITMAP_FONT_TYPE = "ngui_bitmap_font"


class UnsupportedBitmapFontError(RuntimeError):
    """Raised when confirmed, unsupported bitmap fonts make the pipeline unsafe."""

    def __init__(self, confirmed_count: int, report_path: Path):
        self.confirmed_count = confirmed_count
        self.report_path = report_path
        super().__init__(
            f"确认检测到 {confirmed_count} 个当前流程仍不支持的非 NGUI 位图字体；"
            f"报告: {report_path}"
        )


def _summarize_detections(detections: list[dict[str, Any]]) -> dict[str, int]:
    confirmed_items = [item for item in detections if item.get("confidence") == "confirmed"]
    supported_ngui = [item for item in confirmed_items if item.get("type") == NGUI_BITMAP_FONT_TYPE]
    unsupported = [item for item in confirmed_items if item.get("type") != NGUI_BITMAP_FONT_TYPE]
    return {
        "detected_count": len(detections),
        "confirmed_count": len(confirmed_items),
        "supported_ngui_count": len(supported_ngui),
        "unsupported_confirmed_count": len(unsupported),
        "likely_count": len(detections) - len(confirmed_items),
    }

_BMFont_TEXT_CHAR = re.compile(
    r"\bchar\s+[^\r\n<>]*\bid\s*=\s*[\"']?\d+[^\r\n<>]*"
    r"\bx\s*=\s*[\"']?-?\d+[^\r\n<>]*\by\s*=\s*[\"']?-?\d+[^\r\n<>]*"
    r"\bwidth\s*=\s*[\"']?\d+[^\r\n<>]*\bheight\s*=\s*[\"']?\d+",
    re.IGNORECASE,
)
_BMFont_XML_CHAR = re.compile(
    r"<char\b[^>]*\bid\s*=\s*[\"']\d+[\"'][^>]*"
    r"\bx\s*=\s*[\"']-?\d+[\"'][^>]*\by\s*=\s*[\"']-?\d+[\"'][^>]*"
    r"\bwidth\s*=\s*[\"']\d+[\"'][^>]*\bheight\s*=\s*[\"']\d+[\"']",
    re.IGNORECASE,
)
_PREFILTER_MARKERS = (
    b"xadvance",
    b"xoffset",
    b"chars count",
    b"<font",
    b"mfont",
    b"msaved",
    b"glyphs",
    b"charsblock",
)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _contains_prefilter_marker(path: Path) -> bool:
    overlap = b""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                lowered = (overlap + chunk).lower()
                if any(marker in lowered for marker in _PREFILTER_MARKERS):
                    return True
                overlap = lowered[-64:]
    except OSError:
        return False
    return False


def _iter_nodes(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _iter_nodes(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_nodes(child, f"{path}[{index}]")


def _array_items(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("Array"), list):
        return value["Array"]
    return []


def _lower_keys(value: dict[str, Any]) -> set[str]:
    return {str(key).lower() for key in value}


def _looks_like_ngui_glyph(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    keys = _lower_keys(value)
    identity = bool(keys & {"index", "id", "mindex"})
    rectangle = {"x", "y", "width", "height"}.issubset(keys)
    metrics = bool(keys & {"advance", "xadvance", "offsetx", "offsety", "channel"})
    return identity and rectangle and metrics


def _detect_ngui_node(value: Any) -> tuple[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    keys = _lower_keys(value)
    metric_keys = keys & {
        "msize",
        "charsize",
        "mbase",
        "baseoffset",
        "mwidth",
        "texwidth",
        "mheight",
        "texheight",
        "mspritename",
        "spritename",
    }
    glyph_fields = [key for key in value if str(key).lower() in {"msaved", "glyphs", "mglyphs"}]
    glyph_count = 0
    for field in glyph_fields:
        glyph_count += sum(1 for item in _array_items(value[field]) if _looks_like_ngui_glyph(item))

    evidence: list[str] = []
    if metric_keys:
        evidence.append("字体指标字段=" + ",".join(sorted(metric_keys)))
    if glyph_fields:
        evidence.append("字形数组字段=" + ",".join(map(str, glyph_fields)))
    if glyph_count:
        evidence.append(f"有效字形记录={glyph_count}")

    if glyph_count > 0 and len(metric_keys) >= 2:
        return "confirmed", evidence
    if glyph_fields and len(metric_keys) >= 3:
        return "likely", evidence
    return None


def _detect_json_data(data: Any, path: Path, root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for field_path, value in _iter_nodes(data):
        if isinstance(value, str):
            descriptor_kind = ""
            if _BMFont_TEXT_CHAR.search(value):
                descriptor_kind = "BMFont 文本描述"
            elif _BMFont_XML_CHAR.search(value):
                descriptor_kind = "BMFont XML 描述"
            if descriptor_kind:
                key = ("bmfont_descriptor", field_path)
                if key not in seen:
                    seen.add(key)
                    results.append(
                        {
                            "type": "bmfont_descriptor",
                            "confidence": "confirmed",
                            "source_file": _relative(path, root),
                            "field_path": field_path,
                            "evidence": [descriptor_kind, "包含字符 ID、图集矩形和字形尺寸"],
                        }
                    )
        # UIFont can retain an old BMFont table while rendering a dynamic Font
        # or delegating to another UIFont. Those tables are not active bitmaps.
        inactive_font = (field_path == "mFont" and isinstance(data, dict) and any(
            _pointer_ids(data.get(name))[1] for name in ("mDynamicFont", "mReplacement")))
        detected = None if inactive_font else _detect_ngui_node(value)
        if detected is not None:
            confidence, evidence = detected
            key = ("ngui_bitmap_font", field_path)
            if key not in seen:
                seen.add(key)
                results.append(
                    {
                        "type": "ngui_bitmap_font",
                        "confidence": confidence,
                        "source_file": _relative(path, root),
                        "field_path": field_path,
                        "evidence": evidence,
                    }
                )
    return results


def _detect_json(path: Path, root: Path) -> list[dict[str, Any]]:
    if not _contains_prefilter_marker(path):
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    return _detect_json_data(data, path, root)


def _detect_fnt(path: Path, root: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    evidence: list[str] = []
    if raw.startswith(b"BMF") and len(raw) >= 4:
        evidence = [f"BMFont 二进制头 BMF，版本={raw[3]}"]
    else:
        text = raw.decode("utf-8-sig", errors="replace")
        if _BMFont_TEXT_CHAR.search(text):
            evidence = ["BMFont 文本描述", "包含字符 ID、图集矩形和字形尺寸"]
        elif _BMFont_XML_CHAR.search(text):
            evidence = ["BMFont XML 描述", "包含字符 ID、图集矩形和字形尺寸"]
    if not evidence:
        return []
    return [
        {
            "type": "bmfont_descriptor",
            "confidence": "confirmed",
            "source_file": _relative(path, root),
            "field_path": "",
            "evidence": evidence,
        }
    ]


def detect_bitmap_fonts(input_root: Path, *, exported_resources_only: bool = False) -> list[dict[str, Any]]:
    if not input_root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    if exported_resources_only:
        json_candidates = [
            path
            for path in collect_json_files(input_root)
            if path.parent.name.lower() in BITMAP_FONT_JSON_TYPES
        ]
    else:
        json_candidates = sorted(input_root.rglob("*.json"))
    candidate_paths = json_candidates + sorted(input_root.rglob("*.fnt"))
    for path in candidate_paths:
        suffix = path.suffix.lower()
        if suffix == ".json":
            results.extend(_detect_json(path, input_root))
        elif suffix == ".fnt":
            results.extend(_detect_fnt(path, input_root))
    return results


def _looks_like_tmp_sdf_font(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("m_CharacterTable"), dict)
        and isinstance(value.get("m_GlyphTable"), dict)
        and isinstance(value.get("m_FaceInfo"), dict)
    )


def detect_tmp_sdf_font_assets(
    input_root: Path,
    *,
    exported_resources_only: bool = False,
) -> list[str]:
    """Return exported JSON resources that structurally contain a TMP FontAsset."""
    if not input_root.is_dir():
        return []
    candidates = collect_json_files(input_root)
    if exported_resources_only:
        candidates = [path for path in candidates if path.parent.name.lower() == "monobehaviour"]

    sources: list[str] = []
    for path in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        lowered = raw.lower()
        if not all(
            marker in lowered
            for marker in (b'"m_charactertable"', b'"m_glyphtable"', b'"m_faceinfo"')
        ):
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if any(_looks_like_tmp_sdf_font(value) for _field_path, value in _iter_nodes(data)):
            sources.append(_relative(path, input_root))
    return sources


def _pointer_ids(value: Any) -> tuple[int, int]:
    if not isinstance(value, dict):
        return (0, 0)
    return (
        int(value.get("m_FileID", value.get("FileID", 0)) or 0),
        int(value.get("m_PathID", value.get("PathID", 0)) or 0),
    )


def detect_ngui_dynamic_ttf_labels(
    input_root: Path,
    *,
    exported_resources_only: bool = False,
) -> dict[str, Any]:
    """Detect NGUI UILabel components that use Unity Font/TTF at runtime."""
    if not input_root.is_dir():
        return {"label_count": 0, "reference_count": 0, "references": []}
    candidates = collect_json_files(input_root)
    if exported_resources_only:
        candidates = [path for path in candidates if path.parent.name.lower() == "monobehaviour"]

    references: dict[tuple[int, int], dict[str, Any]] = {}
    label_count = 0
    for path in candidates:
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        lowered = raw.lower()
        if not all(
            marker in lowered
            for marker in (b'"mtruetypefont"', b'"mfontsize"', b'"mtext"')
        ):
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("mText"), str):
            continue
        if "mFontSize" not in data or "mTrueTypeFont" not in data:
            continue
        file_id, path_id = _pointer_ids(data.get("mTrueTypeFont"))
        if not path_id:
            continue
        label_count += 1
        row = references.setdefault(
            (file_id, path_id),
            {
                "file_id": file_id,
                "path_id": path_id,
                "component_count": 0,
                "sample_sources": [],
            },
        )
        row["component_count"] += 1
        if len(row["sample_sources"]) < 10:
            row["sample_sources"].append(_relative(path, input_root))

    rows = sorted(
        references.values(),
        key=lambda row: (-int(row["component_count"]), int(row["file_id"]), int(row["path_id"])),
    )
    return {
        "label_count": label_count,
        "reference_count": len(rows),
        "references": rows,
    }


def _detect_all_exported_font_features(
    input_root: Path,
    json_candidates: Iterable[Path] | None = None,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    """Detect all exported font modes with one directory walk and one read per JSON."""
    candidates = (
        list(json_candidates)
        if json_candidates is not None
        else collect_json_files(input_root)
    )
    detections: list[dict[str, Any]] = []
    tmp_sdf_sources: list[str] = []
    references: dict[tuple[int, int], dict[str, Any]] = {}
    label_count = 0

    for path in candidates:
        parent_type = path.parent.name.lower()
        if parent_type not in BITMAP_FONT_JSON_TYPES:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        lowered = raw.lower()
        check_bitmap = any(marker in lowered for marker in _PREFILTER_MARKERS)
        check_tmp = parent_type == "monobehaviour" and all(
            marker in lowered
            for marker in (b'"m_charactertable"', b'"m_glyphtable"', b'"m_faceinfo"')
        )
        check_dynamic_ttf = parent_type == "monobehaviour" and all(
            marker in lowered
            for marker in (b'"mtruetypefont"', b'"mfontsize"', b'"mtext"')
        )
        if not (check_bitmap or check_tmp or check_dynamic_ttf):
            continue
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            continue

        if check_bitmap:
            detections.extend(_detect_json_data(data, path, input_root))
        if check_tmp and any(
            _looks_like_tmp_sdf_font(value) for _field_path, value in _iter_nodes(data)
        ):
            tmp_sdf_sources.append(_relative(path, input_root))
        if (
            check_dynamic_ttf
            and isinstance(data, dict)
            and isinstance(data.get("mText"), str)
            and "mFontSize" in data
            and "mTrueTypeFont" in data
        ):
            file_id, path_id = _pointer_ids(data.get("mTrueTypeFont"))
            if path_id:
                label_count += 1
                row = references.setdefault(
                    (file_id, path_id),
                    {
                        "file_id": file_id,
                        "path_id": path_id,
                        "component_count": 0,
                        "sample_sources": [],
                    },
                )
                row["component_count"] += 1
                if len(row["sample_sources"]) < 10:
                    row["sample_sources"].append(_relative(path, input_root))

    for path in sorted(input_root.rglob("*.fnt")):
        detections.extend(_detect_fnt(path, input_root))

    rows = sorted(
        references.values(),
        key=lambda row: (-int(row["component_count"]), int(row["file_id"]), int(row["path_id"])),
    )
    return detections, tmp_sdf_sources, {
        "label_count": label_count,
        "reference_count": len(rows),
        "references": rows,
    }


def _print_ngui_dynamic_ttf_notice(summary: dict[str, Any], *, reused: bool = False) -> None:
    label_count = int(summary.get("label_count", 0) or 0)
    if not label_count:
        return
    reference_count = int(summary.get("reference_count", 0) or 0)
    prefix = "复用报告，已识别" if reused else "已识别"
    print(
        f"\033[94m[字体类型检测][NGUI动态TTF] {prefix} {label_count} 个 NGUI UILabel，"
        f"引用 {reference_count} 组 Unity Font/TTF。此模式会在运行时从 TTF 动态生成字形。\033[0m",
        flush=True,
    )
    print(
        "\033[93m[字体类型检测][NGUI动态TTF][后续需要] 汉化需要执行步骤 7 替换 TTF，"
        "并确认模板字体包含全部译文字符；不需要生成 NGUI 位图字形表或 UIAtlas。\033[0m",
        flush=True,
    )


def load_font_pipeline_modes(cfg: PipelineConfig) -> dict[str, int]:
    """Load the font kinds found by step 0, with compatibility for schema-3 reports."""
    report_path = cfg.stage_record_dir / REPORT_FILENAME
    if not report_path.is_file():
        raise FileNotFoundError(f"未找到脚本 0 的字体检测报告: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"脚本 0 的字体检测报告无效: {report_path}") from exc
    if not isinstance(report, dict):
        raise ValueError(f"脚本 0 的字体检测报告结构无效: {report_path}")

    ngui_count = int(report.get("supported_ngui_count", 0) or 0)
    if not ngui_count:
        detections = report.get("detections", [])
        if isinstance(detections, list):
            ngui_count = sum(
                1
                for item in detections
                if isinstance(item, dict)
                and item.get("type") == NGUI_BITMAP_FONT_TYPE
                and item.get("confidence") == "confirmed"
            )

    if "tmp_sdf_count" in report:
        tmp_sdf_count = int(report.get("tmp_sdf_count", 0) or 0)
    else:
        # Schema 3 reports predate TMP/SDF mode recording. Keep an existing scan usable,
        # but do the structural fallback only once the old report is encountered.
        tmp_sdf_count = len(
            detect_tmp_sdf_font_assets(cfg.resource_input_root, exported_resources_only=True)
        )
        print(
            "[字体流程][兼容] 脚本 0 的旧检测报告未记录 TMP/SDF 数量，"
            f"已补充结构检测：TMP/SDF={tmp_sdf_count}；下次执行脚本 0 后会写入报告。",
            flush=True,
        )
    return {"tmp_sdf": tmp_sdf_count, "ngui": ngui_count}


def run_bitmap_font_detection(
    cfg: PipelineConfig,
    input_fingerprint: str = "",
    *,
    fail_on_confirmed: bool = False,
    json_files: Iterable[Path] | None = None,
) -> Path:
    report_path = cfg.stage_record_dir / REPORT_FILENAME
    if input_fingerprint and report_path.is_file():
        try:
            cached_report = json.loads(report_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            cached_report = None
        if (
            isinstance(cached_report, dict)
            and cached_report.get("schema_version") == REPORT_SCHEMA_VERSION
            and cached_report.get("input_fingerprint") == input_fingerprint
        ):
            detected_count = int(cached_report.get("detected_count", 0) or 0)
            supported_ngui_count = int(cached_report.get("supported_ngui_count", 0) or 0)
            tmp_sdf_count = int(cached_report.get("tmp_sdf_count", 0) or 0)
            dynamic_ttf_summary = cached_report.get("ngui_dynamic_ttf", {})
            if not isinstance(dynamic_ttf_summary, dict):
                dynamic_ttf_summary = {}
            dynamic_ttf_count = int(dynamic_ttf_summary.get("label_count", 0) or 0)
            unsupported_count = int(cached_report.get("unsupported_confirmed_count", 0) or 0)
            likely_count = int(cached_report.get("likely_count", 0) or 0)
            if detected_count:
                print(
                    f"[位图字体检测] 复用未变化输入的检测报告："
                    f"NGUI位图已支持={supported_ngui_count}，"
                    f"仍不支持={unsupported_count}，疑似={likely_count}。",
                    flush=True,
                )
                if supported_ngui_count:
                    print(
                        f"\033[92m[位图字体检测][NGUI位图已支持] 已识别 {supported_ngui_count} 个 NGUI "
                        "位图字体，将由 NGUI 专用流程处理。\033[0m",
                        flush=True,
                    )
            else:
                print("[位图字体检测] 输入未变化，复用报告：未发现 BMFont/NGUI 静态特征。", flush=True)
            _print_ngui_dynamic_ttf_notice(dynamic_ttf_summary, reused=True)
            print(
                f"[字体类型检测] 复用脚本 0 结论：TMP/SDF={tmp_sdf_count}，"
                f"NGUI位图={supported_ngui_count}，NGUI动态TTF={dynamic_ttf_count}。",
                flush=True,
            )
            print(f"[位图字体检测] 报告: {report_path}", flush=True)
            if fail_on_confirmed and unsupported_count > 0:
                print(
                    f"\033[91m[位图字体检测][停止] 已确认 {unsupported_count} 个仍不支持的非 NGUI 位图字体，"
                    "脚本 0 返回失败，不会启动后续步骤。\033[0m",
                    flush=True,
                )
                raise UnsupportedBitmapFontError(unsupported_count, report_path)
            return report_path

    detections, tmp_sdf_sources, dynamic_ttf_summary = _detect_all_exported_font_features(
        cfg.resource_input_root,
        json_files,
    )
    summary = _summarize_detections(detections)
    confirmed = summary["confirmed_count"]
    supported_ngui = summary["supported_ngui_count"]
    unsupported = summary["unsupported_confirmed_count"]
    likely = summary["likely_count"]
    atomic_write_json(
        report_path,
        {
            "schema_version": REPORT_SCHEMA_VERSION,
            "input_root": str(cfg.resource_input_root),
            "input_fingerprint": input_fingerprint,
            "tmp_sdf_count": len(tmp_sdf_sources),
            "tmp_sdf_sources": tmp_sdf_sources,
            "ngui_dynamic_ttf": dynamic_ttf_summary,
            **summary,
            "detections": detections,
        },
    )

    if detections:
        print(
            f"[位图字体检测] 检测到位图字体：确认={confirmed}，"
            f"其中NGUI位图已支持={supported_ngui}，仍不支持={unsupported}，疑似={likely}。",
            flush=True,
        )
        for item in detections[:10]:
            location = item["source_file"]
            if item["field_path"]:
                location += f"::{item['field_path']}"
            color = "\033[93m" if item["type"] == NGUI_BITMAP_FONT_TYPE else "\033[91m"
            print(
                color
                +
                f"  - [{item['confidence']}] {item['type']}: {location}"
                "\033[0m",
                flush=True,
            )
        if len(detections) > 10:
            print(f"  - 其余 {len(detections) - 10} 条请查看报告。", flush=True)
        if supported_ngui:
            print(
                f"\033[92m[位图字体检测][NGUI位图已支持] 已识别 {supported_ngui} 个 NGUI 位图字体，"
                "将由 NGUI 专用流程处理。\033[0m",
                flush=True,
            )
    else:
        print("[位图字体检测] 未发现 BMFont/NGUI 位图字体的静态特征。", flush=True)
    _print_ngui_dynamic_ttf_notice(dynamic_ttf_summary)
    print(
        f"[字体类型检测] 脚本 0 已确定：TMP/SDF={len(tmp_sdf_sources)}，"
        f"NGUI位图={supported_ngui}，"
        f"NGUI动态TTF={int(dynamic_ttf_summary.get('label_count', 0) or 0)}。",
        flush=True,
    )
    print(f"[位图字体检测] 报告已写入: {report_path}", flush=True)
    if fail_on_confirmed and unsupported > 0:
        print(
            f"\033[91m[位图字体检测][停止] 已确认 {unsupported} 个仍不支持的非 NGUI 位图字体，"
            "脚本 0 返回失败，不会启动后续步骤。\033[0m",
            flush=True,
        )
        raise UnsupportedBitmapFontError(unsupported, report_path)
    return report_path
