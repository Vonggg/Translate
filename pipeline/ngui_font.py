from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

from support.config import PipelineConfig
from .bitmap_font_detection import NGUI_BITMAP_FONT_TYPE, REPORT_FILENAME
from .manifest_index import load_tmp_manifest_index
from .shared import atomic_write_json, read_json


GENERATION_SCHEMA_VERSION = 2
GENERATION_MANIFEST_NAME = "ngui_font_generation.json"
REPLACEMENT_REPORT_NAME = "ngui_font_replacements.json"
REMOVED_UNSUPPORTED_CHARS_NAME = "ngui_source_chars_removed_unsupported_by_ttf.txt"
REMOVED_UNSUPPORTED_DETAILS_NAME = "ngui_source_chars_removed_unsupported_by_ttf.tsv"
RESOURCE_TYPE_DIRS = {"monobehaviour", "textasset", "texture2d", "material", "font"}


@dataclass
class _FontJob:
    source_path: Path
    source_relative: Path
    source_data: dict[str, Any]
    font_node: dict[str, Any]
    atlas_path: Path | None
    atlas_data: dict[str, Any]
    texture_path: Path
    sprite_name: str
    sprite_data: dict[str, Any]
    page_name: str
    point_size: int
    existing_codepoints: set[int]
    generated_glyphs: list[tuple[int, dict[str, Any], Image.Image]]


@dataclass
class _ShelfZone:
    x: int
    y: int
    width: int
    height: int
    cursor_x: int = 0
    cursor_y: int = 0
    row_height: int = 0

    def place(self, width: int, height: int) -> tuple[int, int] | None:
        if width > self.width or height > self.height:
            return None
        if self.cursor_x + width > self.width:
            self.cursor_x = 0
            self.cursor_y += self.row_height
            self.row_height = 0
        if self.cursor_y + height > self.height:
            return None
        placed = (self.x + self.cursor_x, self.y + self.cursor_y)
        self.cursor_x += width
        self.row_height = max(self.row_height, height)
        return placed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    with tempfile.NamedTemporaryFile(delete=False, dir=str(path.parent), suffix=".png") as handle:
        temp_path = Path(handle.name)
    try:
        image.save(temp_path, format="PNG", optimize=True)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _normalize_asset_key(value: str | Path) -> str:
    return str(value).replace("/", "\\").strip("\\").lower()


def _resource_asset_key(cfg: PipelineConfig, path: Path) -> str:
    relative = path.relative_to(cfg.resource_input_root)
    parts = list(relative.parts)
    for index, part in enumerate(parts):
        if part.lower() in RESOURCE_TYPE_DIRS:
            return str(Path(*parts[:index]))
    return str(relative.parent)


