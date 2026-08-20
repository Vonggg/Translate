from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from pipeline.codex_cli_provider import (
    CodexCLIError,
    _run_codex_process,
    find_codex_cli,
    request_structured_output,
    translation_schema,
)


class CodexCLIProviderTests(unittest.TestCase):
    def test_structured_request_uses_safe_noninteractive_flags_and_reads_usage(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: list[str], prompt: str, timeout: int) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured["input"] = prompt
            captured["timeout"] = timeout
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                json.dumps({"items": [{"id": 7, "translation": "开始"}]}),
                encoding="utf-8",
            )
            stdout = json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "output_tokens": 2},
            })
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "pipeline.codex_cli_provider.shutil.which",
            return_value=r"C:\Tools\codex.exe",
        ), patch(
            "pipeline.codex_cli_provider._run_codex_process",
            side_effect=fake_run,
        ):
            result, usage = request_structured_output(
                model="gpt-5.3-codex-spark",
                reasoning_effort="low",
                system_prompt="翻译为简体中文。",
                user_content='{"items":[{"id":7,"text":"Play"}]}',
                output_schema=translation_schema(),
                timeout=30,
                working_directory=Path(temp_dir),
            )

        command = captured["command"]
        self.assertIsInstance(command, list)
        self.assertLess(command.index("--ask-for-approval"), command.index("exec"))
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertIn("read-only", command)
        self.assertIn("gpt-5.3-codex-spark", command)
        self.assertEqual(result["items"][0]["translation"], "开始")
        self.assertEqual(usage, {"input_tokens": 10, "output_tokens": 2})

    def test_timeout_is_reported_as_codex_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "pipeline.codex_cli_provider.shutil.which",
            return_value=r"C:\Tools\codex.exe",
        ), patch(
            "pipeline.codex_cli_provider._run_codex_process",
            side_effect=subprocess.TimeoutExpired(["codex"], 7),
        ):
            with self.assertRaisesRegex(CodexCLIError, "7 秒内未返回"):
                request_structured_output(
                    model="gpt-5.3-codex-spark",
                    reasoning_effort="low",
                    system_prompt="筛选字段。",
                    user_content="field:m_Text",
                    output_schema=translation_schema(),
                    timeout=7,
                    working_directory=Path(temp_dir),
                )

    def test_timeout_terminates_the_started_process_tree(self) -> None:
        process = MagicMock()
        process.pid = 4321
        process.communicate.side_effect = subprocess.TimeoutExpired(["codex"], 1)
        with patch(
            "pipeline.codex_cli_provider.subprocess.Popen",
            return_value=process,
        ), patch(
            "pipeline.codex_cli_provider._terminate_process_tree",
        ) as terminate:
            with self.assertRaises(subprocess.TimeoutExpired):
                _run_codex_process(["codex"], "prompt", 1)

        terminate.assert_called_once_with(process)

    def test_finds_codex_bundled_in_vscode_extension_when_path_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            bundled = (
                home
                / ".vscode"
                / "extensions"
                / "openai.chatgpt-26.803.41515-win32-x64"
                / "bin"
                / "windows-x86_64"
                / "codex.exe"
            )
            bundled.parent.mkdir(parents=True)
            bundled.touch()
            with patch(
                "pipeline.codex_cli_provider.shutil.which",
                return_value=None,
            ), patch(
                "pipeline.codex_cli_provider.Path.home",
                return_value=home,
            ), patch.dict(
                "pipeline.codex_cli_provider.os.environ",
                {"CODEX_CLI_EXECUTABLE": "", "APPDATA": str(home / "AppData")},
            ):
                resolved = find_codex_cli()

        self.assertEqual(resolved, str(bundled.resolve()))


if __name__ == "__main__":
    unittest.main()
