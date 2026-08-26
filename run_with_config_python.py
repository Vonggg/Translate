from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
QUICK_CONFIG_SCRIPT = (PROJECT_ROOT / "快速配置.py").resolve()
DEFAULT_CONFIG_PATH = (PROJECT_ROOT / "config.json").resolve()
ACTIVE_CONFIG_PATH_ENV = "TRANSLATE_CONFIG_PATH"
EXPLICIT_CONFIG_ENV = "TRANSLATE_CONFIG_EXPLICIT"
CHANNEL_PACKAGE_DIR_NAME_ENV = "TRANSLATE_CHANNEL_PACKAGE_DIR_NAME"
SCRIPT_CHOICES = (
    ("0", "快速配置.py", "首次使用快速配置"),
    ("1", "resource_menu.py", "资源菜单"),
    ("2", "main.py", "主菜单"),
    ("3", "工具脚本.py", "工具脚本"),
)


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
        print("无效选择，请输入 0/1/2/3/q。")


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
