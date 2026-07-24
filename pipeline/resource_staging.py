from __future__ import annotations

import json
import re
import shutil
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from support.config import PipelineConfig
from .catalog_tools import parse_catalog_to_output
from .split_bundle import find_split_bundle_groups, merge_split_bundle_group


REMOTE_PLACEHOLDER_RE = re.compile(r"\{[^}]*RemoteLoadPath[^}]*\}", re.IGNORECASE)


def _log_blue(message: str) -> None:
    print(f"\033[94m{message}\033[0m", flush=True)


def _log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def _log_red(message: str) -> None:
    print(f"\033[91m{message}\033[0m", flush=True)


def _log_orange(message: str) -> None:
    print(f"\033[38;5;208m{message}\033[0m", flush=True)


def resource_state_root(cfg: PipelineConfig) -> Path:
    return cfg.root_dir / "workspace" / "resource_state"


def resource_source_map_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "resource_source_map.json"


def remote_resource_report_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "addressables_remote_resources.json"


def split_merge_report_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "split_bundle_merges.json"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _game_root(cfg: PipelineConfig) -> Path:
    catalog_path = cfg.catalog_source_path
    if len(catalog_path.parents) >= 3:
        return catalog_path.parents[2]
    return cfg.project_dir / "game-name" / "game"


def _addressables_android_root(cfg: PipelineConfig) -> Path:
    return cfg.catalog_source_path.parent / "Android"


def _addressables_root(cfg: PipelineConfig) -> Path:
    return cfg.catalog_source_path.parent


def _addressables_backup_root(cfg: PipelineConfig) -> Path:
    return _game_root(cfg).parent / "bak" / "aa_before_resource_export"


def _safe_relative_path(value: str) -> Path | None:
    normalized = urllib.parse.unquote(value).replace("\\", "/").strip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        return None
    return Path(*parts)


def _remote_relative_path(internal_id: str) -> Path | None:
    value = internal_id.strip()
    if value.lower().startswith(("http://", "https://")):
        path = urllib.parse.urlparse(value).path
        match = re.search(r"(?:^|/)Android/(.+)$", path, re.IGNORECASE)
        return _safe_relative_path(match.group(1) if match else Path(path).name)

    lowered = value.lower()
    if lowered.startswith("http/"):
        value = value[5:]
    elif lowered.startswith("https/"):
        value = value[6:]
    else:
        value = REMOTE_PLACEHOLDER_RE.sub("", value)

    value = value.lstrip("/")
    if value.lower().startswith("android/"):
        value = value[len("Android/"):]
    return _safe_relative_path(value)


