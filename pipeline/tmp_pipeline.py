from __future__ import annotations

import copy
import json
import re
import shutil
import struct
import subprocess
import unicodedata
import zlib
from pathlib import Path
from typing import Any

from support.config import PipelineConfig
from support.process_lock import interprocess_file_lock
from .dynamic_translation_dictionary import (
    STRINGLITERAL_TRANSLATIONS_FILENAME,
    select_whole_text_dictionary_entries,
)
from .manifest_index import load_tmp_manifest_index, tmp_manifest_index_path
from .shared import collect_json_files, is_visible_char, read_json, unique_preserve_order, write_json


UNITY_YAML_DOCUMENT_RE = re.compile(r"^--- !u!(?P<class_id>\d+) &(?P<file_id>-?\d+)\s*$", re.MULTILINE)
TMP_YAML_ONLY_KEYS = {
    "m_ObjectHideFlags",
    "m_CorrespondingSourceObject",
    "m_PrefabInstance",
    "m_PrefabAsset",
    "m_EditorHideFlags",
    "m_EditorClassIdentifier",
    "m_SourceFontFile_EditorRef",
}
TMP_FIELD_ALIASES = {
    "m_Material": ("m_Material", "material"),
}
TMP_SOURCE_ROOT_KEYS = [
    "hashCode",
    "materialHashCode",
    "m_SourceFontFile",
    "m_AtlasPopulationMode",
    "atlas",
]
TMP_SOURCE_DOTTED_PATHS = [
    "m_GameObject.m_FileID",
    "m_GameObject.m_PathID",
    "m_Script.m_FileID",
    "m_Script.m_PathID",
    "m_Name",
    "hashCode",
    "material.m_FileID",
    "material.m_PathID",
    "m_Material.m_FileID",
    "m_Material.m_PathID",
    "materialHashCode",
    "m_FaceInfo.m_FamilyName",
    "m_SourceFontFileGUID",
    "m_SourceFontFile.m_FileID",
    "m_SourceFontFile.m_PathID",
    "atlas.m_FileID",
    "atlas.m_PathID",
    "m_CreationSettings.sourceFontFileGUID",
    "m_CreationSettings.referencedFontAssetGUID",
    "m_CreationSettings.referencedTextAssetGUID",
    "m_FaceInfo.m_UnitsPerEM",
]

TMP_SDF_MATERIAL_FLOAT_KEYS = {
    "_FaceDilate",
    "_GradientScale",
    "_OutlineSoftness",
    "_OutlineWidth",
    "_PerspectiveFilter",
    "_ScaleRatioA",
    "_ScaleRatioB",
    "_ScaleRatioC",
    "_Sharpness",
    "_TextureHeight",
    "_TextureWidth",
    "_UnderlayDilate",
    "_UnderlayOffsetX",
    "_UnderlayOffsetY",
    "_UnderlaySoftness",
    "_VertexOffsetX",
    "_VertexOffsetY",
    "_WeightBold",
    "_WeightNormal",
}

TMP_BOLD_STRENGTH_SCALE = 0.5

RICH_TEXT_TAG_RE = re.compile(r"<[^>]*>")


def _sdf_generated_template_dir(cfg: PipelineConfig) -> Path:
    return cfg.stage_dir / "Font" / "SDF" / "generated_templates"


def _sdf_generated_asset_path(cfg: PipelineConfig, output_name: str = "generated_tmp_font.asset") -> Path:
    return _sdf_generated_template_dir(cfg) / output_name


def _sdf_replacement_summary_path(cfg: PipelineConfig) -> Path:
    return cfg.stage_dir / "Font" / "SDF" / "tmp_font_replacements.json"


def _estimate_sdf_atlas_size_for_auto_point_size(visible_char_count: int, padding: int, max_atlas_size: int = 8192) -> int:
    # Keep enough sampling resolution for dense CJK glyphs with fixed padding.
    # Unity still uses Auto Sizing; this only chooses a reasonable atlas tier.
    if visible_char_count <= 1200:
        estimated = 2048
    elif visible_char_count <= 3000:
        estimated = 4096
    else:
        estimated = 8192
    return min(estimated, max(1024, int(max_atlas_size or 8192)))


def _log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def _log_red(message: str) -> None:
    print(f"\033[91m{message}\033[0m", flush=True)


def _supported_codepoints_from_ttf(font_path: Path) -> set[int] | None:
    if not font_path.is_file():
        print(f"[TMP] TTF 模板不存在，跳过不支持字符过滤: {font_path}", flush=True)
        return None
    try:
        from fontTools.ttLib import TTFont
    except Exception as exc:
        print(f"[TMP] 未安装 fontTools，跳过不支持字符过滤: {exc}", flush=True)
        return None

    try:
        font = TTFont(font_path)
        supported: set[int] = set()
        for table in font["cmap"].tables:
            if table.isUnicode():
                supported.update(table.cmap.keys())
        return supported
    except Exception as exc:
        print(f"[TMP] 读取 TTF cmap 失败，跳过不支持字符过滤: {font_path} ({exc})", flush=True)
        return None


def _is_tmp_char_candidate(char: str) -> bool:
    if not char:
        return False
    return not unicodedata.category(char).startswith("C")


def _chars_from_supported_codepoints(supported: set[int] | None) -> str:
    if supported is None:
        return ""
    chars: list[str] = []
    for codepoint in sorted(supported):
        try:
            char = chr(codepoint)
        except ValueError:
            continue
        if _is_tmp_char_candidate(char):
            chars.append(char)
    return "".join(chars)


def _supported_codepoints_from_tmp_json(json_path: Path) -> set[int] | None:
    if not json_path.is_file():
        print(f"[TMP] 老工具 SDF 模板不存在，跳过模板字符合并: {json_path}", flush=True)
        return None
    try:
        data = read_json(json_path)
    except Exception as exc:
        print(f"[TMP] 读取老工具 SDF 模板失败，跳过模板字符合并: {exc}", flush=True)
        return None
    if not isinstance(data, dict):
        print("[TMP] 老工具 SDF 模板不是 JSON 对象，跳过模板字符合并。", flush=True)
        return None

    supported: set[int] = set()
    character_table = data.get("m_CharacterTable", {}).get("Array", [])
    if isinstance(character_table, list):
        for item in character_table:
            if isinstance(item, dict) and isinstance(item.get("m_Unicode"), int):
                supported.add(item["m_Unicode"])
    if not supported:
        chars = _extract_chars_from_tmp_json(data)
        supported.update(ord(char) for char in chars)
    if not supported:
        print("[TMP] 老工具 SDF 模板未提取到字符表，跳过模板字符合并。", flush=True)
        return None
    return supported


def _filter_chars_by_ttf_support(chars: str, cfg: PipelineConfig) -> tuple[str, str]:
    supported = _supported_codepoints_from_ttf(cfg.ttf_template_path)
    if supported is None:
        return chars, ""

    kept: list[str] = []
    missing: list[str] = []
    for char in chars:
        if ord(char) in supported:
            kept.append(char)
        else:
            missing.append(char)

    missing_text = "".join(unique_preserve_order(missing))
    if missing_text:
        missing_path = cfg.stage_record_dir / "tmp_chars_missing_from_ttf.txt"
        missing_detail_path = cfg.stage_record_dir / "tmp_chars_missing_from_ttf.tsv"
        missing_path.parent.mkdir(parents=True, exist_ok=True)
        missing_path.write_text(missing_text, encoding="utf-8")
        missing_detail_path.write_text(
            "\n".join(
                ["code\tchar", *(f"U+{ord(char):04X}\t{char}" for char in missing_text)]
            ),
            encoding="utf-8",
        )
        print(f"[TMP] TTF 不支持字符: {len(missing_text)} 个，已从 tmp_chars.txt 排除。", flush=True)
        print(missing_text, flush=True)
        print(f"[TMP] 不支持字符清单: {missing_path}", flush=True)
        print(f"[TMP] 不支持字符详情: {missing_detail_path}", flush=True)
    else:
        print("[TMP] TTF 已覆盖全部待生成字符。", flush=True)
    return "".join(kept), missing_text


