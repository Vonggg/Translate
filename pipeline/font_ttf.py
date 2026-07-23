from __future__ import annotations

import json
import shutil
from pathlib import Path

from support.config import PipelineConfig
from .manifest_index import load_tmp_manifest_index, tmp_manifest_index_path


FONT_SUFFIXES = {".ttf", ".otf"}


def _manifest_item_value(item: dict, key: str, default=None):
    if key in item:
        return item[key]
    camel_key = key[:1].lower() + key[1:]
    return item.get(camel_key, default)


def _manifest_font_targets(cfg: PipelineConfig) -> list[tuple[Path, Path | None]]:
    targets: list[tuple[Path, Path | None]] = []
    if not cfg.resource_input_root.is_dir():
        return targets

    indexed_items = load_tmp_manifest_index(cfg)
    if indexed_items is not None:
        print(f"[TTF] 使用脚本0生成的 manifest 索引: {tmp_manifest_index_path(cfg)}，条目: {len(indexed_items)}", flush=True)
        return _font_targets_from_manifest_items(cfg, indexed_items)

    for manifest_path in sorted(cfg.resource_input_root.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue

        items = manifest.get("Items", []) if isinstance(manifest, dict) else []
        if not isinstance(items, list):
            continue

        manifest_dir = manifest_path.parent
        for item in items:
            if not isinstance(item, dict):
                continue
            if _manifest_item_value(item, "TypeName") != "Font":
                continue
            relative_path = _manifest_item_value(item, "RelativePath")
            if not isinstance(relative_path, str) or not relative_path:
                continue

            relative = Path(relative_path)
            if relative.suffix.lower() not in FONT_SUFFIXES:
                continue

            exported_path = manifest_dir / relative
            try:
                target_relative_path = (manifest_dir / relative).relative_to(cfg.resource_input_root)
            except ValueError:
                continue
            targets.append((target_relative_path, exported_path if exported_path.is_file() else None))

    return targets


def _font_targets_from_manifest_items(cfg: PipelineConfig, items: list[tuple[Path, Path, dict]]) -> list[tuple[Path, Path | None]]:
    targets: list[tuple[Path, Path | None]] = []
    for _manifest_path, manifest_dir, item in items:
        if _manifest_item_value(item, "TypeName") != "Font":
            continue
        relative_path = _manifest_item_value(item, "RelativePath")
        if not isinstance(relative_path, str) or not relative_path:
            continue

        relative = Path(relative_path)
        if relative.suffix.lower() not in FONT_SUFFIXES:
            continue

        exported_path = manifest_dir / relative
        try:
            target_relative_path = exported_path.relative_to(cfg.resource_input_root)
        except ValueError:
            continue
        targets.append((target_relative_path, exported_path if exported_path.is_file() else None))
    return targets


def _existing_source_fonts(cfg: PipelineConfig) -> list[tuple[Path, Path]]:
    if not cfg.ttf_old_dir.is_dir():
        return []

    fonts: list[tuple[Path, Path]] = []
    for source_path in sorted(cfg.ttf_old_dir.rglob("*")):
        if not source_path.is_file() or source_path.suffix.lower() not in FONT_SUFFIXES:
            continue
        fonts.append((source_path.relative_to(cfg.ttf_old_dir), source_path))
    return fonts


def build_ttf_replacements(cfg: PipelineConfig) -> list[Path]:
    if not cfg.ttf_template_path.is_file():
        raise FileNotFoundError(f"TTF template not found: {cfg.ttf_template_path}")

    font_targets = _manifest_font_targets(cfg)
    archived_count = 0
    if font_targets:
        cfg.ttf_old_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, exported_path in font_targets:
            if exported_path is None:
                continue
            source_copy_path = cfg.ttf_old_dir / relative_path
            source_copy_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(exported_path, source_copy_path)
            archived_count += 1
        font_sources = [(relative_path, cfg.ttf_old_dir / relative_path) for relative_path, _ in font_targets]
    else:
        font_sources = _existing_source_fonts(cfg)

    if not font_sources:
        font_sources = [(Path(cfg.ttf_template_path.name), cfg.ttf_template_path)]

    if cfg.ttf_new_dir.exists():
        shutil.rmtree(cfg.ttf_new_dir)
    cfg.ttf_new_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    for relative_path, _source_path in font_sources:
        destination = cfg.ttf_new_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cfg.ttf_template_path, destination)
        outputs.append(destination)
    if font_targets:
        print(f"[TTF] 已从 manifest 读取游戏 Font 目标: {len(font_targets)} 个", flush=True)
        print(f"[TTF] 已归档实际导出的原游戏字体: {archived_count} 个 -> {cfg.ttf_old_dir}", flush=True)
        if archived_count < len(font_targets):
            print(f"[TTF] 有 {len(font_targets) - archived_count} 个 Font 只有 manifest 目标，未找到实际导出的 ttf/otf 文件。", flush=True)
    else:
        print(f"[TTF] 未在 workspace/input manifest 中找到已导出的 Font，使用已有 source/模板生成。", flush=True)
    print(f"[TTF] 已生成模板替换字体: {len(outputs)} 个 -> {cfg.ttf_new_dir}", flush=True)
    if outputs:
        print("[TTF] 输出文件:", flush=True)
        for index, output_path in enumerate(outputs[:20], start=1):
            try:
                display_path = output_path.relative_to(cfg.ttf_new_dir)
            except ValueError:
                display_path = output_path
            print(f"[TTF]   {index}. {display_path}", flush=True)
        if len(outputs) > 20:
            print(f"[TTF]   ... 其余 {len(outputs) - 20} 个省略", flush=True)
    return outputs