def _is_remote_internal_id(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    return (
        lowered.startswith(("http://", "https://", "http/", "https/"))
        or REMOTE_PLACEHOLDER_RE.search(stripped) is not None
    )


@dataclass(frozen=True)
class RemoteDownload:
    internal_id: str
    url: str
    relative_path: Path
    destination: Path


def _build_remote_downloads(
    cfg: PipelineConfig,
    catalog: dict[str, Any],
) -> tuple[list[RemoteDownload], list[dict[str, str]]]:
    internal_ids = catalog.get("m_InternalIds")
    if not isinstance(internal_ids, list):
        return [], []

    android_root = _addressables_android_root(cfg)
    downloads: list[RemoteDownload] = []
    unresolved: list[dict[str, str]] = []
    seen_destinations: set[Path] = set()

    for raw_value in internal_ids:
        if not isinstance(raw_value, str) or not _is_remote_internal_id(raw_value):
            continue
        relative_path = _remote_relative_path(raw_value)
        if relative_path is None:
            unresolved.append({"internal_id": raw_value, "reason": "无法提取本地相对路径"})
            continue

        if not raw_value.lower().startswith(("http://", "https://")):
            unresolved.append({"internal_id": raw_value, "reason": "catalog 未提供完整 HTTP/HTTPS 下载链接"})
            continue
        destination = android_root / relative_path
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        url = raw_value

        destination_key = destination.resolve()
        if destination_key in seen_destinations:
            continue
        seen_destinations.add(destination_key)
        downloads.append(
            RemoteDownload(
                internal_id=raw_value,
                url=url,
                relative_path=relative_path,
                destination=destination,
            )
        )
    return downloads, unresolved


def _download_remote_file(task: RemoteDownload, timeout: int) -> tuple[bool, str]:
    task.destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = task.destination.with_name(task.destination.name + ".download")
    request = urllib.request.Request(
        task.url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Accept": "*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, temp_path.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if temp_path.stat().st_size <= 0:
            raise RuntimeError("下载结果为空")
        temp_path.replace(task.destination)
        return True, ""
    except Exception as exc:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        return False, str(exc)


def backup_game_addressables(cfg: PipelineConfig) -> Path | None:
    source_root = _addressables_root(cfg)
    if not source_root.is_dir():
        print(f"[Addressables备份] 未找到 assets/aa，跳过备份: {source_root}")
        return None

    backup_root = _addressables_backup_root(cfg)
    if backup_root.exists():
        shutil.rmtree(backup_root)
    backup_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, backup_root)
    _log_green(f"[Addressables备份] 已在操作前备份: {source_root} -> {backup_root}")
    return backup_root


def _local_internal_id(relative_path: Path) -> str:
    return (
        "{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/"
        + relative_path.as_posix()
    )


def _localize_downloaded_catalog_resources(
    cfg: PipelineConfig,
    catalog: dict[str, Any],
) -> list[dict[str, str]]:
    internal_ids = catalog.get("m_InternalIds")
    if not isinstance(internal_ids, list):
        return []

    changes: list[dict[str, str]] = []
    android_root = _addressables_android_root(cfg)
    for index, raw_value in enumerate(internal_ids):
        if not isinstance(raw_value, str) or not raw_value.lower().startswith(("http://", "https://")):
            continue
        relative_path = _remote_relative_path(raw_value)
        if relative_path is None:
            continue
        local_file = android_root / relative_path
        if not local_file.is_file() or local_file.stat().st_size <= 0:
            raise FileNotFoundError(f"远程资源尚未完整落地，不能本地化 catalog: {local_file}")
        local_id = _local_internal_id(relative_path)
        internal_ids[index] = local_id
        changes.append(
            {
                "index": str(index),
                "remote_internal_id": raw_value,
                "local_internal_id": local_id,
                "local_file": str(local_file),
            }
        )

    if not changes:
        return []

    catalog_path = cfg.catalog_source_path
    temp_path = catalog_path.with_name(catalog_path.name + ".localize.tmp")
    temp_path.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp_path.replace(catalog_path)
    return changes


def inspect_and_download_catalog_resources(cfg: PipelineConfig) -> bool:
    catalog_path = cfg.catalog_source_path
    if not catalog_path.is_file():
        print(f"[catalog] 未找到 catalog，跳过远程资源检查: {catalog_path}")
        return True

    print(f"[catalog] 导出前解析: {catalog_path}")
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[catalog][停止] catalog JSON 读取失败: {exc}")
        return False
    try:
        parse_catalog_to_output(cfg)
    except Exception as exc:
        print(f"[catalog][提示] catalog 四字段展开失败，仍继续检查 m_InternalIds: {exc}")

    internal_ids = catalog.get("m_InternalIds")
    remote_internal_ids = [
        value
        for value in internal_ids
        if isinstance(value, str) and _is_remote_internal_id(value)
    ] if isinstance(internal_ids, list) else []
    downloads, unresolved = _build_remote_downloads(cfg, catalog)
    report: dict[str, Any] = {
        "catalog": str(catalog_path),
        "android_destination": str(_addressables_android_root(cfg)),
        "remote_internal_id_count": len(remote_internal_ids),
        "remote_internal_ids": remote_internal_ids,
        "download_count": len(downloads),
        "unresolved_count": len(unresolved),
        "downloads": [
            {
                "internal_id": item.internal_id,
                "url": item.url,
                "relative_path": str(item.relative_path),
                "destination": str(item.destination),
            }
            for item in downloads
        ],
        "unresolved": unresolved,
    }
    report_path = remote_resource_report_path(cfg)
    _write_json(report_path, report)
    print(
        f"[catalog] 远程资源判断: m_InternalIds={len(internal_ids) if isinstance(internal_ids, list) else 0}, "
        f"远程={len(remote_internal_ids)}, 本地缺失待下载={len(downloads)}, 无法解析={len(unresolved)}"
    )

    if unresolved:
        _log_red(f"[catalog][停止] 发现 {len(unresolved)} 个远程资源，但 catalog 没有提供完整下载链接。")
        _log_red(f"[catalog][停止] 详情: {report_path}")
        return False

    failures: list[dict[str, str]] = []
    download_results: list[dict[str, Any]] = []
    if downloads:
        print(
            f"[catalog] 需要下载远程资源: {len(downloads)} 个 -> "
            f"{_addressables_android_root(cfg)}"
        )
        with ThreadPoolExecutor(max_workers=cfg.addressables_download_workers) as executor:
            futures = {
                executor.submit(_download_remote_file, task, cfg.addressables_download_timeout): task
                for task in downloads
            }
            for future in as_completed(futures):
                task = futures[future]
                ok, error = future.result()
                result_row = {
                    "success": ok,
                    "url": task.url,
                    "relative_path": str(task.relative_path),
                    "destination": str(task.destination),
                    "error": error,
                }
                download_results.append(result_row)
                if not ok:
                    failures.append(
                        {"url": task.url, "destination": str(task.destination), "error": error}
                    )

    report["download_results"] = sorted(
        download_results,
        key=lambda item: str(item.get("relative_path", "")),
    )
    report["failures"] = failures
    report["success_count"] = len(downloads) - len(failures)
    if failures:
        for failure in failures:
            _log_red(
                f"[catalog][下载失败] {failure['url']} -> "
                f"{failure['destination']}: {failure['error']}"
            )
        _write_json(report_path, report)
        _log_red(f"[catalog][停止] 有 {len(failures)} 个远程资源下载失败，详情: {report_path}")
        return False

    try:
        localized = _localize_downloaded_catalog_resources(cfg, catalog)
    except Exception as exc:
        report["catalog_localization_error"] = str(exc)
        _write_json(report_path, report)
        _log_red(f"[catalog][停止] 远程资源已下载，但 catalog 切换为本地加载失败: {exc}")
        return False

    report["localized_internal_ids"] = localized
    _write_json(report_path, report)
    if downloads:
        _log_green(f"[catalog] 远程资源全部下载成功: {len(downloads)} 个。")
        _log_orange(
            f"[源文件已修改] 已将 {len(downloads)} 个远程资源写入: "
            f"{_addressables_android_root(cfg)}"
        )
    elif remote_internal_ids:
        _log_green(f"[catalog] catalog 中的远程资源已全部存在于本地: {len(remote_internal_ids)} 个。")
    else:
        print("[catalog] 没有需要下载的远程资源。")
    if remote_internal_ids:
        _log_green(f"[catalog] 下载/本地资源保存目录: {_addressables_android_root(cfg)}")
    if localized:
        _log_orange(f"[源文件已修改] 已将 {len(localized)} 个远程 InternalId 改为本地 RuntimePath。")
        _log_orange(f"[源文件已修改] 已更新游戏 catalog: {catalog_path}")
        try:
            parse_catalog_to_output(cfg)
        except Exception as exc:
            print(f"[catalog][提示] 本地化后重新展开 catalog 失败: {exc}")
    return True


def _copy_source_tree(source_root: Path, destination_root: Path, skip_managed: bool = False) -> int:
    if not source_root.is_dir():
        return 0
    files = [
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and not (
            skip_managed
            and path.relative_to(source_root).parts
            and path.relative_to(source_root).parts[0].lower() == "managed"
        )
    ]
    total = len(files)
    for index, source_path in enumerate(files, start=1):
        relative = source_path.relative_to(source_root)
        target_path = destination_root / relative
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, target_path)
        if index == total or index % 500 == 0:
            print(f"[资源暂存] 复制进度: {index}/{total}", flush=True)
    return total


