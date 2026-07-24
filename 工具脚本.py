from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from support.config import load_config
from support.image_import_utils import copy_image_for_import
from pipeline.ai_translation_strategy import get_strategy
from pipeline.catalog_tools import (
    auto_patch_and_repack_catalog_after_import,
    calculate_final_bundle_crcs_manually,
    patch_expanded_catalog_from_final_bundles,
    parse_catalog_to_output,
    repack_expanded_catalog,
    validate_catalog_crc_algorithm,
    _rebuild_request_option_raw,
    _source_catalog_android_root,
)
from pipeline.translation import _is_blacklisted_string_field, disable_translated_text_effect_components
from pipeline.tmp_pipeline import sync_generated_tmp_material_parameters


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_ROOT = SCRIPT_DIR / "workspace" / "input"
DEFAULT_DEST_ROOT = SCRIPT_DIR / "workspace" / "手动替换"
DEFAULT_ALL_IMAGE_ROOT = SCRIPT_DIR / "workspace" / "AllPNG"
DEFAULT_ALL_IMAGE_PNG_ROOT = DEFAULT_ALL_IMAGE_ROOT / "PNG"
DEFAULT_EDITED_IMAGE_ROOT = SCRIPT_DIR / "workspace" / "output" / "Image" / "修改后的图片目录"
DEFAULT_IMAGE_TO_IMPORT_ROOT = SCRIPT_DIR / "workspace" / "output" / "Image" / "ToImport"
DEFAULT_ALL_IMAGE_MAP = DEFAULT_ALL_IMAGE_ROOT / "_allpng_map.json"
DEFAULT_MISSING_TTF_CHARS_FILE = SCRIPT_DIR / "workspace" / "records" / "translation_chars_missing_from_ttf.txt"
DEFAULT_TRANS_JSON = SCRIPT_DIR / "workspace" / "records" / "trans.json"
DEFAULT_RECORDS_JSON = SCRIPT_DIR / "workspace" / "records" / "records.json"
FIND_PATH_ID_SCRIPT = SCRIPT_DIR / "support" / "查找PathID文件.py"
FIND_ASSET_NAME_SCRIPT = SCRIPT_DIR / "support" / "查找资源名文件.py"
AI_TRANSLATION_BATCH_TOOL = SCRIPT_DIR / "tools" / "ai_translation_batch_tool.py"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp"}
M_NAME_JSON_RE = re.compile(r'("m_Name"\s*:\s*)"(?:\\.|[^"\\])*"')
M_NAME_YAML_RE = re.compile(r"(^\s*m_Name:\s*)(.*)$", re.MULTILINE)


def prompt_input(message: str) -> str:
    return input(f"\033[38;5;208m{message}\033[0m")


def iter_json_files(root: Path):
    for path in root.rglob("*.json"):
        if path.is_file():
            yield path


def iter_monobehaviour_json_files(root: Path):
    for path in root.rglob("*.json"):
        if path.is_file() and "MonoBehaviour" in path.parts:
            yield path


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


def read_text_fallback(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore")


def extract_m_name(path: Path) -> str:
    text = read_text_fallback(path)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, dict):
        value = data.get("m_Name")
        if isinstance(value, str) and value:
            return value

    match = M_NAME_JSON_RE.search(text)
    if match:
        value_text = match.group(0).split(":", 1)[1].strip()
        try:
            value = json.loads(value_text)
        except json.JSONDecodeError:
            value = ""
        if isinstance(value, str) and value:
            return value

    match = M_NAME_YAML_RE.search(text)
    if match:
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if value:
            return value

    return path.stem


def replace_m_name_text(text: str, name: str) -> str:
    json_name = json.dumps(name, ensure_ascii=False)
    if M_NAME_JSON_RE.search(text):
        return M_NAME_JSON_RE.sub(lambda match: f"{match.group(1)}{json_name}", text, count=1)
    if M_NAME_YAML_RE.search(text):
        return M_NAME_YAML_RE.sub(lambda match: f"{match.group(1)}{name}", text, count=1)
    raise ValueError("空 mesh 文件中没有找到 m_Name 属性")


