from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from support.image_import_utils import copy_image_for_import


NGUI_FONT_GENERATION_MANIFEST = "ngui_font_generation.json"


def _print_red(message: str) -> None:
    print(f"\033[91m{message}\033[0m", flush=True)


def print_not_imported_images(paths: list[Path]) -> None:
    unique_paths = sorted(
        {Path(path) for path in paths},
        key=lambda path: str(path).lower(),
    )
    if not unique_paths:
        print("\033[92m[图片导入][未导入图片] 无。\033[0m", flush=True)
        return
    _print_red(f"[图片导入][未导入图片] 共 {len(unique_paths)} 张:")
    for path in unique_paths:
        _print_red(f"  - {path}")


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


def restore_images_to_import(
    edited_root: Path,
    to_import_root: Path,
    map_path: Path,
    imported_images: set[Path] | None = None,
) -> int:
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
        if imported_images is not None:
            imported_images.add(edited_path.resolve())
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
    imported_images: set[Path] | None = None,
    ngui_font_atlas_bases: Mapping[str, Path] | None = None,
    prepared_ngui_atlases: set[str] | None = None,
) -> tuple[int, int, int]:
    """Patch edited Sprite/NGUI UIAtlas PNGs into their Texture2D atlases.

    An NGUI bitmap-font atlas is larger than its source Texture2D after the
    font generator runs.  For an edited Sprite belonging to one of those
    atlases, start from that generated atlas, then paste the Sprite into it.
    This keeps both the generated glyphs and the translated UI Sprite.
    """
    from PIL import Image

    items = load_allpng_map(sprite_map_path)
    # A flat filename can legitimately collide between an exported Texture2D
    # and one of its split Sprites (for example ``TextureName_2.png``).  The
    # ordinary-image restore runs first, so a file already imported there is a
    # deliberate full-texture replacement.  Do not reinterpret that same flat
    # file as a cropped Sprite and patch it into the replacement again.
    directly_restored_images = set(imported_images or ())
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
        if edited_path.resolve() in directly_restored_images:
            print(
                f"[图集回拼] {edited_path.name} 已作为同名完整 Texture2D 直接替换，"
                "跳过重复子图回拼。"
            )
            continue
        if not isinstance(rect, dict):
            invalid += 1
            _print_red(f"[图集回拼][跳过] 缺少矩形数据: {flat_name}")
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
            _print_red(f"[图集回拼][跳过] 原图集不在 workspace/input 中: {texture_path}")
            continue
        target_path = to_import_root / relative_texture_path
        font_atlas_path = (ngui_font_atlas_bases or {}).get(key)
        # A direct full-texture edit was already merged into an expanded font
        # atlas by ``compose_ngui_font_atlas_image_overrides``.  Otherwise a
        # generated font atlas must win over a stale/plain Texture2D target.
        if font_atlas_path is not None and key not in (prepared_ngui_atlases or set()):
            base_path = font_atlas_path
        else:
            base_path = target_path if target_path.is_file() else texture_path
        if not base_path.is_file():
            invalid += len(group["sprites"])
            _print_red(f"[图集回拼][跳过] 找不到原始 Texture2D: {texture_path}")
            continue
        try:
            with Image.open(base_path) as opened_atlas:
                atlas = opened_atlas.convert("RGBA")
        except Exception as exc:
            invalid += len(group["sprites"])
            _print_red(f"[图集回拼][跳过] 无法读取原始 Texture2D {base_path}: {exc}")
            continue

        atlas_patched = 0
        for item, edited_path in group["sprites"]:
            rect = item["rect"]
            x = round(_number(rect.get("x")))
            y = round(_number(rect.get("y")))
            width = round(_number(rect.get("width")))
            height = round(_number(rect.get("height")))
            downscale_multiplier = _number(
                item.get("downscale_multiplier"), 1.0
            )
            if downscale_multiplier <= 0:
                downscale_multiplier = 1.0
            is_ngui = item.get("item_type") == "ngui_sprite"
            rotation = 0 if is_ngui else int(item.get("packing_rotation", 0) or 0)
            expected_size = (height, width) if rotation == 4 else (width, height)
            if width <= 0 or height <= 0:
                invalid += 1
                _print_red(f"[图集回拼][跳过] 矩形尺寸无效: {edited_path.name}")
                continue
            try:
                with Image.open(edited_path) as opened_sprite:
                    sprite = opened_sprite.convert("RGBA")
            except Exception as exc:
                invalid += 1
                _print_red(f"[图集回拼][跳过] 无法读取子图 {edited_path.name}: {exc}")
                continue
            if sprite.size != expected_size:
                invalid += 1
                _print_red(
                    f"[图集回拼][跳过] {edited_path.name} 尺寸={sprite.size[0]}x{sprite.size[1]}，"
                    f"应为={expected_size[0]}x{expected_size[1]}"
                )
                continue
            sprite = _undo_sprite_packing(sprite, rotation)
            if sprite.size != (width, height):
                invalid += 1
                _print_red(f"[图集回拼][跳过] 还原 packing rotation 后尺寸异常: {edited_path.name}")
                continue
            left = round(x * downscale_multiplier)
            right = round((x + width) * downscale_multiplier)
            scaled_y = round(y * downscale_multiplier)
            scaled_upper = round((y + height) * downscale_multiplier)
            scaled_width = right - left
            scaled_height = scaled_upper - scaled_y
            if scaled_width <= 0 or scaled_height <= 0:
                invalid += 1
                _print_red(f"[图集回拼][跳过] 缩放后的矩形尺寸无效: {edited_path.name}")
                continue
            if sprite.size != (scaled_width, scaled_height):
                sprite = sprite.resize(
                    (scaled_width, scaled_height),
                    Image.Resampling.LANCZOS,
                )
            # Unity Sprite textureRect uses a bottom-left origin; NGUI UIAtlas
            # mSprites stores y from the top edge of the atlas.
            top = scaled_y if is_ngui else atlas.height - scaled_upper
            if (
                left < 0
                or top < 0
                or right > atlas.width
                or top + scaled_height > atlas.height
            ):
                invalid += 1
                _print_red(f"[图集回拼][跳过] 子图矩形超出原图集: {edited_path.name}")
                continue
            # 透明像素也必须覆盖原区域，否则旧图会从透明处残留。
            atlas.paste(sprite, (left, top))
            atlas_patched += 1
            patched_sprites += 1
            if imported_images is not None:
                imported_images.add(edited_path.resolve())
            print(
                f"[图集回拼] {edited_path.name} -> {relative_texture_path} "
                f"({left}, {scaled_y}, {scaled_width}, {scaled_height})"
                + (
                    f" [SpriteAtlas scale={downscale_multiplier:g}]"
                    if downscale_multiplier != 1.0
                    else ""
                )
            )
        if atlas_patched:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            atlas.save(target_path, "PNG")
            rebuilt_atlases += 1
            print(f"[图集回拼][完成] {atlas_patched} 张子图 -> {target_path}")

    if missing:
        print(f"[图集回拼] 未修改的 Sprite/NGUI 子图={missing}，保留原图集对应区域。")
    return patched_sprites, rebuilt_atlases, invalid