def _manifest_item_value(item: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in item:
        return item[key]
    return item.get(key[:1].lower() + key[1:], default)


def _resolve_manifest_item_path(manifest_dir: Path, item: dict[str, Any]) -> Path | None:
    relative = str(_manifest_item_value(item, "RelativePath", "") or "")
    if not relative:
        return None
    relative_path = Path(*relative.replace("\\", "/").split("/"))
    candidates = [manifest_dir / relative_path]
    bundle_entry_name = str(_manifest_item_value(item, "BundleEntryName", "") or "")
    if bundle_entry_name and not relative.replace("\\", "/").startswith("bundle/"):
        sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", bundle_entry_name).strip()
        candidates.append(manifest_dir / "bundle" / sanitized / relative_path)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


class _ResourceResolver:
    def __init__(self, cfg: PipelineConfig):
        self.cfg = cfg
        self.items: dict[tuple[str, int], Path] = {}
        indexed = load_tmp_manifest_index(cfg)
        if indexed is None:
            raise FileNotFoundError("未找到 tmp_manifest_index.json，请先执行脚本 0。")
        for _manifest_path, manifest_dir, item in indexed:
            try:
                path_id = int(_manifest_item_value(item, "PathId", 0) or 0)
            except (TypeError, ValueError):
                continue
            path = _resolve_manifest_item_path(manifest_dir, item)
            if path is None:
                continue
            try:
                asset_key = _resource_asset_key(cfg, path)
            except ValueError:
                continue
            self.items[(_normalize_asset_key(asset_key), path_id)] = path

        file_id_path = cfg.stage_record_dir / cfg.output_file_id_map_json
        raw_file_ids = read_json(file_id_path) if file_id_path.is_file() else {}
        self.file_ids: dict[str, dict[str, str]] = {}
        if isinstance(raw_file_ids, dict):
            for asset_key, entry in raw_file_ids.items():
                if not isinstance(asset_key, str) or not isinstance(entry, dict):
                    continue
                bucket = entry.get("file_ids", entry)
                if isinstance(bucket, dict):
                    self.file_ids[_normalize_asset_key(asset_key)] = {
                        str(file_id): str(target)
                        for file_id, target in bucket.items()
                        if isinstance(target, str)
                    }

    def resolve(self, source_path: Path, reference: Any, description: str) -> Path:
        if not isinstance(reference, dict):
            raise ValueError(f"{description} 不是有效的 PPtr: {source_path}")
        try:
            file_id = int(reference.get("m_FileID", 0) or 0)
            path_id = int(reference.get("m_PathID", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{description} 的 FileID/PathID 无效: {source_path}") from exc
        if path_id <= 0:
            raise ValueError(f"{description} 的 PathID 为空: {source_path}")

        source_asset = _resource_asset_key(self.cfg, source_path)
        source_key = _normalize_asset_key(source_asset)
        if file_id == 0:
            target_key = source_key
        else:
            target_asset = self.file_ids.get(source_key, {}).get(str(file_id))
            if not target_asset:
                raise KeyError(f"无法解析 {description} FileID={file_id}: {source_path}")
            target_key = _normalize_asset_key(target_asset)
        target = self.items.get((target_key, path_id))
        if target is None:
            raise KeyError(f"manifest 索引中找不到 {description}: asset={target_key}, PathID={path_id}")
        return target


def _translated_characters(cfg: PipelineConfig) -> list[str]:
    """Read the final shared TMP/NGUI character set from script 8."""
    combined_chars_path = cfg.stage_record_dir / getattr(
        cfg,
        "output_tmp_chars_txt",
        "tmp_chars.txt",
    )
    if not combined_chars_path.is_file():
        raise FileNotFoundError(
            f"未找到 TMP/NGUI 共用字符文件 tmp_chars.txt，请先执行脚本 8: "
            f"{combined_chars_path}"
        )
    payload: Any = combined_chars_path.read_text(encoding="utf-8")
    strings: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, list):
            for child in value:
                collect(child)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)

    if isinstance(payload, str):
        collect(payload)
    else:
        raise ValueError(f"翻译字符数据结构无效: {combined_chars_path}")

    unique: dict[str, None] = {}
    for text in strings:
        for char in text:
            if char in {"\r", "\n", "\t"} or unicodedata.category(char).startswith("C"):
                continue
            unique.setdefault(char, None)
    return list(unique)


def _font_supported_codepoints(font_path: Path) -> set[int]:
    font = TTFont(str(font_path), lazy=True)
    try:
        cmap = font.getBestCmap() or {}
        return {int(codepoint) for codepoint in cmap}
    finally:
        font.close()


def _display_codepoint(codepoint: int) -> str:
    char = chr(codepoint)
    return char if char.isprintable() and not unicodedata.category(char).startswith("C") else ""


def _write_removed_source_chars_report(
    cfg: PipelineConfig,
    jobs: list[_FontJob],
    removed_codepoints: set[int],
) -> tuple[Path, Path] | None:
    text_path = cfg.stage_record_dir / REMOVED_UNSUPPORTED_CHARS_NAME
    detail_path = cfg.stage_record_dir / REMOVED_UNSUPPORTED_DETAILS_NAME
    if not removed_codepoints:
        for stale_path in (text_path, detail_path):
            if stale_path.exists():
                stale_path.unlink()
        return None

    rows = ["code\tchar\tunicode_name\tsource_fonts"]
    display_chars: list[str] = []
    for codepoint in sorted(removed_codepoints):
        display_char = _display_codepoint(codepoint)
        if display_char:
            display_chars.append(display_char)
        source_fonts = sorted(
            job.source_relative.as_posix()
            for job in jobs
            if codepoint in job.existing_codepoints
        )
        rows.append(
            "\t".join(
                (
                    f"U+{codepoint:04X}",
                    display_char,
                    unicodedata.name(chr(codepoint), "UNKNOWN"),
                    " | ".join(source_fonts),
                )
            )
        )
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("".join(display_chars), encoding="utf-8")
    detail_path.write_text("\n".join(rows), encoding="utf-8")
    return text_path, detail_path


def _find_main_texture_reference(material: dict[str, Any]) -> dict[str, Any] | None:
    entries = (
        material.get("m_SavedProperties", {})
        .get("m_TexEnvs", {})
        .get("Array", [])
    )
    if not isinstance(entries, list):
        return None
    fallback: dict[str, Any] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        second = entry.get("second")
        texture = second.get("m_Texture") if isinstance(second, dict) else None
        if not isinstance(texture, dict):
            continue
        fallback = fallback or texture
        if entry.get("first") == "_MainTex":
            return texture
    return fallback


def _atlas_sprites(atlas: dict[str, Any]) -> list[dict[str, Any]]:
    sprites = atlas.get("mSprites", {}).get("Array", [])
    if not isinstance(sprites, list):
        raise ValueError("NGUI UIAtlas 缺少 mSprites.Array")
    return [item for item in sprites if isinstance(item, dict)]


def _find_sprite(atlas: dict[str, Any], name: str) -> dict[str, Any]:
    for sprite in _atlas_sprites(atlas):
        if str(sprite.get("name", "")) == name:
            return sprite
    raise KeyError(f"NGUI UIAtlas 中找不到字体 Sprite: {name}")


def _font_node(data: dict[str, Any], field_path: str) -> dict[str, Any]:
    node: Any = data
    for part in field_path.split("."):
        if not part or "[" in part or not isinstance(node, dict):
            raise ValueError(f"暂不支持的 NGUI 字段路径: {field_path}")
        node = node.get(part)
    if not isinstance(node, dict):
        raise ValueError(f"NGUI 字体字段不是对象: {field_path}")
    return node


def _existing_glyphs(font_node: dict[str, Any]) -> list[dict[str, Any]]:
    saved = font_node.get("mSaved")
    array = saved.get("Array") if isinstance(saved, dict) else None
    if not isinstance(array, list):
        raise ValueError("NGUI 字体缺少 mFont.mSaved.Array")
    return [glyph for glyph in array if isinstance(glyph, dict)]


def _page_name(source_path: Path, sprite_name: str) -> str:
    path_id_match = re.search(r"_(-?\d+)\.json$", source_path.name, re.IGNORECASE)
    suffix = path_id_match.group(1) if path_id_match else hashlib.sha1(str(source_path).encode()).hexdigest()[:8]
    safe_name = re.sub(r"[^0-9A-Za-z_-]+", "_", sprite_name).strip("_") or "Font"
    return f"__CodexNGUI_{safe_name}_{suffix}"


def _render_glyph(
    pil_font: ImageFont.FreeTypeFont,
    char: str,
    point_size: int,
    stroke_width: int,
) -> tuple[dict[str, Any], Image.Image]:
    ascent, _descent = pil_font.getmetrics()
    bbox = pil_font.getbbox(char, anchor="ls", stroke_width=stroke_width)
    advance = max(0, int(math.ceil(float(pil_font.getlength(char)) + stroke_width * 2)))
    if bbox is None:
        bbox = (0, 0, 0, 0)
    left, top, right, bottom = (int(value) for value in bbox)
    width = max(0, right - left)
    height = max(0, bottom - top)
    glyph = {
        "index": ord(char),
        "x": 0,
        "y": 0,
        "width": width,
        "height": height,
        "offsetX": left,
        "offsetY": int(ascent + top),
        "advance": advance,
        "channel": 15,
        "kerning": {"Array": []},
    }
    if width == 0 or height == 0:
        return glyph, Image.new("RGBA", (0, 0))

    alpha = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(alpha)
    draw.text(
        (-left, -top),
        char,
        font=pil_font,
        anchor="ls",
        fill=255,
        stroke_width=stroke_width,
        stroke_fill=255,
    )
    rgba = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    rgba.putalpha(alpha)
    return glyph, rgba


def _next_power_of_two(value: int) -> int:
    result = 1
    while result < value:
        result <<= 1
    return result


def _pack_images(
    original_size: tuple[int, int],
    glyph_images: list[tuple[_FontJob, int, dict[str, Any], Image.Image]],
    padding: int,
    max_size: int,
) -> tuple[int, dict[tuple[int, int], tuple[int, int]]]:
    original_width, original_height = original_size
    start_size = _next_power_of_two(max(original_width, original_height)) * 2
    if start_size > max_size:
        raise ValueError(
            f"NGUI 原图 {original_width}x{original_height} 扩展后至少需要 {start_size}，"
            f"超过 ngui_max_atlas_size={max_size}"
        )

    sorted_images = sorted(
        glyph_images,
        key=lambda item: (-item[3].height, -item[3].width, str(item[0].source_relative), item[1]),
    )
    size = start_size
    while size <= max_size:
        zones = [
            _ShelfZone(original_width, 0, size - original_width, original_height),
            _ShelfZone(0, original_height, size, size - original_height),
        ]
        placements: dict[tuple[int, int], tuple[int, int]] = {}
        success = True
        for job, codepoint, _glyph, image in sorted_images:
            outer_width = image.width + padding * 2
            outer_height = image.height + padding * 2
            position = None
            for zone in zones:
                position = zone.place(outer_width, outer_height)
                if position is not None:
                    break
            if position is None:
                success = False
                break
            placements[(job.point_size, codepoint)] = (position[0] + padding, position[1] + padding)
        if success:
            return size, placements
        size *= 2
    raise ValueError(
        f"NGUI 新增字形无法放入最大 {max_size}x{max_size} 图集；"
        "请提高 ngui_max_atlas_size、减少字符或降低字体字号。"
    )


def _copy_full_atlas_sprite(template: dict[str, Any], name: str, size: int) -> dict[str, Any]:
    result = dict(template)
    result.update({"name": name, "x": 0, "y": 0, "width": size, "height": size})
    for field in (
        "borderLeft", "borderRight", "borderTop", "borderBottom",
        "paddingLeft", "paddingRight", "paddingTop", "paddingBottom",
    ):
        result[field] = 0
    return result


def _load_font_jobs(cfg: PipelineConfig, resolver: _ResourceResolver) -> list[_FontJob]:
    report_path = cfg.stage_record_dir / REPORT_FILENAME
    if not report_path.is_file():
        raise FileNotFoundError(f"未找到位图字体检测报告，请先执行脚本 0: {report_path}")
    report = read_json(report_path)
    detections = report.get("detections") if isinstance(report, dict) else None
    if not isinstance(detections, list):
        raise ValueError(f"位图字体检测报告结构无效: {report_path}")

    jobs: list[_FontJob] = []
    atlas_cache: dict[Path, dict[str, Any]] = {}
    for detection in detections:
        if not isinstance(detection, dict):
            continue
        if detection.get("type") != NGUI_BITMAP_FONT_TYPE or detection.get("confidence") != "confirmed":
            continue
        relative_text = detection.get("source_file")
        field_path = str(detection.get("field_path", "mFont") or "mFont")
        if not isinstance(relative_text, str) or not relative_text:
            continue
        source_relative = Path(*relative_text.replace("\\", "/").split("/"))
        source_path = cfg.resource_input_root / source_relative
        source_data = read_json(source_path)
        if not isinstance(source_data, dict):
            raise ValueError(f"NGUI 字体 JSON 结构无效: {source_path}")
        font_node = _font_node(source_data, field_path)
        if any(isinstance(source_data.get(name), dict) and source_data[name].get("m_PathID", 0)
               for name in ("mDynamicFont", "mReplacement")):
            print(f"[NGUI] 跳过动态/替代 UIFont 的遗留位图表: {source_path}")
            continue
        sprite_name = str(font_node.get("mSpriteName", "") or "")
        has_atlas = bool((source_data.get("mAtlas") or {}).get("m_PathID", 0))
        if not sprite_name and has_atlas:
            raise ValueError(f"NGUI 字体缺少 mSpriteName: {source_path}")

        atlas_path = resolver.resolve(source_path, source_data.get("mAtlas"), "NGUI UIAtlas") if has_atlas else None
        atlas_data = atlas_cache.get(atlas_path)
        if atlas_data is None and atlas_path is not None:
            loaded_atlas = read_json(atlas_path)
            if not isinstance(loaded_atlas, dict):
                raise ValueError(f"NGUI UIAtlas JSON 结构无效: {atlas_path}")
            atlas_data = loaded_atlas
            atlas_cache[atlas_path] = atlas_data
        atlas_data = atlas_data or {}
        sprite_data = _find_sprite(atlas_data, sprite_name) if has_atlas else {}
        # AtlasMaker may trim transparent rows/columns from the source font
        # sprite and describe the removed area with padding*. That metadata is
        # only needed while the UIFont addresses the old, trimmed sprite. This
        # pipeline regenerates every glyph, points the UIFont at a new full-
        # atlas sprite, expands mUVRect to the whole texture, and explicitly
        # zeros the new sprite's padding. Therefore the source trimming does
        # not participate in any generated glyph coordinate calculation.

        material_ref = atlas_data.get("material", atlas_data.get("mMaterial")) if has_atlas else source_data.get("mMat")
        material_path = resolver.resolve(atlas_path or source_path, material_ref, "NGUI Font Material")
        material_data = read_json(material_path)
        if not isinstance(material_data, dict):
            raise ValueError(f"NGUI Material JSON 结构无效: {material_path}")
        texture_ref = _find_main_texture_reference(material_data)
        texture_path = resolver.resolve(material_path, texture_ref, "NGUI Atlas Texture2D")
        if texture_path.suffix.lower() != ".png" or not texture_path.is_file():
            raise FileNotFoundError(f"NGUI Atlas Texture2D PNG 不存在: {texture_path}")

        existing = _existing_glyphs(font_node)
        existing_codepoints = {
            int(glyph.get("index")) for glyph in existing
            if isinstance(glyph.get("index"), int)
        }
        point_size = int(font_node.get("mSize", 0) or 0)
        if point_size <= 0:
            raise ValueError(f"NGUI 字体字号无效: {source_path}")
        jobs.append(
            _FontJob(
                source_path=source_path,
                source_relative=source_relative,
                source_data=source_data,
                font_node=font_node,
                atlas_path=atlas_path,
                atlas_data=atlas_data,
                texture_path=texture_path,
                sprite_name=sprite_name,
                sprite_data=sprite_data,
                page_name=_page_name(source_path, sprite_name),
                point_size=point_size,
                existing_codepoints=existing_codepoints,
                generated_glyphs=[],
            )
        )
    return jobs


def _artifact_entry(cfg: PipelineConfig, source: Path, generated: Path, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "source_relative": source.relative_to(cfg.resource_input_root).as_posix(),
        "generated_relative": generated.relative_to(cfg.ngui_generated_dir).as_posix(),
        "source_sha256": _sha256(source),
        "generated_sha256": _sha256(generated),
    }


def generate_ngui_fonts(cfg: PipelineConfig) -> dict[str, Any]:
    """Generate expanded NGUI bitmap font atlases and modified JSON artifacts for step 9."""
    if not cfg.ttf_template_path.is_file():
        raise FileNotFoundError(f"NGUI 字形来源 TTF 不存在: {cfg.ttf_template_path}")
    if cfg.ngui_generated_dir.exists():
        shutil.rmtree(cfg.ngui_generated_dir)
    cfg.ngui_generated_dir.mkdir(parents=True, exist_ok=True)

    resolver = _ResourceResolver(cfg)
    jobs = _load_font_jobs(cfg, resolver)
    if not jobs:
        manifest = {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "status": "no_ngui_fonts",
            "font_count": 0,
            "artifacts": [],
        }
        atomic_write_json(cfg.ngui_generated_dir / GENERATION_MANIFEST_NAME, manifest)
        print("[NGUI生成] 未发现需要处理的已确认 NGUI 位图字体，跳过。", flush=True)
        return manifest

    translated_chars = _translated_characters(cfg)
    translated_codepoints = {ord(char) for char in translated_chars}
    supported_codepoints = _font_supported_codepoints(cfg.ttf_template_path)

    missing_translated = sorted(translated_codepoints - supported_codepoints)
    if missing_translated:
        preview = "".join(_display_codepoint(codepoint) or f"[U+{codepoint:04X}]" for codepoint in missing_translated[:30])
        raise ValueError(
            f"NGUI 字形来源 TTF 缺少 {len(missing_translated)} 个译文字符: {preview}；"
            "请先按 SDF 缺字流程修改静态/动态词库，或更换模板 TTF。"
        )

    source_codepoints: set[int] = set()
    for job in jobs:
        source_codepoints.update(job.existing_codepoints)
    removed_source_codepoints = (source_codepoints - supported_codepoints) - translated_codepoints
    removed_report = _write_removed_source_chars_report(cfg, jobs, removed_source_codepoints)
    if removed_report is not None:
        text_path, detail_path = removed_report
        print(
            f"[NGUI生成][提示] 原 NGUI 字体有 {len(removed_source_codepoints)} 个字符不被模板 TTF 支持，"
            "已从统一重生成字符集中删除；不会混用旧字形。",
            flush=True,
        )
        print(f"[NGUI生成] 删除字符清单: {text_path}", flush=True)
        print(f"[NGUI生成] 删除字符详情: {detail_path}", flush=True)

    groups: dict[Path, list[_FontJob]] = {}
    for job in jobs:
        groups.setdefault(job.texture_path, []).append(job)

    pil_fonts: dict[int, ImageFont.FreeTypeFont] = {}
    rendered_glyphs: dict[tuple[int, int], tuple[dict[str, Any], Image.Image]] = {}
    for texture_path, group_jobs in groups.items():
        group_codepoints = set(translated_codepoints)
        for job in group_jobs:
            group_codepoints.update(job.existing_codepoints)
        group_codepoints.intersection_update(supported_codepoints)

        for job in group_jobs:
            pil_font = pil_fonts.get(job.point_size)
            if pil_font is None:
                pil_font = ImageFont.truetype(str(cfg.ttf_template_path), job.point_size)
                pil_fonts[job.point_size] = pil_font
            for codepoint in sorted(group_codepoints):
                cache_key = (job.point_size, codepoint)
                rendered = rendered_glyphs.get(cache_key)
                if rendered is None:
                    rendered = _render_glyph(pil_font, chr(codepoint), job.point_size, 0)
                    rendered_glyphs[cache_key] = rendered
                glyph, image = rendered
                job.generated_glyphs.append((codepoint, dict(glyph), image))

    artifacts: list[dict[str, Any]] = []
    group_reports: list[dict[str, Any]] = []
    generated_atlases: set[Path] = set()
    for texture_path, group_jobs in groups.items():
        with Image.open(texture_path) as opened:
            original = opened.convert("RGBA")
        original_size = original.size
        unique_images: dict[tuple[int, int], tuple[_FontJob, int, dict[str, Any], Image.Image]] = {}
        for job in group_jobs:
            for codepoint, glyph, image in job.generated_glyphs:
                if image.width <= 0 or image.height <= 0:
                    continue
                unique_images.setdefault((job.point_size, codepoint), (job, codepoint, glyph, image))
        all_images = list(unique_images.values())
        final_size, placements = _pack_images(
            original_size,
            all_images,
            cfg.ngui_glyph_padding,
            cfg.ngui_max_atlas_size,
        )
        expanded = Image.new("RGBA", (final_size, final_size), (0, 0, 0, 0))
        expanded.paste(original, (0, 0))

        atlas_sources: dict[Path, dict[str, Any]] = {}
        pasted_glyphs: set[tuple[int, int]] = set()
        for job in group_jobs:
            if job.atlas_path is not None:
                atlas_sources[job.atlas_path] = job.atlas_data
            regenerated_glyphs: list[dict[str, Any]] = []
            for codepoint, glyph, image in job.generated_glyphs:
                if image.width > 0 and image.height > 0:
                    x, y = placements[(job.point_size, codepoint)]
                    glyph["x"] = x
                    glyph["y"] = y
                    glyph_key = (job.point_size, codepoint)
                    if glyph_key not in pasted_glyphs:
                        expanded.alpha_composite(image, (x, y))
                        pasted_glyphs.add(glyph_key)
                regenerated_glyphs.append(glyph)
            regenerated_glyphs.sort(key=lambda glyph: int(glyph.get("index", 0) or 0))
            job.font_node["mSaved"]["Array"] = regenerated_glyphs

            job.font_node["mWidth"] = final_size
            job.font_node["mHeight"] = final_size
            if job.atlas_path is not None:
                job.font_node["mSpriteName"] = job.page_name
            job.source_data["mUVRect"] = {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}

            if job.atlas_path is None:
                continue  # Direct material font uses mUVRect, not a UIAtlas sprite.
            sprites = job.atlas_data.get("mSprites", {}).get("Array")
            if not isinstance(sprites, list):
                raise ValueError(f"NGUI UIAtlas 缺少 mSprites.Array: {job.atlas_path}")
            sprites[:] = [
                sprite for sprite in sprites
                if not isinstance(sprite, dict) or sprite.get("name") != job.page_name
            ]
            sprites.append(_copy_full_atlas_sprite(job.sprite_data, job.page_name, final_size))

        if expanded.crop((0, 0, original_size[0], original_size[1])).tobytes() != original.tobytes():
            raise RuntimeError(f"NGUI 图集原始区域像素校验失败: {texture_path}")

        generated_texture = cfg.ngui_generated_dir / texture_path.relative_to(cfg.resource_input_root)
        _atomic_save_png(expanded, generated_texture)
        artifacts.append(_artifact_entry(cfg, texture_path, generated_texture, "texture"))

        for atlas_path, atlas_data in atlas_sources.items():
            if atlas_path in generated_atlases:
                continue
            generated_atlas = cfg.ngui_generated_dir / atlas_path.relative_to(cfg.resource_input_root)
            atomic_write_json(generated_atlas, atlas_data)
            artifacts.append(_artifact_entry(cfg, atlas_path, generated_atlas, "atlas"))
            generated_atlases.add(atlas_path)

        for job in group_jobs:
            generated_font = cfg.ngui_generated_dir / job.source_relative
            atomic_write_json(generated_font, job.source_data)
            artifacts.append(_artifact_entry(cfg, job.source_path, generated_font, "font"))

        group_reports.append(
            {
                "texture": texture_path.relative_to(cfg.resource_input_root).as_posix(),
                "original_size": list(original_size),
                "generated_size": [final_size, final_size],
                "forbidden_rect": [0, 0, original_size[0], original_size[1]],
                "generated_glyph_bitmap_count": len(all_images),
                "fonts": [
                    {
                        "source": job.source_relative.as_posix(),
                        "name": job.source_data.get("m_Name", job.source_path.stem),
                        "point_size": job.point_size,
                        "source_glyph_count": len(job.existing_codepoints),
                        "generated_glyph_count": len(job.generated_glyphs),
                        "page_name": job.page_name,
                    }
                    for job in group_jobs
                ],
            }
        )

    manifest = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "status": "generated",
        "template_ttf": str(cfg.ttf_template_path),
        "template_ttf_sha256": _sha256(cfg.ttf_template_path),
        "translated_character_count": len(translated_chars),
        "removed_unsupported_source_character_count": len(removed_source_codepoints),
        "removed_unsupported_source_codepoints": sorted(removed_source_codepoints),
        "generation_mode": "regenerate_all_source_and_translated_glyphs",
        "font_count": len(jobs),
        "groups": group_reports,
        "artifacts": artifacts,
    }
    atomic_write_json(cfg.ngui_generated_dir / GENERATION_MANIFEST_NAME, manifest)
    print(
        f"[NGUI生成] 完成：字体={len(jobs)}，图集={len(groups)}，"
        f"译文字符={len(translated_chars)}，输出={cfg.ngui_generated_dir}",
        flush=True,
    )
    for group in group_reports:
        print(
            f"[NGUI生成] 图集 {group['texture']}: "
            f"{group['original_size'][0]}x{group['original_size'][1]} -> "
            f"{group['generated_size'][0]}x{group['generated_size'][1]}，"
            f"统一重生成字形位图={group['generated_glyph_bitmap_count']}",
            flush=True,
        )
    return manifest