def looks_like_mesh_dump(path: Path, text: str) -> bool:
    if "Mesh" in path.parts:
        return True
    mesh_markers = (
        '"m_SubMeshes"',
        '"m_VertexData"',
        '"m_IndexBuffer"',
        '"m_MeshCompression"',
        "m_SubMeshes:",
        "m_VertexData:",
        "m_IndexBuffer:",
        "m_MeshCompression:",
    )
    return any(marker in text for marker in mesh_markers)


def iter_empty_mesh_targets(target_root: Path, include_all_files: bool):
    for path in sorted(target_root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.lower() == "manifest.json":
            continue
        if include_all_files or path.suffix.lower() == ".json":
            yield path


def clear_unity_array(value):
    if isinstance(value, dict) and isinstance(value.get("Array"), list):
        value["Array"] = []
    elif isinstance(value, list):
        value.clear()


def clear_packed_mesh_stream(value):
    if not isinstance(value, dict):
        return
    for key in ("m_NumItems", "m_BitSize", "m_Start", "m_Range"):
        if key in value and isinstance(value[key], (int, float)):
            value[key] = 0
    if "m_Data" in value:
        if isinstance(value["m_Data"], str):
            value["m_Data"] = ""
        else:
            clear_unity_array(value["m_Data"])


def clear_compressed_mesh(value):
    if not isinstance(value, dict):
        return
    for child in value.values():
        if isinstance(child, dict):
            clear_packed_mesh_stream(child)


def clear_mesh_shapes(value):
    if not isinstance(value, dict):
        return
    for key in ("vertices", "shapes", "channels", "fullWeights"):
        if key in value:
            clear_unity_array(value[key])


def clear_stream_data(value):
    if not isinstance(value, dict):
        return
    for key in ("offset", "size"):
        if key in value:
            value[key] = 0
    if "path" in value:
        value["path"] = ""


def clear_vertex_data(value):
    if not isinstance(value, dict):
        return
    if "m_VertexCount" in value:
        value["m_VertexCount"] = 0
    if "m_Channels" in value:
        clear_unity_array(value["m_Channels"])
    if "m_DataSize" in value:
        if isinstance(value["m_DataSize"], str):
            value["m_DataSize"] = ""
        else:
            clear_unity_array(value["m_DataSize"])
    if "m_StreamData" in value:
        clear_stream_data(value["m_StreamData"])


def clear_mesh_data(data: dict) -> None:
    for key in (
        "m_SubMeshes",
        "m_BindPose",
        "m_BoneNameHashes",
        "m_BakedConvexCollisionMesh",
        "m_BakedTriangleCollisionMesh",
    ):
        if key in data:
            clear_unity_array(data[key])

    for key in ("m_RootBoneNameHash", "m_MeshUsageFlags", "m_IndexFormat", "m_MeshCompression"):
        if key in data:
            data[key] = 0

    for key in ("m_IsReadable", "m_KeepVertices", "m_KeepIndices"):
        if key in data:
            data[key] = 1

    for key in ("m_IndexBuffer", "m_Skin"):
        if key in data:
            if isinstance(data[key], str):
                data[key] = ""
            else:
                clear_unity_array(data[key])

    if "m_Shapes" in data:
        clear_mesh_shapes(data["m_Shapes"])
    if "m_VertexData" in data:
        clear_vertex_data(data["m_VertexData"])
    if "m_CompressedMesh" in data:
        clear_compressed_mesh(data["m_CompressedMesh"])
    if "m_StreamData" in data:
        clear_stream_data(data["m_StreamData"])

    for key in ("m_LocalAABB", "m_CollisionMeshAABB"):
        if isinstance(data.get(key), dict):
            center = data[key].get("m_Center")
            extent = data[key].get("m_Extent")
            for vector in (center, extent):
                if isinstance(vector, dict):
                    for axis in ("x", "y", "z"):
                        if axis in vector:
                            vector[axis] = 0


def clear_mesh_files(target_root: Path, include_all_files: bool = False) -> int:
    if not target_root.is_dir():
        raise FileNotFoundError(f"目标目录不存在: {target_root}")

    cleared = 0
    skipped = 0
    files = list(iter_empty_mesh_targets(target_root, include_all_files))
    print(f"[清空Mesh] 待处理文件数: {len(files)}")
    for index, target_path in enumerate(files, start=1):
        if index == 1 or index % 200 == 0 or index == len(files):
            print(f"[清空Mesh] 进度: {index}/{len(files)}，已清空: {cleared}，已跳过: {skipped}", flush=True)

        target_text = read_text_fallback(target_path)
        if not looks_like_mesh_dump(target_path, target_text):
            skipped += 1
            continue
        try:
            data = json.loads(target_text)
        except json.JSONDecodeError:
            skipped += 1
            print(f"[SKIP] 暂不支持非 JSON Mesh dump: {target_path}")
            continue
        if not isinstance(data, dict):
            skipped += 1
            continue

        clear_mesh_data(data)
        target_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cleared += 1
        print(f"[CLEAR] {target_path}，m_Name={data.get('m_Name', target_path.stem)}")

    if skipped:
        print(f"[清空Mesh] 已跳过 {skipped} 个文件。")
    return cleared


def replace_with_empty_mesh(target_root: Path, empty_mesh_file: Path, include_all_files: bool = False) -> int:
    if not target_root.is_dir():
        raise FileNotFoundError(f"目标目录不存在: {target_root}")
    if not empty_mesh_file.is_file():
        raise FileNotFoundError(f"空 mesh 文件不存在: {empty_mesh_file}")

    template_text = read_text_fallback(empty_mesh_file)
    if not looks_like_mesh_dump(empty_mesh_file, template_text):
        raise ValueError(f"空 mesh 文件不像 Mesh 导出文件，已停止: {empty_mesh_file}")

    replaced = 0
    skipped = 0
    files = list(iter_empty_mesh_targets(target_root, include_all_files))
    print(f"[空Mesh] 待处理文件数: {len(files)}")
    for index, target_path in enumerate(files, start=1):
        if target_path.resolve() == empty_mesh_file.resolve():
            continue
        if index == 1 or index % 200 == 0 or index == len(files):
            print(f"[空Mesh] 替换进度: {index}/{len(files)}，已替换: {replaced}，已跳过: {skipped}", flush=True)

        target_text = read_text_fallback(target_path)
        if not looks_like_mesh_dump(target_path, target_text):
            skipped += 1
            continue
        original_name = extract_m_name(target_path)
        new_text = replace_m_name_text(template_text, original_name)
        target_path.write_text(new_text, encoding="utf-8")
        replaced += 1
        print(f"[REPLACE] {target_path}，m_Name={original_name}")

    if skipped:
        print(f"[空Mesh] 已跳过 {skipped} 个不像 Mesh 导出的文件。")
    return replaced


def run_clear_empty_mesh() -> None:
    print()
    print("清空成空 Mesh")
    print("说明: 不使用模板覆盖，而是在原 Mesh JSON 中清空顶点、索引、压缩网格、子网格等数据，保留 m_Name 和原文件结构。")
    print()
    target_root = prompt_path("要清空的 Mesh 目录", DEFAULT_DEST_ROOT)

    try:
        cleared = clear_mesh_files(target_root)
    except FileNotFoundError as exc:
        print(exc)
        return

    print(f"完成，已清空 {cleared} 个 Mesh 文件。")


def run_clear_empty_mesh_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="把指定目录下的 Mesh JSON 就地清空为空 Mesh，保留原文件结构和 m_Name。")
    parser.add_argument("target_dir", type=Path, help="要批量清空的 Mesh 目录")
    parser.add_argument("--all-files", action="store_true", help="允许扫描非 .json 文件；非 JSON Mesh dump 仍会跳过")
    args = parser.parse_args(argv)

    try:
        cleared = clear_mesh_files(args.target_dir, args.all_files)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    print(f"完成，已清空 {cleared} 个 Mesh 文件。")
    return 0


