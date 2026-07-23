from __future__ import annotations

import json
import subprocess
import sys
import shutil
from pathlib import Path

from support.config import load_config
from pipeline.catalog_tools import auto_patch_and_repack_catalog_after_import
from pipeline.manifest_index import load_tmp_manifest_index, tmp_manifest_index_path
from pipeline.split_bundle import find_split_bundle_groups, merge_split_bundle_groups, print_split_bundle_report

# The Python wrapper that calls UnityResourceCLI.
PIPELINE_SCRIPT = Path(__file__).resolve().parent / "AssetPipeline_CLI" / "scripts" / "unity_resource_pipeline.py"


def workspace_temp_root(cfg) -> Path:
    return cfg.root_dir / "workspace" / "temp"


def clean_workspace_temp_root(cfg) -> None:
    temp_root = workspace_temp_root(cfg)
    if temp_root.exists():
        print(f"[临时目录] 导出前清空: {temp_root}", flush=True)
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)


def clean_import_temp_roots(cfg) -> None:
    temp_root = workspace_temp_root(cfg)
    for name in ("selected_import_overlay", "selected_import_work"):
        path = temp_root / name
        if path.exists():
            print(f"[临时目录] 导入前清空: {path}", flush=True)
            shutil.rmtree(path)


def prepare_managed_dlls(cfg) -> None:
    managed_root = cfg.resource_managed_root
    existing_dlls = sorted(managed_root.rglob("*.dll")) if managed_root.is_dir() else []
    if existing_dlls:
        print(f"[Managed] 已找到 DLL: {managed_root}，数量={len(existing_dlls)}")
        return

    dummy_root = cfg.il2cpp_dummydll_root
    if not dummy_root.is_dir():
        print(f"[Managed] 未找到 Managed DLL，也未找到 DummyDll，MonoBehaviour 可能只能导出基础字段。")
        print(f"[Managed] Managed: {managed_root}")
        print(f"[Managed] DummyDll: {dummy_root}")
        return

    source_dlls = sorted(dummy_root.rglob("*.dll"))
    if not source_dlls:
        print(f"[Managed] DummyDll 目录存在但没有 DLL，MonoBehaviour 可能只能导出基础字段: {dummy_root}")
        return

    managed_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source_path in source_dlls:
        relative = source_path.relative_to(dummy_root)
        target_path = managed_root / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied += 1

    print(f"[Managed] Managed 下没有 DLL，已从 DummyDll 自动复制: {copied} 个")
    print(f"[Managed] 来源: {dummy_root}")
    print(f"[Managed] 目标: {managed_root}")


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text: str) -> None:
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()


