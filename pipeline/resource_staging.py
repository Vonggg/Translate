from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from support.config import PipelineConfig
from .catalog_tools import (
    parse_catalog_to_output,
    patch_and_repack_embedded_catalog_after_import,
)
from .obb_container import (
    ObbEntryMetadata,
    discover_obb_files,
    extract_obb_resources,
    write_obb_from_template,
)
from .split_bundle import find_split_bundle_groups, merge_split_bundle_group
from .resource_crypto import decrypt_staged, load_candidates, encrypt_results, settings_path
from tools.catalog_bin_tool import repack_binary_catalog_from_legacy_output


REMOTE_PLACEHOLDER_RE = re.compile(r"\{[^}]*RemoteLoadPath[^}]*\}", re.IGNORECASE)
RESOURCE_STAGING_STATE_VERSION = 6
OBB_STAGING_CONTENT_SUFFIX = ".contents"


def _log_blue(message: str) -> None:
    print(f"\033[94m{message}\033[0m", flush=True)


def _log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def _log_red(message: str) -> None:
    print(f"\033[91m{message}\033[0m", flush=True)


def _log_orange(message: str) -> None:
    print(f"\033[38;5;208m{message}\033[0m", flush=True)


def resource_state_root(cfg: PipelineConfig) -> Path:
    return cfg.workspace_root / "resource_state"


def resource_source_map_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "resource_source_map.json"


def remote_resource_report_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "addressables_remote_resources.json"


def split_merge_report_path(cfg: PipelineConfig) -> Path:
    return resource_state_root(cfg) / "split_bundle_merges.json"