def run_replace_empty_mesh() -> None:
    print()
    print("快捷替换空 Mesh")
    print("说明: 用一个空 mesh 文件覆盖指定目录下的所有文件；文件名不变，文件内 m_Name 使用原文件的 m_Name。")
    print()
    target_root = prompt_path("要替换的目录", DEFAULT_DEST_ROOT)
    empty_mesh_file = Path(prompt_text("空 mesh 文件路径"))

    try:
        replaced = replace_with_empty_mesh(target_root, empty_mesh_file)
    except (FileNotFoundError, ValueError) as exc:
        print(exc)
        return

    print(f"完成，已替换 {replaced} 个文件。")


def run_replace_empty_mesh_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用空 mesh 文件替换指定目录下的所有文件，并保留原文件名和原 m_Name。")
    parser.add_argument("target_dir", type=Path, help="要批量替换的目录")
    parser.add_argument("empty_mesh_file", type=Path, help="作为模板的空 mesh 文件")
    parser.add_argument("--all-files", action="store_true", help="允许处理非 .json 文件；默认只处理 .json 并跳过 manifest.json")
    args = parser.parse_args(argv)

    try:
        replaced = replace_with_empty_mesh(args.target_dir, args.empty_mesh_file, args.all_files)
    except (FileNotFoundError, ValueError) as exc:
        print(exc)
        return 1

    print(f"完成，已替换 {replaced} 个文件。")
    return 0


