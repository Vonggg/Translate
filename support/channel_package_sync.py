from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path


EXPLICIT_CONFIG_ENV = "TRANSLATE_CONFIG_EXPLICIT"
CHANNEL_PACKAGE_DIR_NAME_ENV = "TRANSLATE_CHANNEL_PACKAGE_DIR_NAME"
CHANNEL_PACKAGE_NAME_RE = re.compile(r"^GAME_[^<>:\"/\\|?*]+$", re.IGNORECASE)


@dataclass(frozen=True)
class ChannelPackageTarget:
    name: str
    root: Path
    assets_root: Path


@dataclass(frozen=True)
class ChannelSyncSummary:
    source_aa_files: int = 0
    final_result_files: int = 0
    copied_bytes: int = 0


def explicit_channel_sync_request() -> tuple[bool, str]:
    explicit_config = os.environ.get(EXPLICIT_CONFIG_ENV, "").strip() == "1"
    channel_name = os.environ.get(CHANNEL_PACKAGE_DIR_NAME_ENV, "").strip()
    return explicit_config, channel_name


def resolve_channel_package_target(cfg, channel_name: str) -> ChannelPackageTarget:
    name = str(channel_name or "").strip()
    if not name or not CHANNEL_PACKAGE_NAME_RE.fullmatch(name):
        raise ValueError(f"渠道包目录名无效，必须是单层 GAME_* 目录名: {channel_name!r}")

    catalog_path = Path(cfg.catalog_source_path).resolve()
    if len(catalog_path.parents) < 4:
        raise ValueError(f"无法从 catalog 路径定位 game-name 目录: {catalog_path}")
    game_name_root = catalog_path.parents[3].resolve()
    project_dir = Path(cfg.project_dir).resolve()
    try:
        game_name_root.relative_to(project_dir)
    except ValueError as exc:
        raise ValueError(
            f"catalog 不在当前项目目录内，拒绝定位渠道包: {catalog_path}"
        ) from exc

    channel_root = (game_name_root / name).resolve()
    if channel_root.parent != game_name_root:
        raise ValueError(f"渠道包路径越界: {channel_root}")
    if not channel_root.is_dir():
        raise FileNotFoundError(f"渠道包目录不存在: {channel_root}")
    assets_root = (channel_root / "assets").resolve()
    if assets_root.parent != channel_root:
        raise ValueError(f"渠道包 assets 路径越界: {assets_root}")
    if not assets_root.is_dir():
        raise FileNotFoundError(f"渠道包 assets 目录不存在: {assets_root}")
    manifest_path = channel_root / "AndroidManifest.xml"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"渠道包缺少 AndroidManifest.xml: {manifest_path}")
    return ChannelPackageTarget(name=name, root=channel_root, assets_root=assets_root)


def _has_localized_remote_resources(cfg) -> bool:
    report_path = Path(cfg.workspace_root) / "resource_state" / "addressables_remote_resources.json"
    if not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(report, dict):
        return False
    rows = report.get("localized_internal_ids")
    if isinstance(rows, list):
        return bool(rows)
    count = report.get("localized_internal_id_count", 0)
    return isinstance(count, int) and not isinstance(count, bool) and count > 0


def _safe_destination(allowed_root: Path, destination: Path) -> Path:
    allowed_root = allowed_root.resolve()
    destination = destination.resolve()
    try:
        destination.relative_to(allowed_root)
    except ValueError as exc:
        raise ValueError(f"渠道包写入路径越界: {destination}") from exc
    return destination


def _copy_file_atomic(source: Path, destination: Path, allowed_root: Path) -> int:
    destination = _safe_destination(allowed_root, destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".translate-channel-sync.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return source.stat().st_size


def _overlay_tree(
    source_root: Path,
    destination_root: Path,
    allowed_root: Path,
) -> tuple[int, int]:
    if not source_root.is_dir():
        return 0, 0
    copied = 0
    copied_bytes = 0
    for source in sorted(path for path in source_root.rglob("*") if path.is_file()):
        relative = source.relative_to(source_root)
        copied_bytes += _copy_file_atomic(
            source,
            destination_root / relative,
            allowed_root,
        )
        copied += 1
        if copied % 500 == 0:
            print(f"[渠道包同步] {source_root.name}: {copied} 个文件", flush=True)
    return copied, copied_bytes


def sync_import_result_to_channel_package(
    cfg,
    final_root: Path,
    channel_name: str,
) -> ChannelSyncSummary:
    target = resolve_channel_package_target(cfg, channel_name)
    final_root = Path(final_root).resolve()
    expected_final_root = (Path(cfg.workspace_root) / "FinalResult").resolve()
    if final_root != expected_final_root:
        raise ValueError(
            f"导入结果不属于当前配置的 workspace: {final_root} != {expected_final_root}"
        )
    final_data = final_root / "Data"
    final_bundle = final_root / "Bundle"
    if not final_data.is_dir() and not final_bundle.is_dir():
        raise FileNotFoundError(f"导入结果中没有 Data 或 Bundle: {final_root}")

    source_aa_files = 0
    final_result_files = 0
    copied_bytes = 0
    if _has_localized_remote_resources(cfg):
        source_aa = Path(cfg.catalog_source_path).resolve().parent
        if not source_aa.is_dir():
            raise FileNotFoundError(f"远程资源模式下原游戏 aa 目录不存在: {source_aa}")
        print(
            f"\033[92m[渠道包同步] 检测到远程资源本地化，先同步完整 aa: "
            f"{source_aa} -> {target.assets_root / 'aa'}\033[0m",
            flush=True,
        )
        source_aa_files, size = _overlay_tree(
            source_aa,
            target.assets_root / "aa",
            target.assets_root,
        )
        copied_bytes += size

    # FinalResult must be the last overlay so translated bundles/catalog always
    # win over the original aa tree copied above.
    mappings = (
        (final_data, target.assets_root / "bin" / "Data"),
        (final_bundle / "Android", target.assets_root / "aa" / "Android"),
    )
    for source_root, destination_root in mappings:
        count, size = _overlay_tree(
            source_root,
            destination_root,
            target.assets_root,
        )
        final_result_files += count
        copied_bytes += size

    if final_bundle.is_dir():
        for source in sorted(path for path in final_bundle.iterdir() if path.is_file()):
            copied_bytes += _copy_file_atomic(
                source,
                target.assets_root / "aa" / source.name,
                target.assets_root,
            )
            final_result_files += 1

    if final_result_files <= 0:
        raise RuntimeError(f"FinalResult 没有可同步到渠道包的文件: {final_root}")
    print(
        f"\033[92m[渠道包同步][完成] {target.name}: "
        f"原 aa={source_aa_files}，导入结果={final_result_files}，"
        f"合计={copied_bytes / 1024 / 1024:.1f} MB -> {target.root}\033[0m",
        flush=True,
    )
    return ChannelSyncSummary(
        source_aa_files=source_aa_files,
        final_result_files=final_result_files,
        copied_bytes=copied_bytes,
    )