def _load_ngui_font_generation_groups(generation_manifest_path: Path) -> dict[Path, dict]:
    if not generation_manifest_path.is_file():
        raise FileNotFoundError(f"缺少 NGUI 字体生成清单: {generation_manifest_path}")

    manifest = json.loads(generation_manifest_path.read_text(encoding="utf-8-sig"))
    groups = manifest.get("groups", []) if isinstance(manifest, dict) else []
    if not isinstance(groups, list):
        raise ValueError(f"NGUI 字体生成清单 groups 无效: {generation_manifest_path}")

    group_by_texture: dict[Path, dict] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        texture = group.get("texture")
        if isinstance(texture, str) and texture:
            relative = Path(*texture.replace("\\", "/").split("/"))
            group_by_texture[relative] = group
    return group_by_texture


def load_ngui_font_atlas_bases(
    ngui_import_root: Path,
    source_root: Path,
    generation_manifest_path: Path,
) -> dict[str, Path]:
    """Return source Texture2D path -> generated NGUI font-atlas PNG."""
    if not ngui_import_root.is_dir():
        return {}

    groups = _load_ngui_font_generation_groups(generation_manifest_path)
    bases: dict[str, Path] = {}
    for relative in groups:
        source_path = source_root / relative
        generated_path = ngui_import_root / relative
        if not source_path.is_file():
            raise FileNotFoundError(f"NGUI 字体图集原图不存在: {source_path}")
        if not generated_path.is_file():
            raise FileNotFoundError(f"NGUI 字体图集待导入文件不存在: {generated_path}")
        bases[str(source_path.resolve()).lower()] = generated_path
    return bases