def copy_matched_json_files(source_root: Path, dest_root: Path, needle: str) -> list[Path]:
    matched: list[Path] = []
    json_files = list(iter_monobehaviour_json_files(source_root))
    print(f"[查找] 仅扫描 MonoBehaviour JSON，文件数: {len(json_files)}")
    for index, json_path in enumerate(json_files, start=1):
        if index == 1 or index % 500 == 0 or index == len(json_files):
            try:
                display_path = json_path.relative_to(source_root)
            except ValueError:
                display_path = json_path
            print(f"[查找] 扫描进度: {index}/{len(json_files)}，当前命中: {len(matched)}，当前文件: {display_path}", flush=True)
        if not file_contains_text(json_path, needle):
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


def _ai_response_state(request_path: Path, response_path: Path) -> str:
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
        parsed = get_strategy(load_config()).parse_response(content)
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
    for index, request_path in enumerate(request_files, start=1):
        response_path = _default_response_path_for_ai_request(request_path)
        response_state = _ai_response_state(request_path, response_path)
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
    run_ai_translation_batch_tool(mode, request_path, response_path)


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


def run_clean_unsupported_ttf_chars() -> None:
    print()
    print("清理 trans.json 中模板 TTF 不支持的字符")
    print("说明: 逐个读取 translation_chars_missing_from_ttf.txt 中的字符，查找 trans.json 译文中包含该字符的条目。")
    print("      确认替换后，直接回车代表删除该字符；输入内容则统一替换为输入内容。")
    print()

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

    changed_total = 0
    for index, char in enumerate(missing_chars, start=1):
        matches = find_trans_values_containing_char(trans_data, char)
        if not matches:
            continue
        print()
        print(f"[清理字符] {index}/{len(missing_chars)} 当前字符: {repr(char)}，命中 {len(matches)} 条")
        for item_index, (source, translated) in enumerate(matches[:50], start=1):
            print(f"  {item_index}. key:   {format_preview_text(source)}")
            print(f"     value: {format_preview_text(translated)}")
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


