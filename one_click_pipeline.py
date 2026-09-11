from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Callable

from support.config import activate_config_path, load_config, resolve_config_path


PROJECT_ROOT = Path(__file__).resolve().parent
RESOURCE_MENU_SCRIPT = PROJECT_ROOT / "resource_menu.py"
MAIN_SCRIPT = PROJECT_ROOT / "main.py"
TOOLS_SCRIPT = PROJECT_ROOT / "工具脚本.py"
RUN_STEP_ARGUMENT = "--run-step"
AI_REQUEST_PATTERNS = {
    "2": "ai_translation_request*.json",
    "3": "ai_stringliteral_translation_request*.json",
}
MISSING_TTF_CHARS_FILENAME = "translation_chars_missing_from_ttf.txt"
WORKBENCH_EVENT_PREFIX = "@@WORKBENCH_EVENT@@"
RUN_STATE_SCHEMA_VERSION = 1
PIPELINE_VERSION = 1
RUN_STATE_FILENAME = "run-state.json"


def emit_workbench_event(kind: str, **payload: object) -> None:
    event = {"kind": kind, **payload}
    print(
        WORKBENCH_EVENT_PREFIX + json.dumps(event, ensure_ascii=False),
        flush=True,
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _config_fingerprint(config_path: Path) -> str:
    try:
        content = config_path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(content).hexdigest()


def _update_tree_fingerprint(hasher, root: Path) -> None:
    try:
        root = root.resolve()
    except OSError:
        pass
    hasher.update(str(root).encode("utf-8", errors="surrogatepass"))
    if root.is_file():
        try:
            stat = root.stat()
        except OSError:
            hasher.update(b"\0missing")
            return
        hasher.update(f"\0f\0{stat.st_size}\0{stat.st_mtime_ns}".encode("ascii"))
        return
    if not root.is_dir():
        hasher.update(b"\0missing")
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            try:
                relative = path.relative_to(root).as_posix()
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if entry.is_dir(follow_symlinks=False):
                hasher.update(f"\0d\0{relative}".encode("utf-8", errors="surrogatepass"))
                stack.append(path)
            elif entry.is_file(follow_symlinks=False):
                hasher.update(
                    f"\0f\0{relative}\0{stat.st_size}\0{stat.st_mtime_ns}".encode(
                        "utf-8",
                        errors="surrogatepass",
                    )
                )


def _input_fingerprint(cfg, config_path: Path) -> str:
    """Fingerprint source metadata without rereading every large asset body."""

    hasher = hashlib.sha256()
    try:
        hasher.update(config_path.read_bytes())
    except OSError:
        hasher.update(b"missing-config")
    source_names = (
        "resource_source_root",
        "resource_managed_root",
        "catalog_source_path",
        "stringliteral_json_path",
        "il2cpp_dummydll_root",
        "il2cpp_script_json_path",
        "il2cpp_dump_cs_path",
        "libil2cpp_arm64_path",
    )
    seen: set[str] = set()
    for name in source_names:
        value = getattr(cfg, name, None)
        if value is None:
            continue
        path = Path(value)
        normalized = os.path.normcase(str(path.resolve()))
        if normalized in seen:
            continue
        seen.add(normalized)
        _update_tree_fingerprint(hasher, path)
    return hasher.hexdigest()


def _default_run_state_path(config_path: Path) -> Path:
    # Workbench stores each project's Translate config in
    # ``<project>/.translate/config.json``.  Keeping the checkpoint beside it
    # makes project export/import preserve progress without copying caches.
    return config_path.parent / RUN_STATE_FILENAME


def _prepare_run_state(
    path: Path,
    *,
    config_path: Path,
    config_fingerprint: str,
    input_fingerprint: str = "",
    project_name: str,
    workspace_root: Path,
    record_dir: Path,
    allow_resume: bool = True,
) -> dict[str, object]:
    previous = _read_json_object(path)
    resumable = allow_resume and (
        int(previous.get("schema_version") or 0) == RUN_STATE_SCHEMA_VERSION
        and int(previous.get("pipeline_version") or 0) == PIPELINE_VERSION
        and str(previous.get("config_fingerprint") or "") == config_fingerprint
        and str(previous.get("input_fingerprint") or "") == input_fingerprint
        and str(previous.get("status") or "") in {"running", "failed", "interrupted"}
    )
    completed = [
        str(item)
        for item in (previous.get("completed_stages") or [])
        if str(item).strip()
    ] if resumable else []
    now = _now()
    state: dict[str, object] = {
        "schema_version": RUN_STATE_SCHEMA_VERSION,
        "pipeline_version": PIPELINE_VERSION,
        "project_name": project_name,
        "config_path": str(config_path),
        "config_fingerprint": config_fingerprint,
        "input_fingerprint": input_fingerprint,
        "workspace_root": str(workspace_root),
        "record_dir": str(record_dir),
        "status": "running",
        "current_stage": "",
        "completed_stages": completed,
        "last_error": "",
        "last_returncode": 0,
        "resumable": True,
        "started_at": str(previous.get("started_at") or now) if resumable else now,
        "resumed_at": now if resumable else "",
        "updated_at": now,
        "finished_at": "",
    }
    _write_json_atomic(path, state)
    if resumable and completed:
        print(
            f"[一键执行][断点恢复] 已恢复 {len(completed)} 个完成阶段；"
            "从第一个未完成阶段继续。",
            flush=True,
        )
        emit_workbench_event(
            "translate-resume",
            completed_stages=completed,
            state_path=str(path),
        )
    return state


def _save_run_state(path: Path | None, state: dict[str, object] | None) -> None:
    if path is None or state is None:
        return
    state["updated_at"] = _now()
    _write_json_atomic(path, state)


def _finish_run_state(
    path: Path | None,
    state: dict[str, object] | None,
    *,
    status: str,
    returncode: int,
    error: str = "",
) -> None:
    if state is None:
        return
    state["status"] = status
    state["current_stage"] = ""
    state["last_returncode"] = int(returncode)
    state["last_error"] = error
    state["resumable"] = status != "completed"
    state["finished_at"] = _now()
    _save_run_state(path, state)
    emit_workbench_event(
        "translate-checkpoint",
        status=status,
        returncode=int(returncode),
        completed_stages=list(state.get("completed_stages") or []),
        state_path=str(path) if path is not None else "",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="非交互执行资源全部导出、主菜单全部步骤和图片导出工具。"
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--start-at",
        choices=["export", *(str(index) for index in range(11)), "tools-1", "tools-2"],
        default="export",
        help="从指定阶段继续；默认从资源全部导出开始。",
    )
    return parser.parse_args(argv)


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _request_snapshot(record_dir: Path, pattern: str) -> dict[Path, tuple[int, int] | None]:
    return {
        path.resolve(): _file_signature(path)
        for path in sorted(record_dir.glob(pattern))
        if path.is_file()
    }


def _changed_requests(
    record_dir: Path,
    pattern: str,
    before: dict[Path, tuple[int, int] | None],
) -> list[Path]:
    changed: list[Path] = []
    for path in sorted(record_dir.glob(pattern)):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if before.get(resolved) != _file_signature(path):
            changed.append(resolved)
    return changed


def run_python_script(script: Path, args: list[str], label: str) -> int:
    command = [sys.executable, "-u", str(script), *args]
    child_env = dict(os.environ)
    child_env["TRANSLATE_SUPPRESS_UNITY_CONFIG_NOTICE"] = "1"
    child_env["TRANSLATE_NONINTERACTIVE_STEP"] = "1"
    print()
    print(f"[一键执行] 开始: {label}", flush=True)
    print(f"[一键执行] 命令: {' '.join(command)}", flush=True)
    emit_workbench_event("translate-stage-start", label=label)
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=child_env,
            check=False,
        ).returncode
    except OSError as exc:
        print(f"\033[91m[一键执行][失败] {label}: {exc}\033[0m", flush=True)
        emit_workbench_event(
            "translate-stage-error",
            label=label,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 1
    if result == 0:
        print(f"\033[92m[一键执行][完成] {label}\033[0m", flush=True)
    else:
        print(
            f"\033[91m[一键执行][失败] {label}，返回码={result}\033[0m",
            flush=True,
        )
    emit_workbench_event(
        "translate-stage-finish",
        label=label,
        returncode=result,
    )
    return result


def _run_main_step(step: str) -> int:
    return run_python_script(
        MAIN_SCRIPT,
        [RUN_STEP_ARGUMENT, step],
        f"主菜单脚本 {step}",
    )


def _retry_ai_failure(cfg, step: str, before: dict[Path, tuple[int, int] | None]) -> int:
    request_paths = _changed_requests(
        cfg.stage_record_dir,
        AI_REQUEST_PATTERNS[step],
        before,
    )
    if not request_paths:
        print(
            f"[一键执行][AI自动补批] 脚本 {step} 没有生成或更新 request 文件，"
            "无法调用补批工具；直接重试原步骤。",
            flush=True,
        )
    else:
        run_python_script(
            TOOLS_SCRIPT,
            ["retry-failed-ai-batches", *(str(path) for path in request_paths)],
            f"工具脚本 AI 失败批次自动补跑（主菜单脚本 {step}）",
        )
    return _run_main_step(step)


def _retry_ttf_merge_after_cleanup(cfg, before_signature: tuple[int, int] | None) -> int:
    missing_path = cfg.stage_record_dir / MISSING_TTF_CHARS_FILENAME
    after_signature = _file_signature(missing_path)
    if (
        after_signature is None
        or after_signature == before_signature
        or after_signature[1] == 0
    ):
        print(
            "[一键执行][缺字自动清理] 未检测到本次脚本 8 新生成的不支持字符清单，"
            "跳过清理并继续后续步骤。",
            flush=True,
        )
        return 1

    cleanup_result = run_python_script(
        TOOLS_SCRIPT,
        ["clean-unsupported-ttf-chars-all"],
        "工具脚本全部清除模板 TTF 不支持字符",
    )
    if cleanup_result != 0:
        return cleanup_result
    rebuild_result = _run_main_step("3")
    if rebuild_result != 0:
        return rebuild_result
    chars_result = _run_main_step("4")
    if chars_result != 0:
        return chars_result
    return _run_main_step("8")


def _run_checkpointed_stage(
    stage_id: str,
    label: str,
    callback: Callable[[], int],
    *,
    state_path: Path | None,
    state: dict[str, object] | None,
    stage_index: int,
    stage_total: int,
) -> int:
    completed = list(state.get("completed_stages") or []) if state is not None else []
    if stage_id in completed:
        print(f"[一键执行][断点复用] {label}", flush=True)
        emit_workbench_event(
            "translate-stage-finish",
            stage=stage_id,
            label=label,
            returncode=0,
            reused=True,
            index=stage_index,
            total=stage_total,
        )
        return 0

    if state is not None:
        state["status"] = "running"
        state["current_stage"] = stage_id
        state["current_stage_label"] = label
        state["last_error"] = ""
        state["last_returncode"] = 0
        _save_run_state(state_path, state)
        emit_workbench_event(
            "translate-checkpoint",
            status="running",
            stage=stage_id,
            label=label,
            index=stage_index,
            total=stage_total,
            state_path=str(state_path) if state_path is not None else "",
        )

    result = int(callback())
    if state is not None:
        if result == 0:
            completed.append(stage_id)
            state["completed_stages"] = completed
            state["current_stage"] = ""
            state["current_stage_label"] = ""
        else:
            state["status"] = "failed"
            state["last_returncode"] = result
            state["last_error"] = f"{label} 返回码 {result}"
        _save_run_state(state_path, state)
        emit_workbench_event(
            "translate-stage-progress",
            stage=stage_id,
            label=label,
            returncode=result,
            completed=len(completed),
            index=stage_index,
            total=stage_total,
        )
    return result


def run_one_click_pipeline(
    cfg,
    *,
    start_at: str = "export",
    state_path: Path | None = None,
    state: dict[str, object] | None = None,
) -> int:

    steps = ["0"]
    if cfg.enable_ai_field_review:
        steps.append("1")
    else:
        print("[一键执行] enable_ai_field_review=false，按主菜单全部执行规则跳过脚本 1。")
    steps.extend(["2", "3", "4", "5", "6", "7", "8", "9", "10"])

    if start_at not in {"export", "tools-1", "tools-2"}:
        start_number = int(start_at)
        steps = [step for step in steps if int(step) >= start_number]
    elif start_at in {"tools-1", "tools-2"}:
        steps = []

    final_tools = [
        ("tools-1", "copy-all-images", "工具脚本 1：一键复制导出图片"),
        ("tools-2", "split-sprite-atlases", "工具脚本 2：拆分 Sprite / NGUI 图集"),
    ]
    if start_at == "tools-2":
        final_tools = final_tools[1:]

    plan: list[tuple[str, str]] = []
    if start_at == "export":
        plan.append(("export", "资源菜单全部导出"))
    plan.extend((f"main-{step}", f"主菜单脚本 {step}") for step in steps)
    plan.extend((stage_id, label) for stage_id, _command, label in final_tools)
    stage_positions = {
        stage_id: (index, len(plan))
        for index, (stage_id, _label) in enumerate(plan, start=1)
    }

    try:
        if start_at == "export":
            stage_id = "export"
            index, total = stage_positions[stage_id]
            export_result = _run_checkpointed_stage(
                stage_id,
                "资源菜单全部导出",
                lambda: run_python_script(
                    RESOURCE_MENU_SCRIPT,
                    ["export-all"],
                    "资源菜单全部导出",
                ),
                state_path=state_path,
                state=state,
                stage_index=index,
                stage_total=total,
            )
            if export_result != 0:
                print(
                    f"\033[91m[一键执行][已中断] 资源菜单全部导出失败，"
                    f"返回码={export_result}。\033[0m",
                    flush=True,
                )
                _finish_run_state(
                    state_path,
                    state,
                    status="failed",
                    returncode=export_result,
                    error=f"资源菜单全部导出返回码 {export_result}",
                )
                return export_result

        for step in steps:
            stage_id = f"main-{step}"
            index, total = stage_positions[stage_id]

            def run_main_stage(current_step: str = step) -> int:
                request_before = (
                    _request_snapshot(
                        cfg.stage_record_dir,
                        AI_REQUEST_PATTERNS[current_step],
                    )
                    if current_step in AI_REQUEST_PATTERNS
                    else {}
                )
                missing_before = (
                    _file_signature(
                        cfg.stage_record_dir / MISSING_TTF_CHARS_FILENAME
                    )
                    if current_step == "8"
                    else None
                )
                result = _run_main_step(current_step)
                if result != 0 and current_step in AI_REQUEST_PATTERNS:
                    result = _retry_ai_failure(cfg, current_step, request_before)
                elif result != 0 and current_step == "8":
                    result = _retry_ttf_merge_after_cleanup(cfg, missing_before)
                return result

            result = _run_checkpointed_stage(
                stage_id,
                f"主菜单脚本 {step}",
                run_main_stage,
                state_path=state_path,
                state=state,
                stage_index=index,
                stage_total=total,
            )
            if result != 0:
                print(
                    f"\033[91m[一键执行][已中断] 主菜单脚本 {step} "
                    f"失败，最终返回码={result}。\033[0m",
                    flush=True,
                )
                _finish_run_state(
                    state_path,
                    state,
                    status="failed",
                    returncode=result,
                    error=f"主菜单脚本 {step} 返回码 {result}",
                )
                return result

        for stage_id, command, label in final_tools:
            index, total = stage_positions[stage_id]
            result = _run_checkpointed_stage(
                stage_id,
                label,
                lambda current_command=command, current_label=label: run_python_script(
                    TOOLS_SCRIPT,
                    [current_command],
                    current_label,
                ),
                state_path=state_path,
                state=state,
                stage_index=index,
                stage_total=total,
            )
            if result != 0:
                print(
                    f"\033[91m[一键执行][已中断] {label}失败，"
                    f"返回码={result}。\033[0m",
                    flush=True,
                )
                _finish_run_state(
                    state_path,
                    state,
                    status="failed",
                    returncode=result,
                    error=f"{label} 返回码 {result}",
                )
                return result
    except KeyboardInterrupt:
        print("\n[一键执行][已取消] 已保留完成阶段，下次从断点继续。", flush=True)
        _finish_run_state(
            state_path,
            state,
            status="interrupted",
            returncode=130,
            error="用户中断",
        )
        return 130
    except BaseException as exc:
        _finish_run_state(
            state_path,
            state,
            status="failed",
            returncode=1,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    print()
    print("\033[92m[一键执行][全部完成] 所有步骤均已成功。\033[0m", flush=True)
    _finish_run_state(
        state_path,
        state,
        status="completed",
        returncode=0,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = resolve_config_path(args.config)
    activate_config_path(config_path)
    cfg = load_config()
    print(f"当前项目工作区: {cfg.workspace_root}")
    print("执行顺序: 资源全部导出 -> 主菜单全部脚本 -> 工具脚本 1 -> 工具脚本 2")
    print("运行方式: 全程非交互；仅 AI 翻译批次失败与模板 TTF 缺字会自动恢复，其他失败立即中断。")
    if args.start_at != "export":
        print(f"续跑起点: {args.start_at}")
    state_path = _default_run_state_path(config_path)
    state = _prepare_run_state(
        state_path,
        config_path=config_path,
        config_fingerprint=_config_fingerprint(config_path),
        input_fingerprint=_input_fingerprint(cfg, config_path),
        project_name=str(cfg.project_name),
        workspace_root=cfg.workspace_root,
        record_dir=cfg.stage_record_dir,
        allow_resume=args.start_at == "export",
    )
    print(f"断点记录: {state_path}", flush=True)
    return run_one_click_pipeline(
        cfg,
        start_at=args.start_at,
        state_path=state_path,
        state=state,
    )


if __name__ == "__main__":
    raise SystemExit(main())
