from __future__ import annotations

import argparse
import copy
import getpass
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.json"
RENAMED_CONFIG_SOURCE = ROOT_DIR / "config（删除括弧后缀后运行快速配置）.json"
CODEX_TRANSLATION_MODEL = "gpt-5.3-codex-spark"
CODEX_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
MINIMUM_PYTHON_VERSION = (3, 9)
REQUIRED_PYTHON_MODULES = (
    "requests",
    "fontTools",
    "UnityPy",
    "yaml",
    "spookyhash",
    "lz4",
    "PIL",
)
PYTHON_PROBE_PREFIX = "__TRANSLATE_PYTHON_PROBE__="

SECRET_FIELDS = (
    "baidu_appid",
    "baidu_appkey",
    "ai_translation_api_key",
    "ai_field_review_api_key",
)
PROXY_FIELDS = (
    "google_proxy_http",
    "google_proxy_https",
    "ai_translation_proxy_http",
    "ai_translation_proxy_https",
    "ai_field_review_proxy_http",
    "ai_field_review_proxy_https",
)


@dataclass(frozen=True)
class ProjectLayout:
    resource_source_subpath: str
    resource_managed_subpath: str
    catalog_source_subpath: str
    stringliteral_json_subpath: str


@dataclass(frozen=True)
class ConfigCheck:
    label: str
    ok: bool
    required: bool
    detail: str


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        if path.resolve() == DEFAULT_CONFIG_PATH.resolve():
            raise FileNotFoundError(
                f"配置文件不存在: {path}\n"
                f"首次使用前请先手动将 {RENAMED_CONFIG_SOURCE.name} 重命名为 config.json"
            )
        raise FileNotFoundError(f"配置文件不存在: {path}")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"配置根节点必须是对象: {path}")
    return value


def _path_text(path: Path) -> str:
    return path.resolve().as_posix()