def run_clean_blacklisted_records() -> None:
    cfg = load_config()
    print()
    print("清理 records.json 中当前黑名单字段")
    print("说明: 读取 config.json 的 string_field_blacklist，删除 records.json 中命中的记录。")
    print("      被删除记录对应的 source_text 如果不再被其它保留记录使用，也会从 trans.json 删除。")
    print()

    records_path = prompt_path("records.json", DEFAULT_RECORDS_JSON)
    trans_path = prompt_path("trans.json", DEFAULT_TRANS_JSON)
    try:
        records_data = json.loads(records_path.read_text(encoding="utf-8-sig"))
        trans_data = json.loads(trans_path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"[清理黑名单] 读取失败: {exc}")
        return
    if not isinstance(records_data, list):
        print(f"[清理黑名单] records.json 不是数组: {records_path}")
        return
    if not isinstance(trans_data, dict):
        print(f"[清理黑名单] trans.json 不是对象: {trans_path}")
        return

    kept_records: list[object] = []
    removed_records: list[dict] = []
    for item in records_data:
        if not isinstance(item, dict):
            kept_records.append(item)
            continue
        field = item.get("field")
        if isinstance(field, str) and _is_blacklisted_string_field(cfg, field):
            removed_records.append(item)
        else:
            kept_records.append(item)

    kept_source_texts = {
        item.get("source_text")
        for item in kept_records
        if isinstance(item, dict) and isinstance(item.get("source_text"), str)
    }
    removed_source_texts = {
        item.get("source_text")
        for item in removed_records
        if isinstance(item.get("source_text"), str)
    }
    trans_keys_to_remove = sorted(
        text
        for text in removed_source_texts
        if text not in kept_source_texts and text in trans_data
    )

    print(f"[清理黑名单] records 原记录: {len(records_data)}")
    print(f"[清理黑名单] 命中黑名单记录: {len(removed_records)}")
    print(f"[清理黑名单] records 保留记录: {len(kept_records)}")
    print(f"[清理黑名单] trans 将删除键值: {len(trans_keys_to_remove)}")
    if removed_records:
        field_counts: dict[str, int] = {}
        for item in removed_records:
            field = str(item.get("field", ""))
            field_counts[field] = field_counts.get(field, 0) + 1
        print("[清理黑名单] 命中字段统计:")
        for field, count in sorted(field_counts.items(), key=lambda row: (-row[1], row[0]))[:50]:
            print(f"  {field}: {count}")
        if len(field_counts) > 50:
            print(f"  ... 其余字段 {len(field_counts) - 50} 个省略")

    confirm = prompt_input("确认写回 records.json 并清理 trans.json ? 输入 y 确认，其它任意键取消: ").strip().lower()
    if confirm != "y":
        print("[清理黑名单] 已取消，未修改文件。")
        return

    for key in trans_keys_to_remove:
        trans_data.pop(key, None)

    report = {
        "records_path": str(records_path),
        "trans_path": str(trans_path),
        "original_records": len(records_data),
        "removed_records": len(removed_records),
        "kept_records": len(kept_records),
        "removed_trans_keys": trans_keys_to_remove,
        "removed_record_samples": removed_records[:200],
    }
    report_path = records_path.with_name("blacklisted_records_cleanup_report.json")

    atomic_write_json_file(records_path, kept_records)
    atomic_write_json_file(trans_path, trans_data)
    atomic_write_json_file(report_path, report)
    print(f"[清理黑名单] 已写回 records.json: {records_path}")
    print(f"[清理黑名单] 已写回 trans.json: {trans_path}")
    print(f"[清理黑名单] 清理报告: {report_path}")


def run_sync_generated_tmp_material_parameters() -> None:
    cfg = load_config()
    print()
    print("同步生成字体的 SDF 材质参数")
    print("说明: 主流程默认保留原游戏材质。本工具只在 SDF/ToImport 中新增 Material 替换 JSON。")
    print("      保留原游戏材质的 PathID、Shader、纹理、颜色和遮罩设置，只同步影响 SDF 边缘的数值参数。")
    print("      重新执行主流程的 SDF 待导入准备步骤，会清空本工具生成的材质替换。")
    print()
    confirm = prompt_input("确认同步生成材质参数到当前 SDF/ToImport ? 输入 y 确认，其它任意键取消: ").strip().lower()
    if confirm != "y":
        print("[TMP材质] 已取消，未修改文件。")
        return
    try:
        sync_generated_tmp_material_parameters(cfg)
    except Exception as exc:
        print(f"[TMP材质] 处理失败: {exc}")


def run_clean_all_text_effect_materials() -> None:
    cfg = load_config()
    print()
    print("清理全部 TMP 阴影/描边/发光材质")
    print("说明: 扫描 workspace/input 中具有 TMP SDF 专属参数且带效果参数的 Material JSON，")
    print("      将对应替换 JSON 写入 workspace/output/Text；本工具只处理材质，不处理组件。")
    print("      高风险: 这会修改所有共享 TMP 字体材质，可能影响教程和运行时 UI。")
    print("      主流程不会自动执行本功能，仅用于手工测试。")
    print()
    confirm = prompt_input("确认执行全部材质清理? 输入 y 确认，其它任意键取消: ").strip().lower()
    if confirm != "y":
        print("[材质阴影描边] 已取消，未修改文件。")
        return
    try:
        disable_translated_text_effect_components(
            cfg,
            force_all_text_effect_materials=True,
            material_only=True,
        )
    except Exception as exc:
        print(f"[材质阴影描边] 处理失败: {exc}")