def _report_translation_chars_missing_from_ttf(
    cfg: PipelineConfig,
    supported: set[int] | None,
    label: str = "模板 TTF",
    output_prefix: str = "translation_chars_missing_from_ttf",
    stop_on_missing: bool = True,
) -> str:
    missing_path = cfg.stage_record_dir / f"{output_prefix}.txt"
    missing_detail_path = cfg.stage_record_dir / f"{output_prefix}.tsv"
    if supported is None:
        return ""

    translation_paths = [
        ("静态", cfg.stage_record_dir / cfg.output_trans_json),
        ("动态", cfg.stage_record_dir / STRINGLITERAL_TRANSLATIONS_FILENAME),
    ]
    translation_items: list[tuple[str, str]] = []
    loaded_labels: list[str] = []
    for translation_kind, trans_path in translation_paths:
        if not trans_path.is_file():
            continue
        try:
            translations = read_json(trans_path)
        except Exception as exc:
            print(
                f"[TMP] 读取{translation_kind}词库失败，跳过该词库字符支持检查: "
                f"{trans_path} ({exc})",
                flush=True,
            )
            continue
        if not isinstance(translations, dict):
            print(
                f"[TMP] {trans_path.name} 不是键值表，跳过该词库字符支持检查。",
                flush=True,
            )
            continue
        loaded_labels.append(translation_kind)
        if translation_kind == "动态":
            dynamic_entries, _dynamic_skipped = select_whole_text_dictionary_entries(
                translations
            )
            translation_items.extend(dynamic_entries)
        else:
            translation_items.extend(
                (source_text, translated_text)
                for source_text, translated_text in translations.items()
                if isinstance(source_text, str)
                and isinstance(translated_text, str)
                and translated_text
                and source_text != translated_text
            )
    if not loaded_labels:
        print("[TMP] 未找到可用的静态/动态翻译词库，跳过译文 TTF 字符支持检查。", flush=True)
        return ""

    missing_chars: list[str] = []
    rows = ["source\ttranslated\tcode\tchar"]
    seen_rows: set[tuple[str, str, str]] = set()
    for source_text, translated_text in translation_items:
        visible_translated_text = RICH_TEXT_TAG_RE.sub("", translated_text)
        for char in unique_preserve_order(visible_translated_text):
            if not is_visible_char(char) or ord(char) in supported:
                continue
            missing_chars.append(char)
            row_key = (source_text, translated_text, char)
            if row_key in seen_rows:
                continue
            seen_rows.add(row_key)
            rows.append(
                "\t".join(
                    [
                        source_text.replace("\t", " ").replace("\n", "\\n"),
                        translated_text.replace("\t", " ").replace("\n", "\\n"),
                        f"U+{ord(char):04X}",
                        char,
                    ]
                )
            )

    missing_text = "".join(unique_preserve_order(missing_chars))
    if not missing_text:
        for stale_path in (missing_path, missing_detail_path):
            if stale_path.exists():
                stale_path.unlink()
        _log_green(f"[TMP] {label} 已覆盖静态/动态词库中的全部有效译文字符。")
        return ""

    missing_path.write_text(missing_text, encoding="utf-8")
    missing_detail_path.write_text("\n".join(rows), encoding="utf-8")
    print("", flush=True)
    _log_red(f"[TMP][需要处理] 静态/动态词库的译文包含{label}不支持的字符。")
    _log_red(f"[TMP][需要处理] 不支持字符数: {len(missing_text)}")
    _log_red(f"[TMP][需要处理] 字符: {missing_text}")
    _log_red(f"[TMP][需要处理] 清单: {missing_path}")
    _log_red(f"[TMP][需要处理] 详情: {missing_detail_path}")
    if stop_on_missing:
        _log_red(
            f"[TMP][停止] 请先修改 trans.json 或 {STRINGLITERAL_TRANSLATIONS_FILENAME} 中对应译文，"
            f"或更换包含这些字符的{label}，然后重新执行菜单 8。"
        )
        if output_prefix == "translation_chars_missing_from_ttf":
            print("[TMP][提示] 可运行 工具脚本.py -> 主菜单 4. 清理 trans.json 中模板 TTF 不支持的字符。", flush=True)
            print(f"[TMP][提示] 命令行: python {cfg.root_dir / '工具脚本.py'} clean-unsupported-ttf-chars", flush=True)
    else:
        print(f"[TMP][提示] 这只影响{label}兼容性，不中断当前菜单 8 后续流程。", flush=True)
    return missing_text


def _filter_merged_chars_by_ttf_support(
    cfg: PipelineConfig,
    chars: str,
    supported: set[int] | None,
) -> tuple[str, str]:
    for stale_path in (
        cfg.stage_record_dir / "tmp_chars_missing_from_ttf.txt",
        cfg.stage_record_dir / "tmp_chars_missing_from_ttf.tsv",
    ):
        if stale_path.exists():
            stale_path.unlink()

    if supported is None:
        return chars, ""

    source_label = (
        f"{'老工具 SDF 模板字符 + ' if cfg.include_old_sdf_template_chars else ''}"
        "原游戏字体字符 + 译文字符 + 配置额外字符"
    )

    kept: list[str] = []
    missing: list[str] = []
    for char in unique_preserve_order(chars):
        if not _is_tmp_char_candidate(char):
            continue
        if ord(char) in supported:
            kept.append(char)
        else:
            missing.append(char)

    missing_text = "".join(unique_preserve_order(missing))
    if not missing_text:
        for stale_path in (
            cfg.stage_record_dir / "merged_chars_removed_unsupported_by_ttf.txt",
            cfg.stage_record_dir / "merged_chars_removed_unsupported_by_ttf.tsv",
        ):
            if stale_path.exists():
                stale_path.unlink()
        print(f"[TMP] {source_label}均被模板 TTF 覆盖。", flush=True)
        return "".join(kept), ""

    missing_path = cfg.stage_record_dir / "merged_chars_removed_unsupported_by_ttf.txt"
    missing_detail_path = cfg.stage_record_dir / "merged_chars_removed_unsupported_by_ttf.tsv"
    filtered = "".join(kept)
    missing_path.write_text(missing_text, encoding="utf-8")
    missing_detail_path.write_text(
        "\n".join(["code\tchar", *(f"U+{ord(char):04X}\t{char}" for char in missing_text)]),
        encoding="utf-8",
    )
    print(f"[TMP] {source_label}中有 {len(missing_text)} 个字符不被模板 TTF 支持，已从 tmp_chars.txt 候选字符中删除。", flush=True)
    print(f"[TMP] 删除字符清单: {missing_path}", flush=True)
    print(f"[TMP] 删除字符详情: {missing_detail_path}", flush=True)
    return filtered, missing_text


def _extract_chars_from_tmp_json(data: dict[str, Any]) -> str:
    chars: list[str] = []
    character_table = data.get("m_CharacterTable", {}).get("Array", [])
    for item in character_table:
        code = item.get("m_Unicode")
        if isinstance(code, int):
            try:
                chars.append(chr(code))
            except ValueError:
                continue
    if chars:
        return "".join(chars)

    creation_settings = data.get("m_CreationSettings", {})
    character_sequence = creation_settings.get("characterSequence")
    if isinstance(character_sequence, str) and character_sequence:
        return _chars_from_tmp_character_sequence(character_sequence)
    return ""


def _chars_from_tmp_character_sequence(character_sequence: str) -> str:
    compact = character_sequence.replace("\r", "").replace("\n", "").replace(" ", "")
    if not compact:
        return ""
    if not re.fullmatch(r"[0-9A-Fa-f,\-]+", compact):
        return compact

    chars: list[str] = []
    for part in compact.split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start = int(start_text, 16)
                end = int(end_text, 16)
            except ValueError:
                continue
            if start > end or end - start > 0x20000:
                continue
            chars.extend(chr(value) for value in range(start, end + 1))
            continue
        try:
            chars.append(chr(int(part, 16)))
        except ValueError:
            continue
    return "".join(chars)


def _iter_manifest_tmp_json_paths(cfg: PipelineConfig) -> list[Path]:
    paths: list[Path] = []
    indexed_items = load_tmp_manifest_index(cfg)
    if indexed_items is None:
        indexed_items = _iter_manifest_items(cfg)
    for _, manifest_dir, item in indexed_items:
        if not isinstance(item, dict) or _manifest_item_value(item, "TypeName") != "MonoBehaviour":
            continue
        asset_name = str(_manifest_item_value(item, "AssetName", ""))
        relative_path = _manifest_item_value(item, "RelativePath")
        if not isinstance(relative_path, str) or not relative_path:
            continue
        marker_text = f"{asset_name} {relative_path}".lower()
        if not any(marker in marker_text for marker in ("sdf", "fontasset", "font asset")):
            continue
        json_path = _resolve_manifest_item_path(manifest_dir, item)
        if json_path is not None and json_path.is_file() and json_path.suffix.lower() == ".json":
            paths.append(json_path)
    return paths


def collect_tmp_chars_from_resource_input(cfg: PipelineConfig) -> str:
    chars: list[str] = []
    json_files = _iter_manifest_tmp_json_paths(cfg)
    if not json_files:
        json_files = collect_json_files(cfg.resource_input_root)
    print(f"[TMP] 扫描资源导出目录 TMP 字符: {cfg.resource_input_root}，候选 JSON: {len(json_files)}", flush=True)
    for index, json_path in enumerate(json_files, start=1):
        if len(json_files) < 20 or index == 1 or index % 5 == 0:
            print(f"[TMP] 检查 TMP 字体 JSON: {index}/{len(json_files)} {json_path}", flush=True)
        try:
            data = read_json(json_path)
        except Exception:
            continue
        if isinstance(data, dict) and _is_tmp_font_asset(data):
            chars.append(_extract_chars_from_tmp_json(data))
    return "".join(unique_preserve_order("".join(chars)))


