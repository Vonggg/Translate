from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from support.config import load_config
from support.image_import_utils import copy_image_for_import
from support.menu_selection import parse_number_ranges
from support.script_output_cleanup import (
    MANIFEST_PATH as SCRIPT_OUTPUT_MANIFEST_PATH,
    collect_script_cleanup_targets,
    delete_script_cleanup_targets,
    load_script_output_manifest,
    target_size,
)
from pipeline.ai_translation_strategy import get_strategy
from pipeline.shared import atomic_write_json


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = SCRIPT_DIR / "workspace" / "input"
DEFAULT_DEST_ROOT = SCRIPT_DIR / "workspace" / "手动替换"
DEFAULT_ALL_IMAGE_ROOT = SCRIPT_DIR / "workspace" / "AllPNG"
DEFAULT_ALL_IMAGE_PNG_ROOT = DEFAULT_ALL_IMAGE_ROOT / "PNG"
DEFAULT_EDITED_IMAGE_ROOT = SCRIPT_DIR / "workspace" / "output" / "Image" / "修改后的图片目录"
DEFAULT_IMAGE_TO_IMPORT_ROOT = SCRIPT_DIR / "workspace" / "output" / "Image" / "ToImport"
DEFAULT_OBJECT_TO_IMPORT_ROOT = SCRIPT_DIR / "workspace" / "output" / "Object" / "ToImport"
DEFAULT_ALL_IMAGE_MAP = DEFAULT_ALL_IMAGE_ROOT / "_allpng_map.json"
DEFAULT_BLOCK_IMAGE_ROOT = DEFAULT_ALL_IMAGE_ROOT / "BlockImages"
DEFAULT_BLOCK_RECORD = SCRIPT_DIR / "workspace" / "records" / "blocked_image_objects.json"
DEFAULT_IMAGE_OBJECT_INDEX = SCRIPT_DIR / "workspace" / "records" / "image_object_index.json"
DEFAULT_CATALOG_OUTPUT = SCRIPT_DIR / "workspace" / "output" / "catalog" / "Output.json"
DEFAULT_ALL_SPRITE_ROOT = DEFAULT_ALL_IMAGE_ROOT / "Sprite"
DEFAULT_ALL_SPRITE_MAP = DEFAULT_ALL_SPRITE_ROOT / "_allsprite_map.json"
DEFAULT_MISSING_TTF_CHARS_FILE = SCRIPT_DIR / "workspace" / "records" / "translation_chars_missing_from_ttf.txt"
DEFAULT_TRANS_JSON = SCRIPT_DIR / "workspace" / "records" / "trans.json"
DEFAULT_RECORDS_JSON = SCRIPT_DIR / "workspace" / "records" / "records.json"
FIND_PATH_ID_SCRIPT = SCRIPT_DIR / "support" / "查找PathID文件.py"
FIND_ASSET_NAME_SCRIPT = SCRIPT_DIR / "support" / "查找资源名文件.py"
AI_TRANSLATION_BATCH_TOOL = SCRIPT_DIR / "tools" / "ai_translation_batch_tool.py"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp"}
OBJECT_INDEX_DIR_NAMES = {
    "gameobject",
    "transform",
    "recttransform",
    "sprite",
    "spriterenderer",
    "mesh",
    "meshfilter",
    "skinnedmeshrenderer",
}


def prompt_input(message: str) -> str:
    return input(f"\033[38;5;208m{message}\033[0m")


def iter_json_files(root: Path):
    for path in root.rglob("*.json"):
        if path.is_file():
            yield path


def iter_monobehaviour_json_files(root: Path):
    for current_root, dir_names, file_names in os.walk(root, topdown=True):
        dir_names.sort()
        dir_names[:] = [
            name for name in dir_names
            if name.lower() not in OBJECT_INDEX_DIR_NAMES
        ]
        current_path = Path(current_root)
        if current_path.name.lower() != "monobehaviour":
            continue
        for file_name in sorted(file_names):
            if file_name.lower().endswith(".json"):
                yield current_path / file_name


def iter_image_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
            yield path


def file_contains_text(path: Path, needle: str) -> bool:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="ignore")
    for line in text.splitlines():
        if needle in line:
            return True
    return False


def _search_worker_count(total_files: int) -> int:
    auto_count = 1 if total_files < 200 else max(2, min(16, (os.cpu_count() or 4) * 2))
    try:
        configured = int(load_config().max_scan_workers)
    except Exception:
        configured = 0
    return max(1, min(auto_count, configured)) if configured > 0 else auto_count


def copy_matched_json_files(source_root: Path, dest_root: Path, needle: str) -> list[Path]:
    matched: list[Path] = []
    json_files = list(iter_monobehaviour_json_files(source_root))
    print(f"[查找] 仅扫描 MonoBehaviour JSON，文件数: {len(json_files)}")
    worker_count = _search_worker_count(len(json_files))
    print(f"[查找] 并发搜索线程数: {worker_count}")
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = executor.map(lambda path: file_contains_text(path, needle), json_files)
        for index, (json_path, contains) in enumerate(zip(json_files, results), start=1):
            if index == 1 or index % 500 == 0 or index == len(json_files):
                try:
                    display_path = json_path.relative_to(source_root)
                except ValueError:
                    display_path = json_path
                print(f"[查找] 扫描进度: {index}/{len(json_files)}，当前命中: {len(matched)}，当前文件: {display_path}", flush=True)
            if not contains:
                continue
            relative_path = json_path.relative_to(source_root)
            target_path = dest_root / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(json_path, target_path)
            matched.append(target_path)
            print(f"[MATCH] {json_path} -> {target_path}")
    return matched


def prompt_path(label: str, default_path: Path) -> Path:
    raw = prompt_input(f"{label}（直接回车使用默认 {default_path}）: ").strip()
    return Path(raw) if raw else default_path


def prompt_text(label: str) -> str:
    while True:
        raw = prompt_input(f"{label}: ").strip()
        if raw:
            return raw
        print("输入不能为空，请重新输入。")


def run_search_copy() -> None:
    print()
    print("查找 JSON 并复制到手动替换")
    print("说明: 只递归扫描源目录下 MonoBehaviour 目录中的 .json 文件，逐行查找子串，命中就复制。")
    print()
    source_root = prompt_path("源目录", DEFAULT_SOURCE_ROOT)
    dest_root = prompt_path("目标目录", DEFAULT_DEST_ROOT)
    needle = prompt_text("请输入要逐行查找的子串")

    if not source_root.is_dir():
        print(f"源目录不存在: {source_root}")
        return

    dest_root.mkdir(parents=True, exist_ok=True)
    matched = copy_matched_json_files(source_root, dest_root, needle)
    print(f"完成，命中 {len(matched)} 个 JSON 文件。")


def run_find_path_id() -> None:
    print()
    print("查找 PathID 对应文件路径")
    print("说明: 默认递归扫描 workspace/input，输出所有命中的路径；如果命中图片，可选择编号复制到 Image/ToImport。")
    print()
    path_id = prompt_text("请输入 PathID")
    input_root = prompt_path("input 目录", DEFAULT_SOURCE_ROOT)
    subprocess.run(
        [
            sys.executable,
            str(FIND_PATH_ID_SCRIPT),
            path_id,
            "--input-root",
            str(input_root),
        ],
        check=False,
    )


def run_find_asset_name() -> None:
    print()
    print("查找资源名对应文件路径")
    print("说明: 默认递归扫描 workspace/input，按 manifest 的 AssetName 和文件名查找；如果命中图片，可选择编号复制到 Image/ToImport。")
    print()
    asset_name = prompt_text("请输入资源名")
    input_root = prompt_path("input 目录", DEFAULT_SOURCE_ROOT)
    subprocess.run(
        [
            sys.executable,
            str(FIND_ASSET_NAME_SCRIPT),
            asset_name,
            "--input-root",
            str(input_root),
        ],
        check=False,
    )


def copy_all_images(source_root: Path, dest_root: Path, map_path: Path) -> int:
    copied = 0
    mapping: list[dict[str, str]] = []
    dest_root.mkdir(parents=True, exist_ok=True)
    image_files = list(iter_image_files(source_root))
    print(f"[图片] 待复制图片数: {len(image_files)}")
    for index, image_path in enumerate(image_files, start=1):
        if image_path.resolve() == map_path.resolve():
            continue
        target_path = make_flat_unique_path(dest_root, image_path.name)
        shutil.copy2(image_path, target_path)
        original_relative_path = image_path.relative_to(source_root)
        flat_relative_path = target_path.relative_to(dest_root)
        mapping.append(
            {
                "flat_name": target_path.name,
                "flat_relative_path": flat_relative_path.as_posix(),
                "original_name": image_path.name,
                "original_relative_path": original_relative_path.as_posix(),
            }
        )
        copied += 1
        if copied == 1 or copied % 200 == 0 or index == len(image_files):
            print(f"[图片] 复制进度: {index}/{len(image_files)}，已复制 {copied} 个", flush=True)

    write_allpng_map(map_path, source_root, dest_root, mapping)
    return copied


def write_allpng_map(map_path: Path, source_root: Path, dest_root: Path, mapping: list[dict[str, str]]) -> None:
    map_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_root": str(source_root.resolve()),
        "allpng_root": str(dest_root.resolve()),
        "items": mapping,
    }
    map_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_flat_unique_path(dest_root: Path, file_name: str) -> Path:
    target_path = dest_root / file_name
    if not target_path.exists():
        return target_path

    stem = target_path.stem
    suffix = target_path.suffix
    index = 2
    while True:
        candidate = dest_root / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def run_copy_all_images() -> None:
    print()
    print("一键复制导出图片到 AllPNG")
    print(f"源目录: {DEFAULT_SOURCE_ROOT}")
    print(f"目标目录: {DEFAULT_ALL_IMAGE_PNG_ROOT}")
    print(f"映射文件: {DEFAULT_ALL_IMAGE_MAP}")
    print("说明: 会把图片直接平铺复制到 AllPNG\\PNG；同名文件会自动追加编号，并写入映射 JSON。")
    print()

    if not DEFAULT_SOURCE_ROOT.is_dir():
        print(f"源目录不存在: {DEFAULT_SOURCE_ROOT}")
        return

    if DEFAULT_ALL_IMAGE_ROOT.exists():
        print(f"正在清空: {DEFAULT_ALL_IMAGE_ROOT}")
        shutil.rmtree(DEFAULT_ALL_IMAGE_ROOT)
    DEFAULT_ALL_IMAGE_PNG_ROOT.mkdir(parents=True, exist_ok=True)
    copied = copy_all_images(DEFAULT_SOURCE_ROOT, DEFAULT_ALL_IMAGE_PNG_ROOT, DEFAULT_ALL_IMAGE_MAP)
    DEFAULT_EDITED_IMAGE_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"完成，已复制 {copied} 个图片到: {DEFAULT_ALL_IMAGE_PNG_ROOT}")
    print(f"映射已写入: {DEFAULT_ALL_IMAGE_MAP}")
    print(f"修改后的图片请放到: {DEFAULT_EDITED_IMAGE_ROOT}")


def load_allpng_map(map_path: Path) -> list[dict[str, str]]:
    if not map_path.is_file():
        raise FileNotFoundError(f"映射文件不存在: {map_path}")
    data = json.loads(map_path.read_text(encoding="utf-8-sig"))
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
        print(f"[RESTORE] {edited_path} -> {target_path}")

    if missing:
        print(f"提示: 有 {missing} 个映射文件未在修改目录中找到，已跳过。")
    return restored