def run_parse_catalog() -> None:
    cfg = load_config()
    print()
    print("解析 Addressables catalog")
    print("说明: 从配置的 catalog_source_subpath 读取 catalog.json，输出格式化版和四个字段展开版。")
    print(f"[catalog] 源文件: {cfg.catalog_source_path}")
    print(f"[catalog] 输出目录: {cfg.result_dir / 'catalog'}")
    try:
        formatted_path, expanded_path = parse_catalog_to_output(cfg)
    except Exception as exc:
        print(f"[catalog] 解析失败: {exc}")
        return
    print(f"[catalog] 已输出格式化 catalog: {formatted_path}")
    print(f"[catalog] 已输出展开解析结果: {expanded_path}")
    print(f"[catalog] 解析报告: {expanded_path.with_name('catalog_parse_report.txt')}")


def run_repack_catalog() -> None:
    cfg = load_config()
    default_output_json = cfg.result_dir / "catalog" / "Output.json"
    default_destination = cfg.result_dir / "catalog" / "catalog.repacked.json"
    print()
    print("回打 Addressables catalog")
    print("说明: 读取展开后的 Output.json，将四个字段重新编码回原始 catalog 格式。")
    output_json = prompt_path("展开后的 Output.json", default_output_json)
    destination = prompt_path("输出 catalog 文件", default_destination)
    try:
        result_path = repack_expanded_catalog(output_json, destination)
    except Exception as exc:
        print(f"[catalog] 回打失败: {exc}")
        return
    print(f"[catalog] 已生成可导入 catalog: {result_path}")


def run_auto_patch_catalog() -> None:
    cfg = load_config()
    final_result_root = cfg.root_dir / "workspace" / "FinalResult"
    log_paths = sorted(cfg.log_dir.glob("*.log")) + sorted(cfg.log_dir.glob("*.txt"))
    print()
    print("按最终 Bundle 自动修正并回打 catalog")
    print("说明: 解析原 catalog，匹配 FinalResult/Bundle/Android 下的 bundle，修正 size，并将命中条目的 m_Crc 置为 0。")
    print(f"[catalog] 最终输出目录: {final_result_root}")
    try:
        bundle_root = final_result_root / "Bundle"
        android_root = bundle_root / "Android"
        if bundle_root.is_dir():
            move_sources = [
                path for path in bundle_root.iterdir()
                if path.name != "Android" and path.name.lower() != "catalog.json"
            ]
            if move_sources:
                android_root.mkdir(parents=True, exist_ok=True)
                for source in move_sources:
                    target = android_root / source.name
                    if target.exists():
                        if target.is_dir():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    shutil.move(str(source), str(target))
                print(f"[catalog] 已整理 Bundle 输出目录: {android_root}")
        result_path = auto_patch_and_repack_catalog_after_import(cfg, final_result_root, log_paths)
    except Exception as exc:
        print(f"[catalog] 自动修正失败: {exc}")
        return
    if result_path is None:
        return
    print(f"[catalog] 自动修正完成: {result_path}")


def _catalog_row_original_runtime(row: dict):
    raw = row.get("raw")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        raw_obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return raw_obj if isinstance(raw_obj, dict) else {}