def build_merged_tmp_chars(cfg: PipelineConfig) -> Path:
    translated_chars_path = cfg.stage_record_dir / cfg.output_game_chars_txt
    print(f"[TMP] 翻译字符文件: {translated_chars_path}", flush=True)
    source_chars = translated_chars_path.read_text(encoding="utf-8") if translated_chars_path.is_file() else ""
    print(f"[TMP] 翻译字符数: {len(source_chars)}", flush=True)
    supported_ttf_chars = _supported_codepoints_from_ttf(cfg.ttf_template_path)
    missing_translation_chars = _report_translation_chars_missing_from_ttf(
        cfg,
        supported_ttf_chars,
        label="模板 TTF",
        output_prefix="translation_chars_missing_from_ttf",
    )
    old_sdf_template_path = cfg.root_dir / "templates" / "老工具的SDF模板.json"
    supported_old_sdf_chars = _supported_codepoints_from_tmp_json(old_sdf_template_path)
    for stale_path in (
        cfg.stage_record_dir / "translation_chars_missing_from_old_sdf_template.txt",
        cfg.stage_record_dir / "translation_chars_missing_from_old_sdf_template.tsv",
    ):
        if stale_path.exists():
            stale_path.unlink()
    if missing_translation_chars:
        raise SystemExit(1)
    all_old_sdf_template_chars = _chars_from_supported_codepoints(supported_old_sdf_chars)
    old_sdf_template_chars = all_old_sdf_template_chars if cfg.include_old_sdf_template_chars else ""
    print("[TMP] 模板 TTF 仅用于字符支持检查，不合并其全部字符。", flush=True)
    if cfg.include_old_sdf_template_chars:
        print(f"[TMP] 老工具 SDF 模板字符: {len(old_sdf_template_chars)} 个，将参与 tmp_chars.txt 合并。", flush=True)
    else:
        print(
            f"[TMP] 老工具 SDF 模板字符: {len(all_old_sdf_template_chars)} 个；"
            "配置 include_old_sdf_template_chars=false，跳过合并。",
            flush=True,
        )
    old_tmp_chars = collect_tmp_chars_from_resource_input(cfg)
    if old_tmp_chars:
        print(f"[TMP] 已从资源导出目录提取原 TMP 字符: {len(old_tmp_chars)} 个", flush=True)
    old_tmp_char_set = set(old_tmp_chars)
    extra_chars = getattr(cfg, "tmp_extra_chars", "") or ""
    if not isinstance(extra_chars, str):
        raise ValueError("tmp_extra_chars 必须是字符串，例如：霰髅")
    merged = "".join(unique_preserve_order(old_sdf_template_chars + old_tmp_chars + source_chars + extra_chars))
    merged = "".join(char for char in merged if _is_tmp_char_candidate(char))
    merged_before_filter_count = len(merged)
    merged, missing_from_ttf = _filter_merged_chars_by_ttf_support(cfg, merged, supported_ttf_chars)
    merged_char_set = set(merged)
    extra_kept = "".join(char for char in unique_preserve_order(extra_chars)
                         if char in merged_char_set and is_visible_char(char))
    if extra_chars:
        print(f"[TMP] 配置额外字符（去重且通过 TTF 支持检查）: {len(extra_kept)} 个：{extra_kept}", flush=True)
    added_chars = "".join(
        char
        for char in unique_preserve_order(source_chars)
        if char not in old_tmp_char_set and char in merged_char_set and is_visible_char(char)
    )
    output_path = cfg.stage_record_dir / cfg.output_tmp_chars_txt
    output_path.write_text(merged, encoding="utf-8")
    print(
        f"[TMP] "
        f"{'老工具 SDF 模板字符 + ' if cfg.include_old_sdf_template_chars else ''}"
        f"原游戏字体字符 + 译文字符 + 配置额外字符: {merged_before_filter_count} 个；"
        f"写入 tmp_chars.txt: {len(merged)} 个；删除模板 TTF 不支持字符: {len(missing_from_ttf)} 个",
        flush=True,
    )
    if added_chars:
        print(f"[TMP] 翻译新增 TMP 字符: {len(added_chars)} 个", flush=True)
    else:
        print("[TMP] 翻译没有新增 TMP 字符，当前原 TMP 字体字符已覆盖翻译结果。", flush=True)
    return output_path


def _copy_if_present(target: dict[str, Any], source: dict[str, Any], dotted_path: str) -> None:
    keys = dotted_path.split(".")
    source_cursor: Any = source
    target_cursor: Any = target
    for key in keys[:-1]:
        if not isinstance(source_cursor, dict) or key not in source_cursor:
            return
        source_cursor = source_cursor[key]
        if not isinstance(target_cursor, dict) or key not in target_cursor:
            return
        target_cursor = target_cursor[key]
    leaf = keys[-1]
    if isinstance(source_cursor, dict) and leaf in source_cursor and isinstance(target_cursor, dict):
        target_cursor[leaf] = copy.deepcopy(source_cursor[leaf])


def _sync_root_field_to_source(target: dict[str, Any], source: dict[str, Any], key: str) -> None:
    if key in source:
        target[key] = copy.deepcopy(source[key])
    else:
        target.pop(key, None)


def _sync_alias_group_to_source(target: dict[str, Any], source: dict[str, Any], keys: tuple[str, ...]) -> None:
    source_key = next((key for key in keys if key in source), None)
    for key in keys:
        target.pop(key, None)
    if source_key is not None:
        target[source_key] = copy.deepcopy(source[source_key])


def _is_tmp_font_asset(data: Any) -> bool:
    return (
        isinstance(data, dict)
        and isinstance(data.get("m_CharacterTable"), dict)
        and isinstance(data.get("m_GlyphTable"), dict)
        and isinstance(data.get("m_FaceInfo"), dict)
    )


def _merge_generated_font_data(generated: Any, original: Any) -> Any:
    """Overlay generated values while retaining fields only the game asset has."""
    if not isinstance(generated, dict) or not isinstance(original, dict):
        # Unity's YAML parser emits whole-number float fields as Python ints
        # (for example ``m_Scale: 1``).  Preserve the scalar kind from the
        # exported game JSON so schema checks still see Float rather than
        # Integer while retaining the newly generated metric value.
        if (
            isinstance(original, float)
            and isinstance(generated, (int, float))
            and not isinstance(generated, bool)
        ):
            return float(generated)
        if (
            isinstance(original, int)
            and not isinstance(original, bool)
            and isinstance(generated, float)
            and generated.is_integer()
        ):
            return int(generated)
        return copy.deepcopy(generated)

    merged = copy.deepcopy(original)
    for key, generated_value in generated.items():
        if key in original:
            merged[key] = _merge_generated_font_data(generated_value, original[key])
    return merged


