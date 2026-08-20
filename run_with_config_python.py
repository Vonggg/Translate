from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
QUICK_CONFIG_SCRIPT = (PROJECT_ROOT / "快速配置.py").resolve()
SCRIPT_CHOICES = (
    ("0", "快速配置.py", "首次使用快速配置"),
    ("1", "resource_menu.py", "资源菜单"),
    ("2", "main.py", "主菜单"),
    ("3", "工具脚本.py", "工具脚本"),
)


def load_config() -> dict:
    config_path = PROJECT_ROOT / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"config.json not found: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict):
        raise ValueError(f"config.json root must be an object: {config_path}")
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


def resolve_configured_python() -> Path | None:
    try:
        return resolve_python(load_config())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[launcher] Python 配置不可用: {exc}", file=sys.stderr)
        print("[launcher] 请重新运行启动器并选择 0（首次使用快速配置）进行修复。", file=sys.stderr)
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a project script with python_executable from config.json.",
    )
    parser.add_argument(
        "script",
        nargs="?",
        help="Script to run, relative to this project. If omitted, show an entry menu.",
    )
    parser.add_argument("script_args", nargs=argparse.REMAINDER, help="Arguments passed to the target script.")
    parser.add_argument("--print-python", action="store_true", help="Only print resolved Python path and exit.")
    return parser.parse_args()


def choose_script() -> str | None:
    print("Translate Project Launcher")
    for key, script, label in SCRIPT_CHOICES:
        print(f"{key}. {label}: {PROJECT_ROOT / script}")
    print("q. 退出")
    print()

    valid = {key: script for key, script, _ in SCRIPT_CHOICES}
    while True:
        choice = input("请选择: ").strip().lower()
        if choice in ("q", "quit", "exit"):
            return None
        if choice in valid:
            return valid[choice]
        print("无效选择，请输入 0/1/2/3/q。")


def main() -> int:
    args = parse_args()

    if args.print_python:
        python_path = resolve_configured_python()
        if python_path is None:
            return 2
        print(python_path)
        return 0

    script = args.script or choose_script()
    if not script:
        return 0

    script_path = resolve_script(script)
    if script_path == QUICK_CONFIG_SCRIPT:
        python_path = Path(sys.executable)
    else:
        python_path = resolve_configured_python()
        if python_path is None:
            return 2
    command = [str(python_path), "-u", str(script_path), *args.script_args]
    print(f"[launcher] Python: {python_path}", flush=True)
    print(f"[launcher] Script: {script_path}", flush=True)
    try:
        return subprocess.run(command, cwd=str(PROJECT_ROOT), check=False).returncode
    except OSError as exc:
        print(f"[launcher] 无法启动 Python: {exc}", file=sys.stderr)
        if script_path != QUICK_CONFIG_SCRIPT:
            print("[launcher] 请选择 0（首次使用快速配置）检查 Python 环境。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
