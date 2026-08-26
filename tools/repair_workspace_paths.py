from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_ROOT = SCRIPT_DIR.parent
WORKSPACE_PREFIX = "workspace"
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".tsv", ".csv"}
TOP_LEVEL_METADATA_DIRS = ("records", "resource_state")
EXPLICIT_METADATA_FILES = (
    Path("AllPNG") / "_allpng_map.json",
    Path("AllPNG") / "Sprite" / "_allsprite_map.json",
    Path("output") / "catalog" / "Output.json",
)


@dataclass
class RepairStats:
    workspaces: int = 0
    scanned_files: int = 0
    changed_files: int = 0
    replacements: int = 0
    stale_caches: int = 0
    restored_files: int = 0
    restored_bytes: int = 0
    missing_sources: int = 0


def find_workspaces(project_root: Path, selected: list[str] | None = None) -> list[Path]:
    selected_names = set(selected or [])
    result = []
    for path in project_root.iterdir():
        if not path.is_dir() or not path.name.startswith(WORKSPACE_PREFIX):
            continue
        suffix = path.name[len(WORKSPACE_PREFIX) :]
        if selected_names and path.name not in selected_names and suffix not in selected_names:
            continue
        result.append(path)
    return sorted(result, key=lambda value: value.name.casefold())


def metadata_files(workspace: Path):
    seen: set[Path] = set()
    for relative in EXPLICIT_METADATA_FILES:
        path = workspace / relative
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path
    for tree_name in TOP_LEVEL_METADATA_DIRS:
        tree = workspace / tree_name
        if not tree.is_dir():
            continue
        for path in tree.iterdir():
            if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path
    preview_tree = workspace / "preview"
    if preview_tree.is_dir():
        for path in preview_tree.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in TEXT_SUFFIXES:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path


def repair_text(
    text: str,
    *,
    project_root: Path,
    workspace: Path,
    workspace_names: list[str],
) -> tuple[str, int]:
    target_native = str(workspace.resolve())
    target_posix = workspace.resolve().as_posix()
    result = text
    total = 0
    del workspace_names  # The regex also catches stale workspace names no longer on disk.

    variants = (
        (
            re.compile(
                re.escape(str(project_root.resolve()))
                + r"\\workspace[^\\/\"\r\n]*\\",
                re.IGNORECASE,
            ),
            target_native + "\\",
        ),
        (
            re.compile(
                re.escape(str(project_root.resolve()).replace("\\", "\\\\"))
                + r"\\\\workspace[^\\/\"\r\n]*\\\\",
                re.IGNORECASE,
            ),
            target_native.replace("\\", "\\\\") + "\\\\",
        ),
        (
            re.compile(
                re.escape(project_root.resolve().as_posix())
                + r"/workspace[^\\/\"\r\n]*/",
                re.IGNORECASE,
            ),
            target_posix + "/",
        ),
    )
    for pattern, replacement in variants:
        def replace_match(match: re.Match[str]) -> str:
            nonlocal total
            if match.group(0).casefold() == replacement.casefold():
                return match.group(0)
            total += 1
            return replacement

        result = pattern.sub(replace_match, result)
    return result, total


def _read_utf8(path: Path) -> tuple[str, bool] | None:
    try:
        payload = path.read_bytes()
    except OSError:
        return None
    has_bom = payload.startswith(b"\xef\xbb\xbf")
    try:
        return payload.decode("utf-8-sig"), has_bom
    except UnicodeDecodeError:
        return None


def _atomic_write_utf8(path: Path, text: str, *, bom: bool) -> None:
    temp_path = path.with_name(f"{path.name}.workspace-path-fix.tmp")
    payload = text.encode("utf-8")
    if bom:
        payload = b"\xef\xbb\xbf" + payload
    try:
        temp_path.write_bytes(payload)
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _safe_staged_relative(value: object) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    relative = Path(value)
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        return None
    return relative


def restore_missing_staged_files(
    workspace: Path,
    *,
    apply: bool,
) -> tuple[int, int, int]:
    map_path = workspace / "resource_state" / "resource_source_map.json"
    if not map_path.is_file():
        return 0, 0, 0
    try:
        state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return 0, 0, 0
    entries = state.get("entries") if isinstance(state, dict) else None
    if not isinstance(entries, list):
        return 0, 0, 0

    configured_staging_root = (workspace / "input_sources").resolve()
    staging_value = state.get("staging_root")
    staging_root = (
        Path(staging_value).resolve()
        if isinstance(staging_value, str) and staging_value.strip()
        else configured_staging_root
    )
    try:
        staging_root.relative_to(workspace.resolve())
    except ValueError:
        # Never write through a stale map into another workspace.
        staging_root = configured_staging_root

    restored = 0
    restored_bytes = 0
    missing_sources = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = _safe_staged_relative(entry.get("staged_relative"))
        if relative is None:
            continue
        target = staging_root / relative
        if target.is_file():
            continue
        source_value = entry.get("source_path")
        source = Path(source_value) if isinstance(source_value, str) else None
        if source is None or not source.is_file():
            missing_sources += 1
            print(f"[源文件缺失] {workspace.name}: {source_value or '(未记录)'}")
            continue
        try:
            file_size = source.stat().st_size
        except OSError:
            missing_sources += 1
            print(f"[源文件无法读取] {workspace.name}: {source}")
            continue
        restored += 1
        restored_bytes += file_size
        if not apply:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = target.with_name(f"{target.name}.workspace-source-restore.tmp")
        try:
            shutil.copy2(source, temp_path)
            os.replace(temp_path, target)
        finally:
            temp_path.unlink(missing_ok=True)
        print(f"[原始资源恢复] {source} -> {target}")
    return restored, restored_bytes, missing_sources