def _build_tmp_font_replacement(template: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    # Keep the original FontAsset as the body and patch only generated glyph
    # data. This preserves fallback tables, source font references, face metrics,
    # material identity and other runtime structure from the game asset.
    new = copy.deepcopy(old)

    # Always derive from the source FontAsset, never the previous ToImport output.
    if isinstance(old.get("boldStyle"), (int, float)):
        new["boldStyle"] = old["boldStyle"] * TMP_BOLD_STRENGTH_SCALE

    for key in (
        "m_GlyphTable",
        "m_CharacterTable",
        "m_UsedGlyphRects",
        "m_FreeGlyphRects",
        "m_glyphInfoList",
        "m_KerningTable",
        "m_FontFeatureTable",
        "m_AtlasWidth",
        "m_AtlasHeight",
        "m_AtlasPadding",
        "m_AtlasRenderMode",
        "m_IsMultiAtlasTexturesEnabled",
        "m_ClearDynamicDataOnBuild",
    ):
        if key in template:
            new[key] = _merge_generated_font_data(template[key], old.get(key))

    if "m_AtlasTextureIndex" in template:
        new["m_AtlasTextureIndex"] = copy.deepcopy(template["m_AtlasTextureIndex"])

    if isinstance(template.get("m_FaceInfo"), dict) and isinstance(new.get("m_FaceInfo"), dict):
        patched_face_info = _merge_generated_font_data(template["m_FaceInfo"], old["m_FaceInfo"])
        for key in ("m_FamilyName", "m_StyleName", "m_UnitsPerEM"):
            if key in old["m_FaceInfo"]:
                patched_face_info[key] = copy.deepcopy(old["m_FaceInfo"][key])
        new["m_FaceInfo"] = patched_face_info

    if isinstance(template.get("atlas"), dict) and "atlas" in new:
        atlas_ref = copy.deepcopy(template["atlas"])
        if isinstance(old.get("atlas"), dict):
            for key in ("m_FileID", "m_PathID"):
                if key in old["atlas"]:
                    atlas_ref[key] = copy.deepcopy(old["atlas"][key])
        new["atlas"] = atlas_ref

    old_atlases = old.get("m_AtlasTextures", {}).get("Array", [])
    template_atlases = template.get("m_AtlasTextures", {}).get("Array", [])
    if isinstance(template_atlases, list) and template_atlases:
        patched_atlases: list[Any] = []
        for index, atlas in enumerate(template_atlases):
            patched = copy.deepcopy(atlas)
            if (
                isinstance(old_atlases, list)
                and index < len(old_atlases)
                and isinstance(old_atlases[index], dict)
                and isinstance(patched, dict)
            ):
                for key in ("m_FileID", "m_PathID"):
                    if key in old_atlases[index]:
                        patched[key] = copy.deepcopy(old_atlases[index][key])
            patched_atlases.append(patched)
        new["m_AtlasTextures"] = {"Array": patched_atlases}

    for glyph in new.get("m_GlyphTable", {}).get("Array", []):
        if isinstance(glyph, dict):
            glyph["m_ClassDefinitionType"] = 0
    return new


def _generated_tmp_material_floats(asset_path: Path) -> dict[str, Any]:
    if not asset_path.is_file():
        return {}

    yaml = _load_yaml_module()
    text = asset_path.read_text(encoding="utf-8-sig")
    documents = _split_unity_yaml_documents(text)
    material_document = next(
        (body for class_id, _, body in documents if class_id == 21 and body.startswith("Material:")),
        None,
    )
    if material_document is None:
        return {}

    loaded = yaml.safe_load(material_document)
    material = loaded.get("Material") if isinstance(loaded, dict) else None
    saved_properties = material.get("m_SavedProperties") if isinstance(material, dict) else None
    floats = saved_properties.get("m_Floats") if isinstance(saved_properties, dict) else None
    if not isinstance(floats, list):
        return {}

    values: dict[str, Any] = {}
    for item in floats:
        if not isinstance(item, dict) or len(item) != 1:
            continue
        name, value = next(iter(item.items()))
        if name in TMP_SDF_MATERIAL_FLOAT_KEYS and isinstance(value, (int, float)):
            values[name] = value
    return values


def _material_path_id(font_json: dict[str, Any]) -> tuple[int, int] | None:
    material = font_json.get("m_Material")
    if not isinstance(material, dict):
        material = font_json.get("material")
    if not isinstance(material, dict):
        return None
    try:
        return (
            int(material.get("m_FileID", 0) or 0),
            int(material.get("m_PathID", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def _material_main_texture_refs(material_json: dict[str, Any]) -> list[tuple[int, int]]:
    """Return the Texture2D references sampled by a material's ``_MainTex``."""
    saved_properties = material_json.get("m_SavedProperties")
    tex_envs = saved_properties.get("m_TexEnvs") if isinstance(saved_properties, dict) else None
    array = tex_envs.get("Array") if isinstance(tex_envs, dict) else tex_envs
    if not isinstance(array, list):
        return []

    refs: list[tuple[int, int]] = []
    for item in array:
        if not isinstance(item, dict) or item.get("first") != "_MainTex":
            continue
        value = item.get("second")
        texture = value.get("m_Texture") if isinstance(value, dict) else None
        if not isinstance(texture, dict):
            continue
        try:
            ref = (
                int(texture.get("m_FileID", 0) or 0),
                int(texture.get("m_PathID", 0) or 0),
            )
        except (TypeError, ValueError):
            continue
        if ref[1] != 0 and ref not in refs:
            refs.append(ref)
    return refs


def _apply_generated_sdf_material_floats(
    source_material: dict[str, Any],
    generated_values: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    replacement = copy.deepcopy(source_material)
    saved_properties = replacement.get("m_SavedProperties")
    floats = saved_properties.get("m_Floats") if isinstance(saved_properties, dict) else None
    array = floats.get("Array") if isinstance(floats, dict) else None
    if not isinstance(array, list):
        return replacement, []

    updated: list[str] = []
    for item in array:
        if not isinstance(item, dict):
            continue
        name = item.get("first")
        if name == "_WeightBold" and "second" in item:
            value = generated_values.get(name, item["second"])
            if isinstance(value, (int, float)):
                item["second"] = value * TMP_BOLD_STRENGTH_SCALE
                updated.append(name)
            continue
        if name not in generated_values or "second" not in item:
            continue
        item["second"] = copy.deepcopy(generated_values[name])
        updated.append(name)
    return replacement, updated


def _load_yaml_module():
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to parse Unity text assets. Please install it with: pip install pyyaml") from exc
    return yaml


def _split_unity_yaml_documents(text: str) -> list[tuple[int, int, str]]:
    matches = list(UNITY_YAML_DOCUMENT_RE.finditer(text))
    documents: list[tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        documents.append((int(match.group("class_id")), int(match.group("file_id")), text[start:end].strip()))
    return documents


def _to_int_if_possible(value: Any) -> Any:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _is_unity_object_reference(value: Any) -> bool:
    return isinstance(value, dict) and "fileID" in value and set(value).issubset({"fileID", "guid", "type"})


def _convert_unity_object_reference(value: dict[str, Any]) -> dict[str, Any]:
    file_id = _to_int_if_possible(value.get("fileID", 0))
    if value.get("guid"):
        converted: dict[str, Any] = {
            "m_FileID": file_id,
            "m_PathID": 0,
            "m_GUID": value.get("guid"),
        }
        if "type" in value:
            converted["m_Type"] = value.get("type")
        return converted
    return {
        "m_FileID": 0,
        "m_PathID": file_id,
    }


def _convert_unity_yaml_value(value: Any) -> Any:
    if _is_unity_object_reference(value):
        return _convert_unity_object_reference(value)
    if isinstance(value, list):
        return {"Array": [_convert_unity_yaml_value(item) for item in value]}
    if isinstance(value, dict):
        return {key: _convert_unity_yaml_value(item) for key, item in value.items()}
    return value


def _normalize_generated_tmp_font_json(data: dict[str, Any]) -> dict[str, Any]:
    normalized = _convert_unity_yaml_value(data)
    for key in TMP_YAML_ONLY_KEYS:
        normalized.pop(key, None)
    if "material" in normalized and "m_Material" not in normalized:
        normalized["m_Material"] = normalized.pop("material")
    return normalized


def _reshape_tmp_font_json(data: dict[str, Any], shape: dict[str, Any]) -> dict[str, Any]:
    reshaped: dict[str, Any] = {}
    for key, default_value in shape.items():
        aliases = TMP_FIELD_ALIASES.get(key, (key,))
        source_key = next((alias for alias in aliases if alias in data), None)
        if source_key is None:
            reshaped[key] = copy.deepcopy(default_value)
        else:
            reshaped[key] = copy.deepcopy(data[source_key])
    return reshaped


def export_generated_tmp_json_template(
    cfg: PipelineConfig,
    asset_path: Path | None = None,
    output_path: Path | None = None,
) -> Path:
    asset_path = asset_path or _sdf_generated_asset_path(cfg)
    output_path = output_path or asset_path.with_suffix(".json")
    if not asset_path.is_file():
        raise FileNotFoundError(f"Generated TMP asset not found: {asset_path}")

    yaml = _load_yaml_module()
    text = asset_path.read_text(encoding="utf-8-sig")
    documents = _split_unity_yaml_documents(text)
    mono_document = next((body for class_id, _, body in documents if class_id == 114 and body.startswith("MonoBehaviour:")), None)
    if mono_document is None:
        raise ValueError(f"TMP MonoBehaviour document was not found in generated asset: {asset_path}")

    loaded = yaml.safe_load(mono_document)
    if not isinstance(loaded, dict) or not isinstance(loaded.get("MonoBehaviour"), dict):
        raise ValueError(f"TMP MonoBehaviour document is invalid: {asset_path}")

    generated = _normalize_generated_tmp_font_json(loaded["MonoBehaviour"])
    if cfg.tmp_template_json_path.is_file():
        shape = read_json(cfg.tmp_template_json_path)
        if isinstance(shape, dict):
            generated = _reshape_tmp_font_json(generated, shape)
            print(f"[TMP] JSON 字段形状参考: {cfg.tmp_template_json_path}", flush=True)

    write_json(output_path, generated)
    print(f"[TMP] 已解析生成 TMP JSON 模板: {output_path}", flush=True)
    return output_path


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(data, crc) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _write_rgba_png(path: Path, width: int, height: int, rgba: bytes | bytearray, flip_y: bool = True) -> None:
    stride = width * 4
    expected_size = stride * height
    if len(rgba) != expected_size:
        raise ValueError(f"RGBA data size mismatch: expected {expected_size}, got {len(rgba)}")

    rows: list[bytes] = []
    for target_y in range(height):
        source_y = height - 1 - target_y if flip_y else target_y
        start = source_y * stride
        rows.append(b"\x00" + bytes(rgba[start:start + stride]))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=6))
        + _png_chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _decode_unity_texture_to_rgba(texture: dict[str, Any]) -> tuple[int, int, bytearray]:
    width = int(texture.get("m_Width", 0) or 0)
    height = int(texture.get("m_Height", 0) or 0)
    texture_format = int(texture.get("m_TextureFormat", 0) or 0)
    hex_data = texture.get("_typelessdata")
    if width <= 0 or height <= 0:
        raise ValueError("Texture2D width/height is invalid.")
    if not isinstance(hex_data, str) or not hex_data.strip():
        raise ValueError("Texture2D does not contain inline _typelessdata.")

    raw = bytes.fromhex("".join(hex_data.split()))
    pixel_count = width * height
    rgba = bytearray(pixel_count * 4)

    if texture_format == 1:  # TextureFormat.Alpha8
        expected_size = pixel_count
        if len(raw) < expected_size:
            raise ValueError(f"Alpha8 texture data is truncated: expected {expected_size}, got {len(raw)}")
        for index, alpha in enumerate(raw[:expected_size]):
            target = index * 4
            rgba[target:target + 3] = bytes((alpha, alpha, alpha))
            rgba[target + 3] = alpha
        return width, height, rgba

    if texture_format == 3:  # TextureFormat.RGB24
        expected_size = pixel_count * 3
        if len(raw) < expected_size:
            raise ValueError(f"RGB24 texture data is truncated: expected {expected_size}, got {len(raw)}")
        for index in range(pixel_count):
            source = index * 3
            target = index * 4
            rgba[target:target + 3] = raw[source:source + 3]
            rgba[target + 3] = 255
        return width, height, rgba

    if texture_format == 4:  # TextureFormat.RGBA32
        expected_size = pixel_count * 4
        if len(raw) < expected_size:
            raise ValueError(f"RGBA32 texture data is truncated: expected {expected_size}, got {len(raw)}")
        rgba[:] = raw[:expected_size]
        return width, height, rgba

    raise NotImplementedError(
        f"Unsupported Texture2D format {texture_format}. "
        "Currently supported formats are Alpha8(1), RGB24(3), and RGBA32(4)."
    )


def export_generated_tmp_atlas_png(
    cfg: PipelineConfig,
    asset_path: Path | None = None,
    output_path: Path | None = None,
) -> list[Path]:
    asset_path = asset_path or _sdf_generated_asset_path(cfg)
    output_path = output_path or asset_path.with_suffix(".png")
    if not asset_path.is_file():
        raise FileNotFoundError(f"Generated TMP asset not found: {asset_path}")

    yaml = _load_yaml_module()
    text = asset_path.read_text(encoding="utf-8-sig")
    texture_documents = [
        (file_id, body)
        for class_id, file_id, body in _split_unity_yaml_documents(text)
        if class_id == 28 and body.startswith("Texture2D:")
    ]
    if not texture_documents:
        raise ValueError(f"Texture2D document was not found in generated asset: {asset_path}")

    outputs: list[Path] = []
    for index, (file_id, body) in enumerate(texture_documents):
        loaded = yaml.safe_load(body)
        if not isinstance(loaded, dict) or not isinstance(loaded.get("Texture2D"), dict):
            raise ValueError(f"Texture2D document is invalid in generated asset: {asset_path}")

        width, height, rgba = _decode_unity_texture_to_rgba(loaded["Texture2D"])
        target_path = output_path
        if len(texture_documents) > 1:
            target_path = output_path.with_name(f"{output_path.stem}_atlas_{index}{output_path.suffix}")
        _write_rgba_png(target_path, width, height, rgba, flip_y=True)
        outputs.append(target_path)
        print(f"[TMP] 已解析 Texture2D 图集 PNG: {target_path} (PathID={file_id}, {width}x{height})", flush=True)

    return outputs


def _path_from_manifest_relative(relative_path: str) -> Path:
    return Path(*relative_path.replace("\\", "/").split("/"))


def _sanitize_resource_name(value: str) -> str:
    invalid = set('<>:"/\\|?*')
    sanitized = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return sanitized.strip()


def _starts_with_bundle_prefix(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    return normalized == "bundle" or normalized.startswith("bundle/")


def _manifest_item_value(item: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in item:
        return item[key]
    camel_key = key[:1].lower() + key[1:]
    return item.get(camel_key, default)


def _resolve_manifest_item_path(manifest_dir: Path, item: dict[str, Any]) -> Path | None:
    relative = str(_manifest_item_value(item, "RelativePath", ""))
    if not relative:
        return None
    relative_path = _path_from_manifest_relative(relative)
    candidates = [manifest_dir / relative_path]
    bundle_entry_name = str(_manifest_item_value(item, "BundleEntryName", "") or "")
    if bundle_entry_name and not _starts_with_bundle_prefix(relative):
        candidates.append(manifest_dir / "bundle" / _sanitize_resource_name(bundle_entry_name) / relative_path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _iter_manifest_items(cfg: PipelineConfig) -> list[tuple[Path, Path, dict[str, Any]]]:
    cached_items = load_tmp_manifest_index(cfg)
    if cached_items is not None:
        print(f"[TMP替换] 使用脚本1生成的 manifest 索引: {tmp_manifest_index_path(cfg)}，条目: {len(cached_items)}", flush=True)
        return cached_items

    print(f"[TMP替换] 未找到 manifest 索引，回退扫描资源导出目录: {cfg.resource_input_root}", flush=True)
    items: list[tuple[Path, Path, dict[str, Any]]] = []
    for manifest_path in sorted(cfg.resource_input_root.rglob("manifest.json")):
        try:
            manifest = read_json(manifest_path)
        except Exception as exc:
            print(f"[TMP替换] 跳过无效 manifest: {manifest_path} ({exc})", flush=True)
            continue
        manifest_items = manifest.get("Items") if isinstance(manifest, dict) else None
        if not isinstance(manifest_items, list):
            continue
        manifest_dir = manifest_path.parent
        for item in manifest_items:
            if isinstance(item, dict):
                items.append((manifest_path, manifest_dir, item))
    return items


def _item_path_id(item: dict[str, Any]) -> int | None:
    value = _manifest_item_value(item, "PathId")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _item_bundle_entry(item: dict[str, Any]) -> str:
    return str(_manifest_item_value(item, "BundleEntryName", "") or "")


def _overlay_path_for_input_path(cfg: PipelineConfig, input_path: Path) -> Path:
    return cfg.import_overlay_dir / input_path.relative_to(cfg.resource_input_root)


def _input_path_for_relative(cfg: PipelineConfig, relative: str | Path) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else cfg.resource_input_root / path


def _bundle_key_for_json_path(cfg: PipelineConfig, json_path: Path) -> str:
    relative = json_path.relative_to(cfg.resource_input_root)
    parts = list(relative.parts)
    for index, part in enumerate(parts):
        if part in {"MonoBehaviour", "TextAsset", "Texture2D", "Material", "Font"}:
            return str(Path(*parts[:index]))
    return str(relative.parent)


def _atlas_path_ids(font_json: dict[str, Any]) -> list[int]:
    values: list[int] = []
    atlas_array = font_json.get("m_AtlasTextures", {}).get("Array", [])
    if not isinstance(atlas_array, list):
        return values
    for item in atlas_array:
        if not isinstance(item, dict):
            continue
        try:
            file_id = int(item.get("m_FileID", 0) or 0)
            path_id = int(item.get("m_PathID", 0) or 0)
        except (TypeError, ValueError):
            continue
        if file_id == 0 and path_id != 0:
            values.append(path_id)
    return values


def _copy_atlas_png_for_import(source_path: Path, target_path: Path) -> None:
    shutil.copy2(source_path, target_path)


def _read_png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as handle:
            header = handle.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return width, height


def _is_valid_existing_texture_png(path: Path) -> bool:
    dimensions = _read_png_dimensions(path)
    return dimensions is not None and dimensions[0] > 0 and dimensions[1] > 0


def _used_tmp_font_paths_from_font_map(cfg: PipelineConfig) -> set[Path]:
    font_map_path = cfg.stage_record_dir / cfg.output_font_map_json
    if not font_map_path.is_file():
        print(f"[TMP替换] 未找到 font_map.json，不按字体使用记录过滤: {font_map_path}", flush=True)
        return set()
    try:
        font_map = read_json(font_map_path)
    except Exception as exc:
        print(f"[TMP替换] 读取 font_map.json 失败，不按字体使用记录过滤: {exc}", flush=True)
        return set()
    if not isinstance(font_map, dict):
        return set()

    paths: set[Path] = set()
    unresolved = 0
    for key, value in font_map.items():
        candidates: list[Any] = [key]
        if isinstance(value, dict):
            candidates.append(value.get("font_file"))
        resolved = False
        for candidate in candidates:
            if not isinstance(candidate, str) or not candidate:
                continue
            path = Path(candidate)
            if path.is_file() and path.suffix.lower() == ".json":
                paths.add(path.resolve())
                resolved = True
        if not resolved:
            unresolved += 1

    if paths:
        print(f"[TMP替换] font_map 已解析实际使用字体文件: {len(paths)} 个，未解析引用: {unresolved} 个", flush=True)
    else:
        print(f"[TMP替换] font_map 没有解析到实际字体文件，未解析引用: {unresolved} 个", flush=True)
    return paths


def _load_path_id_map_for_tmp(cfg: PipelineConfig) -> dict[str, dict[str, str]]:
    path = cfg.stage_record_dir / cfg.output_path_id_map_json
    if not path.is_file():
        return {}
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


def _load_file_id_map_for_tmp(cfg: PipelineConfig) -> dict[str, dict[str, str]]:
    path = cfg.stage_record_dir / cfg.output_file_id_map_json
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}



def _i2_tmp_font_and_material_targets(cfg: PipelineConfig) -> tuple[set[Path], set[str]]:
    report_path = cfg.stage_record_dir / "i2_text_sdf_and_effect_components.json"
    font_relatives: set[str] = set()
    material_relatives: set[str] = set()
    if report_path.is_file():
        try:
            report = read_json(report_path)
        except Exception:
            report = None
        items = report.get("items") if isinstance(report, dict) else None
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                font_asset = item.get("font_asset")
                relative = font_asset.get("file") if isinstance(font_asset, dict) else None
                if isinstance(relative, str) and relative:
                    font_relatives.add(relative)
                materials = item.get("materials")
                if isinstance(materials, list):
                    for material in materials:
                        if not isinstance(material, dict):
                            continue
                        material_relative = material.get("material_file")
                        if isinstance(material_relative, str) and material_relative:
                            material_relatives.add(material_relative)

    if not font_relatives:
        runtime_report_path = cfg.stage_record_dir / cfg.output_runtime_text_binding_report_json
        if runtime_report_path.is_file():
            try:
                runtime_report = read_json(runtime_report_path)
            except Exception:
                runtime_report = None
            sources = runtime_report.get("sources") if isinstance(runtime_report, dict) else None
            path_id_map = _load_path_id_map_for_tmp(cfg)
            file_id_map = _load_file_id_map_for_tmp(cfg)
            if isinstance(sources, list) and path_id_map:
                for source in sources:
                    if not isinstance(source, dict) or source.get("kind") != "i2_language_table":
                        continue
                    component_file = source.get("component_file") or source.get("file")
                    if not isinstance(component_file, str):
                        continue
                    component_path = _input_path_for_relative(cfg, component_file)
                    if not component_path.is_file():
                        continue
                    try:
                        data = read_json(component_path)
                    except Exception:
                        continue
                    font_ref = data.get("m_fontAsset") if isinstance(data, dict) else None
                    if not isinstance(font_ref, dict):
                        continue
                    file_id = font_ref.get("m_FileID")
                    path_id = font_ref.get("m_PathID")
                    if not isinstance(file_id, int) or not isinstance(path_id, int) or path_id <= 0:
                        continue
                    asset_key = _bundle_key_for_json_path(cfg, component_path)
                    target_asset = asset_key if file_id == 0 else file_id_map.get(asset_key, {}).get(str(file_id))
                    relative = path_id_map.get(target_asset or "", {}).get(str(path_id))
                    if isinstance(relative, str) and relative:
                        font_relatives.add(relative)

    font_paths = {
        _input_path_for_relative(cfg, relative).resolve()
        for relative in font_relatives
    }
    return font_paths, material_relatives


def prepare_generated_tmp_import_replacements(
    cfg: PipelineConfig,
    template_json_path: Path | None = None,
    atlas_png_path: Path | None = None,
) -> dict[str, int]:
    from .translation import collect_translated_text_effect_material_sources, disable_tmp_sdf_material_effects

    template_json_path = template_json_path or (_sdf_generated_template_dir(cfg) / "generated_tmp_font.json")
    atlas_png_path = atlas_png_path or (_sdf_generated_template_dir(cfg) / "generated_tmp_font.png")
    generated_asset_path = _sdf_generated_asset_path(cfg)
    if not template_json_path.is_file():
        raise FileNotFoundError(f"Generated TMP JSON template not found: {template_json_path}")
    if not atlas_png_path.is_file():
        raise FileNotFoundError(f"Generated TMP atlas PNG not found: {atlas_png_path}")
    if not cfg.resource_input_root.is_dir():
        raise FileNotFoundError(f"Resource input root not found: {cfg.resource_input_root}")

    print(f"[TMP替换] 字体 JSON 模板: {template_json_path}", flush=True)
    print(f"[TMP替换] 字体 PNG 图集: {atlas_png_path}", flush=True)
    print(f"[TMP替换] 导入覆盖层: {cfg.import_overlay_dir}", flush=True)

    if cfg.import_overlay_dir.exists():
        shutil.rmtree(cfg.import_overlay_dir)
    cfg.import_overlay_dir.mkdir(parents=True, exist_ok=True)

    material_work_dir = cfg.workspace_root / "temp" / "sdf_material_work"
    if material_work_dir.exists():
        shutil.rmtree(material_work_dir)
    material_work_dir.mkdir(parents=True, exist_ok=True)

    template = read_json(template_json_path)
    if not _is_tmp_font_asset(template):
        raise ValueError(f"Generated TMP JSON template is not a TMP FontAsset: {template_json_path}")

    generated_values = _generated_tmp_material_floats(generated_asset_path)
    if generated_values:
        print(f"[TMP替换] 已读取生成字体材质参数: {len(generated_values)} 项", flush=True)
    else:
        print(f"[TMP替换][提示] 未读取到生成字体材质参数，仅执行材质阴影/描边清理。", flush=True)

    used_tmp_font_paths = _used_tmp_font_paths_from_font_map(cfg)
    reported_tmp_paths: set[Path] | None = None
    bitmap_report_path = cfg.stage_record_dir / "bitmap_font_detection.json"
    if bitmap_report_path.is_file():
        try:
            bitmap_report = read_json(bitmap_report_path)
            reported_sources = bitmap_report.get("tmp_sdf_sources")
            reported_count = int(bitmap_report.get("tmp_sdf_count", 0) or 0)
            if (
                isinstance(reported_sources, list)
                and (reported_sources or reported_count == 0)
            ):
                reported_tmp_paths = {
                    (cfg.resource_input_root / str(relative)).resolve()
                    for relative in reported_sources
                    if str(relative).strip()
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(
                f"[TMP替换][提示] 字体检测报告无法用于候选预筛选，将回退全量扫描: {exc}",
                flush=True,
            )

    all_items = list(_iter_manifest_items(cfg))
    texture_items: dict[tuple[Path, str, int], tuple[Path, dict[str, Any]]] = {}
    material_items: dict[tuple[Path, str, int], tuple[Path, dict[str, Any]]] = {}
    mono_items: list[tuple[Path, Path, dict[str, Any]]] = []
    for manifest_path, manifest_dir, item in all_items:
        type_name = str(_manifest_item_value(item, "TypeName", "") or "")
        path_id = _item_path_id(item)
        if type_name == "Texture2D" and path_id is not None:
            texture_items[(manifest_path, _item_bundle_entry(item), path_id)] = (manifest_dir, item)
        elif type_name == "Material" and path_id is not None:
            material_items[(manifest_path, _item_bundle_entry(item), path_id)] = (manifest_dir, item)
        elif type_name == "MonoBehaviour":
            if reported_tmp_paths is not None:
                item_path = _resolve_manifest_item_path(manifest_dir, item)
                if item_path is None or item_path.resolve() not in reported_tmp_paths:
                    continue
            mono_items.append((manifest_path, manifest_dir, item))

    path_id_map = _load_path_id_map_for_tmp(cfg)
    file_id_map = _load_file_id_map_for_tmp(cfg)
    material_targets: dict[str, dict[str, Any]] = {}

    def add_material_target(relative: str, reason: str, source: str | None = None) -> None:
        if not relative:
            return
        target = material_targets.setdefault(
            relative,
            {"reasons": set(), "sources": [], "disable_effects": False, "sync_sdf_material": True},
        )
        target["reasons"].add(reason)
        if source:
            target["sources"].append(source)
        if reason == "translated_text_effect_material":
            target["disable_effects"] = True

    try:
        translated_material_sources = collect_translated_text_effect_material_sources(
            cfg,
            include_i2_bound_materials=True,
        )
    except FileNotFoundError as exc:
        translated_material_sources = {}
        print(f"[TMP替换][材质][提示] 缺少扫描记录，跳过译文材质收集: {exc}", flush=True)

    for relative, sources in translated_material_sources.items():
        for source in sources:
            source_text = ""
            if isinstance(source, dict):
                source_text = str(source.get("text_file") or source.get("file") or "")
            add_material_target(relative, "translated_text_effect_material", source_text)

    if used_tmp_font_paths:
        print(
            "[TMP替换] font_map 直接引用仅用于诊断，不再排除其他 TMP FontAsset；"
            f"候选 MonoBehaviour={len(mono_items)}",
            flush=True,
        )
    if reported_tmp_paths is not None:
        print(
            "[TMP替换] 复用脚本 0 字体检测报告预筛选 TMP FontAsset："
            f"报告源={len(reported_tmp_paths)}，manifest 命中={len(mono_items)}",
            flush=True,
        )

    print(
        f"[TMP替换] manifest 条目: MonoBehaviour={len(mono_items)}, "
        f"Texture2D={len(texture_items)}, Material={len(material_items)}",
        flush=True,
    )

    font_count = 0
    texture_count = 0
    material_count = 0
    material_parameter_updates = 0
    material_effect_changes = 0
    written_textures: set[Path] = set()
    material_main_textures: set[Path] = set()
    missing_textures: list[dict[str, Any]] = []
    skipped_textures: list[dict[str, Any]] = []
    skipped_fonts: list[dict[str, Any]] = []
    replacement_records: list[dict[str, Any]] = []
    material_records: list[dict[str, Any]] = []
    skipped_materials: list[dict[str, Any]] = []

    for index, (manifest_path, manifest_dir, item) in enumerate(mono_items, start=1):
        if index == 1 or index % 200 == 0:
            print(f"[TMP替换] 扫描 MonoBehaviour: {index}/{len(mono_items)}", flush=True)

        old_json_path = _resolve_manifest_item_path(manifest_dir, item)
        if old_json_path is None or not old_json_path.is_file():
            continue
        try:
            old = read_json(old_json_path)
        except Exception:
            continue
        if not _is_tmp_font_asset(old):
            continue

        target_json_path = _overlay_path_for_input_path(cfg, old_json_path)
        texture_outputs: list[str] = []
        for atlas_path_id in _atlas_path_ids(old):
            texture_key = (manifest_path, _item_bundle_entry(item), atlas_path_id)
            texture_entry = texture_items.get(texture_key)
            if texture_entry is None:
                missing_textures.append(
                    {
                        "font": str(old_json_path),
                        "atlas_path_id": atlas_path_id,
                        "bundle_entry": _item_bundle_entry(item),
                    }
                )
                continue
            texture_manifest_dir, texture_item = texture_entry
            old_texture_path = _resolve_manifest_item_path(texture_manifest_dir, texture_item)
            if old_texture_path is None or not old_texture_path.is_file():
                skipped_textures.append(
                    {
                        "font": str(old_json_path),
                        "atlas_path_id": atlas_path_id,
                        "reason": "source texture png was not exported",
                    }
                )
                continue
            if not _is_valid_existing_texture_png(old_texture_path):
                skipped_textures.append(
                    {
                        "font": str(old_json_path),
                        "atlas_path_id": atlas_path_id,
                        "source_texture": str(old_texture_path),
                        "reason": "source texture png is missing a valid non-zero PNG size",
                    }
                )
                continue
            target_texture_path = _overlay_path_for_input_path(cfg, old_texture_path)
            if target_texture_path not in written_textures:
                target_texture_path.parent.mkdir(parents=True, exist_ok=True)
                _copy_atlas_png_for_import(atlas_png_path, target_texture_path)
                written_textures.add(target_texture_path)
                texture_count += 1
            texture_outputs.append(str(target_texture_path))

        if not texture_outputs:
            if target_json_path.exists():
                target_json_path.unlink()
            skipped_fonts.append(
                {
                    "source_json": str(old_json_path),
                    "font_name": old.get("m_Name", ""),
                    "path_id": _item_path_id(item),
                    "bundle_entry": _item_bundle_entry(item),
                    "reason": "no valid atlas texture replacement was prepared",
                }
            )
            print(
                f"[TMP替换][跳过字体] {old_json_path}：没有可替换的有效图集，避免字体表和贴图不一致。",
                flush=True,
            )
            continue

        new = _build_tmp_font_replacement(template, old)
        write_json(target_json_path, new)
        font_count += 1

        material_ref = _material_path_id(old)
        material_relative = ""
        material_path: Path | None = None
        material_entry: tuple[Path, dict[str, Any]] | None = None
        material_texture_outputs: list[str] = []
        if material_ref is not None:
            material_file_id, material_path_id = material_ref
            if material_file_id == 0 and material_path_id != 0:
                asset_key = _bundle_key_for_json_path(cfg, old_json_path)
                material_relative = path_id_map.get(asset_key, {}).get(str(material_path_id), "")
                material_entry = material_items.get((manifest_path, _item_bundle_entry(item), material_path_id))
            if not material_relative:
                if material_entry is not None:
                    resolved_material_path = _resolve_manifest_item_path(material_entry[0], material_entry[1])
                    if resolved_material_path is not None and resolved_material_path.is_file():
                        material_path = resolved_material_path
                        material_relative = str(resolved_material_path.relative_to(cfg.resource_input_root))
            if material_relative:
                add_material_target(material_relative, "font_default_material", str(old_json_path))
                if material_path is None:
                    candidate = _input_path_for_relative(cfg, material_relative)
                    if candidate.is_file():
                        material_path = candidate

        if material_path is not None:
            try:
                material_json = read_json(material_path)
            except Exception:
                material_json = None
            if isinstance(material_json, dict):
                material_asset_key = _bundle_key_for_json_path(cfg, material_path)
                for texture_file_id, texture_path_id in _material_main_texture_refs(material_json):
                    source_texture_path: Path | None = None
                    target_asset = (
                        material_asset_key
                        if texture_file_id == 0
                        else file_id_map.get(material_asset_key, {}).get(str(texture_file_id), "")
                    )
                    texture_relative = (
                        path_id_map.get(target_asset, {}).get(str(texture_path_id), "")
                        if target_asset
                        else ""
                    )
                    if texture_relative:
                        candidate = _input_path_for_relative(cfg, texture_relative)
                        if candidate.is_file():
                            source_texture_path = candidate
                    if source_texture_path is None and texture_file_id == 0:
                        texture_entry = texture_items.get(
                            (manifest_path, _item_bundle_entry(item), texture_path_id)
                        )
                        if texture_entry is not None:
                            candidate = _resolve_manifest_item_path(texture_entry[0], texture_entry[1])
                            if candidate is not None and candidate.is_file():
                                source_texture_path = candidate

                    if source_texture_path is None:
                        missing_textures.append(
                            {
                                "font": str(old_json_path),
                                "material": str(material_path),
                                "atlas_path_id": texture_path_id,
                                "file_id": texture_file_id,
                                "reason": "material _MainTex texture could not be resolved",
                            }
                        )
                        continue
                    if not _is_valid_existing_texture_png(source_texture_path):
                        skipped_textures.append(
                            {
                                "font": str(old_json_path),
                                "material": str(material_path),
                                "atlas_path_id": texture_path_id,
                                "source_texture": str(source_texture_path),
                                "reason": "material _MainTex texture is missing a valid non-zero PNG size",
                            }
                        )
                        continue

                    target_texture_path = _overlay_path_for_input_path(cfg, source_texture_path)
                    if target_texture_path not in written_textures:
                        target_texture_path.parent.mkdir(parents=True, exist_ok=True)
                        _copy_atlas_png_for_import(atlas_png_path, target_texture_path)
                        written_textures.add(target_texture_path)
                        texture_count += 1
                    material_main_textures.add(target_texture_path)
                    material_texture_outputs.append(str(target_texture_path))

        print(f"[TMP替换] {old_json_path} -> {target_json_path}", flush=True)
        replacement_records.append(
            {
                "source_json": str(old_json_path),
                "replacement_json": str(target_json_path),
                "replacement_textures": texture_outputs,
                "material_main_texture_replacements": material_texture_outputs,
                "font_name": old.get("m_Name", ""),
                "path_id": _item_path_id(item),
                "bundle_entry": _item_bundle_entry(item),
                "material": material_relative,
            }
        )

    # Runtime localization can select material presets which no Text component
    # references in the serialized scene. Match their actual atlas dependency,
    # not a game-specific mapping field or material name.
    from .translation import _is_tmp_sdf_material_json
    replaced_atlases = {path.resolve() for path in written_textures}
    for (manifest_path, _entry, _id), (manifest_dir, item) in material_items.items():
        material_path = _resolve_manifest_item_path(manifest_dir, item)
        if material_path is None or not material_path.is_file():
            continue
        material_json = read_json(material_path)
        if not _is_tmp_sdf_material_json(material_json):
            continue
        asset_key = _bundle_key_for_json_path(cfg, material_path)
        for file_id, path_id in _material_main_texture_refs(material_json):
            target_asset = asset_key if file_id == 0 else file_id_map.get(asset_key, {}).get(str(file_id), "")
            texture_relative = path_id_map.get(target_asset, {}).get(str(path_id), "")
            if not texture_relative and target_asset:
                entry_name = target_asset.replace("\\", "/").rsplit("/", 1)[-1]
                texture_entry = texture_items.get((manifest_path, entry_name, path_id))
                if texture_entry is not None:
                    texture_path = _resolve_manifest_item_path(texture_entry[0], texture_entry[1])
                    if texture_path is not None:
                        texture_relative = str(texture_path.relative_to(cfg.resource_input_root))
            if not texture_relative:
                continue
            texture_output = _overlay_path_for_input_path(cfg, _input_path_for_relative(cfg, texture_relative))
            if texture_output.resolve() in replaced_atlases:
                relative = str(material_path.relative_to(cfg.resource_input_root))
                add_material_target(relative, "replaced_font_atlas_material", texture_relative)
                material_targets[relative]["disable_effects"] = True
                break

    for relative, target in sorted(material_targets.items()):
        source_material_path = _input_path_for_relative(cfg, relative)
        if not source_material_path.is_file():
            skipped_materials.append({"material": relative, "reason": "source material json was not exported"})
            continue
        try:
            source_material = read_json(source_material_path)
        except Exception as exc:
            skipped_materials.append({"material": relative, "reason": f"failed to read material json: {exc}"})
            continue

        work_path = material_work_dir / Path(relative)
        work_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_material_path, work_path)

        replacement = source_material
        updated: list[str] = []
        if target.get("sync_sdf_material"):
            replacement, updated = _apply_generated_sdf_material_floats(replacement, generated_values)

        disabled = 0
        effect_kinds: set[str] = set()
        disable_reason = ""
        if target.get("disable_effects"):
            disabled, effect_kinds, disable_reason = disable_tmp_sdf_material_effects(replacement)

        if not updated and not disabled:
            skipped_materials.append(
                {
                    "material": relative,
                    "reason": disable_reason or "no material fields changed",
                    "reasons": sorted(target.get("reasons", [])),
                }
            )
            continue

        write_json(work_path, replacement)
        target_material_path = _overlay_path_for_input_path(cfg, source_material_path)
        write_json(target_material_path, replacement)
        material_count += 1
        material_parameter_updates += len(updated)
        material_effect_changes += disabled
        material_records.append(
            {
                "source_json": str(source_material_path),
                "work_json": str(work_path),
                "replacement_json": str(target_material_path),
                "updated_sdf_material_fields": updated,
                "disabled_effect_fields": disabled,
                "effect_kinds": sorted(effect_kinds),
                "reasons": sorted(target.get("reasons", [])),
                "sources": target.get("sources", []),
            }
        )
        print(
            f"[TMP替换][材质] {source_material_path} -> {target_material_path} "
            f"(同步={len(updated)}, 屏蔽={disabled})",
            flush=True,
        )

    summary = {
        "template_json": str(template_json_path),
        "template_png": str(atlas_png_path),
        "generated_asset": str(generated_asset_path),
        "import_overlay_dir": str(cfg.import_overlay_dir),
        "material_work_dir": str(material_work_dir),
        "font_replacements": font_count,
        "texture_replacements": texture_count,
        "material_main_texture_replacements": len(material_main_textures),
        "material_replacements": material_count,
        "updated_material_parameters": material_parameter_updates,
        "disabled_material_effect_fields": material_effect_changes,
        "missing_texture_refs": len(missing_textures),
        "skipped_texture_refs": len(skipped_textures),
        "skipped_font_replacements": len(skipped_fonts),
        "skipped_material_replacements": len(skipped_materials),
        "items": replacement_records,
        "materials": material_records,
        "missing_textures": missing_textures,
        "skipped_textures": skipped_textures,
        "skipped_fonts": skipped_fonts,
        "skipped_materials": skipped_materials,
    }
    summary_path = _sdf_replacement_summary_path(cfg)
    write_json(summary_path, summary)
    print(
        f"[TMP替换] 完成: TMP字体={font_count}, 图集={texture_count}, 材质={material_count}, "
        f"材质主纹理={len(material_main_textures)}, "
        f"材质参数同步={material_parameter_updates}, 阴影描边屏蔽字段={material_effect_changes}, "
        f"缺失图集引用={len(missing_textures)}, 跳过无效图集={len(skipped_textures)}, "
        f"跳过字体={len(skipped_fonts)}, 跳过材质={len(skipped_materials)}",
        flush=True,
    )
    print(f"[TMP替换] 替换清单: {summary_path}", flush=True)
    return {
        "font_replacements": font_count,
        "texture_replacements": texture_count,
        "material_main_texture_replacements": len(material_main_textures),
        "material_replacements": material_count,
        "updated_material_parameters": material_parameter_updates,
        "disabled_material_effect_fields": material_effect_changes,
        "missing_texture_refs": len(missing_textures),
        "skipped_texture_refs": len(skipped_textures),
        "skipped_font_replacements": len(skipped_fonts),
        "skipped_material_replacements": len(skipped_materials),
    }


def _tmp_generation_settings_from_template(cfg: PipelineConfig) -> dict[str, Any]:
    if not cfg.tmp_template_json_path.is_file():
        return {}

    try:
        template = read_json(cfg.tmp_template_json_path)
    except Exception:
        return {}

    creation = template.get("m_CreationSettings", {}) if isinstance(template, dict) else {}
    face_info = template.get("m_FaceInfo", {}) if isinstance(template, dict) else {}
    settings: dict[str, Any] = {}

    point_size = creation.get("pointSize") or face_info.get("m_PointSize")
    if point_size:
        settings["point_size"] = int(round(float(point_size)))

    padding = creation.get("padding") or template.get("m_AtlasPadding")
    if padding is not None:
        settings["padding"] = int(padding)

    point_size_sampling_mode = creation.get("pointSizeSamplingMode")
    if point_size_sampling_mode is not None:
        settings["point_size_mode"] = "auto" if int(point_size_sampling_mode) == 0 else "custom"

    padding_mode = creation.get("paddingMode")
    if padding_mode is not None:
        settings["padding_mode"] = "percent" if int(padding_mode) == 1 else "pixel"

    packing_mode = creation.get("packingMode")
    if packing_mode is not None:
        settings["packing_mode"] = int(packing_mode) if int(packing_mode) != 0 else 4

    atlas_width = creation.get("atlasWidth") or template.get("m_AtlasWidth")
    if atlas_width:
        settings["atlas_width"] = int(atlas_width)

    atlas_height = creation.get("atlasHeight") or template.get("m_AtlasHeight")
    if atlas_height:
        settings["atlas_height"] = int(atlas_height)

    render_mode = creation.get("renderMode") or template.get("m_AtlasRenderMode")
    if render_mode:
        settings["render_mode"] = str(render_mode)

    return settings


def _count_visible_chars(path: Path) -> int:
    if not path.is_file():
        return 0
    return len({char for char in path.read_text(encoding="utf-8") if is_visible_char(char)})


def _atlas_size_for_char_count(char_count: int) -> int:
    if char_count <= 1200:
        return 2048
    if char_count <= 3000:
        return 4096
    return 8192


UNITY_VERSION_PART_RE = re.compile(r"^\d+(?:\.\d+){2,3}[a-z]\d+(?:c\d+)?$", re.IGNORECASE)


def _unity_version_from_exe(unity_exe: Path) -> str | None:
    for part in reversed(unity_exe.parts):
        if UNITY_VERSION_PART_RE.match(part):
            return part
    return None


def _sync_unity_project_version(cfg: PipelineConfig) -> None:
    unity_version = _unity_version_from_exe(cfg.unity_exe)
    if not unity_version:
        print(f"[TMP] 未能从 Unity 路径推导版本号，跳过 ProjectVersion 同步: {cfg.unity_exe}", flush=True)
        return

    project_version_path = cfg.unity_font_project / "ProjectSettings" / "ProjectVersion.txt"
    if not project_version_path.is_file():
        print(f"[TMP] ProjectVersion 文件不存在，跳过同步: {project_version_path}", flush=True)
        return

    text = project_version_path.read_text(encoding="utf-8-sig")
    revision_match = re.search(r"^m_EditorVersionWithRevision:\s*(\S+)(?:\s+\(([^)]+)\))?", text, re.MULTILINE)
    revision = revision_match.group(2) if revision_match and revision_match.group(1) == unity_version else None
    revision_line = f"m_EditorVersionWithRevision: {unity_version} ({revision})" if revision else f"m_EditorVersionWithRevision: {unity_version}"
    new_text = f"m_EditorVersion: {unity_version}\n{revision_line}\n"

    if text.replace("\r\n", "\n") != new_text:
        project_version_path.write_text(new_text, encoding="utf-8")
        print(f"[TMP] 已同步 Unity 项目版本: {project_version_path} -> {unity_version}", flush=True)


def launch_unity_tmp_generator(
    cfg: PipelineConfig,
    characters_file: Path | None = None,
    output_name: str | None = None,
    prepare_import: bool = False,
) -> int:
    lock_path = cfg.unity_font_project / ".translate-unity.lock"
    with interprocess_file_lock(lock_path, label="TMP排队"):
        return _launch_unity_tmp_generator_locked(
            cfg,
            characters_file=characters_file,
            output_name=output_name,
            prepare_import=prepare_import,
        )


def _launch_unity_tmp_generator_locked(
    cfg: PipelineConfig,
    characters_file: Path | None = None,
    output_name: str | None = None,
    prepare_import: bool = False,
) -> int:
    import sys

    if not cfg.unity_exe.is_file():
        raise FileNotFoundError(f"Unity executable not found: {cfg.unity_exe}")
    _sync_unity_project_version(cfg)
    launcher = cfg.unity_font_launcher
    if not launcher.is_absolute():
        launcher = cfg.unity_font_project / launcher
    if not launcher.is_file():
        raise FileNotFoundError(f"Unity launcher script not found: {launcher}")
    characters_file = characters_file or build_merged_tmp_chars(cfg)
    output_name = output_name or "generated_tmp_font.asset"
    output_asset_path = cfg.unity_font_project / "Assets" / "GeneratedFonts" / output_name
    export_dir = _sdf_generated_template_dir(cfg)
    export_dir.mkdir(parents=True, exist_ok=True)
    export_path = export_dir / output_name
    tmp_settings = _tmp_generation_settings_from_template(cfg)
    visible_char_count = _count_visible_chars(characters_file)
    fixed_padding = 9
    atlas_size = _estimate_sdf_atlas_size_for_auto_point_size(visible_char_count, fixed_padding, cfg.tmp_max_atlas_size)
    tmp_settings["point_size_mode"] = "auto"
    tmp_settings.pop("point_size", None)
    tmp_settings["padding"] = fixed_padding
    tmp_settings["atlas_width"] = atlas_size
    tmp_settings["atlas_height"] = atlas_size
    print(f"[TMP] 启动前检查通过", flush=True)
    print(f"[TMP] Unity 可执行文件: {cfg.unity_exe}", flush=True)
    print(f"[TMP] Unity 项目根目录: {cfg.unity_font_project}", flush=True)
    print(f"[TMP] Launcher 脚本: {launcher}", flush=True)
    print(f"[TMP] 字符文件: {characters_file}", flush=True)
    print(f"[TMP] 可见字符数: {visible_char_count}", flush=True)
    _log_green(
        f"[TMP] 生成参数: pointSize=Auto, atlas={atlas_size}x{atlas_size}, padding={fixed_padding} "
        f"(质量档位: <=1200 用 2048，<=3000 用 4096，更多用 8192；"
        f"配置允许上限 {cfg.tmp_max_atlas_size}，Unity 不支持时自动回退)"
    )
    print(f"[TMP] 预期输出目录: {output_asset_path.parent}", flush=True)
    print(f"[TMP] 预期输出文件: {output_asset_path}", flush=True)
    print(f"[TMP] 复制输出目录: {export_dir}", flush=True)
    print(f"[TMP] 复制输出文件: {export_path}", flush=True)
    cmd = [
        sys.executable,
        str(launcher),
        "--font",
        str(cfg.ttf_template_path),
        "--output-name",
        output_name,
        "--characters-file",
        str(characters_file),
        "--project-root",
        str(cfg.unity_font_project),
        "--unity-exe",
        str(cfg.unity_exe),
        "--log-file",
        str(cfg.log_dir / "tmp_font_unity.log"),
    ]
    if "point_size" in tmp_settings:
        cmd.extend(["--point-size", str(tmp_settings["point_size"])])
    cmd.extend(["--point-size-mode", str(tmp_settings.get("point_size_mode", "auto"))])
    if "padding" in tmp_settings:
        cmd.extend(["--padding", str(tmp_settings["padding"])])
    cmd.extend(["--padding-mode", str(tmp_settings.get("padding_mode", "pixel"))])
    cmd.extend(["--packing-mode", str(tmp_settings.get("packing_mode", 4))])
    if "atlas_width" in tmp_settings:
        cmd.extend(["--atlas-width", str(tmp_settings["atlas_width"])])
    if "atlas_height" in tmp_settings:
        cmd.extend(["--atlas-height", str(tmp_settings["atlas_height"])])
    if "render_mode" in tmp_settings:
        cmd.extend(["--render-mode", str(tmp_settings["render_mode"])])
    cmd.append("--static")
    print("[TMP] 开始执行 Unity 字体生成命令", flush=True)
    result = subprocess.run(cmd, cwd=str(cfg.unity_font_project))
    print(f"[TMP] Unity 结束，返回码: {result.returncode}", flush=True)
    if result.returncode == 0 and output_asset_path.is_file():
        shutil.copy2(output_asset_path, export_path)
        meta_path = output_asset_path.with_suffix(output_asset_path.suffix + ".meta")
        if meta_path.is_file():
            shutil.copy2(meta_path, export_path.with_suffix(export_path.suffix + ".meta"))
        print(f"[TMP] 已复制到: {export_path}", flush=True)
        template_json_path = export_generated_tmp_json_template(cfg, export_path)
        atlas_paths = export_generated_tmp_atlas_png(cfg, export_path)
        if prepare_import and atlas_paths:
            prepare_generated_tmp_import_replacements(cfg, template_json_path, atlas_paths[0])
    return result.returncode


