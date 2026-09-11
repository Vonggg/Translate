from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO


PROJECT_ROOT = Path(__file__).resolve().parent
QUICK_CONFIG_SCRIPT = (PROJECT_ROOT / "快速配置.py").resolve()
DEFAULT_CONFIG_PATH = (PROJECT_ROOT / "config.json").resolve()
ACTIVE_CONFIG_PATH_ENV = "TRANSLATE_CONFIG_PATH"
EXPLICIT_CONFIG_ENV = "TRANSLATE_CONFIG_EXPLICIT"
CHANNEL_PACKAGE_DIR_NAME_ENV = "TRANSLATE_CHANNEL_PACKAGE_DIR_NAME"
CONTROL_CENTER_ENV = "MYWORKBENCH_CONTROL_CENTER"
RESOURCE_DIR_ENV = "MYWORKBENCH_TRANSLATE_RESOURCE_DIR"
WORKBENCH_EVENT_PREFIX = "@@WORKBENCH_EVENT@@"
GIB = 1024 ** 3
MIN_ONE_CLICK_AVAILABLE_MEMORY = 2 * GIB
MIN_ONE_CLICK_DISK_FREE = 30 * GIB
ONE_CLICK_SCRIPT = (PROJECT_ROOT / "one_click_pipeline.py").resolve()
SCRIPT_CHOICES = (
    ("0", "快速配置.py", "首次使用快速配置"),
    ("1", "resource_menu.py", "资源菜单"),
    ("2", "main.py", "主菜单"),
    ("3", "工具脚本.py", "工具脚本"),
    ("4", "one_click_pipeline.py", "一键执行完整流程"),
)


class _OneClickResourceLease:
    def __init__(self, stream: BinaryIO, path: Path) -> None:
        self.stream = stream
        self.path = path
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()


def _emit_workbench_event(kind: str, **payload: object) -> None:
    if os.environ.get(CONTROL_CENTER_ENV) != "1":
        return
    print(
        WORKBENCH_EVENT_PREFIX
        + json.dumps({"kind": kind, **payload}, ensure_ascii=False),
        flush=True,
    )


def _physical_memory() -> tuple[int, int]:
    if os.name != "nt":
        try:
            values: dict[str, int] = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                values[key] = int(raw.strip().split()[0]) * 1024
            return values.get("MemTotal", 0), values.get(
                "MemAvailable", values.get("MemFree", 0)
            )
        except (OSError, ValueError, IndexError):
            return 0, 0

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 0, 0
    return int(status.total_physical), int(status.available_physical)


def _one_click_slot_limit(total_memory: int, available_memory: int) -> int:
    if (
        total_memory >= 30 * GIB
        and available_memory >= 12 * GIB
        and (os.cpu_count() or 1) >= 8
    ):
        return 2
    return 1


def _one_click_worker_budget(slot_limit: int) -> dict[str, str]:
    scan, export, translate, import_workers = (4, 2, 2, 2) if slot_limit >= 2 else (8, 4, 4, 4)
    return {
        "TRANSLATE_AUTO_MAX_SCAN_WORKERS": str(scan),
        "TRANSLATE_AUTO_MAX_EXPORT_WORKERS": str(export),
        "TRANSLATE_AUTO_MAX_TRANSLATE_WORKERS": str(translate),
        "TRANSLATE_AUTO_MAX_IMPORT_WORKERS": str(import_workers),
        "MYWORKBENCH_TRANSLATE_SLOT_LIMIT": str(slot_limit),
    }


def _try_lock_one_click_slot(resource_dir: Path, slot_limit: int) -> _OneClickResourceLease | None:
    resource_dir.mkdir(parents=True, exist_ok=True)
    for index in range(slot_limit):
        path = resource_dir / f"slot-{index + 1}.lock"
        stream = path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            continue
        return _OneClickResourceLease(stream, path)
    return None


