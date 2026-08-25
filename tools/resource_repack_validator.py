from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from support.config import load_config
from pipeline.resource_staging import load_prepared_resource_source


CLI_PROJECT = SCRIPT_DIR / "AssetPipeline_CLI" / "UnityResourceCLI" / "UnityResourceCLI.csproj"


def _load_source_map(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"未找到导出资源路径映射，请先执行一键导出: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not isinstance(payload.get("entries"), list):
        raise ValueError(f"资源路径映射结构异常: {path}")
    return payload


def _final_path(final_root: Path, entry: dict[str, Any]) -> Path:
    relative = Path(str(entry.get("staged_relative", "")))
    parts = relative.parts
    category = entry.get("category")
    if category == "data" and len(parts) >= 2:
        return final_root / "Data" / Path(*parts[2:])
    if category == "addressables_android" and len(parts) >= 2:
        return final_root / "Bundle" / "Android" / Path(*parts[2:])
    return final_root / relative


def _link_or_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def _merge_final_split(entry: dict[str, Any], final_merged_path: Path, destination: Path) -> bool:
    part_rows = entry.get("split_parts")
    if not isinstance(part_rows, list) or not part_rows:
        return False
    first_name = Path(str(part_rows[0].get("name", ""))).name
    first_path = final_merged_path.with_name(first_name)
    if not first_path.is_file():
        return False

    parts: list[Path] = []
    for row in part_rows:
        if not isinstance(row, dict):
            return False
        name = Path(str(row.get("name", ""))).name
        part_path = final_merged_path.with_name(name)
        if not part_path.is_file():
            raise RuntimeError(f"FinalResult 的 split 序号不连续或缺失: {part_path}")
        parts.append(part_path)

    # A modified resource may grow and create new split indexes. Include every
    # consecutive final part after the recorded range as well.
    prefix = parts[0].name.rsplit("split", 1)[0] + "split"
    index = len(parts)
    while True:
        extra = final_merged_path.with_name(f"{prefix}{index}")
        if not extra.is_file():
            break
        parts.append(extra)
        index += 1

    original_sizes = [int(row.get("size", 0) or 0) for row in part_rows]
    fixed_sizes = original_sizes[:-1] or original_sizes
    chunk_size = fixed_sizes[0]
    if chunk_size <= 0 or any(size != chunk_size for size in fixed_sizes):
        raise RuntimeError(
            f"原 split 非尾片大小不一致，无法验证固定切片边界: "
            f"{final_merged_path} sizes={original_sizes}"
        )
    actual_sizes = [part.stat().st_size for part in parts]
    if any(size != chunk_size for size in actual_sizes[:-1]):
        raise RuntimeError(
            f"FinalResult 的 split 边界错误；所有非尾片必须保持 {chunk_size} 字节: "
            f"{final_merged_path} sizes={actual_sizes}"
        )
    if actual_sizes[-1] <= 0 or actual_sizes[-1] > chunk_size:
        raise RuntimeError(
            f"FinalResult 的 split 尾片大小无效，必须在 1..{chunk_size} 字节之间: "
            f"{final_merged_path} size={actual_sizes[-1]}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    print(f"[资源验证][split] 临时合并 {len(parts)} 个切片: {final_merged_path.name}")
    return True


def _prepare_candidates(
    state: dict[str, Any],
    final_root: Path,
    candidate_root: Path,
) -> tuple[Path, int]:
    staging_value = state.get("staging_root")
    source_root = Path(str(staging_value)) if staging_value else final_root.parent / "input_sources"
    if not source_root.is_dir():
        raise FileNotFoundError(f"原始资源缓存不存在，请重新执行一键导出: {source_root}")

    prepared = 0
    for raw_entry in state["entries"]:
        if not isinstance(raw_entry, dict):
            continue
        staged_relative = Path(str(raw_entry.get("staged_relative", "")))
        if not staged_relative.parts:
            continue
        original = source_root / staged_relative
        if not original.is_file():
            print(f"\033[93m[资源验证][警告] 原始缓存缺失，跳过: {original}\033[0m")
            continue
        final_path = _final_path(final_root, raw_entry)
        destination = candidate_root / staged_relative
        if final_path.is_file():
            _link_or_copy(final_path, destination)
            prepared += 1
            continue
        if _merge_final_split(raw_entry, final_path, destination):
            prepared += 1

    return source_root, prepared


def run_resource_repack_validation() -> int:
    cfg = load_config()
    workspace = cfg.workspace_root
    source_map = workspace / "resource_state" / "resource_source_map.json"
    final_root = workspace / "FinalResult"
    report_path = cfg.record_dir / "resource_repack_validation.json"
    log_path = cfg.log_dir / "资源重打验证.log"

    try:
        state = _load_source_map(source_map)
        source_root = load_prepared_resource_source(cfg)
        if source_root is None:
            raise FileNotFoundError("原始资源缓存不存在，请重新执行一键导出。")
        state["staging_root"] = str(source_root)
        if not final_root.is_dir():
            raise FileNotFoundError(f"FinalResult 不存在，请先执行一键导入: {final_root}")
        (workspace / "temp").mkdir(parents=True, exist_ok=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="resource_verify_", dir=workspace / "temp") as temp_name:
            candidate_root = Path(temp_name) / "candidate"
            source_root, prepared = _prepare_candidates(state, final_root, candidate_root)
            if prepared == 0:
                raise RuntimeError("FinalResult 中没有找到可与原始缓存配对的修改资源。")

            print(f"[资源验证] 已配对修改资源: {prepared} 个")
            artifacts_root = workspace / "temp" / "dotnet_artifacts"
            command = [
                "dotnet", "run", "--project", str(CLI_PROJECT), "-c", "Release",
                "--artifacts-path", str(artifacts_root), "--",
                "verify",
                "--source", str(source_root),
                "--work", str(cfg.record_dir),
                "--result-root", str(candidate_root),
                "--managed", str(cfg.resource_managed_root),
                "--report", str(report_path),
            ]
            with log_path.open("w", encoding="utf-8") as log_file:
                process = subprocess.Popen(
                    command,
                    cwd=SCRIPT_DIR,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                assert process.stdout is not None
                for line in process.stdout:
                    print(line, end="")
                    log_file.write(line)
                return_code = process.wait()

            if report_path.is_file():
                report = json.loads(report_path.read_text(encoding="utf-8-sig"))
                if isinstance(report, dict):
                    report["validation_staging_root"] = report.get("candidate_root", "")
                    report["candidate_root"] = str(final_root)
                    report["paired_resource_count"] = prepared
                    report_path.write_text(
                        json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )

        if return_code == 0:
            print(f"\033[92m[资源验证][通过] 修改后资源未发现结构性错误。\033[0m")
        else:
            print(f"\033[91m[资源验证][失败] 发现结构性错误，请查看报告后再替换游戏资源。\033[0m")
        print(f"[资源验证] JSON 报告: {report_path}")
        print(f"[资源验证] 完整日志: {log_path}")
        return return_code
    except Exception as exc:
        print(f"\033[91m[资源验证][失败] {exc}\033[0m")
        return 1


def main() -> int:
    return run_resource_repack_validation()


if __name__ == "__main__":
    raise SystemExit(main())
