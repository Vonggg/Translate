from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class CodexCLIError(RuntimeError):
    """Raised when a non-interactive Codex CLI request cannot be completed."""


def find_codex_cli() -> str | None:
    configured = os.environ.get("CODEX_CLI_EXECUTABLE", "").strip()
    if configured:
        configured_path = Path(configured).expanduser()
        if configured_path.is_file():
            return str(configured_path.resolve())

    executable = shutil.which("codex")
    if executable:
        return executable

    if os.name == "nt":
        candidates: list[Path] = []
        extension_root = Path.home() / ".vscode" / "extensions"
        candidates.extend(
            extension_root.glob("openai.chatgpt-*/bin/windows-*/codex.exe")
        )
        candidates.extend(
            extension_root.glob("openai.chatgpt-*/bin/windows-*/codex.EXE")
        )
        candidates.extend([
            Path.home() / ".codex" / "bin" / "codex.exe",
            Path(os.environ.get("APPDATA", "")) / "npm" / "codex.cmd",
        ])
        existing = [path for path in candidates if path.is_file()]
        if existing:
            existing.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            return str(existing[0].resolve())
    return None


def codex_cli_available() -> bool:
    return find_codex_cli() is not None


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_codex_process(
    command: list[str],
    prompt: str,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict[str, Any] = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **popen_kwargs)
    print(
        f"[Codex CLI] 已启动 PID={process.pid}，最长等待={timeout} 秒；超时将自动终止并停止后续流程。",
        flush=True,
    )
    try:
        stdout, stderr = process.communicate(input=prompt, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        _terminate_process_tree(process)
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def request_structured_output(
    *,
    model: str,
    reasoning_effort: str,
    system_prompt: str,
    user_content: str,
    output_schema: dict[str, Any],
    timeout: int,
    working_directory: Path,
) -> tuple[dict[str, Any], dict[str, int]]:
    executable = find_codex_cli()
    if not executable:
        raise CodexCLIError("找不到 codex CLI，请先安装并执行 codex login。")
    if not model.strip():
        raise CodexCLIError("codex_cli 模式未配置 model。")

    prompt = (
        "下面是一项纯文本数据处理任务。不要读取项目文件，不要调用工具，不要修改任何文件。\n\n"
        "任务规则：\n"
        f"{system_prompt.strip()}\n\n"
        "待处理内容：\n"
        f"{user_content}"
    )
    working_directory = working_directory.resolve()
    with tempfile.TemporaryDirectory(prefix="translate_codex_") as temp_dir_text:
        temp_dir = Path(temp_dir_text)
        schema_path = temp_dir / "output_schema.json"
        output_path = temp_dir / "output.json"
        schema_path.write_text(
            json.dumps(output_schema, ensure_ascii=False),
            encoding="utf-8",
        )
        command = [
            executable,
            "--ask-for-approval",
            "never",
            "--config",
            f'model_reasoning_effort="{reasoning_effort.strip() or "low"}"',
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model.strip(),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "--json",
            "--cd",
            str(working_directory),
            "-",
        ]
        try:
            completed = _run_codex_process(command, prompt, max(1, int(timeout)))
        except subprocess.TimeoutExpired as exc:
            raise CodexCLIError(f"Codex CLI 在 {timeout} 秒内未返回结果。") from exc
        except OSError as exc:
            raise CodexCLIError(f"无法启动 Codex CLI: {exc}") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "未知错误").strip()
            if len(detail) > 1500:
                detail = detail[-1500:]
            raise CodexCLIError(
                f"Codex CLI 返回码={completed.returncode}: {detail}"
            )
        if not output_path.is_file():
            raise CodexCLIError("Codex CLI 未生成结构化输出文件。")
        try:
            result = json.loads(output_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexCLIError(f"Codex CLI 结构化输出无法解析: {exc}") from exc
        if not isinstance(result, dict):
            raise CodexCLIError("Codex CLI 结构化输出不是 JSON 对象。")

        usage: dict[str, int] = {}
        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "turn.completed":
                continue
            raw_usage = event.get("usage")
            if isinstance(raw_usage, dict):
                usage = {
                    str(key): value
                    for key, value in raw_usage.items()
                    if isinstance(value, int)
                }
        return result, usage


def translation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "translation": {"type": "string"},
                    },
                    "required": ["id", "translation"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }


def single_translation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"translation": {"type": "string"}},
        "required": ["translation"],
        "additionalProperties": False,
    }


def field_selection_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "fields": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["fields"],
        "additionalProperties": False,
    }