def _acquire_one_click_resource(config_path: Path) -> tuple[_OneClickResourceLease | None, dict[str, str]]:
    """Wait only for launcher option 4; all other menus bypass this gate."""

    if os.environ.get(CONTROL_CENTER_ENV) != "1":
        return None, {}
    raw_root = os.environ.get(RESOURCE_DIR_ENV)
    resource_dir = Path(raw_root) if raw_root else Path(tempfile.gettempdir()) / "workbench_translate_one_click_slots"
    last_reason = ""
    while True:
        total_memory, available_memory = _physical_memory()
        if total_memory > 0 and available_memory < MIN_ONE_CLICK_AVAILABLE_MEMORY:
            reason = "等待可用内存达到 2 GiB"
        else:
            try:
                disk_free = shutil.disk_usage(config_path.parent).free
            except OSError:
                disk_free = -1
            if 0 <= disk_free < MIN_ONE_CLICK_DISK_FREE:
                reason = "等待工作盘可用空间达到 30 GiB"
            else:
                slot_limit = _one_click_slot_limit(total_memory, available_memory)
                lease = _try_lock_one_click_slot(resource_dir, slot_limit)
                if lease is not None:
                    budget = _one_click_worker_budget(slot_limit)
                    _emit_workbench_event(
                        "translate-resource-acquired",
                        label="一键执行完整流程",
                        slot=lease.path.name,
                        slot_limit=slot_limit,
                        available_memory_bytes=available_memory,
                    )
                    return lease, budget
                reason = "等待其他 Translate 一键流程释放全局资源"
        if reason != last_reason:
            _emit_workbench_event(
                "translate-resource-wait",
                label="一键执行完整流程",
                reason=reason,
            )
            print(f"[调度] {reason}。", flush=True)
            last_reason = reason
        time.sleep(5.0)


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    selected = config_path
    if selected is None:
        selected = os.environ.get(ACTIVE_CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH
    path = Path(selected)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_config(config_path: str | Path | None = None) -> dict:
    config_path = resolve_config_path(config_path)
    if not config_path.is_file():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError(f"配置文件根节点必须是 JSON 对象: {config_path}")
    return config


def resolve_python(config: dict) -> Path:
    raw = str(config.get("python_executable", "") or "").strip()
    if not raw:
        return Path(sys.executable)

    python_path = Path(raw)
    if not python_path.is_absolute():
        python_path = (PROJECT_ROOT / python_path).resolve()
    if not python_path.is_file():
        raise FileNotFoundError(f"Configured python_executable not found: {python_path}")
    return python_path


def resolve_script(script: str) -> Path:
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = PROJECT_ROOT / script_path
    script_path = script_path.resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"Script not found: {script_path}")
    return script_path


def resolve_configured_python(config_path: str | Path | None = None) -> Path | None:
    try:
        return resolve_python(load_config(config_path))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[launcher] Python 配置不可用: {exc}", file=sys.stderr)
        print("[launcher] 请重新运行启动器并选择 0（首次使用快速配置）进行修复。", file=sys.stderr)
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="使用指定配置及其中的 python_executable 启动 Translate 菜单。",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="本次启动会话使用的配置文件；相对路径按 Translate 项目根目录解析。",
    )
    parser.add_argument(
        "--channel-package-dir-name",
        default=None,
        help="导入成功后自动回写的渠道包目录名，例如 GAME_hongtu_L。",
    )
    parser.add_argument("--print-python", action="store_true", help="只打印配置解析出的 Python 路径并退出。")
    parser.add_argument(
        "script",
        nargs="?",
        help="Script to run, relative to this project. If omitted, show an entry menu.",
    )
    parser.add_argument("script_args", nargs=argparse.REMAINDER, help="Arguments passed to the target script.")
    return parser.parse_args()