def run_patch_catalog_real_crc_interactive() -> None:
    cfg = load_config()
    final_result_root = cfg.root_dir / "workspace" / "FinalResult"
    output_dir = cfg.result_dir / "catalog"
    output_json = output_dir / "Output.json"
    final_catalog_path = final_result_root / "Bundle" / "catalog.json"
    bundle_root = final_result_root / "Bundle" / "Android"
    source_bundle_root = _source_catalog_android_root(cfg)
    log_paths = sorted(cfg.log_dir.glob("*.log")) + sorted(cfg.log_dir.glob("*.txt"))

    print()
    print("按最终 Bundle 真实 CRC 修正 catalog（交互处理长度溢出）")
    print("说明: 先用新 bundle 文件名匹配原 bundle，再用原 bundle 的 CRC/size 定位 Output.json 条目。")
    print("说明: 能原位写入的条目写真实 CRC；真实 CRC 导致片段变长时，询问置 0 或跳过该条。")
    print(f"[catalog] 最终 bundle 目录: {bundle_root}")

    try:
        _formatted_path, expanded_path = parse_catalog_to_output(cfg, cfg.catalog_source_path, output_dir)
        if not validate_catalog_crc_algorithm(cfg, expanded_path, source_bundle_root, bundle_root, output_dir):
            print("[catalog][停止] CRC 算法自校验未通过，本次不修改 catalog。")
            return

        manual_crc = calculate_final_bundle_crcs_manually(bundle_root)
        size_updates, crc_updates, backup_path = patch_expanded_catalog_from_final_bundles(
            output_json,
            bundle_root,
            log_paths=log_paths,
            crc_by_bundle_name=manual_crc,
            source_bundle_root=source_bundle_root,
        )

        catalog = json.loads(output_json.read_text(encoding="utf-8-sig"))
        options = catalog.get("m_ExtraDataString", {}).get("AssetBundleRequestOptions", [])
        overflow_rows = []
        if isinstance(options, list):
            for row in options:
                if not isinstance(row, dict):
                    continue
                raw = row.get("raw")
                if not isinstance(raw, str):
                    continue
                rebuilt = _rebuild_request_option_raw(row)
                if len(rebuilt) > len(raw):
                    overflow_rows.append(row)

        if overflow_rows:
            print(f"[catalog][需要处理] 有 {len(overflow_rows)} 条真实 CRC/size 原位回打会变长。")
            changed = 0
            skipped = 0
            for index, row in enumerate(overflow_rows, 1):
                original_runtime = _catalog_row_original_runtime(row)
                print()
                print(
                    f"[catalog][{index}/{len(overflow_rows)}] "
                    f"hash={row.get('m_Hash')} real_crc={row.get('m_Crc')} size={row.get('m_BundleSize')}"
                )
                while True:
                    choice = prompt_input("输入 0 置 m_Crc=0 跳过校验；输入 s 跳过该条真实 CRC 替换: ").strip().lower()
                    if choice in {"0", ""}:
                        row["m_Crc"] = 0
                        changed += 1
                        break
                    if choice in {"s", "skip"}:
                        for key in (
                            "m_Hash",
                            "m_Crc",
                            "m_Timeout",
                            "m_ChunkedTransfer",
                            "m_RedirectLimit",
                            "m_RetryCount",
                            "m_BundleName",
                            "m_AssetLoadMode",
                            "m_BundleSize",
                            "m_UseCrcForCachedBundles",
                            "m_UseUWRForLocalBundles",
                            "m_ClearOtherCachedVersionsWhenLoaded",
                        ):
                            if key in original_runtime:
                                row[key] = original_runtime[key]
                        skipped += 1
                        break
                    print("请输入 0 或 s。")
            output_json.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"[catalog] 长度溢出处理完成: 置0={changed}, 跳过={skipped}")

        result_path = repack_expanded_catalog(output_json, final_catalog_path, crc_overflow="error")
    except Exception as exc:
        print(f"[catalog] 真实 CRC 修正失败: {exc}")
        return

    print(f"[catalog] 已按真实 CRC 修正 Output.json: size={size_updates}, crc={crc_updates}")
    print(f"[catalog] Output.json 自动修正前备份: {backup_path}")
    print(f"[catalog] 已回打 catalog 并输出到: {result_path}")