def _resolve_from_root(root: Path, raw: object) -> Path:
    path = Path(str(raw or "").strip().strip('"'))
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _probe_python(path: Path) -> tuple[bool, str]:
    """Check that an interpreter can start and import all project dependencies."""
    path = path.resolve()
    if not path.is_file():
        return False, f"文件不存在: {path}"

    probe_code = (
        "import importlib,json,sys\n"
        f"modules={REQUIRED_PYTHON_MODULES!r}\n"
        "errors={}\n"
        "for name in modules:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception as exc:\n"
        "        errors[name]=f'{type(exc).__name__}: {exc}'\n"
        f"print({PYTHON_PROBE_PREFIX!r}+json.dumps({{'version':list(sys.version_info[:3]),'errors':errors}}, ensure_ascii=False))\n"
    )
    try:
        completed = subprocess.run(
            [str(path), "-c", probe_code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法启动: {path} ({exc})"

    payload: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(PYTHON_PROBE_PREFIX):
            try:
                decoded = json.loads(line[len(PYTHON_PROBE_PREFIX):])
            except json.JSONDecodeError:
                break
            if isinstance(decoded, dict):
                payload = decoded
            break
    if completed.returncode != 0 or payload is None:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"退出码 {completed.returncode}"
        return False, f"解释器探测失败: {detail[-500:]}"

    version = tuple(int(part) for part in payload.get("version", (0, 0, 0))[:3])
    if version < MINIMUM_PYTHON_VERSION:
        required = ".".join(str(part) for part in MINIMUM_PYTHON_VERSION)
        actual = ".".join(str(part) for part in version)
        return False, f"Python 版本过低: {actual}，项目要求 >= {required}"

    errors = payload.get("errors", {})
    if isinstance(errors, dict) and errors:
        missing = ", ".join(str(name) for name in errors)
        return False, f"缺少或无法导入项目依赖: {missing}"
    return True, f"Python {'.'.join(str(part) for part in version)}，项目依赖完整"


def _python_environment_candidates(root: Path = ROOT_DIR) -> list[Path]:
    candidates: list[Path] = []
    for base in (root, root.parent):
        for directory in (".venv", "venv"):
            candidates.append(base / directory / "Scripts" / "python.exe")
            candidates.append(base / directory / "bin" / "python")
    candidates.append(Path(sys.executable))
    return _unique_paths(candidates)


def choose_default_python(
    config: dict[str, Any],
    explicit: str | None = None,
    *,
    root: Path = ROOT_DIR,
) -> str:
    """Prefer an explicit path, then a valid saved environment, then discovered venvs."""
    if explicit:
        return str(_resolve_from_root(root, explicit))

    configured = str(config.get("python_executable", "") or "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(_resolve_from_root(root, configured))
    candidates.extend(_python_environment_candidates(root))

    for candidate in _unique_paths(candidates):
        ok, _ = _probe_python(candidate)
        if ok:
            return str(candidate)
    return str(Path(sys.executable).resolve())


def _unique_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path.resolve())
    return result


def _configured_project_dir(config: dict[str, Any]) -> Path | None:
    root = str(config.get("project_root_dir", "") or "").strip()
    name = str(config.get("project_name", "") or "").strip()
    if not root:
        return None
    return (Path(root) / name).resolve() if name else Path(root).resolve()


def detect_project_layout(
    project_dir: Path,
    config: dict[str, Any],
    *,
    assume_yes: bool = True,
) -> ProjectLayout:
    del project_dir, config, assume_yes
    return ProjectLayout(
        resource_source_subpath="game-name/game/assets/bin/Data",
        resource_managed_subpath="game-name/game/assets/bin/Data/Managed",
        catalog_source_subpath="game-name/game/assets/aa/catalog.json",
        stringliteral_json_subpath="game-name/bak/64/stringliteral.json",
    )


def sanitize_template(config: dict[str, Any]) -> dict[str, Any]:
    """Return a portable template without local paths or credentials."""
    result = copy.deepcopy(config)
    result.update(
        {
            "project_root_dir": "",
            "project_name": "",
            "resource_source_subpath": "game-name/game/assets/bin/Data",
            "resource_managed_subpath": "game-name/game/assets/bin/Data/Managed",
            "catalog_source_subpath": "game-name/game/assets/aa/catalog.json",
            "stringliteral_json_subpath": "game-name/bak/64/stringliteral.json",
            "python_executable": "",
            "unity_exe": "auto",
            "ttf_template_path": "templates/fzkt.ttf",
            "enable_ai_translation": False,
            "enable_ai_field_review": False,
            "ai_translation_transport": "codex_cli",
            "ai_translation_codex_model": CODEX_TRANSLATION_MODEL,
            "ai_translation_codex_reasoning_effort": "low",
            "include_old_sdf_template_chars": False,
            "protect_i2_tmp_fonts_from_replacement": True,
            "ai_translation_strategy": "default",
            "ai_translation_base_url": "",
            "ai_translation_model": "",
            "ai_field_review_base_url": "",
        }
    )
    for field in (*SECRET_FIELDS, *PROXY_FIELDS):
        result[field] = ""
    return result


def _prompt_value(
    label: str,
    default: str,
    validator: Callable[[str], tuple[bool, str]],
) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip().strip('"')
        value = raw or default
        ok, message = validator(value)
        if ok:
            return value
        print(f"  无效: {message}")


def _valid_directory(raw: str) -> tuple[bool, str]:
    path = Path(raw).resolve()
    return path.is_dir(), f"目录不存在: {path}"


def _valid_file_or_auto(raw: str) -> tuple[bool, str]:
    if raw.casefold() == "auto":
        return True, ""
    path = Path(raw).resolve()
    return path.is_file(), f"文件不存在: {path}"


def _valid_python(raw: str) -> tuple[bool, str]:
    return _probe_python(Path(raw))


def _python_executable_from_environment(
    raw: str,
    *,
    root: Path = ROOT_DIR,
) -> Path:
    path = _resolve_from_root(root, raw)
    if path.is_dir():
        candidates = (
            path / "Scripts" / "python.exe",
            path / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        return candidates[0].resolve()
    return path.resolve()


def _prompt_python_environment() -> str:
    print(
        "虚拟 Python 环境仅供 run_with_config_python.py 切换解释器；"
        "留空时使用启动器自身的当前 Python。"
    )
    while True:
        raw = input(
            "虚拟 Python 环境（可留空；填写环境目录或 python.exe）: "
        ).strip().strip('"')
        if not raw:
            return ""
        executable = _python_executable_from_environment(raw)
        ok, message = _probe_python(executable)
        if ok:
            return _path_text(executable)
        print(f"  无效: {message}")


def _prompt_translation(config: dict[str, Any]) -> None:
    print("\n请选择 AI 自动翻译方式；也可以跳过并稍后修改 config.json。")
    print(f"  0. 跳过，保留当前设置（当前 AI 翻译={'开启' if config.get('enable_ai_translation') else '关闭'}）")
    print(
        f"  1. Codex CLI / {CODEX_TRANSLATION_MODEL}（使用 Codex 额度；失败时回退已配置的 HTTP AI）"
    )
    print("  2. 自定义 OpenAI-compatible API（配置 Base URL、模型和 API Key）")
    print("  3. 关闭 AI 自动翻译并清空全部翻译密钥")
    choice = input("请选择 [0]: ").strip() or "0"
    if choice == "0":
        return
    if choice == "1":
        current_effort = str(config.get("ai_translation_codex_reasoning_effort", "low") or "low").casefold()
        if current_effort not in CODEX_REASONING_EFFORTS:
            current_effort = "low"
        print("推理强度控制思考深度；越高通常越慢、额度消耗也越多，纯文本翻译推荐 low。")
        while True:
            effort = input(
                f"推理强度 low/medium/high/xhigh [{current_effort}]: "
            ).strip().casefold() or current_effort
            if effort in CODEX_REASONING_EFFORTS:
                break
            print("  无效：请输入 low、medium、high 或 xhigh。")
        config["enable_ai_translation"] = True
        config["ai_translation_transport"] = "codex_cli"
        config["ai_translation_codex_model"] = CODEX_TRANSLATION_MODEL
        config["ai_translation_codex_reasoning_effort"] = effort
        http_fallback_ready = all(
            str(config.get(field, "") or "").strip()
            for field in (
                "ai_translation_base_url",
                "ai_translation_model",
                "ai_translation_api_key",
            )
        )
        print(
            "HTTP AI 回退已启用。"
            if http_fallback_ready
            else "HTTP AI 回退尚未完整配置；可先选择 2 配置接口，再重新选择 1 作为主通道。"
        )
        return
    if choice == "2":
        old_base_url = str(config.get("ai_translation_base_url", "") or "").strip()
        old_model = str(config.get("ai_translation_model", "") or "").strip()
        old_api_key = str(config.get("ai_translation_api_key", "") or "").strip()
        base_url = input(f"Base URL{f' [{old_base_url}]' if old_base_url else ''}: ").strip() or old_base_url
        model = input(f"模型{f' [{old_model}]' if old_model else ''}: ").strip() or old_model
        api_key = getpass.getpass(
            f"API Key（{'回车保留现值' if old_api_key else '必填'}）: "
        ).strip() or old_api_key
        missing = [
            label
            for label, value in (("Base URL", base_url), ("模型", model), ("API Key", api_key))
            if not value
        ]
        if missing:
            print(f"自定义 API 配置不完整，未启用：缺少 {', '.join(missing)}。")
            return
        config["ai_translation_base_url"] = base_url
        config["ai_translation_model"] = model
        config["ai_translation_api_key"] = api_key
        config["enable_ai_translation"] = True
        config["ai_translation_transport"] = "http"
        return
    if choice == "3":
        config["enable_ai_translation"] = False
        config["enable_ai_field_review"] = False
        for field in SECRET_FIELDS:
            config[field] = ""
        return
    print("未识别该选项，保持当前翻译设置。")


def build_core_updates(
    project_dir: Path,
    layout: ProjectLayout,
    *,
    python_executable: str,
    unity_exe: str,
) -> dict[str, Any]:
    project_dir = project_dir.resolve()
    return {
        "project_root_dir": project_dir.as_posix(),
        "project_name": "",
        "resource_source_subpath": layout.resource_source_subpath,
        "resource_managed_subpath": layout.resource_managed_subpath,
        "catalog_source_subpath": layout.catalog_source_subpath,
        "stringliteral_json_subpath": layout.stringliteral_json_subpath,
        "python_executable": python_executable,
        "unity_exe": unity_exe,
        "ttf_template_path": "templates/fzkt.ttf",
    }


def validate_config(config: dict[str, Any], root: Path = ROOT_DIR) -> list[ConfigCheck]:
    project_dir = _configured_project_dir(config)
    python_raw = str(config.get("python_executable", "") or "").strip()
    python_path = _resolve_from_root(root, python_raw) if python_raw else None
    if python_path is None:
        python_ok = True
        python_detail = "留空；run_with_config_python.py 将使用启动器当前 Python"
    else:
        python_ok, python_detail = _probe_python(python_path)
    unity_raw = str(config.get("unity_exe", "auto") or "auto").strip()
    font_path = _resolve_from_root(root, config.get("ttf_template_path", "fzkt.ttf"))
    unity_project = _resolve_from_root(root, config.get("unity_font_project", "TMP_Font_Generator"))
    return [
        ConfigCheck("adbuybox 目录", bool(project_dir and project_dir.is_dir()), True, str(project_dir or "<未配置>")),
        ConfigCheck(
            "虚拟 Python 环境",
            python_ok,
            True,
            f"{python_path} ({python_detail})" if python_path else python_detail,
        ),
        ConfigCheck("模板 TTF", font_path.is_file(), True, str(font_path)),
        ConfigCheck("Unity", unity_raw.casefold() == "auto" or Path(unity_raw).is_file(), True, unity_raw),
        ConfigCheck("Unity 字体辅助工程", unity_project.is_dir(), True, str(unity_project)),
    ]


def print_checks(checks: list[ConfigCheck]) -> None:
    print("\n配置检查:")
    for check in checks:
        status = "通过" if check.ok else ("失败" if check.required else "可选缺失")
        print(f"  [{status}] {check.label}: {check.detail}")


def _backup_path(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.backup-{stamp}")
    index = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.backup-{stamp}-{index}")
        index += 1
    return candidate


def write_config_with_backup(path: Path, config: dict[str, Any]) -> Path | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.is_file():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
    temporary = path.with_name(f".{path.name}.tmp")
    _write_json_lf(temporary, config)
    temporary.replace(path)
    return backup


def _write_json_lf(path: Path, value: dict[str, Any]) -> None:
    """Write stable UTF-8 JSON without Windows newline conversion."""
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=4) + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Translate 项目首次使用快速配置")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="要生成或检查的 config.json")
    parser.add_argument(
        "--template",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="可选的基础配置文件；默认直接使用 config.json",
    )
    parser.add_argument(
        "--from-template",
        action="store_true",
        help="从 --template 指定的基础配置重新配置；未指定时仍直接使用 config.json",
    )
    parser.add_argument(
        "--project-dir",
        type=Path,
        help="adbuybox 目录（game-name 的上一级目录）",
    )
    parser.add_argument(
        "--python-executable",
        help="虚拟 Python 环境目录或 Python 可执行文件；省略则留空",
    )
    parser.add_argument("--unity-exe", help="Unity.exe 路径或 auto")
    parser.add_argument("--check", action="store_true", help="只检查现有配置，不写文件")
    parser.add_argument("--sanitize-template", action="store_true", help="将模板原地清理为无密钥、无本机路径的通用模板")
    parser.add_argument("-y", "--yes", action="store_true", help="接受自动探测结果，不进入交互确认")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    config_path = args.config.resolve()
    template_path = args.template.resolve()

    if args.sanitize_template:
        template = sanitize_template(_read_json(template_path))
        _write_json_lf(template_path, template)
        print(f"已生成通用安全模板: {template_path}")
        return 0

    if args.check:
        checks = validate_config(_read_json(config_path))
        print_checks(checks)
        return 0 if all(check.ok for check in checks if check.required) else 1

    base_path = template_path if args.from_template or not config_path.is_file() else config_path
    config = _read_json(base_path)
    print("Translate 首次使用快速配置")
    print(f"基础配置: {base_path}")

    current_project = _configured_project_dir(config)
    project_default = str(current_project) if current_project and current_project.is_dir() else ""
    if args.project_dir:
        project_dir = args.project_dir.resolve()
        if not project_dir.is_dir():
            raise FileNotFoundError(f"adbuybox 目录不存在: {project_dir}")
    elif args.yes:
        if not project_default:
            raise ValueError("-y 模式必须提供 --project-dir，或现有配置中的项目目录必须有效")
        project_dir = Path(project_default).resolve()
    else:
        project_dir = Path(
            _prompt_value(
                "adbuybox 目录（game-name 的上一级目录）",
                project_default,
                _valid_directory,
            )
        ).resolve()

    layout = detect_project_layout(project_dir, config, assume_yes=args.yes)
    default_unity = args.unity_exe or str(config.get("unity_exe", "auto") or "auto")
    if default_unity.casefold() != "auto" and not Path(default_unity).is_file():
        default_unity = "auto"

    if args.yes:
        python_executable = (
            _path_text(_python_executable_from_environment(args.python_executable))
            if args.python_executable
            else ""
        )
        unity_exe = default_unity
    else:
        python_executable = _prompt_python_environment()
        unity_exe = _prompt_value("Unity.exe 路径或 auto", default_unity, _valid_file_or_auto)

    config.update(
        build_core_updates(
            project_dir,
            layout,
            python_executable=python_executable,
            unity_exe="auto" if unity_exe.casefold() == "auto" else _path_text(Path(unity_exe)),
        )
    )

    if not args.yes:
        _prompt_translation(config)

    print("\n即将写入的核心配置:")
    for key in (
        "project_root_dir", "project_name", "resource_source_subpath",
        "resource_managed_subpath", "catalog_source_subpath",
        "stringliteral_json_subpath", "python_executable", "unity_exe",
    ):
        print(f"  {key}: {config.get(key, '')}")
    project_name = str(config.get("project_name", "") or "").strip()
    print(f"  实际工作区目录: {ROOT_DIR / f'workspace{project_name}'}")
    checks = validate_config(config)
    print_checks(checks)
    if not all(check.ok for check in checks if check.required):
        print("\n存在必填项错误，未写入配置。")
        return 1
    if not args.yes:
        confirm = input(f"\n写入 {config_path}？[Y/n]: ").strip().casefold()
        if confirm in {"n", "no"}:
            print("已取消，配置未修改。")
            return 0

    backup = write_config_with_backup(config_path, config)
    if backup:
        print(f"原配置备份: {backup}")
    print(f"配置已写入: {config_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