def run_pipeline(
    mode: str,
    source_root: Path,
    input_root: Path,
    managed_root: Path,
    log_path: Path,
    replacement_root: Path | None = None,
    result_root: Path | None = None,
) -> int:
    command = [
        sys.executable,
        str(PIPELINE_SCRIPT),
        str(source_root),
        "--work",
        str(input_root),
        "--managed",
        str(managed_root),
        "--mode",
        mode,
        "--dump-format",
        "json",
        "--image-format",
        "png",
        "--quality",
        "90",
    ]
    if replacement_root is not None:
        command.extend(["--replacement-root", str(replacement_root)])
    if result_root is not None:
        command.extend(["--result-root", str(result_root)])
    print()
    print("Running:")
    print(" ".join(command))
    print()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        command,
        cwd=str(PIPELINE_SCRIPT.parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    with log_path.open("w", encoding="utf-8") as log_file:
        tee = Tee(sys.stdout, log_file)
        for line in proc.stdout:
            tee.write(line)
    return proc.wait()


def clean_input_root(input_root: Path) -> None:
    if input_root.exists():
        shutil.rmtree(input_root)
    input_root.mkdir(parents=True, exist_ok=True)


def clean_result_root(result_root: Path) -> None:
    if result_root.exists():
        shutil.rmtree(result_root)
    result_root.mkdir(parents=True, exist_ok=True)


def prepare_split_bundles_before_export(source_root: Path) -> bool:
    groups = find_split_bundle_groups(source_root)
    if not groups:
        return True

    print()
    print_split_bundle_report(groups)
    invalid_groups = [group for group in groups if not group.is_contiguous_from_zero]
    if invalid_groups:
        print("[分卷] 存在序号缺失或不是从 split0 开始的分卷，已取消导出。")
        print("[分卷] 请先补齐分卷，或手动处理后再执行一键导出。")
        print()
        return False

    confirm = input("导出前发现分卷，是否自动合并到原资源目录并删除原 .splitN 分卷？输入 y 确认: ").strip().lower()
    if confirm != "y":
        print("[分卷] 已取消自动合并，也取消本次导出。")
        print()
        return False

    merged = merge_split_bundle_groups(groups, overwrite=True, delete_parts=True)
    print(f"[分卷] 合并完成: {merged}/{len(groups)} 组。")
    print()
    return True


def _path_key(path: Path) -> str:
    return str(path).replace("/", "\\")


def _sanitize_resource_name(value: str) -> str:
    if not value or not value.strip():
        return "unnamed"
    invalid = set('<>:"/\\|?*')
    sanitized = "".join("_" if char in invalid or ord(char) < 32 else char for char in value)
    return sanitized.strip() or "unnamed"


def _normalize_external_path(path_name: str) -> str:
    return path_name.replace("\\", "/").strip()


def _archive_external_name(normalized_path: str) -> str:
    lowered = normalized_path.lower()
    if not lowered.startswith("archive:/"):
        return ""
    archive_path = normalized_path.split(":/", 1)[1].strip("/")
    if not archive_path:
        return ""
    return Path(archive_path).name


def _asset_key_from_manifest_base(input_root: Path, manifest_dir: Path, relative_base: str) -> str:
    relative_base = _normalize_external_path(relative_base)
    base_path = manifest_dir
    if relative_base:
        base_path = base_path / Path(relative_base)
    return _path_key(base_path.relative_to(input_root))


def _resolve_external_asset_key(input_root: Path, manifest_dir: Path, path_name: str) -> str:
    normalized = _normalize_external_path(path_name)
    if not normalized:
        return ""

    manifest_relative_dir = manifest_dir.relative_to(input_root)
    candidates: list[Path] = []
    archive_name = _archive_external_name(normalized)
    if archive_name:
        candidates.append(manifest_dir / "bundle" / _sanitize_resource_name(archive_name))
        candidates.append(input_root / _sanitize_resource_name(archive_name))

    normalized_path = Path(normalized)
    if not archive_name:
        candidates.append(manifest_dir / "bundle" / _sanitize_resource_name(normalized_path.name))
        candidates.append(manifest_dir / normalized_path.with_suffix(""))
        candidates.append(input_root / normalized_path.with_suffix(""))
        candidates.append(input_root / normalized_path)

    if normalized.lower().startswith(("resources/", "library/")):
        parts = normalized.split("/")
        if len(parts) >= 2:
            candidates.append(input_root / "Resources" / parts[-1])

    for candidate in candidates:
        try:
            if candidate.is_dir() or (candidate / "manifest.json").is_file():
                return _path_key(candidate.relative_to(input_root))
        except (OSError, ValueError):
            continue

    if manifest_relative_dir:
        fallback_name = archive_name or normalized_path.name
        return _path_key(manifest_relative_dir / "bundle" / _sanitize_resource_name(fallback_name))
    fallback_name = archive_name or normalized_path.name
    return _path_key(Path("bundle") / _sanitize_resource_name(fallback_name))


def build_file_id_map(cfg) -> Path:
    input_root = cfg.resource_input_root
    file_id_map: dict[str, dict[str, object]] = {}
    for manifest_path in sorted(input_root.rglob("manifest.json")):
        manifest_dir = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        assets_files = manifest.get("AssetsFiles")
        if not isinstance(assets_files, list):
            continue
        for assets_file in assets_files:
            if not isinstance(assets_file, dict):
                continue
            relative_base = assets_file.get("RelativeBase", "")
            if not isinstance(relative_base, str):
                relative_base = ""
            asset_key = _asset_key_from_manifest_base(input_root, manifest_dir, relative_base)
            file_ids: dict[str, str] = {"0": asset_key}
            externals = assets_file.get("Externals", [])
            if isinstance(externals, list):
                for external in externals:
                    if not isinstance(external, dict):
                        continue
                    file_id = external.get("FileId")
                    path_name = external.get("PathName")
                    if not isinstance(file_id, int) or not isinstance(path_name, str):
                        continue
                    file_ids[str(file_id)] = _resolve_external_asset_key(input_root, manifest_dir, path_name)
            file_id_map[asset_key] = {
                "asset": asset_key,
                "source_relative_path": manifest.get("SourceRelativePath", ""),
                "source_kind": manifest.get("SourceKind", ""),
                "bundle_entry_name": assets_file.get("BundleEntryName", ""),
                "file_ids": file_ids,
                "externals": externals if isinstance(externals, list) else [],
            }

    cfg.stage_record_dir.mkdir(parents=True, exist_ok=True)
    output_path = cfg.stage_record_dir / cfg.output_file_id_map_json
    output_path.write_text(json.dumps(file_id_map, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[导出] FileID 映射已写入: {output_path}，资源文件数: {len(file_id_map)}")
    return output_path


def print_monobehaviour_export_summary(input_root: Path) -> None:
    total = 0
    custom = 0
    base_only = 0
    failed = 0
    manifest_count = 0
    for manifest_path in sorted(input_root.rglob("manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        summaries = manifest.get("MonoBehaviourSummaries")
        if not isinstance(summaries, list):
            continue
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            manifest_count += 1
            total += int(summary.get("Total", 0) or 0)
            custom += int(summary.get("WithCustomFields", 0) or 0)
            base_only += int(summary.get("BaseOnly", 0) or 0)
            failed += int(summary.get("Failed", 0) or 0)
    if total == 0:
        print("[导出] UnityResourceCLI MonoBehaviour 展开统计: 未发现 MonoBehaviour。")
        return
    print(
        f"[导出] UnityResourceCLI MonoBehaviour 展开统计: manifest={manifest_count}, total={total}, "
        f"custom={custom}, baseOnly={base_only}, failed={failed}"
    )
    if custom == 0:
        print("[导出] 提示: UnityResourceCLI 未展开业务字段；若 AssetStudio 回填成功，以实际 JSON 统计为准。")


def print_actual_monobehaviour_json_summary(input_root: Path, sample_limit: int = 20000) -> None:
    base_keys = {"m_GameObject", "m_Enabled", "m_Script", "m_Name"}
    total = 0
    base_only = 0
    custom = 0
    inspected_json = 0
    print(f"[导出] 开始抽样统计当前 MonoBehaviour JSON 实际字段，最多检查 {sample_limit} 个文件...", flush=True)
    for json_path in input_root.rglob("*.json"):
        if "MonoBehaviour" not in json_path.parts:
            continue
        if inspected_json >= sample_limit:
            break
        total += 1
        inspected_json += 1
        if inspected_json == 1 or inspected_json % 5000 == 0:
            print(
                f"[导出] MonoBehaviour JSON 实际字段统计进度: {inspected_json}，"
                f"custom={custom}, baseOnly={base_only}",
                flush=True,
            )
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(data, dict) and set(data.keys()).issubset(base_keys):
            base_only += 1
        else:
            custom += 1
    suffix = "，已达到抽样上限" if inspected_json >= sample_limit else ""
    print(f"[导出] 当前 MonoBehaviour JSON 实际字段抽样统计: checked={inspected_json}, custom={custom}, baseOnly={base_only}{suffix}", flush=True)
    if inspected_json and custom == 0:
        print("[导出] 警告: 当前 MonoBehaviour JSON 仍未展开业务字段，脚本 0 大概率不会命中文本。")


def print_import_options() -> None:
    print("导入内容选项:")
    print("  1. 文本替换 (workspace/output/Text)")
    print("  2. TMP/SDF 字体替换 (workspace/output/Font/SDF/ToImport)")
    print("  3. TTF 字体替换 (workspace/output/Font/TTF/ToImport)")
    print("  4. 图片替换 (workspace/output/Image/ToImport)")
    print("  5. 全部")
    print("  q. 取消")
    print()
    print("提示: 可以一次输入多个编号，用逗号分隔，例如 2,3,4。")
    print("      建议把需要同时导入的内容一次性选择，避免分开导入时互相覆盖。")
    print()


def _merge_tree(source_root: Path, destination_root: Path, label: str = "") -> int:
    if not source_root.is_dir():
        return 0

    files = [path for path in source_root.rglob("*") if path.is_file()]
    total = len(files)
    total_bytes = sum(path.stat().st_size for path in files)
    if label:
        print(f"[导入覆盖] 开始合并 {label}: {total} 个文件，{total_bytes / 1024 / 1024:.1f} MB")

    copied = 0
    skipped = 0
    copied_bytes = 0
    for source_path in files:
        target_path = destination_root / source_path.relative_to(source_root)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied += 1
        copied_bytes += source_path.stat().st_size
        if label and (copied == total or copied % 500 == 0):
            print(
                f"[导入覆盖] {label}: {copied}/{total} 个，"
                f"{copied_bytes / 1024 / 1024:.1f}/{total_bytes / 1024 / 1024:.1f} MB",
                flush=True,
            )
    return copied


ASSETSTUDIO_CHAR_FIELDS = {"m_AsteriskChar"}


def _wrap_assetstudio_json_arrays(value, parent_key: str = ""):
    if isinstance(value, list):
        wrapped_items = [_wrap_assetstudio_json_arrays(item) for item in value]
        if parent_key == "Array":
            return wrapped_items
        return {"Array": wrapped_items}
    if isinstance(value, dict):
        return {key: _wrap_assetstudio_json_arrays(child, str(key)) for key, child in value.items()}
    if parent_key in ASSETSTUDIO_CHAR_FIELDS and isinstance(value, str) and len(value) == 1:
        return ord(value)
    return value


def _normalize_monobehaviour_json_arrays_for_import(root: Path) -> int:
    fixed = 0
    for json_path in sorted(root.rglob("*.json")):
        if "MonoBehaviour" not in json_path.parts:
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        normalized = _wrap_assetstudio_json_arrays(data)
        if normalized == data:
            continue
        json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
        fixed += 1
    if fixed:
        print(f"[导入覆盖] 已兼容 AssetStudio MonoBehaviour 数组格式: {fixed} 个 JSON")
    return fixed


def _is_unsupported_json_import_source(path: Path) -> bool:
    if path.suffix.lower() != ".json":
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return (
        isinstance(data.get("m_LocaleId"), dict)
        and isinstance(data.get("m_SharedData"), dict)
        and isinstance(data.get("m_TableData"), dict)
    )


def _font_target_relative_paths(input_root: Path) -> list[Path]:
    manifest_paths = sorted(input_root.rglob("manifest.json"))
    targets: list[Path] = []
    for manifest_path in manifest_paths:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        items = manifest.get("Items", [])
        if not isinstance(items, list):
            continue

        manifest_dir = manifest_path.parent
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("TypeName") != "Font":
                continue
            relative_path = item.get("RelativePath")
            if not isinstance(relative_path, str) or not relative_path:
                continue
            targets.append((manifest_dir / Path(relative_path)).relative_to(input_root))
    return targets


def _manifest_item_value(item: dict, key: str, default=None):
    if key in item:
        return item[key]
    camel_key = key[:1].lower() + key[1:]
    return item.get(camel_key, default)


def _font_target_relative_paths_from_index(cfg) -> list[Path]:
    indexed_items = load_tmp_manifest_index(cfg)
    if indexed_items is None:
        return []

    targets: list[Path] = []
    for _manifest_path, manifest_dir, item in indexed_items:
        if _manifest_item_value(item, "TypeName") != "Font":
            continue
        relative_path = _manifest_item_value(item, "RelativePath")
        if not isinstance(relative_path, str) or not relative_path:
            continue
        try:
            targets.append((manifest_dir / Path(relative_path)).relative_to(cfg.resource_input_root))
        except ValueError:
            continue
    if targets:
        print(f"TTF 导入使用脚本0字体索引: {tmp_manifest_index_path(cfg)}，目标={len(targets)}")
    return targets


def _find_ttf_source_for_target(candidates: list[Path], target_relative_path: Path) -> Path | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    target_name = target_relative_path.stem.rsplit("_", 1)[0].lower()
    for candidate in candidates:
        if candidate.stem.lower() == target_name:
            return candidate
    return None


def _stage_ttf_replacements(cfg, destination_root: Path) -> int:
    if not cfg.ttf_new_dir.is_dir():
        return 0

    copied = 0
    missing_targets: list[Path] = []
    target_relative_paths = _font_target_relative_paths_from_index(cfg) or _font_target_relative_paths(cfg.resource_input_root)
    if not target_relative_paths:
        print("未找到 TTF Font 目标，请先执行菜单0生成索引，或确认 workspace/input 中存在 Font manifest。")
        return 0

    for target_relative_path in target_relative_paths:
        source_path = cfg.ttf_new_dir / target_relative_path
        if source_path.is_file():
            target_path = destination_root / target_relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied += 1
        else:
            missing_targets.append(target_relative_path)

    if copied:
        for target_relative_path in missing_targets:
            print(f"未找到精确路径的 TTF 替换源，跳过: {target_relative_path}")
        return copied

    font_candidates = sorted(
        path for path in cfg.ttf_new_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in {".ttf", ".otf"}
    )
    if not font_candidates:
        return 0

    for target_relative_path in target_relative_paths:
        source_path = _find_ttf_source_for_target(font_candidates, target_relative_path)
        if source_path is None:
            print(f"未找到匹配的 TTF 替换源，跳过: {target_relative_path}")
            continue
        target_path = destination_root / target_relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        copied += 1
    return copied


def build_import_overlay(cfg, selection: set[str]) -> Path | None:
    overlay_root = cfg.root_dir / "workspace" / "temp" / "selected_import_overlay"
    if overlay_root.exists():
        shutil.rmtree(overlay_root)
    overlay_root.mkdir(parents=True, exist_ok=True)

    copied_counts: list[str] = []

    if "text" in selection:
        text_count = _merge_tree(cfg.stage_dir / "Text", overlay_root, "文本")
        copied_counts.append(f"文本={text_count}")

    if "tmp" in selection:
        tmp_count = _merge_tree(cfg.import_overlay_dir, overlay_root, "TMP")
        copied_counts.append(f"TMP={tmp_count}")

    if "ttf" in selection:
        ttf_count = _stage_ttf_replacements(cfg, overlay_root)
        copied_counts.append(f"TTF={ttf_count}")

    if "image" in selection:
        image_count = _merge_tree(cfg.image_import_dir, overlay_root, "图片")
        copied_counts.append(f"图片={image_count}")

    total_files = sum(1 for _ in overlay_root.rglob("*") if _.is_file())
    if total_files == 0:
        shutil.rmtree(overlay_root)
        return None

    _normalize_monobehaviour_json_arrays_for_import(overlay_root)

    print(f"已构建临时导入覆盖层: {overlay_root}")
    print(f"包含文件: {', '.join(copied_counts)}")
    print()
    return overlay_root


def _has_files(root: Path) -> bool:
    return root.is_dir() and any(path.is_file() for path in root.rglob("*"))


def build_filtered_import_work_root(cfg, replacement_root: Path) -> Path | None:
    work_root = cfg.root_dir / "workspace" / "temp" / "selected_import_work"
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True, exist_ok=True)

    matched_count = 0
    for manifest_path in sorted(cfg.resource_input_root.rglob("manifest.json")):
        manifest_dir = manifest_path.parent
        try:
            manifest_relative_dir = manifest_dir.relative_to(cfg.resource_input_root)
        except ValueError:
            continue

        replacement_dir = replacement_root / manifest_relative_dir
        if not _has_files(replacement_dir):
            continue

        target_manifest_path = work_root / manifest_relative_dir / "manifest.json"
        target_manifest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest_path, target_manifest_path)
        matched_count += 1

    if matched_count == 0:
        shutil.rmtree(work_root)
        return None

    print(f"已构建临时导入索引: {work_root}")
    print(f"本次只导入 {matched_count} 个包含替换文件的资源 manifest。")
    print()
    return work_root


def normalize_final_bundle_android_layout(final_result_root: Path) -> None:
    bundle_root = final_result_root / "Bundle"
    android_root = bundle_root / "Android"
    if not bundle_root.is_dir():
        return
    move_sources = [
        path for path in bundle_root.iterdir()
        if path.name != "Android" and path.name.lower() != "catalog.json"
    ]
    if not move_sources:
        return
    android_root.mkdir(parents=True, exist_ok=True)
    for source in move_sources:
        target = android_root / source.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(source), str(target))
    print(f"已整理 Bundle 输出目录: {android_root}")


def prompt_import_selection() -> set[str] | None:
    print_import_options()
    raw = input("请选择要导入的内容，可输入多个编号并用逗号分隔: ").strip().lower()
    if raw in {"q", "quit", "exit"}:
        return None

    selected_codes = {part.strip() for part in raw.replace("，", ",").split(",") if part.strip()}
    if not selected_codes:
        return set()

    selection: set[str] = set()
    mapping = {
        "1": {"text"},
        "2": {"tmp"},
        "3": {"ttf"},
        "4": {"image"},
        "5": {"text", "tmp", "ttf", "image"},
    }
    for code in selected_codes:
        if code not in mapping:
            return set()
        selection.update(mapping[code])
    return selection


def print_menu() -> None:
    print("Font Generator Menu")
    print("1. 一键导出")
    print("2. 一键导入")
    print("q. 退出")
    print()
    print("说明:")
    print("  导出: 从 config.json 里的 project_root_dir + project_name 拼出资源目录")
    print("        导出前会检测 .splitN 分卷；确认后可自动合并到原资源目录再导出")
    print("        导出成功后会在 workspace\\records\\file_id_map.json 记录各资源文件的 FileID 外部依赖映射")
    print("  导入: 仍然从 project_root_dir + project_name 读取原始资源，并按选择读取文本、字体或图片替换结果")
    print("        导入后的文件会输出到 workspace\\FinalResult\\...，不会覆盖原始文件")
    print()


def main() -> int:
    cfg = load_config()
    source_root = cfg.resource_source_root
    input_root = cfg.resource_input_root
    managed_root = cfg.resource_managed_root
    replacement_root = cfg.import_overlay_dir
    import_result_root = cfg.root_dir / "workspace" / "FinalResult"
    log_dir = cfg.log_dir

    while True:
        print_menu()
        choice = input("请选择: ").strip().lower()
        if choice == "1":
            clean_workspace_temp_root(cfg)
            if not prepare_split_bundles_before_export(source_root):
                return 1
            confirm = input(f"导出前是否清空目标目录 {input_root} ? 输入 y 确认，其它任意键取消: ").strip().lower()
            if confirm == "y":
                print(f"正在清空: {input_root}")
                clean_input_root(input_root)
            else:
                print("已取消清空，继续保留现有文件。")
                print()
            prepare_managed_dlls(cfg)
            result = run_pipeline("export", source_root, input_root, managed_root, log_dir / "一键导出.log")
            if result == 0:
                build_file_id_map(cfg)
                print_monobehaviour_export_summary(input_root)
            return result
        if choice == "2":
            clean_import_temp_roots(cfg)
            selection = prompt_import_selection()
            if selection is None:
                print("已取消导入。")
                print()
                return 0
            if not selection:
                print("无效选择，请重新运行并输入 1、2、3、4、5 或 q。")
                print()
                return 1
            replacement_root = build_import_overlay(cfg, selection)
            if replacement_root is None:
                print("未找到可导入的替换文件，本次未执行导入。")
                print()
                return 1
            import_work_root = build_filtered_import_work_root(cfg, replacement_root)
            if import_work_root is None:
                print("替换文件没有匹配到任何已导出的 manifest，请检查 ToImport 目录结构是否和 workspace/input 一致。")
                print()
                return 1
            print(f"正在强制清空导入输出目录: {import_result_root}")
            clean_result_root(import_result_root)
            result = run_pipeline(
                "import",
                source_root,
                import_work_root,
                managed_root,
                log_dir / "一键导入.log",
                replacement_root,
                import_result_root,
            )
            if result == 0:
                normalize_final_bundle_android_layout(import_result_root)
                catalog_logs = sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.txt"))
                auto_patch_and_repack_catalog_after_import(cfg, import_result_root, catalog_logs)
            return result
        if choice in {"q", "quit", "exit"}:
            return 0
        print("无效选择，请输入 1、2 或 q。")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