def restore_edited_images_before_import(
    workspace_root: Path,
    source_root: Path,
    to_import_root: Path,
    not_imported_images: list[Path] | None = None,
    ngui_font_import_root: Path | None = None,
    ngui_generation_manifest_path: Path | None = None,
) -> tuple[int, int, int, int]:
    """Restore flat edited images immediately before the image import overlay is built."""
    all_image_root = workspace_root / "AllPNG"
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
    imported_images: set[Path] = set()
    restored = 0
    if image_map_path.is_file():
        restored = restore_images_to_import(
            edited_root,
            to_import_root,
            image_map_path,
            imported_images,
        )
    else:
        print("[图片恢复] 修改目录中只有已映射的 Sprite 子图，跳过普通图片恢复。")

    ngui_font_atlas_bases: dict[str, Path] = {}
    prepared_ngui_atlases: set[str] = set()
    if (
        ngui_font_import_root is not None
        and ngui_generation_manifest_path is not None
        and ngui_generation_manifest_path.is_file()
    ):
        ngui_font_atlas_bases = load_ngui_font_atlas_bases(
            ngui_font_import_root,
            source_root,
            ngui_generation_manifest_path,
        )
        # Full-atlas image replacements are merged before Sprite reassembly;
        # reassembly below then pastes every edited Sprite onto this final font
        # atlas.  This is intentionally done even when no Sprite needs it so a
        # manually edited original atlas cannot erase generated glyphs.
        prepared = compose_ngui_font_atlas_image_overrides(
            ngui_font_import_root,
            to_import_root,
            to_import_root,
            ngui_generation_manifest_path,
        )
        prepared_ngui_atlases = {
            str((source_root / relative).resolve()).lower()
            for relative in prepared
        }

    patched_sprites = 0
    rebuilt_atlases = 0
    invalid = 0
    if sprite_map_path.is_file():
        patched_sprites, rebuilt_atlases, invalid = restore_split_sprites_to_import(
            edited_root,
            to_import_root,
            sprite_map_path,
            source_root,
            imported_images,
            ngui_font_atlas_bases,
            prepared_ngui_atlases,
        )
    else:
        print("[图集回拼] 未找到 Sprite 映射，跳过拆分子图回拼；如需回拼请先执行工具脚本菜单 2。")

    print(
        f"[图片导入] 自动恢复完成：普通图片={restored}，回拼子图={patched_sprites}，"
        f"生成图集={rebuilt_atlases}，无效子图={invalid}。"
    )
    if not_imported_images is not None:
        not_imported_images.extend(
            path for path in edited_paths if path.resolve() not in imported_images
        )
    return restored, patched_sprites, rebuilt_atlases, invalid