def _source_info_for_staged_path(
    cfg: PipelineConfig,
    staged_relative: Path,
) -> tuple[str, Path, Path]:
    parts = staged_relative.parts
    game_root = _game_root(cfg)
    if len(parts) >= 2 and parts[0].lower() == "aa" and parts[1].lower() == "android":
        relative = Path(*parts[2:])
        source_path = _addressables_android_root(cfg) / relative
        source_relative_game = Path("assets") / "aa" / "Android" / relative
        return "addressables_android", source_path, source_relative_game
    if len(parts) >= 2 and parts[0].lower() == "bin" and parts[1].lower() == "data":
        relative = Path(*parts[2:])
        source_path = cfg.resource_source_root / relative
        try:
            source_relative_game = source_path.relative_to(game_root)
        except ValueError:
            source_relative_game = Path("assets") / "bin" / "Data" / relative
        return "data", source_path, source_relative_game
    return "unknown", cfg.resource_staging_root / staged_relative, staged_relative


def prepare_unified_resource_source(cfg: PipelineConfig) -> Path | None:
    try:
        backup_game_addressables(cfg)
    except Exception as exc:
        _log_red(f"[Addressables备份][停止] assets/aa 备份失败: {exc}")
        return None
    if not inspect_and_download_catalog_resources(cfg):
        return None
    if not cfg.resource_source_root.is_dir():
        print(f"[资源暂存][停止] Data 资源目录不存在: {cfg.resource_source_root}")
        return None

    staging_root = cfg.resource_staging_root
    if staging_root.exists():
        print(f"[资源暂存] 导出前清空: {staging_root}")
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    android_root = _addressables_android_root(cfg)
    android_count = _copy_source_tree(android_root, staging_root / "aa" / "Android")
    data_count = _copy_source_tree(cfg.resource_source_root, staging_root / "bin" / "Data", skip_managed=True)
    print(f"[资源暂存] Addressables Android: {android_count} 个文件")
    print(f"[资源暂存] bin/Data（不含 Managed）: {data_count} 个文件")

    groups = find_split_bundle_groups(staging_root)
    invalid_groups = [group for group in groups if not group.is_contiguous_from_zero]
    if invalid_groups:
        print(f"[分卷][停止] 暂存区发现 {len(invalid_groups)} 组序号缺失的 split，无法自动合并。")
        for group in invalid_groups:
            print(f"[分卷][停止] {group.base_path}: 序号={group.part_indexes}")
        return None

    split_records: dict[str, dict[str, Any]] = {}
    for group in groups:
        staged_relative = group.base_path.relative_to(staging_root)
        category, source_base, source_relative_game = _source_info_for_staged_path(cfg, staged_relative)
        part_rows = []
        for part, size in zip(group.parts, (path.stat().st_size for path in group.parts)):
            source_part = source_base.with_name(part.name)
            try:
                source_part_relative_game = source_part.relative_to(_game_root(cfg))
            except ValueError:
                source_part_relative_game = source_relative_game.with_name(part.name)
            part_rows.append(
                {
                    "name": part.name,
                    "size": size,
                    "source_path": str(source_part),
                    "source_relative_game": str(source_part_relative_game),
                }
            )
        merge_split_bundle_group(group, overwrite=True, delete_parts=True)
        split_records[str(staged_relative)] = {
            "category": category,
            "staged_relative": str(staged_relative),
            "source_base_path": str(source_base),
            "source_relative_game": str(source_relative_game),
            "parts": part_rows,
        }
        _log_blue(
            f"[分卷] 已在暂存区自动合并: {staged_relative} <- "
            f"{len(part_rows)} 个 split"
        )

    entries: list[dict[str, Any]] = []
    for staged_path in sorted(path for path in staging_root.rglob("*") if path.is_file()):
        staged_relative = staged_path.relative_to(staging_root)
        category, source_path, source_relative_game = _source_info_for_staged_path(cfg, staged_relative)
        entry: dict[str, Any] = {
            "category": category,
            "staged_relative": str(staged_relative),
            "staged_path": str(staged_path),
            "source_path": str(source_path),
            "source_relative_game": str(source_relative_game),
        }
        split_record = split_records.get(str(staged_relative))
        if split_record is not None:
            entry["split_parts"] = split_record["parts"]
        entries.append(entry)

    state = {
        "staging_root": str(staging_root),
        "data_source_root": str(cfg.resource_source_root),
        "addressables_android_root": str(android_root),
        "entries": entries,
        "split_group_count": len(split_records),
    }
    _write_json(resource_source_map_path(cfg), state)
    _write_json(split_merge_report_path(cfg), list(split_records.values()))
    print(f"[资源暂存] 统一资源目录: {staging_root}")
    print(f"[资源暂存] 路径映射: {resource_source_map_path(cfg)}")
    if split_records:
        _log_blue(f"[分卷] 自动合并记录: {split_merge_report_path(cfg)}")
    return staging_root