def _cache_has_stale_workspace_path(
    cache_path: Path,
    *,
    project_root: Path,
    workspace: Path,
    workspace_names: list[str],
) -> bool:
    try:
        payload = cache_path.read_bytes()
    except OSError:
        return False
    del workspace_names
    target_name = workspace.name.casefold()
    root_native = re.escape(str(project_root.resolve()).encode("utf-8"))
    root_posix = re.escape(project_root.resolve().as_posix().encode("utf-8"))
    patterns = (
        re.compile(root_native + rb"\\(workspace[^\\/\x00\r\n]*)\\", re.IGNORECASE),
        re.compile(root_posix + rb"/(workspace[^\\/\x00\r\n]*)/", re.IGNORECASE),
    )
    for pattern in patterns:
        for match in pattern.finditer(payload):
            try:
                found_name = match.group(1).decode("utf-8").casefold()
            except UnicodeDecodeError:
                continue
            if found_name != target_name:
                return True
    return False


def repair_all(
    project_root: Path,
    *,
    apply: bool,
    selected: list[str] | None = None,
) -> RepairStats:
    project_root = project_root.resolve()
    workspaces = find_workspaces(project_root, selected)
    workspace_names = [path.name for path in workspaces]
    stats = RepairStats(workspaces=len(workspaces))

    for workspace in workspaces:
        workspace_changed = 0
        workspace_replacements = 0
        for path in metadata_files(workspace):
            stats.scanned_files += 1
            loaded = _read_utf8(path)
            if loaded is None:
                continue
            text, has_bom = loaded
            repaired, count = repair_text(
                text,
                project_root=project_root,
                workspace=workspace,
                workspace_names=workspace_names,
            )
            if not count:
                continue
            workspace_changed += 1
            workspace_replacements += count
            if apply:
                _atomic_write_utf8(path, repaired, bom=has_bom)

        cache_path = workspace / "records" / "object_graph_cache.pkl"
        stale_cache = cache_path.is_file() and _cache_has_stale_workspace_path(
            cache_path,
            project_root=project_root,
            workspace=workspace,
            workspace_names=workspace_names,
        )
        if stale_cache:
            stats.stale_caches += 1
            if apply:
                cache_path.unlink()

        restored, restored_bytes, missing_sources = restore_missing_staged_files(
            workspace,
            apply=apply,
        )
        stats.restored_files += restored
        stats.restored_bytes += restored_bytes
        stats.missing_sources += missing_sources

        stats.changed_files += workspace_changed
        stats.replacements += workspace_replacements
        action = "已修复" if apply else "待修复"
        cache_note = "，旧对象图缓存将重建" if stale_cache else ""
        print(
            f"[{action}] {workspace.name}: 文件={workspace_changed}, "
            f"路径={workspace_replacements}, 恢复暂存={restored}, "
            f"源文件也缺失={missing_sources}{cache_note}"
        )

    return stats


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "按每个 workspace* 的实际目录名修复内部元数据绝对路径，"
            "并从资源清单记录的原始路径补回缺失的 input_sources 文件。"
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=DEFAULT_PROJECT_ROOT,
        help="Translate 项目根目录，默认自动使用本脚本的上一级目录。",
    )
    parser.add_argument(
        "--workspace",
        action="append",
        help="只处理指定 workspace 目录名或 project_name；可重复传入。",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；不传时仅预览统计。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.root.is_dir():
        print(f"[停止] 项目根目录不存在: {args.root}")
        return 2
    stats = repair_all(args.root, apply=args.apply, selected=args.workspace)
    mode = "完成" if args.apply else "预览"
    print(
        f"[{mode}] workspace={stats.workspaces}, 扫描文件={stats.scanned_files}, "
        f"修改文件={stats.changed_files}, 替换路径={stats.replacements}, "
        f"失效对象图缓存={stats.stale_caches}, 恢复暂存={stats.restored_files}, "
        f"恢复大小={stats.restored_bytes / 1024 / 1024:.1f} MB, "
        f"源文件也缺失={stats.missing_sources}"
    )
    if not args.apply and (
        stats.changed_files or stats.stale_caches or stats.restored_files
    ):
        print("确认后执行同一命令并追加 --apply。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