def prepare_generated_ngui_import_replacements(cfg: PipelineConfig) -> dict[str, int]:
    """Validate step-9 NGUI artifacts and copy them into the step-10 import tree."""
    manifest_path = cfg.ngui_generated_dir / GENERATION_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"未找到脚本 9 的 NGUI 生成清单: {manifest_path}")
    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != GENERATION_SCHEMA_VERSION:
        raise ValueError(f"NGUI 生成清单版本无效: {manifest_path}")

    if cfg.ngui_import_dir.exists():
        shutil.rmtree(cfg.ngui_import_dir)
    cfg.ngui_import_dir.mkdir(parents=True, exist_ok=True)

    counts = {"font": 0, "atlas": 0, "texture": 0}
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise ValueError(f"NGUI 生成清单 artifacts 无效: {manifest_path}")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        source_relative = artifact.get("source_relative")
        generated_relative = artifact.get("generated_relative")
        kind = str(artifact.get("kind", ""))
        if not isinstance(source_relative, str) or not isinstance(generated_relative, str):
            raise ValueError(f"NGUI 生成清单包含无效路径: {artifact}")
        source = cfg.resource_input_root / Path(*source_relative.split("/"))
        generated = cfg.ngui_generated_dir / Path(*generated_relative.split("/"))
        if not source.is_file() or _sha256(source) != artifact.get("source_sha256"):
            raise RuntimeError(f"NGUI 源资源已变化，请重新执行脚本 9: {source}")
        if not generated.is_file() or _sha256(generated) != artifact.get("generated_sha256"):
            raise RuntimeError(f"NGUI 生成产物缺失或已变化，请重新执行脚本 9: {generated}")
        destination = cfg.ngui_import_dir / Path(*source_relative.split("/"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(generated, destination)
        counts[kind] = counts.get(kind, 0) + 1

    report_path = cfg.ngui_import_dir.parent / REPLACEMENT_REPORT_NAME
    atomic_write_json(
        report_path,
        {
            "schema_version": GENERATION_SCHEMA_VERSION,
            "source_generation_manifest": str(manifest_path),
            "import_root": str(cfg.ngui_import_dir),
            "counts": counts,
        },
    )
    print(
        f"[NGUI替换] 已准备待导入文件：UIFont={counts.get('font', 0)}，"
        f"UIAtlas={counts.get('atlas', 0)}，Texture2D={counts.get('texture', 0)} "
        f"-> {cfg.ngui_import_dir}",
        flush=True,
    )
    return counts
