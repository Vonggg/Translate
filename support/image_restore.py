from __future__ import annotations

import json
from pathlib import Path

from support.image_import_utils import copy_image_for_import


def load_allpng_map(map_path: Path) -> list[dict[str, str]]:
    if not map_path.is_file():
        raise FileNotFoundError(f"映射文件不存在: {map_path}")
    data = json.loads(map_path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"映射文件格式无效: {map_path}")
    items = data.get("items", [])
    if not isinstance(items, list):
        raise ValueError(f"映射文件格式无效: {map_path}")
    return [item for item in items if isinstance(item, dict)]


def restore_images_to_import(edited_root: Path, to_import_root: Path, map_path: Path) -> int:
    restored = 0
    missing = 0
    items = load_allpng_map(map_path)
    print(f"[图片] 映射条目数: {len(items)}")
    for index, item in enumerate(items, start=1):
        if index == 1 or index % 500 == 0 or index == len(items):
            print(f"[图片] 恢复进度: {index}/{len(items)}，已恢复: {restored}，缺失: {missing}", flush=True)
        flat_name = item.get("flat_name")
        original_relative_path = item.get("original_relative_path")
        if not isinstance(flat_name, str) or not isinstance(original_relative_path, str):
            continue

        edited_path = edited_root / flat_name
        if not edited_path.is_file():
            missing += 1
            continue

        target_path = to_import_root / Path(original_relative_path)
        copy_image_for_import(edited_path, target_path)
        restored += 1
        print(f"[图片恢复] {edited_path} -> {target_path}")

    if missing:
        print(f"[图片恢复] 未修改的普通图片={missing}，已跳过。")
    return restored


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _undo_sprite_packing(image, rotation: int):
    from PIL import Image

    if rotation == 1:
        return image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    if rotation == 2:
        return image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    if rotation == 3:
        return image.transpose(Image.Transpose.ROTATE_180)
    if rotation == 4:
        return image.transpose(Image.Transpose.ROTATE_270)
    return image


def restore_split_sprites_to_import(
    edited_root: Path,
    to_import_root: Path,
    sprite_map_path: Path,
    source_root: Path,
) -> tuple[int, int, int]:
    """Patch edited Unity Sprite/NGUI UIAtlas PNGs into original Texture2D atlases."""
    from PIL import Image

    items = load_allpng_map(sprite_map_path)
    atlas_groups: dict[str, dict] = {}
    missing = 0
    invalid = 0
    for item in items:
        if item.get("item_type") not in {"sprite", "ngui_sprite"}:
            continue
        flat_name = item.get("flat_name")
        texture_png = item.get("texture_png")
        rect = item.get("rect")
        if not isinstance(flat_name, str) or not isinstance(texture_png, str):
            continue
        edited_path = edited_root / flat_name
        if not edited_path.is_file():
            missing += 1
            continue
        if not isinstance(rect, dict):
            invalid += 1
            print(f"[图集回拼][跳过] 缺少矩形数据: {flat_name}")
            continue
        texture_path = Path(texture_png)
        key = str(texture_path.resolve()).lower()
        group = atlas_groups.setdefault(
            key,
            {"texture_path": texture_path, "sprites": []},
        )
        group["sprites"].append((item, edited_path))

    patched_sprites = 0
    rebuilt_atlases = 0
    for group in atlas_groups.values():
        texture_path = group["texture_path"]
        try:
            relative_texture_path = texture_path.relative_to(source_root)
        except ValueError:
            invalid += len(group["sprites"])
            print(f"[图集回拼][跳过] 原图集不在 workspace/input 中: {texture_path}")
            continue
        target_path = to_import_root / relative_texture_path
        base_path = target_path if target_path.is_file() else texture_path
        if not base_path.is_file():
            invalid += len(group["sprites"])
            print(f"[图集回拼][跳过] 找不到原始 Texture2D: {texture_path}")
            continue
        try:
            with Image.open(base_path) as opened_atlas:
                atlas = opened_atlas.convert("RGBA")
        except Exception as exc:
            invalid += len(group["sprites"])
            print(f"[图集回拼][跳过] 无法读取原始 Texture2D {base_path}: {exc}")
            continue

        atlas_patched = 0
        for item, edited_path in group["sprites"]:
            rect = item["rect"]
            x = round(_number(rect.get("x")))
            y = round(_number(rect.get("y")))
            width = round(_number(rect.get("width")))
            height = round(_number(rect.get("height")))
            is_ngui = item.get("item_type") == "ngui_sprite"
            rotation = 0 if is_ngui else int(item.get("packing_rotation", 0) or 0)
            expected_size = (height, width) if rotation == 4 else (width, height)
            if width <= 0 or height <= 0:
                invalid += 1
                print(f"[图集回拼][跳过] 矩形尺寸无效: {edited_path.name}")
                continue
            try:
                with Image.open(edited_path) as opened_sprite:
                    sprite = opened_sprite.convert("RGBA")
            except Exception as exc:
                invalid += 1
                print(f"[图集回拼][跳过] 无法读取子图 {edited_path.name}: {exc}")
                continue
            if sprite.size != expected_size:
                invalid += 1
                print(
                    f"[图集回拼][跳过] {edited_path.name} 尺寸={sprite.size[0]}x{sprite.size[1]}，"
                    f"应为={expected_size[0]}x{expected_size[1]}"
                )
                continue
            sprite = _undo_sprite_packing(sprite, rotation)
            if sprite.size != (width, height):
                invalid += 1
                print(f"[图集回拼][跳过] 还原 packing rotation 后尺寸异常: {edited_path.name}")
                continue
            # Unity Sprite textureRect uses a bottom-left origin; NGUI UIAtlas
            # mSprites stores y from the top edge of the atlas.
            top = y if is_ngui else atlas.height - y - height
            if x < 0 or top < 0 or x + width > atlas.width or top + height > atlas.height:
                invalid += 1
                print(f"[图集回拼][跳过] 子图矩形超出原图集: {edited_path.name}")
                continue
            # 透明像素也必须覆盖原区域，否则旧图会从透明处残留。
            atlas.paste(sprite, (x, top))
            atlas_patched += 1
            patched_sprites += 1
            print(
                f"[图集回拼] {edited_path.name} -> {relative_texture_path} "
                f"({x}, {y}, {width}, {height})"
            )
        if atlas_patched:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            atlas.save(target_path, "PNG")
            rebuilt_atlases += 1
            print(f"[图集回拼][完成] {atlas_patched} 张子图 -> {target_path}")

    if missing:
        print(f"[图集回拼] 未修改的 Sprite/NGUI 子图={missing}，保留原图集对应区域。")
    return patched_sprites, rebuilt_atlases, invalid