def compose_ngui_font_atlas_image_overrides(
    ngui_import_root: Path,
    image_import_root: Path,
    destination_root: Path,
    generation_manifest_path: Path,
) -> set[Path]:
    """Merge translated image pixels into expanded NGUI font atlases.

    NGUI font generation keeps the original atlas at the same top-left
    coordinates and packs new glyphs outside that rectangle.  Image restore,
    however, produces an atlas with the original dimensions.  When both are
    selected for import, copy only the original image rectangle onto the
    expanded font atlas so neither side overwrites the other.
    """
    from PIL import Image

    if not ngui_import_root.is_dir() or not image_import_root.is_dir():
        return set()

    ngui_files = {
        path.relative_to(ngui_import_root)
        for path in ngui_import_root.rglob("*")
        if path.is_file()
    }
    image_files = {
        path.relative_to(image_import_root)
        for path in image_import_root.rglob("*")
        if path.is_file()
    }
    conflicts = ngui_files & image_files
    if not conflicts:
        return set()
    if not generation_manifest_path.is_file():
        raise FileNotFoundError(
            f"图片与 NGUI 字体存在同路径文件，但缺少字体生成清单: {generation_manifest_path}"
        )

    manifest = json.loads(generation_manifest_path.read_text(encoding="utf-8-sig"))
    groups = manifest.get("groups", []) if isinstance(manifest, dict) else []
    if not isinstance(groups, list):
        raise ValueError(f"NGUI 字体生成清单 groups 无效: {generation_manifest_path}")

    group_by_texture: dict[Path, dict] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        texture = group.get("texture")
        if isinstance(texture, str) and texture:
            group_by_texture[Path(*texture.replace("\\", "/").split("/"))] = group

    unknown_conflicts = sorted(
        (relative for relative in conflicts if relative not in group_by_texture),
        key=lambda path: str(path).lower(),
    )
    if unknown_conflicts:
        preview = "、".join(str(path) for path in unknown_conflicts[:5])
        raise ValueError(f"图片与 NGUI 字体发生无法合成的同路径冲突: {preview}")

    composed: set[Path] = set()
    for relative in sorted(conflicts, key=lambda path: str(path).lower()):
        group = group_by_texture[relative]
        original_size = group.get("original_size")
        generated_size = group.get("generated_size")
        forbidden_rect = group.get("forbidden_rect")
        if (
            not isinstance(original_size, list)
            or len(original_size) != 2
            or not isinstance(generated_size, list)
            or len(generated_size) != 2
            or not isinstance(forbidden_rect, list)
            or len(forbidden_rect) != 4
        ):
            raise ValueError(f"NGUI 字体图集合成参数无效: {relative}")

        original_width, original_height = (int(value) for value in original_size)
        generated_width, generated_height = (int(value) for value in generated_size)
        x, y, width, height = (int(value) for value in forbidden_rect)
        if (width, height) != (original_width, original_height):
            raise ValueError(f"NGUI 字体图集原图区域尺寸不一致: {relative}")

        font_path = ngui_import_root / relative
        image_path = image_import_root / relative
        with Image.open(font_path) as opened_font:
            font_atlas = opened_font.convert("RGBA")
        with Image.open(image_path) as opened_image:
            image_override = opened_image.convert("RGBA")

        if font_atlas.size != (generated_width, generated_height):
            raise ValueError(
                f"NGUI 字体图集尺寸异常: {relative}，"
                f"实际={font_atlas.width}x{font_atlas.height}，"
                f"应为={generated_width}x{generated_height}"
            )
        if image_override.size == (original_width, original_height):
            original_region = image_override
        elif image_override.width >= x + width and image_override.height >= y + height:
            original_region = image_override.crop((x, y, x + width, y + height))
        else:
            raise ValueError(
                f"汉化图片无法覆盖 NGUI 原图区域: {relative}，"
                f"图片={image_override.width}x{image_override.height}，"
                f"需要={width}x{height}"
            )

        font_atlas.paste(original_region, (x, y))
        output_path = destination_root / relative
        output_path.parent.mkdir(parents=True, exist_ok=True)
        font_atlas.save(output_path, "PNG")
        composed.add(relative)
        print(
            f"[NGUI图集合成] 汉化图片原图区域 + 扩展字体区域 -> {relative} "
            f"({original_width}x{original_height} -> {generated_width}x{generated_height})",
            flush=True,
        )
    return composed