def main() -> int:
    if len(sys.argv) > 1:
        command = sys.argv[1].strip().lower()
        if command in {"clear-empty-mesh", "clear-mesh", "empty-mesh"}:
            return run_clear_empty_mesh_cli(sys.argv[2:])
        if command in {"replace-empty-mesh", "empty-mesh", "replace-mesh"}:
            return run_replace_empty_mesh_cli(sys.argv[2:])
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
        if command in {"clean-blacklisted-records", "clean-records-blacklist"}:
            run_clean_blacklisted_records()
            return 0
        if command in {"sync-generated-sdf-material", "sync-sdf-material"}:
            run_sync_generated_tmp_material_parameters()
            return 0
        if command in {"clean-all-text-effects", "clean-all-tmp-effects"}:
            run_clean_all_text_effect_materials()
            return 0
        if command in {"parse-catalog", "catalog"}:
            run_parse_catalog()
            return 0
        if command in {"repack-catalog", "pack-catalog"}:
            cfg = load_config()
            output_json = Path(sys.argv[2]) if len(sys.argv) >= 3 else cfg.result_dir / "catalog" / "Output.json"
            destination = Path(sys.argv[3]) if len(sys.argv) >= 4 else cfg.result_dir / "catalog" / "catalog.repacked.json"
            try:
                result_path = repack_expanded_catalog(output_json, destination)
            except Exception as exc:
                print(f"[catalog] 回打失败: {exc}")
                return 1
            print(f"[catalog] 已生成可导入 catalog: {result_path}")
            return 0
        if command in {"auto-patch-catalog", "patch-catalog"}:
            run_auto_patch_catalog()
            return 0
        if command in {"patch-catalog-real-crc", "real-crc-catalog"}:
            run_patch_catalog_real_crc_interactive()
            return 0
        print(f"未知命令: {sys.argv[1]}")
        print(f"用法: python {Path(__file__).name} clear-empty-mesh <要清空的Mesh目录>")
        print(f"或: python {Path(__file__).name} replace-empty-mesh <要替换的目录> <空mesh文件>")
        print(f"或: python {Path(__file__).name} resend-ai-batch <ai_translation_request_batch_XXX.json>")
        print(f"或: python {Path(__file__).name} clean-unsupported-ttf-chars")
        print(f"或: python {Path(__file__).name} clean-blacklisted-records")
        print(f"或: python {Path(__file__).name} sync-generated-sdf-material")
        print(f"或: python {Path(__file__).name} clean-all-text-effects")
        print(f"或: python {Path(__file__).name} parse-catalog")
        print(f"或: python {Path(__file__).name} repack-catalog [Output.json] [catalog.repacked.json]")
        print(f"或: python {Path(__file__).name} auto-patch-catalog")
        print(f"或: python {Path(__file__).name} patch-catalog-real-crc")
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
        print("7. 清空成空 Mesh")
        print("8. 快捷替换空 Mesh（旧方式）")
        print("9. AI 翻译单批补跑 / 修补 trans.json")
        print("10. 清理 trans.json 中模板 TTF 不支持的字符")
        print("11. 解析 Addressables catalog 到 workspace/output/catalog")
        print("12. 将 Output.json 回打成原始 catalog 格式")
        print("13. 按最终 Bundle 自动修正并回打 catalog（CRC 置 0）")
        print("14. 按最终 Bundle 真实 CRC 修正 catalog（长度溢出时询问）")
        print("15. 清理 records.json 中当前黑名单字段，并同步清理 trans.json")
        print("16. 同步生成字体的 SDF 材质参数到待导入目录（可选实验）")
        print("17. 清理全部 TMP 阴影/描边/发光材质")
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
            run_clear_empty_mesh()
            continue
        if choice == "8":
            run_replace_empty_mesh()
            continue
        if choice == "9":
            run_ai_translation_batch_menu()
            continue
        if choice == "10":
            run_clean_unsupported_ttf_chars()
            continue
        if choice == "15":
            run_clean_blacklisted_records()
            continue
        if choice == "16":
            run_sync_generated_tmp_material_parameters()
            continue
        if choice == "17":
            run_clean_all_text_effect_materials()
            continue
        if choice == "11":
            run_parse_catalog()
            continue
        if choice == "12":
            run_repack_catalog()
            continue
        if choice == "13":
            run_auto_patch_catalog()
            continue
        if choice == "14":
            run_patch_catalog_real_crc_interactive()
            continue
        if choice in {"q", "quit", "exit"}:
            return 0

        print("无效选择，请输入 1、2、3、4、5、6、7、8、9、10、11、12、13、14、15、16、17 或 q。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
