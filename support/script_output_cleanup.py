from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from support.config import PipelineConfig


MANIFEST_PATH = Path(__file__).with_name("script_output_manifest.json")


@dataclass(frozen=True)
class CleanupTarget:
    path: Path
    source_rule: str
    allowed_root: Path


def load_script_output_manifest() -> dict[str, Any]:
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        raise ValueError(f"脚本产物清单格式无效: {MANIFEST_PATH}")
    return data


def _base_paths(cfg: PipelineConfig) -> dict[str, Path]:
    return {
        "root": cfg.root_dir.resolve(),
        "records": cfg.stage_record_dir.resolve(),
        "output": cfg.stage_dir.resolve(),
        "logs": cfg.log_dir.resolve(),
        "unity_project": cfg.unity_font_project.resolve(),
    }


def _format_rule_path(cfg: PipelineConfig, value: str) -> str:
    return value.format(
        ttf_template_name=cfg.ttf_template_path.name,
    )


def _safe_target(path: Path, allowed_roots: Iterable[Path]) -> Path:
    resolved = path.resolve()
    for root in allowed_roots:
        root = root.resolve()
        if resolved != root and resolved.is_relative_to(root):
            return resolved
    raise ValueError(f"拒绝处理清单允许目录之外的路径: {resolved}")


def _report_output_targets(
    report_path: Path,
    rule: dict[str, Any],
    bases: dict[str, Path],
) -> list[CleanupTarget]:
    if not report_path.is_file():
        return []
    try:
        report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return []
    target_base_name = rule.get("target_base")
    target_base = bases.get(target_base_name)
    arrays = rule.get("arrays")
    field = rule.get("field")
    if target_base is None or not isinstance(arrays, list) or not isinstance(field, str):
        return []

    targets: list[CleanupTarget] = []
    for array_name in arrays:
        items = report.get(array_name) if isinstance(report, dict) else None
        if not isinstance(items, list):
            continue
        for item in items:
            value = item.get(field) if isinstance(item, dict) else None
            if not isinstance(value, str) or not value:
                continue
            targets.append(
                CleanupTarget(
                    path=target_base / Path(value),
                    source_rule=f"{report_path.name}:{array_name}.{field}",
                    allowed_root=target_base,
                )
            )
    return targets


def collect_script_cleanup_targets(
    cfg: PipelineConfig,
    script_ids: Iterable[str],
) -> tuple[list[CleanupTarget], list[str]]:
    manifest = load_script_output_manifest()
    scripts = manifest["scripts"]
    bases = _base_paths(cfg)
    targets: list[CleanupTarget] = []
    notes: list[str] = []
    expanded_ids: list[str] = []

    def expand(script_id: str, active: set[str]) -> None:
        if script_id in active:
            raise ValueError(f"脚本产物清单 includes 存在循环: {script_id}")
        entry = scripts.get(script_id)
        if not isinstance(entry, dict):
            raise KeyError(f"清单中不存在脚本 {script_id}")
        if script_id not in expanded_ids:
            expanded_ids.append(script_id)
        includes = entry.get("includes")
        if not isinstance(includes, list):
            return
        for included in includes:
            if isinstance(included, str):
                expand(included, active | {script_id})

    for script_id in script_ids:
        expand(str(script_id), set())

    for script_id in expanded_ids:
        entry = scripts.get(script_id)
        if not isinstance(entry, dict):
            raise KeyError(f"清单中不存在脚本 {script_id}")
        for note in entry.get("notes", []):
            if isinstance(note, str):
                notes.append(f"脚本 {script_id}: {note}")
        for rule in entry.get("outputs", []):
            if not isinstance(rule, dict):
                continue
            base_name = rule.get("base")
            kind = rule.get("kind")
            relative = rule.get("path")
            base = bases.get(base_name)
            if base is None or not isinstance(relative, str) or not relative:
                continue
            relative = _format_rule_path(cfg, relative)
            path = base / Path(relative)
            if kind == "glob":
                for match in base.glob(relative):
                    targets.append(CleanupTarget(match, f"{base_name}:{relative}", base))
            elif kind == "report_output_files":
                targets.extend(_report_output_targets(path, rule, bases))
            elif kind in {"file", "directory"}:
                targets.append(CleanupTarget(path, f"{base_name}:{relative}", base))

    unique: dict[str, CleanupTarget] = {}
    for target in targets:
        safe_path = _safe_target(target.path, (target.allowed_root,))
        unique[str(safe_path).lower()] = CleanupTarget(
            safe_path,
            target.source_rule,
            target.allowed_root.resolve(),
        )
    ordered = sorted(unique.values(), key=lambda item: (len(item.path.parts), str(item.path).lower()), reverse=True)
    return ordered, notes


def target_size(target: Path) -> tuple[int, int]:
    if target.is_file():
        return 1, target.stat().st_size
    if not target.is_dir():
        return 0, 0
    count = 0
    size = 0
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        count += 1
        try:
            size += path.stat().st_size
        except OSError:
            pass
    return count, size


def delete_script_cleanup_targets(targets: Iterable[CleanupTarget]) -> list[Path]:
    removed: list[Path] = []
    for target in targets:
        path = target.path
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.is_file() or path.is_symlink():
            path.unlink()
            removed.append(path)
    return removed