def load_prepared_resource_source(cfg: PipelineConfig) -> Path | None:
    map_path = resource_source_map_path(cfg)
    if not map_path.is_file():
        print(f"[资源暂存][停止] 未找到导出路径映射，请先执行一键导出: {map_path}")
        return None
    try:
        state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"[资源暂存][停止] 路径映射读取失败: {exc}")
        return None
    staging_value = state.get("staging_root") if isinstance(state, dict) else None
    staging_root = Path(staging_value) if isinstance(staging_value, str) else cfg.resource_staging_root
    if not staging_root.is_dir():
        print(f"[资源暂存][停止] 统一资源目录不存在，请重新执行一键导出: {staging_root}")
        return None
    return staging_root


def _final_path_for_entry(final_root: Path, entry: dict[str, Any]) -> Path:
    category = entry.get("category")
    staged_relative = Path(str(entry.get("staged_relative", "")))
    parts = staged_relative.parts
    if category == "addressables_android" and len(parts) >= 2:
        return final_root / "Bundle" / "Android" / Path(*parts[2:])
    if category == "data" and len(parts) >= 2:
        return final_root / "Data" / Path(*parts[2:])
    return final_root / staged_relative


def restore_imported_resource_paths(cfg: PipelineConfig, final_root: Path) -> dict[str, Path]:
    map_path = resource_source_map_path(cfg)
    state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    entries = state.get("entries") if isinstance(state, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"资源路径映射缺少 entries: {map_path}")

    restored: dict[str, Path] = {}
    moved = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        staged_relative = Path(str(entry.get("staged_relative", "")))
        candidates = [
            final_root / staged_relative,
            final_root / "Bundle" / "Android" / staged_relative,
        ]
        source_result = next((path for path in candidates if path.is_file()), None)
        if source_result is None:
            continue
        destination = _final_path_for_entry(final_root, entry)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_result.resolve() != destination.resolve():
            if destination.exists():
                destination.unlink()
            shutil.move(str(source_result), str(destination))
            moved += 1
        restored[str(staged_relative)] = destination

    for root in (final_root / "aa", final_root / "bin", final_root / "Bundle" / "Android" / "aa", final_root / "Bundle" / "Android" / "bin"):
        if not root.exists():
            continue
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            root.rmdir()
        except OSError:
            pass

    print(f"[导入路径] 已按原始来源整理修改结果: {moved} 个文件")
    print(f"[导入路径] Addressables: {final_root / 'Bundle' / 'Android'}")
    print(f"[导入路径] bin/Data: {final_root / 'Data'}")
    return restored