def choose_script() -> str | None:
    print("Translate Project Launcher")
    for key, script, label in SCRIPT_CHOICES:
        print(f"{key}. {label}: {PROJECT_ROOT / script}")
    print("q. 退出")
    print()

    valid = {key: script for key, script, _ in SCRIPT_CHOICES}
    while True:
        try:
            choice = input("请选择: ").strip().lower()
        except EOFError:
            print()
            return None
        if choice in ("q", "quit", "exit"):
            return None
        if choice in valid:
            return valid[choice]
        print("无效选择，请输入 0/1/2/3/4/q。")


def run_script(
    script: str,
    script_args: list[str],
    config_path: Path,
    *,
    explicit_config: bool = False,
    channel_package_dir_name: str = "",
) -> int:
    script_path = resolve_script(script)
    if script_path == QUICK_CONFIG_SCRIPT:
        python_path = Path(sys.executable)
    else:
        python_path = resolve_configured_python(config_path)
        if python_path is None:
            return 2

    forwarded_args = list(script_args)
    if script_path == QUICK_CONFIG_SCRIPT:
        forwarded_args = ["--config", str(config_path), *forwarded_args]
    command = [str(python_path), "-u", str(script_path), *forwarded_args]
    child_env = dict(os.environ)
    child_env[ACTIVE_CONFIG_PATH_ENV] = str(config_path)
    child_env[EXPLICIT_CONFIG_ENV] = "1" if explicit_config else "0"
    if channel_package_dir_name:
        child_env[CHANNEL_PACKAGE_DIR_NAME_ENV] = channel_package_dir_name
    else:
        child_env.pop(CHANNEL_PACKAGE_DIR_NAME_ENV, None)
    print(f"[launcher] Python: {python_path}", flush=True)
    print(f"[launcher] Config: {config_path}", flush=True)
    if explicit_config:
        print(f"[launcher] Config mode: explicit", flush=True)
    if channel_package_dir_name:
        print(f"[launcher] Channel package: {channel_package_dir_name}", flush=True)
    print(f"[launcher] Script: {script_path}", flush=True)
    resource_lease: _OneClickResourceLease | None = None
    if script_path == ONE_CLICK_SCRIPT:
        resource_lease, worker_budget = _acquire_one_click_resource(config_path)
        child_env.update(worker_budget)
    try:
        return subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=child_env,
            check=False,
        ).returncode
    except OSError as exc:
        print(f"[launcher] 无法启动 Python: {exc}", file=sys.stderr)
        if script_path != QUICK_CONFIG_SCRIPT:
            print("[launcher] 请选择 0（首次使用快速配置）检查 Python 环境。", file=sys.stderr)
        return 2
    finally:
        if resource_lease is not None:
            resource_lease.release()
            _emit_workbench_event(
                "translate-resource-release",
                label="一键执行完整流程",
            )


def main() -> int:
    args = parse_args()
    config_path = resolve_config_path(args.config)
    explicit_config = args.config is not None or os.environ.get(EXPLICIT_CONFIG_ENV) == "1"
    channel_package_dir_name = str(
        args.channel_package_dir_name
        if args.channel_package_dir_name is not None
        else os.environ.get(CHANNEL_PACKAGE_DIR_NAME_ENV, "")
    ).strip()

    if args.print_python:
        python_path = resolve_configured_python(config_path)
        if python_path is None:
            return 2
        print(python_path)
        return 0

    if args.script:
        return run_script(
            args.script,
            args.script_args,
            config_path,
            explicit_config=explicit_config,
            channel_package_dir_name=channel_package_dir_name,
        )

    while True:
        script = choose_script()
        if not script:
            return 0
        result = run_script(
            script,
            [],
            config_path,
            explicit_config=explicit_config,
            channel_package_dir_name=channel_package_dir_name,
        )
        if result != 0:
            print(f"\033[91m[launcher] 子菜单执行失败，返回码: {result}\033[0m", flush=True)
        print("[launcher] 已返回 Translate Project Launcher。", flush=True)
        print()


if __name__ == "__main__":
    raise SystemExit(main())
