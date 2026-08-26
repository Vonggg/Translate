from __future__ import annotations

import argparse
import difflib
import json
import hashlib
import math
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from support.config import activate_config_path, load_config
from support.image_restore import load_allpng_map
from support.menu_selection import parse_number_ranges
from support.script_output_cleanup import (
    MANIFEST_PATH as SCRIPT_OUTPUT_MANIFEST_PATH,
    collect_script_cleanup_targets,
    count_script_output_rules,
    delete_script_cleanup_targets,
    expand_script_output_ids,
    load_script_output_manifest,
    target_size,
)
from pipeline.ai_translation_strategy import get_strategy
from pipeline.codex_cli_provider import (
    codex_cli_available,
    request_structured_output,
)
from pipeline.shared import atomic_write_json


SCRIPT_DIR = Path(__file__).resolve().parent


def _parse_entry_args(argv: list[str], *, activate: bool) -> list[str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--config", type=Path)
    options, remaining = parser.parse_known_args(argv)
    if activate and options.config is not None:
        activate_config_path(options.config)
    return remaining


# 工具脚本的大量默认路径会在模块初始化时生成。作为脚本直接运行时，
# 必须先激活 --config，不能等到 main() 才切换。
_ENTRY_ARGS: list[str] | None = None
if __name__ == "__main__":
    _ENTRY_ARGS = _parse_entry_args(sys.argv[1:], activate=True)

try:
    _DEFAULT_CONFIG = load_config(quiet=True)
except Exception:
    _DEFAULT_CONFIG = None
DEFAULT_WORKSPACE_ROOT = (
    _DEFAULT_CONFIG.workspace_root
    if _DEFAULT_CONFIG is not None
    else SCRIPT_DIR / "workspace"
)
DEFAULT_SOURCE_ROOT = (
    _DEFAULT_CONFIG.resource_input_root
    if _DEFAULT_CONFIG is not None
    else DEFAULT_WORKSPACE_ROOT / "input"
)
DEFAULT_RECORD_ROOT = (
    _DEFAULT_CONFIG.stage_record_dir
    if _DEFAULT_CONFIG is not None
    else DEFAULT_WORKSPACE_ROOT / "records"
)
DEFAULT_DEST_ROOT = DEFAULT_WORKSPACE_ROOT / "手动替换"
DEFAULT_ALL_IMAGE_ROOT = DEFAULT_WORKSPACE_ROOT / "AllPNG"
DEFAULT_ALL_IMAGE_PNG_ROOT = DEFAULT_ALL_IMAGE_ROOT / "PNG"
DEFAULT_EDITED_IMAGE_ROOT = DEFAULT_ALL_IMAGE_ROOT / "修改后的图片目录"
DEFAULT_OBJECT_TO_IMPORT_ROOT = (
    _DEFAULT_CONFIG.object_import_dir
    if _DEFAULT_CONFIG is not None
    else DEFAULT_WORKSPACE_ROOT / "output" / "Object" / "ToImport"
)
DEFAULT_ALL_IMAGE_MAP = DEFAULT_ALL_IMAGE_ROOT / "_allpng_map.json"
DEFAULT_BLOCK_IMAGE_ROOT = DEFAULT_ALL_IMAGE_ROOT / "屏蔽object"
DEFAULT_BLOCK_RECORD = DEFAULT_RECORD_ROOT / "blocked_image_objects.json"
DEFAULT_STORE_PRODUCT_BLOCK_RECORD = (
    DEFAULT_RECORD_ROOT / "blocked_store_products.json"
)
DEFAULT_DYNAMIC_LIST_AI_REVIEW = (
    DEFAULT_RECORD_ROOT / "dynamic_list_ai_review.json"
)
DEFAULT_IMAGE_OBJECT_INDEX = DEFAULT_RECORD_ROOT / "image_object_index.json"
DEFAULT_OBJECT_GRAPH_CACHE = DEFAULT_RECORD_ROOT / "object_graph_cache.pkl"
DEFAULT_FILE_ID_MAP = DEFAULT_RECORD_ROOT / "file_id_map.json"
DEFAULT_OBJECT_PREVIEW_ROOT = DEFAULT_WORKSPACE_ROOT / "preview" / "ObjectHierarchy"
DEFAULT_CATALOG_OUTPUT = (
    _DEFAULT_CONFIG.result_dir / "catalog" / "Output.json"
    if _DEFAULT_CONFIG is not None
    else DEFAULT_WORKSPACE_ROOT / "output" / "catalog" / "Output.json"
)
DEFAULT_ALL_SPRITE_ROOT = DEFAULT_ALL_IMAGE_ROOT / "Sprite"
DEFAULT_ALL_SPRITE_PNG_ROOT = DEFAULT_ALL_SPRITE_ROOT / "PNG"
DEFAULT_ALL_SPRITE_MAP = DEFAULT_ALL_SPRITE_ROOT / "_allsprite_map.json"
DEFAULT_MISSING_TTF_CHARS_FILE = DEFAULT_RECORD_ROOT / "translation_chars_missing_from_ttf.txt"
DEFAULT_TRANS_JSON = DEFAULT_RECORD_ROOT / "trans.json"
DEFAULT_RECORDS_JSON = DEFAULT_RECORD_ROOT / "records.json"
FIND_PATH_ID_SCRIPT = SCRIPT_DIR / "support" / "查找PathID文件.py"
FIND_ASSET_NAME_SCRIPT = SCRIPT_DIR / "support" / "查找资源名文件.py"
AI_TRANSLATION_BATCH_TOOL = SCRIPT_DIR / "tools" / "ai_translation_batch_tool.py"
RESOURCE_REPACK_VALIDATOR = SCRIPT_DIR / "tools" / "resource_repack_validator.py"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp"}
OBJECT_INDEX_DIR_NAMES = {
    "gameobject",
    "transform",
    "recttransform",
    "sprite",
    "spriteatlas",
    "spriterenderer",
    "mesh",
    "meshfilter",
    "skinnedmeshrenderer",
}
OBJECT_GRAPH_CACHE_VERSION = 3
DYNAMIC_STORE_SCAN_VERSION = 11
RUNTIME_LAYOUT_RENDER_VERSION = 1
_OBJECT_GRAPH_CACHE_STATE: dict | None = None
_UNITY_HORIZONTAL_LAYOUT_SCRIPT_PATH_IDS = {
    -3229211799126679632,
    664,  # Unity 6000.4 globalgamemanagers MonoScript
}
_UNITY_VERTICAL_LAYOUT_SCRIPT_PATH_IDS = {
    -4621643977240678714,
    1227,  # Unity 6000.4 globalgamemanagers MonoScript
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


def reset_allpng_generated_content(
    allpng_root: Path,
    edited_root: Path,
    block_image_root: Path | None = None,
) -> None:
    """Clear generated AllPNG content without deleting user-maintained work directories."""
    allpng_root.mkdir(parents=True, exist_ok=True)
    block_image_root = block_image_root or (allpng_root / "屏蔽object")
    preserved_roots = {edited_root.resolve(), block_image_root.resolve()}
    for child in allpng_root.iterdir():
        if child.resolve() in preserved_roots:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    edited_root.mkdir(parents=True, exist_ok=True)
    block_image_root.mkdir(parents=True, exist_ok=True)


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
        print(f"正在刷新生成内容（保留用户工作目录）: {DEFAULT_ALL_IMAGE_ROOT}")
    reset_allpng_generated_content(
        DEFAULT_ALL_IMAGE_ROOT,
        DEFAULT_EDITED_IMAGE_ROOT,
        DEFAULT_BLOCK_IMAGE_ROOT,
    )
    DEFAULT_ALL_IMAGE_PNG_ROOT.mkdir(parents=True, exist_ok=True)
    copied = copy_all_images(DEFAULT_SOURCE_ROOT, DEFAULT_ALL_IMAGE_PNG_ROOT, DEFAULT_ALL_IMAGE_MAP)
    print(f"完成，已复制 {copied} 个图片到: {DEFAULT_ALL_IMAGE_PNG_ROOT}")
    print(f"映射已写入: {DEFAULT_ALL_IMAGE_MAP}")
    print(f"修改后的图片请放到: {DEFAULT_EDITED_IMAGE_ROOT}")
    print(f"需要按图片屏蔽对象时，请把图片放到: {DEFAULT_BLOCK_IMAGE_ROOT}")


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


def _normalized_object_asset_key(value: object) -> str:
    return str(value or "").replace("\\", "/").strip("/").casefold()


def _mapped_pointer_scope(
    scope_by_asset_key: dict[str, dict],
    scopes_by_bundle_entry: dict[str, list[dict]],
    target_asset_key: object,
) -> tuple[dict | None, bool]:
    """Resolve a FileID target, falling back to a globally unique CAB entry."""
    normalized_target = _normalized_object_asset_key(target_asset_key)
    target_scope = scope_by_asset_key.get(normalized_target)
    if target_scope is not None:
        return target_scope, False
    bundle_entry = normalized_target.rsplit("/", 1)[-1]
    candidates = scopes_by_bundle_entry.get(bundle_entry, [])
    if bundle_entry and len(candidates) == 1:
        return candidates[0], True
    return None, False


def _build_object_graph() -> tuple[dict, dict]:
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
        assets_files = _manifest_value(manifest, "AssetsFiles", "assetsFiles", default=[])
        relative_base_by_bundle: dict[str, str] = {}
        if isinstance(assets_files, list):
            for assets_file in assets_files:
                if not isinstance(assets_file, dict):
                    continue
                entry_name = str(
                    _manifest_value(
                        assets_file,
                        "BundleEntryName",
                        "bundleEntryName",
                        default="",
                    )
                )
                relative_base_by_bundle[entry_name] = str(
                    _manifest_value(
                        assets_file,
                        "RelativeBase",
                        "relativeBase",
                        default="",
                    )
                )
        for item in items:
            if not isinstance(item, dict):
                continue
            type_name = str(_manifest_value(item, "TypeName", "typeName", default=""))
            if type_name not in {
                "Texture2D", "Sprite", "SpriteAtlas", "Material", "GameObject", "Transform",
                "RectTransform", "SpriteRenderer", "MonoBehaviour",
                "Mesh", "MeshFilter", "SkinnedMeshRenderer",
            }:
                continue
            relative_path = str(_manifest_value(item, "RelativePath", "relativePath", default=""))
            path_id = int(_manifest_value(item, "PathId", "PathID", "pathId", default=0) or 0)
            bundle_entry = str(_manifest_value(item, "BundleEntryName", "bundleEntryName", default=""))
            scope_key = (str(manifest_path), bundle_entry)
            relative_base = relative_base_by_bundle.get(bundle_entry, "")
            try:
                asset_key = (
                    manifest_path.parent / Path(relative_base)
                ).relative_to(DEFAULT_SOURCE_ROOT).as_posix().lower()
            except ValueError:
                asset_key = relative_base.replace("\\", "/").lower()
            scope = scopes.setdefault(
                scope_key,
                {
                    "scope_key": scope_key,
                    "manifest": manifest_path,
                    "source": str(_manifest_value(manifest, "SourceRelativePath", default="")),
                    "bundle_entry": bundle_entry,
                    "asset_key": asset_key,
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

    scope_by_asset_key = {
        _normalized_object_asset_key(scope.get("asset_key", "")): scope
        for scope in scopes.values()
        if scope.get("asset_key")
    }
    scopes_by_bundle_entry: dict[str, list[dict]] = {}
    for scope in scopes.values():
        bundle_entry = _normalized_object_asset_key(scope.get("bundle_entry", ""))
        if bundle_entry:
            scopes_by_bundle_entry.setdefault(bundle_entry, []).append(scope)
    file_id_map = _safe_read_json(DEFAULT_FILE_ID_MAP)
    normalized_file_id_map = {
        _normalized_object_asset_key(key): value
        for key, value in file_id_map.items()
    } if isinstance(file_id_map, dict) else {}
    resolved_external_count = 0
    fallback_external_count = 0
    for scope in scopes.values():
        pointer_scopes: dict[int, dict] = {0: scope}
        record = normalized_file_id_map.get(
            _normalized_object_asset_key(scope.get("asset_key", ""))
        )
        file_ids = record.get("file_ids") if isinstance(record, dict) else None
        if isinstance(file_ids, dict):
            for file_id_raw, target_asset_key in file_ids.items():
                try:
                    file_id = int(file_id_raw)
                except (TypeError, ValueError):
                    continue
                target_scope, used_fallback = _mapped_pointer_scope(
                    scope_by_asset_key,
                    scopes_by_bundle_entry,
                    target_asset_key,
                )
                if target_scope is not None:
                    pointer_scopes[file_id] = target_scope
                    if file_id != 0:
                        resolved_external_count += 1
                        if used_fallback:
                            fallback_external_count += 1
        scope["pointer_scopes"] = pointer_scopes
    if resolved_external_count:
        fallback_note = (
            f"，其中按唯一 Bundle entry 回退: {fallback_external_count} 条"
            if fallback_external_count else ""
        )
        print(
            f"[对象索引] FileID 跨文件映射: {resolved_external_count} 条"
            f"{fallback_note}",
            flush=True,
        )
    return scopes, texture_by_relative_path


def _write_object_graph_cache() -> None:
    state = _OBJECT_GRAPH_CACHE_STATE
    if not isinstance(state, dict):
        return
    DEFAULT_OBJECT_GRAPH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = DEFAULT_OBJECT_GRAPH_CACHE.with_suffix(".tmp")
    try:
        with temp_path.open("wb") as stream:
            pickle.dump(state, stream, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(temp_path, DEFAULT_OBJECT_GRAPH_CACHE)
    except (OSError, pickle.PickleError, TypeError) as exc:
        print(f"[对象索引][缓存提示] 无法写入共享缓存: {exc}")
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _object_graph_cache_bucket(name: str) -> dict:
    state = _OBJECT_GRAPH_CACHE_STATE
    if not isinstance(state, dict):
        return {}
    derived = state.setdefault("derived", {})
    if not isinstance(derived, dict):
        derived = {}
        state["derived"] = derived
    bucket = derived.setdefault(name, {})
    if not isinstance(bucket, dict):
        bucket = {}
        derived[name] = bucket
    return bucket


def _load_object_graph() -> tuple[dict, dict]:
    global _OBJECT_GRAPH_CACHE_STATE

    snapshot = _object_manifest_snapshot()
    if (
        isinstance(_OBJECT_GRAPH_CACHE_STATE, dict)
        and _OBJECT_GRAPH_CACHE_STATE.get("version") == OBJECT_GRAPH_CACHE_VERSION
        and _OBJECT_GRAPH_CACHE_STATE.get("snapshot") == snapshot
    ):
        print("[对象索引] 复用当前工具进程中的共享对象图缓存。")
        return (
            _OBJECT_GRAPH_CACHE_STATE["scopes"],
            _OBJECT_GRAPH_CACHE_STATE["textures"],
        )

    if DEFAULT_OBJECT_GRAPH_CACHE.is_file():
        try:
            with DEFAULT_OBJECT_GRAPH_CACHE.open("rb") as stream:
                cached = pickle.load(stream)
            if (
                isinstance(cached, dict)
                and cached.get("version") == OBJECT_GRAPH_CACHE_VERSION
                and cached.get("snapshot") == snapshot
                and isinstance(cached.get("scopes"), dict)
                and isinstance(cached.get("textures"), dict)
            ):
                _OBJECT_GRAPH_CACHE_STATE = cached
                print(f"[对象索引] 已复用磁盘共享缓存: {DEFAULT_OBJECT_GRAPH_CACHE}")
                return cached["scopes"], cached["textures"]
            print("[对象索引] manifest/FileID 映射已变化，共享缓存自动失效。")
        except (OSError, EOFError, pickle.PickleError, AttributeError, ValueError) as exc:
            print(f"[对象索引][缓存提示] 共享缓存无法读取，将重新构建: {exc}")

    scopes, textures = _build_object_graph()
    _OBJECT_GRAPH_CACHE_STATE = {
        "version": OBJECT_GRAPH_CACHE_VERSION,
        "snapshot": snapshot,
        "scopes": scopes,
        "textures": textures,
        "derived": {},
    }
    _write_object_graph_cache()
    print(f"[对象索引] 共享缓存已写入: {DEFAULT_OBJECT_GRAPH_CACHE}")
    return scopes, textures


def _resolve_pointer_scope(scope: dict, file_id: int) -> dict | None:
    pointer_scopes = scope.get("pointer_scopes")
    if isinstance(pointer_scopes, dict):
        resolved = pointer_scopes.get(file_id)
        if isinstance(resolved, dict):
            return resolved
    return scope if file_id == 0 else None


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


def _sprite_texture_path_id(sprite_data: dict, scope: dict | None = None) -> int:
    render_data = _sprite_render_data(sprite_data, scope)
    return _pptr(render_data.get("texture"))[1] or _pptr(render_data.get("m_Texture"))[1]


def _sprite_render_data(sprite_data: dict, scope: dict | None = None) -> dict:
    """Resolve Sprite render data, preferring its packed SpriteAtlas entry."""
    return _sprite_render_data_with_scope(sprite_data, scope)[0]


def _sprite_render_data_with_scope(
    sprite_data: dict,
    scope: dict | None = None,
) -> tuple[dict, dict | None]:
    """Return Sprite render data together with the scope owning its pointers."""
    if scope is not None:
        file_id, atlas_path_id = _pptr(sprite_data.get("m_SpriteAtlas"))
        render_key = sprite_data.get("m_RenderDataKey")
        atlas_scope = _resolve_pointer_scope(scope, file_id)
        if atlas_scope is not None and atlas_path_id and isinstance(render_key, dict):
            atlas_entry = _scope_entry(atlas_scope, ("SpriteAtlas",), atlas_path_id)
            atlas_data = _entry_data(atlas_entry) if atlas_entry else None
            if isinstance(atlas_data, dict):
                render_map = atlas_data.get("m_RenderDataMap")
                if isinstance(render_map, dict):
                    render_map = render_map.get("Array")
                if isinstance(render_map, list):
                    for pair in render_map:
                        if not isinstance(pair, dict) or pair.get("first") != render_key:
                            continue
                        packed_data = pair.get("second")
                        if isinstance(packed_data, dict):
                            return packed_data, atlas_scope
    value = sprite_data.get("m_RD")
    if not isinstance(value, dict):
        value = sprite_data.get("m_RenderData")
    return (value if isinstance(value, dict) else {}), scope


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ngui_atlas_sprite_rows(data: dict | None) -> list[dict]:
    """Return structurally valid NGUI UIAtlas sprite records."""
    if not isinstance(data, dict):
        return []
    rows = data.get("mSprites")
    if isinstance(rows, dict):
        rows = rows.get("Array")
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and all(key in row for key in ("x", "y", "width", "height"))
    ]


def _material_main_texture(material_data: dict | None) -> tuple[int, int]:
    if not isinstance(material_data, dict):
        return (0, 0)
    properties = material_data.get("m_SavedProperties")
    tex_envs = properties.get("m_TexEnvs") if isinstance(properties, dict) else None
    if isinstance(tex_envs, dict):
        tex_envs = tex_envs.get("Array")
    if not isinstance(tex_envs, list):
        return (0, 0)
    fallback = (0, 0)
    for pair in tex_envs:
        if not isinstance(pair, dict):
            continue
        value = pair.get("second")
        pointer = _pptr(value.get("m_Texture")) if isinstance(value, dict) else (0, 0)
        if pointer[1] and not fallback[1]:
            fallback = pointer
        if pair.get("first") == "_MainTex":
            return pointer
    return fallback


def _resolve_ngui_atlas(scope: dict, atlas_path_id: int) -> dict | None:
    """Resolve an NGUI UIAtlas, including mReplacement -> Material -> Texture2D."""
    current_scope = scope
    current_path_id = atlas_path_id
    visited: set[tuple[object, int]] = set()
    sprite_rows: list[dict] = []
    material_pointer: tuple[int, int] = (0, 0)
    material_origin_scope = current_scope
    atlas_chain: list[tuple[dict, int]] = []
    for _ in range(16):
        scope_key = current_scope.get("scope_key") or id(current_scope)
        visit_key = (scope_key, current_path_id)
        if visit_key in visited:
            break
        visited.add(visit_key)
        entry = _scope_entry(current_scope, ("MonoBehaviour",), current_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict):
            break
        rows = _ngui_atlas_sprite_rows(data)
        if rows and not sprite_rows:
            sprite_rows = rows
        pointer = _pptr(data.get("material") or data.get("mMaterial"))
        if pointer[1]:
            material_pointer = pointer
            material_origin_scope = current_scope
        atlas_chain.append((current_scope, current_path_id))
        replacement_file_id, replacement_path_id = _pptr(data.get("mReplacement"))
        if not replacement_path_id:
            break
        replacement_scope = _resolve_pointer_scope(current_scope, replacement_file_id)
        if replacement_scope is None:
            break
        current_scope = replacement_scope
        current_path_id = replacement_path_id

    material_file_id, material_path_id = material_pointer
    material_scope = _resolve_pointer_scope(material_origin_scope, material_file_id)
    material_entry = (
        _scope_entry(material_scope, ("Material",), material_path_id)
        if material_scope is not None and material_path_id else None
    )
    material_data = _entry_data(material_entry) if material_entry else None
    texture_file_id, texture_path_id = _material_main_texture(material_data)
    texture_scope = (
        _resolve_pointer_scope(material_scope, texture_file_id)
        if material_scope is not None else None
    )
    texture_entry = (
        _scope_entry(texture_scope, ("Texture2D",), texture_path_id)
        if texture_scope is not None and texture_path_id else None
    )
    if not sprite_rows or texture_entry is None:
        return None
    return {
        "sprites": sprite_rows,
        "texture_scope": texture_scope,
        "texture_path_id": texture_path_id,
        "texture_entry": texture_entry,
        "atlas_chain": atlas_chain,
    }


def run_split_sprite_atlases() -> None:
    try:
        from PIL import Image
    except ImportError:
        print("[图集拆分][错误] 当前 Python 环境缺少 Pillow。")
        return

    print()
    print("拆分 Sprite / NGUI UIAtlas 图集")
    print("说明: 按 Unity Sprite textureRect 或 NGUI UIAtlas mSprites 裁出独立 PNG。")
    scopes, _ = _load_object_graph()
    if DEFAULT_ALL_SPRITE_ROOT.exists():
        shutil.rmtree(DEFAULT_ALL_SPRITE_ROOT)
    png_root = DEFAULT_ALL_SPRITE_ROOT / "PNG"
    png_root.mkdir(parents=True, exist_ok=True)
    mapping: list[dict] = []
    atlas_texture_paths: set[str] = set()
    skipped = 0
    packed_atlas_sprites = 0
    ngui_sprite_count = 0

    for scope_key, scope in scopes.items():
        for (type_name, sprite_path_id), sprite_entry in scope["items"].items():
            if type_name != "Sprite":
                continue
            sprite_data = _entry_data(sprite_entry)
            if not sprite_data:
                skipped += 1
                continue
            own_render_data = _sprite_render_data(sprite_data)
            render_data, render_scope = _sprite_render_data_with_scope(
                sprite_data, scope
            )
            render_data_source = (
                "sprite_atlas" if render_data is not own_render_data else "sprite"
            )
            file_id, texture_path_id = _pptr(
                render_data.get("texture") or render_data.get("m_Texture")
            )
            texture_scope = (
                _resolve_pointer_scope(render_scope, file_id)
                if render_scope is not None else None
            )
            if texture_scope is None or not texture_path_id:
                skipped += 1
                continue
            texture_entry = _scope_entry(
                texture_scope, ("Texture2D",), texture_path_id
            )
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
                    "render_data_source": render_data_source,
                }
            )
            if render_data_source == "sprite_atlas":
                packed_atlas_sprites += 1
            if len(mapping) == 1 or len(mapping) % 200 == 0:
                print(f"[图集拆分] 已输出: {len(mapping)}，跳过: {skipped}", flush=True)

    # NGUI does not create Unity Sprite assets. UIAtlas stores top-left based
    # rectangles in mSprites and UISprite refers to them by mAtlas+mSpriteName.
    ngui_groups: dict[tuple[str, str, int, int, int, int], dict] = {}
    for scope_key, scope in scopes.items():
        for (type_name, atlas_path_id), atlas_entry in scope["items"].items():
            if type_name != "MonoBehaviour":
                continue
            atlas_data = _entry_data(atlas_entry)
            if not _ngui_atlas_sprite_rows(atlas_data):
                continue
            resolved = _resolve_ngui_atlas(scope, atlas_path_id)
            if not resolved:
                skipped += 1
                continue
            texture_entry = resolved["texture_entry"]
            texture_path = texture_entry.get("path")
            if not isinstance(texture_path, Path) or not texture_path.is_file():
                skipped += 1
                continue
            texture_scope = resolved["texture_scope"]
            texture_path_id = int(resolved["texture_path_id"])
            atlas_reference = {
                "source_resource": str(scope.get("source", "")),
                "bundle_entry": str(scope.get("bundle_entry", "")),
                "atlas_path_id": atlas_path_id,
            }
            for row in resolved["sprites"]:
                name = str(row.get("name", "")).strip()
                x = round(_number(row.get("x")))
                y = round(_number(row.get("y")))
                width = round(_number(row.get("width")))
                height = round(_number(row.get("height")))
                if not name or width <= 0 or height <= 0:
                    skipped += 1
                    continue
                group_key = (
                    str(texture_path.resolve()).lower(), name.casefold(), x, y, width, height
                )
                group = ngui_groups.setdefault(
                    group_key,
                    {
                        "name": name,
                        "x": x,
                        "y": y,
                        "width": width,
                        "height": height,
                        "row": row,
                        "texture_path": texture_path,
                        "texture_scope": texture_scope,
                        "texture_path_id": texture_path_id,
                        "atlas_references": [],
                    },
                )
                if atlas_reference not in group["atlas_references"]:
                    group["atlas_references"].append(atlas_reference)

    for group in ngui_groups.values():
        texture_path = group["texture_path"]
        x = group["x"]
        y = group["y"]
        width = group["width"]
        height = group["height"]
        try:
            with Image.open(texture_path) as atlas:
                if x < 0 or y < 0 or x + width > atlas.width or y + height > atlas.height:
                    raise ValueError("mSprites 矩形超出图集范围")
                cropped = atlas.crop((x, y, x + width, y + height))
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", group["name"])
            first_reference = group["atlas_references"][0]
            target = make_flat_unique_path(
                png_root,
                f"{safe_name}_NGUI_{first_reference['atlas_path_id']}.png",
            )
            cropped.save(target, "PNG")
        except Exception as exc:
            skipped += 1
            print(
                f"[图集拆分][跳过] NGUI Sprite={group['name']}: {exc}"
            )
            continue
        try:
            atlas_texture_paths.add(
                texture_path.relative_to(DEFAULT_SOURCE_ROOT).as_posix().lower()
            )
        except ValueError:
            pass
        row = group["row"]
        mapping.append(
            {
                "item_type": "ngui_sprite",
                "flat_name": target.name,
                "sprite_name": group["name"],
                "source_resource": first_reference["source_resource"],
                "bundle_entry": first_reference["bundle_entry"],
                "atlas_path_id": first_reference["atlas_path_id"],
                "atlas_references": group["atlas_references"],
                "texture_path_id": group["texture_path_id"],
                "texture_png": str(texture_path),
                "rect": {"x": x, "y": y, "width": width, "height": height},
                "coordinate_origin": "top_left",
                "border": {
                    key: int(row.get(key, 0) or 0)
                    for key in ("borderLeft", "borderRight", "borderTop", "borderBottom")
                },
            }
        )
        ngui_sprite_count += 1
        if ngui_sprite_count == 1 or ngui_sprite_count % 200 == 0:
            print(
                f"[图集拆分] 已输出 NGUI 子图: {ngui_sprite_count}，跳过: {skipped}",
                flush=True,
            )

    copied_regular = 0
    unresolved_runtime_atlases: list[str] = []
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
        original_name = str(item.get("original_name", flat_name))
        if original_name.lower().startswith("sactx-"):
            unresolved_runtime_atlases.append(original_name)

    DEFAULT_ALL_SPRITE_MAP.write_text(
        json.dumps({"items": mapping}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    sprite_count = sum(1 for item in mapping if item.get("item_type") == "sprite")
    print(
        f"[图集拆分][完成] Unity Sprite={sprite_count}，NGUI Sprite={ngui_sprite_count}，"
        f"其中 SpriteAtlas={packed_atlas_sprites}，"
        f"普通图片={copied_regular}，跳过={skipped}"
    )
    if unresolved_runtime_atlases:
        print(
            f"[图集拆分][提示] 仍有 {len(unresolved_runtime_atlases)} 张 sactx 运行时图集"
            "缺少 SpriteAtlas 矩形数据，已保留整图。请重新一键导出对象索引后再次拆分。"
        )
        for name in unresolved_runtime_atlases[:10]:
            print(f"  未拆分: {name}")
        if len(unresolved_runtime_atlases) > 10:
            print(f"  ... 其余 {len(unresolved_runtime_atlases) - 10} 张")
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


def _find_game_object_name_candidates(
    scopes: dict,
    query: str,
    source_filter: str = "",
) -> tuple[list[dict], str]:
    raw_query = query.strip()
    force_contains = "*" in raw_query
    needle = raw_query.replace("*", "").strip().casefold()
    if not needle:
        return ([], "contains" if force_contains else "exact")
    filter_text = source_filter.strip().casefold()
    exact: list[dict] = []
    contains: list[dict] = []
    for scope_key, scope in scopes.items():
        searchable_scope = " ".join(
            str(scope.get(key, ""))
            for key in ("source", "bundle_entry", "asset_key")
        ).casefold()
        if filter_text and filter_text not in searchable_scope:
            continue
        for (type_name, path_id), entry in scope.get("items", {}).items():
            if type_name != "GameObject" or not isinstance(entry, dict):
                continue
            item = entry.get("item")
            name = str(
                _manifest_value(item, "AssetName", "assetName", default="")
                if isinstance(item, dict)
                else ""
            ).strip()
            if not name:
                data = _entry_data(entry)
                name = str(data.get("m_Name", "")) if isinstance(data, dict) else ""
            folded_name = name.casefold()
            if needle not in folded_name:
                continue
            candidate = {
                "scope_key": scope_key,
                "scope": scope,
                "source": str(scope.get("source", "")),
                "bundle_entry": str(scope.get("bundle_entry", "")),
                "path_id": int(path_id),
                "name": name,
            }
            contains.append(candidate)
            if folded_name == needle:
                exact.append(candidate)
    selected = contains if force_contains or not exact else exact
    selected.sort(
        key=lambda row: (
            str(row.get("source", "")).casefold(),
            str(row.get("bundle_entry", "")).casefold(),
            str(row.get("name", "")).casefold(),
            int(row.get("path_id", 0) or 0),
        )
    )
    return (selected, "contains" if force_contains or not exact else "exact")


def _cached_game_object_name_candidates(
    scopes: dict,
    query: str,
    source_filter: str = "",
) -> tuple[list[dict], str]:
    cache = _object_graph_cache_bucket("object_name_queries")
    cache_key = json.dumps(
        [query.strip().casefold(), source_filter.strip().casefold()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("candidates"), list):
        print(f"[名称屏蔽] 复用名称链路缓存: {query}")
        return cached["candidates"], str(cached.get("match_mode", "exact"))
    candidates, match_mode = _find_game_object_name_candidates(scopes, query, source_filter)
    cache[cache_key] = {"candidates": candidates, "match_mode": match_mode}
    _write_object_graph_cache()
    return candidates, match_mode


def _game_object_name_match(candidate: dict) -> dict:
    scope = candidate["scope"]
    path_id = int(candidate.get("path_id", 0) or 0)
    return {
        "scope_key": candidate.get("scope_key"),
        "scope": scope,
        "source": str(scope.get("source", "")),
        "bundle_entry": str(scope.get("bundle_entry", "")),
        "component_type": "GameObject",
        "component_path_id": path_id,
        "chain": _object_chain_direct(scope, path_id),
    }


def _selected_allpng_items() -> list[dict]:
    available: dict[str, dict] = {}
    try:
        items = load_allpng_map(DEFAULT_ALL_IMAGE_MAP)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        items = []
        print(f"[屏蔽对象][提示] 普通图片映射不可用，将继续读取拆分图集: {exc}")
    for item in items:
        flat_name = str(item.get("flat_name", "")).strip()
        if flat_name:
            available[flat_name.lower()] = {
                **item,
                "_selection_kind": "texture",
                "_selection_image": str(DEFAULT_ALL_IMAGE_PNG_ROOT / flat_name),
            }

    sprite_count = 0
    ngui_sprite_count = 0
    sprite_map = _safe_read_json(DEFAULT_ALL_SPRITE_MAP)
    if isinstance(sprite_map, dict) and isinstance(sprite_map.get("items"), list):
        for item in sprite_map["items"]:
            if isinstance(item, dict):
                flat_name = str(item.get("flat_name", "")).strip()
                if not flat_name:
                    continue
                selection_kind = str(item.get("item_type", "sprite"))
                if selection_kind == "sprite":
                    sprite_count += 1
                elif selection_kind == "ngui_sprite":
                    ngui_sprite_count += 1
                available[flat_name.lower()] = {
                    **item,
                    "_selection_kind": selection_kind,
                    "_selection_image": str(DEFAULT_ALL_SPRITE_PNG_ROOT / flat_name),
                }
    elif DEFAULT_ALL_SPRITE_PNG_ROOT.is_dir():
        print(
            f"[屏蔽对象][提示] 找到了拆分图片目录，但缺少映射文件: "
            f"{DEFAULT_ALL_SPRITE_MAP}。请重新执行工具脚本主菜单 2。"
        )

    if sprite_count:
        print(f"[屏蔽对象] 已加载拆分图集 Sprite: {sprite_count} 个")
    if ngui_sprite_count:
        print(f"[屏蔽对象] 已加载拆分 NGUI Sprite: {ngui_sprite_count} 个")

    print("图片来源:")
    print("  1. 手动输入图片名称或路径")
    print(f"  2. 从 {DEFAULT_BLOCK_IMAGE_ROOT} 自动读取")
    print("  q. 返回")
    while True:
        source_choice = prompt_input("请选择图片来源: ").strip().lower()
        if source_choice in {"q", "quit", "exit"}:
            return []
        if source_choice in {"1", "2"}:
            break
        print("[屏蔽对象][错误] 请输入 1、2 或 q。")

    selected_names: list[str] = []
    if source_choice == "2":
        if DEFAULT_BLOCK_IMAGE_ROOT.is_dir():
            selected_names = sorted(
                path.name
                for path in DEFAULT_BLOCK_IMAGE_ROOT.iterdir()
                if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
            )
        if selected_names:
            print(f"[屏蔽对象] 从 {DEFAULT_BLOCK_IMAGE_ROOT} 读取指定图片: {len(selected_names)} 个")
        else:
            print(f"[屏蔽对象] 批量目录中没有支持的图片: {DEFAULT_BLOCK_IMAGE_ROOT}")
            return []
    else:
        raw = prompt_input(
            "请输入普通图片或拆分 Sprite/NGUI 图片名称/路径，多个值用逗号分隔: "
        ).strip()
        selected_names = [name.strip() for name in re.split(r"[,，]", raw) if name.strip()]

    def selection_key(value: str) -> str:
        # 支持直接粘贴或拖入 AllPNG/PNG、AllPNG/Sprite/PNG 下的完整路径。
        cleaned = value.strip().strip('"').strip("'")
        return Path(cleaned.replace("\\", "/")).name.lower()

    missing = [name for name in selected_names if selection_key(name) not in available]
    for name in missing:
        print(f"[屏蔽对象][未找到] {name}")
    if missing and not (sprite_count or ngui_sprite_count):
        print(
            "[屏蔽对象][提示] 若目标来自图集，请先执行主菜单 2“按 Sprite / "
            "NGUI UIAtlas 数据拆分 Texture2D 图集”，再输入 Sprite/PNG 中的子图名称。"
        )
    return [
        available[selection_key(name)]
        for name in selected_names
        if selection_key(name) in available
    ]


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
    file_id_map_signature: dict[str, object] = {"exists": False}
    try:
        file_id_stat = DEFAULT_FILE_ID_MAP.stat()
        file_id_map_signature = {
            "exists": True,
            "size": file_id_stat.st_size,
            "mtime_ns": file_id_stat.st_mtime_ns,
        }
    except OSError:
        pass
    return {
        "source_root": str(DEFAULT_SOURCE_ROOT.resolve()),
        "manifest_count": count,
        "manifest_total_size": total_size,
        "latest_mtime_ns": latest_mtime_ns,
        "fingerprint": digest.hexdigest(),
        "file_id_map": file_id_map_signature,
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


def _prefilter_reference_entries_across_scopes(
    scopes: dict,
    type_names: set[str],
    path_ids: set[int],
) -> list[tuple[tuple[str, str], dict, str, int, dict]]:
    """Search all component scopes in one rg invocation for cross-file PPtrs."""
    if not path_ids:
        return []
    entries = [
        (scope_key, scope, type_name, path_id, entry)
        for scope_key, scope in scopes.items()
        for (type_name, path_id), entry in scope["items"].items()
        if type_name in type_names
    ]
    if not entries:
        return []
    entry_by_path = {
        str(entry[4]["path"].resolve()).lower(): entry
        for entry in entries
    }
    search_dirs = sorted({str(entry[4]["path"].parent) for entry in entries})
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
        worker_count = _search_worker_count(len(entries))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            flags = executor.map(
                lambda item: _entry_contains_any_path_id(item[4], path_ids),
                entries,
            )
            return [item for item, matched in zip(entries, flags) if matched]


def _find_image_object_matches(
    scopes: dict,
    texture_targets: set[tuple[tuple[str, str], int]],
    sprite_targets: set[tuple[tuple[str, str], int]],
    ngui_sprite_targets: set[tuple[tuple[str, str], int, str]] | None = None,
) -> list[dict]:
    matches: list[dict] = []
    ngui_sprite_targets = ngui_sprite_targets or set()
    resolved_sprite_targets = set(sprite_targets)
    sprite_total = 0
    for scope_key, scope in scopes.items():
        for (type_name, sprite_id), entry in scope["items"].items():
            if type_name != "Sprite":
                continue
            sprite_total += 1
            sprite_key = (scope_key, sprite_id)
            if sprite_key in resolved_sprite_targets:
                continue
            sprite_data = _entry_data(entry)
            if not sprite_data:
                continue
            render_data = _sprite_render_data(sprite_data, scope)
            texture_file_id, texture_path_id = _pptr(
                render_data.get("texture") or render_data.get("m_Texture")
            )
            texture_scope = _resolve_pointer_scope(scope, texture_file_id)
            if texture_scope is None:
                continue
            texture_key = (texture_scope.get("scope_key"), texture_path_id)
            if texture_key in texture_targets:
                resolved_sprite_targets.add(sprite_key)

    sprite_path_ids = {path_id for _scope_key, path_id in resolved_sprite_targets}
    print(
        f"[对象索引] Sprite 关系: 扫描={sprite_total}，目标={len(resolved_sprite_targets)}",
        flush=True,
    )
    total_components = sum(
        1
        for scope in scopes.values()
        for type_name, _path_id in scope["items"]
        if type_name in {"MonoBehaviour", "SpriteRenderer"}
    )
    component_candidates = (
        _prefilter_reference_entries_across_scopes(
            scopes,
            {"MonoBehaviour", "SpriteRenderer"},
            sprite_path_ids,
        )
        if sprite_path_ids else []
    )
    checked_components = len(component_candidates)
    cross_file_matches = 0
    chain_caches: dict[tuple[str, str], dict[int, list[dict]]] = {}
    for scope_key, scope, type_name, component_id, entry in component_candidates:
        data = _entry_data(entry)
        if not data:
            continue
        sprite_file_id, sprite_id = _pptr(data.get("m_Sprite"))
        sprite_scope = _resolve_pointer_scope(scope, sprite_file_id)
        _, game_object_id = _pptr(data.get("m_GameObject"))
        if sprite_scope is None or not game_object_id:
            continue
        sprite_scope_key = sprite_scope.get("scope_key")
        if (sprite_scope_key, sprite_id) not in resolved_sprite_targets:
            continue
        sprite_entry = _scope_entry(sprite_scope, ("Sprite",), sprite_id)
        sprite_data = _entry_data(sprite_entry) if sprite_entry else None
        chain_cache = chain_caches.setdefault(scope_key, {})
        chain = chain_cache.get(game_object_id)
        if chain is None:
            chain = _object_chain_direct(scope, game_object_id)
            chain_cache[game_object_id] = chain
        if sprite_file_id != 0:
            cross_file_matches += 1
        matches.append(
            {
                "scope_key": scope_key,
                "scope": scope,
                "source": scope["source"],
                "bundle_entry": scope["bundle_entry"],
                "component_type": type_name,
                "component_path_id": component_id,
                "sprite_path_id": sprite_id,
                "sprite_source": sprite_scope.get("source", ""),
                "sprite_bundle_entry": sprite_scope.get("bundle_entry", ""),
                "texture_path_id": _sprite_texture_path_id(sprite_data or {}, sprite_scope),
                "chain": chain,
            }
        )
    print(
        f"[对象索引] 组件预筛选: {total_components} -> {checked_components}；"
        f"匹配={len(matches)}，其中跨文件={cross_file_matches}",
        flush=True,
    )

    if texture_targets:
        texture_path_ids = {
            texture_path_id for _scope_key, texture_path_id in texture_targets
        }
        texture_candidates = _prefilter_reference_entries_across_scopes(
            scopes,
            {"MonoBehaviour"},
            texture_path_ids,
        )
        direct_texture_matches = 0
        direct_seen: set[tuple[tuple[str, str], int, tuple[str, str], int]] = set()
        for scope_key, scope, type_name, component_id, entry in texture_candidates:
            data = _entry_data(entry)
            if not data:
                continue
            matched_texture: tuple[dict, int, str] | None = None
            for field_name in ("mTexture", "m_Texture"):
                texture_file_id, texture_path_id = _pptr(data.get(field_name))
                texture_scope = _resolve_pointer_scope(scope, texture_file_id)
                if texture_scope is None or not texture_path_id:
                    continue
                if (texture_scope.get("scope_key"), texture_path_id) in texture_targets:
                    matched_texture = (texture_scope, texture_path_id, field_name)
                    break
            _, game_object_id = _pptr(data.get("m_GameObject"))
            if matched_texture is None or not game_object_id:
                continue
            texture_scope, texture_path_id, field_name = matched_texture
            seen_key = (
                scope_key,
                component_id,
                texture_scope.get("scope_key"),
                texture_path_id,
            )
            if seen_key in direct_seen:
                continue
            direct_seen.add(seen_key)
            chain_cache = chain_caches.setdefault(scope_key, {})
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
                    "component_type": (
                        "NGUI UITexture" if field_name == "mTexture" else type_name
                    ),
                    "component_path_id": component_id,
                    "sprite_path_id": 0,
                    "texture_path_id": texture_path_id,
                    "texture_source": texture_scope.get("source", ""),
                    "texture_bundle_entry": texture_scope.get("bundle_entry", ""),
                    "texture_reference_field": field_name,
                    "chain": chain,
                }
            )
            direct_texture_matches += 1
        print(
            f"[对象索引] Texture2D 直连预筛选={len(texture_candidates)}，"
            f"匹配={direct_texture_matches}",
            flush=True,
        )

    if ngui_sprite_targets:
        atlas_path_ids = {
            atlas_path_id for _scope_key, atlas_path_id, _sprite_name in ngui_sprite_targets
        }
        ngui_candidates = _prefilter_reference_entries_across_scopes(
            scopes,
            {"MonoBehaviour"},
            atlas_path_ids,
        )
        ngui_matches = 0
        for scope_key, scope, _type_name, component_id, entry in ngui_candidates:
            data = _entry_data(entry)
            if not data:
                continue
            sprite_name = str(data.get("mSpriteName", "")).strip()
            atlas_file_id, atlas_path_id = _pptr(data.get("mAtlas"))
            atlas_scope = _resolve_pointer_scope(scope, atlas_file_id)
            _, game_object_id = _pptr(data.get("m_GameObject"))
            if atlas_scope is None or not atlas_path_id or not sprite_name or not game_object_id:
                continue
            target_key = (
                atlas_scope.get("scope_key"),
                atlas_path_id,
                sprite_name.casefold(),
            )
            if target_key not in ngui_sprite_targets:
                continue
            chain_cache = chain_caches.setdefault(scope_key, {})
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
                    "component_type": "NGUI UISprite",
                    "component_path_id": component_id,
                    "sprite_path_id": 0,
                    "texture_path_id": 0,
                    "ngui_atlas_path_id": atlas_path_id,
                    "ngui_sprite_name": sprite_name,
                    "chain": chain,
                }
            )
            ngui_matches += 1
        print(
            f"[对象索引] NGUI UISprite 预筛选={len(ngui_candidates)}，匹配={ngui_matches}",
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


def _cached_mesh_object_matches(
    scopes: dict,
    mesh_name: str,
    mesh_targets: set[tuple[tuple[str, str], int]],
) -> list[dict]:
    cache = _object_graph_cache_bucket("mesh_queries")
    cache_key = json.dumps(
        sorted(
            [str(scope_key[0]), str(scope_key[1]), int(path_id)]
            for scope_key, path_id in mesh_targets
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cached = cache.get(cache_key)
    if isinstance(cached, list):
        print(f"[Mesh索引] 已复用链路缓存: {mesh_name}，匹配={len(cached)}")
        return cached
    matches = _find_mesh_object_matches(scopes, mesh_targets)
    cache[cache_key] = matches
    _write_object_graph_cache()
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
        "sprite_path_id": match.get("sprite_path_id", 0),
        "texture_path_id": match.get("texture_path_id", 0),
        "ngui_atlas_path_id": match.get("ngui_atlas_path_id", 0),
        "ngui_sprite_name": match.get("ngui_sprite_name", ""),
        "chain": match["chain"],
    }


def _image_query_cache_key(selected: list[dict]) -> str:
    identities: list[dict] = []
    for item in selected:
        selection_kind = item.get("_selection_kind")
        if selection_kind == "sprite":
            identities.append(
                {
                    "kind": "sprite",
                    "source": str(item.get("source_resource", "")),
                    "bundle_entry": str(item.get("bundle_entry", "")),
                    "sprite_path_id": int(item.get("sprite_path_id", 0) or 0),
                }
            )
        elif selection_kind == "ngui_sprite":
            references = item.get("atlas_references")
            if not isinstance(references, list) or not references:
                references = [item]
            identities.append(
                {
                    "kind": "ngui_sprite",
                    "sprite_name": str(item.get("sprite_name", "")).casefold(),
                    "atlas_references": sorted(
                        [
                            {
                                "source": str(reference.get("source_resource", "")),
                                "bundle_entry": str(reference.get("bundle_entry", "")),
                                "atlas_path_id": int(reference.get("atlas_path_id", 0) or 0),
                            }
                            for reference in references
                            if isinstance(reference, dict)
                        ],
                        key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True),
                    ),
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
        and cached.get("version") == 8
        and cached.get("manifest_snapshot") == snapshot
        and isinstance(cached.get("queries"), dict)
    ):
        return cached
    if DEFAULT_IMAGE_OBJECT_INDEX.is_file():
        print("[对象索引] 导出 manifest 已变化或缓存格式已升级，清空旧索引。")
    return {
        "version": 8,
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
    blocked_rows = _blocked_object_rows(_load_block_records())
    for level, node in enumerate(match["chain"]):
        if _is_preview_node_blocked(node, blocked_rows, source, bundle_entry):
            print(
                f"      \033[91m{level}. [已屏蔽] {node['name']} "
                f"(PathID={node['path_id']})\033[0m"
            )
        else:
            print(f"      {level}. {node['name']} (PathID={node['path_id']})")


def _match_scope(scopes: dict, match: dict) -> dict | None:
    """Resolve both fresh and cached object matches back to their asset scope."""
    source = str(match.get("source", ""))
    bundle_entry = str(match.get("bundle_entry", ""))
    for scope in scopes.values():
        if scope.get("source") == source and scope.get("bundle_entry") == bundle_entry:
            return scope
    return None


def _array_value(value: object) -> list:
    if isinstance(value, dict):
        value = value.get("Array")
    return value if isinstance(value, list) else []


def _vec2(value: object, default: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(value, dict):
        return default
    return (_number(value.get("x"), default[0]), _number(value.get("y"), default[1]))


def _preview_root_world_scale(
    transform_data: dict,
    *,
    uses_ui_reference_size: bool = False,
) -> tuple[float, float]:
    """Return a usable root scale for a serialized UI hierarchy preview.

    Some inactive or not-yet-initialized Unity Canvas prefabs serialize their
    root RectTransform scale as (0, 0, 0).  Applying that runtime placeholder
    to UI coordinates collapses every descendant RectTransform to 1x1 in the
    static preview.  A preview rooted in UI reference pixels must treat that
    all-zero scale the same way as an NGUI UIRoot: as unit scale.
    """
    if uses_ui_reference_size:
        return (1.0, 1.0)
    return _preview_effective_local_scale(transform_data)


def _preview_effective_local_scale(transform_data: dict) -> tuple[float, float]:
    """Keep preview geometry inspectable when runtime animation serialized zero scale."""
    scale_x, scale_y = _vec2(transform_data.get("m_LocalScale"), (1.0, 1.0))
    if abs(scale_x) < 1e-6:
        scale_x = 1.0
    if abs(scale_y) < 1e-6:
        scale_y = 1.0
    return (scale_x, scale_y)


def _preview_transform_z_angle(transform_data: dict) -> float:
    rotation = transform_data.get("m_LocalRotation")
    if not isinstance(rotation, dict):
        return 0.0
    z = _number(rotation.get("z"))
    w = _number(rotation.get("w"), 1.0)
    if abs(z) < 1e-9:
        return 0.0
    return math.degrees(2.0 * math.atan2(z, w))


def _preview_rotated_rect(
    rect: tuple[float, float, float, float],
    angle: float,
) -> tuple[float, float, float, float]:
    if abs(angle) < 0.01:
        return rect
    x, y, width, height = rect
    radians = math.radians(angle)
    rotated_width = abs(width * math.cos(radians)) + abs(height * math.sin(radians))
    rotated_height = abs(width * math.sin(radians)) + abs(height * math.cos(radians))
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    return (
        center_x - rotated_width / 2.0,
        center_y - rotated_height / 2.0,
        max(1.0, rotated_width),
        max(1.0, rotated_height),
    )


def _preview_rotate_rect_position(
    rect: tuple[float, float, float, float],
    origin: tuple[float, float],
    angle: float,
) -> tuple[float, float, float, float]:
    """Rotate a child rectangle's center in its parent's oriented frame."""
    if abs(angle) < 0.01:
        return rect
    x, y, width, height = rect
    center_x = x + width / 2.0
    center_y = y + height / 2.0
    offset_x = center_x - origin[0]
    offset_y = center_y - origin[1]
    radians = math.radians(angle)
    rotated_x = offset_x * math.cos(radians) - offset_y * math.sin(radians)
    rotated_y = offset_x * math.sin(radians) + offset_y * math.cos(radians)
    return (
        origin[0] + rotated_x - width / 2.0,
        origin[1] + rotated_y - height / 2.0,
        width,
        height,
    )


def _preview_canvas_scale(
    logical_width: float,
    logical_height: float,
    viewport_width: float,
    viewport_height: float,
) -> float:
    """Choose detail from the UI reference while bounding total memory."""
    reference_max_side = max(logical_width, logical_height, 1.0)
    scale = min(1.0, 1600.0 / reference_max_side)
    scale = min(scale, 8192.0 / max(viewport_width, viewport_height, 1.0))
    viewport_pixels = max(1.0, viewport_width * viewport_height)
    return min(scale, math.sqrt(48_000_000.0 / viewport_pixels))


def _rect_transform_child_rect(
    transform: dict,
    parent_rect: tuple[float, float, float, float],
    parent_scale: tuple[float, float] = (1.0, 1.0),
) -> tuple[float, float, float, float]:
    """Approximate Unity RectTransform.GetWorldCorners in an unrotated parent."""
    px, py, parent_width, parent_height = parent_rect
    anchor_min = _vec2(transform.get("m_AnchorMin"), (0.5, 0.5))
    anchor_max = _vec2(transform.get("m_AnchorMax"), anchor_min)
    anchored = _vec2(
        transform.get("m_AnchoredPosition") or transform.get("m_LocalPosition"),
        (0.0, 0.0),
    )
    size_delta = _vec2(transform.get("m_SizeDelta"), (0.0, 0.0))
    pivot = _vec2(transform.get("m_Pivot"), (0.5, 0.5))
    width = (
        parent_width * (anchor_max[0] - anchor_min[0])
        + size_delta[0] * abs(parent_scale[0])
    )
    height = (
        parent_height * (anchor_max[1] - anchor_min[1])
        + size_delta[1] * abs(parent_scale[1])
    )
    local_scale = _preview_effective_local_scale(transform)
    width = max(1.0, abs(width * local_scale[0]))
    height = max(1.0, abs(height * local_scale[1]))
    anchor_x = anchor_min[0] * (1.0 - pivot[0]) + anchor_max[0] * pivot[0]
    anchor_y = anchor_min[1] * (1.0 - pivot[1]) + anchor_max[1] * pivot[1]
    x = (
        px + parent_width * anchor_x
        + anchored[0] * parent_scale[0] - pivot[0] * width
    )
    y = (
        py + parent_height * anchor_y
        + anchored[1] * parent_scale[1] - pivot[1] * height
    )
    return (x, y, width, height)


def _preview_aspect_fitted_rect(
    scope: dict,
    game_object_data: dict,
    transform_data: dict,
    rect: tuple[float, float, float, float],
) -> tuple[tuple[float, float, float, float], bool]:
    """Apply Unity AspectRatioFitter geometry before laying out descendants."""
    fitter_data = None
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if (
            isinstance(data, dict)
            and int(data.get("m_Enabled", 1) or 0) != 0
            and "m_AspectMode" in data
            and "m_AspectRatio" in data
        ):
            fitter_data = data
            break
    if fitter_data is None:
        return rect, False
    mode = int(fitter_data.get("m_AspectMode", 0) or 0)
    ratio = abs(_number(fitter_data.get("m_AspectRatio")))
    if mode == 0 or ratio <= 1e-6:
        return rect, False
    x, y, width, height = rect
    target_width, target_height = width, height
    if mode == 1:  # WidthControlsHeight
        target_height = width / ratio
    elif mode == 2:  # HeightControlsWidth
        target_width = height * ratio
    elif mode in (3, 4):  # FitInParent / EnvelopeParent
        current_ratio = width / max(height, 1e-6)
        use_width = (
            current_ratio <= ratio if mode == 3 else current_ratio >= ratio
        )
        if use_width:
            target_height = width / ratio
        else:
            target_width = height * ratio
    else:
        return rect, False
    pivot_x, pivot_y = _vec2(transform_data.get("m_Pivot"), (0.5, 0.5))
    pivot_point_x = x + width * pivot_x
    pivot_point_y = y + height * pivot_y
    return (
        (
            pivot_point_x - target_width * pivot_x,
            pivot_point_y - target_height * pivot_y,
            max(1.0, abs(target_width)),
            max(1.0, abs(target_height)),
        ),
        True,
    )


def _preview_layout_element_data(scope: dict, game_object_data: dict) -> dict | None:
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if (
            isinstance(data, dict)
            and int(data.get("m_Enabled", 1) or 0) != 0
            and "m_LayoutPriority" in data
        ):
            return data
    return None


def _preview_horizontal_layout_data(
    scope: dict,
    game_object_data: dict,
) -> dict | None:
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict) or int(data.get("m_Enabled", 1) or 0) == 0:
            continue
        if not {
            "m_ChildAlignment", "m_ChildControlWidth", "m_ChildControlHeight",
            "m_ChildForceExpandWidth", "m_ChildForceExpandHeight", "m_Spacing",
        }.issubset(data):
            continue
        _file_id, script_path_id = _pptr(data.get("m_Script"))
        if script_path_id in _UNITY_HORIZONTAL_LAYOUT_SCRIPT_PATH_IDS:
            return data
    return None


def _preview_vertical_layout_data(
    scope: dict,
    game_object_data: dict,
) -> dict | None:
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict) or int(data.get("m_Enabled", 1) or 0) == 0:
            continue
        if not {
            "m_ChildAlignment", "m_ChildControlWidth", "m_ChildControlHeight",
            "m_ChildForceExpandWidth", "m_ChildForceExpandHeight", "m_Spacing",
        }.issubset(data):
            continue
        _file_id, script_path_id = _pptr(data.get("m_Script"))
        if script_path_id in _UNITY_VERTICAL_LAYOUT_SCRIPT_PATH_IDS:
            return data
    return None


def _preview_horizontal_layout_child_rects(
    scope: dict,
    parent_game_object_data: dict,
    parent_rect: tuple[float, float, float, float],
    world_scale: tuple[float, float],
    child_nodes: list[tuple[int, dict]],
) -> tuple[dict[int, tuple[float, float, float, float]], bool]:
    """Evaluate the positional part of Unity HorizontalLayoutGroup."""
    layout = _preview_horizontal_layout_data(scope, parent_game_object_data)
    if layout is None or not child_nodes:
        return {}, False
    padding = layout.get("m_Padding")
    if not isinstance(padding, dict):
        padding = {}
    scale_x, scale_y = abs(world_scale[0]), abs(world_scale[1])
    padding_left = _number(padding.get("m_Left")) * scale_x
    padding_right = _number(padding.get("m_Right")) * scale_x
    padding_top = _number(padding.get("m_Top")) * scale_y
    padding_bottom = _number(padding.get("m_Bottom")) * scale_y
    spacing = _number(layout.get("m_Spacing")) * scale_x
    control_width = bool(layout.get("m_ChildControlWidth", False))
    control_height = bool(layout.get("m_ChildControlHeight", False))
    force_width = bool(layout.get("m_ChildForceExpandWidth", False))
    force_height = bool(layout.get("m_ChildForceExpandHeight", False))
    inner_width = max(0.0, parent_rect[2] - padding_left - padding_right)
    inner_height = max(0.0, parent_rect[3] - padding_top - padding_bottom)
    rows: list[dict] = []
    for object_id, transform_data in child_nodes:
        object_entry = _scope_entry(scope, ("GameObject",), object_id)
        object_data = _entry_data(object_entry) if object_entry else None
        if not isinstance(object_data, dict) or not bool(object_data.get("m_IsActive", True)):
            continue
        base_rect = _rect_transform_child_rect(transform_data, parent_rect, world_scale)
        layout_element = _preview_layout_element_data(scope, object_data)
        if layout_element and bool(layout_element.get("m_IgnoreLayout", False)):
            continue
        width, height = base_rect[2], base_rect[3]
        preferred_width = (
            _number(layout_element.get("m_PreferredWidth"), -1.0) * scale_x
            if layout_element else -1.0
        )
        preferred_height = (
            _number(layout_element.get("m_PreferredHeight"), -1.0) * scale_y
            if layout_element else -1.0
        )
        flexible_width = (
            _number(layout_element.get("m_FlexibleWidth"), -1.0)
            if layout_element else -1.0
        )
        if control_width and preferred_width >= 0:
            width = preferred_width
        if control_height:
            if force_height:
                height = inner_height
            elif preferred_height >= 0:
                height = min(inner_height, preferred_height)
        provisional = (base_rect[0], base_rect[1], max(1.0, width), max(1.0, height))
        provisional, _used_aspect = _preview_aspect_fitted_rect(
            scope, object_data, transform_data, provisional
        )
        rows.append(
            {
                "path_id": object_id,
                "transform": transform_data,
                "object_data": object_data,
                "width": provisional[2],
                "height": provisional[3],
                "flexible_width": max(
                    flexible_width,
                    1.0 if control_width and force_width else 0.0,
                ),
            }
        )
    if not rows:
        return {}, True
    total_spacing = spacing * max(0, len(rows) - 1)
    total_width = sum(row["width"] for row in rows) + total_spacing
    remaining = inner_width - total_width
    total_flexible = sum(row["flexible_width"] for row in rows)
    if control_width and remaining > 0 and total_flexible > 0:
        for row in rows:
            row["width"] += remaining * row["flexible_width"] / total_flexible
            fitted, _used_aspect = _preview_aspect_fitted_rect(
                scope,
                row["object_data"],
                row["transform"],
                (0.0, 0.0, row["width"], row["height"]),
            )
            row["width"], row["height"] = fitted[2], fitted[3]
        total_width = sum(row["width"] for row in rows) + total_spacing
    alignment = int(layout.get("m_ChildAlignment", 0) or 0)
    horizontal_alignment = alignment % 3
    vertical_alignment = max(0, min(2, alignment // 3))
    horizontal_surplus = inner_width - total_width
    cursor_x = (
        parent_rect[0] + padding_left
        + horizontal_surplus * (horizontal_alignment / 2.0)
    )
    ordered_rows = (
        list(reversed(rows))
        if bool(layout.get("m_ReverseArrangement", False)) else rows
    )
    result: dict[int, tuple[float, float, float, float]] = {}
    for row in ordered_rows:
        height = min(row["height"], inner_height) if control_height else row["height"]
        vertical_surplus = inner_height - height
        y = (
            parent_rect[1] + padding_bottom
            + vertical_surplus * ((2 - vertical_alignment) / 2.0)
        )
        result[int(row["path_id"])] = (
            cursor_x,
            y,
            max(1.0, row["width"]),
            max(1.0, height),
        )
        cursor_x += row["width"] + spacing
    return result, True


def _preview_vertical_layout_child_rects(
    scope: dict,
    parent_game_object_data: dict,
    parent_rect: tuple[float, float, float, float],
    world_scale: tuple[float, float],
    child_nodes: list[tuple[int, dict]],
) -> tuple[dict[int, tuple[float, float, float, float]], bool]:
    """Evaluate a stable Unity VerticalLayoutGroup from top to bottom.

    Some prefabs serialize mutually exclusive runtime states as overlapping
    children.  When their combined preferred height cannot fit the parent, the
    safer cross-axis-only fallback remains in use instead of inventing a stack.
    """
    layout = _preview_vertical_layout_data(scope, parent_game_object_data)
    if layout is None or not child_nodes:
        return {}, False
    padding = layout.get("m_Padding")
    if not isinstance(padding, dict):
        padding = {}
    scale_x, scale_y = abs(world_scale[0]), abs(world_scale[1])
    padding_left = _number(padding.get("m_Left")) * scale_x
    padding_right = _number(padding.get("m_Right")) * scale_x
    padding_top = _number(padding.get("m_Top")) * scale_y
    padding_bottom = _number(padding.get("m_Bottom")) * scale_y
    spacing = _number(layout.get("m_Spacing")) * scale_y
    control_width = bool(layout.get("m_ChildControlWidth", False))
    control_height = bool(layout.get("m_ChildControlHeight", False))
    force_width = bool(layout.get("m_ChildForceExpandWidth", False))
    force_height = bool(layout.get("m_ChildForceExpandHeight", False))
    inner_width = max(0.0, parent_rect[2] - padding_left - padding_right)
    inner_height = max(0.0, parent_rect[3] - padding_top - padding_bottom)
    if inner_height <= 2.0:
        return {}, False

    rows: list[dict] = []
    for object_id, transform_data in child_nodes:
        object_entry = _scope_entry(scope, ("GameObject",), object_id)
        object_data = _entry_data(object_entry) if object_entry else None
        if not isinstance(object_data, dict) or not bool(object_data.get("m_IsActive", True)):
            continue
        base_rect = _rect_transform_child_rect(transform_data, parent_rect, world_scale)
        layout_element = _preview_layout_element_data(scope, object_data)
        if layout_element and bool(layout_element.get("m_IgnoreLayout", False)):
            continue
        width, height = base_rect[2], base_rect[3]
        preferred_width = (
            _number(layout_element.get("m_PreferredWidth"), -1.0) * scale_x
            if layout_element else -1.0
        )
        preferred_height = (
            _number(layout_element.get("m_PreferredHeight"), -1.0) * scale_y
            if layout_element else -1.0
        )
        flexible_height = (
            _number(layout_element.get("m_FlexibleHeight"), -1.0)
            if layout_element else -1.0
        )
        if control_width:
            if force_width:
                width = inner_width
            elif preferred_width >= 0:
                width = min(inner_width, preferred_width)
        if control_height and preferred_height >= 0:
            height = preferred_height
        provisional, _used_aspect = _preview_aspect_fitted_rect(
            scope,
            object_data,
            transform_data,
            (base_rect[0], base_rect[1], max(1.0, width), max(1.0, height)),
        )
        rows.append(
            {
                "path_id": object_id,
                "transform": transform_data,
                "object_data": object_data,
                "width": provisional[2],
                "height": provisional[3],
                "flexible_height": max(
                    flexible_height,
                    1.0 if force_height else 0.0,
                ),
            }
        )
    if not rows:
        return {}, True

    total_spacing = spacing * max(0, len(rows) - 1)
    preferred_total_height = sum(row["height"] for row in rows) + total_spacing
    # A stable menu/list container has room for its serialized children.  If
    # not, it is commonly a holder for mutually exclusive runtime states.
    if len(rows) > 1 and preferred_total_height > inner_height + max(2.0, inner_height * 0.05):
        return {}, False

    for row in rows:
        row["cell_height"] = row["height"]
    remaining = inner_height - preferred_total_height
    total_flexible = sum(row["flexible_height"] for row in rows)
    if remaining > 0 and total_flexible > 0:
        for row in rows:
            row["cell_height"] += (
                remaining * row["flexible_height"] / total_flexible
            )

    alignment = int(layout.get("m_ChildAlignment", 0) or 0)
    horizontal_alignment = alignment % 3
    vertical_alignment = max(0, min(2, alignment // 3))
    ordered_rows = (
        list(reversed(rows))
        if bool(layout.get("m_ReverseArrangement", False)) else rows
    )
    total_height = sum(row["cell_height"] for row in ordered_rows) + total_spacing
    inner_bottom = parent_rect[1] + padding_bottom
    inner_top = parent_rect[1] + parent_rect[3] - padding_top
    group_bottom = (
        inner_bottom
        + max(0.0, inner_height - total_height) * ((2 - vertical_alignment) / 2.0)
    )
    cursor_top = group_bottom + total_height
    result: dict[int, tuple[float, float, float, float]] = {}
    for row in ordered_rows:
        cell_height = max(1.0, row["cell_height"])
        cell_bottom = cursor_top - cell_height
        child_height = cell_height if control_height else min(row["height"], cell_height)
        fitted, _used_aspect = _preview_aspect_fitted_rect(
            scope,
            row["object_data"],
            row["transform"],
            (0.0, 0.0, row["width"], max(1.0, child_height)),
        )
        child_width = min(fitted[2], inner_width) if control_width else fitted[2]
        child_height = min(fitted[3], cell_height)
        x = (
            parent_rect[0] + padding_left
            + (inner_width - child_width) * (horizontal_alignment / 2.0)
        )
        y = (
            cell_bottom
            + (cell_height - child_height) * ((2 - vertical_alignment) / 2.0)
        )
        result[int(row["path_id"])] = (
            x,
            y,
            max(1.0, child_width),
            max(1.0, child_height),
        )
        cursor_top = cell_bottom - spacing
    return result, True


def _preview_vertical_layout_cross_axis_rects(
    scope: dict,
    parent_game_object_data: dict,
    parent_rect: tuple[float, float, float, float],
    world_scale: tuple[float, float],
    child_nodes: list[tuple[int, dict]],
) -> tuple[dict[int, tuple[float, float, float, float]], bool]:
    """Restore VerticalLayoutGroup's horizontal alignment without guessing state order.

    Runtime UI prefabs often serialize several mutually exclusive footer states
    as active, then let a controller choose one before the first layout rebuild.
    Stacking every serialized state vertically is misleading, but the group's
    cross-axis alignment is still deterministic and safe to preview.
    """
    layout = _preview_vertical_layout_data(scope, parent_game_object_data)
    if layout is None or not child_nodes:
        return {}, False
    padding = layout.get("m_Padding")
    if not isinstance(padding, dict):
        padding = {}
    scale_x = abs(world_scale[0])
    padding_left = _number(padding.get("m_Left")) * scale_x
    padding_right = _number(padding.get("m_Right")) * scale_x
    inner_width = max(0.0, parent_rect[2] - padding_left - padding_right)
    control_width = bool(layout.get("m_ChildControlWidth", False))
    force_width = bool(layout.get("m_ChildForceExpandWidth", False))
    horizontal_alignment = int(layout.get("m_ChildAlignment", 0) or 0) % 3
    result: dict[int, tuple[float, float, float, float]] = {}
    for object_id, transform_data in child_nodes:
        object_entry = _scope_entry(scope, ("GameObject",), object_id)
        object_data = _entry_data(object_entry) if object_entry else None
        if not isinstance(object_data, dict) or not bool(object_data.get("m_IsActive", True)):
            continue
        base_rect = _rect_transform_child_rect(transform_data, parent_rect, world_scale)
        layout_element = _preview_layout_element_data(scope, object_data)
        if layout_element and bool(layout_element.get("m_IgnoreLayout", False)):
            continue
        width = base_rect[2]
        if control_width:
            preferred_width = (
                _number(layout_element.get("m_PreferredWidth"), -1.0) * scale_x
                if layout_element else -1.0
            )
            if force_width:
                width = inner_width
            elif preferred_width >= 0:
                width = min(inner_width, preferred_width)
        fitted, _used_aspect = _preview_aspect_fitted_rect(
            scope,
            object_data,
            transform_data,
            (base_rect[0], base_rect[1], max(1.0, width), base_rect[3]),
        )
        horizontal_surplus = inner_width - fitted[2]
        x = (
            parent_rect[0] + padding_left
            + horizontal_surplus * (horizontal_alignment / 2.0)
        )
        result[object_id] = (x, fitted[1], fitted[2], fitted[3])
    return result, True


def _preview_ngui_component_rect(
    component_data: dict,
    transform_data: dict,
    transform_rect: tuple[float, float, float, float],
    world_scale: tuple[float, float] | None = None,
) -> tuple[float, float, float, float] | None:
    pivot_factors = {
        0: (0.0, 1.0), 1: (0.5, 1.0), 2: (1.0, 1.0),
        3: (0.0, 0.5), 4: (0.5, 0.5), 5: (1.0, 0.5),
        6: (0.0, 0.0), 7: (0.5, 0.0), 8: (1.0, 0.0),
    }
    point_x = transform_rect[0] + transform_rect[2] / 2.0
    point_y = transform_rect[1] + transform_rect[3] / 2.0
    scale_x, scale_y = (
        world_scale
        if world_scale is not None
        else _vec2(transform_data.get("m_LocalScale"), (1.0, 1.0))
    )
    width = abs(_number(component_data.get("mWidth")) * scale_x)
    height = abs(_number(component_data.get("mHeight")) * scale_y)
    if width <= 0 or height <= 0:
        return None
    pivot_x, pivot_y = pivot_factors.get(
        int(component_data.get("mPivot", 4) or 0), (0.5, 0.5)
    )
    return (
        point_x - pivot_x * width,
        point_y - pivot_y * height,
        width,
        height,
    )


def _preview_ngui_widget_rect(
    scope: dict,
    game_object_data: dict,
    transform_data: dict,
    transform_rect: tuple[float, float, float, float],
    world_scale: tuple[float, float] | None = None,
) -> tuple[float, float, float, float]:
    """Expand a 1x1 Transform point to the serialized NGUI UIWidget bounds."""
    widget_rects: list[tuple[float, float, float, float]] = []
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict):
            continue
        component_rect = _preview_ngui_component_rect(
            data, transform_data, transform_rect, world_scale
        )
        if component_rect is not None:
            widget_rects.append(component_rect)
    if not widget_rects:
        return transform_rect
    min_x = min(rect[0] for rect in widget_rects)
    min_y = min(rect[1] for rect in widget_rects)
    max_x = max(rect[0] + rect[2] for rect in widget_rects)
    max_y = max(rect[1] + rect[3] for rect in widget_rects)
    return (min_x, min_y, max_x - min_x, max_y - min_y)


def _preview_ngui_root_reference_size(
    scope: dict,
    game_object_data: dict,
) -> tuple[float, float] | None:
    """Return NGUI UIRoot's UI-coordinate reference size when serialized."""
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict):
            continue
        if not {
            "scalingStyle", "manualWidth", "manualHeight"
        }.issubset(data):
            continue
        width = abs(_number(data.get("manualWidth")))
        height = abs(_number(data.get("manualHeight")))
        if width >= 2 and height >= 2:
            return (width, height)
    return None


def _preview_ugui_root_reference_size(
    scope: dict,
    game_object_data: dict,
) -> tuple[float, float] | None:
    """Return a CanvasScaler reference resolution when one is serialized."""
    for component_path_id in _game_object_component_path_ids(game_object_data):
        entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        data = _entry_data(entry) if entry else None
        if not isinstance(data, dict):
            continue
        reference = data.get("m_ReferenceResolution")
        if not isinstance(reference, dict) or "m_UiScaleMode" not in data:
            continue
        width, height = _vec2(reference, (0.0, 0.0))
        width, height = abs(width), abs(height)
        if width >= 2 and height >= 2:
            return (width, height)
    return None


def _preview_ui_root_reference_size(
    scope: dict,
    game_object_data: dict,
) -> tuple[float, float] | None:
    return (
        _preview_ngui_root_reference_size(scope, game_object_data)
        or _preview_ugui_root_reference_size(scope, game_object_data)
    )


def _preview_sprite_image(scope: dict, sprite_path_id: int):
    from PIL import Image

    sprite_entry = _scope_entry(scope, ("Sprite",), sprite_path_id)
    sprite_data = _entry_data(sprite_entry) if sprite_entry else None
    if not sprite_data:
        return None
    render_data, render_scope = _sprite_render_data_with_scope(sprite_data, scope)
    file_id, texture_path_id = _pptr(
        render_data.get("texture") or render_data.get("m_Texture")
    )
    texture_scope = (
        _resolve_pointer_scope(render_scope, file_id)
        if render_scope is not None else None
    )
    if texture_scope is None or not texture_path_id:
        return None
    texture_entry = _scope_entry(texture_scope, ("Texture2D",), texture_path_id)
    rect = render_data.get("textureRect") or render_data.get("m_TextureRect")
    if not texture_entry or not isinstance(rect, dict):
        return None
    x = round(_number(rect.get("x")))
    y = round(_number(rect.get("y")))
    width = round(_number(rect.get("width")))
    height = round(_number(rect.get("height")))
    if width <= 0 or height <= 0:
        return None
    try:
        with Image.open(texture_entry["path"]) as atlas:
            atlas = atlas.convert("RGBA")
            top = atlas.height - y - height
            cropped = atlas.crop((x, top, x + width, top + height))
        rotation = (int(render_data.get("settingsRaw", 0) or 0) >> 2) & 0xF
        if rotation == 1:
            cropped = cropped.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        elif rotation == 2:
            cropped = cropped.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        elif rotation == 3:
            cropped = cropped.transpose(Image.Transpose.ROTATE_180)
        elif rotation == 4:
            cropped = cropped.transpose(Image.Transpose.ROTATE_90)
        return cropped
    except (OSError, ValueError):
        return None


def _preview_ngui_sprite_image(scope: dict, atlas_path_id: int, sprite_name: str):
    from PIL import Image

    resolved = _resolve_ngui_atlas(scope, atlas_path_id)
    if not resolved:
        return None
    wanted_name = sprite_name.casefold()
    row = next(
        (
            value for value in resolved["sprites"]
            if str(value.get("name", "")).casefold() == wanted_name
        ),
        None,
    )
    if row is None:
        return None
    x = round(_number(row.get("x")))
    y = round(_number(row.get("y")))
    width = round(_number(row.get("width")))
    height = round(_number(row.get("height")))
    if width <= 0 or height <= 0:
        return None
    texture_path = resolved["texture_entry"].get("path")
    if not isinstance(texture_path, Path):
        return None
    try:
        with Image.open(texture_path) as atlas:
            if x < 0 or y < 0 or x + width > atlas.width or y + height > atlas.height:
                return None
            return atlas.convert("RGBA").crop((x, y, x + width, y + height))
    except (OSError, ValueError):
        return None


def _preview_texture_image(scope: dict, texture_path_id: int):
    from PIL import Image

    texture_entry = _scope_entry(scope, ("Texture2D",), texture_path_id)
    texture_path = texture_entry.get("path") if texture_entry else None
    if not isinstance(texture_path, Path):
        return None
    try:
        with Image.open(texture_path) as opened:
            return opened.convert("RGBA")
    except (OSError, ValueError):
        return None


def _preview_asset_name(entry: dict | None, type_name: str, path_id: int) -> str:
    if not isinstance(entry, dict):
        return f"{type_name}_{path_id}"
    item = entry.get("item")
    if isinstance(item, dict):
        asset_name = _manifest_value(item, "AssetName", "assetName", default="")
        if isinstance(asset_name, str) and asset_name.strip():
            return asset_name.strip()
    data = _entry_data(entry)
    if isinstance(data, dict):
        data_name = data.get("m_Name")
        if isinstance(data_name, str) and data_name.strip():
            return data_name.strip()
    entry_path = entry.get("path")
    if entry_path:
        stem = Path(str(entry_path)).stem
        return re.sub(rf"_{re.escape(str(path_id))}$", "", stem) or stem
    return f"{type_name}_{path_id}"


def _preview_entry_path(entry: dict | None) -> str:
    if not isinstance(entry, dict):
        return ""
    entry_path = entry.get("path")
    return str(entry_path) if entry_path else ""


def _preview_split_image_path(
    *,
    item_type: str,
    path_id: int,
    source: str,
    bundle_entry: str,
    sprite_name: str = "",
) -> str:
    mapping = _safe_read_json(DEFAULT_ALL_SPRITE_MAP)
    items = mapping.get("items") if isinstance(mapping, dict) else None
    if not isinstance(items, list):
        return ""
    wanted_source = source.replace("/", "\\").casefold()
    wanted_bundle = bundle_entry.casefold()
    wanted_name = sprite_name.casefold()
    for item in items:
        if not isinstance(item, dict) or item.get("item_type") != item_type:
            continue
        if item_type == "sprite" and int(item.get("sprite_path_id", 0) or 0) != path_id:
            continue
        if item_type == "ngui_sprite":
            references = item.get("atlas_references")
            if not isinstance(references, list):
                references = [item]
            if not any(
                int(reference.get("atlas_path_id", 0) or 0) == path_id
                and str(reference.get("source_resource", "")).replace("/", "\\").casefold()
                == wanted_source
                and str(reference.get("bundle_entry", "")).casefold() == wanted_bundle
                for reference in references
                if isinstance(reference, dict)
            ):
                continue
            if wanted_name and str(item.get("sprite_name", "")).casefold() != wanted_name:
                continue
        elif (
            str(item.get("source_resource", "")).replace("/", "\\").casefold()
            != wanted_source
            or str(item.get("bundle_entry", "")).casefold() != wanted_bundle
        ):
            continue
        flat_name = str(item.get("flat_name", "")).strip()
        if flat_name:
            return str(DEFAULT_ALL_SPRITE_PNG_ROOT / flat_name)
    return ""


def _preview_allpng_path(export_path: str) -> str:
    if not export_path:
        return ""
    mapping = _safe_read_json(DEFAULT_ALL_IMAGE_MAP)
    if not isinstance(mapping, dict):
        return ""
    source_root = Path(str(mapping.get("source_root", DEFAULT_SOURCE_ROOT)))
    wanted = os.path.normcase(os.path.abspath(export_path))
    for item in mapping.get("items", []):
        if not isinstance(item, dict):
            continue
        original_relative = str(item.get("original_relative_path", "")).strip()
        flat_name = str(item.get("flat_name", "")).strip()
        if not original_relative or not flat_name:
            continue
        original_path = source_root / Path(original_relative)
        if os.path.normcase(os.path.abspath(original_path)) == wanted:
            return str(DEFAULT_ALL_IMAGE_PNG_ROOT / flat_name)
    return ""


def _preview_sprite_resource_records(scope: dict, sprite_path_id: int) -> list[dict]:
    sprite_entry = _scope_entry(scope, ("Sprite",), sprite_path_id)
    sprite_data = _entry_data(sprite_entry) if sprite_entry else None
    if not sprite_data:
        return []
    records = [
        {
            "type": "Sprite",
            "path_id": sprite_path_id,
            "name": _preview_asset_name(sprite_entry, "Sprite", sprite_path_id),
            "source": str(scope.get("source", "")),
            "bundle_entry": str(scope.get("bundle_entry", "")),
            "export_path": _preview_entry_path(sprite_entry),
            "image_path": _preview_split_image_path(
                item_type="sprite",
                path_id=sprite_path_id,
                source=str(scope.get("source", "")),
                bundle_entry=str(scope.get("bundle_entry", "")),
            ),
        }
    ]
    render_data, render_scope = _sprite_render_data_with_scope(sprite_data, scope)
    texture_file_id, texture_path_id = _pptr(
        render_data.get("texture") or render_data.get("m_Texture")
    )
    texture_scope = (
        _resolve_pointer_scope(render_scope, texture_file_id)
        if render_scope is not None else None
    )
    if texture_scope is None or not texture_path_id:
        return records
    texture_entry = _scope_entry(texture_scope, ("Texture2D",), texture_path_id)
    texture_export_path = _preview_entry_path(texture_entry)
    records.append(
        {
            "type": "Texture2D",
            "path_id": texture_path_id,
            "name": _preview_asset_name(texture_entry, "Texture2D", texture_path_id),
            "source": str(texture_scope.get("source", "")),
            "bundle_entry": str(texture_scope.get("bundle_entry", "")),
            "export_path": texture_export_path,
            "image_path": _preview_allpng_path(texture_export_path),
        }
    )
    return records


def _preview_ngui_resource_records(
    scope: dict,
    atlas_path_id: int,
    sprite_name: str,
) -> list[dict]:
    atlas_entry = _scope_entry(scope, ("MonoBehaviour",), atlas_path_id)
    records = [
        {
            "type": "NGUI Sprite",
            "path_id": atlas_path_id,
            "name": sprite_name,
            "source": str(scope.get("source", "")),
            "bundle_entry": str(scope.get("bundle_entry", "")),
            "export_path": _preview_entry_path(atlas_entry),
            "image_path": _preview_split_image_path(
                item_type="ngui_sprite",
                path_id=atlas_path_id,
                source=str(scope.get("source", "")),
                bundle_entry=str(scope.get("bundle_entry", "")),
                sprite_name=sprite_name,
            ),
        }
    ]
    resolved = _resolve_ngui_atlas(scope, atlas_path_id)
    if not resolved:
        return records
    texture_scope = resolved["texture_scope"]
    texture_path_id = int(resolved["texture_path_id"])
    texture_export_path = _preview_entry_path(resolved["texture_entry"])
    records.append(
        {
            "type": "Texture2D",
            "path_id": texture_path_id,
            "name": _preview_asset_name(
                resolved["texture_entry"], "Texture2D", texture_path_id
            ),
            "source": str(texture_scope.get("source", "")),
            "bundle_entry": str(texture_scope.get("bundle_entry", "")),
            "export_path": texture_export_path,
            "image_path": _preview_allpng_path(texture_export_path),
        }
    )
    return records


def _preview_texture_resource_records(scope: dict, texture_path_id: int) -> list[dict]:
    texture_entry = _scope_entry(scope, ("Texture2D",), texture_path_id)
    if texture_entry is None:
        return []
    texture_export_path = _preview_entry_path(texture_entry)
    return [
        {
            "type": "Texture2D",
            "path_id": texture_path_id,
            "name": _preview_asset_name(texture_entry, "Texture2D", texture_path_id),
            "source": str(scope.get("source", "")),
            "bundle_entry": str(scope.get("bundle_entry", "")),
            "export_path": texture_export_path,
            "image_path": _preview_allpng_path(texture_export_path),
        }
    ]


def _preview_named_image_resources(region: dict) -> list[dict]:
    resources = region.get("image_resources")
    if not isinstance(resources, list):
        return []
    result: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        type_name = str(resource.get("type", ""))
        path_id = int(resource.get("path_id", 0) or 0)
        name = str(resource.get("name", "")).strip()
        if type_name not in {"Sprite", "NGUI Sprite", "Texture2D"} or not path_id or not name:
            continue
        key = (type_name, str(resource.get("source", "")).casefold(), path_id)
        if key in seen:
            continue
        seen.add(key)
        result.append(resource)
    return result


def _preview_resource_name_text(region: dict) -> str:
    lines: list[str] = []
    for resource in _preview_named_image_resources(region):
        type_name = str(resource.get("type", ""))
        name = str(resource.get("name", ""))
        path_id = int(resource.get("path_id", 0) or 0)
        lines.append(f"{type_name} Unity 资源名: {name} (PathID={path_id})")
        source = str(resource.get("source", ""))
        bundle_entry = str(resource.get("bundle_entry", ""))
        export_path = str(resource.get("export_path", ""))
        image_path = str(resource.get("image_path", ""))
        if source:
            lines.append(f"  来源资源: {source}")
        if bundle_entry:
            lines.append(f"  Bundle entry: {bundle_entry}")
        if export_path:
            lines.append(f"  导出文件: {export_path}")
        if image_path:
            image_label = "拆分 PNG" if type_name in {"Sprite", "NGUI Sprite"} else "AllPNG 文件"
            lines.append(f"  {image_label}: {image_path}")
    return "\n".join(lines)


def _show_copyable_text_dialog(parent, title: str, text: str) -> None:
    import tkinter as tk
    from tkinter import ttk

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.transient(parent)
    dialog.minsize(680, 320)
    dialog.geometry("900x480")

    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=1)
    body.rowconfigure(0, weight=1)

    text_box = tk.Text(body, wrap="none", font=("Consolas", 10), undo=False)
    vertical = ttk.Scrollbar(body, orient="vertical", command=text_box.yview)
    horizontal = ttk.Scrollbar(body, orient="horizontal", command=text_box.xview)
    text_box.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    text_box.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    text_box.insert("1.0", text)

    buttons = ttk.Frame(body)
    buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(10, 0))

    def copy_all() -> None:
        dialog.clipboard_clear()
        dialog.clipboard_append(text)
        dialog.update_idletasks()

    ttk.Button(buttons, text="复制全部", command=copy_all).pack(side="left", padx=(0, 8))
    ttk.Button(buttons, text="关闭", command=dialog.destroy).pack(side="left")

    def select_all(_event=None):
        text_box.tag_add("sel", "1.0", "end-1c")
        text_box.mark_set("insert", "1.0")
        return "break"

    text_box.bind("<Control-a>", select_all)
    text_box.bind("<Control-A>", select_all)
    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    text_box.focus_set()
    dialog.grab_set()
    dialog.wait_window()


def _preview_resource_query_label(region: dict) -> str:
    types = {
        str(resource.get("type", ""))
        for resource in _preview_named_image_resources(region)
    }
    ordered = [
        type_name for type_name in ("Sprite", "NGUI Sprite", "Texture2D")
        if type_name in types
    ]
    return f"查询 {' / '.join(ordered)} 资源名字" if ordered else ""


def _preview_component(scope: dict, game_object_data: dict) -> tuple[dict, object] | None:
    """Return the first enabled UI Image/SpriteRenderer and its decoded sprite."""
    for component_path_id in _game_object_component_path_ids(game_object_data):
        for component_type in ("MonoBehaviour", "SpriteRenderer"):
            entry = _scope_entry(scope, (component_type,), component_path_id)
            data = _entry_data(entry) if entry else None
            if not data or int(data.get("m_Enabled", 1) or 0) == 0:
                continue
            atlas_file_id, atlas_path_id = _pptr(data.get("mAtlas"))
            sprite_name = str(data.get("mSpriteName", "")).strip()
            atlas_scope = _resolve_pointer_scope(scope, atlas_file_id)
            if atlas_scope is not None and atlas_path_id and sprite_name:
                image = _preview_ngui_sprite_image(atlas_scope, atlas_path_id, sprite_name)
                if image is not None:
                    return data, image
            texture_pointer = data.get("mTexture")
            if texture_pointer is None:
                texture_pointer = data.get("m_Texture")
            texture_file_id, texture_path_id = _pptr(texture_pointer)
            texture_scope = _resolve_pointer_scope(scope, texture_file_id)
            if texture_scope is not None and texture_path_id:
                image = _preview_texture_image(texture_scope, texture_path_id)
                if image is not None:
                    return data, image
            file_id, sprite_path_id = _pptr(data.get("m_Sprite"))
            sprite_scope = _resolve_pointer_scope(scope, file_id)
            if sprite_scope is None or not sprite_path_id:
                continue
            image = _preview_sprite_image(sprite_scope, sprite_path_id)
            if image is not None:
                return data, image
    return None


def _has_unresolved_preview_sprite(scope: dict, game_object_data: dict) -> bool:
    for component_path_id in _game_object_component_path_ids(game_object_data):
        for component_type in ("MonoBehaviour", "SpriteRenderer"):
            entry = _scope_entry(scope, (component_type,), component_path_id)
            data = _entry_data(entry) if entry else None
            if not data:
                continue
            if "m_Sprite" not in data:
                atlas_path_id = _pptr(data.get("mAtlas"))[1]
                if atlas_path_id and str(data.get("mSpriteName", "")).strip():
                    return True
                if _pptr(data.get("mTexture"))[1] or _pptr(data.get("m_Texture"))[1]:
                    return True
                continue
            _file_id, sprite_path_id = _pptr(data.get("m_Sprite"))
            if sprite_path_id:
                return True
    return False


def _preview_font(size: int):
    from PIL import ImageFont

    for path in (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simsun.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "simhei.ttf",
    ):
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            continue
    return ImageFont.load_default()


def _preview_clean_ngui_text(value: str) -> str:
    """Remove NGUI formatting tokens that Pillow would otherwise draw literally."""
    value = re.sub(r"\[(?:[0-9A-Fa-f]{6,8}|-|/?(?:b|i|u|s|sub|sup))\]", "", value)
    return value.replace("\\n", "\n").replace("\r", "").strip()


def _create_object_hierarchy_preview(match: dict, root_level: int, scopes: dict) -> Path:
    """Render a best-effort static Unity UI/sprite hierarchy preview."""
    from PIL import Image, ImageChops, ImageDraw

    scope = _match_scope(scopes, match)
    if scope is None:
        raise ValueError("无法从导出数据中恢复该匹配项所在的资源范围")
    chain = match.get("chain", [])
    if root_level < 0 or root_level >= len(chain):
        raise ValueError(f"预览起始层级必须在 0-{len(chain) - 1} 之间")
    root_object_id = int(chain[root_level]["path_id"])
    root_transform = _find_game_object_transform(scope, root_object_id)
    if not root_transform:
        raise ValueError("预览起始对象没有可用的 Transform/RectTransform")
    root_object_entry = _scope_entry(scope, ("GameObject",), root_object_id)
    root_object_data = _entry_data(root_object_entry) if root_object_entry else None
    serialized_object_order = {
        path_id: index
        for index, path_id in enumerate(
            _game_object_subtree_path_ids(scope, root_object_id, max_objects=4000)
        )
    }
    ui_root_size = (
        _preview_ui_root_reference_size(scope, root_object_data)
        if isinstance(root_object_data, dict) else None
    )
    root_size = _vec2(root_transform.get("m_SizeDelta"), (0.0, 0.0))
    logical_width = (
        ui_root_size[0] if ui_root_size
        else abs(root_size[0]) if abs(root_size[0]) >= 2
        else 1920.0
    )
    logical_height = (
        ui_root_size[1] if ui_root_size
        else abs(root_size[1]) if abs(root_size[1]) >= 2
        else 1080.0
    )
    chain_levels = {
        int(node["path_id"]): level
        for level, node in enumerate(chain[:root_level + 1])
    }
    # The preview has a node cap, so always walk the selected image's ancestor
    # chain before its siblings.  Otherwise a large sibling branch can consume
    # the whole cap and make the entry list contain only the top few levels.
    chain_child_by_parent = {
        int(chain[level]["path_id"]): int(chain[level - 1]["path_id"])
        for level in range(1, root_level + 1)
    }
    node_rects: dict[int, tuple[float, float, float, float]] = {}
    content_rects: dict[int, tuple[float, float, float, float]] = {}
    visual_path_ids: set[int] = set()
    tree_records: dict[int, dict] = {}
    rendered_images: list[
        tuple[int, str, tuple[float, float, float, float], bool, list[dict], dict, object]
    ] = []
    rendered_texts: list[
        tuple[int, str, tuple[float, float, float, float], bool, dict]
    ] = []
    missing_sprites = 0
    visited: set[int] = set()
    force_active_path_ids = {
        int(value) for value in match.get("force_active_path_ids", [])
        if str(value).lstrip("-").isdigit()
    }
    limited_hierarchy_values = match.get("limited_hierarchy_path_ids")
    hidden_path_ids = {
        int(value) for value in match.get("hidden_path_ids", [])
        if str(value).lstrip("-").isdigit()
    }
    limited_hierarchy_path_ids = (
        {
            int(value) for value in limited_hierarchy_values
            if str(value).lstrip("-").isdigit()
        }
        if isinstance(limited_hierarchy_values, (list, tuple, set)) else None
    )

    def render_node(
        object_id: int,
        rect: tuple[float, float, float, float],
        parent_rect: tuple[float, float, float, float] | None,
        inherited_active: bool,
        parent_chain: list[dict],
        world_scale: tuple[float, float],
        world_rotation: float,
    ) -> None:
        nonlocal missing_sprites
        if object_id in hidden_path_ids:
            return
        if (
            limited_hierarchy_path_ids is not None
            and object_id not in limited_hierarchy_path_ids
        ):
            return
        if object_id in visited or len(visited) >= 2000:
            return
        visited.add(object_id)
        object_entry = _scope_entry(scope, ("GameObject",), object_id)
        object_data = _entry_data(object_entry) if object_entry else None
        transform_data = _find_game_object_transform(scope, object_id)
        if not object_data or not transform_data:
            return
        # Both NGUI UIRoot and UGUI CanvasScaler serialize UI reference pixels.
        # Their runtime placeholder scale/size must not collapse descendants.
        node_ui_reference_size = _preview_ui_root_reference_size(scope, object_data)
        if node_ui_reference_size:
            rect = (
                rect[0], rect[1],
                node_ui_reference_size[0], node_ui_reference_size[1],
            )
            world_scale = (1.0, 1.0)
        active = inherited_active and (
            bool(object_data.get("m_IsActive", True))
            or object_id in force_active_path_ids
        )
        name = str(object_data.get("m_Name") or object_id)
        hierarchy_chain = [*parent_chain, {"name": name, "path_id": object_id}]
        rect, used_aspect_fitter = _preview_aspect_fitted_rect(
            scope, object_data, transform_data, rect
        )
        visual_rect = _preview_ngui_widget_rect(
            scope, object_data, transform_data, rect, world_scale
        )
        node_rects[object_id] = visual_rect
        tree_records[object_id] = {
            "path_id": object_id,
            "name": name,
            "active": active,
            "source_json": str(object_entry["path"]) if object_entry else "",
            "parent_path_id": (
                int(parent_chain[-1]["path_id"]) if parent_chain else 0
            ),
            "children": [],
            "chain": hierarchy_chain,
        }
        if used_aspect_fitter:
            tree_records[object_id]["geometry_note"] = "aspect_ratio_fitter"
        raw_local_scale = _vec2(
            transform_data.get("m_LocalScale"), (1.0, 1.0)
        )
        if abs(raw_local_scale[0]) < 1e-6 or abs(raw_local_scale[1]) < 1e-6:
            tree_records[object_id]["geometry_note"] = "zero_scale_previewed_as_unit"
        if node_ui_reference_size:
            tree_records[object_id]["ui_reference_size"] = {
                "width": node_ui_reference_size[0],
                "height": node_ui_reference_size[1],
            }
        for component_path_id in _game_object_component_path_ids(object_data):
            label_entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
            label_data = _entry_data(label_entry) if label_entry else None
            if not isinstance(label_data, dict):
                continue
            label_text = next(
                (
                    value for key in ("mText", "m_Text", "m_text")
                    if isinstance((value := label_data.get(key)), str)
                    and value.strip()
                ),
                None,
            )
            if not isinstance(label_text, str):
                continue
            label_rect = _preview_ngui_component_rect(
                label_data, transform_data, rect, world_scale
            ) or visual_rect
            rendered_texts.append(
                (
                    object_id, label_text.strip(), label_rect, active,
                    {**label_data, "_preview_world_scale": world_scale},
                )
            )
            visual_path_ids.add(object_id)
            tree_records[object_id].setdefault("texts", []).append(label_text.strip())
        component = _preview_component(scope, object_data)
        if component:
            component_data, sprite = component
            component_rect = visual_rect
            # LayoutGroup/Slider-driven RectTransforms can serialize as 0x0 and
            # only receive their size at runtime.  Keep those images inspectable
            # by falling back to their decoded native dimensions.
            if component_rect[2] <= 2.0 and component_rect[3] <= 2.0:
                native_width = max(1.0, float(sprite.width) * abs(world_scale[0]))
                native_height = max(1.0, float(sprite.height) * abs(world_scale[1]))
                if parent_rect is not None:
                    if native_width <= 2.0 and parent_rect[2] > 2.0:
                        native_width = parent_rect[2]
                    if native_height <= 2.0 and parent_rect[3] > 2.0:
                        native_height = parent_rect[3]
                center_x = component_rect[0] + component_rect[2] / 2.0
                center_y = component_rect[1] + component_rect[3] / 2.0
                component_rect = (
                    center_x - native_width / 2.0,
                    center_y - native_height / 2.0,
                    native_width,
                    native_height,
                )
                node_rects[object_id] = component_rect
                tree_records[object_id]["geometry_note"] = "native_sprite_size_fallback"
            if bool(component_data.get("m_FlipX", False)):
                sprite = sprite.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if bool(component_data.get("m_FlipY", False)):
                sprite = sprite.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            if world_scale[0] < 0:
                sprite = sprite.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
            if world_scale[1] < 0:
                sprite = sprite.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
            sprite_file_id, sprite_path_id = _pptr(component_data.get("m_Sprite"))
            sprite_scope = _resolve_pointer_scope(scope, sprite_file_id)
            if sprite_scope is not None and sprite_path_id:
                tree_records[object_id]["image_resources"] = (
                    _preview_sprite_resource_records(sprite_scope, sprite_path_id)
                )
            else:
                atlas_file_id, atlas_path_id = _pptr(component_data.get("mAtlas"))
                atlas_scope = _resolve_pointer_scope(scope, atlas_file_id)
                sprite_name = str(component_data.get("mSpriteName", "")).strip()
                if atlas_scope is not None and atlas_path_id and sprite_name:
                    tree_records[object_id]["image_resources"] = (
                        _preview_ngui_resource_records(
                            atlas_scope, atlas_path_id, sprite_name
                        )
                    )
                else:
                    texture_pointer = component_data.get("mTexture")
                    if texture_pointer is None:
                        texture_pointer = component_data.get("m_Texture")
                    texture_file_id, texture_path_id = _pptr(texture_pointer)
                    texture_scope = _resolve_pointer_scope(scope, texture_file_id)
                    if texture_scope is not None and texture_path_id:
                        tree_records[object_id]["image_resources"] = (
                            _preview_texture_resource_records(
                                texture_scope, texture_path_id
                            )
                        )
            color = component_data.get("m_Color") or component_data.get("mColor")
            if isinstance(color, dict):
                tint = Image.new(
                    "RGBA",
                    sprite.size,
                    tuple(
                        max(0, min(255, round(_number(color.get(key), 1.0) * 255)))
                        for key in ("r", "g", "b", "a")
                    ),
                )
                sprite = ImageChops.multiply(sprite, tint)
            if abs(world_rotation) > 0.01:
                sprite = sprite.rotate(
                    -world_rotation,
                    expand=True,
                    resample=Image.Resampling.BICUBIC,
                )
                component_rect = _preview_rotated_rect(component_rect, world_rotation)
                node_rects[object_id] = component_rect
            if not active:
                alpha = sprite.getchannel("A").point(lambda value: value // 3)
                sprite.putalpha(alpha)
            rendered_images.append(
                (
                    object_id,
                    name,
                    component_rect,
                    active,
                    hierarchy_chain,
                    component_data,
                    sprite,
                )
            )
            visual_path_ids.add(object_id)
        elif _has_unresolved_preview_sprite(scope, object_data):
            missing_sprites += 1
            tree_records[object_id]["preview_warning"] = "image_reference_unresolved"

        serialized_child_nodes: list[tuple[int, dict]] = []
        for pointer in _array_value(transform_data.get("m_Children")):
            file_id, child_transform_id = _pptr(pointer)
            child_transform_entry = (
                _scope_entry(scope, ("Transform", "RectTransform"), child_transform_id)
                if file_id == 0 else None
            )
            child_transform = _entry_data(child_transform_entry) if child_transform_entry else None
            if not child_transform:
                continue
            child_file_id, child_object_id = _pptr(child_transform.get("m_GameObject"))
            if child_file_id != 0 or not child_object_id:
                continue
            serialized_child_nodes.append((child_object_id, child_transform))

        preferred_child_id = chain_child_by_parent.get(object_id)
        traversal_child_nodes = list(serialized_child_nodes)
        if preferred_child_id:
            traversal_child_nodes.sort(
                key=lambda item: 0 if item[0] == preferred_child_id else 1
            )
        layout_child_rects, used_horizontal_layout = (
            _preview_horizontal_layout_child_rects(
                scope, object_data, rect, world_scale, serialized_child_nodes
            )
        )
        if used_horizontal_layout:
            tree_records[object_id]["layout_note"] = "horizontal_layout_group"
        elif serialized_child_nodes:
            layout_child_rects, used_vertical_layout = (
                _preview_vertical_layout_child_rects(
                    scope, object_data, rect, world_scale,
                    serialized_child_nodes,
                )
            )
            if used_vertical_layout:
                tree_records[object_id]["layout_note"] = "vertical_layout_group"
            else:
                layout_child_rects, used_vertical_cross_layout = (
                    _preview_vertical_layout_cross_axis_rects(
                        scope, object_data, rect, world_scale,
                        serialized_child_nodes,
                    )
                )
                if used_vertical_cross_layout:
                    tree_records[object_id]["layout_note"] = (
                        "vertical_layout_cross_axis_runtime_states"
                    )
        rendered_child_ids: set[int] = set()
        for child_object_id, child_transform in traversal_child_nodes:
            if (
                limited_hierarchy_path_ids is not None
                and child_object_id not in limited_hierarchy_path_ids
            ):
                continue
            child_rect = layout_child_rects.get(
                child_object_id,
                _rect_transform_child_rect(child_transform, rect, world_scale),
            )
            child_rect = _preview_rotate_rect_position(
                child_rect,
                (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0),
                world_rotation,
            )
            child_local_scale = _preview_effective_local_scale(child_transform)
            child_world_scale = (
                world_scale[0] * child_local_scale[0],
                world_scale[1] * child_local_scale[1],
            )
            child_world_rotation = (
                world_rotation + _preview_transform_z_angle(child_transform)
            )
            render_node(
                child_object_id, child_rect, rect, active, hierarchy_chain,
                child_world_scale, child_world_rotation,
            )
            if child_object_id in tree_records:
                rendered_child_ids.add(child_object_id)
        tree_records[object_id]["children"] = [
            child_object_id
            for child_object_id, _child_transform in serialized_child_nodes
            if child_object_id in rendered_child_ids
        ]

    root_world_scale = _preview_root_world_scale(
        root_transform,
        uses_ui_reference_size=bool(ui_root_size),
    )
    render_node(
        root_object_id,
        (0.0, 0.0, logical_width, logical_height),
        None,
        True,
        [],
        root_world_scale,
        _preview_transform_z_angle(root_transform),
    )

    # NGUI commonly uses empty GameObjects as panels, anchors and containers.
    # Their Transform has no rectangle of its own; use the visual union of the
    # descendants so selection highlights the actual group instead of a 1px dot.
    pending_container_ids = list(tree_records)
    for object_id in reversed(pending_container_ids):
        rect = node_rects.get(object_id)
        record = tree_records.get(object_id, {})
        if not rect or rect[2] > 2.0 or rect[3] > 2.0:
            continue
        child_rects = [
            node_rects[child_id]
            for child_id in record.get("children", [])
            if child_id in node_rects
            and node_rects[child_id][2] > 2.0
            and node_rects[child_id][3] > 2.0
        ]
        if not child_rects:
            continue
        min_child_x = min(value[0] for value in child_rects)
        min_child_y = min(value[1] for value in child_rects)
        max_child_x = max(value[0] + value[2] for value in child_rects)
        max_child_y = max(value[1] + value[3] for value in child_rects)
        node_rects[object_id] = (
            min_child_x,
            min_child_y,
            max_child_x - min_child_x,
            max_child_y - min_child_y,
        )

    # Track visible descendant content separately from the serialized
    # RectTransform.  This keeps selection crops tight even when a logical
    # container stretches across the entire Canvas.
    for object_id in reversed(pending_container_ids):
        candidates: list[tuple[float, float, float, float]] = []
        if object_id in visual_path_ids and object_id in node_rects:
            candidates.append(node_rects[object_id])
        for child_id in tree_records.get(object_id, {}).get("children", []):
            child_content = content_rects.get(child_id)
            if child_content is not None:
                candidates.append(child_content)
        if not candidates:
            continue
        content_min_x = min(value[0] for value in candidates)
        content_min_y = min(value[1] for value in candidates)
        content_max_x = max(value[0] + value[2] for value in candidates)
        content_max_y = max(value[1] + value[3] for value in candidates)
        content_rects[object_id] = (
            content_min_x,
            content_min_y,
            content_max_x - content_min_x,
            content_max_y - content_min_y,
        )

    visible_bounds = [(0.0, 0.0, logical_width, logical_height)]
    visible_bounds.extend(node_rects.values())
    min_x = min(rect[0] for rect in visible_bounds)
    min_y = min(rect[1] for rect in visible_bounds)
    max_x = max(rect[0] + rect[2] for rect in visible_bounds)
    max_y = max(rect[1] + rect[3] for rect in visible_bounds)
    padding = max(12.0, min(logical_width, logical_height) * 0.015)
    min_x -= padding
    min_y -= padding
    max_x += padding
    max_y += padding
    viewport_width = max_x - min_x
    viewport_height = max_y - min_y
    # Preserve detail according to the UI reference canvas, not according to
    # every inactive/off-screen sibling.  A very wide ScrollRect content node
    # must not shrink an otherwise normal selected subtree to 1-2 pixels.
    scale = _preview_canvas_scale(
        logical_width, logical_height, viewport_width, viewport_height
    )
    canvas_width = max(320, round(viewport_width * scale))
    canvas_height = max(240, round(viewport_height * scale))
    canvas = Image.new("RGBA", (canvas_width, canvas_height), (28, 31, 38, 255))
    draw = ImageDraw.Draw(canvas, "RGBA")
    preview_layers: list[tuple[int, object, int, int]] = []

    def pixel_rect(rect: tuple[float, float, float, float]) -> tuple[int, int, int, int]:
        return (
            round((rect[0] - min_x) * scale),
            round((max_y - rect[1] - rect[3]) * scale),
            max(1, round(rect[2] * scale)),
            max(1, round(rect[3] * scale)),
        )

    rendered_images.sort(
        key=lambda row: (
            int(row[5].get("mDepth", row[5].get("m_Depth", 0)) or 0),
            serialized_object_order.get(int(row[0]), 1_000_000),
        )
    )
    for (
        _object_id,
        _name,
        rect,
        _active,
        _hierarchy_chain,
        component_data,
        sprite,
    ) in rendered_images:
        left, top, target_width, target_height = pixel_rect(rect)
        preserve_aspect = bool(
            component_data.get("m_PreserveAspect", False)
            or component_data.get("mFixedAspect", False)
            or component_data.get("keepAspectRatio", False)
        )
        if preserve_aspect:
            sprite.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
        else:
            sprite = sprite.resize((target_width, target_height), Image.Resampling.LANCZOS)
        paste_left = round(left + (target_width - sprite.width) / 2)
        paste_top = round(top + (target_height - sprite.height) / 2)
        canvas.alpha_composite(sprite, (paste_left, paste_top))
        preview_layers.append((_object_id, sprite.copy(), paste_left, paste_top))

    for _object_id, label_text, label_rect, active, label_data in rendered_texts:
        left, top, target_width, target_height = pixel_rect(label_rect)
        text_layer = Image.new(
            "RGBA", (target_width, target_height), (0, 0, 0, 0)
        )
        text_draw = ImageDraw.Draw(text_layer, "RGBA")
        cleaned_text = _preview_clean_ngui_text(label_text)
        if len(cleaned_text) > 160:
            cleaned_text = cleaned_text[:157] + "..."
        text_world_scale = label_data.get("_preview_world_scale", (1.0, 1.0))
        world_font_scale = math.sqrt(
            abs(float(text_world_scale[0]) * float(text_world_scale[1]))
        )
        serialized_font_size = label_data.get("mFontSize")
        if serialized_font_size is None:
            serialized_font_size = label_data.get("m_fontSize")
        requested_size = max(6, round(
            _number(serialized_font_size, 16)
            * _number(label_data.get("mFontScale"), 1.0)
            * world_font_scale
            * scale
        ))
        font_size = max(8, min(requested_size, max(8, target_height - 2), 64))
        font = _preview_font(font_size)
        color_data = (
            label_data.get("mColor")
            or label_data.get("m_Color")
            or label_data.get("m_fontColor")
        )
        rgba = tuple(
            max(0, min(255, round(_number(color_data.get(key), 1.0) * 255)))
            if isinstance(color_data, dict) else 255
            for key in ("r", "g", "b", "a")
        )
        if not active:
            rgba = (rgba[0], rgba[1], rgba[2], rgba[3] // 3)
        # NGUI ShrinkContent and TMP auto-sizing both reduce glyph size until
        # the label fits the serialized rectangle.
        overflow_mode = label_data.get("mOverflow")
        if overflow_mode is None:
            overflow_mode = label_data.get("m_overflowMode", 0)
        auto_sizing = bool(label_data.get("m_enableAutoSizing", False))
        minimum_size = max(6, round(
            _number(label_data.get("m_fontSizeMin"), 6)
            * world_font_scale
            * scale
        ))
        if auto_sizing or int(overflow_mode or 0) == 0:
            while font_size > minimum_size:
                probe = text_draw.multiline_textbbox(
                    (0, 0), cleaned_text, font=font, spacing=1
                )
                if (
                    probe[2] - probe[0] <= max(1, target_width - 3)
                    and probe[3] - probe[1] <= max(1, target_height - 2)
                ):
                    break
                font_size -= 1
                font = _preview_font(font_size)
        text_box = text_draw.multiline_textbbox(
            (0, 0), cleaned_text, font=font, spacing=1
        )
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        pivot = int(label_data.get("mPivot", 4) or 0)
        alignment = int(label_data.get("mAlignment", 0) or 0)
        tmp_horizontal = label_data.get("m_HorizontalAlignment")
        if tmp_horizontal is not None:
            tmp_horizontal = int(tmp_horizontal or 0)
        if (
            (tmp_horizontal is not None and bool(tmp_horizontal & 1))
            or (tmp_horizontal is None and alignment == 1)
            or (
                tmp_horizontal is None
                and alignment == 0
                and pivot in {0, 3, 6}
            )
        ):
            text_left = 0
        elif (
            (tmp_horizontal is not None and bool(tmp_horizontal & 4))
            or (tmp_horizontal is None and alignment == 3)
            or (
                tmp_horizontal is None
                and alignment == 0
                and pivot in {2, 5, 8}
            )
        ):
            text_left = max(0, target_width - text_width)
        else:
            text_left = max(0, (target_width - text_width) // 2)
        tmp_vertical = label_data.get("m_VerticalAlignment")
        if tmp_vertical is not None:
            tmp_vertical = int(tmp_vertical or 0)
        if (
            (tmp_vertical is not None and bool(tmp_vertical & 256))
            or (tmp_vertical is None and pivot in {0, 1, 2})
        ):
            text_top = 0
        elif (
            (tmp_vertical is not None and bool(tmp_vertical & 1024))
            or (tmp_vertical is None and pivot in {6, 7, 8})
        ):
            text_top = max(0, target_height - text_height)
        else:
            text_top = max(0, (target_height - text_height) // 2)
        effect_style = int(label_data.get("mEffectStyle", 0) or 0)
        if effect_style:
            effect_color = label_data.get("mEffectColor")
            effect_rgba = tuple(
                max(0, min(255, round(_number(effect_color.get(key), 0.0) * 255)))
                if isinstance(effect_color, dict) else 0
                for key in ("r", "g", "b", "a")
            )
            distance = _vec2(label_data.get("mEffectDistance"), (1.0, 1.0))
            text_draw.multiline_text(
                (text_left + round(distance[0]), text_top - round(distance[1])),
                cleaned_text, font=font, fill=effect_rgba, spacing=1,
            )
        text_draw.multiline_text(
            (text_left, text_top), cleaned_text, font=font,
            fill=rgba, spacing=1,
        )
        canvas.alpha_composite(text_layer, (left, top))
        preview_layers.append((_object_id, text_layer, left, top))

    draw = ImageDraw.Draw(canvas, "RGBA")
    small_font = _preview_font(max(11, round(13 * min(1.0, scale + 0.25))))
    footer = (
        f"静态近似预览 | 起始层级 {root_level} | 对象 {len(visited)} | "
        f"已绘制图片 {len(rendered_images)} | 已绘制文本 {len(rendered_texts)} | "
        f"无法解码/外部图片 {missing_sprites} | "
        "已还原比例/横纵布局 | 不执行动画、脚本、完整 Layout、Mask 和 Shader"
    )
    footer_box = draw.textbbox((0, 0), footer, font=small_font)
    footer_height = footer_box[3] - footer_box[1] + 10
    draw.rectangle((0, canvas_height - footer_height, canvas_width, canvas_height), fill=(0, 0, 0, 210))
    draw.text((6, canvas_height - footer_height + 4), footer, font=small_font, fill=(230, 234, 240, 255))

    DEFAULT_OBJECT_PREVIEW_ROOT.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^0-9A-Za-z._\-\u4e00-\u9fff]+', "_", str(chain[root_level]["name"]))
    target = DEFAULT_OBJECT_PREVIEW_ROOT / f"level_{root_level}_{safe_name}_{root_object_id}.png"
    canvas.convert("RGB").save(target, "PNG", quality=95)
    layer_root = DEFAULT_OBJECT_PREVIEW_ROOT / f"{target.stem}_layers"
    layer_root.mkdir(parents=True, exist_ok=True)
    image_layers = []
    for layer_index, (object_id, layer_image, left, top) in enumerate(preview_layers):
        layer_path = layer_root / f"{layer_index:04d}_{object_id}.png"
        layer_image.save(layer_path, "PNG")
        image_layers.append(
            {
                "path_id": object_id,
                "image": str(layer_path),
                "x": left,
                "y": top,
            }
        )
    regions = []
    for (
        object_id,
        name,
        rect,
        active,
        hierarchy_chain,
        _component_data,
        _sprite,
    ) in rendered_images:
        x, y, width, height = pixel_rect(rect)
        regions.append(
            {
                "path_id": object_id,
                "name": name,
                "active": active,
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "chain": hierarchy_chain,
            }
        )
    metadata_path = target.with_suffix(".regions.json")
    tree_nodes = []
    for object_id, record in tree_records.items():
        rect = node_rects[object_id]
        x, y, width, height = pixel_rect(rect)
        serialized_record = {
            **record,
            "x": x,
            "y": y,
            "width": width,
            "height": height,
        }
        content_rect = content_rects.get(object_id)
        if content_rect is not None:
            content_x, content_y, content_width, content_height = pixel_rect(content_rect)
            serialized_record.update(
                {
                    "content_x": content_x,
                    "content_y": content_y,
                    "content_width": content_width,
                    "content_height": content_height,
                }
            )
        tree_nodes.append(serialized_record)
    main_chain_regions = []
    for object_id, level in chain_levels.items():
        rect = node_rects.get(object_id)
        if not rect:
            continue
        x, y, width, height = pixel_rect(rect)
        main_chain_regions.append(
            {
                "level": level,
                "path_id": object_id,
                "name": str(chain[level].get("name", "")),
                "source_json": str(chain[level].get("source_json", "")),
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "chain": [
                    {
                        "level": chain_level,
                        "name": str(node.get("name", "")),
                        "path_id": int(node.get("path_id", 0) or 0),
                    }
                    for chain_level, node in enumerate(
                        chain[level:root_level + 1],
                        start=level,
                    )
                    if isinstance(node, dict)
                ],
            }
        )
    metadata_path.write_text(
        json.dumps(
            {
                "image": str(target),
                "source": str(match.get("source", "")),
                "bundle_entry": str(match.get("bundle_entry", "")),
                "root_level": root_level,
                "root_object_id": root_object_id,
                "canvas_width": canvas_width,
                "canvas_height": canvas_height,
                "image_layers": image_layers,
                "tree_nodes": tree_nodes,
                "main_chain": main_chain_regions,
                "regions": regions,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _print_preview_region_chain(metadata: dict, region: dict) -> None:
    print()
    print(
        f"\033[96m[层级预览][选择] {region.get('name', '')} "
        f"(PathID={region.get('path_id', 0)})\033[0m"
    )
    source = str(metadata.get("source", ""))
    bundle_entry = str(metadata.get("bundle_entry", ""))
    if source:
        print(f"  来源文件: {source}")
    if bundle_entry:
        print(f"  Bundle entry: {bundle_entry}")
    if "level" in region:
        print(
            f"  \033[38;5;208m当前屏蔽层级: {region.get('level', '')}. "
            f"{region.get('name', '')} "
            f"(PathID={region.get('path_id', 0)})\033[0m"
        )
    print("  完整对象链（当前层级 -> 上级对象）:")
    chain = region.get("chain")
    if not isinstance(chain, list):
        chain = []
    for depth, node in enumerate(chain):
        if not isinstance(node, dict):
            continue
        print(
            f"    {node.get('level', depth)}. {node.get('name', '')} "
            f"(PathID={node.get('path_id', 0)})"
        )
    print("  链路: " + " -> ".join(
        str(node.get("name", "")) for node in chain if isinstance(node, dict)
    ), flush=True)


def _print_preview_tree_selection(
    metadata: dict,
    selected: dict,
    displayed_nodes: list[dict],
    display_mode: str,
) -> None:
    print()
    print(
        f"\033[96m[层级预览][选择] {selected.get('name', '')} "
        f"(PathID={selected.get('path_id', 0)})\033[0m"
    )
    source = str(metadata.get("source", ""))
    bundle_entry = str(metadata.get("bundle_entry", ""))
    if source:
        print(f"  来源文件: {source}")
    if bundle_entry:
        print(f"  Bundle entry: {bundle_entry}")
    mode_text = "所在链路" if display_mode == "chain" else "以该对象为根的完整子树"
    print(f"  当前显示: {mode_text}，节点数={len(displayed_nodes)}")
    for node in displayed_nodes:
        depth = int(node.get("display_depth", 0) or 0)
        branch = "  " * depth
        print(
            f"    {branch}{depth}. {node.get('name', '')} "
            f"(PathID={node.get('path_id', 0)})"
        )
    print(flush=True)


def _preview_subtree_nodes(tree_by_id: dict[int, dict], root_path_id: int) -> list[dict]:
    result: list[dict] = []
    visited_ids: set[int] = set()

    def walk(path_id: int, depth: int) -> None:
        if path_id in visited_ids:
            return
        node = tree_by_id.get(path_id)
        if not node:
            return
        visited_ids.add(path_id)
        result.append({**node, "display_depth": depth})
        children = node.get("children")
        if not isinstance(children, list):
            return
        for child_path_id in children:
            walk(int(child_path_id or 0), depth + 1)

    walk(root_path_id, 0)
    return result


def _preview_display_nodes(
    tree_by_id: dict[int, dict],
    selected_path_id: int,
) -> tuple[list[dict], str]:
    selected = tree_by_id.get(selected_path_id)
    if not selected:
        return ([], "subtree")
    children = selected.get("children")
    if isinstance(children, list) and children:
        return (_preview_subtree_nodes(tree_by_id, selected_path_id), "subtree")
    chain_items = selected.get("chain")
    if not isinstance(chain_items, list):
        chain_items = []
    result = []
    for depth, chain_node in enumerate(chain_items):
        if not isinstance(chain_node, dict):
            continue
        node = tree_by_id.get(int(chain_node.get("path_id", 0) or 0))
        if node:
            result.append({**node, "display_depth": depth})
    return (result, "chain")


def _preview_nodes_crop_box(
    nodes: list[dict],
    width: int,
    height: int,
    padding: int = 20,
) -> tuple[int, int, int, int]:
    if not nodes:
        return (0, 0, width, height)

    def bounds(node: dict) -> tuple[int, int, int, int]:
        if all(
            key in node
            for key in ("content_x", "content_y", "content_width", "content_height")
        ):
            return (
                int(node.get("content_x", 0) or 0),
                int(node.get("content_y", 0) or 0),
                max(1, int(node.get("content_width", 1) or 1)),
                max(1, int(node.get("content_height", 1) or 1)),
            )
        return (
            int(node.get("x", 0) or 0),
            int(node.get("y", 0) or 0),
            max(1, int(node.get("width", 1) or 1)),
            max(1, int(node.get("height", 1) or 1)),
        )

    node_bounds = [bounds(node) for node in nodes]
    left = max(0, min(value[0] for value in node_bounds) - padding)
    top = max(0, min(value[1] for value in node_bounds) - padding)
    right = min(
        width,
        max(value[0] + value[2] for value in node_bounds) + padding,
    )
    bottom = min(
        height,
        max(value[1] + value[3] for value in node_bounds) + padding,
    )
    if right <= left or bottom <= top:
        return (0, 0, width, height)
    return (left, top, right, bottom)


def _preview_image_nodes_for_display(
    tree_by_id: dict[int, dict],
    displayed_nodes: list[dict],
    display_mode: str,
) -> list[dict]:
    if display_mode != "chain" or not displayed_nodes:
        return displayed_nodes
    chain_root_path_id = int(displayed_nodes[0].get("path_id", 0) or 0)
    return _preview_subtree_nodes(tree_by_id, chain_root_path_id)


def _preview_image_nodes_for_isolated_level(
    tree_by_id: dict[int, dict],
    image_nodes: list[dict],
    selected_path_id: int,
) -> list[dict]:
    """Keep the selected branch while hiding peer branches at/below its depth."""
    selected = next(
        (
            node
            for node in image_nodes
            if int(node.get("path_id", 0) or 0) == selected_path_id
        ),
        tree_by_id.get(selected_path_id),
    )
    if not isinstance(selected, dict):
        return image_nodes
    selected_depth = int(
        selected.get(
            "display_depth",
            max(0, len(selected.get("chain", [])) - 1)
            if isinstance(selected.get("chain"), list)
            else 0,
        )
        or 0
    )
    selected_subtree_ids = {
        int(node.get("path_id", 0) or 0)
        for node in _preview_subtree_nodes(tree_by_id, selected_path_id)
    }
    result: list[dict] = []
    for node in image_nodes:
        path_id = int(node.get("path_id", 0) or 0)
        depth = int(
            node.get(
                "display_depth",
                max(0, len(node.get("chain", [])) - 1)
                if isinstance(node.get("chain"), list)
                else 0,
            )
            or 0
        )
        if depth < selected_depth or path_id in selected_subtree_ids:
            result.append(node)
    return result


def _normalized_preview_scope_value(value: object) -> str:
    return str(value or "").replace("/", "\\").strip().lower()


def _blocked_object_rows(records: dict) -> list[dict]:
    items = records.get("items") if isinstance(records, dict) else None
    if not isinstance(items, list):
        return []
    rows: list[dict] = []
    seen: set[tuple[str, str, int]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        path_id = int(item.get("game_object_path_id", 0) or 0)
        if not path_id:
            continue
        source = _normalized_preview_scope_value(item.get("source_resource", ""))
        bundle_entry = _normalized_preview_scope_value(item.get("bundle_entry", ""))
        source_json = _normalized_preview_scope_value(item.get("source_json", ""))
        key = (source_json or source, "" if source_json else bundle_entry, path_id)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "source": source,
                "source_display": str(item.get("source_resource", "")),
                "bundle_entry": bundle_entry,
                "bundle_display": str(item.get("bundle_entry", "")),
                "source_json": source_json,
                "path_id": path_id,
                "name": str(item.get("game_object_name", "")),
                "record": item,
            }
        )
    return rows


def _blocked_path_ids_for_preview_scope(
    records: dict,
    source: object,
    bundle_entry: object,
) -> set[int]:
    normalized_source = _normalized_preview_scope_value(source)
    normalized_bundle = _normalized_preview_scope_value(bundle_entry)
    return {
        int(row["path_id"])
        for row in _blocked_object_rows(records)
        if row["source"] == normalized_source
        and row["bundle_entry"] == normalized_bundle
    }


def _preview_blocked_row(
    node: dict,
    blocked_rows: list[dict],
    source: object,
    bundle_entry: object,
) -> dict | None:
    node_path_id = int(node.get("path_id", 0) or 0)
    node_source_json = _normalized_preview_scope_value(node.get("source_json", ""))
    if node_source_json:
        return next(
            (
                row
                for row in blocked_rows
                if row.get("source_json") == node_source_json
                and int(row.get("path_id", 0) or 0) == node_path_id
            ),
            None,
        )
    normalized_source = _normalized_preview_scope_value(source)
    normalized_bundle = _normalized_preview_scope_value(bundle_entry)
    return next(
        (
            row
            for row in blocked_rows
            if row.get("source") == normalized_source
            and row.get("bundle_entry") == normalized_bundle
            and int(row.get("path_id", 0) or 0) == node_path_id
        ),
        None,
    )


def _is_preview_node_blocked(
    node: dict,
    blocked_rows: list[dict],
    source: object,
    bundle_entry: object,
) -> bool:
    return _preview_blocked_row(node, blocked_rows, source, bundle_entry) is not None


def _preview_hierarchy_overlay_text(
    region: dict,
    *,
    visible: bool,
    max_depth: int | None = None,
    selected: bool = False,
    blocked: bool = False,
) -> str:
    if not _preview_hierarchy_overlay_is_visible(
        region,
        visible=visible,
        max_depth=max_depth,
    ):
        return ""
    depth = int(region.get("display_depth", 0) or 0)
    name = str(region.get("name", ""))
    if selected:
        return f"已选择层级 {depth}: {name}"
    blocked_prefix = "已屏蔽 " if blocked else ""
    return f"{blocked_prefix}层级 {depth}: {name}"


def _preview_hierarchy_overlay_is_visible(
    region: dict,
    *,
    visible: bool,
    max_depth: int | None = None,
    hidden_path_ids: set[int] | None = None,
) -> bool:
    if not visible:
        return False
    depth = int(region.get("display_depth", 0) or 0)
    if max_depth is not None and depth > max_depth:
        return False
    path_id = int(region.get("path_id", 0) or 0)
    return not hidden_path_ids or path_id not in hidden_path_ids


def _preview_hierarchy_text_depth_limit(value: object) -> int | None:
    raw = str(value or "").strip().lower()
    if raw in {"", "全部", "all", "不限", "无限"}:
        return None
    try:
        return max(0, int(raw))
    except ValueError:
        return None


def _preview_visible_tree_nodes(
    nodes: list[dict],
    collapsed_path_ids: set[int],
) -> list[dict]:
    visible: list[dict] = []
    node_ids = {int(node.get("path_id", 0) or 0) for node in nodes}
    for node in nodes:
        chain = node.get("chain")
        if not isinstance(chain, list):
            chain = []
        ancestor_ids = [
            int(item.get("path_id", 0) or 0)
            for item in chain[:-1]
            if isinstance(item, dict)
            and int(item.get("path_id", 0) or 0) in node_ids
        ]
        if any(path_id in collapsed_path_ids for path_id in ancestor_ids):
            continue
        visible.append(node)
    return visible


def _preview_nodes_at_point(nodes: list[dict], x: float, y: float) -> list[dict]:
    hits: list[dict] = []
    for node in nodes:
        left = _number(node.get("x"))
        top = _number(node.get("y"))
        width = max(0.0, _number(node.get("width")))
        height = max(0.0, _number(node.get("height")))
        if left <= x <= left + width and top <= y <= top + height:
            hits.append(node)
    return sorted(
        hits,
        key=lambda node: (
            -int(node.get("display_depth", 0) or 0),
            max(0.0, _number(node.get("width")))
            * max(0.0, _number(node.get("height"))),
        ),
    )


def _show_interactive_object_preview(target: Path) -> dict:
    import tkinter as tk
    from tkinter import messagebox, ttk
    from PIL import Image, ImageTk

    metadata = _safe_read_json(target.with_suffix(".regions.json"))
    if not isinstance(metadata, dict):
        raise ValueError("预览点击区域数据不存在或无效")
    main_chain = metadata.get("main_chain")
    if not isinstance(main_chain, list):
        main_chain = []
    tree_nodes = metadata.get("tree_nodes")
    if not isinstance(tree_nodes, list):
        tree_nodes = []
    tree_by_id = {
        int(node.get("path_id", 0) or 0): node
        for node in tree_nodes
        if isinstance(node, dict) and int(node.get("path_id", 0) or 0)
    }
    blocked_records = _load_block_records()
    blocked_rows = _blocked_object_rows(blocked_records)

    def is_blocked_node(node: dict) -> bool:
        return _is_preview_node_blocked(
            node,
            blocked_rows,
            metadata.get("source", ""),
            metadata.get("bundle_entry", ""),
        )
    image_layers = metadata.get("image_layers")
    if not isinstance(image_layers, list):
        image_layers = []
    current_display: dict[str, object] = {
        "nodes": [],
        "image_nodes": [],
        "mode": "subtree",
        "isolated_image_path_id": 0,
    }
    visible_tree_nodes: dict[str, list[dict]] = {"value": []}
    collapsed_tree_path_ids: set[int] = set()
    hidden_overlay_path_ids: set[int] = set()

    window = tk.Tk()
    window.title(f"对象层级预览 - {target.name}（左侧选择；右键管理层级）")
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    source_image = Image.open(target).convert("RGB")
    side_width = 300
    view_width = min(source_image.width, max(640, screen_width - side_width - 140))
    view_height = min(source_image.height, max(480, screen_height - 180))
    window.geometry(f"{view_width + side_width}x{view_height + 34}")

    frame = ttk.Frame(window)
    frame.pack(fill="both", expand=True)
    split_view = tk.PanedWindow(
        frame,
        orient="horizontal",
        sashwidth=7,
        sashrelief="raised",
        background="#30343d",
        borderwidth=0,
    )
    split_view.pack(fill="both", expand=True)
    side_panel = tk.Frame(split_view, background="#12151b", width=side_width)
    preview_panel = ttk.Frame(split_view)
    split_view.add(side_panel, minsize=220, width=side_width, stretch="never")
    split_view.add(preview_panel, minsize=400, stretch="always")

    canvas = tk.Canvas(preview_panel, background="#1c1f26", highlightthickness=0)
    horizontal = ttk.Scrollbar(preview_panel, orient="horizontal", command=canvas.xview)
    vertical = ttk.Scrollbar(preview_panel, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    preview_panel.rowconfigure(0, weight=1)
    preview_panel.columnconfigure(0, weight=1)
    image_item = canvas.create_image(0, 0, anchor="nw")

    status = tk.StringVar(
        value="左侧选择层级；右键仅显示层级时会同时隐藏其它同级分支及其下级图片"
    )
    ttk.Label(window, textvariable=status, anchor="w").pack(fill="x")
    selected_region: dict[str, dict | None] = {"value": None}
    action_result = {"blocked": False, "path_id": 0, "name": ""}
    view_origin: dict[str, int] = {"x": 0, "y": 0}
    zoom: dict[str, float] = {
        "value": max(
            0.2,
            min(1.0, view_width / source_image.width, view_height / source_image.height),
        )
    }
    zoom_levels = (0.2, 0.25, 0.33, 0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
    zoom_text = tk.StringVar()
    hierarchy_text_visible: dict[str, bool] = {"value": True}
    hierarchy_text_button_text = tk.StringVar(value="隐藏层级标注")
    hierarchy_text_depth_text = tk.StringVar(value="全部")
    hidden_overlay_count_text = tk.StringVar(value="当前隐藏标注：0")
    preview_photo = None
    image_cache: dict[float, object] = {}

    tk.Label(
        side_panel,
        text="层级树浏览器",
        foreground="#43e081",
        background="#12151b",
        font=("SimSun", 12, "bold"),
        anchor="w",
        padx=10,
        pady=10,
    ).pack(fill="x")
    zoom_panel = tk.Frame(side_panel, background="#12151b")
    zoom_panel.pack(fill="x", padx=10, pady=(0, 10))
    overlay_control_panel = tk.Frame(side_panel, background="#12151b")
    overlay_control_panel.pack(fill="x", padx=10, pady=(0, 10))

    tk.Label(
        side_panel,
        text="进入时所在的层级链路",
        foreground="#43e081",
        background="#12151b",
        font=("SimSun", 11, "bold"),
        anchor="w",
    ).pack(fill="x", padx=10)
    entry_list_frame = tk.Frame(side_panel, background="#12151b")
    entry_list_frame.pack(fill="x", padx=10, pady=(3, 10))
    entry_list = tk.Listbox(
        entry_list_frame,
        foreground="#43e081",
        background="#1c1f26",
        selectforeground="#ff9f43",
        selectbackground="#292016",
        activestyle="none",
        font=("SimSun", 12, "bold"),
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#43e081",
        exportselection=False,
        height=max(3, min(7, len(main_chain))),
    )
    entry_scrollbar = ttk.Scrollbar(
        entry_list_frame,
        orient="vertical",
        command=entry_list.yview,
    )
    entry_list.configure(yscrollcommand=entry_scrollbar.set)
    entry_list.pack(side="left", fill="x", expand=True)
    entry_scrollbar.pack(side="right", fill="y")
    for region in main_chain:
        path_id = int(region.get("path_id", 0) or 0)
        entry_list.insert(
            "end",
            f"入口 {region.get('level', '')}  {region.get('name', '')}",
        )
        if is_blocked_node(region):
            item_index = entry_list.size() - 1
            entry_list.itemconfigure(
                item_index,
                foreground="#ff5c5c",
                selectforeground="#ff5c5c",
            )

    tk.Label(
        side_panel,
        text="当前显示的层级链路（树）",
        foreground="#43e081",
        background="#12151b",
        font=("SimSun", 11, "bold"),
        anchor="w",
    ).pack(fill="x", padx=10)
    tree_list_frame = tk.Frame(side_panel, background="#12151b")
    tree_list_frame.pack(fill="both", expand=True, padx=10, pady=(3, 10))
    tree_list = tk.Listbox(
        tree_list_frame,
        foreground="#43e081",
        background="#1c1f26",
        selectforeground="#ff9f43",
        selectbackground="#292016",
        activestyle="none",
        font=("SimSun", 11, "bold"),
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#43e081",
        exportselection=False,
    )
    tree_scrollbar = ttk.Scrollbar(
        tree_list_frame,
        orient="vertical",
        command=tree_list.yview,
    )
    tree_list.configure(yscrollcommand=tree_scrollbar.set)
    tree_list.pack(side="left", fill="both", expand=True)
    tree_scrollbar.pack(side="right", fill="y")

    def display_nodes_for(path_id: int) -> tuple[list[dict], str]:
        return _preview_display_nodes(tree_by_id, path_id)

    def populate_tree_list(nodes: list[dict]) -> None:
        tree_list.delete(0, "end")
        displayed_node_ids = {int(node.get("path_id", 0) or 0) for node in nodes}
        visible_nodes = _preview_visible_tree_nodes(nodes, collapsed_tree_path_ids)
        visible_tree_nodes["value"] = visible_nodes
        for node in visible_nodes:
            depth = int(node.get("display_depth", 0) or 0)
            path_id = int(node.get("path_id", 0) or 0)
            children = node.get("children")
            has_visible_children = isinstance(children, list) and any(
                int(child_path_id or 0) in displayed_node_ids
                for child_path_id in children
            )
            marker = (
                "▸ "
                if has_visible_children and path_id in collapsed_tree_path_ids
                else "▾ "
                if has_visible_children
                else "└─ "
                if depth
                else "• "
            )
            node_is_blocked = is_blocked_node(node)
            blocked_prefix = "[已屏蔽] " if node_is_blocked else ""
            overlay_hidden = path_id in hidden_overlay_path_ids
            hidden_prefix = "[标注隐藏] " if overlay_hidden else ""
            tree_list.insert(
                "end",
                f"{'  ' * depth}{marker}{depth}. {hidden_prefix}{blocked_prefix}{node.get('name', '')}",
            )
            item_index = tree_list.size() - 1
            if node_is_blocked:
                tree_list.itemconfigure(
                    item_index,
                    foreground="#ff5c5c",
                    selectforeground="#ff5c5c",
                )
            elif overlay_hidden:
                tree_list.itemconfigure(
                    item_index,
                    foreground="#7f8794",
                    selectforeground="#ff9f43",
                )

    def draw_main_chain_overlays() -> None:
        canvas.delete("preview_main_chain")
        factor = zoom["value"]
        displayed_nodes = current_display.get("nodes")
        if not isinstance(displayed_nodes, list):
            displayed_nodes = []
        for region in displayed_nodes:
            path_id = int(region.get("path_id", 0) or 0)
            node_is_blocked = is_blocked_node(region)
            text_color = "#ff5c5c" if node_is_blocked else "#43e081"
            max_depth = _preview_hierarchy_text_depth_limit(
                hierarchy_text_depth_text.get()
            )
            if not _preview_hierarchy_overlay_is_visible(
                region,
                visible=hierarchy_text_visible["value"],
                max_depth=max_depth,
                hidden_path_ids=hidden_overlay_path_ids,
            ):
                continue
            left = (_number(region.get("x")) - view_origin["x"]) * factor
            top = (_number(region.get("y")) - view_origin["y"]) * factor
            right = left + _number(region.get("width")) * factor
            bottom = top + _number(region.get("height")) * factor
            canvas.create_rectangle(
                left,
                top,
                right,
                bottom,
                outline="#43e081",
                width=3,
                tags="preview_main_chain",
            )
            display_depth = int(region.get("display_depth", 0) or 0)
            text = _preview_hierarchy_overlay_text(
                region,
                visible=hierarchy_text_visible["value"],
                max_depth=max_depth,
                blocked=node_is_blocked,
            )
            text_id = canvas.create_text(
                left + 5,
                top + 5 + display_depth * 22,
                anchor="nw",
                text=text,
                fill=text_color,
                font=("SimSun", 14, "bold"),
                tags="preview_main_chain",
            )
            text_box = canvas.bbox(text_id)
            if text_box:
                background_id = canvas.create_rectangle(
                    text_box[0] - 4,
                    text_box[1] - 3,
                    text_box[2] + 4,
                    text_box[3] + 3,
                    fill="#0c0e12",
                    outline=text_color,
                    width=1,
                    tags="preview_main_chain",
                )
                canvas.tag_lower(background_id, text_id)

    def show_selected_region(region: dict) -> None:
        selected_region["value"] = region
        canvas.delete("preview_selected")
        max_depth = _preview_hierarchy_text_depth_limit(
            hierarchy_text_depth_text.get()
        )
        if not _preview_hierarchy_overlay_is_visible(
            region,
            visible=hierarchy_text_visible["value"],
            max_depth=max_depth,
            hidden_path_ids=hidden_overlay_path_ids,
        ):
            return
        factor = zoom["value"]
        left = (_number(region.get("x")) - view_origin["x"]) * factor
        top = (_number(region.get("y")) - view_origin["y"]) * factor
        right = left + _number(region.get("width")) * factor
        bottom = top + _number(region.get("height")) * factor
        canvas.create_rectangle(
            left,
            top,
            right,
            bottom,
            outline="#ff9f43",
            width=4,
            tags="preview_selected",
        )
        selected_text = _preview_hierarchy_overlay_text(
            region,
            visible=hierarchy_text_visible["value"],
            max_depth=max_depth,
            selected=True,
        )
        text_id = canvas.create_text(
            left + 5,
            top + 5,
            anchor="nw",
            text=selected_text,
            fill="#ff9f43",
            font=("SimSun", 14, "bold"),
            tags="preview_selected",
        )
        text_box = canvas.bbox(text_id)
        if text_box:
            background_id = canvas.create_rectangle(
                text_box[0] - 4,
                text_box[1] - 3,
                text_box[2] + 4,
                text_box[3] + 3,
                fill="#0c0e12",
                outline="#ff9f43",
                width=1,
                tags="preview_selected",
            )
            canvas.tag_lower(background_id, text_id)

    def toggle_hierarchy_text() -> None:
        hierarchy_text_visible["value"] = not hierarchy_text_visible["value"]
        hierarchy_text_button_text.set(
            "隐藏层级标注" if hierarchy_text_visible["value"] else "显示层级标注"
        )
        draw_main_chain_overlays()
        if selected_region["value"] is not None:
            show_selected_region(selected_region["value"])
        status.set(
            "预览层级文字和边框已显示"
            if hierarchy_text_visible["value"]
            else "预览层级文字和边框已隐藏"
        )

    def refresh_hierarchy_text_depth(_event=None) -> None:
        draw_main_chain_overlays()
        if selected_region["value"] is not None:
            show_selected_region(selected_region["value"])
        limit = _preview_hierarchy_text_depth_limit(hierarchy_text_depth_text.get())
        status.set(
            "层级文字和边框深度：全部"
            if limit is None
            else f"层级文字和边框深度：当前层级及其下 {limit} 层"
        )

    def refresh_overlay_visibility_ui(message: str = "") -> None:
        displayed_nodes = current_display.get("nodes")
        if not isinstance(displayed_nodes, list):
            displayed_nodes = []
        selected_path_id = 0
        if isinstance(selected_region["value"], dict):
            selected_path_id = int(selected_region["value"].get("path_id", 0) or 0)
        populate_tree_list(displayed_nodes)
        selected_index = next(
            (
                index
                for index, node in enumerate(visible_tree_nodes["value"])
                if int(node.get("path_id", 0) or 0) == selected_path_id
            ),
            None,
        )
        if selected_index is not None:
            tree_list.selection_set(selected_index)
            tree_list.see(selected_index)
        hidden_overlay_count_text.set(
            f"当前隐藏标注：{len(hidden_overlay_path_ids)}"
        )
        draw_main_chain_overlays()
        if selected_region["value"] is not None:
            show_selected_region(selected_region["value"])
        if message:
            status.set(message)

    def only_show_overlay(region: dict) -> None:
        path_id = int(region.get("path_id", 0) or 0)
        displayed_nodes = current_display.get("nodes")
        image_nodes = current_display.get("image_nodes")
        if (
            not path_id
            or not isinstance(displayed_nodes, list)
            or not isinstance(image_nodes, list)
        ):
            return
        hidden_overlay_path_ids.clear()
        hidden_overlay_path_ids.update(
            int(node.get("path_id", 0) or 0)
            for node in displayed_nodes
            if int(node.get("path_id", 0) or 0) != path_id
        )
        isolated_image_nodes = _preview_image_nodes_for_isolated_level(
            tree_by_id,
            image_nodes,
            path_id,
        )
        current_display["isolated_image_path_id"] = path_id
        reload_image_for_nodes(displayed_nodes, isolated_image_nodes)
        refresh_overlay_visibility_ui(
            f"仅显示层级：{region.get('name', '')} (PathID={path_id})；"
            "已隐藏其它同级分支及其下级图片"
        )

    def hide_only_overlay(region: dict) -> None:
        path_id = int(region.get("path_id", 0) or 0)
        if not path_id:
            return
        hidden_overlay_path_ids.add(path_id)
        refresh_overlay_visibility_ui(
            f"已隐藏层级标注：{region.get('name', '')} (PathID={path_id})"
        )

    def restore_overlay(region: dict) -> None:
        path_id = int(region.get("path_id", 0) or 0)
        hidden_overlay_path_ids.discard(path_id)
        refresh_overlay_visibility_ui(
            f"已恢复层级标注：{region.get('name', '')} (PathID={path_id})"
        )

    def restore_selected_overlay() -> None:
        region = selected_region["value"]
        if isinstance(region, dict):
            restore_overlay(region)

    def restore_all_overlays() -> None:
        hidden_overlay_path_ids.clear()
        displayed_nodes = current_display.get("nodes")
        image_nodes = current_display.get("image_nodes")
        if isinstance(displayed_nodes, list) and isinstance(image_nodes, list):
            reload_image_for_nodes(displayed_nodes, image_nodes)
        current_display["isolated_image_path_id"] = 0
        refresh_overlay_visibility_ui("已恢复全部层级标注和图片")

    def show_resource_names(region: dict) -> None:
        detail = _preview_resource_name_text(region)
        if not detail:
            status.set("该层级没有可查询的 Sprite、NGUI Sprite 或 Texture2D 资源")
            return
        object_name = str(region.get("name", ""))
        status.set(f"已查询图片资源名字：{object_name}")
        print(
            f"\n\033[96m[层级预览][资源名字] {object_name} "
            f"(Object PathID={region.get('path_id', 0)})\033[0m\n{detail}",
            flush=True,
        )
        _show_copyable_text_dialog(
            window,
            "Sprite / NGUI / Texture2D 资源名字",
            f"对象层级: {object_name}\n\n{detail}",
        )

    def set_zoom(value: float, keep_center: bool = True) -> None:
        nonlocal preview_photo
        old_zoom = zoom["value"]
        center_x = canvas.canvasx(max(1, canvas.winfo_width()) / 2) / old_zoom
        center_y = canvas.canvasy(max(1, canvas.winfo_height()) / 2) / old_zoom
        zoom["value"] = max(0.2, min(4.0, round(value, 4)))
        scaled_width = max(1, round(source_image.width * zoom["value"]))
        scaled_height = max(1, round(source_image.height * zoom["value"]))
        cache_key = zoom["value"]
        preview_photo = image_cache.get(cache_key)
        if preview_photo is None:
            if cache_key == 1.0:
                resized = source_image
            else:
                resized = source_image.resize(
                    (scaled_width, scaled_height),
                    Image.Resampling.BILINEAR,
                )
            preview_photo = ImageTk.PhotoImage(resized)
            image_cache[cache_key] = preview_photo
            if len(image_cache) > 4:
                for old_key in list(image_cache):
                    if old_key != cache_key:
                        del image_cache[old_key]
                        break
        canvas.itemconfigure(image_item, image=preview_photo)
        canvas.configure(scrollregion=(0, 0, scaled_width, scaled_height))
        zoom_text.set(f"{round(zoom['value'] * 100)}%")
        draw_main_chain_overlays()
        if selected_region["value"] is not None:
            show_selected_region(selected_region["value"])
        if keep_center:
            canvas.update_idletasks()
            visible_width = max(1, canvas.winfo_width())
            visible_height = max(1, canvas.winfo_height())
            target_left = center_x * zoom["value"] - visible_width / 2
            target_top = center_y * zoom["value"] - visible_height / 2
            canvas.xview_moveto(max(0.0, target_left / max(1, scaled_width)))
            canvas.yview_moveto(max(0.0, target_top / max(1, scaled_height)))

    def reload_image_for_nodes(
        nodes: list[dict],
        image_nodes: list[dict] | None = None,
    ) -> None:
        nonlocal source_image, preview_photo
        if image_nodes is None:
            image_nodes = nodes
        allowed_path_ids = {
            int(node.get("path_id", 0) or 0)
            for node in image_nodes
            if isinstance(node, dict)
        }
        width = int(metadata.get("canvas_width", source_image.width) or source_image.width)
        height = int(metadata.get("canvas_height", source_image.height) or source_image.height)
        crop_left, crop_top, crop_right, crop_bottom = _preview_nodes_crop_box(
            nodes,
            width,
            height,
        )
        recomposed = Image.new(
            "RGBA",
            (max(1, crop_right - crop_left), max(1, crop_bottom - crop_top)),
            (28, 31, 38, 255),
        )
        for layer in image_layers:
            if not isinstance(layer, dict):
                continue
            if int(layer.get("path_id", 0) or 0) not in allowed_path_ids:
                continue
            layer_path = Path(str(layer.get("image", "")))
            try:
                with Image.open(layer_path) as opened_layer:
                    layer_image = opened_layer.convert("RGBA")
            except OSError:
                continue
            recomposed.alpha_composite(
                layer_image,
                (
                    int(layer.get("x", 0) or 0) - crop_left,
                    int(layer.get("y", 0) or 0) - crop_top,
                ),
            )
        view_origin["x"] = crop_left
        view_origin["y"] = crop_top
        source_image = recomposed.convert("RGB")
        image_cache.clear()
        preview_photo = None
        fit_to_window()

    def fit_to_window() -> None:
        canvas.update_idletasks()
        fit = min(
            1.0,
            max(1, canvas.winfo_width()) / source_image.width,
            max(1, canvas.winfo_height()) / source_image.height,
        )
        set_zoom(fit, keep_center=False)
        canvas.xview_moveto(0)
        canvas.yview_moveto(0)

    def step_zoom(direction: int) -> None:
        if direction > 0:
            candidates = [value for value in zoom_levels if value > zoom["value"] + 0.001]
            target_zoom = candidates[0] if candidates else zoom_levels[-1]
        else:
            candidates = [value for value in zoom_levels if value < zoom["value"] - 0.001]
            target_zoom = candidates[-1] if candidates else zoom_levels[0]
        set_zoom(target_zoom)

    def on_mouse_wheel(event) -> str:
        step_zoom(1 if event.delta > 0 else -1)
        return "break"

    def on_list_mouse_wheel(listbox, event) -> str:
        listbox.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    ttk.Button(
        zoom_panel,
        text="−",
        width=3,
        command=lambda: step_zoom(-1),
    ).pack(side="left")
    ttk.Label(zoom_panel, textvariable=zoom_text, width=7, anchor="center").pack(side="left", padx=4)
    ttk.Button(
        zoom_panel,
        text="+",
        width=3,
        command=lambda: step_zoom(1),
    ).pack(side="left")
    ttk.Button(zoom_panel, text="适应窗口", command=fit_to_window).pack(side="left", padx=(8, 0))
    ttk.Button(
        overlay_control_panel,
        textvariable=hierarchy_text_button_text,
        command=toggle_hierarchy_text,
    ).pack(fill="x", pady=(0, 5))
    hierarchy_depth_panel = tk.Frame(overlay_control_panel, background="#12151b")
    hierarchy_depth_panel.pack(fill="x")
    tk.Label(
        hierarchy_depth_panel,
        text="标注向下层级（当前=0）：",
        foreground="#a8adb8",
        background="#12151b",
        anchor="w",
    ).pack(side="left")
    hierarchy_depth_input = ttk.Combobox(
        hierarchy_depth_panel,
        textvariable=hierarchy_text_depth_text,
        values=("全部", *[str(value) for value in range(0, 21)]),
        width=6,
    )
    hierarchy_depth_input.pack(side="right")
    hierarchy_depth_input.bind("<<ComboboxSelected>>", refresh_hierarchy_text_depth)
    hierarchy_depth_input.bind("<KeyRelease>", refresh_hierarchy_text_depth)
    tk.Label(
        overlay_control_panel,
        textvariable=hidden_overlay_count_text,
        foreground="#7f8794",
        background="#12151b",
        anchor="w",
    ).pack(fill="x", pady=(5, 3))
    overlay_restore_panel = tk.Frame(overlay_control_panel, background="#12151b")
    overlay_restore_panel.pack(fill="x")
    ttk.Button(
        overlay_restore_panel,
        text="恢复所选标注",
        command=restore_selected_overlay,
    ).pack(side="left", fill="x", expand=True, padx=(0, 3))
    ttk.Button(
        overlay_restore_panel,
        text="恢复全部标注和图片",
        command=restore_all_overlays,
    ).pack(side="left", fill="x", expand=True, padx=(3, 0))

    refreshing_tree: dict[str, bool] = {"value": False}

    def refresh_for_path_id(path_id: int) -> None:
        region = tree_by_id.get(path_id)
        if not region:
            return
        nodes, mode = display_nodes_for(path_id)
        image_nodes = _preview_image_nodes_for_display(tree_by_id, nodes, mode)
        current_display["nodes"] = nodes
        current_display["image_nodes"] = image_nodes
        current_display["mode"] = mode
        current_display["isolated_image_path_id"] = 0
        selected_copy = next(
            (node for node in nodes if int(node.get("path_id", 0) or 0) == path_id),
            {**region, "display_depth": 0},
        )
        selected_chain = selected_copy.get("chain")
        if not isinstance(selected_chain, list):
            selected_chain = []
        selected_chain_ids = {
            int(item.get("path_id", 0) or 0)
            for item in selected_chain
            if isinstance(item, dict)
        }
        displayed_ids = {int(node.get("path_id", 0) or 0) for node in nodes}
        collapsed_tree_path_ids.clear()
        collapsed_tree_path_ids.update(
            int(node.get("path_id", 0) or 0)
            for node in nodes
            if int(node.get("display_depth", 0) or 0) > 0
            and int(node.get("path_id", 0) or 0) not in selected_chain_ids
            and isinstance(node.get("children"), list)
            and any(
                int(child_path_id or 0) in displayed_ids
                for child_path_id in node.get("children", [])
            )
        )
        refreshing_tree["value"] = True
        populate_tree_list(nodes)
        selected_index = next(
            (
                index
                for index, node in enumerate(visible_tree_nodes["value"])
                if int(node.get("path_id", 0) or 0) == path_id
            ),
            0,
        )
        if visible_tree_nodes["value"]:
            tree_list.selection_set(selected_index)
            tree_list.see(selected_index)
        window.after_idle(lambda: refreshing_tree.__setitem__("value", False))
        reload_image_for_nodes(nodes, image_nodes)
        draw_main_chain_overlays()
        show_selected_region(selected_copy)
        parent_path_id = int(selected_copy.get("parent_path_id", 0) or 0)
        back_parent_button.configure(state="normal" if parent_path_id else "disabled")
        status.set(
            f"已重新加载：{region.get('name', '')}；"
            f"{'所在链路' if mode == 'chain' else '完整子树'}，"
            f"框选节点={len(nodes)}，图片上下文节点={len(image_nodes)}"
        )
        _print_preview_tree_selection(metadata, selected_copy, nodes, mode)

    def on_entry_selected(_event=None) -> None:
        selection = entry_list.curselection()
        if not selection:
            return
        entry_region = main_chain[int(selection[0])]
        refresh_for_path_id(int(entry_region.get("path_id", 0) or 0))

    def on_tree_selected(_event=None) -> None:
        if refreshing_tree["value"]:
            return
        selection = tree_list.curselection()
        tree_nodes = visible_tree_nodes["value"]
        if not selection:
            return
        index = int(selection[0])
        if index < 0 or index >= len(tree_nodes):
            return
        region = tree_nodes[index]
        refresh_for_path_id(int(region.get("path_id", 0) or 0))

    def tree_region_at_event(event) -> dict | None:
        if not visible_tree_nodes["value"] or tree_list.size() <= 0:
            return None
        index = int(tree_list.nearest(event.y))
        if index < 0 or index >= len(visible_tree_nodes["value"]):
            return None
        return visible_tree_nodes["value"][index]

    def toggle_tree_branch(region: dict) -> None:
        path_id = int(region.get("path_id", 0) or 0)
        displayed_nodes = current_display.get("nodes")
        if not path_id or not isinstance(displayed_nodes, list):
            return
        displayed_ids = {int(node.get("path_id", 0) or 0) for node in displayed_nodes}
        children = region.get("children")
        if not isinstance(children, list) or not any(
            int(child_path_id or 0) in displayed_ids for child_path_id in children
        ):
            return
        if path_id in collapsed_tree_path_ids:
            collapsed_tree_path_ids.remove(path_id)
            action = "展开"
        else:
            collapsed_tree_path_ids.add(path_id)
            action = "折叠"
        refreshing_tree["value"] = True
        populate_tree_list(displayed_nodes)
        window.after_idle(lambda: refreshing_tree.__setitem__("value", False))
        status.set(f"已{action}树枝：{region.get('name', '')}")

    def on_tree_click(event):
        region = tree_region_at_event(event)
        if not isinstance(region, dict):
            return None
        depth = int(region.get("display_depth", 0) or 0)
        if event.x <= 28 + depth * 16:
            toggle_tree_branch(region)
            return "break"
        return None

    def on_tree_context_menu(event) -> str:
        region = tree_region_at_event(event)
        if not isinstance(region, dict):
            return "break"
        menu = tk.Menu(window, tearoff=False)
        blocked = is_blocked_node(region)
        menu.add_command(
            label="屏蔽此层级 Object",
            command=lambda: block_preview_region(region),
            state="disabled" if blocked else "normal",
        )
        menu.add_command(
            label="取消屏蔽此层级 Object",
            command=lambda: unblock_preview_region(region),
            state="normal" if blocked else "disabled",
        )
        menu.add_separator()
        menu.add_command(
            label="仅显示此层级（隐藏其它同级分支及其下级图片）",
            command=lambda: only_show_overlay(region),
        )
        menu.add_command(label="仅隐藏此层级标注", command=lambda: hide_only_overlay(region))
        menu.add_command(label="恢复此层级标注", command=lambda: restore_overlay(region))
        menu.add_separator()
        menu.add_command(label="展开 / 折叠此树枝", command=lambda: toggle_tree_branch(region))
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def on_canvas_context_menu(event) -> str:
        displayed_nodes = current_display.get("nodes")
        if not isinstance(displayed_nodes, list):
            return "break"
        factor = max(0.001, zoom["value"])
        source_x = canvas.canvasx(event.x) / factor + view_origin["x"]
        source_y = canvas.canvasy(event.y) / factor + view_origin["y"]
        hits = _preview_nodes_at_point(displayed_nodes, source_x, source_y)
        if not hits:
            status.set("右键位置没有命中层级框")
            return "break"
        menu = tk.Menu(window, tearoff=False)
        for region in hits[:30]:
            path_id = int(region.get("path_id", 0) or 0)
            depth = int(region.get("display_depth", 0) or 0)
            submenu = tk.Menu(menu, tearoff=False)
            blocked = is_blocked_node(region)
            submenu.add_command(
                label="选择并进入此层级",
                command=lambda item=region: refresh_for_path_id(
                    int(item.get("path_id", 0) or 0)
                ),
            )
            resource_query_label = _preview_resource_query_label(region)
            if resource_query_label:
                submenu.add_command(
                    label=resource_query_label,
                    command=lambda item=region: show_resource_names(item),
                )
                submenu.add_separator()
            submenu.add_command(
                label="屏蔽此层级 Object",
                command=lambda item=region: block_preview_region(item),
                state="disabled" if blocked else "normal",
            )
            submenu.add_command(
                label="取消屏蔽此层级 Object",
                command=lambda item=region: unblock_preview_region(item),
                state="normal" if blocked else "disabled",
            )
            submenu.add_separator()
            submenu.add_command(
                label="仅显示此层级（隐藏其它同级分支及其下级图片）",
                command=lambda item=region: only_show_overlay(item),
            )
            submenu.add_command(
                label="仅隐藏此层级标注",
                command=lambda item=region: hide_only_overlay(item),
            )
            submenu.add_command(
                label="恢复此层级标注",
                command=lambda item=region: restore_overlay(item),
            )
            hidden_suffix = " [标注隐藏]" if path_id in hidden_overlay_path_ids else ""
            menu.add_cascade(
                label=f"层级 {depth}: {region.get('name', '')} | {path_id}{hidden_suffix}",
                menu=submenu,
            )
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def refresh_block_state_ui(region: dict, message: str) -> None:
        refreshed_records = _load_block_records()
        blocked_records.clear()
        blocked_records.update(refreshed_records)
        blocked_rows[:] = _blocked_object_rows(refreshed_records)
        refresh_blocked_list()
        refresh_entry_list_colors()
        displayed_nodes = current_display.get("nodes")
        if isinstance(displayed_nodes, list):
            populate_tree_list(displayed_nodes)
            selected_index = next(
                (
                    index
                    for index, node in enumerate(visible_tree_nodes["value"])
                    if int(node.get("path_id", 0) or 0)
                    == int(region.get("path_id", 0) or 0)
                ),
                None,
            )
            if selected_index is not None:
                tree_list.selection_set(selected_index)
                tree_list.see(selected_index)
        draw_main_chain_overlays()
        show_selected_region(region)
        status.set(message)

    def block_preview_region(region: dict) -> None:
        if is_blocked_node(region):
            status.set(
                f"此层级已经屏蔽：{region.get('name', '')} "
                f"(PathID={region.get('path_id', 0)})"
            )
            return
        saved_chain = [
            {
                "path_id": int(node.get("path_id", 0) or 0),
                "name": str(node.get("name", "")),
                "source_json": (
                    str(region.get("source_json", ""))
                    if int(node.get("path_id", 0) or 0)
                    == int(region.get("path_id", 0) or 0)
                    else ""
                ),
            }
            for node in reversed(region.get("chain", []))
            if isinstance(node, dict)
        ]
        if not saved_chain:
            saved_chain = [
                {
                    "path_id": int(region.get("path_id", 0) or 0),
                    "name": str(region.get("name", "")),
                    "source_json": str(region.get("source_json", "")),
                }
            ]
        synthetic_match = {
            "source": str(metadata.get("source", "")),
            "bundle_entry": str(metadata.get("bundle_entry", "")),
            "chain": saved_chain,
            "preview_root_level": len(saved_chain) - 1,
            "hierarchy_descendants": [
                {
                    "path_id": int(node.get("path_id", 0) or 0),
                    "name": str(node.get("name", "")),
                    "source_json": str(node.get("source_json", "")),
                    "depth": (
                        len(node.get("chain", []))
                        - next(
                            (
                                index for index, chain_node in enumerate(node.get("chain", []))
                                if isinstance(chain_node, dict)
                                and int(chain_node.get("path_id", 0) or 0)
                                == int(region.get("path_id", 0) or 0)
                            ),
                            len(node.get("chain", [])),
                        )
                        - 1
                    ),
                }
                for node in tree_nodes
                if isinstance(node, dict)
                and any(
                    isinstance(chain_node, dict)
                    and int(chain_node.get("path_id", 0) or 0)
                    == int(region.get("path_id", 0) or 0)
                    for chain_node in node.get("chain", [])
                )
                and 0 < (
                    len(node.get("chain", []))
                    - next(
                        (
                            index for index, chain_node in enumerate(node.get("chain", []))
                            if isinstance(chain_node, dict)
                            and int(chain_node.get("path_id", 0) or 0)
                            == int(region.get("path_id", 0) or 0)
                        ),
                        len(node.get("chain", [])),
                    )
                    - 1
                ) <= 3
            ],
        }
        if not _block_game_object(synthetic_match, 0):
            status.set("屏蔽失败，请查看终端错误信息")
            return
        action_result["blocked"] = True
        action_result["path_id"] = int(region.get("path_id", 0) or 0)
        action_result["name"] = str(region.get("name", ""))
        refresh_block_state_ui(
            region,
            f"已实时屏蔽：{region.get('name', '')} "
            f"(PathID={region.get('path_id', 0)})",
        )

    def unblock_preview_region(region: dict) -> None:
        if not is_blocked_node(region):
            status.set(
                f"此层级尚未屏蔽：{region.get('name', '')} "
                f"(PathID={region.get('path_id', 0)})"
            )
            return
        if not _unblock_preview_node(
            region,
            metadata.get("source", ""),
            metadata.get("bundle_entry", ""),
        ):
            status.set("取消屏蔽失败，请查看终端错误信息")
            return
        refresh_block_state_ui(
            region,
            f"已取消屏蔽：{region.get('name', '')} "
            f"(PathID={region.get('path_id', 0)})",
        )

    def return_to_parent_level() -> None:
        region = selected_region["value"]
        if not isinstance(region, dict):
            return
        parent_path_id = int(region.get("parent_path_id", 0) or 0)
        if not parent_path_id:
            back_parent_button.configure(state="disabled")
            status.set("当前已经是预览根层级")
            return
        refresh_for_path_id(parent_path_id)

    def finish_preview() -> None:
        action_result["completed"] = True
        window.destroy()

    blocked_title = tk.StringVar(value=f"已屏蔽 Object（{len(blocked_rows)}）")
    tk.Label(
        side_panel,
        textvariable=blocked_title,
        foreground="#ff5c5c",
        background="#12151b",
        font=("SimSun", 11, "bold"),
        anchor="w",
    ).pack(fill="x", padx=10)
    tk.Label(
        side_panel,
        text="对象身份：来源资源 + Bundle entry + PathID",
        foreground="#a8adb8",
        background="#12151b",
        font=("SimSun", 9),
        anchor="w",
    ).pack(fill="x", padx=10)
    blocked_list_frame = tk.Frame(side_panel, background="#12151b")
    blocked_list_frame.pack(fill="x", padx=10, pady=(3, 10))
    blocked_list = tk.Listbox(
        blocked_list_frame,
        foreground="#ff5c5c",
        background="#1c1f26",
        selectforeground="#ff5c5c",
        selectbackground="#351b1b",
        activestyle="none",
        font=("SimSun", 10, "bold"),
        borderwidth=0,
        highlightthickness=1,
        highlightbackground="#ff5c5c",
        exportselection=False,
        height=max(2, min(5, len(blocked_rows) or 1)),
    )
    blocked_scrollbar = ttk.Scrollbar(
        blocked_list_frame,
        orient="vertical",
        command=blocked_list.yview,
    )
    blocked_list.configure(yscrollcommand=blocked_scrollbar.set)
    blocked_list.pack(side="left", fill="x", expand=True)
    blocked_scrollbar.pack(side="right", fill="y")
    def refresh_blocked_list() -> None:
        blocked_title.set(f"已屏蔽 Object（{len(blocked_rows)}）")
        blocked_list.delete(0, "end")
        if blocked_rows:
            for row in blocked_rows:
                bundle_label = row.get("bundle_display") or "<无 Bundle entry>"
                source_label = str(row.get("source_display", "")).replace("\\", "/")
                blocked_list.insert(
                    "end",
                    f"{source_label}/{bundle_label} | PathID={row.get('path_id', 0)} | {row.get('name', '')}",
                )
        else:
            blocked_list.insert("end", "（暂无已记录的屏蔽对象）")

    def refresh_entry_list_colors() -> None:
        for index, region in enumerate(main_chain):
            blocked = is_blocked_node(region)
            entry_list.itemconfigure(
                index,
                foreground="#ff5c5c" if blocked else "#43e081",
                selectforeground="#ff5c5c" if blocked else "#ff9f43",
            )

    refresh_blocked_list()
    refresh_entry_list_colors()

    action_panel = tk.Frame(side_panel, background="#12151b")
    action_panel.pack(fill="x", padx=10, pady=(0, 10))
    back_parent_button = ttk.Button(
        action_panel,
        text="返回当前层级的上一层级",
        command=return_to_parent_level,
        state="disabled",
    )
    back_parent_button.pack(fill="x", pady=(0, 5))
    ttk.Button(
        action_panel,
        text="完成并进入下一个 Object 链路",
        command=finish_preview,
    ).pack(fill="x")

    entry_list.bind("<<ListboxSelect>>", on_entry_selected)
    entry_list.bind(
        "<MouseWheel>",
        lambda event: on_list_mouse_wheel(entry_list, event),
    )
    tree_list.bind("<<ListboxSelect>>", on_tree_selected)
    tree_list.bind("<Button-1>", on_tree_click)
    tree_list.bind("<Button-3>", on_tree_context_menu)
    tree_list.bind(
        "<MouseWheel>",
        lambda event: on_list_mouse_wheel(tree_list, event),
    )
    canvas.bind("<MouseWheel>", on_mouse_wheel)
    canvas.bind("<ButtonPress-1>", lambda event: canvas.scan_mark(event.x, event.y))
    canvas.bind("<B1-Motion>", lambda event: canvas.scan_dragto(event.x, event.y, gain=1))
    canvas.bind("<Button-3>", on_canvas_context_menu)
    canvas.configure(cursor="fleur")
    window.bind("<Control-plus>", lambda _event: step_zoom(1))
    window.bind("<Control-minus>", lambda _event: step_zoom(-1))
    window.bind("<Key-t>", lambda _event: toggle_hierarchy_text())
    window.bind("<Key-T>", lambda _event: toggle_hierarchy_text())
    root_path_id = int(metadata.get("root_object_id", 0) or 0)
    if root_path_id:
        refresh_for_path_id(root_path_id)
    window.after(0, fit_to_window)
    entry_list.focus_set()
    window.mainloop()
    return action_result


def _open_object_hierarchy_preview(match: dict, root_level: int, scopes: dict) -> bool:
    try:
        target = _create_object_hierarchy_preview(match, root_level, scopes)
    except ImportError:
        print("[层级预览][错误] 当前 Python 环境缺少 Pillow，请先安装 requirements.txt。")
        return False
    except (OSError, ValueError) as exc:
        print(f"[层级预览][错误] {exc}")
        return False
    print(f"[层级预览][完成] {target}")
    print(
        "[层级预览] 左侧可选择/折叠层级；树和图片右键可屏蔽对象、"
        "管理标注或隔离层级图片。"
    )
    try:
        result = _show_interactive_object_preview(target)
        if result.get("completed") or result.get("blocked"):
            blocked_suffix = (
                f"，最后操作 {result.get('name', '')} "
                f"(PathID={result.get('path_id', 0)})"
                if result.get("blocked")
                else ""
            )
            print(
                f"[层级预览] 当前预览操作完成{blocked_suffix}，"
                "继续下一个 Object 链路。"
            )
            return True
    except Exception as exc:
        print(f"[层级预览][提示] 交互窗口打开失败，将使用系统图片查看器: {exc}")
        try:
            os.startfile(target)  # type: ignore[attr-defined]
        except OSError as open_exc:
            print(f"[层级预览][提示] 无法自动打开图片: {open_exc}")
    return False


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


def _block_record_json_location(item: dict) -> str:
    source_json_value = str(item.get("source_json", "")).strip()
    if not source_json_value:
        return "<未记录>"
    source_json = Path(source_json_value)
    try:
        return str(source_json.relative_to(DEFAULT_SOURCE_ROOT))
    except ValueError:
        return str(source_json)


def _restore_block_record_item(item: dict) -> bool:
    source = Path(str(item.get("source_json", "")))
    target = Path(str(item.get("replacement_json", "")))
    source_data = _safe_read_json(source)
    if not isinstance(source_data, dict):
        print(f"[撤销屏蔽][失败] 原始 JSON 不存在或无效: {source}")
        return False
    source_data["m_IsActive"] = int(item.get("original_m_IsActive", 1))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(source_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[撤销屏蔽][完成] {item.get('game_object_name')} "
        f"(PathID={item.get('game_object_path_id')}, "
        f"Bundle entry={item.get('bundle_entry') or '<无>'}) "
        f"-> m_IsActive={source_data['m_IsActive']}"
    )
    return True


def _unblock_preview_node(node: dict, source: object, bundle_entry: object) -> bool:
    records = _load_block_records()
    items = records.get("items")
    if not isinstance(items, list):
        return False
    matched_indexes: list[int] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        rows = _blocked_object_rows({"items": [item]})
        if rows and _is_preview_node_blocked(node, rows, source, bundle_entry):
            matched_indexes.append(index)
    if not matched_indexes:
        print("[撤销屏蔽][失败] 没有找到当前资源路径下的屏蔽记录。")
        return False
    restored_indexes: set[int] = set()
    for index in matched_indexes:
        item = items[index]
        if isinstance(item, dict) and _restore_block_record_item(item):
            restored_indexes.add(index)
    if not restored_indexes:
        return False
    records["items"] = [
        item for index, item in enumerate(items) if index not in restored_indexes
    ]
    _write_block_records(records)
    return True


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
            "hierarchy_chain": match.get("chain", []),
            "hierarchy_descendants": match.get("hierarchy_descendants", []),
            "blocked_level": level,
            "hierarchy_preview_root_level": match.get("preview_root_level", level),
            "component_type": match.get("component_type", "GameObject"),
            "component_path_id": match.get("component_path_id", node["path_id"]),
            "preview_sprite_path_id": match.get("sprite_path_id", 0),
            "preview_texture_path_id": match.get("texture_path_id", 0),
        }
    )
    _write_block_records(records)
    print(f"[屏蔽对象][完成] {node['name']} (PathID={node['path_id']})")
    print(f"[屏蔽对象] 待导入 JSON: {target}")
    return True


def _store_product_pointer_key(file_id: int, path_id: int) -> str:
    return f"{int(file_id)}:{int(path_id)}"


def _is_serialized_pointer(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    keys = {str(key) for key in value}
    return bool(keys & {"m_PathID", "PathID"}) and keys <= {
        "m_FileID", "m_PathID", "FileID", "PathID",
    }


def _store_inline_product_key(value: object, index: int) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"inline:{int(index)}:{digest}"


def _store_array_item_key(value: object, index: int) -> str:
    if _is_serialized_pointer(value):
        file_id, path_id = _pptr(value)
        return f"pointer:{int(index)}:{int(file_id)}:{int(path_id)}"
    return _store_inline_product_key(value, index)


def _store_product_config_key(
    source_json: object,
    path_id: int,
    array_path: object = "_products.Array",
) -> str:
    if isinstance(array_path, (list, tuple)):
        array_path = ".".join(str(part) for part in array_path)
    return f"{Path(str(source_json)).resolve()}|{int(path_id)}|{array_path}"


def _asset_name(scope: dict | None, type_name: str, path_id: int) -> str:
    if not scope or not path_id:
        return ""
    entry = _scope_entry(scope, (type_name,), path_id)
    data = _entry_data(entry) if entry else None
    if isinstance(data, dict):
        return str(data.get("m_Name", "")).strip()
    return ""


def _walk_serialized_arrays(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, list):
                yield path + (str(key),), child
            elif isinstance(child, dict):
                yield from _walk_serialized_arrays(child, path + (str(key),))


def _nested_value(data: dict, path: list[str] | tuple[str, ...]) -> object:
    value: object = data
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _serialized_pointer_assets(value: object, scope: dict, path: tuple[str, ...] = ()) -> list[dict]:
    assets: list[dict] = []
    if isinstance(value, dict):
        if any(key in value for key in ("m_PathID", "PathID")):
            file_id, path_id = _pptr(value)
            target_scope = _resolve_pointer_scope(scope, file_id)
            if target_scope and path_id:
                for type_name in ("Sprite", "Texture2D", "GameObject", "MonoBehaviour"):
                    entry = _scope_entry(target_scope, (type_name,), path_id)
                    if entry:
                        assets.append(
                            {
                                "field_path": ".".join(path),
                                "field_name": path[-1] if path else "",
                                "type_name": type_name,
                                "file_id": file_id,
                                "path_id": path_id,
                                "scope": target_scope,
                                "entry": entry,
                                "name": _asset_name(target_scope, type_name, path_id),
                            }
                        )
                        break
        else:
            for key, child in value.items():
                assets.extend(_serialized_pointer_assets(child, scope, path + (str(key),)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assets.extend(_serialized_pointer_assets(child, scope, path + (str(index),)))
    return assets


STORE_ARRAY_HINTS = (
    "product", "offer", "goods", "item", "skin", "shop", "store",
    "bundle", "package", "catalog", "iap", "purchase", "trade", "sell", "buy",
)
STORE_CONTEXT_HINTS = (
    "shop", "store", "market", "bank", "goods", "iap", "purchase", "offer",
    "trade", "trader", "merchant", "vendor",
)
TASK_LIST_HINTS = (
    "task", "quest", "mission", "achievement", "challenge", "objective",
    "daily", "event",
)
TASK_ENTRY_FIELD_HINTS = (
    "taskid", "questid", "missionid", "achievementid", "objective",
    "progress", "target", "completed", "complete", "claim", "description",
    "reward", "requirement",
)
DYNAMIC_LIST_ARRAY_HINTS = tuple(dict.fromkeys((*STORE_ARRAY_HINTS, *TASK_LIST_HINTS)))
DYNAMIC_LIST_CONTEXT_HINTS = tuple(dict.fromkeys((*STORE_CONTEXT_HINTS, *TASK_LIST_HINTS)))
STORE_COMMERCE_FIELD_HINTS = (
    "productid", "itemid", "templateid", "offerid", "sku", "price", "stock",
    "gemprice", "adamount",
    "_cost", ".cost", "currency",
    "purchase", "iap", "reward", "quantity", "discount", "ads",
    "unlock", "owned", "billing", "receipt", "realmoney", "softcurrency",
)
STORE_IMAGE_FIELD_HINTS = (
    "icon", "sprite", "image", "thumb", "preview", "picture", "art", "texture",
)


def _walk_serialized_strings(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_serialized_strings(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_serialized_strings(child, path + (str(index),))
    elif isinstance(value, str) and value.strip():
        yield path, value.strip()


def _serialized_field_paths(value: object, path: tuple[str, ...] = ()) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            paths.add(".".join(child_path).casefold())
            paths.update(_serialized_field_paths(child, child_path))
    elif isinstance(value, list):
        for child in value:
            paths.update(_serialized_field_paths(child, path))
    return paths


def _product_signal_fields(data: dict) -> list[str]:
    return sorted(
        path for path in _serialized_field_paths(data)
        if any(
            hint in path
            for hint in (*STORE_COMMERCE_FIELD_HINTS, *TASK_ENTRY_FIELD_HINTS)
        )
    )


def _best_product_image(data: dict, scope: dict) -> dict | None:
    """Resolve Unity Sprite, NGUI atlas sprite, or direct Texture2D product art."""
    assets = _serialized_pointer_assets(data, scope)
    candidates: list[dict] = []
    for asset in assets:
        if asset["type_name"] == "Sprite":
            candidates.append({**asset, "image_kind": "sprite", "image_name": asset["name"]})
        elif asset["type_name"] == "Texture2D":
            candidates.append({**asset, "image_kind": "texture", "image_name": asset["name"]})

    string_values = list(_walk_serialized_strings(data))
    for asset in assets:
        if asset["type_name"] != "MonoBehaviour":
            continue
        atlas_scope = asset["scope"]
        atlas_path_id = int(asset["path_id"])
        referenced_data = _entry_data(asset.get("entry"))
        if isinstance(referenced_data, dict):
            sprite_pointer = referenced_data.get("m_Sprite")
            if sprite_pointer is None:
                sprite_pointer = referenced_data.get("m_sprite")
            if sprite_pointer is None:
                sprite_pointer = referenced_data.get("sprite")
            if sprite_pointer is None:
                sprite_pointer = referenced_data.get("icon")
            sprite_file_id, sprite_path_id = _pptr(sprite_pointer)
            sprite_scope = _resolve_pointer_scope(atlas_scope, sprite_file_id)
            sprite_entry = (
                _scope_entry(sprite_scope, ("Sprite",), sprite_path_id)
                if sprite_scope is not None and sprite_path_id else None
            )
            if sprite_entry is not None:
                candidates.append(
                    {
                        "field_path": asset["field_path"],
                        "field_name": asset["field_name"],
                        "type_name": "Sprite",
                        "file_id": sprite_file_id,
                        "path_id": sprite_path_id,
                        "scope": sprite_scope,
                        "entry": sprite_entry,
                        "name": _asset_name(sprite_scope, "Sprite", sprite_path_id),
                        "image_kind": "sprite",
                        "image_name": _asset_name(sprite_scope, "Sprite", sprite_path_id),
                    }
                )
            texture_pointer = referenced_data.get("mTexture")
            if texture_pointer is None:
                texture_pointer = referenced_data.get("m_Texture")
            texture_file_id, texture_path_id = _pptr(texture_pointer)
            texture_scope = _resolve_pointer_scope(atlas_scope, texture_file_id)
            texture_entry = (
                _scope_entry(texture_scope, ("Texture2D",), texture_path_id)
                if texture_scope is not None and texture_path_id else None
            )
            if texture_entry is not None:
                texture_name = _asset_name(texture_scope, "Texture2D", texture_path_id)
                candidates.append(
                    {
                        "field_path": asset["field_path"],
                        "field_name": asset["field_name"],
                        "type_name": "Texture2D",
                        "file_id": texture_file_id,
                        "path_id": texture_path_id,
                        "scope": texture_scope,
                        "entry": texture_entry,
                        "name": texture_name,
                        "image_kind": "texture",
                        "image_name": texture_name,
                    }
                )
            component_atlas_file_id, component_atlas_path_id = _pptr(
                referenced_data.get("mAtlas")
            )
            component_atlas_scope = _resolve_pointer_scope(
                atlas_scope, component_atlas_file_id
            )
            component_sprite_name = str(
                referenced_data.get("mSpriteName", "")
            ).strip()
            if (
                component_atlas_scope is not None
                and component_atlas_path_id
                and component_sprite_name
                and _resolve_ngui_atlas(
                    component_atlas_scope, component_atlas_path_id
                )
            ):
                candidates.append(
                    {
                        "field_path": asset["field_path"],
                        "field_name": asset["field_name"],
                        "type_name": "NGUI Sprite",
                        "file_id": component_atlas_file_id,
                        "path_id": component_atlas_path_id,
                        "scope": component_atlas_scope,
                        "entry": asset["entry"],
                        "name": component_sprite_name,
                        "image_kind": "ngui_sprite",
                        "image_name": component_sprite_name,
                        "ngui_atlas_path_id": component_atlas_path_id,
                        "ngui_sprite_name": component_sprite_name,
                    }
                )
        resolved = _resolve_ngui_atlas(atlas_scope, atlas_path_id)
        if not resolved:
            continue
        names = {
            str(row.get("name", "")).casefold(): str(row.get("name", ""))
            for row in resolved["sprites"]
            if str(row.get("name", "")).strip()
        }
        for string_path, value in string_values:
            sprite_name = names.get(value.casefold())
            if not sprite_name:
                continue
            field_path = ".".join(string_path)
            candidates.append(
                {
                    "field_path": field_path,
                    "field_name": string_path[-1] if string_path else "",
                    "type_name": "NGUI Sprite",
                    "file_id": int(asset["file_id"]),
                    "path_id": atlas_path_id,
                    "scope": atlas_scope,
                    "entry": asset["entry"],
                    "name": sprite_name,
                    "image_kind": "ngui_sprite",
                    "image_name": sprite_name,
                    "ngui_atlas_path_id": atlas_path_id,
                    "ngui_sprite_name": sprite_name,
                    "atlas_field_path": asset["field_path"],
                }
            )

    if not candidates:
        return None

    kind_priority = {"sprite": 3, "ngui_sprite": 3, "texture": 2}

    def score(candidate: dict) -> tuple[int, int, int, str]:
        field = str(candidate.get("field_path", "")).casefold()
        atlas_field = str(candidate.get("atlas_field_path", "")).casefold()
        hint_score = sum(
            hint in field or hint in atlas_field for hint in STORE_IMAGE_FIELD_HINTS
        )
        auxiliary_penalty = sum(
            penalty
            for hint, penalty in (
                ("progress", 4), ("fill", 3), ("background", 2), ("back", 2),
                ("frame", 1), ("mask", 1),
            )
            if hint in field or hint in atlas_field
        )
        return (
            -hint_score,
            auxiliary_penalty,
            -kind_priority.get(str(candidate.get("image_kind", "")), 0),
            field,
        )

    candidates.sort(key=score)
    return candidates[0]


def _dynamic_product_image_name_forms(value: object) -> set[str]:
    """Return conservative comparable forms for a product ID or image asset name."""
    normalized = re.sub(r"[^a-z0-9]", "", str(value).casefold())
    if not normalized:
        return set()
    forms = {normalized}
    prefixes = (
        "producticon", "product", "itemicon", "itemsprite", "item",
        "shopicon", "storeicon", "icon",
    )
    suffixes = ("sprite", "texture", "thumbnail", "thumb", "preview", "icon")
    changed = True
    while changed:
        changed = False
        for current in tuple(forms):
            for prefix in prefixes:
                if current.startswith(prefix) and len(current) - len(prefix) >= 4:
                    candidate = current[len(prefix):]
                    if candidate not in forms:
                        forms.add(candidate)
                        changed = True
            for suffix in suffixes:
                if current.endswith(suffix) and len(current) - len(suffix) >= 4:
                    candidate = current[:-len(suffix)]
                    if candidate not in forms:
                        forms.add(candidate)
                        changed = True
    return forms


def _named_product_image_index(scopes: dict) -> dict:
    """Index Sprite/Texture2D assets for rows that store a string template ID.

    Unity data frequently keeps only an ItemTemplateId in a trade table.  That
    is not a PPtr, so the normal reference walker cannot reach the icon even
    though an exported Sprite with the same semantic name exists elsewhere.
    """
    rows: list[dict] = []
    exact: dict[str, list[dict]] = {}
    for scope_key, scope in scopes.items():
        for (type_name, path_id), entry in scope.get("items", {}).items():
            if type_name not in {"Sprite", "Texture2D"}:
                continue
            name = str(
                _manifest_value(
                    entry.get("item", {}), "AssetName", "assetName", default=""
                )
            ).strip()
            if not name:
                asset_data = _entry_data(entry)
                name = str(asset_data.get("m_Name", "")).strip() if asset_data else ""
            forms = _dynamic_product_image_name_forms(name)
            if not forms:
                continue
            row = {
                "scope_key": scope_key,
                "scope": scope,
                "entry": entry,
                "type_name": type_name,
                "path_id": int(path_id),
                "name": name,
                "forms": forms,
            }
            rows.append(row)
            for form in forms:
                exact.setdefault(form, []).append(row)
    return {"rows": rows, "exact": exact}


def _best_named_product_image(data: dict, image_index: dict | None) -> dict | None:
    """Resolve an icon from ItemTemplateId-like strings without inventing a link."""
    if not isinstance(image_index, dict):
        return None
    identity_fields = {
        "itemtemplateid", "templateid", "productid", "itemid", "offerid", "sku",
    }
    identities: list[tuple[str, str, set[str]]] = []
    for path, value in _walk_serialized_strings(data):
        if not path:
            continue
        field = re.sub(r"[^a-z0-9]", "", str(path[-1]).casefold())
        if not any(field == hint or field.endswith(hint) for hint in identity_fields):
            continue
        forms = _dynamic_product_image_name_forms(value)
        if forms:
            identities.append((".".join(path), value, forms))
    if not identities:
        return None

    rows = image_index.get("rows", [])
    exact_index = image_index.get("exact", {})
    ranked: list[tuple[float, float, int, str, dict, str, str]] = []
    seen: set[tuple[object, str, int, str]] = set()
    for field_path, value, wanted_forms in identities:
        exact_rows = [
            row
            for form in wanted_forms
            for row in exact_index.get(form, [])
        ]
        candidates = exact_rows or rows
        for row in candidates:
            key = (
                row.get("scope_key"), str(row.get("type_name", "")),
                int(row.get("path_id", 0) or 0), field_path,
            )
            if key in seen:
                continue
            seen.add(key)
            asset_forms = row.get("forms", set())
            if not isinstance(asset_forms, set) or not asset_forms:
                continue
            exact_match = bool(wanted_forms & asset_forms)
            similarity = 1.0 if exact_match else max(
                difflib.SequenceMatcher(None, wanted, asset).ratio()
                for wanted in wanted_forms
                for asset in asset_forms
            )
            shortest = min(
                min((len(form) for form in wanted_forms), default=0),
                min((len(form) for form in asset_forms), default=0),
            )
            if not exact_match and (shortest < 7 or similarity < 0.86):
                continue
            kind_priority = 2 if row.get("type_name") == "Sprite" else 1
            item_asset_bonus = (
                0.015
                if re.sub(r"[^a-z0-9]", "", str(row.get("name", "")).casefold())
                .startswith("item")
                else 0.0
            )
            combined = similarity + (0.06 if kind_priority == 2 else 0.0) + item_asset_bonus
            ranked.append(
                (
                    combined, similarity, kind_priority,
                    str(row.get("name", "")).casefold(), row, field_path, value,
                )
            )
    if not ranked:
        return None
    ranked.sort(
        key=lambda item: (
            -item[0], -item[1], -item[2], item[3],
            str(item[4].get("scope", {}).get("source", "")).casefold(),
            int(item[4].get("path_id", 0) or 0),
        )
    )
    _combined, similarity, _kind_priority, _name_key, best, field_path, value = ranked[0]
    if similarity < 1.0:
        distinct_runner = next(
            (
                row for row in ranked[1:]
                if row[4].get("forms") != best.get("forms")
            ),
            None,
        )
        if distinct_runner is not None and distinct_runner[1] >= similarity - 0.025:
            return None
    image_kind = "sprite" if best.get("type_name") == "Sprite" else "texture"
    return {
        "field_path": f"{field_path} -> 资源名匹配",
        "field_name": field_path.rsplit(".", 1)[-1],
        "type_name": str(best.get("type_name", "")),
        "file_id": 0,
        "path_id": int(best.get("path_id", 0) or 0),
        "scope": best.get("scope"),
        "entry": best.get("entry"),
        "name": str(best.get("name", "")),
        "image_kind": image_kind,
        "image_name": str(best.get("name", "")),
        "identity_value": value,
        "identity_similarity": round(similarity, 4),
    }


def _best_product_sprite(data: dict, scope: dict) -> dict | None:
    """Backward-compatible Unity Sprite-only view used by older block records."""
    image = _best_product_image(data, scope)
    return image if image and image.get("image_kind") == "sprite" else None


def _prefab_product_image(
    scope: dict | None,
    root_object_id: int,
    max_objects: int = 32,
) -> dict | None:
    if not scope or not root_object_id:
        return None
    queue = [root_object_id]
    visited: set[int] = set()
    while queue and len(visited) < max_objects:
        object_id = queue.pop(0)
        if object_id in visited:
            continue
        visited.add(object_id)
        object_entry = _scope_entry(scope, ("GameObject",), object_id)
        object_data = _entry_data(object_entry) if object_entry else None
        if not isinstance(object_data, dict):
            continue
        for component_path_id in _game_object_component_path_ids(object_data):
            component_entry = _scope_entry(
                scope, ("MonoBehaviour", "SpriteRenderer"), component_path_id
            )
            component_data = _entry_data(component_entry) if component_entry else None
            if not isinstance(component_data, dict):
                continue
            image = _best_product_image(component_data, scope)
            if image:
                return {
                    **image,
                    "field_path": f"prefab.{object_id}.{image.get('field_path', '')}",
                }
        transform = _find_game_object_transform(scope, object_id)
        if not isinstance(transform, dict):
            continue
        for child_pointer in _array_value(transform.get("m_Children")):
            file_id, child_transform_id = _pptr(child_pointer)
            child_entry = (
                _scope_entry(scope, ("Transform", "RectTransform"), child_transform_id)
                if file_id == 0 else None
            )
            child_transform = _entry_data(child_entry) if child_entry else None
            child_object_file_id, child_object_id = _pptr(
                child_transform.get("m_GameObject")
                if isinstance(child_transform, dict) else None
            )
            if child_object_file_id == 0 and child_object_id:
                queue.append(child_object_id)
    return None


def _store_product_descriptor(
    scope: dict,
    file_id: int,
    path_id: int,
    index: int,
) -> dict:
    product_scope = _resolve_pointer_scope(scope, file_id)
    product_entry = (
        _scope_entry(product_scope, ("MonoBehaviour",), path_id)
        if product_scope else None
    )
    data = _entry_data(product_entry) if product_entry else None
    data = data if isinstance(data, dict) else {}
    _object_file_id, product_object_id = _pptr(data.get("m_GameObject"))
    product_object_scope = (
        _resolve_pointer_scope(product_scope, _object_file_id)
        if product_scope else None
    )
    product_object_name = _asset_name(
        product_object_scope, "GameObject", product_object_id
    )

    best_image = _best_product_image(data, product_scope) if product_scope else None
    back_file_id, back_path_id = _pptr(data.get("_backSprite"))
    back_scope = _resolve_pointer_scope(product_scope, back_file_id) if product_scope else None
    prefab_file_id, prefab_path_id = _pptr(data.get("_prefab"))
    prefab_scope = _resolve_pointer_scope(product_scope, prefab_file_id) if product_scope else None
    prefab_name = _asset_name(prefab_scope, "GameObject", prefab_path_id)
    prefab_object_id = prefab_path_id if prefab_name else 0
    if prefab_scope and prefab_path_id and not prefab_name:
        prefab_entry = _scope_entry(prefab_scope, ("MonoBehaviour",), prefab_path_id)
        prefab_data = _entry_data(prefab_entry) if prefab_entry else None
        prefab_object_file_id, prefab_object_id = _pptr(
            prefab_data.get("m_GameObject") if isinstance(prefab_data, dict) else None
        )
        prefab_object_scope = _resolve_pointer_scope(prefab_scope, prefab_object_file_id)
        prefab_name = _asset_name(prefab_object_scope, "GameObject", prefab_object_id)
        if prefab_object_scope is not None:
            prefab_scope = prefab_object_scope
    if best_image is None and prefab_scope and prefab_object_id:
        best_image = _prefab_product_image(prefab_scope, prefab_object_id)

    image_kind = str(best_image.get("image_kind", "")) if best_image else ""
    image_scope = best_image.get("scope") if best_image else None
    image_path_id = int(best_image.get("path_id", 0)) if best_image else 0
    sprite_path_id = image_path_id if image_kind == "sprite" else 0
    texture_path_id = image_path_id if image_kind == "texture" else 0
    ngui_atlas_path_id = (
        int(best_image.get("ngui_atlas_path_id", image_path_id))
        if best_image and image_kind == "ngui_sprite" else 0
    )
    signal_fields = _product_signal_fields(data)
    signal_hints = {
        hint
        for hint in (*STORE_COMMERCE_FIELD_HINTS, *TASK_ENTRY_FIELD_HINTS)
        if any(hint in field for field in signal_fields)
    }
    evidence_score = (
        (2 if best_image else 0)
        + min(4, len(signal_hints))
        + (1 if prefab_path_id else 0)
    )

    return {
        "index": index,
        "file_id": file_id,
        "path_id": path_id,
        "pointer_key": _store_product_pointer_key(file_id, path_id),
        "name": str(
            data.get("m_Name")
            or product_object_name
            or f"<条目 PathID={path_id}>"
        ),
        "source_json": str(product_entry["path"]) if product_entry else "",
        "resolved": bool(product_entry),
        "product_scope": product_scope,
        "product_object_path_id": product_object_id,
        "product_object_scope": product_object_scope,
        "image_kind": image_kind,
        "image_name": str(best_image.get("image_name", "")) if best_image else "",
        "image_scope": image_scope,
        "image_path_id": image_path_id,
        "image_field": str(best_image.get("field_path", "")) if best_image else "",
        "texture_path_id": texture_path_id,
        "ngui_atlas_path_id": ngui_atlas_path_id,
        "ngui_sprite_name": (
            str(best_image.get("ngui_sprite_name", "")) if best_image else ""
        ),
        "sprite_file_id": int(best_image.get("file_id", 0)) if best_image else 0,
        "sprite_path_id": sprite_path_id,
        "sprite_scope": image_scope if image_kind == "sprite" else None,
        "sprite_field": (
            str(best_image.get("field_path", ""))
            if best_image and image_kind == "sprite" else ""
        ),
        "sprite_name": (
            str(best_image.get("image_name", "")) if best_image else ""
        ),
        "back_sprite_path_id": back_path_id,
        "back_sprite_name": _asset_name(back_scope, "Sprite", back_path_id),
        "prefab_path_id": prefab_path_id,
        "prefab_object_path_id": prefab_object_id,
        "prefab_name": prefab_name,
        "reward_ads": int(data.get("_rewardADS", 0) or 0),
        "reward_ads_to_show": int(data.get("_rewardADSToShow", 0) or 0),
        "signal_fields": signal_fields,
        "evidence_score": evidence_score,
        "field_signature": sorted(
            str(key).casefold()
            for key in data
            if str(key) not in {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}
        ),
    }


def _reward_pet_catalog_item(scope: dict, pet_id: int) -> dict | None:
    if pet_id < 0:
        return None
    catalogs: list[tuple[int, list[dict]]] = []
    for (type_name, _path_id), entry in scope.get("items", {}).items():
        if type_name != "MonoBehaviour":
            continue
        container = _entry_data(entry)
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("m_Name", "")).casefold()
        if "pet" not in container_name:
            continue
        for array_path, values in _walk_serialized_arrays(container):
            path_label = ".".join(array_path).casefold()
            if "pet" not in path_label or "item" not in path_label:
                continue
            if not values or not all(isinstance(value, dict) for value in values):
                continue
            score = (
                (20 if "shop" in container_name else 0)
                + (10 if "data" in container_name else 0)
                + len(values)
            )
            catalogs.append((score, values))
    for _score, values in sorted(catalogs, key=lambda row: -row[0]):
        explicit = next(
            (
                value for value in values
                if int(_number(value.get("m_id", value.get("id", -1)), -1)) == pet_id
                and int(_number(value.get("m_id", value.get("id", -1)), -1)) != 0
            ),
            None,
        )
        if explicit is not None:
            return explicit
        if 0 <= pet_id < len(values):
            return values[pet_id]
    return None


def _store_inline_product_descriptor(
    scope: dict,
    data: dict,
    index: int,
    named_image_index: dict | None = None,
) -> dict:
    """Describe a product serialized directly inside its owner's array.

    A large number of Unity games use arrays of serializable structs rather
    than PPtrs to standalone MonoBehaviours.  Those rows are just as safe to
    remove from the owning array, but they do not have their own PathID.
    """
    assets = _serialized_pointer_assets(data, scope)
    linked_type_name = next(
        (
            str(linked_data.get("m_Name", "")).strip()
            for asset in assets
            if asset.get("type_name") == "MonoBehaviour"
            and any(
                hint in str(asset.get("field_path", "")).casefold()
                for hint in ("infotype", "rewardtype", "currencytype")
            )
            for linked_data in [_entry_data(asset.get("entry"))]
            if isinstance(linked_data, dict)
            and str(linked_data.get("m_Name", "")).strip()
        ),
        "",
    )
    best_image = _best_product_image(data, scope)
    if best_image is None:
        best_image = _best_named_product_image(data, named_image_index)
    reward_pet_name = ""
    try:
        reward_pet_id = int(float(data.get("m_petId", data.get("petId", -1))))
    except (TypeError, ValueError):
        reward_pet_id = -1
    if "pet" in linked_type_name.casefold() and reward_pet_id >= 0:
        reward_pet_data = _reward_pet_catalog_item(scope, reward_pet_id)
        if isinstance(reward_pet_data, dict):
            reward_pet_image = _best_product_image(reward_pet_data, scope)
            if reward_pet_image is not None:
                best_image = reward_pet_image
            reward_pet_name = str(
                reward_pet_data.get("m_itemName")
                or reward_pet_data.get("itemName")
                or reward_pet_data.get("m_Name")
                or ""
            ).strip()
    prefab = next(
        (
            asset for asset in assets
            if asset.get("type_name") == "GameObject"
            and any(
                hint in str(asset.get("field_path", "")).casefold()
                for hint in ("prefab", "template", "view", "button", "card", "entry")
            )
        ),
        None,
    )
    back_file_id, back_path_id = _pptr(
        data.get("_backSprite")
        or data.get("m_backSprite")
        or data.get("backgroundSprite")
    )
    back_scope = _resolve_pointer_scope(scope, back_file_id)
    image_kind = str(best_image.get("image_kind", "")) if best_image else ""
    image_scope = best_image.get("scope") if best_image else None
    image_path_id = int(best_image.get("path_id", 0) or 0) if best_image else 0
    sprite_path_id = image_path_id if image_kind == "sprite" else 0
    texture_path_id = image_path_id if image_kind == "texture" else 0
    ngui_atlas_path_id = (
        int(best_image.get("ngui_atlas_path_id", image_path_id) or 0)
        if best_image and image_kind == "ngui_sprite" else 0
    )
    signal_fields = _product_signal_fields(data)
    signal_hints = {
        hint
        for hint in (*STORE_COMMERCE_FIELD_HINTS, *TASK_ENTRY_FIELD_HINTS)
        if any(hint in field for field in signal_fields)
    }
    evidence_score = (
        (2 if best_image else 0)
        + min(4, len(signal_hints))
        + (1 if prefab else 0)
    )

    def first_value(*names: str) -> object:
        for name in names:
            value = data.get(name)
            if value not in (None, ""):
                return value
        return ""

    def as_int(value: object) -> int:
        try:
            return int(float(value or 0))
        except (TypeError, ValueError):
            return 0

    name = str(first_value(
        "m_itemName", "itemName", "m_Name", "name", "title", "displayName",
        "ItemTemplateId", "itemTemplateId", "itemTemplateID", "TemplateId",
        "templateId", "productId", "itemId", "m_id", "id",
    )).strip()
    if not name:
        amount = as_int(first_value("m_amount", "amount", "quantity", "m_quantity"))
        pet_id = as_int(first_value("m_petId", "petId"))
        if linked_type_name:
            name = linked_type_name
            if amount:
                name += f" ×{amount}"
            if reward_pet_name:
                name += f" ({reward_pet_name})"
            elif pet_id:
                name += f" (Pet {pet_id})"
    if not name:
        name = f"<内嵌条目 #{index + 1}>"
    pointer_key = _store_inline_product_key(data, index)
    prefab_path_id = int(prefab.get("path_id", 0) or 0) if prefab else 0
    return {
        "index": index,
        "file_id": 0,
        "path_id": 0,
        "pointer_key": pointer_key,
        "entry_kind": "inline",
        "inline_hash": pointer_key.rsplit(":", 1)[-1],
        "name": name,
        "source_json": "",
        "resolved": True,
        "product_scope": scope,
        "product_object_path_id": 0,
        "product_object_scope": None,
        "image_kind": image_kind,
        "image_name": str(best_image.get("image_name", "")) if best_image else "",
        "image_scope": image_scope,
        "image_path_id": image_path_id,
        "image_field": str(best_image.get("field_path", "")) if best_image else "",
        "texture_path_id": texture_path_id,
        "ngui_atlas_path_id": ngui_atlas_path_id,
        "ngui_sprite_name": (
            str(best_image.get("ngui_sprite_name", "")) if best_image else ""
        ),
        "sprite_file_id": int(best_image.get("file_id", 0) or 0) if best_image else 0,
        "sprite_path_id": sprite_path_id,
        "sprite_scope": image_scope if image_kind == "sprite" else None,
        "sprite_field": (
            str(best_image.get("field_path", ""))
            if best_image and image_kind == "sprite" else ""
        ),
        "sprite_name": str(best_image.get("image_name", "")) if best_image else "",
        "back_sprite_path_id": back_path_id,
        "back_sprite_name": _asset_name(back_scope, "Sprite", back_path_id),
        "prefab_path_id": prefab_path_id,
        "prefab_object_path_id": prefab_path_id,
        "prefab_name": str(prefab.get("name", "")) if prefab else "",
        "reward_ads": as_int(first_value(
            "_rewardADS", "m_rewardADS", "m_itemAdAmount", "itemAdAmount",
        )),
        "reward_ads_to_show": as_int(first_value(
            "_rewardADSToShow", "m_rewardADSToShow", "m_itemAdAmount", "itemAdAmount",
        )),
        "signal_fields": signal_fields,
        "evidence_score": evidence_score,
        "field_signature": sorted(
            str(key).casefold()
            for key in data
            if str(key) not in {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}
        ),
    }


def _store_product_config_entries(scopes: dict) -> list[tuple[object, dict, int, dict]]:
    prefilter_hints = tuple(dict.fromkeys(
        (
            *STORE_ARRAY_HINTS,
            *STORE_CONTEXT_HINTS,
            "task", "quest", "mission", "achievement", "challenge",
            "objective", "daily", "reward", "price", "currency",
        )
    ))
    entries = [
        (scope_key, scope, path_id, entry)
        for scope_key, scope in scopes.items()
        for (type_name, path_id), entry in scope.get("items", {}).items()
        if type_name == "MonoBehaviour"
    ]
    technical_array_names = {
        "m_component", "m_children", "m_materials", "materials", "m_calls",
        "m_persistentcalls", "m_modifications", "m_addedcomponents",
        "m_addedgameobjects", "m_removedcomponents", "m_removedgameobjects",
        "m_exposedreferences", "m_animationclips", "m_events",
    }

    def has_structural_inline_array(data: dict) -> bool:
        for array_path, values in _walk_serialized_arrays(data):
            if not (2 <= len(values) <= 500):
                continue
            if not values or not all(isinstance(value, dict) for value in values):
                continue
            if all(_is_serialized_pointer(value) for value in values):
                continue
            if any(str(part).casefold() in technical_array_names for part in array_path):
                continue
            signatures = [
                tuple(sorted(str(key).casefold() for key in value))
                for value in values[:20]
            ]
            if not signatures or len(set(signatures)) > max(2, len(signatures) // 3):
                continue
            common_fields = set(signatures[0])
            if len(common_fields) < 2:
                continue
            if any(
                not isinstance(child, (dict, list))
                for value in values[:5]
                for child in value.values()
            ):
                return True
        return False

    def has_candidate_array(data: dict) -> bool:
        container_name = str(data.get("m_Name", "")).casefold()
        named_context = any(
            hint in container_name
            for hint in (*DYNAMIC_LIST_CONTEXT_HINTS, "reward")
        )
        for array_path, values in _walk_serialized_arrays(data):
            if not (1 <= len(values) <= 500):
                continue
            if not values or not all(isinstance(value, dict) for value in values):
                continue
            if any(str(part).casefold() in technical_array_names for part in array_path):
                continue
            path_label = ".".join(array_path).casefold()
            if named_context or any(
                hint in path_label
                for hint in (*DYNAMIC_LIST_ARRAY_HINTS, "reward")
            ):
                return True
        return bool(container_name) and has_structural_inline_array(data)

    preloaded = [
        row for row in entries
        if isinstance(row[3].get("data"), dict)
        and has_candidate_array(row[3]["data"])
    ]
    if entries and all(isinstance(row[3].get("data"), dict) for row in entries):
        return preloaded
    entry_by_path = {
        str(row[3]["path"].resolve()).casefold(): row
        for row in entries
        if row[3].get("path")
    }
    try:
        command = [
            "rg", "-l", "-i", "--glob", "**/MonoBehaviour/*.json", "-e",
            '"[^\"]*(' + "|".join(prefilter_hints) + ')[^\"]*"\\s*:\\s*\\{',
            str(DEFAULT_SOURCE_ROOT),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr.strip() or f"rg 返回码 {result.returncode}")
        matched = [
            row
            for line in result.stdout.splitlines()
            if (key := str(Path(line.strip()).resolve()).casefold()) in entry_by_path
            for row in [entry_by_path[key]]
            if isinstance((data := _entry_data(row[3])), dict)
            and has_candidate_array(data)
        ]
        unique = {
            str(row[3]["path"].resolve()).casefold(): row
            for row in preloaded + matched
        }
        return list(unique.values())
    except (FileNotFoundError, OSError, RuntimeError):
        pass
    result: list[tuple[object, dict, int, dict]] = list({
        str(row[3]["path"]): row for row in preloaded
    }.values())
    preloaded_paths = {str(row[3]["path"]) for row in result}
    for row in entries:
        if str(row[3]["path"]) in preloaded_paths:
            continue
        try:
            text = row[3]["path"].read_text(encoding="utf-8-sig").casefold()
            data = _entry_data(row[3])
            if (
                any(hint in text for hint in prefilter_hints)
                and isinstance(data, dict)
                and has_candidate_array(data)
            ):
                result.append(row)
        except (OSError, UnicodeError):
            continue
    return result


def _store_array_classification(
    container_data: dict,
    array_path: tuple[str, ...],
    products: list[dict],
) -> dict:
    path_label = ".".join(array_path).casefold()
    container_name = str(container_data.get("m_Name", "")).casefold()
    strong_array_hints = (
        "product", "offer", "goods", "bundle", "package", "iap", "purchase",
        "trade", "sell", "buy",
    )
    medium_array_hints = ("item", "skin", "shop", "store", "catalog")
    commerce_context = any(
        hint in path_label or hint in container_name
        for hint in (*strong_array_hints, *STORE_CONTEXT_HINTS)
    )
    task_context = any(
        hint in path_label or hint in container_name for hint in TASK_LIST_HINTS
    )
    if any(hint in path_label for hint in (*strong_array_hints, *TASK_LIST_HINTS)):
        array_score = 5
    elif any(hint in path_label for hint in medium_array_hints):
        array_score = 3
    else:
        array_score = 0
    context_score = 3 if any(
        hint in container_name for hint in DYNAMIC_LIST_CONTEXT_HINTS
    ) else 0
    evidence_scores = [int(product.get("evidence_score", 0) or 0) for product in products]
    average_evidence = (
        round(sum(evidence_scores) / len(evidence_scores)) if evidence_scores else 0
    )
    signatures = [tuple(product.get("field_signature", [])) for product in products]
    consistency_score = 1 if signatures and len(set(signatures)) == 1 else 0
    signature_blob = " ".join(
        str(field)
        for product in products[:3]
        for field in product.get("field_signature", [])
    ).casefold()
    image_count = sum(bool(product.get("image_kind")) for product in products)
    strong_inline_product_structure = (
        products[0].get("entry_kind") == "inline"
        and "item" in path_label
        and average_evidence >= 4
        and image_count >= max(1, len(products) // 2)
        and any(
            hint in signature_blob
            for hint in ("price", "cost", "currency", "adamount", "displayicon")
        )
    )
    if strong_inline_product_structure:
        commerce_context = True
    total_score = array_score + context_score + average_evidence + consistency_score
    accepted = (
        total_score >= 7
        and max(evidence_scores, default=0) >= 2
        and (array_score >= 3 or context_score >= 3)
        and (commerce_context or task_context or strong_inline_product_structure)
    )
    confidence = "高" if total_score >= 11 else "中" if total_score >= 8 else "低"
    list_kind = "任务/活动" if task_context and not commerce_context else "商店/商品"
    return {
        "accepted": accepted,
        "score": total_score,
        "confidence": confidence,
        "kind": list_kind,
        "array_score": array_score,
        "context_score": context_score,
        "average_evidence": average_evidence,
        "consistency_score": consistency_score,
        "strong_inline_product_structure": strong_inline_product_structure,
    }


def _find_store_product_configs(
    scopes: dict,
    ai_accepted_ids: set[str] | None = None,
    rejected_candidates: list[dict] | None = None,
) -> list[dict]:
    """Find structurally scored serialized shop lists pointing at product data."""
    result: list[dict] = []
    named_image_index: dict | None = None
    ai_kind_map = _dynamic_list_ai_accepted_kind_map() if ai_accepted_ids else {}
    candidates = _store_product_config_entries(scopes)
    print(f"[动态列表] 数据数组配置预筛选: {len(candidates)} 个", flush=True)
    for scope_key, scope, path_id, entry in candidates:
        data = _entry_data(entry)
        if not isinstance(data, dict):
            continue
        for array_path, array_values in _walk_serialized_arrays(data):
            if not array_values:
                continue
            products: list[dict] = []
            if all(_is_serialized_pointer(value) for value in array_values):
                for index, pointer in enumerate(array_values):
                    file_id, product_path_id = _pptr(pointer)
                    if not product_path_id:
                        products = []
                        break
                    descriptor = _store_product_descriptor(
                        scope, file_id, product_path_id, index
                    )
                    if not descriptor.get("resolved"):
                        products = []
                        break
                    descriptor["legacy_pointer_key"] = descriptor["pointer_key"]
                    descriptor["pointer_key"] = _store_array_item_key(pointer, index)
                    products.append(descriptor)
            elif all(isinstance(value, dict) for value in array_values):
                if named_image_index is None:
                    named_image_index = _named_product_image_index(scopes)
                products = [
                    _store_inline_product_descriptor(
                        scope, value, index, named_image_index
                    )
                    for index, value in enumerate(array_values)
                ]
            if not products:
                continue
            source_json = str(entry["path"])
            path_label = ".".join(array_path)
            classification = _store_array_classification(data, array_path, products)
            candidate_id = _store_product_config_key(source_json, path_id, array_path)
            accepted_by_ai = bool(
                ai_accepted_ids and candidate_id in ai_accepted_ids
            )
            if not classification["accepted"] and not accepted_by_ai:
                if rejected_candidates is not None:
                    rejected_candidates.append(
                        {
                            "candidate_id": candidate_id,
                            "path_id": int(path_id),
                            "_scope": scope,
                            "_all_scopes": scopes,
                            "name": str(data.get("m_Name", "")),
                            "source": str(scope.get("source", "")),
                            "bundle_entry": str(scope.get("bundle_entry", "")),
                            "array_label": path_label,
                            "item_count": len(products),
                            "entry_kind": products[0].get("entry_kind", "pointer"),
                            "field_signature": products[0].get("field_signature", []),
                            "sample_names": [
                                str(product.get("name", ""))
                                for product in products[:5]
                            ],
                            "image_count": sum(
                                bool(product.get("image_kind")) for product in products
                            ),
                            "classification": classification,
                        }
                    )
                continue
            if accepted_by_ai:
                classification = {
                    **classification,
                    "accepted": True,
                    "confidence": "AI复核",
                    "ai_reviewed": True,
                    "kind": ai_kind_map.get(
                        candidate_id, classification.get("kind", "其他动态列表")
                    ),
                }
            config_object_file_id, config_object_path_id = _pptr(
                data.get("m_GameObject")
            )
            config_object_scope = _resolve_pointer_scope(
                scope, config_object_file_id
            )
            config_object_name = _asset_name(
                config_object_scope, "GameObject", config_object_path_id
            )
            result.append(
                {
                    "scope_key": scope_key,
                    "scope": scope,
                    "source": str(scope.get("source", "")),
                    "bundle_entry": str(scope.get("bundle_entry", "")),
                    "path_id": int(path_id),
                    "name": str(
                        data.get("m_Name")
                        or config_object_name
                        or f"<动态列表 PathID={path_id}>"
                    ),
                    "array_path": list(array_path),
                    "array_label": path_label,
                    "source_json": source_json,
                    "config_key": candidate_id,
                    "products": products,
                    "classification": classification,
                }
            )
    result.sort(
        key=lambda row: (
            str(row.get("source", "")).casefold(),
            str(row.get("bundle_entry", "")).casefold(),
            str(row.get("name", "")).casefold(),
            int(row.get("path_id", 0)),
        )
    )
    return result


def _dynamic_list_ai_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "accepted": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["商店/商品", "任务/活动", "其他动态列表"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["candidate_id", "kind", "reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["accepted"],
        "additionalProperties": False,
    }


def _dynamic_list_ai_candidates(rejected_candidates: list[dict]) -> list[dict]:
    def is_reward_semantic_candidate(candidate: dict) -> bool:
        label = (
            str(candidate.get("name", ""))
            + " "
            + str(candidate.get("array_label", ""))
        ).casefold()
        fields = {
            str(value).casefold()
            for value in candidate.get("field_signature", [])
        }
        return (
            "reward" in label
            and any("infotype" in field for field in fields)
            and any("amount" in field for field in fields)
        )

    candidates = [
        candidate for candidate in rejected_candidates
        if candidate.get("entry_kind") == "inline"
        and str(candidate.get("name", "")).strip()
        and int(candidate.get("item_count", 0) or 0) >= 2
        and (
            int(candidate.get("classification", {}).get("score", 0) or 0) >= 3
            or is_reward_semantic_candidate(candidate)
        )
    ]
    candidates.sort(
        key=lambda candidate: (
            -int(candidate.get("classification", {}).get("score", 0) or 0),
            str(candidate.get("source", "")).casefold(),
            str(candidate.get("name", "")).casefold(),
            str(candidate.get("array_label", "")).casefold(),
        )
    )
    # AI is a reviewer of locally bounded candidates, not an unrestricted
    # project scanner.  Keeping the batch finite also makes failure/fallback
    # predictable on very large exports.
    return candidates[:120]


def _parse_dynamic_list_ai_json(content: str) -> dict:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            raise ValueError("AI 动态列表复核没有返回 JSON 对象")
        value, _end = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(value, dict):
        raise ValueError("AI 动态列表复核结果不是 JSON 对象")
    return value


def _dynamic_list_ai_transport_chain(cfg) -> list[str]:
    transport = str(
        getattr(cfg, "ai_dynamic_list_transport", "codex_cli") or "codex_cli"
    ).strip().lower()
    http_ready = bool(
        str(getattr(cfg, "ai_dynamic_list_base_url", "")).strip()
        and str(getattr(cfg, "ai_dynamic_list_api_key", "")).strip()
        and str(getattr(cfg, "ai_dynamic_list_model", "")).strip()
    )
    if transport == "http":
        return ["http"] if http_ready else []
    if transport != "codex_cli":
        return []
    chain: list[str] = []
    if (
        str(getattr(cfg, "ai_dynamic_list_codex_model", "gpt-5.3-codex-spark")).strip()
        and codex_cli_available()
    ):
        chain.append("codex_cli")
    if http_ready:
        chain.append("http")
    return chain


def _dynamic_list_ai_cache_signature() -> str:
    cfg = load_config(quiet=True)
    payload = {
        "prompt_version": 4,
        "enabled": bool(getattr(cfg, "enable_ai_dynamic_list_review", False)),
        "transport": str(getattr(cfg, "ai_dynamic_list_transport", "")),
        "codex_model": str(getattr(cfg, "ai_dynamic_list_codex_model", "")),
        "codex_reasoning": str(getattr(
            cfg, "ai_dynamic_list_codex_reasoning_effort", ""
        )),
        "http_model": str(getattr(cfg, "ai_dynamic_list_model", "")),
        "http_configured": bool(
            str(getattr(cfg, "ai_dynamic_list_base_url", "")).strip()
            and str(getattr(cfg, "ai_dynamic_list_api_key", "")).strip()
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _dynamic_list_candidate_reference_context(
    candidates: list[dict],
) -> dict[str, list[str]]:
    all_scopes = next(
        (
            candidate.get("_all_scopes")
            for candidate in candidates
            if isinstance(candidate.get("_all_scopes"), dict)
        ),
        None,
    )
    if not isinstance(all_scopes, dict):
        return {}
    target_ids = {
        int(candidate.get("path_id", 0) or 0)
        for candidate in candidates
        if int(candidate.get("path_id", 0) or 0)
    }
    if not target_ids:
        return {}
    targets = {
        (id(candidate.get("_scope")), int(candidate.get("path_id", 0) or 0)):
        str(candidate.get("candidate_id", ""))
        for candidate in candidates
        if isinstance(candidate.get("_scope"), dict)
        and int(candidate.get("path_id", 0) or 0)
    }
    references: dict[str, set[str]] = {}
    # These config assets are normally assigned through a top-level serialized
    # field on a UI controller.  Walking the already-loaded object graph is far
    # faster than launching a disk-wide PathID search, and identity-checking the
    # resolved scope prevents collisions between bundles.
    rows = [
        (scope_key, scope, "MonoBehaviour", component_path_id, entry)
        for scope_key, scope in all_scopes.items()
        for (type_name, component_path_id), entry in scope.get("items", {}).items()
        if type_name == "MonoBehaviour"
    ]
    for _scope_key, scope, _type_name, component_path_id, entry in rows:
        data = _entry_data(entry)
        if not isinstance(data, dict):
            continue
        matched_pointers: list[tuple[str, str]] = []
        for field_name, pointer in data.items():
            file_id, path_id = _pptr(pointer)
            if not path_id:
                continue
            target_scope = _resolve_pointer_scope(scope, file_id)
            candidate_id = targets.get((id(target_scope), int(path_id)))
            if not candidate_id:
                continue
            field_label = str(field_name)
            if any(
                token in field_label.casefold()
                for token in ("onclick", "persistentcalls", "event", "callback")
            ):
                continue
            matched_pointers.append((candidate_id, field_label))
        if not matched_pointers:
            continue
        _object_file_id, object_path_id = _pptr(data.get("m_GameObject"))
        chain = _object_chain_direct(scope, object_path_id, 6) if object_path_id else []
        owner_path = "/".join(
            str(node.get("name", ""))
            for node in reversed(chain)
            if str(node.get("name", "")).strip()
        )
        component_name = str(data.get("m_Name", "")).strip()
        owner_label = owner_path or component_name or f"MonoBehaviour#{component_path_id}"
        for candidate_id, field_label in matched_pointers:
            references.setdefault(candidate_id, set()).add(
                f"{owner_label} -> {field_label or '<direct>'}"
            )
    return {
        candidate_id: sorted(values)[:8]
        for candidate_id, values in references.items()
    }


def _dynamic_list_ai_review_context(
    rejected_candidates: list[dict],
    cfg,
) -> tuple[list[dict], dict[str, str], list[dict], str]:
    candidates = _dynamic_list_ai_candidates(rejected_candidates)
    public_id_to_candidate_id = {
        "candidate_" + hashlib.sha256(
            str(candidate["candidate_id"]).encode("utf-8")
        ).hexdigest()[:16]: str(candidate["candidate_id"])
        for candidate in candidates
    }
    candidate_id_to_public_id = {
        candidate_id: public_id
        for public_id, candidate_id in public_id_to_candidate_id.items()
    }
    reference_context = _dynamic_list_candidate_reference_context(candidates)
    compact_candidates = [
        {
            "candidate_id": candidate_id_to_public_id[str(candidate["candidate_id"])],
            "container_name": candidate.get("name", ""),
            "array_path": candidate.get("array_label", ""),
            "item_count": candidate.get("item_count", 0),
            "field_signature": candidate.get("field_signature", []),
            "sample_names": candidate.get("sample_names", []),
            "resolved_image_count": candidate.get("image_count", 0),
            "heuristic_score": candidate.get("classification", {}).get("score", 0),
            "referenced_by": reference_context.get(
                str(candidate.get("candidate_id", "")), []
            ),
        }
        for candidate in candidates
    ]
    fingerprint_payload = {
        "version": 4,
        "candidates": compact_candidates,
        "codex_model": str(getattr(cfg, "ai_dynamic_list_codex_model", "")),
        "http_model": str(getattr(cfg, "ai_dynamic_list_model", "")),
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return candidates, public_id_to_candidate_id, compact_candidates, fingerprint


def _dynamic_list_ai_review_cache_matches(rejected_candidates: list[dict]) -> bool:
    cfg = load_config(quiet=True)
    if not bool(getattr(cfg, "enable_ai_dynamic_list_review", False)):
        return True
    candidates, _id_map, _compact, fingerprint = _dynamic_list_ai_review_context(
        rejected_candidates, cfg
    )
    if not candidates:
        return True
    cached = _safe_read_json(DEFAULT_DYNAMIC_LIST_AI_REVIEW)
    return bool(
        isinstance(cached, dict)
        and cached.get("fingerprint") == fingerprint
        and isinstance(cached.get("accepted_candidate_ids"), list)
        and isinstance(cached.get("accepted_kinds"), dict)
    )


def _dynamic_list_ai_accepted_kind_map() -> dict[str, str]:
    cached = _safe_read_json(DEFAULT_DYNAMIC_LIST_AI_REVIEW)
    values = cached.get("accepted_kinds") if isinstance(cached, dict) else None
    if not isinstance(values, dict):
        return {}
    allowed = {"商店/商品", "任务/活动", "其他动态列表"}
    return {
        str(candidate_id): str(kind)
        for candidate_id, kind in values.items()
        if str(kind) in allowed
    }


def _request_dynamic_list_ai_review(rejected_candidates: list[dict]) -> set[str]:
    cfg = load_config(quiet=True)
    if not bool(getattr(cfg, "enable_ai_dynamic_list_review", False)):
        return set()
    (
        candidates,
        public_id_to_candidate_id,
        compact_candidates,
        fingerprint,
    ) = _dynamic_list_ai_review_context(rejected_candidates, cfg)
    if not candidates:
        return set()
    transports = _dynamic_list_ai_transport_chain(cfg)
    if not transports:
        print("[动态列表][AI] 未找到可用的 Codex CLI 或 DeepSeek HTTP 配置，跳过困难候选复核。")
        return set()

    cached = _safe_read_json(DEFAULT_DYNAMIC_LIST_AI_REVIEW)
    if isinstance(cached, dict) and cached.get("fingerprint") == fingerprint:
        cached_ids = cached.get("accepted_candidate_ids")
        if isinstance(cached_ids, list) and isinstance(cached.get("accepted_kinds"), dict):
            print(
                f"[动态列表][AI] 已复用困难候选复核缓存："
                f"候选={len(candidates)}，接受={len(cached_ids)}"
            )
            return {str(value) for value in cached_ids}

    system_prompt = (
        "你是 Unity 序列化动态列表审查助手。输入候选均由本地脚本从 MonoBehaviour "
        "或 ScriptableObject 的内嵌结构体数组中提取。请只接受确实表示玩家可见且可按条目"
        "增删的商店商品、任务、成就、活动奖励选项等动态列表。不要接受 UnityEvent、"
        "Transform、材质、动画、渲染配置、调试初始化、坐标或纯技术缓存数组。"
        "referenced_by 是本地解析出的真实组件/Object 引用链；若它明确来自 ShopPopup、"
        "RewardShop、LuckySpin、任务页等玩家界面，应作为强证据。只能从输入 candidate_id "
        "中选择，禁止创造路径。宁可不接受也不要误删运行时技术数据。"
    )
    user_content = json.dumps(
        {"candidates": compact_candidates}, ensure_ascii=False, indent=2
    )
    allowed_public_ids = set(public_id_to_candidate_id)
    failures: list[str] = []
    result: dict | None = None
    used_transport = ""
    for transport in transports:
        display_name = "Codex 5.3" if transport == "codex_cli" else "DeepSeek"
        attempts = 1 if transport == "codex_cli" else 4
        for attempt in range(1, attempts + 1):
            try:
                print(
                    f"[动态列表][AI] {display_name} 复核困难候选："
                    f"{len(candidates)} 个"
                    + (f"，重试 {attempt}/{attempts}" if attempt > 1 else ""),
                    flush=True,
                )
                if transport == "codex_cli":
                    result, _usage = request_structured_output(
                        model=str(getattr(
                            cfg, "ai_dynamic_list_codex_model", "gpt-5.3-codex-spark"
                        )),
                        reasoning_effort=str(getattr(
                            cfg, "ai_dynamic_list_codex_reasoning_effort", "medium"
                        )),
                        system_prompt=system_prompt,
                        user_content=user_content,
                        output_schema=_dynamic_list_ai_schema(),
                        timeout=int(getattr(cfg, "ai_dynamic_list_timeout", 300)),
                        working_directory=cfg.root_dir,
                    )
                else:
                    import requests

                    session = requests.Session()
                    session.trust_env = False
                    proxies = {
                        key: value
                        for key, value in {
                            "http": str(getattr(cfg, "ai_dynamic_list_proxy_http", "")).strip(),
                            "https": str(getattr(cfg, "ai_dynamic_list_proxy_https", "")).strip(),
                        }.items()
                        if value
                    }
                    response = session.post(
                        str(getattr(cfg, "ai_dynamic_list_base_url", "")).strip().rstrip("/")
                        + "/chat/completions",
                        headers={
                            "Authorization": "Bearer "
                            + str(getattr(cfg, "ai_dynamic_list_api_key", "")).strip(),
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": str(getattr(cfg, "ai_dynamic_list_model", "")).strip(),
                            "messages": [
                                {"role": "system", "content": system_prompt + "只返回 JSON。"},
                                {"role": "user", "content": user_content},
                            ],
                            "temperature": 0,
                        },
                        proxies=proxies or None,
                        timeout=int(getattr(cfg, "ai_dynamic_list_timeout", 300)),
                    )
                    response.raise_for_status()
                    payload = response.json()
                    result = _parse_dynamic_list_ai_json(
                        str(payload["choices"][0]["message"]["content"])
                    )
                used_transport = transport
                break
            except Exception as exc:
                failures.append(f"{display_name}: {exc}")
                print(f"[动态列表][AI] {display_name} 请求失败: {exc}")
                if transport == "codex_cli":
                    print("[动态列表][AI] Codex 5.3 不可用，立即回退 DeepSeek。")
                    break
        if result is not None:
            break
    if result is None:
        print("[动态列表][AI] 自动复核失败，继续使用确定性扫描结果：" + "；".join(failures))
        return set()

    accepted_rows = result.get("accepted")
    if not isinstance(accepted_rows, list):
        print("[动态列表][AI] 返回缺少 accepted 数组，忽略 AI 结果。")
        return set()
    accepted_public_ids = {
        str(row.get("candidate_id", ""))
        for row in accepted_rows
        if isinstance(row, dict)
        and str(row.get("candidate_id", "")) in allowed_public_ids
    }
    accepted_ids = {
        public_id_to_candidate_id[public_id]
        for public_id in accepted_public_ids
    }
    accepted_kinds = {
        public_id_to_candidate_id[str(row.get("candidate_id", ""))]:
        str(row.get("kind", "其他动态列表"))
        for row in accepted_rows
        if isinstance(row, dict)
        and str(row.get("candidate_id", "")) in accepted_public_ids
    }
    DEFAULT_DYNAMIC_LIST_AI_REVIEW.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        DEFAULT_DYNAMIC_LIST_AI_REVIEW,
        {
            "version": 4,
            "fingerprint": fingerprint,
            "transport": used_transport,
            "candidate_count": len(candidates),
            "accepted_candidate_ids": sorted(accepted_ids),
            "accepted_kinds": accepted_kinds,
            "accepted": [
                row for row in accepted_rows
                if isinstance(row, dict)
                and str(row.get("candidate_id", "")) in accepted_public_ids
            ],
        },
    )
    print(
        f"[动态列表][AI] 复核完成：候选={len(candidates)}，"
        f"接受={len(accepted_ids)}，结果={DEFAULT_DYNAMIC_LIST_AI_REVIEW}"
    )
    return accepted_ids


def _store_product_image_label(product: dict) -> str:
    name = str(product.get("image_name") or product.get("sprite_name") or "").strip()
    kind = str(product.get("image_kind", "")).strip()
    if name:
        return f"{name} [{kind}]" if kind and kind != "sprite" else name
    path_id = int(
        product.get("image_path_id", 0)
        or product.get("sprite_path_id", 0)
        or product.get("texture_path_id", 0)
        or 0
    )
    return f"#{path_id}" if path_id else "<无图片>"


def _store_product_preview_image(product: dict):
    kind = str(product.get("image_kind", ""))
    scope = product.get("image_scope")
    if not isinstance(scope, dict):
        scope = product.get("sprite_scope")
    if kind == "ngui_sprite" and isinstance(scope, dict):
        return _preview_ngui_sprite_image(
            scope,
            int(product.get("ngui_atlas_path_id", 0) or 0),
            str(product.get("ngui_sprite_name", "")),
        )
    if kind == "texture" and isinstance(scope, dict):
        return _preview_texture_image(
            scope, int(product.get("texture_path_id", 0) or 0)
        )
    sprite_path_id = int(product.get("sprite_path_id", 0) or 0)
    if isinstance(scope, dict) and sprite_path_id:
        return _preview_sprite_image(scope, sprite_path_id)
    return None


def _load_store_product_records() -> dict:
    data = _safe_read_json(DEFAULT_STORE_PRODUCT_BLOCK_RECORD)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"version": 1, "items": []}
    return data


def _write_store_product_records(records: dict) -> None:
    DEFAULT_STORE_PRODUCT_BLOCK_RECORD.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(DEFAULT_STORE_PRODUCT_BLOCK_RECORD, records)


def _store_blocked_keys(config: dict, records: dict | None = None) -> set[str]:
    records = records or _load_store_product_records()
    blocked = {
        str(item.get("pointer_key", ""))
        for item in records.get("items", [])
        if isinstance(item, dict)
        and item.get("config_key") == config.get("config_key")
    }
    for product in config.get("products", []):
        if not isinstance(product, dict):
            continue
        legacy_key = str(product.get("legacy_pointer_key", ""))
        if legacy_key and legacy_key in blocked:
            blocked.add(str(product.get("pointer_key", "")))
    return blocked


def _rebuild_store_product_replacement(config: dict, records: dict) -> Path | None:
    source = Path(str(config.get("source_json", "")))
    original = _safe_read_json(source)
    if not isinstance(original, dict):
        raise ValueError(f"原始动态列表 JSON 不存在或无效: {source}")
    try:
        relative = source.relative_to(DEFAULT_SOURCE_ROOT)
    except ValueError as exc:
        raise ValueError("动态列表 JSON 不在 workspace/input 中") from exc
    target = DEFAULT_OBJECT_TO_IMPORT_ROOT / relative
    current = _safe_read_json(target)
    patched = dict(current) if isinstance(current, dict) else dict(original)
    blocked_keys = _store_blocked_keys(config, records)
    array_path = list(config.get("array_path") or ["_products", "Array"])
    original_products = _nested_value(original, array_path)
    if not isinstance(original_products, list):
        raise ValueError(f"动态列表数组路径无效: {'.'.join(array_path)}")
    kept_products = []
    for index, item in enumerate(original_products):
        entry_key = _store_array_item_key(item, index)
        legacy_key = (
            _store_product_pointer_key(*_pptr(item))
            if _is_serialized_pointer(item) else ""
        )
        if entry_key in blocked_keys or (legacy_key and legacy_key in blocked_keys):
            continue
        kept_products.append(item)
    target_parent: object = patched
    for key in array_path[:-1]:
        if not isinstance(target_parent, dict):
            raise ValueError(f"待导入 JSON 中的数组路径无效: {'.'.join(array_path)}")
        child = target_parent.get(key)
        if not isinstance(child, dict):
            child = {}
            target_parent[key] = child
        target_parent = child
    if not isinstance(target_parent, dict):
        raise ValueError(f"待导入 JSON 中的数组路径无效: {'.'.join(array_path)}")
    target_parent[array_path[-1]] = kept_products
    if not blocked_keys and patched == original:
        if target.is_file():
            target.unlink()
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, patched)
    return target


def _set_store_product_blocked(config: dict, product: dict, blocked: bool) -> bool:
    records = _load_store_product_records()
    items = [item for item in records["items"] if isinstance(item, dict)]
    record_key = f"{config['config_key']}|{product['pointer_key']}"
    product_keys = {
        str(product.get("pointer_key", "")),
        str(product.get("legacy_pointer_key", "")),
    } - {""}
    matched_record_keys = {
        str(item.get("record_key", ""))
        for item in items
        if item.get("config_key") == config.get("config_key")
        and str(item.get("pointer_key", "")) in product_keys
    }
    existed = bool(matched_record_keys) or any(
        item.get("record_key") == record_key for item in items
    )
    if blocked and not existed:
        items.append(
            {
                "record_key": record_key,
                "config_key": config["config_key"],
                "source": config.get("source", ""),
                "bundle_entry": config.get("bundle_entry", ""),
                "store_name": config.get("name", ""),
                "store_path_id": config.get("path_id", 0),
                "array_path": config.get("array_path", ["_products", "Array"]),
                "source_json": config.get("source_json", ""),
                "product_name": product.get("name", ""),
                "product_file_id": product.get("file_id", 0),
                "product_path_id": product.get("path_id", 0),
                "pointer_key": product.get("pointer_key", ""),
                "legacy_pointer_key": product.get("legacy_pointer_key", ""),
                "entry_kind": product.get("entry_kind", "pointer"),
                "inline_hash": product.get("inline_hash", ""),
                "original_index": product.get("index", 0),
                "sprite_name": product.get("sprite_name", ""),
                "sprite_path_id": product.get("sprite_path_id", 0),
                "image_kind": product.get("image_kind", ""),
                "image_name": product.get("image_name", ""),
                "image_path_id": product.get("image_path_id", 0),
                "texture_path_id": product.get("texture_path_id", 0),
                "ngui_atlas_path_id": product.get("ngui_atlas_path_id", 0),
                "ngui_sprite_name": product.get("ngui_sprite_name", ""),
                "sprite_source": (
                    (product.get("image_scope") or product.get("sprite_scope") or {}).get(
                        "source", ""
                    )
                    if isinstance(
                        product.get("image_scope") or product.get("sprite_scope"), dict
                    ) else ""
                ),
                "sprite_bundle_entry": (
                    (product.get("image_scope") or product.get("sprite_scope") or {}).get(
                        "bundle_entry", ""
                    )
                    if isinstance(
                        product.get("image_scope") or product.get("sprite_scope"), dict
                    ) else ""
                ),
                "prefab_name": product.get("prefab_name", ""),
            }
        )
    elif not blocked and existed:
        items = [
            item for item in items
            if item.get("record_key") != record_key
            and str(item.get("record_key", "")) not in matched_record_keys
        ]
    else:
        return False
    records["items"] = items
    target = _rebuild_store_product_replacement(config, records)
    _write_store_product_records(records)
    action = "屏蔽" if blocked else "恢复"
    print(
        f"[动态列表][{action}] {config['name']} -> {product['name']} "
        + (
            f"(内嵌索引={int(product.get('index', 0)) + 1})"
            if product.get("entry_kind") == "inline"
            else f"(PathID={product['path_id']}, 原始索引={int(product.get('index', 0)) + 1})"
        )
    )
    if target:
        print(f"[动态列表] 待导入 JSON: {target}")
    else:
        print("[动态列表] 该列表已无屏蔽项，已移除菜单 8 生成的冗余待导入文件。")
    return True


def _show_store_product_table_window(config: dict) -> bool:
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageTk

    window = tk.Tk()
    window.title(f"动态列表选择性屏蔽 - {config['name']}")
    window.geometry("1120x700")
    window.minsize(900, 560)
    window.configure(background="#12151b")
    changed = False
    photo_holder: dict[str, object] = {}

    header = (
        f"动态列表: {config['name']} (PathID={config['path_id']})\n"
        f"来源: {config['source']} | {config['bundle_entry'] or '<无 Bundle entry>'}\n"
        f"识别置信度: {config.get('classification', {}).get('confidence', '兼容模式')} | "
        f"数组: {config.get('array_label', '_products.Array')}\n"
        "屏蔽仅从数据数组中移除引用；不会删除共享图片、Prefab 或条目源数据。"
    )
    tk.Label(
        window, text=header, justify="left", anchor="w", background="#12151b",
        foreground="#43e081", font=("SimSun", 11, "bold"),
    ).pack(fill="x", padx=12, pady=10)

    body = tk.PanedWindow(window, orient="horizontal", sashwidth=6, background="#30343d")
    body.pack(fill="both", expand=True, padx=12)
    left = tk.Frame(body, background="#12151b")
    right = tk.Frame(body, background="#1c1f26")
    body.add(left, minsize=510)
    body.add(right, minsize=340)

    columns = ("status", "product", "sprite", "prefab", "ads")
    tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="extended")
    headings = {
        "status": "状态", "product": "条目", "sprite": "图片",
        "prefab": "Prefab", "ads": "广告条件",
    }
    widths = {"status": 70, "product": 120, "sprite": 150, "prefab": 120, "ads": 90}
    for column in columns:
        tree.heading(column, text=headings[column])
        tree.column(column, width=widths[column], anchor="w")
    scrollbar = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    tree.tag_configure("blocked", foreground="#ff5c5c")

    preview_label = tk.Label(right, background="#2a2e36", foreground="#a8adb8")
    preview_label.pack(fill="both", expand=True, padx=10, pady=10)
    detail_var = tk.StringVar()
    tk.Label(
        right, textvariable=detail_var, justify="left", anchor="nw",
        background="#1c1f26", foreground="#f0a04b", font=("Consolas", 10),
    ).pack(fill="x", padx=10, pady=(0, 10))

    def refresh() -> None:
        blocked_keys = _store_blocked_keys(config)
        selected_keys = {
            config["products"][int(item_id)].get("pointer_key")
            for item_id in tree.selection()
            if str(item_id).isdigit()
        }
        for item_id in tree.get_children():
            tree.delete(item_id)
        for index, product in enumerate(config["products"]):
            blocked = product["pointer_key"] in blocked_keys
            ads = (
                f"是 / {product['reward_ads_to_show']} 次"
                if product["reward_ads"] else "否"
            )
            tree.insert(
                "", "end", iid=str(index),
                values=("已屏蔽" if blocked else "显示", product["name"],
                        _store_product_image_label(product),
                        product["prefab_name"] or f"#{product['prefab_path_id']}", ads),
                tags=("blocked",) if blocked else (),
            )
            if product["pointer_key"] in selected_keys:
                tree.selection_add(str(index))

    def selected_products() -> list[dict]:
        return [
            config["products"][int(item_id)]
            for item_id in tree.selection()
            if str(item_id).isdigit()
        ]

    def show_product(_event=None) -> None:
        selected = selected_products()
        if not selected:
            return
        product = selected[0]
        detail_var.set(
            f"条目索引: {product['index']}\n"
            f"条目数据: {product['name']} (PathID={product['path_id']})\n"
            f"图片: {_store_product_image_label(product)} "
            f"(字段={product.get('image_field') or product.get('sprite_field', '')})\n"
            f"背景 Sprite: {product['back_sprite_name']} (PathID={product['back_sprite_path_id']})\n"
            f"Prefab: {product['prefab_name']} (组件 PathID={product['prefab_path_id']})\n"
            f"免费广告: {'是' if product['reward_ads'] else '否'}；次数={product['reward_ads_to_show']}\n"
            f"条目证据分: {product.get('evidence_score', 0)}"
        )
        image = _store_product_preview_image(product)
        if image is None:
            photo_holder.clear()
            preview_label.configure(image="", text="该条目没有可解码的图片预览")
            return
        image.thumbnail((430, 430))
        photo = ImageTk.PhotoImage(image)
        photo_holder["image"] = photo
        preview_label.configure(image=photo, text="")

    def set_selected(blocked: bool) -> None:
        nonlocal changed
        for product in selected_products():
            changed = _set_store_product_blocked(config, product, blocked) or changed
        refresh()
        show_product()

    buttons = tk.Frame(window, background="#12151b")
    buttons.pack(fill="x", padx=12, pady=10)
    ttk.Button(buttons, text="屏蔽所选条目", command=lambda: set_selected(True)).pack(side="left")
    ttk.Button(buttons, text="完成", command=window.destroy).pack(side="right")
    context = tk.Menu(window, tearoff=False)
    context.add_command(label="屏蔽此条目", command=lambda: set_selected(True))

    def open_context(event) -> None:
        row = tree.identify_row(event.y)
        if row and row not in tree.selection():
            tree.selection_set(row)
            show_product()
        if row:
            context.tk_popup(event.x_root, event.y_root)

    tree.bind("<<TreeviewSelect>>", show_product)
    tree.bind("<Button-3>", open_context)
    refresh()
    if config["products"]:
        tree.selection_set("0")
        show_product()
    window.mainloop()
    return changed


def _walk_serialized_pointers(value: object, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        if any(key in value for key in ("m_PathID", "PathID")):
            file_id, path_id = _pptr(value)
            if path_id:
                yield path, file_id, path_id
            return
        for key, child in value.items():
            yield from _walk_serialized_pointers(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_serialized_pointers(child, path + (str(index),))


def _pointer_game_object(scope: dict, file_id: int, path_id: int) -> tuple[dict, int] | None:
    target_scope = _resolve_pointer_scope(scope, file_id)
    if not target_scope:
        return None
    if _scope_entry(target_scope, ("GameObject",), path_id):
        return target_scope, path_id
    entry = _scope_entry(
        target_scope,
        ("RectTransform", "Transform", "MonoBehaviour"),
        path_id,
    )
    data = _entry_data(entry) if entry else None
    object_file_id, object_path_id = _pptr(
        data.get("m_GameObject") if isinstance(data, dict) else None
    )
    object_scope = _resolve_pointer_scope(target_scope, object_file_id)
    return (object_scope, object_path_id) if object_scope and object_path_id else None


def _grid_layout_settings(scope: dict, game_object_path_id: int) -> dict | None:
    entry = _scope_entry(scope, ("GameObject",), game_object_path_id)
    data = _entry_data(entry) if entry else None
    if not isinstance(data, dict):
        return None
    for component_path_id in _game_object_component_path_ids(data):
        component = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        component_data = _entry_data(component) if component else None
        if not isinstance(component_data, dict):
            continue
        cell_size = component_data.get("m_CellSize")
        if not isinstance(cell_size, dict):
            continue
        return {
            "cell_size": _vec2(cell_size, (200.0, 200.0)),
            "spacing": _vec2(component_data.get("m_Spacing"), (0.0, 0.0)),
            "padding": component_data.get("m_Padding", {}),
            "child_alignment": int(component_data.get("m_ChildAlignment", 0) or 0),
            "start_corner": int(component_data.get("m_StartCorner", 0) or 0),
            "start_axis": int(component_data.get("m_StartAxis", 0) or 0),
            "constraint": int(component_data.get("m_Constraint", 0) or 0),
            "constraint_count": max(1, int(component_data.get("m_ConstraintCount", 1) or 1)),
        }
    return None


def _rect_transform_reference_size(transform: dict | None) -> tuple[float, float]:
    """Return fixed RectTransform axes and mark stretch-anchored axes as zero."""
    if not isinstance(transform, dict):
        return (0.0, 0.0)
    width, height = _vec2(transform.get("m_SizeDelta"), (0.0, 0.0))
    anchor_min = transform.get("m_AnchorMin")
    anchor_max = transform.get("m_AnchorMax")
    if isinstance(anchor_min, dict) and isinstance(anchor_max, dict):
        if abs(_number(anchor_max.get("x")) - _number(anchor_min.get("x"))) > 0.001:
            width = 0.0
        if abs(_number(anchor_max.get("y")) - _number(anchor_min.get("y"))) > 0.001:
            height = 0.0
    return (width, height)


def _store_layout_root(chain: list[dict]) -> tuple[int, dict, int]:
    def score(node: dict) -> int:
        name = str(node.get("name", "")).casefold()
        if "shop prefab" in name or "store prefab" in name:
            return 120
        if "customsstore" in name or "shop window" in name or "store window" in name:
            return 110
        if "shop" in name:
            return 80
        if "store" in name:
            return 70
        if "market panel" in name:
            return 55
        if any(hint in name for hint in ("trade", "trader", "merchant", "vendor")):
            return 100
        if any(hint in name for hint in ("luckyspin", "lucky spin", "spin popup", "wheel")):
            return 105
        if any(hint in name for hint in ("task window", "quest window", "mission window")):
            return 110
        if any(hint in name for hint in ("achievement", "challenge", "objective")):
            return 95
        if any(hint in name for hint in TASK_LIST_HINTS):
            return 80
        if "list" in name:
            return 50
        return 0

    ranked = [(score(node), index, node) for index, node in enumerate(chain)]
    best_score, best_index, best_node = max(ranked, default=(0, 0, chain[0]), key=lambda row: (row[0], row[1]))
    return (best_index, best_node, best_score) if best_score else (0, chain[0], 0)


def _find_instantiated_list_layout(config: dict) -> dict | None:
    """Recognize NGUI/manual layouts whose array already points at scene item instances."""
    scope = config.get("scope")
    if not isinstance(scope, dict):
        return None
    config_entry = _scope_entry(
        scope, ("MonoBehaviour",), int(config.get("path_id", 0) or 0)
    )
    config_data = _entry_data(config_entry) if config_entry else None
    _config_file_id, config_object_id = _pptr(
        config_data.get("m_GameObject") if isinstance(config_data, dict) else None
    )
    if not config_object_id:
        return None
    item_object_ids = [
        int(product.get("product_object_path_id", 0) or 0)
        for product in config.get("products", [])
    ]
    if not item_object_ids or any(not value for value in item_object_ids):
        return None
    father_transform_ids: set[int] = set()
    for object_id in item_object_ids:
        transform = _find_game_object_transform(scope, object_id)
        file_id, father_transform_id = _pptr(
            transform.get("m_Father") if isinstance(transform, dict) else None
        )
        if file_id != 0 or not father_transform_id:
            return None
        father_transform_ids.add(father_transform_id)
    if len(father_transform_ids) != 1:
        return None
    father_transform_id = next(iter(father_transform_ids))
    father_entry = _scope_entry(
        scope, ("Transform", "RectTransform"), father_transform_id
    )
    father_data = _entry_data(father_entry) if father_entry else None
    father_file_id, content_object_id = _pptr(
        father_data.get("m_GameObject") if isinstance(father_data, dict) else None
    )
    if father_file_id != 0 or not content_object_id:
        return None
    item_chain = _object_chain_direct(scope, item_object_ids[0], 32)
    chain_ids = {int(node.get("path_id", 0) or 0) for node in item_chain}
    root_object_id = config_object_id if config_object_id in chain_ids else content_object_id
    root_entry = _scope_entry(scope, ("GameObject",), root_object_id)
    return {
        "score": 260,
        "mode": "instantiated",
        "scope": scope,
        "component_path_id": int(config.get("path_id", 0) or 0),
        "component_object_id": config_object_id,
        "root_path_id": root_object_id,
        "root_name": _game_object_name(scope, root_object_id),
        "root_source_json": str(root_entry["path"]) if root_entry else "",
        "force_active_path_ids": sorted(
            {root_object_id, content_object_id, *item_object_ids}
        ),
        "content_scope": scope,
        "content_path_id": content_object_id,
        "template_scope": scope,
        "template_path_id": item_object_ids[0],
        "item_path_ids": item_object_ids,
        "grid": None,
        "reference_size": (0.0, 0.0),
        "reference_field": config.get("array_label", ""),
    }


def _runtime_shell_layout(
    shell: dict,
    scopes: dict,
    score: int,
    config: dict | None = None,
) -> dict | None:
    scope = _runtime_shell_scope(shell, scopes)
    if scope is None:
        return None
    component_path_id = int(shell.get("component_path_id", 0) or 0)
    component_entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
    data = _entry_data(component_entry) if component_entry else None
    if not isinstance(data, dict):
        return None
    _object_file_id, component_object_id = _pptr(data.get("m_GameObject"))
    chain = _object_chain_direct(scope, component_object_id, 64)
    if not chain:
        return None
    root_index, root_node, root_score = _store_layout_root(chain)
    pointers = list(_walk_serialized_pointers(data))
    config_leaf = ""
    if isinstance(config, dict):
        config_parts = [
            str(part) for part in config.get("array_path", [])
            if str(part).casefold() != "array"
        ]
        if config_parts:
            config_leaf = re.sub(r"[^a-z0-9]", "", config_parts[-1].casefold())
    content_candidates: list[tuple[int, int, tuple[dict, int]]] = []
    template_candidates: list[tuple[int, int, tuple[dict, int]]] = []
    for pointer_index, (path, file_id, path_id) in enumerate(pointers):
        field = ".".join(path).casefold()
        target = _pointer_game_object(scope, file_id, path_id)
        if not target:
            continue
        content_score = 0
        if any(
            hint in field
            for hint in ("content", "grid", "list", "container", "scroll", "itemsroot")
        ):
            content_score += 30
        normalized_field = re.sub(r"[^a-z0-9]", "", field)
        if config_leaf and (
            config_leaf in normalized_field or normalized_field in config_leaf
        ):
            content_score += 240
        if content_score:
            content_candidates.append((content_score, -pointer_index, target))
        if any(
            hint in field
            for hint in (
                "buttonprefab", "itemaddonprefab", "template", "prefab",
                "itemview", "entry", "card", "row",
            )
        ):
            template_candidates.append((30, -pointer_index, target))
    content = max(content_candidates, default=None, key=lambda row: row[:2])
    template = max(template_candidates, default=None, key=lambda row: row[:2])
    content_target = content[2] if content is not None else None
    template_target = template[2] if template is not None else None
    if content_target is None or template_target is None:
        return None
    grid = _grid_layout_settings(*content_target)
    content_transform = _find_game_object_transform(
        content_target[0], content_target[1]
    )
    content_size = _rect_transform_reference_size(content_transform)
    panel_transform = _find_game_object_transform(scope, component_object_id)
    panel_size = _rect_transform_reference_size(panel_transform)
    reference_size = (
        content_size
        if abs(content_size[0]) > 0.01 or abs(content_size[1]) > 0.01
        else panel_size
    )
    return {
        "score": int(score) + root_score + (30 if grid else 0),
        "mode": "grid" if grid else "runtime_template",
        "scope": scope,
        "component_path_id": component_path_id,
        "component_object_id": component_object_id,
        "root_path_id": int(root_node["path_id"]),
        "root_name": str(root_node["name"]),
        "root_source_json": str(root_node.get("source_json", "")),
        "force_active_path_ids": _game_object_subtree_path_ids(
            scope, int(root_node["path_id"])
        ),
        "content_scope": content_target[0],
        "content_path_id": content_target[1],
        "template_scope": template_target[0],
        "template_path_id": template_target[1],
        "grid": grid,
        "reference_size": reference_size,
        "reference_field": "语义匹配运行时界面",
        "runtime_shell": shell,
    }


def _store_config_shell_score(
    config: dict,
    shell: dict,
    reference_bundle_entries: set[str],
    scopes: dict,
) -> int:
    config_blob = " ".join(
        [
            str(config.get("name", "")),
            str(config.get("array_label", "")),
            *[
                str(value)
                for product in config.get("products", [])[:1]
                for value in product.get("field_signature", [])
            ],
        ]
    ).casefold()
    shell_blob = str(shell.get("name", "")).casefold()
    score = 0
    layout = _runtime_shell_layout(shell, scopes, 0, config)
    if layout is None:
        return 0
    shell_scope = layout.get("scope")
    shell_entry = _scope_entry(
        shell_scope,
        ("MonoBehaviour",),
        int(shell.get("component_path_id", 0) or 0),
    ) if isinstance(shell_scope, dict) else None
    shell_data = _entry_data(shell_entry) if shell_entry else None
    direct_reference = bool(
        isinstance(shell_data, dict)
        and any(
            path_id == int(config.get("path_id", 0) or 0)
            and _resolve_pointer_scope(shell_scope, file_id) is config.get("scope")
            for _path, file_id, path_id in _walk_serialized_pointers(shell_data)
        )
    )
    semantic_matched = direct_reference
    if direct_reference:
        score += 320
    semantic_groups = (
        (("trade", "trader", "merchant", "vendor", "sell", "buy"),
         ("trade", "trader", "merchant", "vendor")),
        (("spin", "lucky"), ("spin", "lucky", "wheel")),
        (("skin",), ("skin",)),
        (("wing", "float", "glove"), ("wing",)),
        (("reward", "gift"), ("reward", "gift")),
        (("pet",), ("pet",)),
        (("task", "quest", "mission"), ("task", "quest", "mission")),
    )
    config_has_domain = False
    for config_hints, shell_hints in semantic_groups:
        if not any(hint in config_blob for hint in config_hints):
            continue
        config_has_domain = True
        if any(hint in shell_blob for hint in shell_hints):
            semantic_matched = True
            score += 180
        elif not direct_reference:
            # A same-level bundle is not proof that two distinct shop domains
            # share a view.  In particular, do not draw pet data in a reward or
            # wing shop merely because both live in sharedassets2.
            return 0
        break
    if not config_has_domain and (
        any(hint in config_blob for hint in ("shop", "store", "product", "item"))
        and any(hint in shell_blob for hint in ("shop", "store"))
    ):
        semantic_matched = True
        score += 35
    if not semantic_matched:
        return 0
    shell_bundle = str(shell.get("bundle_entry", "")).casefold()
    if shell_bundle and shell_bundle in reference_bundle_entries:
        score += 120
    if layout.get("template_scope") is config.get("scope"):
        score += 90
    return score


def _component_instantiated_item_layout(
    config: dict,
    scope: dict,
    component_path_id: int,
    component_object_id: int,
    data: dict,
    chain: list[dict],
    root_index: int,
    root_node: dict,
    root_score: int,
) -> dict | None:
    product_count = len(config.get("products", []))
    if product_count <= 0:
        return None

    def normalized_leaf(path: tuple[str, ...] | list[str]) -> str:
        parts = [str(part) for part in path if str(part).casefold() != "array"]
        value = re.sub(r"[^a-z0-9]", "", parts[-1].casefold() if parts else "")
        return value[1:] if value.startswith("m") else value

    config_leaf = normalized_leaf(config.get("array_path", []))
    ranked: list[tuple[int, str, list[int]]] = []
    for array_path, values in _walk_serialized_arrays(data):
        if len(values) != product_count or not values:
            continue
        if not all(_is_serialized_pointer(value) for value in values):
            continue
        object_ids: list[int] = []
        valid = True
        for pointer in values:
            file_id, path_id = _pptr(pointer)
            target = _pointer_game_object(scope, file_id, path_id)
            if target is None or target[0] is not scope or not target[1]:
                valid = False
                break
            object_ids.append(int(target[1]))
        if not valid or len(set(object_ids)) != len(object_ids):
            continue
        array_leaf = normalized_leaf(array_path)
        semantic_score = 220 if config_leaf and array_leaf == config_leaf else 0
        path_label = ".".join(array_path)
        if any(
            hint in path_label.casefold()
            for hint in ("item", "reward", "offer", "product", "slot", "entry")
        ):
            semantic_score += 60
        if semantic_score:
            ranked.append((semantic_score, path_label, object_ids))
    if not ranked:
        return None
    semantic_score, path_label, object_ids = max(
        ranked, key=lambda row: (row[0], row[1])
    )
    root_path_id = int(root_node.get("path_id", 0) or 0)
    root_entry = _scope_entry(scope, ("GameObject",), root_path_id)
    return {
        "score": 300 + root_score + semantic_score,
        "mode": "instantiated",
        "scope": scope,
        "component_path_id": int(component_path_id),
        "component_object_id": int(component_object_id),
        "root_path_id": root_path_id,
        "root_name": str(root_node.get("name", "")),
        "root_source_json": str(root_entry["path"]) if root_entry else "",
        "force_active_path_ids": _game_object_subtree_path_ids(scope, root_path_id),
        "content_scope": scope,
        "content_path_id": component_object_id,
        "template_scope": scope,
        "template_path_id": object_ids[0],
        "item_path_ids": object_ids,
        "overlay_product_images": True,
        "grid": None,
        "reference_size": (0.0, 0.0),
        "reference_field": path_label,
    }


def _find_store_layout(
    config: dict,
    scopes: dict,
    candidates: list[tuple[tuple[str, str], dict, str, int, dict]] | None = None,
    runtime_shells: list[dict] | None = None,
) -> dict | None:
    config_scope = config["scope"]
    config_path_id = int(config.get("path_id", 0) or 0)
    if candidates is None:
        candidates = _prefilter_reference_entries_across_scopes(
            scopes, {"MonoBehaviour"}, {config_path_id}
        )
    layouts: list[dict] = []
    reference_bundle_entries: set[str] = set()
    for _scope_key, scope, _type_name, component_path_id, entry in candidates:
        data = _entry_data(entry)
        if not isinstance(data, dict):
            continue
        pointers = list(_walk_serialized_pointers(data))
        exact_reference = any(
            path_id == config_path_id
            and _resolve_pointer_scope(scope, file_id) is config_scope
            and not any(
                token in ".".join(_path).casefold()
                for token in ("onclick", "persistentcalls", "event", "callback")
            )
            for _path, file_id, path_id in pointers
        )
        if not exact_reference:
            continue
        reference_bundle_entries.add(
            str(scope.get("bundle_entry", "")).casefold()
        )
        _object_file_id, component_object_id = _pptr(data.get("m_GameObject"))
        if not component_object_id:
            continue
        chain = _object_chain_direct(scope, component_object_id, 24)
        if not chain:
            continue
        root_index, root_node, root_score = _store_layout_root(chain)
        content: tuple[dict, int] | None = None
        template: tuple[dict, int] | None = None
        for path, file_id, path_id in pointers:
            field = ".".join(path).casefold()
            target = _pointer_game_object(scope, file_id, path_id)
            if not target:
                continue
            if content is None and any(hint in field for hint in ("content", "grid", "list", "container")):
                content = target
            if template is None and any(
                hint in field
                for hint in (
                    "product", "item", "view", "prefab", "template", "entry",
                    "row", *TASK_LIST_HINTS,
                )
            ):
                template = target
        instantiated_layout = _component_instantiated_item_layout(
            config,
            scope,
            component_path_id,
            component_object_id,
            data,
            chain,
            root_index,
            root_node,
            root_score,
        )
        if instantiated_layout is not None:
            layouts.append(instantiated_layout)
        grid = _grid_layout_settings(*content) if content else None
        # A data-holder MonoBehaviour often references the config but has no
        # visual list of its own.  Keep its bundle as linkage evidence, but do
        # not mistake that holder for a shop layout.
        if content is None or template is None:
            continue
        content_transform = _find_game_object_transform(content[0], content[1])
        content_size = _rect_transform_reference_size(content_transform)
        panel_transform = _find_game_object_transform(scope, component_object_id)
        panel_size = _rect_transform_reference_size(panel_transform)
        reference_size = (
            content_size
            if abs(content_size[0]) > 0.01 or abs(content_size[1]) > 0.01
            else panel_size
        )
        score = (
            100 + root_score + (35 if content else 0)
            + (35 if template else 0) + (30 if grid else 0)
        )
        layouts.append(
            {
                "score": score,
                "scope": scope,
                "component_path_id": component_path_id,
                "component_object_id": component_object_id,
                "root_path_id": int(root_node["path_id"]),
                "root_name": str(root_node["name"]),
                "root_source_json": str(root_node.get("source_json", "")),
                "force_active_path_ids": [
                    int(node["path_id"]) for node in chain[:root_index + 1]
                ],
                "content_scope": content[0] if content else None,
                "content_path_id": content[1] if content else 0,
                "template_scope": template[0] if template else None,
                "template_path_id": template[1] if template else 0,
                "grid": grid,
                "mode": "grid" if grid else "runtime_template",
                "reference_size": reference_size,
                "reference_field": next(
                    (".".join(path) for path, file_id, path_id in pointers
                     if path_id == config_path_id
                     and _resolve_pointer_scope(scope, file_id) is config_scope),
                    "",
                ),
            }
        )
    instantiated = _find_instantiated_list_layout(config)
    if instantiated is not None:
        layouts.append(instantiated)
    for shell in runtime_shells or []:
        semantic_score = _store_config_shell_score(
            config, shell, reference_bundle_entries, scopes
        )
        if semantic_score < 150:
            continue
        semantic_layout = _runtime_shell_layout(
            shell, scopes, semantic_score, config
        )
        if semantic_layout is not None:
            layouts.append(semantic_layout)
    if not layouts:
        return None
    layouts.sort(
        key=lambda row: (
            -int(row["score"]),
            str(row["scope"].get("bundle_entry", "")).casefold(),
            int(row["component_path_id"]),
        )
    )
    return layouts[0]


def _grid_layout_slots(
    count: int,
    rect: tuple[float, float, float, float],
    settings: dict,
    reference_size: tuple[float, float],
) -> list[tuple[float, float, float, float]]:
    if count <= 0:
        return []
    x, y, width, height = rect
    cell_width, cell_height = settings.get("cell_size", (200.0, 200.0))
    spacing_x, spacing_y = settings.get("spacing", (0.0, 0.0))
    serialized_width = abs(reference_size[0])
    serialized_height = abs(reference_size[1])
    if serialized_width > 0.01 and serialized_height > 0.01:
        stretch_fallback = False
        reference_width = serialized_width
        reference_height = serialized_height
        scale_x, scale_y = width / reference_width, height / reference_height
    else:
        stretch_fallback = True
        # Stretch-anchored runtime Content commonly serializes one SizeDelta
        # axis as zero while an empty preview reports the other axis as 1 px.
        # Scaling each axis from those degenerate values collapses every card.
        # In that case use the GridLayout's Unity units directly and let the
        # scrollable preview canvas expand to contain every generated row.
        reference_width = max(width, cell_width)
        reference_height = max(height, cell_height)
        scale_x = scale_y = 1.0
    padding = settings.get("padding") if isinstance(settings.get("padding"), dict) else {}
    left = float(padding.get("m_Left", 0) or 0)
    right = float(padding.get("m_Right", 0) or 0)
    top = float(padding.get("m_Top", 0) or 0)
    bottom = float(padding.get("m_Bottom", 0) or 0)
    available_width = max(cell_width, reference_width - left - right)
    available_height = max(cell_height, reference_height - top - bottom)
    constraint = int(settings.get("constraint", 0) or 0)
    constraint_count = max(1, int(settings.get("constraint_count", 1) or 1))
    start_axis = int(settings.get("start_axis", 0) or 0)
    if constraint == 1:
        columns = constraint_count
        rows = math.ceil(count / columns)
    elif constraint == 2:
        rows = constraint_count
        columns = math.ceil(count / rows)
    elif start_axis == 0:
        columns = max(1, math.floor((available_width + spacing_x) / (cell_width + spacing_x)))
        rows = math.ceil(count / columns)
    else:
        rows = max(1, math.floor((available_height + spacing_y) / (cell_height + spacing_y)))
        columns = math.ceil(count / rows)
    total_width = columns * cell_width + max(0, columns - 1) * spacing_x
    total_height = rows * cell_height + max(0, rows - 1) * spacing_y
    if stretch_fallback:
        available_width = max(available_width, total_width)
        available_height = max(available_height, total_height)
    alignment = int(settings.get("child_alignment", 0) or 0)
    horizontal_alignment = alignment % 3
    vertical_alignment = alignment // 3
    start_x = left + (available_width - total_width) * (horizontal_alignment / 2.0)
    start_y = top + (available_height - total_height) * (vertical_alignment / 2.0)
    corner = int(settings.get("start_corner", 0) or 0)
    result: list[tuple[float, float, float, float]] = []
    for index in range(count):
        if start_axis == 0:
            row, column = divmod(index, columns)
        else:
            column, row = divmod(index, rows)
        if corner in {1, 3}:
            column = columns - 1 - column
        if corner in {2, 3}:
            row = rows - 1 - row
        result.append(
            (
                x + (start_x + column * (cell_width + spacing_x)) * scale_x,
                y + (start_y + row * (cell_height + spacing_y)) * scale_y,
                cell_width * scale_x,
                cell_height * scale_y,
            )
        )
    return result


def _runtime_template_size(
    scope: dict | None,
    game_object_path_id: int,
) -> tuple[float, float]:
    if not isinstance(scope, dict) or not game_object_path_id:
        return (190.0, 230.0)
    transform = _find_game_object_transform(scope, game_object_path_id)
    width, height = _vec2(
        transform.get("m_SizeDelta") if isinstance(transform, dict) else None,
        (0.0, 0.0),
    )
    width, height = abs(width), abs(height)
    object_entry = _scope_entry(scope, ("GameObject",), game_object_path_id)
    object_data = _entry_data(object_entry) if object_entry else None
    if isinstance(object_data, dict):
        for component_path_id in _game_object_component_path_ids(object_data):
            component_entry = _scope_entry(
                scope, ("MonoBehaviour", "SpriteRenderer"), component_path_id
            )
            component_data = _entry_data(component_entry) if component_entry else None
            if not isinstance(component_data, dict):
                continue
            width = max(width, abs(_number(component_data.get("mWidth"), 0.0)))
            height = max(height, abs(_number(component_data.get("mHeight"), 0.0)))
    return (
        width if width > 8.0 else 190.0,
        height if height > 8.0 else 230.0,
    )


def _template_dynamic_image_object_ids(
    scope: dict | None,
    template_path_id: int,
) -> list[int]:
    """Find template image objects that a runtime item controller replaces."""
    if not isinstance(scope, dict) or not template_path_id:
        return []
    template_entry = _scope_entry(scope, ("GameObject",), template_path_id)
    template_data = _entry_data(template_entry) if template_entry else None
    if not isinstance(template_data, dict):
        return []
    result: list[int] = []
    for component_path_id in _game_object_component_path_ids(template_data):
        component_entry = _scope_entry(scope, ("MonoBehaviour",), component_path_id)
        component_data = _entry_data(component_entry) if component_entry else None
        if not isinstance(component_data, dict):
            continue
        for field_name, pointer in component_data.items():
            folded = str(field_name).casefold()
            if folded in {"m_gameobject", "m_script", "m_sprite", "m_texture"}:
                continue
            if not any(hint in folded for hint in STORE_IMAGE_FIELD_HINTS):
                continue
            file_id, path_id = _pptr(pointer)
            if not path_id:
                continue
            target = _pointer_game_object(scope, file_id, path_id)
            if target is None or target[0] is not scope or not target[1]:
                continue
            object_id = int(target[1])
            if object_id not in result:
                result.append(object_id)
    return result


def _runtime_template_slots(
    count: int,
    content_rect: tuple[float, float, float, float],
    template_scope: dict | None,
    template_path_id: int,
) -> list[tuple[float, float, float, float]]:
    if count <= 0:
        return []
    x, y, width, height = content_rect
    cell_width, cell_height = _runtime_template_size(
        template_scope, template_path_id
    )
    gap_x = max(8.0, cell_width * 0.08)
    gap_y = max(8.0, cell_height * 0.08)
    horizontal = width >= height * 1.15
    if horizontal:
        rows = max(1, math.floor(max(cell_height, height) / (cell_height + gap_y)))
        rows = min(rows, count)
        columns = math.ceil(count / rows)
    else:
        columns = max(1, math.floor(max(cell_width, width) / (cell_width + gap_x)))
        columns = min(columns, count)
        rows = math.ceil(count / columns)
    slots: list[tuple[float, float, float, float]] = []
    for index in range(count):
        if horizontal:
            column, row = divmod(index, rows)
        else:
            row, column = divmod(index, columns)
        slots.append((
            x + column * (cell_width + gap_x),
            y + row * (cell_height + gap_y),
            cell_width,
            cell_height,
        ))
    return slots


def _render_store_layout(config: dict, layout: dict, scopes: dict):
    from PIL import Image, ImageDraw

    scope = layout["scope"]
    match = {
        "source": scope.get("source", ""),
        "bundle_entry": scope.get("bundle_entry", ""),
        "component_type": "GameObject",
        "component_path_id": layout["root_path_id"],
        "chain": [{
            "path_id": layout["root_path_id"],
            "name": layout["root_name"],
            "source_json": layout["root_source_json"],
        }],
        "force_active_path_ids": layout.get("force_active_path_ids", []),
    }
    preview_path = _create_object_hierarchy_preview(match, 0, scopes)
    with Image.open(preview_path) as source_image:
        image = source_image.convert("RGBA")
    metadata = _safe_read_json(preview_path.with_suffix(".regions.json"))
    nodes = metadata.get("tree_nodes", []) if isinstance(metadata, dict) else []
    instantiated_layout = layout.get("mode") == "instantiated"
    overlay_product_images = bool(layout.get("overlay_product_images"))
    card_image = None
    template_image_box: tuple[float, float, float, float] | None = None
    if instantiated_layout:
        node_by_id = {
            int(node.get("path_id", 0) or 0): node
            for node in nodes if isinstance(node, dict)
        }

        def subtree_rect(root_path_id: int) -> tuple[float, float, float, float] | None:
            pending = [root_path_id]
            subtree_nodes: list[dict] = []
            seen: set[int] = set()
            while pending:
                path_id = pending.pop()
                if path_id in seen:
                    continue
                seen.add(path_id)
                node = node_by_id.get(path_id)
                if not isinstance(node, dict):
                    continue
                subtree_nodes.append(node)
                pending.extend(
                    int(value) for value in node.get("children", [])
                    if str(value).lstrip("-").isdigit()
                )
            if not subtree_nodes:
                return None
            min_x = min(float(node.get("x", 0.0)) for node in subtree_nodes)
            min_y = min(float(node.get("y", 0.0)) for node in subtree_nodes)
            max_x = max(
                float(node.get("x", 0.0)) + max(1.0, float(node.get("width", 1.0)))
                for node in subtree_nodes
            )
            max_y = max(
                float(node.get("y", 0.0)) + max(1.0, float(node.get("height", 1.0)))
                for node in subtree_nodes
            )
            return (min_x, min_y, max_x - min_x, max_y - min_y)

        slots = []
        for item_path_id in layout.get("item_path_ids", []):
            item_node = node_by_id.get(int(item_path_id))
            item_rect = None
            if isinstance(item_node, dict):
                width = float(item_node.get("width", 0.0) or 0.0)
                height = float(item_node.get("height", 0.0) or 0.0)
                if width > 2.0 and height > 2.0:
                    item_rect = (
                        float(item_node.get("x", 0.0)),
                        float(item_node.get("y", 0.0)),
                        width,
                        height,
                    )
            if item_rect is None:
                item_rect = subtree_rect(int(item_path_id))
            if item_rect is not None:
                slots.append(item_rect)
        if len(slots) != len(config["products"]):
            raise ValueError("已实例化动态列表的条目层级范围不完整")
        visual_nodes = [
            node for node in nodes
            if int(node.get("path_id", 0) or 0) != int(layout.get("root_path_id", 0) or 0)
            and float(node.get("width", 0.0) or 0.0) < image.width * 0.9
            and float(node.get("height", 0.0) or 0.0) < image.height * 0.9
            and float(node.get("width", 0.0) or 0.0) > 2.0
            and float(node.get("height", 0.0) or 0.0) > 2.0
        ]
        if visual_nodes:
            padding = 18
            crop_left = max(
                0,
                math.floor(min(float(node["x"]) for node in visual_nodes) - padding),
            )
            crop_top = max(
                0,
                math.floor(min(float(node["y"]) for node in visual_nodes) - padding),
            )
            crop_right = min(
                image.width,
                math.ceil(max(
                    float(node["x"]) + float(node["width"])
                    for node in visual_nodes
                ) + padding),
            )
            crop_bottom = min(
                image.height,
                math.ceil(max(
                    float(node["y"]) + float(node["height"])
                    for node in visual_nodes
                ) + padding),
            )
            if crop_right > crop_left and crop_bottom > crop_top:
                image = image.crop((crop_left, crop_top, crop_right, crop_bottom))
                slots = [
                    (x - crop_left, y - crop_top, width, height)
                    for x, y, width, height in slots
                ]
    else:
        content_node = next(
            (
                node for node in nodes
                if int(node.get("path_id", 0) or 0)
                == int(layout.get("content_path_id", 0) or 0)
            ),
            None,
        )
        if not isinstance(content_node, dict):
            raise ValueError("已找到动态列表对象，但缺少可定位的 Content 区域")
        content_rect = tuple(
            float(content_node[key]) for key in ("x", "y", "width", "height")
        )
        if layout.get("grid"):
            slots = _grid_layout_slots(
                len(config["products"]), content_rect, layout["grid"], layout["reference_size"]
            )
        elif layout.get("mode") == "runtime_template":
            slots = _runtime_template_slots(
                len(config["products"]),
                content_rect,
                layout.get("template_scope"),
                int(layout.get("template_path_id", 0) or 0),
            )
        else:
            raise ValueError("已找到动态列表对象，但缺少 GridLayout 或可复用条目模板")
        if slots:
            padding = 24
            left = min(0, math.floor(min(x for x, _y, _width, _height in slots) - padding))
            top = min(0, math.floor(min(y for _x, y, _width, _height in slots) - padding))
            right = max(
                image.width,
                math.ceil(max(x + width for x, _y, width, _height in slots) + padding),
            )
            bottom = max(
                image.height,
                math.ceil(max(y + height for _x, y, _width, height in slots) + padding),
            )
            if left < 0 or top < 0 or right > image.width or bottom > image.height:
                expanded = Image.new(
                    "RGBA",
                    (right - left, bottom - top),
                    (28, 31, 38, 255),
                )
                expanded.alpha_composite(image, (-left, -top))
                image = expanded
                slots = [
                    (x - left, y - top, width, height)
                    for x, y, width, height in slots
                ]

    template_scope = layout.get("template_scope")
    template_path_id = int(layout.get("template_path_id", 0) or 0)
    if not instantiated_layout and template_scope and template_path_id:
        template_entry = _scope_entry(template_scope, ("GameObject",), template_path_id)
        template_match = {
            "source": template_scope.get("source", ""),
            "bundle_entry": template_scope.get("bundle_entry", ""),
            "component_type": "GameObject",
            "component_path_id": template_path_id,
            "chain": [{
                "path_id": template_path_id,
                "name": _game_object_name(template_scope, template_path_id),
                "source_json": str(template_entry["path"]) if template_entry else "",
            }],
        }
        template_preview = _create_object_hierarchy_preview(template_match, 0, scopes)
        template_metadata = _safe_read_json(template_preview.with_suffix(".regions.json"))
        template_nodes = (
            template_metadata.get("tree_nodes", [])
            if isinstance(template_metadata, dict) else []
        )
        root_node = next(
            (node for node in template_nodes if int(node.get("path_id", 0) or 0) == template_path_id),
            None,
        )
        if isinstance(root_node, dict):
            root_x = float(root_node["x"])
            root_y = float(root_node["y"])
            root_width = max(1.0, float(root_node["width"]))
            root_height = max(1.0, float(root_node["height"]))
            with Image.open(template_preview) as template_source:
                card_image = template_source.convert("RGBA").crop((
                    round(root_x), round(root_y),
                    round(root_x + root_width),
                    round(root_y + root_height),
                ))
            dynamic_image_ids = _template_dynamic_image_object_ids(
                template_scope, template_path_id
            )
            dynamic_node = next(
                (
                    node for object_id in dynamic_image_ids
                    for node in template_nodes
                    if int(node.get("path_id", 0) or 0) == object_id
                ),
                None,
            )
            if isinstance(dynamic_node, dict) and card_image is not None:
                relative_x = float(dynamic_node.get("x", 0.0)) - root_x
                relative_y = float(dynamic_node.get("y", 0.0)) - root_y
                dynamic_width = max(1.0, float(dynamic_node.get("width", 1.0)))
                dynamic_height = max(1.0, float(dynamic_node.get("height", 1.0)))
                template_image_box = (
                    relative_x / root_width,
                    relative_y / root_height,
                    dynamic_width / root_width,
                    dynamic_height / root_height,
                )
                card_image.paste(
                    (0, 0, 0, 0),
                    (
                        max(0, math.floor(relative_x)),
                        max(0, math.floor(relative_y)),
                        min(card_image.width, math.ceil(relative_x + dynamic_width)),
                        min(card_image.height, math.ceil(relative_y + dynamic_height)),
                    ),
                )

    blocked_keys = _store_blocked_keys(config)
    draw = ImageDraw.Draw(image, "RGBA")
    label_font = _preview_font(14)

    def fit_label(value: str, max_width: float) -> str:
        if draw.textbbox((0, 0), value, font=label_font)[2] <= max_width:
            return value
        suffix = "…"
        while value and draw.textbbox((0, 0), value + suffix, font=label_font)[2] > max_width:
            value = value[:-1]
        return value + suffix

    for index, (product, (slot_x, slot_y, slot_width, slot_height)) in enumerate(
        zip(config["products"], slots), start=1
    ):
        if not instantiated_layout or overlay_product_images:
            target_size = (max(1, round(slot_width)), max(1, round(slot_height)))
            if not instantiated_layout and card_image is not None:
                card = card_image.resize(target_size, Image.Resampling.LANCZOS)
                image.alpha_composite(card, (round(slot_x), round(slot_y)))
            sprite = _store_product_preview_image(product)
            if sprite is not None:
                if template_image_box is not None and not instantiated_layout:
                    target_x = slot_x + template_image_box[0] * slot_width
                    target_y = slot_y + template_image_box[1] * slot_height
                    target_width = template_image_box[2] * slot_width
                    target_height = template_image_box[3] * slot_height
                else:
                    target_x = slot_x + slot_width * 0.11
                    target_y = slot_y + slot_height * 0.11
                    target_width = slot_width * 0.78
                    target_height = slot_height * 0.78
                sprite.thumbnail((
                    max(1, round(target_width)),
                    max(1, round(target_height)),
                ))
                px = round(target_x + (target_width - sprite.width) / 2)
                py = round(target_y + (target_height - sprite.height) / 2)
                image.alpha_composite(sprite, (px, py))
        blocked = product["pointer_key"] in blocked_keys
        color = (255, 60, 60, 255) if blocked else (35, 220, 105, 255)
        if instantiated_layout:
            # The real subtree already contains its own labels.  A full-width
            # synthetic caption obscures the UI and makes a faithful NGUI
            # layout look like overlapping cards, so only add a compact index.
            label = f"×{index}" if blocked else str(index)
            text_box = draw.textbbox((0, 0), label, font=label_font)
            badge_width = max(20, text_box[2] - text_box[0] + 10)
            badge_height = max(20, text_box[3] - text_box[1] + 7)
            draw.rounded_rectangle(
                (
                    slot_x + 2, slot_y + 2,
                    slot_x + 2 + badge_width, slot_y + 2 + badge_height,
                ),
                radius=4,
                fill=(color[0], color[1], color[2], 225),
            )
            draw.text(
                (slot_x + 7, slot_y + 4), label, font=label_font,
                fill=(255, 255, 255, 255),
            )
        else:
            label = (
                f"已屏蔽 {index}. {product['name']}"
                if blocked else f"{index}. {product['name']}"
            )
            label = fit_label(label, max(20.0, slot_width - 10.0))
            text_box = draw.textbbox((0, 0), label, font=label_font)
            text_height = max(18, text_box[3] - text_box[1] + 7)
            label_top = max(slot_y, slot_y + slot_height - text_height)
            draw.rectangle(
                (slot_x, label_top, slot_x + slot_width, slot_y + slot_height),
                fill=(color[0], color[1], color[2], 220),
            )
            draw.text(
                (slot_x + 5, label_top + 2), label, font=label_font,
                fill=(255, 255, 255, 255),
            )
        draw.rectangle(
            (slot_x, slot_y, slot_x + slot_width, slot_y + slot_height),
            outline=color, width=5 if blocked else 3,
        )
    return image, slots


def _open_store_layout_hierarchy(config: dict) -> bool:
    """Open the verified shop subtree in the existing Object blocking UI."""
    layout = config.get("layout")
    scopes = config.get("all_scopes")
    if not isinstance(layout, dict) or not isinstance(scopes, dict):
        print("[动态列表][界面层级] 当前列表没有可验证的真实界面层级。")
        return False
    runtime_shell = layout.get("runtime_shell")
    if isinstance(runtime_shell, dict):
        return _open_dynamic_view_hierarchy(runtime_shell, scopes)
    scope = layout.get("scope")
    root_path_id = int(layout.get("root_path_id", 0) or 0)
    component_object_id = int(layout.get("component_object_id", 0) or 0)
    if not isinstance(scope, dict) or not root_path_id:
        print("[动态列表][界面层级] 真实布局缺少可定位的根 Object。")
        return False
    chain = _object_chain_direct(scope, component_object_id or root_path_id, 128)
    root_level = next(
        (
            index for index, node in enumerate(chain)
            if int(node.get("path_id", 0) or 0) == root_path_id
        ),
        -1,
    )
    if root_level < 0:
        root_entry = _scope_entry(scope, ("GameObject",), root_path_id)
        chain = [{
            "path_id": root_path_id,
            "name": _game_object_name(scope, root_path_id),
            "source_json": str(root_entry["path"]) if root_entry else "",
        }]
        root_level = 0
    match = {
        "source": str(scope.get("source", "")),
        "bundle_entry": str(scope.get("bundle_entry", "")),
        "component_type": "MonoBehaviour",
        "component_path_id": int(layout.get("component_path_id", 0) or 0),
        "chain": chain,
        "force_active_path_ids": layout.get("force_active_path_ids", []),
        "preview_root_level": root_level,
    }
    print(
        f"[动态列表][界面层级] {config.get('name', '')}："
        "打开真实序列化 Object 子树；树和画面右键均可屏蔽对象。"
    )
    return _open_object_hierarchy_preview(match, root_level, scopes)


def _show_store_product_window(config: dict) -> bool:
    """Render the runtime product list as a spatial shop layout instead of a field table."""
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageTk

    window = tk.Tk()
    window.title(f"动态列表布局预览 - {config['name']}")
    window.geometry("1180x760")
    window.minsize(850, 560)
    window.configure(background="#12151b")
    changed = False
    selected_keys: set[str] = set()
    photos: list[object] = []
    hit_regions: list[tuple[float, float, float, float, dict]] = []
    layout_state = {"value": config.get("layout")}
    all_scopes = config.get("all_scopes")

    header = tk.Frame(window, background="#12151b")
    header.pack(fill="x", padx=12, pady=(10, 6))
    tk.Label(
        header,
        text=f"{config['name']}  ·  条目 {len(config['products'])} 个",
        background="#12151b", foreground="#43e081",
        font=("SimSun", 14, "bold"), anchor="w",
    ).pack(side="left")
    column_count = tk.IntVar(value=max(1, min(4, round(math.sqrt(len(config["products"])) or 1))))
    column_label = tk.Label(
        header, text="每行条目数:", background="#12151b", foreground="#d7dbe3",
        font=("SimSun", 10),
    )
    column_spin = tk.Spinbox(
        header, from_=1, to=max(1, len(config["products"])), width=4,
        textvariable=column_count, command=lambda: render_shop(),
    )
    if not layout_state["value"]:
        column_label.pack(side="left", padx=(30, 5))
        column_spin.pack(side="left")

    layout_note = tk.StringVar(
        value=(
            (
                f"真实实例布局：{layout_state['value']['root_name']}；"
                "条目位置与范围来自场景中已实例化的 Transform/NGUI Widget 子树。"
                if layout_state["value"].get("mode") == "instantiated"
                else
                f"真实商店模板布局：{layout_state['value']['root_name']}；"
                "完整界面来自序列化层级，商品卡片由 Content 与条目 Prefab 按原顺序补入。"
                if layout_state["value"].get("mode") == "runtime_template"
                else
                f"真实网格布局：{layout_state['value']['root_name']} / "
                f"{layout_state['value']['scope'].get('bundle_entry', '')}；"
                "条目位置来自引用组件的 Content 与 GridLayout 参数。"
            )
            if layout_state["value"] else
            f"回退布局：按 {config.get('array_label', '_products.Array')} 顺序重建；"
            "未找到可验证的动态列表视图引用链。"
        ),
    )
    tk.Label(
        window, textvariable=layout_note, justify="left", anchor="w",
        background="#12151b", foreground="#f0a04b", font=("SimSun", 10),
    ).pack(fill="x", padx=12, pady=(0, 7))

    canvas_frame = tk.Frame(window, background="#12151b")
    canvas_frame.pack(fill="both", expand=True, padx=12)
    canvas = tk.Canvas(
        canvas_frame, background="#20242c", highlightthickness=1,
        highlightbackground="#454b57",
    )
    vertical = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
    horizontal = ttk.Scrollbar(canvas_frame, orient="horizontal", command=canvas.xview)
    canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vertical.grid(row=0, column=1, sticky="ns")
    horizontal.grid(row=1, column=0, sticky="ew")
    canvas_frame.rowconfigure(0, weight=1)
    canvas_frame.columnconfigure(0, weight=1)

    def render_shop() -> None:
        nonlocal photos, hit_regions
        blocked_keys = _store_blocked_keys(config)
        layout = layout_state["value"]
        if layout and isinstance(all_scopes, dict):
            canvas.delete("all")
            photos = []
            hit_regions = []
            cache_key = tuple(sorted(blocked_keys))
            cache = config.get("_layout_render_cache")
            try:
                if not isinstance(cache, dict) or cache.get("key") != cache_key:
                    rendered, slots = _render_store_layout(config, layout, all_scopes)
                    cache = {"key": cache_key, "image": rendered, "slots": slots}
                    config["_layout_render_cache"] = cache
                rendered = cache["image"]
                slots = cache["slots"]
                photo = ImageTk.PhotoImage(rendered)
                photos.append(photo)
                canvas.create_image(0, 0, image=photo, anchor="nw")
                for index, (product, slot) in enumerate(zip(config["products"], slots)):
                    x1, y1, width, height = slot
                    x2, y2 = x1 + width, y1 + height
                    if product["pointer_key"] in selected_keys:
                        canvas.create_rectangle(
                            x1, y1, x2, y2, outline="#ff9f43", width=5,
                        )
                    hit_regions.append((x1, y1, x2, y2, product))
                canvas.configure(scrollregion=(0, 0, rendered.width, rendered.height))
                return
            except Exception as exc:
                layout_state["value"] = None
                config.pop("_layout_render_cache", None)
                layout_note.set(f"真实布局还原失败，已回退顺序画布：{exc}")
                column_label.pack(side="left", padx=(30, 5))
                column_spin.pack(side="left")
        try:
            columns = max(1, int(column_count.get()))
        except (TypeError, ValueError, tk.TclError):
            columns = 1
        columns = min(columns, max(1, len(config["products"])))
        canvas.delete("all")
        photos = []
        hit_regions = []
        card_width, card_height = 230, 285
        gap_x, gap_y, margin = 22, 24, 24
        for index, product in enumerate(config["products"]):
            row, column = divmod(index, columns)
            x1 = margin + column * (card_width + gap_x)
            y1 = margin + row * (card_height + gap_y)
            x2, y2 = x1 + card_width, y1 + card_height
            blocked = product["pointer_key"] in blocked_keys
            selected = product["pointer_key"] in selected_keys
            outline = "#ff9f43" if selected else ("#ff5c5c" if blocked else "#43e081")
            fill = "#352126" if blocked else "#171b22"
            canvas.create_rectangle(
                x1, y1, x2, y2, fill=fill, outline=outline,
                width=4 if selected else 2,
            )
            image = _store_product_preview_image(product)
            if image is not None:
                image.thumbnail((card_width - 28, card_height - 70))
                photo = ImageTk.PhotoImage(image)
                photos.append(photo)
                canvas.create_image(
                    (x1 + x2) / 2, y1 + 18 + (card_height - 70) / 2,
                    image=photo,
                )
            else:
                canvas.create_text(
                    (x1 + x2) / 2, y1 + 115, text="无可用条目图片",
                    fill="#a8adb8", font=("SimSun", 11),
                )
            label = f"{index + 1}. {product['name']}"
            if blocked:
                label += "  [已屏蔽]"
            canvas.create_text(
                (x1 + x2) / 2, y2 - 27, text=label,
                fill="#ff5c5c" if blocked else "#e8ebf0",
                font=("SimSun", 11, "bold"), width=card_width - 16,
            )
            hit_regions.append((x1, y1, x2, y2, product))
        rows = math.ceil(len(config["products"]) / columns)
        total_width = margin * 2 + columns * card_width + max(0, columns - 1) * gap_x
        total_height = margin * 2 + rows * card_height + max(0, rows - 1) * gap_y
        canvas.configure(scrollregion=(0, 0, total_width, total_height))

    def product_at(event) -> dict | None:
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        return next(
            (product for x1, y1, x2, y2, product in hit_regions
             if x1 <= x <= x2 and y1 <= y <= y2),
            None,
        )

    def on_click(event) -> None:
        product = product_at(event)
        if not product:
            return
        key = product["pointer_key"]
        if key in selected_keys:
            selected_keys.remove(key)
        else:
            selected_keys.add(key)
        render_shop()

    def set_selected(blocked: bool) -> None:
        nonlocal changed
        for product in config["products"]:
            if product["pointer_key"] in selected_keys:
                changed = _set_store_product_blocked(config, product, blocked) or changed
        render_shop()

    def open_object_hierarchy() -> None:
        window.withdraw()
        try:
            _open_store_layout_hierarchy(config)
        finally:
            try:
                window.deiconify()
                window.lift()
            except tk.TclError:
                pass

    context = tk.Menu(window, tearoff=False)
    context.add_command(label="屏蔽此条目", command=lambda: set_selected(True))

    def on_context(event) -> None:
        product = product_at(event)
        if not product:
            return
        selected_keys.clear()
        selected_keys.add(product["pointer_key"])
        render_shop()
        context.tk_popup(event.x_root, event.y_root)

    buttons = tk.Frame(window, background="#12151b")
    buttons.pack(fill="x", padx=12, pady=10)
    ttk.Button(buttons, text="屏蔽所选条目", command=lambda: set_selected(True)).pack(side="left")
    if layout_state["value"] and isinstance(all_scopes, dict):
        ttk.Button(
            buttons,
            text="打开真实商店 Object 层级",
            command=open_object_hierarchy,
        ).pack(side="left", padx=8)
    ttk.Button(buttons, text="清除选择", command=lambda: (selected_keys.clear(), render_shop())).pack(side="left", padx=8)
    ttk.Button(buttons, text="完成", command=window.destroy).pack(side="right")

    canvas.bind("<Button-1>", on_click)
    canvas.bind("<Button-3>", on_context)
    canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
    column_spin.bind("<Return>", lambda _event: render_shop())
    column_spin.bind("<FocusOut>", lambda _event: render_shop())
    render_shop()
    window.mainloop()
    return changed


def _run_store_product_terminal(config: dict) -> None:
    while True:
        blocked_keys = _store_blocked_keys(config)
        print()
        print(f"动态列表: {config['name']} (PathID={config['path_id']})")
        for number, product in enumerate(config["products"], start=1):
            state = "[已屏蔽]" if product["pointer_key"] in blocked_keys else "[显示]"
            print(
                f"  {number}. {state} {product['name']} | "
                f"图片={_store_product_image_label(product)} | "
                f"槽位={product['index']}"
            )
        raw = prompt_input(
            "输入编号/范围屏蔽（例: 2、1,3,5、2-6、1,3-5）；b 返回: "
        ).strip().lower()
        if raw in {"b", "q", "back", "quit"}:
            return
        try:
            numbers = parse_number_ranges(raw, set(range(1, len(config["products"]) + 1)))
        except ValueError as exc:
            print(f"[动态列表][错误] {exc}")
            continue
        for number in numbers:
            _set_store_product_blocked(config, config["products"][int(number) - 1], True)


def _find_runtime_store_shells(scopes: dict) -> list[dict]:
    """Find list UI controllers whose entries are populated only at runtime."""
    shells: list[dict] = []
    seen: set[tuple[str, int]] = set()
    template_hints = ("template", "prefab")
    runtime_context_hints = tuple(
        hint for hint in DYNAMIC_LIST_CONTEXT_HINTS if hint != "event"
    )
    store_hints = (
        *runtime_context_hints,
        "list", "entries", "spin", "lucky", "wheel",
    )
    commerce_hints = (
        "iap", "offer", "purchase", "trade", "merchant", "vendor",
        *(hint for hint in TASK_LIST_HINTS if hint != "event"),
    )
    candidates = [
        (scope_key, scope, path_id, entry)
        for scope_key, scope in scopes.items()
        for (type_name, path_id), entry in scope.get("items", {}).items()
        if type_name == "MonoBehaviour"
    ]
    for _scope_key, scope, path_id, entry in candidates:
        data = _entry_data(entry)
        if not isinstance(data, dict):
            continue
        raw_pointer_fields: list[tuple[str, int, int]] = []
        for field_name, pointer in data.items():
            folded = str(field_name).casefold()
            if folded in {"m_gameobject", "m_script"}:
                continue
            file_id, target_path_id = _pptr(pointer)
            if target_path_id:
                raw_pointer_fields.append((str(field_name), file_id, target_path_id))
        relevant_hints = (*template_hints, *store_hints, *commerce_hints)
        if not any(
            any(hint in field.casefold() for hint in relevant_hints)
            for field, _file_id, _target_path_id in raw_pointer_fields
        ):
            continue
        template_fields = [
            item for item in raw_pointer_fields
            if any(hint in item[0].casefold() for hint in template_hints)
        ]
        commerce_fields = [
            item for item in raw_pointer_fields
            if any(hint in item[0].casefold() for hint in commerce_hints)
        ]
        layout_fields = [
            item for item in raw_pointer_fields
            if any(hint in item[0].casefold() for hint in store_hints)
        ]
        _object_file_id, object_path_id = _pptr(data.get("m_GameObject"))
        object_name = _game_object_name(scope, object_path_id)
        name_is_store = any(hint in object_name.casefold() for hint in store_hints)
        strong_template_controller = len(template_fields) >= 2 and bool(commerce_fields)
        store_template = name_is_store and bool(
            template_fields or commerce_fields or layout_fields
        )
        if not (strong_template_controller or store_template):
            continue
        pointer_fields: list[dict] = []
        for field_name, file_id, target_path_id in raw_pointer_fields:
            target = _pointer_game_object(scope, file_id, target_path_id)
            target_scope, target_object_id = target if target else (None, 0)
            pointer_fields.append(
                {
                    "field": field_name,
                    "file_id": file_id,
                    "path_id": target_path_id,
                    "object_path_id": target_object_id,
                    "object_name": (
                        _game_object_name(target_scope, target_object_id)
                        if target_scope and target_object_id else ""
                    ),
                }
            )
        source_json = str(entry.get("path", ""))
        dedupe_key = (source_json.casefold(), int(path_id))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        shells.append(
                {
                    "name": object_name or f"<运行时列表 PathID={object_path_id}>",
                    "path_id": int(object_path_id),
                    "component_path_id": int(path_id),
                    "source": str(scope.get("source", "")),
                    "bundle_entry": str(scope.get("bundle_entry", "")),
                    "source_json": source_json,
                    "pointers": pointer_fields,
                }
        )
    shells.sort(
        key=lambda row: (
            str(row["source"]).casefold(),
            str(row["bundle_entry"]).casefold(),
            str(row["name"]).casefold(),
            int(row["component_path_id"]),
        )
    )
    return shells


def _print_runtime_store_shells(shells: list[dict]) -> None:
    print(
        f"\033[94m[动态列表][已识别运行时列表] 找到 {len(shells)} 个列表界面/模板控制器，"
        "但没有发现静态条目引用数组。\033[0m"
    )
    print(
        "\033[91m[动态列表][未找到数据表] 已找到列表界面，但未找到包含具体条目、"
        "显示顺序及资源引用的静态数据表。\033[0m"
    )
    for index, shell in enumerate(shells, start=1):
        print(
            f"  R{index}. {shell['name']} (GameObject PathID={shell['path_id']}, "
            f"组件 PathID={shell['component_path_id']}) | "
            f"{shell['source']} / {shell['bundle_entry'] or '<无 Bundle entry>'}"
        )
        for pointer in shell.get("pointers", []):
            target = pointer.get("object_name") or f"PathID={pointer.get('path_id', 0)}"
            print(f"     {pointer.get('field', '')} -> {target}")
    print(
        "\033[94m[动态列表][运行时数据] 这些界面只保存模板或容器；"
        "条目 ID、文本、进度、价格或图片由代码、联网响应或本地运行时缓存注入。\033[0m"
    )
    print(
        "\033[94m[动态列表][限制] 当前没有可安全删除并保持顺序的序列化条目，"
        "因此不能逐商品删除；仍可用 R编号进入真实 Object 层级，屏蔽整个界面层级。\033[0m"
    )


def _print_additional_runtime_list_shells(shells: list[dict]) -> None:
    if not shells:
        return
    print(
        f"[动态列表] 另识别到 {len(shells)} 个真实界面层级入口；"
        "R编号用于 Object 层级屏蔽，逐条数据屏蔽请选上方列表编号:"
    )
    for index, shell in enumerate(shells, start=1):
        pointer_fields = ", ".join(
            str(pointer.get("field", ""))
            for pointer in shell.get("pointers", [])[:4]
            if pointer.get("field")
        )
        print(
            f"  R{index}. {shell['name']} (组件 PathID={shell['component_path_id']}) | "
            f"{shell['source']}"
            + (f" | 模板/入口={pointer_fields}" if pointer_fields else "")
        )


def _dedupe_runtime_shells(shells: list[dict], configs: list[dict]) -> list[dict]:
    config_components = {
        (
            str(config.get("source", "")).casefold(),
            str(config.get("bundle_entry", "")).casefold(),
            int(config.get("path_id", 0) or 0),
        )
        for config in configs
    }
    return [
        shell for shell in shells
        if (
            str(shell.get("source", "")).casefold(),
            str(shell.get("bundle_entry", "")).casefold(),
            int(shell.get("component_path_id", 0) or 0),
        ) not in config_components
    ]


def _runtime_preview_node(metadata: dict, path_id: int = 0, name: str = "") -> dict | None:
    folded_name = name.casefold()
    return next(
        (
            node for node in metadata.get("tree_nodes", [])
            if isinstance(node, dict)
            and (
                (path_id and int(node.get("path_id", 0) or 0) == path_id)
                or (folded_name and str(node.get("name", "")).casefold() == folded_name)
            )
        ),
        None,
    )


def _runtime_quick_game_object_name(entry: dict, path_id: int) -> str:
    item = entry.get("item")
    if isinstance(item, dict):
        value = _manifest_value(item, "AssetName", "assetName", default="")
        if isinstance(value, str) and value.strip():
            return value.strip()
    entry_path = entry.get("path")
    if entry_path:
        return re.sub(
            rf"_{re.escape(str(path_id))}$", "", Path(str(entry_path)).stem
        )
    return ""


def _runtime_named_root_candidate(
    scopes: dict,
    wanted_name: str,
) -> tuple[dict, int] | None:
    candidates: list[tuple[int, dict, int]] = []
    folded = wanted_name.casefold()
    for candidate_scope in scopes.values():
        for (type_name, path_id), candidate_entry in candidate_scope.get("items", {}).items():
            if type_name != "GameObject":
                continue
            if _runtime_quick_game_object_name(candidate_entry, path_id).casefold() != folded:
                continue
            chain = _object_chain_direct(candidate_scope, path_id, 4)
            if not chain or int(chain[-1]["path_id"]) != int(path_id):
                continue
            subtree_size = len(
                _game_object_subtree_path_ids(candidate_scope, int(path_id))
            )
            candidates.append((subtree_size, candidate_scope, int(path_id)))
    if not candidates:
        return None
    _size, scope, path_id = max(candidates, key=lambda row: row[0])
    return scope, path_id


def _runtime_preview_crop(image, node: dict, transparent: bool = True):
    from PIL import Image

    left = max(0, math.floor(float(node.get("x", 0.0))))
    top = max(0, math.floor(float(node.get("y", 0.0))))
    right = min(
        image.width,
        math.ceil(left + max(1.0, float(node.get("width", 1.0)))),
    )
    bottom = min(
        image.height,
        math.ceil(top + max(1.0, float(node.get("height", 1.0)))),
    )
    cropped = image.crop((left, top, right, bottom)).convert("RGBA")
    if transparent:
        pixels = []
        for red, green, blue, alpha in cropped.getdata():
            if (red, green, blue) == (28, 31, 38):
                pixels.append((red, green, blue, 0))
            else:
                pixels.append((red, green, blue, alpha))
        cropped.putdata(pixels)
    return cropped


def _render_runtime_root_asset(
    scope: dict,
    root_path_id: int,
    scopes: dict,
    label: str,
    force_all: bool = True,
    hidden_path_ids: tuple[int, ...] | list[int] = (),
):
    from PIL import Image

    root_entry = _scope_entry(scope, ("GameObject",), root_path_id)
    if root_entry is None:
        raise ValueError(f"运行时布局根 GameObject 不存在: {root_path_id}")
    match = {
        "source": scope.get("source", ""),
        "bundle_entry": scope.get("bundle_entry", ""),
        "component_type": "GameObject",
        "component_path_id": root_path_id,
        "chain": [{
            "path_id": root_path_id,
            "name": label,
            "source_json": str(root_entry.get("path", "")),
        }],
        "force_active_path_ids": (
            _game_object_subtree_path_ids(scope, root_path_id)
            if force_all else [root_path_id]
        ),
        "hidden_path_ids": list(hidden_path_ids),
    }
    target = _create_object_hierarchy_preview(match, 0, scopes)
    with Image.open(target) as opened:
        image = opened.convert("RGBA")
    metadata = _safe_read_json(target.with_suffix(".regions.json"))
    if not isinstance(metadata, dict):
        raise ValueError(f"运行时布局预览元数据无效: {target}")
    return image, metadata


def _runtime_preview_visual_bounds(metadata: dict, root_path_id: int) -> tuple[int, int, int, int]:
    nodes = [
        node for node in metadata.get("tree_nodes", [])
        if isinstance(node, dict)
        and int(node.get("path_id", 0) or 0) != int(root_path_id)
        and float(node.get("width", 0.0) or 0.0) > 2.0
        and float(node.get("height", 0.0) or 0.0) > 2.0
    ]
    if not nodes:
        root = _runtime_preview_node(metadata, root_path_id)
        if root is None:
            raise ValueError("运行时布局没有可裁剪的可视节点")
        nodes = [root]
    padding = 8
    return (
        max(0, math.floor(min(float(node["x"]) for node in nodes) - padding)),
        max(0, math.floor(min(float(node["y"]) for node in nodes) - padding)),
        math.ceil(max(float(node["x"]) + float(node["width"]) for node in nodes) + padding),
        math.ceil(max(float(node["y"]) + float(node["height"]) for node in nodes) + padding),
    )


def _runtime_preview_subtree_bounds(
    metadata: dict,
    root_path_id: int,
) -> tuple[int, int, int, int] | None:
    node_by_id = {
        int(node.get("path_id", 0) or 0): node
        for node in metadata.get("tree_nodes", [])
        if isinstance(node, dict) and int(node.get("path_id", 0) or 0)
    }
    pending = [int(root_path_id)]
    seen: set[int] = set()
    visual_nodes: list[dict] = []
    root_visual: dict | None = None
    while pending:
        path_id = pending.pop()
        if path_id in seen:
            continue
        seen.add(path_id)
        node = node_by_id.get(path_id)
        if not node:
            continue
        if (
            float(node.get("width", 0.0) or 0.0) > 2
            and float(node.get("height", 0.0) or 0.0) > 2
        ):
            if path_id == int(root_path_id):
                root_visual = node
            else:
                visual_nodes.append(node)
        pending.extend(
            int(value) for value in node.get("children", [])
            if str(value).lstrip("-").isdigit()
        )
    if not visual_nodes and root_visual is not None:
        visual_nodes = [root_visual]
    if not visual_nodes:
        return None
    return (
        math.floor(min(float(node["x"]) for node in visual_nodes)),
        math.floor(min(float(node["y"]) for node in visual_nodes)),
        math.ceil(max(
            float(node["x"]) + float(node["width"]) for node in visual_nodes
        )),
        math.ceil(max(
            float(node["y"]) + float(node["height"]) for node in visual_nodes
        )),
    )


def _paste_runtime_prefab_repeated(
    canvas,
    prefab_image,
    target_node: dict,
    count: int = 3,
) -> None:
    from PIL import Image

    target_x = float(target_node.get("x", 0.0))
    target_y = float(target_node.get("y", 0.0))
    target_width = max(1.0, float(target_node.get("width", 1.0)))
    target_height = max(1.0, float(target_node.get("height", 1.0)))
    scale = min(
        target_height * 0.94 / max(1, prefab_image.height),
        target_width * 0.96 / max(1, prefab_image.width * count),
    )
    width = max(1, round(prefab_image.width * scale))
    height = max(1, round(prefab_image.height * scale))
    prefab = prefab_image.resize((width, height), Image.Resampling.LANCZOS)
    gap = max(2, round((target_width - width * count) / max(1, count - 1)))
    total_width = width * count + gap * max(0, count - 1)
    left = round(target_x + (target_width - total_width) / 2)
    top = round(target_y + (target_height - height) / 2)
    for index in range(count):
        canvas.alpha_composite(prefab, (left + index * (width + gap), top))


def _render_runtime_list_composite(
    shell: dict,
    scope: dict,
    root_path_id: int,
    scopes: dict,
) -> Path:
    """Compose referenced runtime prefabs back into their serialized containers."""
    from PIL import Image, ImageDraw

    shell_name = str(shell.get("name", "")).casefold()
    hidden_base_nodes: list[int] = []
    if "task" in shell_name:
        root_transform = _find_game_object_transform(scope, root_path_id)
        for child_pointer in _array_value(
            root_transform.get("m_Children") if isinstance(root_transform, dict) else None
        ):
            file_id, child_transform_id = _pptr(child_pointer)
            child_transform_entry = (
                _scope_entry(scope, ("Transform", "RectTransform"), child_transform_id)
                if file_id == 0 else None
            )
            child_transform = _entry_data(child_transform_entry) if child_transform_entry else None
            child_file_id, child_object_id = _pptr(
                child_transform.get("m_GameObject")
                if isinstance(child_transform, dict) else None
            )
            if child_file_id != 0 or not child_object_id:
                continue
            child_entry = _scope_entry(scope, ("GameObject",), child_object_id)
            child_data = _entry_data(child_entry) if child_entry else None
            child_name = str(
                child_data.get("m_Name", "") if isinstance(child_data, dict) else ""
            ).casefold()
            if "background" not in child_name and "shade" not in child_name:
                continue
            for component_path_id in _game_object_component_path_ids(
                child_data if isinstance(child_data, dict) else {}
            ):
                component_entry = _scope_entry(
                    scope, ("MonoBehaviour",), component_path_id
                )
                component_data = _entry_data(component_entry) if component_entry else None
                color = component_data.get("mColor") if isinstance(component_data, dict) else None
                if (
                    isinstance(color, dict)
                    and _number(color.get("a"), 1.0) < 0.95
                    and max(_number(color.get(key), 1.0) for key in ("r", "g", "b")) < 0.08
                ):
                    hidden_base_nodes.append(child_object_id)
                    break
    base, metadata = _render_runtime_root_asset(
        scope,
        root_path_id,
        scopes,
        str(shell.get("name", "RuntimeList")),
        force_all="task" not in shell_name,
        hidden_path_ids=hidden_base_nodes,
    )
    component_entry = _scope_entry(
        scope, ("MonoBehaviour",), int(shell.get("component_path_id", 0) or 0)
    )
    component_data = _entry_data(component_entry) if component_entry else None
    component_data = component_data if isinstance(component_data, dict) else {}

    if "task" in shell_name:
        # Runtime activates several horizontally arranged widgets.  The prefab
        # stores their cards and coordinates, while the uncovered canvas uses
        # the same panel blue as the serialized center widget.
        sample_x = min(base.width - 1, max(0, round(base.width * 0.55)))
        sample_y = min(base.height - 1, max(0, round(base.height * 0.46)))
        panel_color = base.getpixel((sample_x, sample_y))
        if sum(panel_color[:3]) > 50:
            base.putdata([
                panel_color if pixel[:3] == (28, 31, 38) else pixel
                for pixel in base.getdata()
            ])
        template_to_container = (
            ("_mainTaskItem", "_widgetTasks"),
            ("_dailyTaskItem", "_widgetDaily"),
            ("_marathonTaskItem", "_widgetMarathon"),
        )
        for template_field, container_field in template_to_container:
            template_file_id, template_path_id = _pptr(component_data.get(template_field))
            template_scope = _resolve_pointer_scope(scope, template_file_id)
            template_target = _pointer_game_object(
                scope, template_file_id, template_path_id
            )
            _container_file_id, container_path_id = _pptr(
                component_data.get(container_field)
            )
            container_target = _pointer_game_object(
                scope, _container_file_id, container_path_id
            )
            if not template_scope or not template_target or not container_target:
                continue
            container_node = _runtime_preview_node(metadata, container_target[1])
            if not container_node:
                continue
            prefab_canvas, prefab_metadata = _render_runtime_root_asset(
                template_target[0], template_target[1], scopes,
                _game_object_name(template_target[0], template_target[1]),
            )
            prefab_root = _runtime_preview_node(prefab_metadata, template_target[1])
            if not prefab_root:
                continue
            prefab = _runtime_preview_crop(prefab_canvas, prefab_root, True)
            _paste_runtime_prefab_repeated(base, prefab, container_node, 3)

    if "bank" in shell_name:
        coins_target = _runtime_named_root_candidate(scopes, "Coins")
        table_target = _pointer_game_object(
            scope, *_pptr(component_data.get("_tableGoods"))
        )
        table_node = (
            _runtime_preview_node(metadata, table_target[1])
            if table_target else None
        )
        viewport_node = next(
            (
                node for node in metadata.get("tree_nodes", [])
                if isinstance(node, dict)
                and "scroller" in str(node.get("name", "")).casefold()
                and float(node.get("width", 0.0) or 0.0) > 2
            ),
            None,
        )
        if coins_target and table_node and viewport_node:
            coins_canvas, coins_metadata = _render_runtime_root_asset(
                coins_target[0], coins_target[1], scopes, "Coins_Runtime"
            )
            coins_root = _runtime_preview_node(coins_metadata, coins_target[1])
            if coins_root:
                coins = _runtime_preview_crop(coins_canvas, coins_root, True)
                scale = min(
                    float(viewport_node["width"]) * 0.98 / coins.width,
                    float(viewport_node["height"]) * 0.92 / coins.height,
                )
                coins = coins.resize(
                    (max(1, round(coins.width * scale)), max(1, round(coins.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                center_x = float(table_node["x"]) + float(table_node["width"]) / 2
                center_y = float(table_node["y"]) + float(table_node["height"]) / 2
                base.alpha_composite(
                    coins,
                    (
                        round(center_x - coins.width / 2),
                        round(center_y - coins.height / 2),
                    ),
                )

        currency_target = _runtime_named_root_candidate(scopes, "PanelUserCurrency")
        frame_node = _runtime_preview_node(metadata, name="Sprite_frame")
        if currency_target and frame_node:
            currency_canvas, currency_metadata = _render_runtime_root_asset(
                currency_target[0], currency_target[1], scopes, "PanelUserCurrency_Runtime"
            )
            currency_bounds = _runtime_preview_subtree_bounds(
                currency_metadata, currency_target[1]
            )
            if currency_bounds:
                currency = currency_canvas.crop(currency_bounds).convert("RGBA")
                currency.putdata([
                    (red, green, blue, 0)
                    if (red, green, blue) == (28, 31, 38)
                    else (red, green, blue, alpha)
                    for red, green, blue, alpha in currency.getdata()
                ])
                target_width = max(1, round(float(frame_node["width"]) * 0.30))
                scale = target_width / max(1, currency.width)
                currency = currency.resize(
                    (target_width, max(1, round(currency.height * scale))),
                    Image.Resampling.LANCZOS,
                )
                base.alpha_composite(
                    currency,
                    (
                        round(float(frame_node["x"]) + float(frame_node["width"]) - currency.width - 6),
                        round(float(frame_node["y"]) + 3),
                    ),
                )

        tabs_target = _pointer_game_object(
            scope, *_pptr(component_data.get("_tableTabs"))
        )
        template_target = _pointer_game_object(
            scope, *_pptr(component_data.get("_tabTemplate"))
        )
        tabs_node = (
            _runtime_preview_node(metadata, tabs_target[1])
            if tabs_target else None
        )
        template_bounds = (
            _runtime_preview_subtree_bounds(metadata, template_target[1])
            if template_target else None
        )
        if tabs_node and template_bounds:
            tab_template = base.crop(template_bounds).convert("RGBA")
            tab_width = float(tabs_node["width"]) / 6.0
            tab_height = float(tabs_node["height"])
            draw = ImageDraw.Draw(base, "RGBA")
            for index in range(6):
                left = float(tabs_node["x"]) + index * tab_width
                top = float(tabs_node["y"])
                draw.rectangle(
                    (left, top, left + tab_width, top + tab_height),
                    fill=(12, 36, 61, 235),
                    outline=(3, 18, 31, 255),
                    width=1,
                )
                icon = tab_template.copy()
                icon.thumbnail(
                    (max(1, round(tab_width * 0.72)), max(1, round(tab_height * 0.82))),
                    Image.Resampling.LANCZOS,
                )
                base.alpha_composite(
                    icon,
                    (
                        round(left + (tab_width - icon.width) / 2),
                        round(top + (tab_height - icon.height) / 2),
                    ),
                )

    crop_box = _runtime_preview_visual_bounds(metadata, root_path_id)
    result = base.crop(crop_box)
    if result.width < 1000:
        scale = 1140 / max(1, result.width)
        result = result.resize(
            (1140, max(1, round(result.height * scale))),
            Image.Resampling.LANCZOS,
        )
    safe_name = re.sub(r'[^0-9A-Za-z._\-\u4e00-\u9fff]+', "_", str(shell.get("name", "RuntimeList")))
    target = DEFAULT_OBJECT_PREVIEW_ROOT / f"dynamic_runtime_{safe_name}_{root_path_id}.png"
    result.convert("RGB").save(target, "PNG", quality=95)
    return target


def _preview_runtime_list_shell(shell: dict, scopes: dict) -> Path:
    """Render the serialized prefab shell without pretending runtime rows exist."""
    scope = next(
        (
            value for value in scopes.values()
            if str(value.get("source", "")) == str(shell.get("source", ""))
            and str(value.get("bundle_entry", ""))
            == str(shell.get("bundle_entry", ""))
        ),
        None,
    )
    if scope is None:
        raise ValueError("无法定位运行时列表模板所在的资源范围")
    component_object_id = int(shell.get("path_id", 0) or 0)
    chain = _object_chain_direct(scope, component_object_id, 128)
    if not chain:
        raise ValueError("运行时列表模板没有可用的 GameObject 层级")
    # The component may sit below the prefab root.  Start at the highest
    # serialized object in that prefab, then follow the ordinary Transform
    # children downward.  This is the same rule for shops, tasks and any other
    # dynamically populated UI.
    root_path_id = int(chain[-1]["path_id"])
    root_entry = _scope_entry(scope, ("GameObject",), root_path_id)
    if root_entry is None:
        raise ValueError("运行时列表模板的根 GameObject 不存在")
    cached_path = Path(str(shell.get("_runtime_preview_path", "")))
    if (
        int(shell.get("_runtime_preview_version", 0) or 0)
        == RUNTIME_LAYOUT_RENDER_VERSION
        and cached_path.is_file()
    ):
        target = cached_path
        print(f"[动态列表][模板预览] 已复用合成预览: {target}")
    else:
        target = _render_runtime_list_composite(shell, scope, root_path_id, scopes)
        shell["_runtime_preview_version"] = RUNTIME_LAYOUT_RENDER_VERSION
        shell["_runtime_preview_path"] = str(target)
        _write_object_graph_cache()
    print(f"[动态列表][模板预览] {shell['name']} -> {target}")
    print("[动态列表][模板预览] 仅还原序列化面板/槽位；运行时商品或任务数据不会虚构补入。")
    try:
        os.startfile(target)  # type: ignore[attr-defined]
    except OSError as exc:
        print(f"[动态列表][模板预览][提示] 无法自动打开图片: {exc}")
    return target


def _runtime_shell_scope(shell: dict, scopes: dict) -> dict | None:
    return next(
        (
            scope for scope in scopes.values()
            if str(scope.get("source", "")) == str(shell.get("source", ""))
            and str(scope.get("bundle_entry", ""))
            == str(shell.get("bundle_entry", ""))
        ),
        None,
    )


def _dynamic_view_hierarchy_match(shell: dict, scopes: dict) -> tuple[dict, int] | None:
    """Build a real serialized hierarchy match for the existing Object UI."""
    scope = _runtime_shell_scope(shell, scopes)
    if scope is None:
        return None
    anchor_object_id = int(shell.get("path_id", 0) or 0)
    chain = _object_chain_direct(scope, anchor_object_id, 128)
    if not chain:
        return None
    root_index, _root_node, root_score = _store_layout_root(chain)
    if not root_score:
        # A controller name can be opaque while its immediate UI parent still
        # owns the useful subtree.  Avoid jumping to the global Canvas, which
        # can exceed the 2,000-node preview cap.
        root_index = min(len(chain) - 1, 2)
    root_path_id = int(chain[root_index].get("path_id", 0) or 0)
    match = {
        "source": str(scope.get("source", "")),
        "bundle_entry": str(scope.get("bundle_entry", "")),
        "component_type": "MonoBehaviour",
        "component_path_id": int(shell.get("component_path_id", 0) or 0),
        "chain": chain,
        "force_active_path_ids": _game_object_subtree_path_ids(scope, root_path_id),
        "preview_root_level": root_index,
    }
    return match, root_index


def _open_dynamic_view_hierarchy(shell: dict, scopes: dict) -> bool:
    prepared = _dynamic_view_hierarchy_match(shell, scopes)
    if prepared is None:
        print("[动态列表][界面层级][错误] 无法定位控制器的 GameObject 父子层级。")
        return False
    match, root_index = prepared
    print(
        f"[动态列表][界面层级] {shell.get('name', '')}："
        "使用真实序列化 Object 层级；可在树或画面上右键屏蔽。"
    )
    print(
        "[动态列表][界面层级] 为便于检查，预览会强制显示该子树中的静态禁用节点；"
        "这不代表它们运行时会同时出现。"
    )
    return _open_object_hierarchy_preview(match, root_index, scopes)


def _game_object_subtree_path_ids(
    scope: dict,
    root_path_id: int,
    max_objects: int = 4000,
) -> list[int]:
    """Collect a prefab/scene subtree strictly through Transform children."""
    pending = [int(root_path_id)]
    result: list[int] = []
    seen: set[int] = set()
    while pending and len(seen) < max_objects:
        object_path_id = pending.pop()
        if object_path_id in seen:
            continue
        seen.add(object_path_id)
        result.append(object_path_id)
        transform = _find_game_object_transform(scope, object_path_id)
        if not isinstance(transform, dict):
            continue
        children: list[int] = []
        for pointer in _array_value(transform.get("m_Children")):
            file_id, transform_path_id = _pptr(pointer)
            if file_id != 0 or not transform_path_id:
                continue
            child_entry = _scope_entry(
                scope, ("Transform", "RectTransform"), transform_path_id
            )
            child_transform = _entry_data(child_entry) if child_entry else None
            child_file_id, child_object_id = _pptr(
                child_transform.get("m_GameObject")
                if isinstance(child_transform, dict) else None
            )
            if child_file_id == 0 and child_object_id:
                children.append(child_object_id)
        pending.extend(reversed(children))
    return result


def run_block_dynamic_store_products() -> None:
    print()
    print("动态列表索引与选择性屏蔽（测试阶段）")
    print(
        "说明: 静态分析商店、任务、成就、活动等数据数组与界面绑定；"
        "屏蔽时只移除所选条目引用。"
    )
    scopes, _textures = _load_object_graph()
    store_cache = _object_graph_cache_bucket("dynamic_store_scan")
    ai_cache_signature = _dynamic_list_ai_cache_signature()
    if (
        store_cache.get("version") == DYNAMIC_STORE_SCAN_VERSION
        and store_cache.get("ai_cache_signature") == ai_cache_signature
        and bool(store_cache.get("ai_review_completed"))
        and "configs" in store_cache
        and isinstance(store_cache.get("configs"), list)
    ):
        configs = store_cache["configs"]
        runtime_shells = store_cache.get("runtime_shells", [])
        print(f"[动态列表] 已复用扫描缓存：静态列表={len(configs)}")
    else:
        store_cache.clear()
        rejected_candidates: list[dict] = []
        configs = _find_store_product_configs(
            scopes,
            rejected_candidates=rejected_candidates,
        )
        ai_accepted_ids = _request_dynamic_list_ai_review(rejected_candidates)
        if ai_accepted_ids:
            configs = _find_store_product_configs(
                scopes,
                ai_accepted_ids=ai_accepted_ids,
            )
        runtime_shells = _find_runtime_store_shells(scopes)
        runtime_shells = _dedupe_runtime_shells(runtime_shells, configs)
        store_cache["version"] = DYNAMIC_STORE_SCAN_VERSION
        store_cache["ai_cache_signature"] = ai_cache_signature
        store_cache["ai_review_completed"] = (
            _dynamic_list_ai_review_cache_matches(rejected_candidates)
        )
        store_cache["configs"] = configs
        store_cache["runtime_shells"] = runtime_shells
        _write_object_graph_cache()
        print("[动态列表] 扫描结果已加入共享缓存。")
    if not configs:
        if runtime_shells:
            _print_runtime_store_shells(runtime_shells)
        else:
            print("[动态列表][未找到] 没有发现静态数据数组或运行时列表模板控制器。")
            return
    if configs:
        records = _load_store_product_records()
        print(f"[动态列表] 找到 {len(configs)} 个可安全修改的数据列表:")
        for number, config in enumerate(configs, start=1):
            blocked_count = len(_store_blocked_keys(config, records))
            classification = config.get("classification", {})
            print(
                f"  {number}. {config['name']} (PathID={config['path_id']}) | "
                f"类型={classification.get('kind', '动态列表')}，"
                f"{config.get('array_label', '')}，条目={len(config['products'])}，"
                f"已屏蔽={blocked_count}，识别={classification.get('confidence', '兼容')}"
                f"/{classification.get('score', '-')}分 | "
                f"{config['source']} / {config['bundle_entry'] or '<无 Bundle entry>'}"
            )
    if configs:
        _print_additional_runtime_list_shells(runtime_shells)
    raw = prompt_input(
        "请选择数据列表编号/范围（例: 2、1,3-5）；"
        "输入 R编号打开真实界面层级（例: R8），或 b 返回: "
    ).strip().lower()
    if raw in {"b", "q", "back", "quit"}:
        return
    runtime_match = re.fullmatch(r"r\s*(\d+)", raw, flags=re.IGNORECASE)
    if runtime_match:
        runtime_index = int(runtime_match.group(1))
        if runtime_index < 1 or runtime_index > len(runtime_shells):
            print(f"[动态列表][错误] 运行时模板编号必须在 1-{len(runtime_shells)} 之间。")
            return
        try:
            _open_dynamic_view_hierarchy(runtime_shells[runtime_index - 1], scopes)
        except (ImportError, OSError, ValueError) as exc:
            print(f"[动态列表][界面层级][错误] {exc}")
        return
    try:
        selected = parse_number_ranges(raw, set(range(1, len(configs) + 1)))
    except ValueError as exc:
        print(f"[动态列表][错误] {exc}")
        return
    selected_configs = [configs[int(number) - 1] for number in selected]
    unresolved_layout_configs = [
        config for config in selected_configs
        if not bool(config.get("_layout_cache_ready"))
    ]
    pre_resolved_layouts: dict[str, dict] = {}
    reference_layout_configs: list[dict] = []
    for config in unresolved_layout_configs:
        fast_layout = _find_store_layout(
            config,
            scopes,
            [],
            runtime_shells,
        )
        if fast_layout is not None and int(fast_layout.get("score", 0) or 0) >= 260:
            pre_resolved_layouts[str(config.get("config_key", ""))] = fast_layout
        else:
            reference_layout_configs.append(config)
    layout_candidates = (
        _prefilter_reference_entries_across_scopes(
            scopes,
            {"MonoBehaviour"},
            {
                int(config.get("path_id", 0) or 0)
                for config in reference_layout_configs
            },
        )
        if reference_layout_configs
        else []
    )
    for config in selected_configs:
        if bool(config.get("_layout_cache_ready")):
            print(f"[动态列表] 已复用布局链路缓存: {config['name']}")
        else:
            config["layout"] = (
                pre_resolved_layouts.get(str(config.get("config_key", "")))
                or _find_store_layout(
                    config,
                    scopes,
                    layout_candidates,
                    runtime_shells,
                )
            )
            config["_layout_cache_ready"] = True
            _write_object_graph_cache()
        config["all_scopes"] = scopes
        if config["layout"]:
            layout = config["layout"]
            mode_label = (
                "已实例化NGUI/Transform"
                if layout.get("mode") == "instantiated"
                else "运行时Prefab+Content"
                if layout.get("mode") == "runtime_template"
                else "GridLayout"
            )
            print(
                f"[动态列表][真实布局] {config['name']} -> "
                f"{layout['root_name']} / {layout['scope'].get('bundle_entry', '')}，"
                f"模式={mode_label}，"
                f"Content PathID={layout.get('content_path_id', 0)}，"
                f"Template PathID={layout.get('template_path_id', 0)}"
            )
        else:
            print(f"[动态列表][布局回退] {config['name']} 未找到可验证的列表视图引用链。")
        try:
            try:
                _show_store_product_window(config)
            except Exception as exc:
                print(f"[动态列表][提示] 图形窗口不可用，改用终端选择: {exc}")
                _run_store_product_terminal(config)
        finally:
            config.pop("_layout_render_cache", None)
            config.pop("all_scopes", None)


def run_block_objects_by_image() -> None:
    print()
    print("按图片定位并屏蔽 GameObject")
    print(
        "说明: 图片可批量放入 AllPNG/屏蔽object，也可输入 AllPNG/PNG 或 "
        "AllPNG/Sprite/PNG 中的图片名称/路径。"
    )
    print(
        "      脚本会按 Texture2D 直连、Sprite PathID 或 NGUI "
        "Atlas+SpriteName 精确反查对象；图集子图须先通过主菜单 2 拆分。"
    )
    print("      每个匹配项按 0-8 显示对象父链，可选择屏蔽链上的任意对象。")
    if not DEFAULT_BLOCK_IMAGE_ROOT.exists():
        print(f"[屏蔽对象][错误] 批量图片目录不存在: {DEFAULT_BLOCK_IMAGE_ROOT}")
        print("[屏蔽对象][提示] 请先执行工具脚本主菜单 1；导出 AllPNG 时会自动创建该目录。")
        return
    if not DEFAULT_BLOCK_IMAGE_ROOT.is_dir():
        print(f"[屏蔽对象][错误] 批量图片路径不是目录: {DEFAULT_BLOCK_IMAGE_ROOT}")
        return
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
            ngui_sprite_targets: set[tuple[tuple[str, str], int, str]] = set()
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
            elif item.get("_selection_kind") == "ngui_sprite":
                references = item.get("atlas_references")
                if not isinstance(references, list) or not references:
                    references = [item]
                sprite_name = str(item.get("sprite_name", "")).strip().casefold()
                for reference in references:
                    if not isinstance(reference, dict):
                        continue
                    source_resource = str(reference.get("source_resource", ""))
                    bundle_entry = str(reference.get("bundle_entry", ""))
                    atlas_path_id = int(reference.get("atlas_path_id", 0) or 0)
                    scope_key = next(
                        (
                            key
                            for key, scope in scopes.items()
                            if scope["source"] == source_resource
                            and scope["bundle_entry"] == bundle_entry
                        ),
                        None,
                    )
                    if scope_key is not None and atlas_path_id and sprite_name:
                        ngui_sprite_targets.add((scope_key, atlas_path_id, sprite_name))
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
                    ngui_sprite_targets,
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
        skip_current_image = False
        while True:
            if len(matches) == 1:
                selected_matches = [1]
            else:
                print()
                while True:
                    raw = prompt_input(
                        f"选择当前图片的匹配项编号 [1-{len(matches)}]；"
                        "支持 1-2 或 1,2；q 跳过，x 结束: "
                    ).strip().lower()
                    if raw == "x":
                        return
                    if raw == "q":
                        skip_current_image = True
                        break
                    try:
                        selected_matches = [
                            int(value)
                            for value in parse_number_ranges(
                                raw,
                                set(range(1, len(matches) + 1)),
                            )
                        ]
                    except ValueError as exc:
                        print(f"[屏蔽对象][错误] {exc}，请重新选择当前图片的匹配项。")
                        continue
                    break
                if skip_current_image:
                    break

            return_to_match_selection = False
            for match_index in selected_matches:
                match = matches[match_index - 1]
                if len(matches) > 1:
                    _print_object_match(match_index, match)
                while True:
                    level_raw = prompt_input(
                        "选择要屏蔽的层级编号；0 为当前对象，p 从最高层预览、p3 从层级 3 预览，"
                        "b 返回匹配项选择，"
                        "q 跳过此匹配项，x 结束: "
                    ).strip().lower()
                    if level_raw.startswith("p"):
                        preview_level_raw = level_raw[1:].strip()
                        try:
                            preview_level = (
                                int(preview_level_raw)
                                if preview_level_raw
                                else len(match["chain"]) - 1
                            )
                        except ValueError:
                            print("[层级预览][错误] 请输入 p 或 p 加层级数字，例如 p3。")
                            continue
                        if preview_level < 0 or preview_level >= len(match["chain"]):
                            print(
                                f"[层级预览][错误] 层级编号无效，请输入 0-"
                                f"{len(match['chain']) - 1}。"
                            )
                            continue
                        if scopes is None:
                            scopes, textures = _load_object_graph()
                        if _open_object_hierarchy_preview(match, preview_level, scopes):
                            break
                        continue
                    if level_raw == "b":
                        return_to_match_selection = True
                        break
                    if level_raw == "q":
                        break
                    if level_raw == "x":
                        return
                    try:
                        level = int(level_raw)
                    except ValueError:
                        print(
                            "[屏蔽对象][错误] 请输入有效层级数字；"
                            "p 预览，b 返回匹配项选择，q 跳过当前匹配项，x 结束。"
                        )
                        continue
                    if level < 0 or level >= len(match["chain"]):
                        print(
                            f"[屏蔽对象][错误] 层级编号无效，请输入 0-{len(match['chain']) - 1}；"
                            "b 返回匹配项选择，q 跳过当前匹配项，x 结束。"
                        )
                        continue
                    _block_game_object(match, level)
                    break
                if return_to_match_selection:
                    break

            if return_to_match_selection:
                continue
            break

        if skip_current_image:
            continue


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
        matches = _cached_mesh_object_matches(scopes, mesh_name, mesh_targets)
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
            while True:
                level_raw = prompt_input(
                    "选择要屏蔽的层级编号；0 为当前对象，q 跳过此匹配项，x 结束: "
                ).strip().lower()
                if level_raw == "q":
                    break
                if level_raw == "x":
                    return
                try:
                    level = int(level_raw)
                except ValueError:
                    print("[Mesh屏蔽][错误] 请输入有效层级数字；q 跳过当前匹配项，x 结束。")
                    continue
                if level < 0 or level >= len(match["chain"]):
                    print(
                        f"[Mesh屏蔽][错误] 层级编号无效，请输入 0-{len(match['chain']) - 1}；"
                        "q 跳过当前匹配项，x 结束。"
                    )
                    continue
                _block_game_object(match, level)
                break


def run_block_objects_by_name() -> None:
    print()
    print("按 GameObject 名称获取链路并屏蔽")
    print("说明: 名称不区分大小写，优先精确匹配；无精确结果时自动使用包含匹配。")
    print("      可输入 *关键词* 强制包含匹配；支持预览父链并屏蔽链上任意层级。")
    scopes, _textures = _load_object_graph()
    if not any(
        type_name == "GameObject"
        for scope in scopes.values()
        for type_name, _path_id in scope["items"]
    ):
        print("[名称屏蔽][错误] 没有 GameObject 数据，请先在一键导出中选择第 2 类对象数据。")
        return

    while True:
        query = prompt_input("输入 GameObject 名称，q 返回: ").strip()
        if query.casefold() in {"q", "quit", "exit"}:
            return
        if not query:
            continue
        source_filter = ""
        candidates, match_mode = _cached_game_object_name_candidates(scopes, query)
        while len(candidates) > 200:
            counts: dict[str, int] = {}
            for candidate in candidates:
                label = str(candidate.get("source", ""))
                bundle_entry = str(candidate.get("bundle_entry", ""))
                if bundle_entry:
                    label = f"{label} | {bundle_entry}"
                counts[label] = counts.get(label, 0) + 1
            print(f"[名称屏蔽] 当前命中 {len(candidates)} 个，数量过多。主要来源:")
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:30]:
                print(f"  {count:>5}  {label}")
            source_filter = prompt_input(
                "输入来源文件/Bundle关键字缩小范围；! 仅查看前200条，q 取消: "
            ).strip()
            if source_filter.casefold() == "q":
                candidates = []
                break
            if source_filter == "!":
                candidates = candidates[:200]
                break
            candidates, match_mode = _cached_game_object_name_candidates(
                scopes,
                query,
                source_filter,
            )
        if not candidates:
            print(f"[名称屏蔽] 未找到 GameObject: {query}")
            continue

        print(
            f"[名称屏蔽] {'精确' if match_mode == 'exact' else '包含'}匹配="
            f"{len(candidates)}"
            + (f"，来源过滤={source_filter}" if source_filter else "")
        )
        matches = [_game_object_name_match(candidate) for candidate in candidates]
        matches = [match for match in matches if match.get("chain")]
        for match_index, match in enumerate(matches, start=1):
            _print_object_match(match_index, match)
        if not matches:
            print("[名称屏蔽] 命中对象无法生成父链。")
            continue

        while True:
            if len(matches) == 1:
                selected_matches = [1]
                break
            raw = prompt_input(
                f"选择匹配项编号 [1-{len(matches)}]；支持 1-2 或 1,2；"
                "q 返回名称搜索，x 结束: "
            ).strip().lower()
            if raw == "x":
                return
            if raw == "q":
                selected_matches = []
                break
            try:
                selected_matches = [
                    int(value)
                    for value in parse_number_ranges(
                        raw,
                        set(range(1, len(matches) + 1)),
                    )
                ]
                break
            except ValueError as exc:
                print(f"[名称屏蔽][错误] {exc}，请重新选择。")

        for match_index in selected_matches:
            match = matches[match_index - 1]
            if len(matches) > 1:
                _print_object_match(match_index, match)
            while True:
                level_raw = prompt_input(
                    "选择要屏蔽的层级编号；0 为当前对象，p 从最高层预览、"
                    "p3 从层级 3 预览，q 跳过，x 结束: "
                ).strip().lower()
                if level_raw == "q":
                    break
                if level_raw == "x":
                    return
                if level_raw.startswith("p"):
                    preview_level_raw = level_raw[1:].strip()
                    try:
                        preview_level = (
                            int(preview_level_raw)
                            if preview_level_raw
                            else len(match["chain"]) - 1
                        )
                    except ValueError:
                        print("[名称屏蔽][错误] 请输入 p 或 p 加层级数字，例如 p3。")
                        continue
                    if preview_level < 0 or preview_level >= len(match["chain"]):
                        print(
                            f"[名称屏蔽][错误] 预览层级必须在 0-{len(match['chain']) - 1}。"
                        )
                        continue
                    if _open_object_hierarchy_preview(match, preview_level, scopes):
                        break
                    continue
                try:
                    level = int(level_raw)
                except ValueError:
                    print("[名称屏蔽][错误] 请输入层级数字、p、q 或 x。")
                    continue
                if level < 0 or level >= len(match["chain"]):
                    print(
                        f"[名称屏蔽][错误] 层级编号必须在 0-{len(match['chain']) - 1}。"
                    )
                    continue
                _block_game_object(match, level)
                break


def _restore_store_product_record(item: dict) -> bool:
    records = _load_store_product_records()
    record_key = str(item.get("record_key", ""))
    remaining = [
        row for row in records.get("items", [])
        if isinstance(row, dict) and row.get("record_key") != record_key
    ]
    if len(remaining) == len(records.get("items", [])):
        return False
    config = {
        "config_key": item.get("config_key", ""),
        "source_json": item.get("source_json", ""),
        "source": item.get("source", ""),
        "bundle_entry": item.get("bundle_entry", ""),
        "name": item.get("store_name", ""),
        "path_id": item.get("store_path_id", 0),
        "array_path": item.get("array_path", ["_products", "Array"]),
    }
    updated = {**records, "items": remaining}
    try:
        _rebuild_store_product_replacement(config, updated)
    except ValueError as exc:
        print(f"[撤销动态条目][失败] {exc}")
        return False
    _write_store_product_records(updated)
    print(
        f"[撤销动态条目][完成] {item.get('store_name')} -> "
        f"{item.get('product_name')} (PathID={item.get('product_path_id')})"
    )
    return True


def _restore_preview_scope(scopes: dict, source: object, bundle_entry: object) -> dict | None:
    return next(
        (
            scope for scope in scopes.values()
            if str(scope.get("source", "")) == str(source or "")
            and str(scope.get("bundle_entry", "")) == str(bundle_entry or "")
        ),
        None,
    )


def _limited_restore_hierarchy(
    item: dict,
    max_parent_layers: int = 3,
    max_child_layers: int = 3,
) -> tuple[list[dict], list[dict], set[int]]:
    chain = item.get("hierarchy_chain")
    limited_chain = [
        node for node in chain[:max_parent_layers + 1]
        if isinstance(node, dict)
    ] if isinstance(chain, list) else []
    descendants = item.get("hierarchy_descendants")
    limited_descendants = [
        node for node in descendants
        if isinstance(node, dict)
        and 1 <= int(node.get("depth", 0) or 0) <= max_child_layers
    ] if isinstance(descendants, list) else []
    allowed_path_ids = {
        int(node.get("path_id", 0) or 0)
        for node in [*limited_chain, *limited_descendants]
        if int(node.get("path_id", 0) or 0)
    }
    return limited_chain, limited_descendants, allowed_path_ids


def _restore_record_preview_image(row: dict, scopes: dict):
    from PIL import Image

    item = row["item"]
    if row["kind"] == "product":
        image_scope = _restore_preview_scope(
            scopes,
            item.get("sprite_source") or item.get("source"),
            item.get("sprite_bundle_entry") or item.get("bundle_entry"),
        )
        stored_product = {
            "image_kind": item.get("image_kind", ""),
            "image_scope": image_scope,
            "image_path_id": item.get("image_path_id", 0),
            "texture_path_id": item.get("texture_path_id", 0),
            "ngui_atlas_path_id": item.get("ngui_atlas_path_id", 0),
            "ngui_sprite_name": item.get("ngui_sprite_name", ""),
            "sprite_scope": image_scope,
            "sprite_path_id": item.get("sprite_path_id", 0),
        }
        image = _store_product_preview_image(stored_product)
        if image is not None:
            return image
        config_scope = _restore_preview_scope(
            scopes, item.get("source"), item.get("bundle_entry")
        )
        if config_scope:
            product_scope = _resolve_pointer_scope(
                config_scope, int(item.get("product_file_id", 0) or 0)
            )
            descriptor = _store_product_descriptor(
                config_scope,
                int(item.get("product_file_id", 0) or 0),
                int(item.get("product_path_id", 0) or 0),
                int(item.get("original_index", 0) or 0),
            )
            if product_scope and descriptor.get("resolved"):
                return _store_product_preview_image(descriptor)
        return None

    chain = item.get("hierarchy_chain")
    if isinstance(chain, list) and chain:
        limited_chain, _limited_descendants, allowed_path_ids = (
            _limited_restore_hierarchy(item)
        )
        if not limited_chain:
            return None
        match = {
            "source": item.get("source_resource", ""),
            "bundle_entry": item.get("bundle_entry", ""),
            "component_type": item.get("component_type", "GameObject"),
            "component_path_id": item.get("component_path_id", 0),
            "sprite_path_id": item.get("preview_sprite_path_id", 0),
            "texture_path_id": item.get("preview_texture_path_id", 0),
            "chain": limited_chain,
            "limited_hierarchy_path_ids": sorted(allowed_path_ids),
        }
        root_level = len(limited_chain) - 1
        preview_path = _create_object_hierarchy_preview(match, root_level, scopes)
        with Image.open(preview_path) as preview:
            return preview.convert("RGBA")
    scope = _restore_preview_scope(
        scopes, item.get("source_resource"), item.get("bundle_entry")
    )
    sprite_path_id = int(item.get("preview_sprite_path_id", 0) or 0)
    if scope and sprite_path_id:
        return _preview_sprite_image(scope, sprite_path_id)
    if scope:
        object_entry = _scope_entry(
            scope,
            ("GameObject",),
            int(item.get("game_object_path_id", 0) or 0),
        )
        object_data = _entry_data(object_entry) if object_entry else None
        component = _preview_component(scope, object_data) if isinstance(object_data, dict) else None
        if component:
            return component[1]
    return None


def _run_restore_blocked_terminal(rows: list[dict]) -> None:
    for index, row in enumerate(rows, start=1):
        item = row["item"]
        if row["kind"] == "product":
            print(
                f"{index}. [动态条目] {item.get('store_name')} -> "
                f"{item.get('product_name')} (PathID={item.get('product_path_id')})"
            )
        else:
            print(
                f"{index}. [Object] {item.get('game_object_name')} "
                f"(PathID={item.get('game_object_path_id')})"
            )
    raw = prompt_input(
        "选择要撤销的编号（例: 2、1,3,5、2-6），a 全部，q 取消: "
    ).strip().lower()
    if raw == "q":
        return
    selected = (
        list(range(1, len(rows) + 1))
        if raw == "a"
        else [int(value) for value in parse_number_ranges(raw, set(range(1, len(rows) + 1)))]
    )
    for index in sorted(selected, reverse=True):
        row = rows[index - 1]
        if row["kind"] == "product":
            _restore_store_product_record(row["item"])
        elif _restore_block_record_item(row["item"]):
            records = _load_block_records()
            records["items"] = [
                item for item in records.get("items", [])
                if not isinstance(item, dict)
                or item.get("record_key") != row["item"].get("record_key")
            ]
            _write_block_records(records)


RESTORE_PREVIEW_ZOOM_LEVELS = (
    0.1, 0.125, 0.2, 0.25, 0.33, 0.5, 0.67, 0.8,
    1.0, 1.25, 1.5, 2.0, 3.0, 4.0,
)


def _restore_preview_step_zoom(current: float, direction: int) -> float:
    if direction > 0:
        candidates = [
            value for value in RESTORE_PREVIEW_ZOOM_LEVELS
            if value > current + 0.001
        ]
        return candidates[0] if candidates else RESTORE_PREVIEW_ZOOM_LEVELS[-1]
    candidates = [
        value for value in RESTORE_PREVIEW_ZOOM_LEVELS
        if value < current - 0.001
    ]
    return candidates[-1] if candidates else RESTORE_PREVIEW_ZOOM_LEVELS[0]


def _restore_preview_fit_zoom(
    image_size: tuple[int, int],
    viewport_size: tuple[int, int],
) -> float:
    image_width, image_height = image_size
    viewport_width, viewport_height = viewport_size
    if min(image_width, image_height, viewport_width, viewport_height) <= 0:
        return 1.0
    return max(
        RESTORE_PREVIEW_ZOOM_LEVELS[0],
        min(1.0, viewport_width / image_width, viewport_height / image_height),
    )


def _show_restore_blocked_window(rows: list[dict], scopes: dict) -> None:
    import tkinter as tk
    from tkinter import ttk
    from PIL import Image, ImageTk

    window = tk.Tk()
    window.title("统一撤销屏蔽")
    window.geometry("1180x720")
    window.minsize(900, 560)
    window.configure(background="#12151b")
    photos: dict[str, object] = {}

    tk.Label(
        window,
        text=(
            "已屏蔽项目 · Object 层级预览最多向上/向下各 3 层，"
            "动态条目显示对应图片 · 预览区滚轮缩放、左键拖动"
        ),
        background="#12151b", foreground="#43e081",
        font=("SimSun", 12, "bold"), anchor="w",
    ).pack(fill="x", padx=12, pady=10)
    body = tk.PanedWindow(window, orient="horizontal", sashwidth=6, background="#30343d")
    body.pack(fill="both", expand=True, padx=12)
    left = tk.Frame(body, background="#12151b")
    right = tk.Frame(body, background="#1c1f26")
    body.add(left, minsize=380)
    body.add(right, minsize=450)
    tree = ttk.Treeview(left, columns=("kind", "name", "source"), show="headings", selectmode="extended")
    for column, title, width in (
        ("kind", "类型", 80), ("name", "屏蔽项目", 180), ("source", "来源/列表", 160)
    ):
        tree.heading(column, text=title)
        tree.column(column, width=width, anchor="w")
    scrollbar = ttk.Scrollbar(left, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scrollbar.set)
    tree.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")
    zoom_bar = tk.Frame(right, background="#1c1f26")
    zoom_bar.pack(fill="x", padx=10, pady=(10, 0))
    preview_frame = tk.Frame(right, background="#20242c")
    preview_frame.pack(fill="both", expand=True, padx=10, pady=(6, 10))
    preview = tk.Canvas(
        preview_frame,
        background="#20242c",
        highlightthickness=0,
    )
    preview_horizontal = ttk.Scrollbar(
        preview_frame, orient="horizontal", command=preview.xview,
    )
    preview_vertical = ttk.Scrollbar(
        preview_frame, orient="vertical", command=preview.yview,
    )
    preview.configure(
        xscrollcommand=preview_horizontal.set,
        yscrollcommand=preview_vertical.set,
    )
    preview.grid(row=0, column=0, sticky="nsew")
    preview_vertical.grid(row=0, column=1, sticky="ns")
    preview_horizontal.grid(row=1, column=0, sticky="ew")
    preview_frame.rowconfigure(0, weight=1)
    preview_frame.columnconfigure(0, weight=1)
    preview_image_item = preview.create_image(0, 0, anchor="nw")
    preview_message_item = preview.create_text(
        0, 0,
        text="选择左侧项目查看预览",
        fill="#a8adb8",
        anchor="center",
        font=("SimSun", 11),
    )
    preview_source: dict[str, object | None] = {"image": None}
    preview_zoom: dict[str, float] = {"value": 1.0}
    preview_zoom_text = tk.StringVar(value="100%")
    preview_image_cache: dict[float, object] = {}
    detail = tk.StringVar()
    tk.Label(
        right, textvariable=detail, justify="left", anchor="w",
        background="#1c1f26", foreground="#f0a04b", font=("SimSun", 10),
    ).pack(fill="x", padx=10, pady=(0, 10))

    def position_preview_message(_event=None) -> None:
        preview.coords(
            preview_message_item,
            max(1, preview.winfo_width()) / 2,
            max(1, preview.winfo_height()) / 2,
        )

    def clear_preview(message: str) -> None:
        preview_source["image"] = None
        preview_image_cache.clear()
        photos.clear()
        preview.itemconfigure(preview_image_item, image="")
        preview.itemconfigure(preview_message_item, text=message)
        preview.configure(scrollregion=(0, 0, 0, 0))
        preview.xview_moveto(0)
        preview.yview_moveto(0)
        preview_zoom["value"] = 1.0
        preview_zoom_text.set("100%")
        position_preview_message()

    def set_preview_zoom(value: float, keep_center: bool = True) -> None:
        source_image = preview_source["image"]
        if not isinstance(source_image, Image.Image):
            return
        old_zoom = max(0.001, preview_zoom["value"])
        center_x = preview.canvasx(max(1, preview.winfo_width()) / 2) / old_zoom
        center_y = preview.canvasy(max(1, preview.winfo_height()) / 2) / old_zoom
        value = max(
            RESTORE_PREVIEW_ZOOM_LEVELS[0],
            min(RESTORE_PREVIEW_ZOOM_LEVELS[-1], round(value, 4)),
        )
        preview_zoom["value"] = value
        scaled_width = max(1, round(source_image.width * value))
        scaled_height = max(1, round(source_image.height * value))
        photo = preview_image_cache.get(value)
        if photo is None:
            resized = (
                source_image
                if value == 1.0
                else source_image.resize(
                    (scaled_width, scaled_height),
                    Image.Resampling.LANCZOS if value < 1.0 else Image.Resampling.BICUBIC,
                )
            )
            photo = ImageTk.PhotoImage(resized)
            preview_image_cache[value] = photo
        photos["preview"] = photo
        preview.itemconfigure(preview_image_item, image=photo)
        preview.itemconfigure(preview_message_item, text="")
        preview.configure(scrollregion=(0, 0, scaled_width, scaled_height))
        preview_zoom_text.set(f"{round(value * 100)}%")
        if keep_center:
            preview.update_idletasks()
            target_left = center_x * value - max(1, preview.winfo_width()) / 2
            target_top = center_y * value - max(1, preview.winfo_height()) / 2
            preview.xview_moveto(max(0.0, target_left / scaled_width))
            preview.yview_moveto(max(0.0, target_top / scaled_height))

    def fit_preview_to_window() -> None:
        source_image = preview_source["image"]
        if not isinstance(source_image, Image.Image):
            return
        preview.update_idletasks()
        value = _restore_preview_fit_zoom(
            source_image.size,
            (max(1, preview.winfo_width() - 4), max(1, preview.winfo_height() - 4)),
        )
        set_preview_zoom(value, keep_center=False)
        preview.xview_moveto(0)
        preview.yview_moveto(0)

    def step_preview_zoom(direction: int) -> None:
        set_preview_zoom(
            _restore_preview_step_zoom(preview_zoom["value"], direction)
        )

    def on_preview_mouse_wheel(event) -> str:
        step_preview_zoom(1 if event.delta > 0 else -1)
        return "break"

    def start_preview_drag(event) -> str:
        if isinstance(preview_source["image"], Image.Image):
            preview.scan_mark(event.x, event.y)
            preview.configure(cursor="fleur")
        return "break"

    def drag_preview(event) -> str:
        if isinstance(preview_source["image"], Image.Image):
            preview.scan_dragto(event.x, event.y, gain=1)
        return "break"

    def stop_preview_drag(_event=None) -> str:
        preview.configure(cursor="hand2")
        return "break"

    ttk.Button(
        zoom_bar, text="−", width=3,
        command=lambda: step_preview_zoom(-1),
    ).pack(side="left")
    ttk.Label(
        zoom_bar, textvariable=preview_zoom_text, width=7, anchor="center",
    ).pack(side="left", padx=4)
    ttk.Button(
        zoom_bar, text="+", width=3,
        command=lambda: step_preview_zoom(1),
    ).pack(side="left")
    ttk.Button(
        zoom_bar, text="100%", command=lambda: set_preview_zoom(1.0),
    ).pack(side="left", padx=(8, 0))
    ttk.Button(
        zoom_bar, text="适应窗口", command=fit_preview_to_window,
    ).pack(side="left", padx=(8, 0))
    tk.Label(
        zoom_bar,
        text="滚轮缩放 · 左键拖动",
        foreground="#7f8794",
        background="#1c1f26",
    ).pack(side="right")

    def current_rows() -> list[dict]:
        return [rows[int(item_id)] for item_id in tree.selection() if str(item_id).isdigit()]

    def refresh() -> None:
        tree.delete(*tree.get_children())
        for index, row in enumerate(rows):
            item = row["item"]
            if row["kind"] == "product":
                name = str(item.get("product_name", ""))
                source = str(item.get("store_name", ""))
                kind = "动态条目"
            else:
                name = str(item.get("game_object_name", ""))
                source = str(item.get("source_resource", ""))
                kind = "Object"
            tree.insert("", "end", iid=str(index), values=(kind, name, source))

    def show_preview(_event=None) -> None:
        selected = current_rows()
        if not selected:
            return
        row = selected[0]
        item = row["item"]
        if row["kind"] == "product":
            detail.set(
                f"条目: {item.get('product_name')}\n列表: {item.get('store_name')}\n"
                f"图片: {item.get('image_name') or item.get('sprite_name') or '<无图片>'}"
            )
        else:
            chain = item.get("hierarchy_chain")
            limited_chain, limited_descendants, _allowed = _limited_restore_hierarchy(item)
            max_child_depth = max(
                (int(node.get("depth", 0) or 0) for node in limited_descendants),
                default=0,
            )
            detail.set(
                f"Object: {item.get('game_object_name')}\n"
                f"PathID: {item.get('game_object_path_id')}\n"
                f"层级记录: {'有' if isinstance(chain, list) and chain else '无（仅尝试显示对应图片）'}\n"
                f"本次预览范围: 向上 {max(0, len(limited_chain) - 1)} 层，"
                f"向下 {max_child_depth} 层"
            )
        try:
            image = _restore_record_preview_image(row, scopes)
        except Exception as exc:
            image = None
            detail.set(f"{detail.get()}\n预览生成失败: {exc}")
        if image is None:
            clear_preview("该旧记录没有可恢复的层级或图片信息")
            return
        preview_source["image"] = image.convert("RGBA")
        preview_image_cache.clear()
        photos.clear()
        fit_preview_to_window()

    def restore_selected() -> None:
        selected_indexes = sorted(
            (int(item_id) for item_id in tree.selection() if str(item_id).isdigit()),
            reverse=True,
        )
        for index in selected_indexes:
            row = rows[index]
            restored = False
            if row["kind"] == "product":
                restored = _restore_store_product_record(row["item"])
            else:
                restored = _restore_block_record_item(row["item"])
                if restored:
                    records = _load_block_records()
                    records["items"] = [
                        item for item in records.get("items", [])
                        if not isinstance(item, dict)
                        or item.get("record_key") != row["item"].get("record_key")
                    ]
                    _write_block_records(records)
            if restored:
                rows.pop(index)
        refresh()
        clear_preview("选择左侧项目查看预览")
        detail.set("")
        if not rows:
            window.destroy()

    buttons = tk.Frame(window, background="#12151b")
    buttons.pack(fill="x", padx=12, pady=10)
    ttk.Button(buttons, text="撤销所选屏蔽", command=restore_selected).pack(side="left")
    ttk.Button(buttons, text="关闭", command=window.destroy).pack(side="right")
    tree.bind("<<TreeviewSelect>>", show_preview)
    preview.bind("<Configure>", position_preview_message)
    preview.bind("<MouseWheel>", on_preview_mouse_wheel)
    preview.bind("<ButtonPress-1>", start_preview_drag)
    preview.bind("<B1-Motion>", drag_preview)
    preview.bind("<ButtonRelease-1>", stop_preview_drag)
    preview.configure(cursor="hand2")
    window.bind("<Control-plus>", lambda _event: step_preview_zoom(1))
    window.bind("<Control-minus>", lambda _event: step_preview_zoom(-1))
    window.bind("<Control-0>", lambda _event: fit_preview_to_window())
    refresh()
    if rows:
        tree.selection_set("0")
        show_preview()
    window.mainloop()


def run_restore_blocked_objects() -> None:
    object_records = _load_block_records()
    product_records = _load_store_product_records()
    rows = [
        {"kind": "object", "item": item}
        for item in object_records.get("items", []) if isinstance(item, dict)
    ] + [
        {"kind": "product", "item": item}
        for item in product_records.get("items", []) if isinstance(item, dict)
    ]
    if not rows:
        print("[统一撤销] 没有 Object 或动态条目屏蔽记录。")
        return
    print(
        f"[统一撤销] Object={len(object_records.get('items', []))}，"
        f"动态条目={len(product_records.get('items', []))}"
    )
    try:
        scopes, _textures = _load_object_graph()
        _show_restore_blocked_window(rows, scopes)
    except Exception as exc:
        print(f"[统一撤销][提示] 预览窗口不可用，改用终端撤销: {exc}")
        _run_restore_blocked_terminal(rows)


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
        codex_circuit: dict[str, object] = {}
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
                    codex_circuit=codex_circuit,
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
    try:
        return load_config(quiet=True).stage_record_dir
    except Exception:
        return DEFAULT_RECORD_ROOT


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


def _select_ai_batch_request_paths() -> list[Path] | None:
    records_dir = _default_ai_records_dir()
    request_files = sorted(records_dir.glob("ai_translation_request*.json"))
    if not request_files:
        print(f"未找到 AI request 批次文件: {records_dir / 'ai_translation_request*.json'}")
        raw = prompt_input("可手动输入 request 文件路径，或直接回车返回: ").strip()
        return [Path(raw)] if raw else None

    print()
    print("可用 AI request 批次文件:")
    strategy = get_strategy(load_config(quiet=True))
    for index, request_path in enumerate(request_files, start=1):
        response_path = _default_response_path_for_ai_request(request_path)
        response_state = _ai_response_state(request_path, response_path, strategy)
        print(f"{index}. {request_path.name} ({response_state})")
    print("q. 返回")

    while True:
        raw = prompt_input(
            "请选择批次编号/范围（例: 10-12、10,12,15、10-12,15），"
            "或输入 request 文件路径: "
        ).strip()
        if raw.lower() in {"q", "quit", "exit"}:
            return None
        if re.fullmatch(r"[\d\s,，-]+", raw or ""):
            try:
                selected = parse_number_ranges(raw, set(range(1, len(request_files) + 1)))
            except ValueError as exc:
                print(f"批次选择无效: {exc}，请重新输入。")
                continue
            return [request_files[int(value) - 1] for value in selected]
        if raw:
            return [Path(raw)]
        print("输入不能为空，请重新输入。")


def run_ai_translation_batch_tool(
    mode: str,
    request_path: Path,
    response_path: Path | None = None,
    patch_after: bool = False,
    codex_circuit_file: Path | None = None,
) -> int:
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
    if codex_circuit_file is not None:
        command.extend(["--codex-circuit-file", str(codex_circuit_file)])
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
    print("AI 翻译批次补跑 / 修补 trans.json")
    print(
        "说明: 可选择一个或多个 ai_translation_request_batch_XXX.json，"
        "重新生成各自 response，或批量修补 trans.json。"
    )
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

    request_paths = _select_ai_batch_request_paths()
    if not request_paths:
        return
    succeeded: list[Path] = []
    failed: list[tuple[Path, int]] = []
    with tempfile.TemporaryDirectory(prefix="translate_codex_circuit_") as circuit_dir:
        codex_circuit_file = Path(circuit_dir) / "open.json"
        for index, request_path in enumerate(request_paths, start=1):
            response_path = _default_response_path_for_ai_request(request_path)
            print()
            print(
                f"[AI补批] 批次 {index}/{len(request_paths)}: {request_path.name}"
            )
            print(f"[AI补批] response 文件将使用: {response_path}")
            result = run_ai_translation_batch_tool(
                mode,
                request_path,
                response_path,
                codex_circuit_file=codex_circuit_file,
            )
            if result != 0:
                failed.append((request_path, result))
                print(
                    f"\033[91m[AI补批][失败] {request_path.name} "
                    f"返回码={result}；继续后续批次。\033[0m"
                )
                continue
            succeeded.append(request_path)
            print(f"\033[92m[AI补批][完成] {request_path.name}\033[0m")

    print()
    print(
        f"[AI补批][汇总] 选择={len(request_paths)}，"
        f"成功={len(succeeded)}，失败={len(failed)}"
    )
    for request_path, result in failed:
        print(f"  失败: {request_path.name}（返回码={result}）")
    if not succeeded:
        return
    if mode == "resend":
        print(
            "\033[94m[AI补批][下一步] 成功批次的 response 已重新生成。"
            "请继续使用工具脚本主菜单 3 的选项 2，将该 response 修补进 trans.json。\033[0m"
        )
        return
    print("\033[92m[AI补批] 成功批次已修补 trans.json。\033[0m")
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
        context = f"{prefix}{left}\033[91m{char}\033[0m{right}{suffix}"
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
        name = entry.get("name", "") if isinstance(entry, dict) else ""
        rule_count = count_script_output_rules(manifest, [script_id])
        expanded_ids = expand_script_output_ids(manifest, [script_id])
        included_ids = [value for value in expanded_ids if value != script_id]
        if included_ids:
            included_text = ",".join(included_ids)
            print(f"{script_id}. {name}（展开包含 {included_text}，清单规则共 {rule_count} 条）")
        else:
            print(f"{script_id}. {name}（清单规则 {rule_count} 条）")

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


def run_search_tools_menu() -> None:
    while True:
        print()
        print("S. 搜索")
        print("1. 查找字符串并复制 JSON")
        print("2. 查找 PathID 对应文件路径")
        print("3. 查找资源名对应文件路径")
        print("b. 返回主菜单")
        try:
            choice = prompt_input("请选择搜索工具: ").strip().lower().lstrip("\ufeff")
        except EOFError:
            return

        if choice == "1":
            run_search_copy()
            continue
        if choice == "2":
            run_find_path_id()
            continue
        if choice == "3":
            run_find_asset_name()
            continue
        if choice in {"b", "q", "back", "quit", "exit"}:
            return
        print("无效选择，请输入 1-3 或 b。")


def run_test_tools_menu() -> None:
    while True:
        print()
        print("T. 测试")
        print("1. Unity 资源兼容性/导出状态检查")
        print("2. 按 trans_maybe_title.json 清理 trans.json")
        print("3. 从完整备份按字段恢复 records，并补译到 trans")
        print("b. 返回主菜单")
        try:
            choice = prompt_input("请选择测试工具: ").strip().lower().lstrip("\ufeff")
        except EOFError:
            return

        if choice == "1":
            run_compatibility_check()
            continue
        if choice == "2":
            run_remove_maybe_titles_from_trans()
            continue
        if choice == "3":
            run_restore_records_by_field()
            continue
        if choice in {"b", "q", "back", "quit", "exit"}:
            return
        print("无效选择，请输入 1-3 或 b。")


def main() -> int:
    entry_args = _ENTRY_ARGS if _ENTRY_ARGS is not None else _parse_entry_args(sys.argv[1:], activate=False)
    if entry_args:
        command = entry_args[0].strip().lower()
        if command in {"resend-ai-batch", "ai-batch-resend"}:
            if len(entry_args) < 2:
                print(f"用法: python {Path(__file__).name} resend-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("resend", Path(entry_args[1]))
        if command in {"patch-ai-batch", "ai-batch-patch"}:
            if len(entry_args) < 2:
                print(f"用法: python {Path(__file__).name} patch-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("patch-trans", Path(entry_args[1]))
        if command in {"resend-and-patch-ai-batch", "ai-batch-resend-and-patch"}:
            if len(entry_args) < 2:
                print(f"用法: python {Path(__file__).name} resend-and-patch-ai-batch <ai_translation_request_batch_XXX.json>")
                return 1
            return run_ai_translation_batch_tool("resend-and-patch", Path(entry_args[1]))
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
        if command in {"block-named-objects", "block-objects-by-name"}:
            run_block_objects_by_name()
            return 0
        if command in {
            "block-store-products", "block-dynamic-store-products", "block-dynamic-lists"
        }:
            run_block_dynamic_store_products()
            return 0
        if command in {"verify-repacked-resources", "verify-resources"}:
            return subprocess.run([sys.executable, str(RESOURCE_REPACK_VALIDATOR)], check=False).returncode
        print(f"未知命令: {entry_args[0]}")
        print(f"用法: python {Path(__file__).name} resend-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} patch-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} resend-and-patch-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} clean-unsupported-ttf-chars")
        print(f"或: python {Path(__file__).name} clean-script-outputs")
        print(f"或: python {Path(__file__).name} clean-trans-maybe-title")
        print(f"或: python {Path(__file__).name} block-dynamic-lists")
        return 1

    print(f"当前项目工作区: {DEFAULT_WORKSPACE_ROOT}")
    while True:
        print()
        print("工具菜单")
        print("0. 验证修改后重打资源与原始资源")
        print("1. 一键复制导出图片到 workspace/AllPNG")
        print("2. 按 Sprite / NGUI UIAtlas 数据拆分 Texture2D 图集")
        print("3. AI 翻译批次补跑 / 修补 trans.json")
        print("4. 清理 trans.json 中模板 TTF 不支持的字符")
        print("5. 按图片定位对象并选择层级屏蔽")
        print("6. 按 Mesh 定位对象并选择层级屏蔽")
        print("7. 按 Object 名称获取链路并选择层级屏蔽")
        print("8. 动态列表索引与选择性屏蔽（测试阶段）")
        print("9. 统一撤销已记录的 Object/动态条目屏蔽")
        print()
        print("S. 搜索\t\tT. 测试\t\tC. 清理主脚本产物\t\tq. 退出")
        try:
            choice = prompt_input("请选择: ").strip().lower().lstrip("\ufeff")
        except EOFError:
            return 0

        if choice == "0":
            subprocess.run([sys.executable, str(RESOURCE_REPACK_VALIDATOR)], check=False)
            continue
        if choice == "1":
            run_copy_all_images()
            continue
        if choice == "2":
            run_split_sprite_atlases()
            continue
        if choice == "3":
            run_ai_translation_batch_menu()
            continue
        if choice == "4":
            run_clean_unsupported_ttf_chars()
            continue
        if choice == "5":
            run_block_objects_by_image()
            continue
        if choice == "6":
            run_block_objects_by_mesh()
            continue
        if choice == "7":
            run_block_objects_by_name()
            continue
        if choice == "8":
            run_block_dynamic_store_products()
            continue
        if choice == "9":
            run_restore_blocked_objects()
            continue
        if choice == "s":
            run_search_tools_menu()
            continue
        if choice == "t":
            run_test_tools_menu()
            continue
        if choice == "c":
            run_clean_script_outputs()
            continue
        if choice in {"q", "quit", "exit"}:
            return 0

        print("无效选择，请输入 0-9、S、T、C 或 q。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