def run_restore_edited_images_to_import() -> None:
    print()
    print("从修改后的图片目录恢复结构到 ToImport")
    print(f"修改目录: {DEFAULT_EDITED_IMAGE_ROOT}")
    print(f"目标目录: {DEFAULT_IMAGE_TO_IMPORT_ROOT}")
    print(f"映射文件: {DEFAULT_ALL_IMAGE_MAP}")
    print("说明: 按 AllPNG 映射 JSON 还原原始目录结构和原始文件名。")
    print()

    edited_root = prompt_path("修改后的图片目录", DEFAULT_EDITED_IMAGE_ROOT)
    to_import_root = prompt_path("ToImport 目标目录", DEFAULT_IMAGE_TO_IMPORT_ROOT)
    map_path = prompt_path("AllPNG 映射文件", DEFAULT_ALL_IMAGE_MAP)

    if not edited_root.is_dir():
        print(f"修改后的图片目录不存在: {edited_root}")
        return

    try:
        restored = restore_images_to_import(edited_root, to_import_root, map_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(exc)
        return

    print(f"完成，已恢复 {restored} 个图片到: {to_import_root}")


def _manifest_value(data: dict, *names: str, default=None):
    for name in names:
        if name in data:
            return data[name]
    return default


def _pptr(value: object) -> tuple[int, int]:
    if not isinstance(value, dict):
        return (0, 0)
    return (
        int(_manifest_value(value, "m_FileID", "FileID", default=0) or 0),
        int(_manifest_value(value, "m_PathID", "PathID", default=0) or 0),
    )


def _load_object_graph() -> tuple[dict, dict]:
    scopes: dict[tuple[str, str], dict] = {}
    texture_by_relative_path: dict[str, tuple[tuple[str, str], int]] = {}
    manifests = list(DEFAULT_SOURCE_ROOT.rglob("manifest.json"))
    print(f"[对象索引] 读取 manifest: {len(manifests)} 个", flush=True)

    for manifest_index, manifest_path in enumerate(manifests, start=1):
        manifest = _safe_read_json(manifest_path)
        if not isinstance(manifest, dict):
            continue
        items = _manifest_value(manifest, "Items", "items", default=[])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            type_name = str(_manifest_value(item, "TypeName", "typeName", default=""))
            if type_name not in {
                "Texture2D", "Sprite", "GameObject", "Transform",
                "RectTransform", "SpriteRenderer", "MonoBehaviour",
                "Mesh", "MeshFilter", "SkinnedMeshRenderer",
            }:
                continue
            relative_path = str(_manifest_value(item, "RelativePath", "relativePath", default=""))
            path_id = int(_manifest_value(item, "PathId", "PathID", "pathId", default=0) or 0)
            bundle_entry = str(_manifest_value(item, "BundleEntryName", "bundleEntryName", default=""))
            scope_key = (str(manifest_path), bundle_entry)
            scope = scopes.setdefault(
                scope_key,
                {
                    "manifest": manifest_path,
                    "source": str(_manifest_value(manifest, "SourceRelativePath", default="")),
                    "bundle_entry": bundle_entry,
                    "items": {},
                },
            )
            json_path = manifest_path.parent / Path(relative_path)
            scope["items"][(type_name, path_id)] = {
                "item": item,
                "path": json_path,
                "data": None,
            }
            if type_name == "Texture2D":
                try:
                    input_relative_path = json_path.relative_to(DEFAULT_SOURCE_ROOT).as_posix().lower()
                except ValueError:
                    input_relative_path = relative_path.replace("\\", "/").lower()
                texture_by_relative_path[input_relative_path] = (scope_key, path_id)
        if manifest_index == 1 or manifest_index % 100 == 0 or manifest_index == len(manifests):
            print(f"[对象索引] manifest 进度: {manifest_index}/{len(manifests)}", flush=True)
    return scopes, texture_by_relative_path


def _entry_data(entry: dict) -> dict | None:
    if entry["data"] is None:
        entry["data"] = _safe_read_json(entry["path"])
    return entry["data"] if isinstance(entry["data"], dict) else None


def _scope_entry(scope: dict, type_names: tuple[str, ...], path_id: int) -> dict | None:
    for type_name in type_names:
        entry = scope["items"].get((type_name, path_id))
        if entry:
            return entry
    return None


def _sprite_texture_path_id(sprite_data: dict) -> int:
    render_data = sprite_data.get("m_RD")
    if not isinstance(render_data, dict):
        render_data = sprite_data.get("m_RenderData")
    if not isinstance(render_data, dict):
        return 0
    return _pptr(render_data.get("texture"))[1] or _pptr(render_data.get("m_Texture"))[1]


def _sprite_render_data(sprite_data: dict) -> dict:
    value = sprite_data.get("m_RD")
    if not isinstance(value, dict):
        value = sprite_data.get("m_RenderData")
    return value if isinstance(value, dict) else {}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def run_split_sprite_atlases() -> None:
    try:
        from PIL import Image
    except ImportError:
        print("[图集拆分][错误] 当前 Python 环境缺少 Pillow。")
        return

    print()
    print("拆分 Sprite 图集")
    print("说明: 按 Sprite 的 textureRect 从对应 Texture2D 中裁出独立 PNG。")
    scopes, _ = _load_object_graph()
    if DEFAULT_ALL_SPRITE_ROOT.exists():
        shutil.rmtree(DEFAULT_ALL_SPRITE_ROOT)
    png_root = DEFAULT_ALL_SPRITE_ROOT / "PNG"
    png_root.mkdir(parents=True, exist_ok=True)
    mapping: list[dict] = []
    atlas_texture_paths: set[str] = set()
    skipped = 0

    for scope_key, scope in scopes.items():
        for (type_name, sprite_path_id), sprite_entry in scope["items"].items():
            if type_name != "Sprite":
                continue
            sprite_data = _entry_data(sprite_entry)
            if not sprite_data:
                skipped += 1
                continue
            render_data = _sprite_render_data(sprite_data)
            file_id, texture_path_id = _pptr(render_data.get("texture") or render_data.get("m_Texture"))
            if file_id != 0 or not texture_path_id:
                skipped += 1
                continue
            texture_entry = _scope_entry(scope, ("Texture2D",), texture_path_id)
            texture_path = texture_entry["path"] if texture_entry else None
            rect = render_data.get("textureRect") or render_data.get("m_TextureRect")
            if not texture_path or not texture_path.is_file() or not isinstance(rect, dict):
                skipped += 1
                continue
            x = round(_number(rect.get("x")))
            y = round(_number(rect.get("y")))
            width = round(_number(rect.get("width")))
            height = round(_number(rect.get("height")))
            if width <= 0 or height <= 0:
                skipped += 1
                continue
            try:
                with Image.open(texture_path) as atlas:
                    top = atlas.height - y - height
                    cropped = atlas.crop((x, top, x + width, top + height))
                    settings_raw = int(render_data.get("settingsRaw", 0) or 0)
                    rotation = (settings_raw >> 2) & 0xF
                    if rotation == 1:
                        cropped = cropped.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
                    elif rotation == 2:
                        cropped = cropped.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
                    elif rotation == 3:
                        cropped = cropped.transpose(Image.Transpose.ROTATE_180)
                    elif rotation == 4:
                        cropped = cropped.transpose(Image.Transpose.ROTATE_90)
                    asset_name = str(
                        _manifest_value(sprite_entry["item"], "AssetName", "assetName", default="")
                        or f"Sprite_{sprite_path_id}"
                    )
                    safe_asset_name = re.sub(r'[<>:"/\\|?*]', "_", asset_name)
                    target = make_flat_unique_path(
                        png_root,
                        f"{safe_asset_name}_{sprite_path_id}.png",
                    )
                    cropped.save(target, "PNG")
            except Exception as exc:
                skipped += 1
                print(f"[图集拆分][跳过] Sprite PathID={sprite_path_id}: {exc}")
                continue
            try:
                atlas_texture_paths.add(
                    texture_path.relative_to(DEFAULT_SOURCE_ROOT).as_posix().lower()
                )
            except ValueError:
                pass
            mapping.append(
                {
                    "item_type": "sprite",
                    "flat_name": target.name,
                    "sprite_name": asset_name,
                    "sprite_path_id": sprite_path_id,
                    "texture_path_id": texture_path_id,
                    "source_resource": scope["source"],
                    "bundle_entry": scope["bundle_entry"],
                    "sprite_json": str(sprite_entry["path"]),
                    "texture_png": str(texture_path),
                    "rect": {"x": x, "y": y, "width": width, "height": height},
                    "packing_rotation": rotation,
                }
            )
            if len(mapping) == 1 or len(mapping) % 200 == 0:
                print(f"[图集拆分] 已输出: {len(mapping)}，跳过: {skipped}", flush=True)

    copied_regular = 0
    try:
        allpng_items = load_allpng_map(DEFAULT_ALL_IMAGE_MAP)
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        allpng_items = []
    for item in allpng_items:
        original_relative = str(item.get("original_relative_path", "")).replace("\\", "/")
        if original_relative.lower() in atlas_texture_paths:
            continue
        flat_name = str(item.get("flat_name", ""))
        source_path = DEFAULT_ALL_IMAGE_PNG_ROOT / flat_name
        if not flat_name or not source_path.is_file():
            continue
        target = make_flat_unique_path(png_root, flat_name)
        shutil.copy2(source_path, target)
        mapping.append(
            {
                **item,
                "item_type": "texture",
                "flat_name": target.name,
            }
        )
        copied_regular += 1

    DEFAULT_ALL_SPRITE_MAP.write_text(
        json.dumps({"items": mapping}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sprite_count = sum(1 for item in mapping if item.get("item_type") == "sprite")
    print(
        f"[图集拆分][完成] 拆分 Sprite={sprite_count}，"
        f"普通图片={copied_regular}，跳过={skipped}"
    )
    print(f"[图集拆分] 输出目录: {png_root}")
    print(f"[图集拆分] 映射文件: {DEFAULT_ALL_SPRITE_MAP}")


def _game_object_name(scope: dict, path_id: int) -> str:
    entry = _scope_entry(scope, ("GameObject",), path_id)
    data = _entry_data(entry) if entry else None
    if not data:
        return f"<GameObject PathID={path_id}>"
    return str(data.get("m_Name") or f"<GameObject PathID={path_id}>")


def _build_transform_by_game_object(scope: dict) -> dict[int, tuple[int, dict]]:
    transform_by_game_object: dict[int, tuple[int, dict]] = {}
    for (type_name, path_id), entry in scope["items"].items():
        if type_name not in {"Transform", "RectTransform"}:
            continue
        data = _entry_data(entry)
        if not data:
            continue
        _, object_id = _pptr(data.get("m_GameObject"))
        if object_id:
            transform_by_game_object[object_id] = (path_id, data)
    return transform_by_game_object


def _game_object_component_path_ids(game_object_data: dict) -> list[int]:
    components = game_object_data.get("m_Component")
    if isinstance(components, dict):
        components = components.get("Array")
    if not isinstance(components, list):
        return []
    result: list[int] = []
    for item in components:
        if not isinstance(item, dict):
            continue
        pointer = item.get("component") or item.get("m_Component") or item
        file_id, path_id = _pptr(pointer)
        if file_id == 0 and path_id:
            result.append(path_id)
    return result


def _find_game_object_transform(scope: dict, game_object_path_id: int) -> dict | None:
    object_entry = _scope_entry(scope, ("GameObject",), game_object_path_id)
    object_data = _entry_data(object_entry) if object_entry else None
    if not object_data:
        return None
    for component_path_id in _game_object_component_path_ids(object_data):
        transform_entry = _scope_entry(
            scope,
            ("Transform", "RectTransform"),
            component_path_id,
        )
        if transform_entry:
            return _entry_data(transform_entry)
    return None


def _object_chain(
    scope: dict,
    game_object_path_id: int,
    max_parent_count: int = 8,
    transform_by_game_object: dict[int, tuple[int, dict]] | None = None,
) -> list[dict]:
    if transform_by_game_object is None:
        transform_by_game_object = _build_transform_by_game_object(scope)
    chain: list[dict] = []
    current_object = game_object_path_id
    visited: set[int] = set()
    while current_object and current_object not in visited and len(chain) <= max_parent_count:
        visited.add(current_object)
        object_entry = _scope_entry(scope, ("GameObject",), current_object)
        chain.append(
            {
                "path_id": current_object,
                "name": _game_object_name(scope, current_object),
                "source_json": str(object_entry["path"]) if object_entry else "",
            }
        )
        transform = transform_by_game_object.get(current_object)
        if not transform:
            break
        _, parent_transform_id = _pptr(transform[1].get("m_Father"))
        if not parent_transform_id:
            break
        parent_entry = _scope_entry(scope, ("Transform", "RectTransform"), parent_transform_id)
        parent_data = _entry_data(parent_entry) if parent_entry else None
        if not parent_data:
            break
        _, current_object = _pptr(parent_data.get("m_GameObject"))
    return chain


def _object_chain_direct(
    scope: dict,
    game_object_path_id: int,
    max_parent_count: int = 8,
) -> list[dict]:
    chain: list[dict] = []
    current_object = game_object_path_id
    visited: set[int] = set()
    while current_object and current_object not in visited and len(chain) <= max_parent_count:
        visited.add(current_object)
        object_entry = _scope_entry(scope, ("GameObject",), current_object)
        chain.append(
            {
                "path_id": current_object,
                "name": _game_object_name(scope, current_object),
                "source_json": str(object_entry["path"]) if object_entry else "",
            }
        )
        transform_data = _find_game_object_transform(scope, current_object)
        if not transform_data:
            break
        file_id, parent_transform_id = _pptr(transform_data.get("m_Father"))
        if file_id != 0 or not parent_transform_id:
            break
        parent_entry = _scope_entry(scope, ("Transform", "RectTransform"), parent_transform_id)
        parent_data = _entry_data(parent_entry) if parent_entry else None
        if not parent_data:
            break
        parent_file_id, current_object = _pptr(parent_data.get("m_GameObject"))
        if parent_file_id != 0:
            break
    return chain


def _selected_allpng_items() -> list[dict]:
    items = load_allpng_map(DEFAULT_ALL_IMAGE_MAP)
    available = {
        str(item.get("flat_name", "")).lower(): {**item, "_selection_kind": "texture"}
        for item in items
    }
    sprite_map = _safe_read_json(DEFAULT_ALL_SPRITE_MAP)
    if isinstance(sprite_map, dict) and isinstance(sprite_map.get("items"), list):
        for item in sprite_map["items"]:
            if isinstance(item, dict):
                available[str(item.get("flat_name", "")).lower()] = {
                    **item,
                    "_selection_kind": str(item.get("item_type", "sprite")),
                }
    selected_names: list[str]
    if DEFAULT_BLOCK_IMAGE_ROOT.is_dir():
        selected_names = [path.name for path in DEFAULT_BLOCK_IMAGE_ROOT.iterdir() if path.is_file()]
        if selected_names:
            print(f"[屏蔽对象] 从 {DEFAULT_BLOCK_IMAGE_ROOT} 读取指定图片: {len(selected_names)} 个")
        else:
            selected_names = []
    else:
        selected_names = []
    if not selected_names:
        raw = prompt_input("请输入 AllPNG 图片名称，多个名称用逗号分隔: ").strip()
        selected_names = [name.strip() for name in re.split(r"[,，]", raw) if name.strip()]
    missing = [name for name in selected_names if name.lower() not in available]
    for name in missing:
        print(f"[屏蔽对象][未找到] {name}")
    return [available[name.lower()] for name in selected_names if name.lower() in available]


def _object_manifest_snapshot() -> dict:
    digest = hashlib.sha256()
    count = 0
    total_size = 0
    latest_mtime_ns = 0
    for path in sorted(DEFAULT_SOURCE_ROOT.rglob("manifest.json")):
        try:
            stat = path.stat()
            relative = path.relative_to(DEFAULT_SOURCE_ROOT).as_posix()
        except (OSError, ValueError):
            continue
        count += 1
        total_size += stat.st_size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return {
        "manifest_count": count,
        "manifest_total_size": total_size,
        "latest_mtime_ns": latest_mtime_ns,
        "fingerprint": digest.hexdigest(),
    }


def _entry_contains_any_path_id(entry: dict, path_ids: set[int]) -> bool:
    try:
        text = entry["path"].read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return False
    return any(
        f'"m_PathID": {path_id}' in text or f'"m_PathID":{path_id}' in text
        for path_id in path_ids
    )


def _prefilter_reference_entries(
    scope: dict,
    type_names: set[str],
    path_ids: set[int],
) -> list[tuple[str, int, dict]]:
    if not path_ids:
        return []
    entries = [
        (type_name, path_id, entry)
        for (type_name, path_id), entry in scope["items"].items()
        if type_name in type_names
    ]
    if not entries:
        return []
    entry_by_path = {
        str(entry["path"].resolve()).lower(): (type_name, path_id, entry)
        for type_name, path_id, entry in entries
    }
    search_dirs = sorted({str(entry["path"].parent) for _, _, entry in entries})
    matched_paths: set[str] = set()
    try:
        path_id_list = sorted(path_ids)
        for start in range(0, len(path_id_list), 100):
            command = ["rg", "-l", "-F", "--glob", "*.json"]
            for path_id in path_id_list[start:start + 100]:
                command.extend(["-e", f'"m_PathID": {path_id}'])
                command.extend(["-e", f'"m_PathID":{path_id}'])
            command.extend(search_dirs)
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if result.returncode not in {0, 1}:
                raise RuntimeError(result.stderr.strip() or f"rg 返回码 {result.returncode}")
            matched_paths.update(
                str(Path(line.strip()).resolve()).lower()
                for line in result.stdout.splitlines()
                if line.strip()
            )
        return [
            entry_by_path[path]
            for path in sorted(matched_paths)
            if path in entry_by_path
        ]
    except (FileNotFoundError, OSError, RuntimeError):
        pass

    worker_count = _search_worker_count(len(entries))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        flags = executor.map(
            lambda item: _entry_contains_any_path_id(item[2], path_ids),
            entries,
        )
        return [item for item, matched in zip(entries, flags) if matched]


def _find_image_object_matches(
    scopes: dict,
    texture_targets: set[tuple[tuple[str, str], int]],
    sprite_targets: set[tuple[tuple[str, str], int]],
) -> list[dict]:
    matches: list[dict] = []
    total_scopes = len(scopes)
    for scope_index, (scope_key, scope) in enumerate(scopes.items(), start=1):
        target_texture_ids = {path_id for key, path_id in texture_targets if key == scope_key}
        target_sprite_ids = {path_id for key, path_id in sprite_targets if key == scope_key}
        if not target_texture_ids and not target_sprite_ids:
            continue
        print(
            f"[对象索引] 关系分析: {scope_index}/{total_scopes} "
            f"{scope['source']} {scope['bundle_entry']}",
            flush=True,
        )
        sprite_ids: set[int] = set(target_sprite_ids)
        sprite_candidates = _prefilter_reference_entries(
            scope,
            {"Sprite"},
            target_texture_ids,
        )
        print(
            f"[对象索引] Sprite 预筛选: "
            f"{sum(1 for type_name, _ in scope['items'] if type_name == 'Sprite')}"
            f" -> {len(sprite_candidates)}",
            flush=True,
        )
        for _type_name, path_id, entry in sprite_candidates:
            data = _entry_data(entry)
            if data and _sprite_texture_path_id(data) in target_texture_ids:
                sprite_ids.add(path_id)
        if not sprite_ids:
            continue
        chain_cache: dict[int, list[dict]] = {}
        component_candidates = _prefilter_reference_entries(
            scope,
            {"MonoBehaviour", "SpriteRenderer"},
            sprite_ids,
        )
        checked_components = len(component_candidates)
        print(
            f"[对象索引] 组件预筛选: "
            f"{sum(1 for type_name, _ in scope['items'] if type_name in {'MonoBehaviour', 'SpriteRenderer'})}"
            f" -> {checked_components}",
            flush=True,
        )
        for type_name, component_id, entry in component_candidates:
            data = _entry_data(entry)
            if not data:
                continue
            _, sprite_id = _pptr(data.get("m_Sprite"))
            _, game_object_id = _pptr(data.get("m_GameObject"))
            if sprite_id not in sprite_ids or not game_object_id:
                continue
            sprite_entry = _scope_entry(scope, ("Sprite",), sprite_id)
            sprite_data = _entry_data(sprite_entry) if sprite_entry else None
            chain = chain_cache.get(game_object_id)
            if chain is None:
                chain = _object_chain_direct(scope, game_object_id)
                chain_cache[game_object_id] = chain
            matches.append(
                {
                    "scope_key": scope_key,
                    "scope": scope,
                    "source": scope["source"],
                    "bundle_entry": scope["bundle_entry"],
                    "component_type": type_name,
                    "component_path_id": component_id,
                    "sprite_path_id": sprite_id,
                    "texture_path_id": _sprite_texture_path_id(sprite_data or {}),
                    "chain": chain,
                }
            )
        print(
            f"[对象索引] 关系完成: Sprite={len(sprite_ids)}，"
            f"组件={checked_components}，累计引用={len(matches)}",
            flush=True,
        )
    return matches


def _find_mesh_object_matches(
    scopes: dict,
    mesh_targets: set[tuple[tuple[str, str], int]],
) -> list[dict]:
    matches: list[dict] = []
    total_scopes = len(scopes)
    for scope_index, (scope_key, scope) in enumerate(scopes.items(), start=1):
        mesh_ids = {
            path_id for key, path_id in mesh_targets if key == scope_key
        }
        if not mesh_ids:
            continue
        print(
            f"[Mesh索引] 关系分析: {scope_index}/{total_scopes} "
            f"{scope['source']} {scope['bundle_entry']}",
            flush=True,
        )
        candidates = _prefilter_reference_entries(
            scope,
            {"MeshFilter", "SkinnedMeshRenderer"},
            mesh_ids,
        )
        print(
            f"[Mesh索引] 组件预筛选: "
            f"{sum(1 for type_name, _ in scope['items'] if type_name in {'MeshFilter', 'SkinnedMeshRenderer'})}"
            f" -> {len(candidates)}",
            flush=True,
        )
        chain_cache: dict[int, list[dict]] = {}
        for type_name, component_id, entry in candidates:
            data = _entry_data(entry)
            if not data:
                continue
            file_id, mesh_id = _pptr(data.get("m_Mesh"))
            _, game_object_id = _pptr(data.get("m_GameObject"))
            if file_id != 0 or mesh_id not in mesh_ids or not game_object_id:
                continue
            chain = chain_cache.get(game_object_id)
            if chain is None:
                chain = _object_chain_direct(scope, game_object_id)
                chain_cache[game_object_id] = chain
            if not chain:
                continue
            matches.append(
                {
                    "scope_key": scope_key,
                    "scope": scope,
                    "component_type": type_name,
                    "component_path_id": component_id,
                    "mesh_path_id": mesh_id,
                    "chain": chain,
                }
            )
        print(
            f"[Mesh索引] 关系完成: Mesh={len(mesh_ids)}，"
            f"组件={len(candidates)}，累计引用={len(matches)}",
            flush=True,
        )
    return matches


def _selected_mesh_queries(scopes: dict) -> list[tuple[str, set[tuple[tuple[str, str], int]]]]:
    available: dict[str, list[tuple[str, tuple[str, str], int]]] = {}
    for scope_key, scope in scopes.items():
        for (type_name, path_id), entry in scope["items"].items():
            if type_name != "Mesh":
                continue
            asset_name = str(
                _manifest_value(
                    entry["item"],
                    "AssetName",
                    "assetName",
                    default="",
                )
                or entry["path"].stem
            )
            aliases = {
                asset_name.lower(),
                entry["path"].name.lower(),
                entry["path"].stem.lower(),
                re.sub(r"_[-]?\d+$", "", entry["path"].stem).lower(),
            }
            for alias in aliases:
                if alias:
                    available.setdefault(alias, []).append(
                        (asset_name, scope_key, path_id)
                    )
    if not available:
        print(
            "[Mesh屏蔽][错误] workspace/input 中没有 Mesh 导出数据。"
            "请在一键导出中选择第 3 类。"
        )
        return []

    raw = prompt_input("请输入 Mesh 名称，多个名称用逗号分隔: ").strip()
    names = [
        value.strip()
        for value in re.split(r"[,，]", raw)
        if value.strip()
    ]
    queries: list[tuple[str, set[tuple[tuple[str, str], int]]]] = []
    for name in names:
        rows = available.get(name.lower(), [])
        if not rows:
            print(f"[Mesh屏蔽][未找到] {name}")
            continue
        targets = {(scope_key, path_id) for _asset_name, scope_key, path_id in rows}
        queries.append((name, targets))
    return queries


def _catalog_image_locations(item: dict) -> list[dict]:
    catalog = _safe_read_json(DEFAULT_CATALOG_OUTPUT)
    if not isinstance(catalog, dict):
        return []
    locations = catalog.get("Locations")
    if not isinstance(locations, list):
        locations = catalog.get("locations")
    if not isinstance(locations, list):
        entry_data = catalog.get("m_EntryDataString")
        if isinstance(entry_data, dict):
            locations = entry_data.get("locations")
    if not isinstance(locations, list):
        return []

    def normalized_name(value: object) -> str:
        stem = Path(str(value).replace("\\", "/")).stem.lower()
        return re.sub(r"_[-]?\d+$", "", stem)

    names = {
        normalized_name(item.get("original_name", "")),
        normalized_name(item.get("sprite_name", "")),
        normalized_name(item.get("original_relative_path", "")),
    }
    names.discard("")
    result: list[dict] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        internal_id = str(location.get("InternalId", ""))
        internal_name = normalized_name(internal_id)
        if internal_name not in names:
            continue
        result.append(
            {
                "internal_id": internal_id,
                "primary_key": str(location.get("PrimaryKey", "")),
                "resource_type": location.get("ResourceType"),
            }
        )
    return result


def _find_addressable_config_matches(
    scopes: dict,
    item: dict,
    locations: list[dict],
) -> tuple[list[dict], list[dict]]:
    exact_needles: set[str] = set()
    for location in locations:
        exact_needles.add(str(location.get("internal_id", "")))
        exact_needles.add(str(location.get("primary_key", "")))
    needles = {value for value in exact_needles if value}
    if not needles:
        needles = {
            str(item.get("sprite_name", "")),
            re.sub(
                r"_[-]?\d+$",
                "",
                Path(str(item.get("original_relative_path", ""))).stem,
            ),
        }
        needles.discard("")
    if not needles:
        return [], []

    config_hits: list[dict] = []
    object_matches: list[dict] = []
    candidates = [
        (scope_key, scope, type_name, path_id, entry)
        for scope_key, scope in scopes.items()
        for (type_name, path_id), entry in scope["items"].items()
        if type_name == "MonoBehaviour"
    ]
    if not candidates:
        return [], []
    candidate_by_path = {
        str(entry["path"].resolve()).lower(): (
            scope_key, scope, type_name, path_id, entry
        )
        for scope_key, scope, type_name, path_id, entry in candidates
    }
    matched_entries: dict[str, tuple] = {}
    def contains_addressable(entry: tuple) -> str | None:
        path = entry[4]["path"]
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return None
        if any(needle in text for needle in needles):
            return str(path.resolve()).lower()
        return None

    worker_count = _search_worker_count(len(candidates))
    print(
        f"[对象索引] Addressables 配置预筛选: "
        f"MonoBehaviour={len(candidates)}，线程={worker_count}",
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for resolved in executor.map(contains_addressable, candidates):
            if resolved and resolved in candidate_by_path:
                matched_entries[resolved] = candidate_by_path[resolved]

    for (
        scope_key,
        scope,
        type_name,
        component_id,
        entry,
    ) in matched_entries.values():
        data = _entry_data(entry)
        if not data:
            continue
        hit = {
            "source": scope["source"],
            "bundle_entry": scope["bundle_entry"],
            "component_type": type_name,
            "component_path_id": component_id,
            "json_path": str(entry["path"]),
        }
        config_hits.append(hit)
        file_id, game_object_id = _pptr(data.get("m_GameObject"))
        if file_id != 0 or not game_object_id:
            continue
        chain = _object_chain_direct(scope, game_object_id)
        if not chain:
            continue
        object_matches.append(
            {
                **hit,
                "scope_key": scope_key,
                "scope": scope,
                "sprite_path_id": int(item.get("sprite_path_id", 0) or 0),
                "texture_path_id": int(item.get("texture_path_id", 0) or 0),
                "chain": chain,
                "reference_kind": "addressables_config",
            }
        )
    return config_hits, object_matches


def _serializable_object_match(match: dict) -> dict:
    scope = match["scope"]
    return {
        "source": scope["source"],
        "bundle_entry": scope["bundle_entry"],
        "component_type": match["component_type"],
        "component_path_id": match["component_path_id"],
        "sprite_path_id": match["sprite_path_id"],
        "texture_path_id": match.get("texture_path_id", 0),
        "chain": match["chain"],
    }


def _image_query_cache_key(selected: list[dict]) -> str:
    identities: list[dict] = []
    for item in selected:
        if item.get("_selection_kind") == "sprite":
            identities.append(
                {
                    "kind": "sprite",
                    "source": str(item.get("source_resource", "")),
                    "bundle_entry": str(item.get("bundle_entry", "")),
                    "sprite_path_id": int(item.get("sprite_path_id", 0) or 0),
                }
            )
        else:
            identities.append(
                {
                    "kind": "texture",
                    "relative_path": str(item.get("original_relative_path", ""))
                    .replace("\\", "/")
                    .lower(),
                }
            )
    identities.sort(key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True))
    payload = json.dumps(identities, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_incremental_image_index(snapshot: dict) -> dict:
    cached = _safe_read_json(DEFAULT_IMAGE_OBJECT_INDEX)
    if (
        isinstance(cached, dict)
        and cached.get("version") == 5
        and cached.get("manifest_snapshot") == snapshot
        and isinstance(cached.get("queries"), dict)
    ):
        return cached
    if DEFAULT_IMAGE_OBJECT_INDEX.is_file():
        print("[对象索引] 导出 manifest 已变化或缓存格式已升级，清空旧索引。")
    return {
        "version": 5,
        "manifest_snapshot": snapshot,
        "queries": {},
    }


def _write_incremental_image_index(index_data: dict) -> None:
    DEFAULT_IMAGE_OBJECT_INDEX.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_IMAGE_OBJECT_INDEX.write_text(
        json.dumps(index_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _print_object_match(index: int, match: dict) -> None:
    scope = match.get("scope")
    source = match.get("source") or (scope["source"] if scope else "")
    bundle_entry = match.get("bundle_entry") or (scope["bundle_entry"] if scope else "")
    print()
    print(f"[{index}] 来源文件: {source}")
    if bundle_entry:
        print(f"    Bundle entry: {bundle_entry}")
    print(f"    组件: {match['component_type']} (PathID={match['component_path_id']})")
    print("    层级（0 是图片所在对象，数字越大层级越高）:")
    for level, node in enumerate(match["chain"]):
        print(f"      {level}. {node['name']} (PathID={node['path_id']})")


def _load_block_records() -> dict:
    data = _safe_read_json(DEFAULT_BLOCK_RECORD)
    if not isinstance(data, dict):
        return {"items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    return data


def _write_block_records(records: dict) -> None:
    DEFAULT_BLOCK_RECORD.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_BLOCK_RECORD.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _block_game_object(match: dict, level: int) -> bool:
    if level < 0 or level >= len(match["chain"]):
        print("[屏蔽对象][错误] 层级编号无效。")
        return False
    node = match["chain"][level]
    source_path = Path(str(node.get("source_json", "")))
    data = _safe_read_json(source_path)
    if not data:
        print("[屏蔽对象][错误] 没有找到对应 GameObject JSON。")
        return False
    try:
        relative = source_path.relative_to(DEFAULT_SOURCE_ROOT)
    except ValueError:
        print("[屏蔽对象][错误] GameObject JSON 不在 workspace/input 中。")
        return False
    target = DEFAULT_OBJECT_TO_IMPORT_ROOT / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    original_active = int(data.get("m_IsActive", 1))
    patched = dict(data)
    patched["m_IsActive"] = 0
    target.write_text(json.dumps(patched, ensure_ascii=False, indent=2), encoding="utf-8")

    records = _load_block_records()
    record_key = f"{source_path}|{node['path_id']}"
    for old_item in records["items"]:
        if not isinstance(old_item, dict) or old_item.get("record_key") != record_key:
            continue
        old_replacement = Path(str(old_item.get("replacement_json", "")))
        if old_replacement != target and old_replacement.is_file():
            old_replacement.unlink()
            print(f"[屏蔽对象] 已清理旧待导入文件: {old_replacement}")
    records["items"] = [
        item for item in records["items"]
        if not isinstance(item, dict) or item.get("record_key") != record_key
    ]
    records["items"].append(
        {
            "record_key": record_key,
            "source_json": str(source_path),
            "replacement_json": str(target),
            "source_resource": match.get("source", ""),
            "bundle_entry": match.get("bundle_entry", ""),
            "game_object_name": node["name"],
            "game_object_path_id": node["path_id"],
            "original_m_IsActive": original_active,
            "blocked_m_IsActive": 0,
        }
    )
    _write_block_records(records)
    print(f"[屏蔽对象][完成] {node['name']} (PathID={node['path_id']})")
    print(f"[屏蔽对象] 待导入 JSON: {target}")
    return True


def run_block_objects_by_image() -> None:
    print()
    print("按图片定位并屏蔽 GameObject")
    print("说明: 图片可放入 AllPNG/BlockImages，也可直接输入 AllPNG 文件名。")
    print("      每个匹配项按 0-8 显示对象父链，可选择屏蔽链上的任意对象。")
    selected = _selected_allpng_items()
    if not selected:
        print("[屏蔽对象] 没有有效图片。")
        return
    snapshot = _object_manifest_snapshot()
    index_data = _load_incremental_image_index(snapshot)
    queries = index_data["queries"]
    scopes = None
    textures = None

    for image_index, item in enumerate(selected, start=1):
        image_name = str(item.get("flat_name", ""))
        print()
        print(
            f"\033[94m[屏蔽对象] 图片 {image_index}/{len(selected)}: "
            f"{image_name}\033[0m"
        )
        query_key = _image_query_cache_key([item])
        cached_query = queries.get(query_key)
        if isinstance(cached_query, dict):
            matches = [
                match for match in cached_query.get("matches", [])
                if isinstance(match, dict)
            ]
            addressable_locations = [
                value for value in cached_query.get("addressable_locations", [])
                if isinstance(value, dict)
            ]
            addressable_configs = [
                value for value in cached_query.get("addressable_configs", [])
                if isinstance(value, dict)
            ]
            print(
                f"[对象索引] 命中图片缓存: {image_name}，"
                f"对象引用={len(matches)}"
            )
        else:
            if scopes is None or textures is None:
                scopes, textures = _load_object_graph()
            texture_targets: set[tuple[tuple[str, str], int]] = set()
            sprite_targets: set[tuple[tuple[str, str], int]] = set()
            if item.get("_selection_kind") == "sprite":
                source_resource = str(item.get("source_resource", ""))
                bundle_entry = str(item.get("bundle_entry", ""))
                scope_key = next(
                    (
                        key
                        for key, scope in scopes.items()
                        if scope["source"] == source_resource
                        and scope["bundle_entry"] == bundle_entry
                    ),
                    None,
                )
                if scope_key is not None:
                    sprite_targets.add(
                        (scope_key, int(item.get("sprite_path_id", 0) or 0))
                    )
            else:
                relative = (
                    str(item.get("original_relative_path", ""))
                    .replace("\\", "/")
                    .lower()
                )
                target = textures.get(relative)
                if target:
                    texture_targets.add(target)
                else:
                    print(f"[屏蔽对象][未索引] {image_name}: {relative}")
            matches = [
                _serializable_object_match(match)
                for match in _find_image_object_matches(
                    scopes,
                    texture_targets,
                    sprite_targets,
                )
            ]
            addressable_locations: list[dict] = []
            addressable_configs: list[dict] = []
            direct_match_count = len(matches)
            addressable_locations = _catalog_image_locations(item)
            if addressable_locations:
                print(
                    f"[对象索引] 继续反查 Addressables: "
                    f"位置={len(addressable_locations)}",
                    flush=True,
                )
                addressable_configs, addressable_matches = _find_addressable_config_matches(
                    scopes,
                    item,
                    addressable_locations,
                )
                matches.extend(
                    _serializable_object_match(match)
                    for match in addressable_matches
                )
                print(
                    f"[对象索引] 双链路完成: 直接组件={direct_match_count}，"
                    f"Addressables配置={len(addressable_configs)}，"
                    f"Addressables对象={len(addressable_matches)}",
                    flush=True,
                )
            else:
                print(
                    f"[对象索引] 双链路完成: 直接组件={direct_match_count}，"
                    "Addressables位置=0",
                    flush=True,
                )
            queries[query_key] = {
                "matches": matches,
                "addressable_locations": addressable_locations,
                "addressable_configs": addressable_configs,
            }
            _write_incremental_image_index(index_data)
            print(
                f"[对象索引] 图片查询完成: {image_name}，"
                f"对象引用={len(matches)}",
                flush=True,
            )
            print(f"[对象索引] 增量缓存已写入: {DEFAULT_IMAGE_OBJECT_INDEX}")

        unique_matches: dict[tuple[str, str, str, int], dict] = {}
        for match in matches:
            match_key = (
                str(match.get("source", "")),
                str(match.get("bundle_entry", "")),
                str(match.get("component_type", "")),
                int(match.get("component_path_id", 0) or 0),
            )
            unique_matches[match_key] = match
        matches = list(unique_matches.values())
        if not matches:
            if addressable_locations:
                print(
                    f"[屏蔽对象][提示] {image_name} 已在 Addressables 中定位，"
                    f"但没有找到可静态屏蔽的 GameObject。"
                )
                for location in addressable_locations:
                    print(
                        f"  地址: {location.get('internal_id', '')} "
                        f"(Key={location.get('primary_key', '')})"
                    )
                if addressable_configs:
                    print(f"  命中配置 JSON: {len(addressable_configs)} 个")
                    for config_hit in addressable_configs[:10]:
                        print(f"    {config_hit.get('json_path', '')}")
                    if len(addressable_configs) > 10:
                        print(f"    ... 其余 {len(addressable_configs) - 10} 个")
                print(
                    "[屏蔽对象][提示] 该对象可能由代码在运行时动态创建；"
                    "当前不会误改无关对象。"
                )
            else:
                print(
                    f"[屏蔽对象] {image_name} 没有找到静态引用对象，"
                    "也没有匹配到 Addressables 位置。"
                )
            continue

        for match_index, match in enumerate(matches, start=1):
            _print_object_match(match_index, match)
        if len(matches) == 1:
            selected_matches = [1]
        else:
            print()
            raw = prompt_input(
                f"选择当前图片的匹配项编号 [1-{len(matches)}]；"
                "支持 1-2 或 1,2；q 跳过，x 结束: "
            ).strip().lower()
            if raw == "x":
                return
            if raw == "q":
                continue
            try:
                selected_matches = [
                    int(value)
                    for value in parse_number_ranges(raw, set(range(1, len(matches) + 1)))
                ]
            except ValueError as exc:
                print(f"[屏蔽对象][错误] {exc}，已跳过当前图片。")
                continue

        for match_index in selected_matches:
            match = matches[match_index - 1]
            if len(matches) > 1:
                _print_object_match(match_index, match)
            level_raw = prompt_input(
                "选择要屏蔽的层级编号；0 为当前对象，q 跳过此匹配项: "
            ).strip().lower()
            if level_raw == "q":
                continue
            try:
                level = int(level_raw)
            except ValueError:
                print("[屏蔽对象][错误] 请输入数字。")
                continue
            _block_game_object(match, level)


def run_block_objects_by_mesh() -> None:
    print()
    print("按 Mesh 定位并屏蔽 GameObject")
    print("说明: 需要一键导出的第 2 类对象数据和第 3 类 Mesh 数据。")
    print("      每个匹配项显示对象父链，可选择屏蔽链上的任意对象。")
    scopes, _textures = _load_object_graph()
    if not any(
        type_name == "GameObject"
        for scope in scopes.values()
        for type_name, _path_id in scope["items"]
    ):
        print(
            "[Mesh屏蔽][错误] 没有 GameObject 对象数据。"
            "请在一键导出中同时选择第 2 类。"
        )
        return
    queries = _selected_mesh_queries(scopes)
    if not queries:
        return

    for query_index, (mesh_name, mesh_targets) in enumerate(queries, start=1):
        print()
        print(
            f"\033[94m[Mesh屏蔽] Mesh {query_index}/{len(queries)}: "
            f"{mesh_name}\033[0m"
        )
        matches = _find_mesh_object_matches(scopes, mesh_targets)
        unique_matches: dict[tuple[str, str, str, int], dict] = {}
        for match in matches:
            key = (
                str(match.get("source", "")),
                str(match.get("bundle_entry", "")),
                str(match.get("component_type", "")),
                int(match.get("component_path_id", 0) or 0),
            )
            unique_matches[key] = match
        matches = list(unique_matches.values())
        if not matches:
            print(
                f"[Mesh屏蔽][提示] {mesh_name} 没有找到静态 "
                "MeshFilter/SkinnedMeshRenderer 引用，可能由代码运行时创建或赋值。"
            )
            continue

        for match_index, match in enumerate(matches, start=1):
            _print_object_match(match_index, match)
        if len(matches) == 1:
            selected_matches = [1]
        else:
            raw = prompt_input(
                f"选择当前 Mesh 的匹配项编号 [1-{len(matches)}]；"
                "支持 1-2 或 1,2；q 跳过，x 结束: "
            ).strip().lower()
            if raw == "x":
                return
            if raw == "q":
                continue
            try:
                selected_matches = [
                    int(value)
                    for value in parse_number_ranges(
                        raw,
                        set(range(1, len(matches) + 1)),
                    )
                ]
            except ValueError as exc:
                print(f"[Mesh屏蔽][错误] {exc}，已跳过当前 Mesh。")
                continue

        for match_index in selected_matches:
            match = matches[match_index - 1]
            if len(matches) > 1:
                _print_object_match(match_index, match)
            level_raw = prompt_input(
                "选择要屏蔽的层级编号；0 为当前对象，q 跳过此匹配项: "
            ).strip().lower()
            if level_raw == "q":
                continue
            try:
                level = int(level_raw)
            except ValueError:
                print("[Mesh屏蔽][错误] 请输入数字。")
                continue
            _block_game_object(match, level)


def run_restore_blocked_objects() -> None:
    records = _load_block_records()
    items = [item for item in records["items"] if isinstance(item, dict)]
    if not items:
        print("[撤销屏蔽] 没有屏蔽记录。")
        return
    print()
    print("已屏蔽 GameObject:")
    for index, item in enumerate(items, start=1):
        print(
            f"{index}. {item.get('game_object_name')} "
            f"(PathID={item.get('game_object_path_id')}, 文件={item.get('source_resource')})"
        )
    raw = prompt_input("选择要撤销的编号，支持逗号、1-3 或 a，q 取消: ").strip().lower()
    if raw == "q":
        return
    selected = (
        list(range(1, len(items) + 1))
        if raw == "a"
        else [int(value) for value in parse_number_ranges(raw, set(range(1, len(items) + 1)))]
    )
    removed: set[int] = set()
    for index in selected:
        item = items[index - 1]
        source = Path(str(item.get("source_json", "")))
        target = Path(str(item.get("replacement_json", "")))
        source_data = _safe_read_json(source)
        if not isinstance(source_data, dict):
            print(f"[撤销屏蔽][失败] 原始 JSON 不存在或无效: {source}")
            continue
        source_data["m_IsActive"] = int(item.get("original_m_IsActive", 1))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(source_data, ensure_ascii=False, indent=2), encoding="utf-8")
        removed.add(index - 1)
        print(f"[撤销屏蔽][完成] {item.get('game_object_name')} -> m_IsActive={source_data['m_IsActive']}")
    records["items"] = [item for index, item in enumerate(items) if index not in removed]
    _write_block_records(records)


def _safe_read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _manifest_items(manifest: object) -> list[dict]:
    if not isinstance(manifest, dict):
        return []
    items = manifest.get("Items", [])
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _count_material_resolution(material_map: object) -> tuple[int, int, int]:
    total = 0
    resolved = 0
    unresolved_external = 0
    if not isinstance(material_map, dict):
        return total, resolved, unresolved_external
    for entry in material_map.values():
        if not isinstance(entry, dict):
            continue
        materials = entry.get("materials", [])
        if not isinstance(materials, list):
            continue
        for material in materials:
            if not isinstance(material, dict):
                continue
            total += 1
            if material.get("material_file"):
                resolved += 1
            elif isinstance(material.get("file_id"), int) and material.get("file_id") not in (0, None):
                unresolved_external += 1
    return total, resolved, unresolved_external


def _inspect_monobehaviour_exports(input_root: Path) -> dict[str, int]:
    mono_files = sorted(input_root.rglob("MonoBehaviour/*.json"))
    inspected = 0
    base_only = 0
    custom_field_files = 0
    nonempty_string_values = 0
    base_keys = {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}

    def count_strings(node: object) -> int:
        if isinstance(node, dict):
            return sum(count_strings(value) for value in node.values())
        if isinstance(node, list):
            return sum(count_strings(item) for item in node)
        return 1 if isinstance(node, str) and node.strip() else 0

    for path in mono_files[:1000]:
        data = _safe_read_json(path)
        if not isinstance(data, dict):
            continue
        inspected += 1
        keys = set(data.keys())
        if keys and keys.issubset(base_keys):
            base_only += 1
        else:
            custom_field_files += 1
        nonempty_string_values += count_strings(data)

    return {
        "total": len(mono_files),
        "inspected": inspected,
        "base_only": base_only,
        "custom_field_files": custom_field_files,
        "nonempty_string_values": nonempty_string_values,
    }


def run_restore_records_by_field() -> None:
    from pipeline.translation import (
        _ai_translation_request_configured,
        _start_wait_logger,
        _translate_ai_batch,
        _translate_one_text_with_provider_retry,
        rebuild_game_text_outputs,
    )

    cfg = load_config(quiet=True)
    backup_path = cfg.stage_record_dir / "records_unfiltered.json"
    records_path = cfg.stage_record_dir / cfg.output_scan_records_json
    trans_path = cfg.stage_record_dir / cfg.output_trans_json
    if not backup_path.is_file():
        print(
            f"[字段恢复][错误] 未找到完整备份: {backup_path}\n"
            "请先运行脚本 0；启用 AI 字段判断时会自动生成该备份。"
        )
        return
    backup = _safe_read_json(backup_path)
    current = _safe_read_json(records_path)
    translations = _safe_read_json(trans_path)
    if not isinstance(backup, list):
        print(f"[字段恢复][错误] 备份格式无效: {backup_path}")
        return
    if not isinstance(current, list):
        current = []
    if not isinstance(translations, dict):
        translations = {}

    raw = prompt_input(
        "输入要恢复的完整字段名，多个字段用逗号分隔"
        "（数组下标可写成 []）: "
    ).strip()
    requested = {
        value.strip()
        for value in re.split(r"[,，]", raw)
        if value.strip()
    }
    if not requested:
        print("[字段恢复] 未输入字段。")
        return

    def normalized(value: object) -> str:
        return re.sub(r"\[\d+\]", "[]", str(value))

    restored = [
        item for item in backup
        if isinstance(item, dict)
        and (
            str(item.get("field", "")) in requested
            or normalized(item.get("field", "")) in requested
        )
    ]
    if not restored:
        print("[字段恢复][错误] 完整备份中没有命中指定字段。")
        suggestions = sorted(
            {
                normalized(item.get("field", ""))
                for item in backup
                if isinstance(item, dict)
                and any(
                    str(item.get("field", "")).endswith(value)
                    for value in requested
                )
            }
        )
        if suggestions:
            print("[字段恢复][提示] 可能要输入的完整字段:")
            for value in suggestions[:30]:
                print(f"  {value}")
        return

    def record_key(item: dict) -> tuple:
        return (
            item.get("file_path"),
            item.get("field"),
            item.get("source_text"),
            item.get("path_id"),
            item.get("font_path_id"),
        )

    existing_keys = {
        record_key(item) for item in current if isinstance(item, dict)
    }
    added_records = [
        item for item in restored if record_key(item) not in existing_keys
    ]
    current.extend(added_records)
    atomic_write_json(records_path, current)

    restored_texts = list(
        dict.fromkeys(
            str(item.get("source_text", ""))
            for item in restored
            if isinstance(item.get("source_text"), str)
            and item.get("source_text")
        )
    )
    pending_texts: list[str] = []
    for text in restored_texts:
        old_value = translations.get(text)
        if not isinstance(old_value, str) or not old_value:
            translations[text] = ""
            pending_texts.append(text)
    atomic_write_json(trans_path, translations)
    print(
        f"[字段恢复] 命中记录={len(restored)}，新增 records={len(added_records)}，"
        f"待翻译文本={len(pending_texts)}"
    )
    print(f"[字段恢复] records: {records_path}")
    print(f"[字段恢复] trans: {trans_path}")
    if not pending_texts:
        rebuild_game_text_outputs(cfg, translations)
        print("\033[92m[字段恢复] 对应文本已有译文，无需重跑 AI。\033[0m")
        return

    pending_items = list(enumerate(pending_texts))
    completed_ids: set[int] = set()
    if _ai_translation_request_configured(cfg):
        strategy = get_strategy(cfg)
        batches = strategy.build_batches(pending_items)
        for batch_index, batch in enumerate(batches, start=1):
            print(
                f"\033[92m[字段恢复] 开始 AI batch={batch_index}/{len(batches)}，"
                f"条目={len(batch)}\033[0m",
                flush=True,
            )
            wait_stop, wait_thread = _start_wait_logger(
                f"[字段恢复] AI batch={batch_index}/{len(batches)}"
            )
            try:
                result = _translate_ai_batch(
                    batch,
                    cfg,
                    strategy,
                    batch_index,
                    len(batches),
                    artifact_prefix="ai_translation_restore",
                )
            except Exception as exc:
                print(
                    f"[字段恢复] AI batch={batch_index}/{len(batches)} 失败，"
                    f"本批回落到 {cfg.translate_provider}: {exc}"
                )
                continue
            finally:
                wait_stop.set()
                wait_thread.join(timeout=1)
            for item_id, source_text in batch:
                translated = result.get(item_id)
                if isinstance(translated, str) and translated:
                    translations[source_text] = translated
                    completed_ids.add(item_id)
            atomic_write_json(trans_path, translations)

    remaining = [
        (item_id, text)
        for item_id, text in pending_items
        if item_id not in completed_ids
    ]
    for completed, (_item_id, source_text) in enumerate(remaining, start=1):
        _, translated = _translate_one_text_with_provider_retry(
            cfg,
            source_text,
            max_attempts=3,
        )
        translations[source_text] = translated
        atomic_write_json(trans_path, translations)
        print(
            f"[字段恢复] 回落翻译 {completed}/{len(remaining)}: "
            f"{source_text} -> {translated}",
            flush=True,
        )

    rebuild_game_text_outputs(cfg, translations)
    print(
        f"\033[92m[字段恢复][完成] 已恢复字段并更新 trans.json，"
        f"新增译文={len(pending_texts)}。\033[0m"
    )


def run_compatibility_check() -> None:
    print()
    print("Unity 资源兼容性/导出状态检查")
    print("说明: 只读取 workspace/input 与 workspace/records，不修改文件。")
    print()

    cfg = load_config()
    input_root = cfg.resource_input_root
    records_root = cfg.stage_record_dir
    managed_root = cfg.resource_managed_root
    if not input_root.is_dir():
        print(f"[兼容性检查] input 目录不存在: {input_root}")
        return

    manifest_paths = sorted(input_root.rglob("manifest.json"))
    print(f"[兼容性检查] manifest 数: {len(manifest_paths)}")
    type_counts: dict[str, int] = {}
    unity_versions: set[str] = set()
    source_kinds: dict[str, int] = {}
    assets_file_count = 0
    external_count = 0
    item_count = 0
    for index, manifest_path in enumerate(manifest_paths, start=1):
        if index == 1 or index % 20 == 0 or index == len(manifest_paths):
            print(f"[兼容性检查] 读取 manifest: {index}/{len(manifest_paths)}", flush=True)
        manifest = _safe_read_json(manifest_path)
        if not isinstance(manifest, dict):
            continue
        unity_version = manifest.get("UnityVersion")
        if isinstance(unity_version, str) and unity_version:
            unity_versions.add(unity_version)
        source_kind = manifest.get("SourceKind")
        if isinstance(source_kind, str):
            source_kinds[source_kind] = source_kinds.get(source_kind, 0) + 1
        assets_files = manifest.get("AssetsFiles", [])
        if isinstance(assets_files, list):
            assets_file_count += len([item for item in assets_files if isinstance(item, dict)])
            for assets_file in assets_files:
                if isinstance(assets_file, dict) and isinstance(assets_file.get("Externals"), list):
                    external_count += len(assets_file["Externals"])
        items = _manifest_items(manifest)
        item_count += len(items)
        for item in items:
            type_name = item.get("TypeName")
            if isinstance(type_name, str):
                type_counts[type_name] = type_counts.get(type_name, 0) + 1

    print(f"[兼容性检查] SourceKind: {source_kinds or '未知'}")
    print(f"[兼容性检查] UnityVersion: {', '.join(sorted(unity_versions)) if unity_versions else 'manifest 未记录/为空'}")
    print(f"[兼容性检查] manifest 条目总数: {item_count}")
    for type_name in ("MonoBehaviour", "Material", "Font", "Texture2D", "TextAsset"):
        print(f"[兼容性检查] {type_name}: {type_counts.get(type_name, 0)}")
    print(f"[兼容性检查] AssetsFiles 记录数: {assets_file_count}")
    print(f"[兼容性检查] 外部依赖 Externals 记录数: {external_count}")

    file_id_map_path = records_root / cfg.output_file_id_map_json
    path_id_map_path = records_root / cfg.output_path_id_map_json
    material_map_path = records_root / cfg.output_material_map_json
    print(f"[兼容性检查] Managed 目录: {'存在' if managed_root.is_dir() else '不存在'} ({managed_root})")
    print(f"[兼容性检查] file_id_map: {'存在' if file_id_map_path.is_file() else '不存在'} ({file_id_map_path})")
    print(f"[兼容性检查] path_id_map: {'存在' if path_id_map_path.is_file() else '不存在'} ({path_id_map_path})")
    print(f"[兼容性检查] material_map: {'存在' if material_map_path.is_file() else '不存在'} ({material_map_path})")

    mono_export_info = _inspect_monobehaviour_exports(input_root)
    if mono_export_info["total"]:
        print(
            "[兼容性检查] MonoBehaviour 字段展开采样: "
            f"文件={mono_export_info['total']}，采样={mono_export_info['inspected']}，"
            f"仅基础字段={mono_export_info['base_only']}，含自定义字段={mono_export_info['custom_field_files']}，"
            f"非空字符串={mono_export_info['nonempty_string_values']}"
        )

    if material_map_path.is_file():
        total, resolved, unresolved_external = _count_material_resolution(_safe_read_json(material_map_path))
        print(f"[兼容性检查] 材质引用解析: 总数={total}，已定位 material_file={resolved}，未解析外部引用={unresolved_external}")

    warnings: list[str] = []
    if not manifest_paths:
        warnings.append("没有 manifest.json，请先执行资源菜单的一键导出。")
    if type_counts.get("MonoBehaviour", 0) == 0:
        warnings.append("没有导出 MonoBehaviour，文本扫描/阴影描边组件处理不可用。")
    elif mono_export_info["inspected"] and mono_export_info["custom_field_files"] == 0:
        warnings.append("MonoBehaviour 似乎只导出了基础字段，脚本自定义字段没有展开；文本扫描大概率不会命中。")
    if not unity_versions:
        warnings.append("manifest 中 UnityVersion 为空；无 TypeTree 资源可能无法加载 classdata，MonoBehaviour 字段容易展开失败。")
    if type_counts.get("Material", 0) == 0:
        warnings.append("没有导出 Material，材质阴影/描边参数屏蔽不可用。")
    if external_count and not file_id_map_path.is_file():
        warnings.append("存在外部依赖，但 file_id_map.json 不存在，file_id>0 的材质引用无法稳定定位。")
    if material_map_path.is_file():
        total, resolved, unresolved_external = _count_material_resolution(_safe_read_json(material_map_path))
        if unresolved_external:
            warnings.append("material_map 中仍有未解析外部材质引用，建议重新一键导出后再执行脚本 0。")

    if warnings:
        print("[兼容性检查] 结论: 需要注意")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("[兼容性检查] 结论: 当前导出状态适合继续执行脚本 0/4。")


def _default_ai_records_dir() -> Path:
    return SCRIPT_DIR / "workspace" / "records"


def _default_response_path_for_ai_request(request_path: Path) -> Path:
    name = request_path.name
    if "request" in name:
        return request_path.with_name(name.replace("request", "response", 1))
    return request_path.with_name("ai_translation_response.json")


def _ai_request_item_count(request_path: Path) -> int | None:
    try:
        payload = _safe_read_json(request_path)
        if not isinstance(payload, dict):
            return None
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        user_content = ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                user_content = str(message.get("content", ""))
                break
        if not user_content:
            return None
        data = json.loads(user_content)
        items = data.get("items")
        return len(items) if isinstance(items, list) else None
    except Exception:
        return None


def _ai_response_state(request_path: Path, response_path: Path, strategy: object) -> str:
    if not response_path.is_file():
        return "未生成 response"
    try:
        payload = _safe_read_json(response_path)
        if not isinstance(payload, dict):
            return "response 不是 JSON 对象"
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return "response 结构异常"
        finish_reason = str(choices[0].get("finish_reason", ""))
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return "response 缺少 message"
        content = str(message.get("content", ""))
        if finish_reason == "length":
            return "解析失败: length 截断"
        parsed = strategy.parse_response(content)
        request_count = _ai_request_item_count(request_path)
        if request_count is not None and len(parsed) != request_count:
            return f"可解析但数量不一致: {len(parsed)}/{request_count}, finish_reason={finish_reason or '未知'}"
        return f"可解析: {len(parsed)} 条, finish_reason={finish_reason or '未知'}"
    except Exception as exc:
        return f"解析失败: {exc}"


def _select_ai_batch_request_path() -> Path | None:
    records_dir = _default_ai_records_dir()
    request_files = sorted(records_dir.glob("ai_translation_request*.json"))
    if not request_files:
        print(f"未找到 AI request 批次文件: {records_dir / 'ai_translation_request*.json'}")
        raw = prompt_input("可手动输入 request 文件路径，或直接回车返回: ").strip()
        return Path(raw) if raw else None

    print()
    print("可用 AI request 批次文件:")
    strategy = get_strategy(load_config(quiet=True))
    for index, request_path in enumerate(request_files, start=1):
        response_path = _default_response_path_for_ai_request(request_path)
        response_state = _ai_response_state(request_path, response_path, strategy)
        print(f"{index}. {request_path.name} ({response_state})")
    print("q. 返回")

    while True:
        raw = prompt_input("请选择批次编号，或输入 request 文件路径: ").strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        if raw.isdigit():
            choice_index = int(raw)
            if 1 <= choice_index <= len(request_files):
                return request_files[choice_index - 1]
            print("编号超出范围，请重新输入。")
            continue
        if raw:
            return Path(raw)
        print("输入不能为空，请重新输入。")


def run_ai_translation_batch_tool(mode: str, request_path: Path, response_path: Path | None = None, patch_after: bool = False) -> int:
    if not AI_TRANSLATION_BATCH_TOOL.is_file():
        print(f"AI 补批工具不存在: {AI_TRANSLATION_BATCH_TOOL}")
        return 1
    if not request_path.is_file():
        print(f"请求文件不存在: {request_path}")
        return 1

    command = [
        sys.executable,
        str(AI_TRANSLATION_BATCH_TOOL),
        mode,
        "--request",
        str(request_path),
    ]
    if response_path is not None:
        command.extend(["--response", str(response_path)])
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        return result.returncode

    if patch_after and mode == "resend":
        patch_command = [
            sys.executable,
            str(AI_TRANSLATION_BATCH_TOOL),
            "patch-trans",
            "--request",
            str(request_path),
        ]
        if response_path is not None:
            patch_command.extend(["--response", str(response_path)])
        return subprocess.run(patch_command, check=False).returncode
    return 0


def run_ai_translation_batch_menu() -> None:
    print()
    print("AI 翻译单批补跑 / 修补 trans.json")
    print("说明: 读取某个 ai_translation_request_batch_XXX.json，重新请求 AI 生成对应 response；也可以用 response 修补 trans.json。")
    print()
    print("1. 重发单批，生成/覆盖 response")
    print("2. 用已有 response 修补 trans.json")
    print("3. 重发单批后立刻修补 trans.json")
    print("q. 返回")
    choice = prompt_input("请选择: ").strip().lower()
    if choice in {"q", "quit", "exit"}:
        return

    mode_map = {
        "1": "resend",
        "2": "patch-trans",
        "3": "resend-and-patch",
    }
    mode = mode_map.get(choice)
    if mode is None:
        print("无效选择。")
        return

    request_path = _select_ai_batch_request_path()
    if request_path is None:
        return
    response_path = _default_response_path_for_ai_request(request_path)
    print(f"response 文件将使用: {response_path}")
    result = run_ai_translation_batch_tool(mode, request_path, response_path)
    if result != 0:
        print(f"\033[91m[AI补批][失败] 操作未完成，返回码={result}。\033[0m")
        return
    if mode == "resend":
        print(
            "\033[94m[AI补批][下一步] response 已重新生成。"
            "请继续使用工具脚本 7 的选项 2，将该 response 修补进 trans.json。\033[0m"
        )
        return
    print("\033[92m[AI补批] 已成功修补 trans.json。\033[0m")
    print(
        "\033[94m[AI补批][下一步] 请回到主菜单运行脚本 3，"
        "从 trans.json 重建 game.txt 和 game_chars.txt；之后继续运行脚本 4-9。\033[0m"
    )


def atomic_write_json_file(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp_file:
        tmp_file.write(payload)
        tmp_path = Path(tmp_file.name)
    try:
        tmp_path.replace(path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_missing_chars(path: Path) -> list[str]:
    if not path.is_file():
        raise FileNotFoundError(f"不支持字符清单不存在: {path}")
    text = path.read_text(encoding="utf-8-sig")
    chars: list[str] = []
    seen: set[str] = set()
    for char in text:
        if char in seen or char in {"\ufeff", "\r", "\n", "\t"}:
            continue
        if not char.strip():
            continue
        seen.add(char)
        chars.append(char)
    return chars


def load_trans_json(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"trans.json 不存在: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError(f"trans.json 不是 JSON 对象: {path}")
    return {str(key): str(value) for key, value in data.items()}


def find_trans_values_containing_char(trans_data: dict[str, str], char: str) -> list[tuple[str, str]]:
    return [(source, translated) for source, translated in trans_data.items() if char in translated]


def format_preview_text(text: str, max_len: int = 120) -> str:
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return text if len(text) <= max_len else text[:max_len] + "..."


def format_char_contexts(text: str, char: str, radius: int = 45, limit: int = 3) -> list[str]:
    contexts: list[str] = []
    start = 0
    while len(contexts) < limit:
        index = text.find(char, start)
        if index < 0:
            break
        left = text[max(0, index - radius):index]
        right = text[index + len(char):index + len(char) + radius]
        prefix = "..." if index > radius else ""
        suffix = "..." if index + len(char) + radius < len(text) else ""
        context = f"{prefix}{left}>>>{char}<<<{right}{suffix}"
        contexts.append(
            context.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
        )
        start = index + len(char)
    return contexts


def run_clean_unsupported_ttf_chars() -> None:
    print()
    print("清理 trans.json 中模板 TTF 不支持的字符")
    print("说明: 读取 translation_chars_missing_from_ttf.txt，清理 trans.json 译文中的不支持字符。")
    print("  1. 一键删除全部不支持字符")
    print("  2. 逐字符查看并选择替换或删除")
    print("  q. 返回")
    print()

    mode = prompt_input("请选择清理方式: ").strip().lower()
    if mode in {"q", "quit", "exit"}:
        return
    if mode not in {"1", "2"}:
        print("[清理字符][错误] 无效选择，请输入 1、2 或 q。")
        return

    missing_path = prompt_path("不支持字符清单", DEFAULT_MISSING_TTF_CHARS_FILE)
    trans_path = prompt_path("trans.json", DEFAULT_TRANS_JSON)
    try:
        missing_chars = read_missing_chars(missing_path)
        trans_data = load_trans_json(trans_path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(exc)
        return

    print(f"[清理字符] 不支持字符数: {len(missing_chars)}")
    print(f"[清理字符] trans 条目数: {len(trans_data)}")
    counts = [(char, len(find_trans_values_containing_char(trans_data, char))) for char in missing_chars]
    hit_total = sum(count for _char, count in counts)
    print(f"[清理字符] 命中条目次数: {hit_total}")
    print("[清理字符] 命中统计:")
    for char, count in counts:
        print(f"[清理字符]   {repr(char)}: {count}")

    if mode == "1":
        missing_set = set(missing_chars)
        affected_keys = [
            source
            for source, translated in trans_data.items()
            if any(char in missing_set for char in translated)
        ]
        occurrence_total = sum(
            sum(1 for char in translated if char in missing_set)
            for translated in trans_data.values()
        )
        print()
        print(
            f"\033[38;5;208m[清理字符][确认] 将从 {len(affected_keys)} 个译文条目中，"
            f"删除全部 {len(missing_chars)} 种不支持字符，共 {occurrence_total} 次出现。\033[0m"
        )
        confirm = prompt_input(
            "确认一键删除全部不支持字符? 输入 y 确认，其它任意键取消: "
        ).strip().lower()
        if confirm != "y":
            print("\033[94m[清理字符] 已取消，trans.json 未修改。\033[0m")
            return
        translation_table = str.maketrans("", "", "".join(missing_chars))
        changed = 0
        for source, translated in list(trans_data.items()):
            new_value = translated.translate(translation_table)
            if new_value == translated:
                continue
            trans_data[source] = new_value
            changed += 1
        atomic_write_json_file(trans_path, trans_data)
        print(
            f"\033[92m[清理字符] 一键清理完成，更新译文条目={changed}，"
            f"删除字符出现次数={occurrence_total}: {trans_path}\033[0m"
        )
        print(
            "\033[94m[清理字符][下一步] 请运行主菜单脚本 3 重建 game.txt 和 "
            "game_chars.txt，然后重新运行主菜单脚本 7。\033[0m"
        )
        return

    changed_total = 0
    for index, char in enumerate(missing_chars, start=1):
        matches = find_trans_values_containing_char(trans_data, char)
        if not matches:
            continue
        print()
        print(f"[清理字符] {index}/{len(missing_chars)} 当前字符: {repr(char)}，命中 {len(matches)} 条")
        for item_index, (source, translated) in enumerate(matches[:50], start=1):
            print(f"  {item_index}. key:   {format_preview_text(source)}")
            contexts = format_char_contexts(translated, char)
            for context_index, context in enumerate(contexts, start=1):
                label = "value命中" if len(contexts) == 1 else f"value命中{context_index}"
                print(f"     {label}: {context}")
            occurrence_count = translated.count(char)
            if occurrence_count > len(contexts):
                print(f"     ... 本条共出现 {occurrence_count} 次，仅显示前 {len(contexts)} 处")
        if len(matches) > 50:
            print(f"  ... 其余 {len(matches) - 50} 条省略")

        confirm = prompt_input(f"是否统一替换/删除字符 {repr(char)} ? 输入 y 确认，其它任意键跳过: ").strip().lower()
        if confirm != "y":
            print(f"[清理字符] 已跳过: {repr(char)}")
            continue
        replacement = prompt_input("替换为（直接回车表示删除该字符）: ")
        changed = 0
        for source, translated in matches:
            new_value = translated.replace(char, replacement)
            if new_value != translated:
                trans_data[source] = new_value
                changed += 1
        changed_total += changed
        atomic_write_json_file(trans_path, trans_data)
        print(f"[清理字符] 已处理字符 {repr(char)}，更新条目: {changed}，已保存: {trans_path}")

    print()
    print(f"[清理字符] 完成，累计更新条目次数: {changed_total}")
    print(f"[清理字符] 已写回: {trans_path}")
    if changed_total:
        print(
            "\033[94m[清理字符][下一步] 请运行主菜单脚本 3 重建 game.txt 和 "
            "game_chars.txt，然后重新运行主菜单脚本 7。\033[0m"
        )


def run_remove_maybe_titles_from_trans() -> None:
    cfg = load_config()
    trans_path = cfg.stage_record_dir / cfg.output_trans_json
    maybe_title_path = cfg.stage_record_dir / "trans_maybe_title.json"
    print()
    print("从 trans.json 清理疑似资源键/标题键")
    print(f"\033[94m[清理资源键] trans.json: {trans_path}\033[0m")
    print(f"\033[94m[清理资源键] 排除清单: {maybe_title_path}\033[0m")

    missing_files = [
        path for path in (trans_path, maybe_title_path) if not path.is_file()
    ]
    if missing_files:
        for path in missing_files:
            print(f"\033[91m[清理资源键][失败] 文件不存在: {path}\033[0m")
        return

    try:
        trans_data = _safe_read_json(trans_path)
        maybe_title_data = _safe_read_json(maybe_title_path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"\033[91m[清理资源键][失败] 读取 JSON 失败: {exc}\033[0m")
        return
    if not isinstance(trans_data, dict):
        print(f"\033[91m[清理资源键][失败] trans.json 不是 JSON 对象: {trans_path}\033[0m")
        return
    if not isinstance(maybe_title_data, dict):
        print(
            f"\033[91m[清理资源键][失败] trans_maybe_title.json 不是 JSON 对象: "
            f"{maybe_title_path}\033[0m"
        )
        return

    excluded_keys = [key for key in maybe_title_data if isinstance(key, str)]
    matched_keys = [key for key in excluded_keys if key in trans_data]
    missing_count = len(excluded_keys) - len(matched_keys)
    print(
        f"\033[94m[清理资源键] 清单={len(excluded_keys)}，"
        f"trans 命中={len(matched_keys)}，已不存在={missing_count}\033[0m"
    )
    if not matched_keys:
        print("\033[92m[清理资源键] trans.json 中没有需要删除的键。\033[0m")
        return

    preview_limit = 30
    print("\033[94m[清理资源键] 即将删除的键:\033[0m")
    for index, key in enumerate(matched_keys[:preview_limit], start=1):
        print(f"  {index}. {format_preview_text(key)}")
    if len(matched_keys) > preview_limit:
        print(f"  ... 其余 {len(matched_keys) - preview_limit} 条省略")

    confirm = prompt_input(
        f"确认从 trans.json 删除以上 {len(matched_keys)} 个键? 输入 y 确认，其它任意键取消: "
    ).strip().lower()
    if confirm != "y":
        print("\033[94m[清理资源键] 已取消，trans.json 未修改。\033[0m")
        return

    matched_set = set(matched_keys)
    cleaned_data = {
        key: value for key, value in trans_data.items() if key not in matched_set
    }
    atomic_write_json_file(trans_path, cleaned_data)
    print(
        f"\033[92m[清理资源键] 已从 trans.json 删除 {len(matched_keys)} 个键，"
        f"剩余={len(cleaned_data)}: {trans_path}\033[0m"
    )



def _format_file_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def run_clean_script_outputs() -> None:
    cfg = load_config()
    try:
        manifest = load_script_output_manifest()
    except Exception as exc:
        print(f"\033[91m[脚本产物清理] 无法读取清单: {exc}\033[0m")
        return

    scripts = manifest["scripts"]
    print()
    print("按脚本清理生成文件")
    print(f"\033[94m[脚本产物清理] 清单: {SCRIPT_OUTPUT_MANIFEST_PATH}\033[0m")
    print("支持 1-2、4-7 或逗号组合；输入 a 会包含脚本 0 至 9。")
    ordered_script_ids = sorted(
        scripts,
        key=lambda value: (value == "a", int(value) if value.isdigit() else 0),
    )
    for script_id in ordered_script_ids:
        entry = scripts[script_id]
        outputs = entry.get("outputs", []) if isinstance(entry, dict) else []
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        print(f"{script_id}. {name}（清单规则 {len(outputs)} 条）")

    raw = prompt_input("请选择要清理的脚本，可输入多个编号并用逗号分隔，q 取消: ").strip().lower()
    if raw in {"", "q", "quit", "exit"}:
        print("[脚本产物清理] 已取消。")
        return
    try:
        script_ids = ["a"] if raw == "a" else parse_number_ranges(raw, set(range(10)))
    except ValueError as exc:
        print(f"\033[91m[脚本产物清理] {exc}\033[0m")
        return

    try:
        targets, notes = collect_script_cleanup_targets(cfg, script_ids)
    except Exception as exc:
        print(f"\033[91m[脚本产物清理] 解析清单失败: {exc}\033[0m")
        return

    for note in dict.fromkeys(notes):
        print(f"\033[94m[脚本产物清理][说明] {note}\033[0m")
    existing = [target for target in targets if target.path.exists() or target.path.is_symlink()]
    if not existing:
        print("\033[92m[脚本产物清理] 清单中的产物均不存在，无需删除。\033[0m")
        return

    total_files = 0
    total_size = 0
    print("\033[94m[脚本产物清理] 即将删除:\033[0m")
    for index, target in enumerate(existing, start=1):
        file_count, size = target_size(target.path)
        total_files += file_count
        total_size += size
        kind = "目录" if target.path.is_dir() else "文件"
        detail = f"，内部文件={file_count}" if target.path.is_dir() else ""
        print(f"  {index}. [{kind}] {target.path}{detail}，大小={_format_file_size(size)}")
    print(
        f"\033[94m[脚本产物清理] 目标={len(existing)}，"
        f"文件={total_files}，总大小={_format_file_size(total_size)}\033[0m"
    )

    confirm = prompt_input("确认按清单删除以上产物? 输入 y 确认，其它任意键取消: ").strip().lower()
    if confirm != "y":
        print("[脚本产物清理] 已取消，未删除文件。")
        return
    try:
        removed = delete_script_cleanup_targets(existing)
    except Exception as exc:
        print(f"\033[91m[脚本产物清理] 删除失败: {exc}\033[0m")
        return
    print(f"\033[92m[脚本产物清理] 已删除 {len(removed)} 个清单目标。\033[0m")


def main() -> int:
    if len(sys.argv) > 1:
        command = sys.argv[1].strip().lower()
        if command in {"resend-ai-batch", "ai-batch-resend"}:
            if len(sys.argv) < 3:
                print(f"用法: python {Path(__file__).name} resend-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("resend", Path(sys.argv[2]))
        if command in {"patch-ai-batch", "ai-batch-patch"}:
            if len(sys.argv) < 3:
                print(f"用法: python {Path(__file__).name} patch-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("patch-trans", Path(sys.argv[2]))
        if command in {"resend-and-patch-ai-batch", "ai-batch-resend-and-patch"}:
            if len(sys.argv) < 3:
                print(f"用法: python {Path(__file__).name} resend-and-patch-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("resend-and-patch", Path(sys.argv[2]))
        if command in {"clean-unsupported-ttf-chars", "clean-ttf-chars"}:
            run_clean_unsupported_ttf_chars()
            return 0
        if command in {"clean-script-outputs", "clean-step-outputs"}:
            run_clean_script_outputs()
            return 0
        if command in {"clean-trans-maybe-title", "clean-maybe-title"}:
            run_remove_maybe_titles_from_trans()
            return 0
        if command in {"block-image-objects", "block-objects-by-image"}:
            run_block_objects_by_image()
            return 0
        if command in {"restore-blocked-objects", "undo-blocked-objects"}:
            run_restore_blocked_objects()
            return 0
        if command in {"split-sprite-atlases", "split-sprites"}:
            run_split_sprite_atlases()
            return 0
        if command in {"restore-records-by-field", "restore-field"}:
            run_restore_records_by_field()
            return 0
        if command in {"block-mesh-objects", "block-objects-by-mesh"}:
            run_block_objects_by_mesh()
            return 0
        print(f"未知命令: {sys.argv[1]}")
        print(f"用法: python {Path(__file__).name} resend-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} patch-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} resend-and-patch-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} clean-unsupported-ttf-chars")
        print(f"或: python {Path(__file__).name} clean-script-outputs")
        print(f"或: python {Path(__file__).name} clean-trans-maybe-title")
        return 1

    while True:
        print()
        print("工具菜单")
        print("1. 查找字符串并复制 JSON")
        print("2. 查找 PathID 对应文件路径")
        print("3. 查找资源名对应文件路径")
        print("4. 一键复制导出图片到 workspace/AllPNG")
        print("5. 从修改后的图片目录恢复结构到 Image/ToImport")
        print("6. Unity 资源兼容性/导出状态检查")
        print("7. AI 翻译单批补跑 / 修补 trans.json")
        print("8. 清理 trans.json 中模板 TTF 不支持的字符")
        print("9. 按脚本产物清单清理指定脚本生成的文件")
        print("10. 按 trans_maybe_title.json 清理 trans.json")
        print("11. 按图片定位对象并选择层级屏蔽")
        print("12. 撤销已记录的对象屏蔽")
        print("13. 按 Sprite 数据拆分 Texture2D 图集")
        print("14. 从完整备份按字段恢复 records，并补译到 trans")
        print("15. 按 Mesh 定位对象并选择层级屏蔽")
        print("q. 退出")
        try:
            choice = prompt_input("请选择: ").strip().lower()
        except EOFError:
            return 0

        if choice == "1":
            run_search_copy()
            continue
        if choice == "2":
            run_find_path_id()
            continue
        if choice == "3":
            run_find_asset_name()
            continue
        if choice == "4":
            run_copy_all_images()
            continue
        if choice == "5":
            run_restore_edited_images_to_import()
            continue
        if choice == "6":
            run_compatibility_check()
            continue
        if choice == "7":
            run_ai_translation_batch_menu()
            continue
        if choice == "8":
            run_clean_unsupported_ttf_chars()
            continue
        if choice == "9":
            run_clean_script_outputs()
            continue
        if choice == "10":
            run_remove_maybe_titles_from_trans()
            continue
        if choice == "11":
            run_block_objects_by_image()
            continue
        if choice == "12":
            run_restore_blocked_objects()
            continue
        if choice == "13":
            run_split_sprite_atlases()
            continue
        if choice == "14":
            run_restore_records_by_field()
            continue
        if choice == "15":
            run_block_objects_by_mesh()
            continue
        if choice in {"q", "quit", "exit"}:
            return 0

        print("无效选择，请输入 1-15 或 q。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
