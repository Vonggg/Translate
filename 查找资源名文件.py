from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from config import load_config
from image_import_utils import copy_image_for_import


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp"}


def normalize_name(raw: str) -> str:
    return raw.strip()


def name_matches(value: str, query: str, exact: bool) -> bool:
    value_folded = value.casefold()
    query_folded = query.casefold()
    if exact:
        return value_folded == query_folded
    return query_folded in value_folded


def iter_manifest_matches(input_root: Path, query_name: str, exact: bool):
    manifest_paths = sorted(input_root.rglob("manifest.json"))
    print(f"[查找资源名] manifest 数: {len(manifest_paths)}", flush=True)
    for index, manifest_path in enumerate(manifest_paths, start=1):
        if index == 1 or index % 20 == 0 or index == len(manifest_paths):
            print(f"[查找资源名] manifest 进度: {index}/{len(manifest_paths)}", flush=True)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue

        items = manifest.get("Items", [])
        if not isinstance(items, list):
            continue

        for item in items:
            if not isinstance(item, dict):
                continue
            asset_name = item.get("AssetName")
            if not isinstance(asset_name, str) or not name_matches(asset_name, query_name, exact):
                continue

            relative_path = item.get("RelativePath")
            if not isinstance(relative_path, str) or not relative_path:
                yield manifest_path
                continue
            yield manifest_path.parent / relative_path


def iter_loose_filename_matches(input_root: Path, query_name: str, exact: bool):
    paths = sorted(input_root.rglob("*"))
    print(f"[查找资源名] 文件名宽松匹配扫描条目: {len(paths)}", flush=True)
    matched = 0
    for index, path in enumerate(paths, start=1):
        if index == 1 or index % 2000 == 0 or index == len(paths):
            print(f"[查找资源名] 文件名扫描进度: {index}/{len(paths)}，当前命中: {matched}", flush=True)
        if not path.is_file() or path.name == "manifest.json":
            continue
        stem = path.stem
        if name_matches(stem, query_name, exact):
            matched += 1
            yield path


def unique_paths(paths):
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield path


def filter_image_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.suffix.lower() in IMAGE_SUFFIXES and path.is_file()]


def parse_selection(raw: str, count: int) -> list[int]:
    text = raw.strip().lower()
    if not text:
        return []
    if text in {"all", "a", "*"}:
        return list(range(count))

    selected: set[int] = set()
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start - 1, end))
        else:
            selected.add(int(part) - 1)

    return sorted(index for index in selected if 0 <= index < count)


def copy_selected_images(image_paths: list[Path], selected_indexes: list[int], input_root: Path, output_root: Path) -> int:
    copied = 0
    for index in selected_indexes:
        source_path = image_paths[index]
        try:
            relative_path = source_path.relative_to(input_root)
        except ValueError:
            relative_path = source_path.name
        target_path = output_root / relative_path
        copy_image_for_import(source_path, target_path)
        copied += 1
        print(f"[COPY] {source_path} -> {target_path}")
    return copied


def prompt_copy_images(image_paths: list[Path], input_root: Path, output_root: Path) -> int:
    if not image_paths:
        print("未找到可复制的图片文件。")
        return 0

    print()
    print("命中的图片文件:")
    for index, path in enumerate(image_paths, start=1):
        try:
            display_path = path.relative_to(input_root)
        except ValueError:
            display_path = path
        print(f"  {index}. {display_path}")

    print()
    print("输入编号复制图片，可用逗号或范围，例如 1,3,5 或 2-6；输入 all 复制全部；直接回车跳过。")
    raw = input("请选择要复制的图片编号: ")
    try:
        selected_indexes = parse_selection(raw, len(image_paths))
    except ValueError:
        print("编号格式无效，已取消复制。")
        return 1

    if not selected_indexes:
        print("未选择图片，跳过复制。")
        return 0

    copied = copy_selected_images(image_paths, selected_indexes, input_root, output_root)
    print(f"完成，已复制 {copied} 个图片到: {output_root}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 workspace/input 中查找指定资源名对应的导出文件路径。")
    parser.add_argument("asset_name", nargs="?", help="要查找的资源名；默认不区分大小写并支持包含匹配。")
    parser.add_argument(
        "--input-root",
        default=None,
        help="要扫描的 input 目录；默认读取 config.json 里的 resource_input_root。",
    )
    parser.add_argument(
        "--manifest-only",
        action="store_true",
        help="只按 manifest.json 的 AssetName 查找，不做文件名宽松匹配。",
    )
    parser.add_argument(
        "--exact",
        action="store_true",
        help="资源名必须完全相同；默认使用包含匹配。",
    )
    parser.add_argument(
        "--no-copy-prompt",
        action="store_true",
        help="只输出路径，不提示复制图片。",
    )
    parser.add_argument(
        "--image-output-root",
        default=None,
        help="图片复制目标目录；默认读取 config.json 里的 image_import_dir。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_asset_name = args.asset_name or input("请输入资源名: ").strip()
    asset_name = normalize_name(raw_asset_name)
    if not asset_name:
        print("资源名不能为空。")
        return 1

    cfg = load_config()
    input_root = Path(args.input_root).resolve() if args.input_root else cfg.resource_input_root
    image_output_root = Path(args.image_output_root).resolve() if args.image_output_root else cfg.image_import_dir
    if not input_root.is_dir():
        print(f"input 目录不存在: {input_root}")
        return 1

    match_mode = "精确" if args.exact else "包含"
    print(f"[查找资源名] input 目录: {input_root}", flush=True)
    print(f"[查找资源名] 查询资源名: {asset_name}，模式={match_mode}", flush=True)
    matches = list(iter_manifest_matches(input_root, asset_name, args.exact))
    if not args.manifest_only:
        matches.extend(iter_loose_filename_matches(input_root, asset_name, args.exact))

    paths = list(unique_paths(matches))
    for path in paths:
        print(path)

    print(f"完成，资源名={asset_name}，模式={match_mode}，命中 {len(paths)} 个路径。")
    if paths and not args.no_copy_prompt:
        prompt_copy_images(filter_image_paths(paths), input_root, image_output_root)
    return 0 if paths else 2


if __name__ == "__main__":
    raise SystemExit(main())