def restore_edited_images_before_import(
    root_dir: Path,
    source_root: Path,
    to_import_root: Path,
) -> tuple[int, int, int, int]:
    """Restore flat edited images immediately before the image import overlay is built."""
    all_image_root = root_dir / "workspace" / "AllPNG"
    edited_root = all_image_root / "修改后的图片目录"
    edited_paths = sorted(path for path in edited_root.rglob("*") if path.is_file()) if edited_root.is_dir() else []
    if not edited_paths:
        print("[图片导入] 修改后的图片目录为空，跳过自动恢复目录结构。")
        return 0, 0, 0, 0

    image_map_path = all_image_root / "_allpng_map.json"
    sprite_map_path = all_image_root / "Sprite" / "_allsprite_map.json"
    if not image_map_path.is_file():
        sprite_names: set[str] = set()
        if sprite_map_path.is_file():
            sprite_names = {
                str(item.get("flat_name"))
                for item in load_allpng_map(sprite_map_path)
                if item.get("item_type") in {"sprite", "ngui_sprite"}
                and isinstance(item.get("flat_name"), str)
            }
        unknown_names = [path.name for path in edited_paths if path.name not in sprite_names]
        if unknown_names:
            preview = "、".join(unknown_names[:5])
            suffix = f" 等 {len(unknown_names)} 个文件" if len(unknown_names) > 5 else ""
            raise FileNotFoundError(
                f"检测到无法通过 Sprite 映射识别的修改图片（{preview}{suffix}），"
                f"但缺少普通图片映射: {image_map_path}。请先执行工具脚本菜单 1。"
            )

    print("[图片导入] 正在自动恢复修改图片的目录结构并回拼 Sprite 图集...", flush=True)
    restored = 0
    if image_map_path.is_file():
        restored = restore_images_to_import(edited_root, to_import_root, image_map_path)
    else:
        print("[图片恢复] 修改目录中只有已映射的 Sprite 子图，跳过普通图片恢复。")

    patched_sprites = 0
    rebuilt_atlases = 0
    invalid = 0
    if sprite_map_path.is_file():
        patched_sprites, rebuilt_atlases, invalid = restore_split_sprites_to_import(
            edited_root,
            to_import_root,
            sprite_map_path,
            source_root,
        )
    else:
        print("[图集回拼] 未找到 Sprite 映射，跳过拆分子图回拼；如需回拼请先执行工具脚本菜单 2。")

    print(
        f"[图片导入] 自动恢复完成：普通图片={restored}，回拼子图={patched_sprites}，"
        f"生成图集={rebuilt_atlases}，无效子图={invalid}。"
    )
    return restored, patched_sprites, rebuilt_atlases, invalid