def _resource_source_fingerprint(cfg: PipelineConfig) -> dict[str, Any]:
    """Cheap metadata fingerprint used to safely reuse the unified source tree."""
    digest = hashlib.sha256()
    file_count = 0
    total_size = 0
    latest_mtime_ns = 0

    def add_file(label: str, path: Path) -> None:
        nonlocal file_count, total_size, latest_mtime_ns
        try:
            stat = path.stat()
        except OSError:
            return
        file_count += 1
        total_size += stat.st_size
        latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        digest.update(label.replace("\\", "/").lower().encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")

    roots = (
        ("data", cfg.resource_source_root, True),
        ("android", _addressables_android_root(cfg), False),
        ("assetpack", _game_root(cfg) / "assets" / "assetpack", False),
    )
    for label, root, skip_managed in roots:
        if not root.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(root, topdown=True):
            dir_names.sort()
            if skip_managed:
                dir_names[:] = [name for name in dir_names if name.lower() != "managed"]
            current_path = Path(current_root)
            for file_name in sorted(file_names):
                path = current_path / file_name
                try:
                    relative = path.relative_to(root).as_posix()
                except ValueError:
                    relative = path.name
                add_file(f"{label}/{relative}", path)

    catalog_path = cfg.catalog_source_path
    add_file(f"catalog/{catalog_path.name}", catalog_path)
    catalog_hash_path = catalog_path.with_suffix(".hash")
    if catalog_hash_path != catalog_path:
        add_file(f"catalog/{catalog_hash_path.name}", catalog_hash_path)
    game_root = _game_root(cfg)
    digest.update(json.dumps({name: str(getattr(cfg, name, "")) for name in (
        "enable_ai_translation", "ai_translation_transport", "ai_translation_model",
        "ai_translation_codex_model", "ai_translation_base_url", "ai_translation_api_key",
    )}, sort_keys=True).encode())
    # Key discovery/configuration changes must invalidate plaintext staging.
    for path in (game_root / "AndroidManifest.xml", settings_path(cfg.workspace_root)):
        if path.is_file():
            digest.update(path.read_bytes())
    obb_root = game_root / "assets" / "obb"
    for obb_path in discover_obb_files(game_root):
        try:
            relative = obb_path.relative_to(obb_root).as_posix()
        except ValueError:
            relative = obb_path.name
        add_file(f"obb/{relative}", obb_path)
    return {
        "file_count": file_count,
        "total_size": total_size,
        "latest_mtime_ns": latest_mtime_ns,
        "fingerprint": digest.hexdigest(),
    }


def _reusable_staging_root(
    cfg: PipelineConfig,
    source_fingerprint: dict[str, Any],
) -> Path | None:
    state_path = resource_source_map_path(cfg)
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    if state.get("state_version") != RESOURCE_STAGING_STATE_VERSION:
        return None
    if state.get("source_fingerprint") != source_fingerprint:
        return None
    staging_root = _resolve_staging_root_from_state(cfg, state, state_path)
    if not staging_root.is_dir():
        return None
    entries = state.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        staged_relative = entry.get("staged_relative")
        if not isinstance(staged_relative, str) or not (staging_root / staged_relative).is_file():
            return None
    return staging_root


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_staging_root_from_state(
    cfg: PipelineConfig,
    state: dict[str, Any],
    state_path: Path | None = None,
) -> Path:
    """Resolve and, when safe, migrate an absolute staging path in old state.

    Resource maps written before project-specific workspaces contain absolute
    ``workspace/input_sources`` paths.  When the whole workspace is moved to
    ``workspace<project_name>``, the files move with it but those recorded
    paths do not. Prefer the configured staging directory when it exists and
    rebase every derived path by ``staged_relative``.
    """

    configured_root = cfg.resource_staging_root.resolve()
    staging_value = state.get("staging_root")
    recorded_root: Path | None = None
    if isinstance(staging_value, str) and staging_value.strip():
        recorded_root = Path(staging_value)
        if not recorded_root.is_absolute():
            recorded_root = cfg.root_dir / recorded_root
        recorded_root = recorded_root.resolve()

    workspace_root = cfg.workspace_root.resolve()
    recorded_in_current_workspace = False
    if recorded_root is not None:
        try:
            recorded_root.relative_to(workspace_root)
            recorded_in_current_workspace = True
        except ValueError:
            pass

    if configured_root.is_dir():
        staging_root = configured_root
    elif recorded_root is not None and (
        recorded_root == configured_root or recorded_in_current_workspace
    ):
        staging_root = recorded_root
    else:
        staging_root = configured_root

    if recorded_root == staging_root:
        return staging_root
    if staging_root != configured_root or not configured_root.is_dir():
        return staging_root

    state["staging_root"] = str(configured_root)
    entries = state.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            staged_relative = entry.get("staged_relative")
            if isinstance(staged_relative, str) and staged_relative:
                entry["staged_path"] = str(configured_root / staged_relative)
    if state_path is not None:
        _write_json(state_path, state)
    _log_blue(
        f"[资源暂存] 已将旧工作区路径映射迁移到当前项目: "
        f"{recorded_root or '(未记录)'} -> {configured_root}"
    )
    return staging_root


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


def _obb_root(cfg: PipelineConfig) -> Path:
    return _game_root(cfg) / "assets" / "obb"


def _obb_staging_prefix(obb_relative: Path) -> Path:
    return (
        Path("obb")
        / obb_relative.parent
        / f"{obb_relative.name}{OBB_STAGING_CONTENT_SUFFIX}"
    )


def _is_managed_obb_resource(relative: str) -> bool:
    parts = Path(relative).parts
    return (
        len(parts) >= 3
        and parts[0].casefold() == "bin"
        and parts[1].casefold() == "data"
        and parts[2].casefold() == "managed"
    )


def _obb_category(extracted_relative: str) -> str:
    parts = Path(extracted_relative).parts
    if len(parts) >= 2 and parts[0].casefold() == "aa":
        return "obb_addressables"
    if (
        len(parts) >= 2
        and parts[0].casefold() == "bin"
        and parts[1].casefold() == "data"
    ):
        return "obb_data"
    return "obb_resource"


def _obb_entry_record(
    cfg: PipelineConfig,
    obb_path: Path,
    obb_relative: Path,
    staging_prefix: Path,
    metadata: ObbEntryMetadata,
) -> dict[str, Any]:
    staged_relative = staging_prefix / Path(metadata.extracted_relative)
    return {
        "category": _obb_category(metadata.extracted_relative),
        "origin_kind": "obb",
        "staged_relative": str(staged_relative),
        "staged_path": str(cfg.resource_staging_root / staged_relative),
        "source_path": str(obb_path),
        "source_relative_game": str(Path("assets") / "obb" / obb_relative),
        "container_path": str(obb_path),
        "container_relative_assets_obb": obb_relative.as_posix(),
        "container_staging_prefix": str(staging_prefix),
        "archive_entry": metadata.entry_name,
        "original_file_size": metadata.file_size,
        "original_compressed_size": metadata.compressed_size,
        "original_crc32": metadata.crc32,
        "original_compress_type": metadata.compress_type,
    }


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
    *,
    modify_source_catalog: bool,
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
    output_path = cfg.result_dir / "catalog" / "Output.json"
    if catalog_path.suffix.lower() == ".bin":
        entry_field = catalog.get("m_EntryDataString")
        locations = entry_field.get("locations") if isinstance(entry_field, dict) else None
        if not isinstance(locations, list):
            raise RuntimeError(
                "binary Output.json 缺少 m_EntryDataString.locations"
            )
        replacements = {
            row["remote_internal_id"]: row["local_internal_id"]
            for row in changes
        }
        location_changes = 0
        for location in locations:
            if not isinstance(location, dict):
                continue
            current = location.get("InternalId")
            replacement = replacements.get(current)
            if replacement is None:
                continue
            location["InternalId"] = replacement
            location_changes += 1
        if location_changes == 0:
            raise RuntimeError(
                "binary catalog 中没有定位到需要本地化的 ResourceLocation"
            )

        output_temp = output_path.with_name(output_path.name + ".localize.tmp")
        output_temp.parent.mkdir(parents=True, exist_ok=True)
        output_temp.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            output_temp.replace(output_path)
        finally:
            if output_temp.exists():
                output_temp.unlink()

        if modify_source_catalog:
            catalog_temp = catalog_path.with_name(catalog_path.name + ".localize.tmp")
            hash_path = catalog_path.with_suffix(".hash")
            hash_temp = hash_path.with_name(hash_path.name + ".localize.tmp")
            try:
                repack_binary_catalog_from_legacy_output(
                    catalog_path,
                    output_path,
                    catalog_temp,
                    hash_temp,
                )
                catalog_temp.replace(catalog_path)
                hash_temp.replace(hash_path)
            finally:
                for temporary in (catalog_temp, hash_temp):
                    if temporary.exists():
                        temporary.unlink()
    else:
        if output_path.is_file():
            expanded = json.loads(output_path.read_text(encoding="utf-8-sig"))
            expanded_internal_ids = expanded.get("m_InternalIds")
            if isinstance(expanded_internal_ids, list):
                for change in changes:
                    index = int(change["index"])
                    if 0 <= index < len(expanded_internal_ids):
                        expanded_internal_ids[index] = change["local_internal_id"]
                _write_json(output_path, expanded)

        if modify_source_catalog:
            temp_path = catalog_path.with_name(catalog_path.name + ".localize.tmp")
            temp_path.write_text(
                json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temp_path.replace(catalog_path)
    return changes


def inspect_and_download_catalog_resources(cfg: PipelineConfig) -> bool:
    catalog_path = cfg.catalog_source_path
    if not catalog_path.is_file():
        report_path = remote_resource_report_path(cfg)
        if report_path.exists():
            report_path.unlink()
        print(f"[catalog] 未找到 catalog，跳过远程资源检查: {catalog_path}")
        return True

    print(f"[catalog] 导出前解析: {catalog_path}")
    if catalog_path.suffix.lower() == ".bin":
        try:
            _raw_path, output_path = parse_catalog_to_output(
                cfg,
                catalog_path,
                cfg.result_dir / "catalog",
            )
            catalog = json.loads(output_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            _log_red(f"[catalog.bin][停止] 二进制 catalog 解析失败: {exc}")
            return False
    else:
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
        localized = _localize_downloaded_catalog_resources(
            cfg,
            catalog,
            modify_source_catalog=bool(remote_internal_ids),
        )
    except Exception as exc:
        report["catalog_localization_error"] = str(exc)
        _write_json(report_path, report)
        _log_red(f"[catalog][停止] 远程资源已下载，但 catalog 切换为本地加载失败: {exc}")
        return False

    report["localized_internal_ids"] = localized
    report["localized_internal_id_count"] = len(localized)
    report["source_catalog_modified"] = bool(localized)
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
    if parts and parts[0].lower() == "assetpack":
        relative = Path(*parts[1:])
        source_relative_game = Path("assets") / "assetpack" / relative
        return "assetpack", game_root / source_relative_game, source_relative_game
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
    source_fingerprint = _resource_source_fingerprint(cfg)
    reusable_root = _reusable_staging_root(cfg, source_fingerprint)
    if reusable_root is not None:
        print(
            f"[资源暂存] 源资源未变化，复用统一资源目录: {reusable_root} "
            f"（文件={source_fingerprint['file_count']}）"
        )
        return reusable_root

    # Once reuse has been rejected, the old map must no longer authorize an
    # import.  Rebuilding can fail later (for example while opening a damaged
    # OBB); keeping the old map beside a partially replaced staging tree would
    # make that failed export look usable on the next import attempt.
    resource_source_map_path(cfg).unlink(missing_ok=True)
    split_merge_report_path(cfg).unlink(missing_ok=True)

    try:
        backup_game_addressables(cfg)
    except Exception as exc:
        _log_red(f"[Addressables备份][停止] assets/aa 备份失败: {exc}")
        return None
    if not inspect_and_download_catalog_resources(cfg):
        return None
    obb_paths = discover_obb_files(_game_root(cfg))
    assetpack_root = _game_root(cfg) / "assets" / "assetpack"
    if not cfg.resource_source_root.is_dir() and not obb_paths and not assetpack_root.is_dir():
        print(
            f"[资源暂存][停止] Data 资源目录不存在，assets/obb 下也没有 OBB: "
            f"{cfg.resource_source_root}，也未找到 assets/assetpack"
        )
        return None

    staging_root = cfg.resource_staging_root
    if staging_root.exists():
        print(f"[资源暂存] 导出前清空: {staging_root}")
        shutil.rmtree(staging_root)
    staging_root.mkdir(parents=True, exist_ok=True)

    android_root = _addressables_android_root(cfg)
    android_count = _copy_source_tree(android_root, staging_root / "aa" / "Android")
    data_count = _copy_source_tree(cfg.resource_source_root, staging_root / "bin" / "Data", skip_managed=True)
    assetpack_count = _copy_source_tree(assetpack_root, staging_root / "assetpack")
    print(f"[资源暂存] Addressables Android: {android_count} 个文件")
    print(f"[资源暂存] bin/Data（不含 Managed）: {data_count} 个文件")
    print(f"[资源暂存] PAD assets/assetpack: {assetpack_count} 个文件")

    obb_root = _obb_root(cfg)
    obb_entry_records: dict[str, dict[str, Any]] = {}
    obb_containers: list[dict[str, Any]] = []
    obb_resource_count = 0
    for obb_path in obb_paths:
        try:
            obb_relative = obb_path.relative_to(obb_root)
        except ValueError:
            _log_red(f"[OBB][停止] OBB 不在当前项目 assets/obb 下: {obb_path}")
            return None
        staging_prefix = _obb_staging_prefix(obb_relative)
        destination = staging_root / staging_prefix
        try:
            metadata_rows = extract_obb_resources(obb_path, destination)
        except Exception as exc:
            _log_red(f"[OBB][停止] 无法安全读取 OBB: {obb_path} ({exc})")
            return None

        kept_count = 0
        for metadata in metadata_rows:
            extracted_path = destination / Path(metadata.extracted_relative)
            if _is_managed_obb_resource(metadata.extracted_relative):
                extracted_path.unlink(missing_ok=True)
                continue
            record = _obb_entry_record(
                cfg,
                obb_path,
                obb_relative,
                staging_prefix,
                metadata,
            )
            obb_entry_records[record["staged_relative"]] = record
            kept_count += 1
        obb_resource_count += kept_count
        stat = obb_path.stat()
        obb_containers.append(
            {
                "container_path": str(obb_path),
                "container_relative_assets_obb": obb_relative.as_posix(),
                "container_staging_prefix": str(staging_prefix),
                "source_size": stat.st_size,
                "source_mtime_ns": stat.st_mtime_ns,
                "resource_entry_count": kept_count,
            }
        )
        _log_blue(
            f"[OBB] 已纳入统一暂存: {obb_relative.as_posix()}，"
            f"资源条目={kept_count}"
        )
    if obb_containers:
        _log_green(
            f"[OBB] 自动发现 {len(obb_containers)} 个 OBB，"
            f"已暂存资源条目={obb_resource_count}"
        )

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
        group_part_relatives = [str(path.relative_to(staging_root)) for path in group.parts]
        obb_part_records = [obb_entry_records.get(value) for value in group_part_relatives]
        is_obb_group = any(record is not None for record in obb_part_records)
        if is_obb_group:
            if any(record is None for record in obb_part_records):
                _log_red(f"[OBB][停止] split 组跨越 OBB/普通文件来源: {staged_relative}")
                return None
            typed_obb_records = [record for record in obb_part_records if record is not None]
            container_paths = {str(record.get("container_path", "")) for record in typed_obb_records}
            if len(container_paths) != 1:
                _log_red(f"[OBB][停止] split 组跨越多个 OBB: {staged_relative}")
                return None
            base_record = dict(typed_obb_records[0])
            category = str(base_record["category"])
            source_base = Path(str(base_record["container_path"]))
            source_relative_game = Path(str(base_record["source_relative_game"]))
        else:
            base_record = {}
            category, source_base, source_relative_game = _source_info_for_staged_path(cfg, staged_relative)
        part_rows = []
        for index, (part, size) in enumerate(
            zip(group.parts, (path.stat().st_size for path in group.parts))
        ):
            if is_obb_group:
                part_record = typed_obb_records[index]
                part_rows.append(
                    {
                        "name": part.name,
                        "size": size,
                        "source_path": str(part_record["container_path"]),
                        "source_relative_game": str(part_record["source_relative_game"]),
                        "archive_entry": str(part_record["archive_entry"]),
                        "original_crc32": int(part_record["original_crc32"]),
                        "original_file_size": int(part_record["original_file_size"]),
                    }
                )
            else:
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
        split_record = {
            "category": category,
            "staged_relative": str(staged_relative),
            "source_base_path": str(source_base),
            "source_relative_game": str(source_relative_game),
            "parts": part_rows,
        }
        if is_obb_group:
            split_record.update(
                {
                    key: base_record[key]
                    for key in (
                        "origin_kind",
                        "container_path",
                        "container_relative_assets_obb",
                        "container_staging_prefix",
                    )
                }
            )
            first_archive_entry = str(part_rows[0]["archive_entry"])
            split_record["archive_entry"] = re.sub(r"\.split\d+$", "", first_archive_entry)
        split_records[str(staged_relative)] = split_record
        _log_blue(
            f"[分卷] 已在暂存区自动合并: {staged_relative} <- "
            f"{len(part_rows)} 个 split"
        )

    crypto_candidates = load_candidates(_game_root(cfg), cfg.workspace_root)
    crypto_count = 0
    entries: list[dict[str, Any]] = []
    for staged_path in sorted(path for path in staging_root.rglob("*") if path.is_file()):
        staged_relative = staged_path.relative_to(staging_root)
        split_record = split_records.get(str(staged_relative))
        obb_record = obb_entry_records.get(str(staged_relative))
        if split_record is not None and split_record.get("origin_kind") == "obb":
            entry = dict(split_record)
            entry["staged_path"] = str(staged_path)
            entry["split_parts"] = split_record["parts"]
            entry.pop("parts", None)
        elif obb_record is not None:
            entry = dict(obb_record)
            entry["staged_path"] = str(staged_path)
        else:
            category, source_path, source_relative_game = _source_info_for_staged_path(cfg, staged_relative)
            entry = {
                "category": category,
                "origin_kind": "filesystem",
                "staged_relative": str(staged_relative),
                "staged_path": str(staged_path),
                "source_path": str(source_path),
                "source_relative_game": str(source_relative_game),
            }
            if split_record is not None:
                entry["split_parts"] = split_record["parts"]
        crypto = decrypt_staged(staged_path, crypto_candidates)
        if crypto is not None:
            entry["resource_crypto"] = crypto
            crypto_count += 1
        entries.append(entry)

    # AI is a separate evidence/key-candidate stage, only for unresolved resource
    # files. Known locally verified transforms need no network request.
    unresolved = []
    for entry in entries:
        path = Path(entry["staged_path"])
        if entry.get("resource_crypto") or path.suffix.lower() not in (".bundle", ".unity3d", ".assetbundle"):
            continue
        if path.stat().st_size < 32:
            continue
        with path.open("rb") as stream:
            if stream.read(8) in (b"UnityFS\0", b"UnityRaw", b"UnityWeb"):
                continue
        unresolved.append(path)
    if unresolved:
        try:
            from .resource_crypto_ai import discover_ai_candidates
            ai_candidates = discover_ai_candidates(cfg, unresolved)
        except Exception:
            print("[资源AI分析][跳过] 分析暂不可用，保留资源异常告警。")
            ai_candidates = []
        for entry in entries:
            path = Path(entry["staged_path"])
            if path not in unresolved or not ai_candidates:
                continue
            try:
                crypto = decrypt_staged(path, ai_candidates)
            except Exception:
                print(f"[资源AI分析][验证未通过] {entry['staged_relative']}；文件未变更，不采用 AI 建议。")
                continue
            if crypto:
                crypto["key_source"] = "ai_candidate_locally_validated"
                entry["resource_crypto"] = crypto
                crypto_count += 1

    state = {
        "state_version": RESOURCE_STAGING_STATE_VERSION,
        "source_fingerprint": _resource_source_fingerprint(cfg),
        "staging_root": str(staging_root),
        "data_source_root": str(cfg.resource_source_root),
        "addressables_android_root": str(android_root),
        "entries": entries,
        "split_group_count": len(split_records),
        "obb_containers": obb_containers,
        "obb_container_count": len(obb_containers),
        "obb_resource_entry_count": obb_resource_count,
    }
    _write_json(resource_source_map_path(cfg), state)
    if crypto_count:
        _log_green(f"[资源解密] 自动识别并解密 {crypto_count} 个资源包，完整解析及重新加密往返校验通过；原资源未修改。")
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
    if not isinstance(state, dict):
        print(f"[资源暂存][停止] 路径映射结构异常: {map_path}")
        return None
    staging_root = _resolve_staging_root_from_state(cfg, state, map_path)
    if not staging_root.is_dir():
        print(f"[资源暂存][停止] 统一资源目录不存在，请重新执行一键导出: {staging_root}")
        return None
    return staging_root


def _final_path_for_entry(final_root: Path, entry: dict[str, Any]) -> Path:
    category = entry.get("category")
    staged_relative = Path(str(entry.get("staged_relative", "")))
    parts = staged_relative.parts
    if category == "addressables_android" and len(parts) >= 2:
        return final_root / "aa" / "Android" / Path(*parts[2:])
    if category == "data" and len(parts) >= 2:
        return final_root / "Data" / Path(*parts[2:])
    if category == "assetpack" and parts:
        return final_root / "assetpack" / Path(*parts[1:])
    return final_root / staged_relative


def restore_imported_resource_paths(
    cfg: PipelineConfig,
    final_root: Path,
    import_result_root: Path | None = None,
) -> dict[str, Path]:
    map_path = resource_source_map_path(cfg)
    state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    entries = state.get("entries") if isinstance(state, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"资源路径映射缺少 entries: {map_path}")

    restored: dict[str, Path] = {}
    moved = 0
    raw_root = import_result_root or final_root
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        staged_relative = Path(str(entry.get("staged_relative", "")))
        source_result = raw_root / staged_relative
        if not source_result.is_file():
            source_result = None
        if source_result is None:
            continue
        if entry.get("origin_kind") == "obb":
            destination = source_result
        else:
            destination = _final_path_for_entry(final_root, entry)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source_result.resolve() != destination.resolve():
            if destination.exists():
                destination.unlink()
            shutil.move(str(source_result), str(destination))
            moved += 1
        restored[str(staged_relative)] = destination

    for root in (raw_root / "aa", raw_root / "bin"):
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
    print(f"[导入路径] Addressables: {final_root / 'aa'}")
    print(f"[导入路径] bin/Data: {final_root / 'Data'}")
    if any(entry.get("category") == "assetpack" for entry in entries if isinstance(entry, dict)):
        print(f"[导入路径] PAD: {final_root / 'assetpack'}（对应 assets/assetpack）")
    if any(entry.get("origin_kind") == "obb" for entry in entries if isinstance(entry, dict)):
        print(f"[导入路径] OBB 修改暂存: {raw_root / 'obb'}")
    return restored


def encrypt_imported_resource_paths(cfg: PipelineConfig, restored: dict[str, Path], *, catalog_ready: bool = True) -> int:
    state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8-sig"))
    if not catalog_ready and cfg.catalog_source_path.is_file() and any(
        entry.get("resource_crypto") and entry.get("category") == "addressables_android"
        and entry.get("staged_relative") in restored for entry in state["entries"]
    ):
        raise RuntimeError("加密资源的 catalog 校验/回写未成功，停止加密和渠道同步；当前结果不可直接导入。")
    return encrypt_results(state["entries"], restored)


def _write_split_parts_next_to_merged(
    merged_path: Path,
    part_rows: list[dict[str, Any]],
) -> list[Path]:
    first_part_name = Path(str(part_rows[0].get("name", ""))).name if part_rows else ""
    match = re.fullmatch(r"(.+\.split)\d+", first_part_name)
    if match is None:
        raise RuntimeError(f"无法识别原分卷文件名: {first_part_name or '<空>'}")
    part_prefix = match.group(1)
    original_sizes = [int(row.get("size", 0) or 0) for row in part_rows]
    if any(size <= 0 for size in original_sizes):
        raise RuntimeError(f"原分卷大小无效: {merged_path}")
    original_count = len(original_sizes)
    merged_size = merged_path.stat().st_size
    if merged_size < original_count:
        raise RuntimeError(
            f"修改后资源过小，无法在不生成空切片的情况下保持原分卷数量: "
            f"file={merged_path}, size={merged_size}, parts={original_count}"
        )

    # Unity split files are a logical stream divided at a fixed chunk boundary.
    # Every part except the final remainder must retain that boundary size;
    # redistributing the bytes evenly changes offset -> splitN mapping and can
    # leave Unity's Loading.AsyncRead thread spinning forever.
    fixed_size_rows = original_sizes[:-1] or original_sizes
    safe_part_limit = fixed_size_rows[0]
    if any(size != safe_part_limit for size in fixed_size_rows):
        raise RuntimeError(
            f"原分卷的非尾片大小不一致，无法安全推导 Unity 固定切片边界: "
            f"file={merged_path}, sizes={original_sizes}"
        )
    required_count = (merged_size + safe_part_limit - 1) // safe_part_limit
    output_count = max(original_count, required_count)

    final_size = merged_size - safe_part_limit * (output_count - 1)
    if final_size <= 0:
        # A large shrink can cross a split boundary. Keeping the old count would
        # require an empty tail, while reducing it may leave stale high-numbered
        # files in an overlay install. Stop instead of silently producing either
        # unsafe layout.
        raise RuntimeError(
            f"修改后资源已跨越原分卷边界，无法安全保持原分卷数量: "
            f"file={merged_path}, size={merged_size}, parts={original_count}, "
            f"chunk={safe_part_limit}"
        )
    if final_size > safe_part_limit:
        raise RuntimeError(
            f"尾分卷超过 Unity 固定切片大小: {final_size} > {safe_part_limit}"
        )
    target_sizes = [safe_part_limit] * (output_count - 1) + [final_size]

    available_space = shutil.disk_usage(merged_path.parent).free
    required_space = merged_size + 16 * 1024 * 1024
    if available_space < required_space:
        raise RuntimeError(
            f"安全切片临时空间不足: required={required_space}, available={available_space}, "
            f"path={merged_path.parent}"
        )

    source_hash = hashlib.sha256()
    with merged_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            source_hash.update(chunk)

    temp_outputs: list[Path] = []
    outputs: list[Path] = []
    try:
        with merged_path.open("rb") as source:
            for index, target_size in enumerate(target_sizes):
                destination = merged_path.with_name(f"{part_prefix}{index}")
                temporary = destination.with_name(destination.name + ".split_tmp")
                temporary.unlink(missing_ok=True)
                remaining = target_size
                with temporary.open("wb") as output:
                    while remaining:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise RuntimeError(f"切片时提前到达文件结尾: {merged_path}")
                        output.write(chunk)
                        remaining -= len(chunk)
                temp_outputs.append(temporary)
                outputs.append(destination)
            if source.read(1):
                raise RuntimeError(f"切片完成后仍有未写入数据: {merged_path}")

        merged_hash = hashlib.sha256()
        for temporary in temp_outputs:
            with temporary.open("rb") as part:
                while chunk := part.read(1024 * 1024):
                    merged_hash.update(chunk)
        if merged_hash.digest() != source_hash.digest():
            raise RuntimeError(f"切片重新合并 SHA-256 校验失败: {merged_path}")

        for temporary, destination in zip(temp_outputs, outputs):
            temporary.replace(destination)
        for stale_part in merged_path.parent.glob(f"{part_prefix}*"):
            suffix = stale_part.name[len(part_prefix):]
            if stale_part.is_file() and suffix.isdigit() and int(suffix) >= output_count:
                stale_part.unlink()
        return outputs
    finally:
        for temporary in temp_outputs:
            temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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

    records: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        part_rows = entry.get("split_parts")
        staged_relative = str(entry.get("staged_relative", ""))
        modified_path = restored_paths.get(staged_relative)
        if not isinstance(part_rows, list) or not part_rows or modified_path is None or not modified_path.is_file():
            continue

        merged_sha256 = _sha256_file(modified_path)
        part_outputs = _write_split_parts_next_to_merged(modified_path, part_rows)
        if (
            not part_outputs
            or sum(path.stat().st_size for path in part_outputs)
            != modified_path.stat().st_size
        ):
            for part_output in part_outputs:
                part_output.unlink(missing_ok=True)
            raise RuntimeError(f"分卷输出大小校验失败，已保留合并资源: {modified_path}")
        modified_path.unlink()
        original_part_count = len(part_rows)
        added_outputs = part_outputs[original_part_count:]
        if added_outputs:
            _log_blue(
                f"[分卷][新增] 修改后资源超过原分卷容量，已新增 {len(added_outputs)} 个切片；"
                "最终打包时必须把这些新文件一并加入，不能只替换已有条目。"
            )
            for added_output in added_outputs:
                _log_blue(f"[分卷][新增] {added_output}")
        records.append(
            {
                "removed_merged_resource": str(modified_path),
                "split_outputs": [str(path) for path in part_outputs],
                "original_part_count": original_part_count,
                "output_part_count": len(part_outputs),
                "added_split_outputs": [str(path) for path in added_outputs],
                "original_part_sizes": [int(row.get("size", 0) or 0) for row in part_rows],
                "output_part_sizes": [path.stat().st_size for path in part_outputs],
                "remerged_sha256": merged_sha256,
            }
        )

    legacy_split_root = final_root / "SplitBundles"
    if legacy_split_root.exists():
        shutil.rmtree(legacy_split_root)
    if not records:
        return 0
    report_path = resource_state_root(cfg) / "final_split_outputs.json"
    _write_json(report_path, records)
    _log_blue(f"[分卷][完成] 已将 FinalResult 中 {len(records)} 个合并资源替换为原布局 split 文件")
    _log_blue("[分卷][完成] FinalResult 可直接按目录覆盖，不再保留 SplitBundles 和合并版资源")
    _log_blue(f"[分卷][完成] 输出记录: {report_path}")
    return len(records)


def patch_obb_catalogs_after_import(
    cfg: PipelineConfig,
    import_result_root: Path,
    log_paths: list[Path] | tuple[Path, ...] = (),
) -> int:
    """Patch Addressables catalog metadata inside each modified OBB domain."""

    state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8-sig"))
    containers = state.get("obb_containers") if isinstance(state, dict) else None
    if not isinstance(containers, list):
        return 0
    staging_root = _resolve_staging_root_from_state(
        cfg,
        state,
        resource_source_map_path(cfg),
    )
    patched = 0
    for container in containers:
        if not isinstance(container, dict):
            continue
        relative_text = str(container.get("container_relative_assets_obb", ""))
        prefix_text = str(container.get("container_staging_prefix", ""))
        if not relative_text or not prefix_text:
            continue
        relative = Path(relative_text)
        prefix = Path(prefix_text)
        source_work_root = staging_root / prefix
        final_work_root = import_result_root / prefix
        final_bundle_root = final_work_root / "aa" / "Android"
        if not final_bundle_root.is_dir() or not any(final_bundle_root.rglob("*.bundle")):
            continue

        catalog_source = next(
            (
                path
                for path in (
                    source_work_root / "aa" / "catalog.bin",
                    source_work_root / "aa" / "catalog.json",
                )
                if path.is_file()
            ),
            None,
        )
        if catalog_source is None:
            _log_blue(
                f"[OBB catalog] 修改了 bundle，但容器内没有 catalog，跳过: "
                f"{relative.as_posix()}"
            )
            continue
        output_dir = (
            cfg.result_dir
            / "catalog"
            / "obb"
            / relative.parent
            / f"{relative.name}{OBB_STAGING_CONTENT_SUFFIX}"
        )
        generated = patch_and_repack_embedded_catalog_after_import(
            cfg,
            catalog_source,
            source_work_root / "aa" / "Android",
            final_bundle_root,
            output_dir,
            final_work_root / "aa",
            log_paths=log_paths,
        )
        if generated:
            patched += 1
            _log_green(
                f"[OBB catalog] 已生成容器内 catalog 替换: "
                f"{relative.as_posix()}"
            )
    return patched


def _obb_replacements_for_container(
    entries: list[dict[str, Any]],
    container_relative: str,
    import_result_root: Path,
) -> dict[str, Path]:
    replacements: dict[str, Path] = {}
    for entry in entries:
        if (
            entry.get("origin_kind") != "obb"
            or str(entry.get("container_relative_assets_obb", ""))
            != container_relative
        ):
            continue
        staged_relative = Path(str(entry.get("staged_relative", "")))
        modified_path = import_result_root / staged_relative
        part_rows = entry.get("split_parts")
        if isinstance(part_rows, list) and part_rows:
            existing_parts: list[Path] = []
            for row in part_rows:
                if not isinstance(row, dict):
                    continue
                part_path = modified_path.with_name(str(row.get("name", "")))
                if part_path.is_file():
                    existing_parts.append(part_path)
                    archive_entry = str(row.get("archive_entry", ""))
                    if not archive_entry:
                        raise RuntimeError(
                            f"OBB split 缺少 archive_entry 映射: {staged_relative}"
                        )
                    replacements[archive_entry] = part_path
            if existing_parts:
                first_name = str(part_rows[0].get("name", ""))
                match = re.fullmatch(r"(.+\.split)\d+", first_name)
                if match is None:
                    raise RuntimeError(f"无法识别 OBB split 名称: {first_name}")
                archive_first_name = str(part_rows[0].get("archive_entry", ""))
                archive_match = re.fullmatch(r"(.+\.split)\d+", archive_first_name)
                if archive_match is None:
                    raise RuntimeError(
                        f"无法识别 OBB split archive_entry: {archive_first_name or '<空>'}"
                    )
                extra_index = len(part_rows)
                while True:
                    extra_path = modified_path.with_name(f"{match.group(1)}{extra_index}")
                    if not extra_path.is_file():
                        break
                    replacements[f"{archive_match.group(1)}{extra_index}"] = extra_path
                    extra_index += 1
            continue

        if not modified_path.is_file():
            continue
        archive_entry = str(entry.get("archive_entry", ""))
        if not archive_entry:
            raise RuntimeError(f"OBB 条目缺少 archive_entry 映射: {staged_relative}")
        replacements[archive_entry] = modified_path
    return replacements


def finalize_obb_outputs(
    cfg: PipelineConfig,
    final_root: Path,
    import_result_root: Path,
) -> int:
    """Rebuild every modified OBB and emit complete containers under obb/."""

    map_path = resource_source_map_path(cfg)
    state = json.loads(map_path.read_text(encoding="utf-8-sig"))
    if not isinstance(state, dict):
        return 0
    containers = state.get("obb_containers")
    raw_entries = state.get("entries")
    if not isinstance(containers, list) or not isinstance(raw_entries, list):
        return 0
    entries = [entry for entry in raw_entries if isinstance(entry, dict)]

    plans: list[dict[str, Any]] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        relative_text = str(container.get("container_relative_assets_obb", ""))
        prefix_text = str(container.get("container_staging_prefix", ""))
        source_text = str(container.get("container_path", ""))
        if not relative_text or not prefix_text or not source_text:
            raise RuntimeError(f"OBB 来源映射不完整: {container}")
        source_obb = Path(source_text)
        source_stat = source_obb.stat()
        if (
            source_stat.st_size != int(container.get("source_size", -1))
            or source_stat.st_mtime_ns != int(container.get("source_mtime_ns", -1))
        ):
            raise RuntimeError(
                f"导出后源 OBB 已变化，请重新执行一键导出: {source_obb}"
            )

        replacements = _obb_replacements_for_container(
            entries,
            relative_text,
            import_result_root,
        )
        if not replacements:
            continue
        destination = final_root / "obb" / Path(relative_text)
        plans.append(
            {
                "source_path": source_obb,
                "destination_path": destination,
                "work_root": import_result_root / Path(prefix_text),
                "replacements": replacements,
                "source_obb": str(source_obb),
                "output_obb": str(destination),
                "container_relative_assets_obb": relative_text,
                "replacement_count": len(replacements),
                "replacement_entries": sorted(replacements),
            }
        )

    outputs: list[dict[str, Any]] = []
    final_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".obb_finalize_",
        dir=str(final_root.parent),
    ) as batch_dir_text:
        batch_root = Path(batch_dir_text)
        staged_outputs: list[tuple[dict[str, Any], Path]] = []
        for plan in plans:
            staged_output = batch_root / "staged" / Path(
                str(plan["container_relative_assets_obb"])
            )
            write_obb_from_template(
                Path(plan["source_path"]),
                staged_output,
                plan["replacements"],
            )
            with zipfile.ZipFile(staged_output, "r") as archive:
                corrupt_entry = archive.testzip()
            if corrupt_entry is not None:
                raise RuntimeError(
                    f"重打 OBB ZIP 校验失败，损坏条目={corrupt_entry}: {staged_output}"
                )
            staged_outputs.append((plan, staged_output))

        # Build and verify the complete batch first.  Publishing is then a
        # short rename phase with rollback for any destination that existed.
        published: list[tuple[Path, Path | None]] = []
        try:
            for index, (plan, staged_output) in enumerate(staged_outputs):
                destination = Path(plan["destination_path"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                backup: Path | None = None
                if destination.exists():
                    backup = batch_root / "backup" / str(index) / destination.name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(destination), str(backup))
                published.append((destination, backup))
                os.replace(str(staged_output), str(destination))
        except Exception:
            for destination, backup in reversed(published):
                destination.unlink(missing_ok=True)
                if backup is not None and backup.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(str(backup), str(destination))
            raise

        for plan, _staged_output in staged_outputs:
            output_row = {
                key: value
                for key, value in plan.items()
                if key
                not in {
                    "source_path",
                    "destination_path",
                    "work_root",
                    "replacements",
                }
            }
            outputs.append(output_row)
            work_root = Path(plan["work_root"])
            if work_root.exists():
                shutil.rmtree(work_root)
            _log_green(
                f"[OBB][完成] 已重打 {plan['container_relative_assets_obb']}，"
                f"替换条目={plan['replacement_count']} -> {plan['output_obb']}"
            )

    report_path = resource_state_root(cfg) / "final_obb_outputs.json"
    if outputs:
        _write_json(report_path, outputs)
    else:
        report_path.unlink(missing_ok=True)
    return len(outputs)


def print_final_addressables_sync_reminder(cfg: PipelineConfig, final_root: Path) -> None:
    report_path = remote_resource_report_path(cfg)
    if not report_path.is_file():
        return
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(report, dict):
        return
    localized_rows = report.get("localized_internal_ids")
    localized_count = (
        len(localized_rows)
        if isinstance(localized_rows, list)
        else report.get("localized_internal_id_count", 0)
    )
    if (
        not isinstance(localized_count, int)
        or isinstance(localized_count, bool)
        or localized_count <= 0
    ):
        return
    success_count = report.get("success_count", 0)
    if not isinstance(success_count, int) or isinstance(success_count, bool):
        success_count = 0

    source_aa = _addressables_root(cfg)
    expected_project_aa = (
        cfg.project_dir / "game-name" / "GAME_hongtu_P" / "assets" / "aa"
    )
    target_text = (
        str(expected_project_aa)
        if expected_project_aa.parent.is_dir()
        else "<实际项目目录>/assets/aa"
    )
    if success_count > 0:
        _log_green(
            f"[导入完成] 本次实际下载 {success_count} 个 Addressables 资源，"
            f"并将 {localized_count} 个远程路径改为本地路径，资源位于: {source_aa}"
        )
    else:
        _log_green(
            f"[导入完成] 远程资源文件已存在，本次已将 {localized_count} 个 "
            f"catalog 远程路径改为本地路径，资源位于: {source_aa}"
        )
    _log_green("[导入完成] 推荐替换顺序:")
    _log_green(f"[导入完成] 1. 先把 {source_aa} 同步到 {target_text}")
    _log_green(f"[导入完成] 2. 再用 {final_root} 中的修改资源覆盖实际项目对应文件")