def _write_split_parts(merged_path: Path, part_rows: list[dict[str, Any]], output_root: Path) -> list[Path]:
    outputs: list[Path] = []
    with merged_path.open("rb") as source:
        for index, row in enumerate(part_rows):
            relative_value = row.get("source_relative_game")
            if not isinstance(relative_value, str) or not relative_value:
                continue
            destination = output_root / Path(relative_value)
            destination.parent.mkdir(parents=True, exist_ok=True)
            requested_size = int(row.get("size", 0) or 0)
            remaining_for_part = requested_size if index < len(part_rows) - 1 else None
            with destination.open("wb") as output:
                while remaining_for_part is None or remaining_for_part > 0:
                    chunk_size = 1024 * 1024 if remaining_for_part is None else min(1024 * 1024, remaining_for_part)
                    chunk = source.read(chunk_size)
                    if not chunk:
                        break
                    output.write(chunk)
                    if remaining_for_part is not None:
                        remaining_for_part -= len(chunk)
            outputs.append(destination)
    return outputs


def prepare_split_sync_outputs(
    cfg: PipelineConfig,
    final_root: Path,
    restored_paths: dict[str, Path],
) -> int:
    map_path = resource_source_map_path(cfg)
    state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    entries = state.get("entries") if isinstance(state, dict) else None
    if not isinstance(entries, list):
        return 0

    split_root = final_root / "SplitBundles"
    records: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        part_rows = entry.get("split_parts")
        staged_relative = str(entry.get("staged_relative", ""))
        modified_path = restored_paths.get(staged_relative)
        if not isinstance(part_rows, list) or not part_rows or modified_path is None or not modified_path.is_file():
            continue

        source_relative_game = Path(str(entry.get("source_relative_game", staged_relative)))
        merged_destination = split_root / "Merged" / source_relative_game
        merged_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(modified_path, merged_destination)
        part_outputs = _write_split_parts(modified_path, part_rows, split_root / "Parts")
        records.append(
            {
                "modified_resource": str(modified_path),
                "merged_output": str(merged_destination),
                "split_outputs": [str(path) for path in part_outputs],
            }
        )

    if not records:
        return 0
    report_path = split_root / "split_sync_report.json"
    _write_json(report_path, records)
    _log_blue(f"[分卷][需要同步] 已生成修改后分卷资源: {len(records)} 组")
    _log_blue(f"[分卷][需要同步] 合并文件: {split_root / 'Merged'}")
    _log_blue(f"[分卷][需要同步] 可直接替换的 split 文件: {split_root / 'Parts'}")
    _log_blue("[分卷][需要同步] 必须同步替换原 .splitN；否则程序会继续读取 split 缓存，而不是修改后的合并资源。")
    _log_blue(f"[分卷][需要同步] 报告: {report_path}")
    return len(records)


def print_final_addressables_sync_reminder(cfg: PipelineConfig, final_root: Path) -> None:
    report_path = remote_resource_report_path(cfg)
    if not report_path.is_file():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    success_count = report.get("success_count") if isinstance(report, dict) else 0
    if not isinstance(success_count, int) or isinstance(success_count, bool) or success_count <= 0:
        return

    source_aa = _addressables_root(cfg)
    expected_project_aa = (
        cfg.project_dir / "game-name" / "GAME_hongtu_P" / "assets" / "aa"
    )
    target_text = (
        str(expected_project_aa)
        if expected_project_aa.parent.is_dir()
        else "<实际项目目录>/assets/aa"
    )
    _log_green(f"[导入完成] 本次已下载并本地化 {success_count} 个 Addressables 资源，位于: {source_aa}")
    _log_green("[导入完成] 推荐替换顺序:")
    _log_green(f"[导入完成] 1. 先把 {source_aa} 同步到 {target_text}")
    _log_green(f"[导入完成] 2. 再用 {final_root} 中的修改资源覆盖实际项目对应文件")
