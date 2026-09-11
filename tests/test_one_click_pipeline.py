from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import one_click_pipeline as one_click


class OneClickPipelineTests(unittest.TestCase):
    def _cfg(self, record_dir: Path, *, ai_field_review: bool = False):
        return SimpleNamespace(
            stage_record_dir=record_dir,
            enable_ai_field_review=ai_field_review,
        )

    def test_successful_flow_runs_export_all_main_all_and_both_image_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                calls.append((script.name, tuple(args)))
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(Path(temporary)))

        self.assertEqual(result, 0)
        self.assertEqual(calls[0], ("resource_menu.py", ("export-all",)))
        main_steps = [
            args[-1]
            for script, args in calls
            if script == "main.py" and args[:1] == ("--run-step",)
        ]
        self.assertEqual(main_steps, ["0", "2", "3", "4", "5", "6", "7", "8", "9", "10"])
        self.assertEqual(calls[-2], ("工具脚本.py", ("copy-all-images",)))
        self.assertEqual(calls[-1], ("工具脚本.py", ("split-sprite-atlases",)))

    def test_resume_from_step_nine_skips_earlier_completed_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                calls.append((script.name, tuple(args)))
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(
                    self._cfg(Path(temporary)),
                    start_at="9",
                )

        self.assertEqual(0, result)
        self.assertEqual(calls[0], ("main.py", ("--run-step", "9")))
        self.assertEqual(calls[1], ("main.py", ("--run-step", "10")))
        self.assertNotIn(("resource_menu.py", ("export-all",)), calls)
        self.assertEqual(calls[-2], ("工具脚本.py", ("copy-all-images",)))
        self.assertEqual(calls[-1], ("工具脚本.py", ("split-sprite-atlases",)))

    def test_export_failure_stops_before_main_steps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                calls.append(call)
                if call == ("resource_menu.py", ("export-all",)):
                    return 2
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(Path(temporary)))

        self.assertEqual(result, 2)
        self.assertEqual(calls, [("resource_menu.py", ("export-all",))])

    def test_unrecoverable_main_step_failure_stops_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                calls.append(call)
                return 5 if call == ("main.py", ("--run-step", "5")) else 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(Path(temporary)))

        self.assertEqual(result, 5)
        self.assertEqual(calls[-1], ("main.py", ("--run-step", "5")))
        self.assertNotIn(("main.py", ("--run-step", "6")), calls)

    def test_final_image_tool_failure_stops_before_next_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                calls.append(call)
                return 7 if call == ("工具脚本.py", ("copy-all-images",)) else 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(Path(temporary)))

        self.assertEqual(result, 7)
        self.assertEqual(calls[-1], ("工具脚本.py", ("copy-all-images",)))
        self.assertNotIn(("工具脚本.py", ("split-sprite-atlases",)), calls)

    def test_ai_failure_runs_retry_tool_and_retries_original_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            request = records / "ai_translation_request_batch_001.json"
            calls: list[tuple[str, tuple[str, ...]]] = []
            step_two_runs = 0

            def run(script: Path, args: list[str], _label: str) -> int:
                nonlocal step_two_runs
                call = (script.name, tuple(args))
                calls.append(call)
                if call == ("main.py", ("--run-step", "2")):
                    step_two_runs += 1
                    if step_two_runs == 1:
                        request.write_text("{}", encoding="utf-8")
                        return 1
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(records))

        self.assertEqual(result, 0)
        self.assertEqual(step_two_runs, 2)
        self.assertIn(
            ("工具脚本.py", ("retry-failed-ai-batches", str(request.resolve()))),
            calls,
        )

    def test_ai_recovery_failure_stops_before_next_main_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            request = records / "ai_translation_request_batch_001.json"
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                calls.append(call)
                if call == ("main.py", ("--run-step", "2")):
                    request.write_text("{}", encoding="utf-8")
                    return 1
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(records))

        self.assertEqual(result, 1)
        self.assertNotIn(("main.py", ("--run-step", "3")), calls)

    def test_ttf_failure_auto_cleans_rebuilds_and_retries_without_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            missing = records / one_click.MISSING_TTF_CHARS_FILENAME
            calls: list[tuple[str, tuple[str, ...]]] = []
            step_eight_runs = 0

            def run(script: Path, args: list[str], _label: str) -> int:
                nonlocal step_eight_runs
                call = (script.name, tuple(args))
                calls.append(call)
                if call == ("main.py", ("--run-step", "8")):
                    step_eight_runs += 1
                    if step_eight_runs == 1:
                        missing.write_text("★", encoding="utf-8")
                        return 1
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(records))

        self.assertEqual(result, 0)
        self.assertEqual(step_eight_runs, 2)
        cleaner_index = calls.index(
            ("工具脚本.py", ("clean-unsupported-ttf-chars-all",))
        )
        self.assertEqual(calls[cleaner_index + 1], ("main.py", ("--run-step", "3")))
        self.assertEqual(calls[cleaner_index + 2], ("main.py", ("--run-step", "4")))
        self.assertEqual(calls[cleaner_index + 3], ("main.py", ("--run-step", "8")))

    def test_ttf_cleanup_failure_stops_without_rebuilding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            records = Path(temporary)
            missing = records / one_click.MISSING_TTF_CHARS_FILENAME
            calls: list[tuple[str, tuple[str, ...]]] = []

            def run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                calls.append(call)
                if call == ("main.py", ("--run-step", "8")):
                    missing.write_text("★", encoding="utf-8")
                    return 1
                if call == ("工具脚本.py", ("clean-unsupported-ttf-chars-all",)):
                    return 9
                return 0

            with patch.object(one_click, "run_python_script", side_effect=run):
                result = one_click.run_one_click_pipeline(self._cfg(records))

        self.assertEqual(result, 9)
        self.assertEqual(
            calls[-1],
            ("工具脚本.py", ("clean-unsupported-ttf-chars-all",)),
        )

    def test_failed_run_resumes_at_first_incomplete_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / ".translate" / "run-state.json"
            config_path = state_path.with_name("config.json")
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"project_name":"Game-A"}', encoding="utf-8")
            cfg = self._cfg(root / "records")
            cfg.project_name = "Game-A"
            cfg.workspace_root = root / "workspaceGame-A"
            cfg.stage_record_dir.mkdir(parents=True)
            first_calls: list[tuple[str, tuple[str, ...]]] = []

            def first_run(script: Path, args: list[str], _label: str) -> int:
                call = (script.name, tuple(args))
                first_calls.append(call)
                return 5 if call == ("main.py", ("--run-step", "5")) else 0

            state = one_click._prepare_run_state(
                state_path,
                config_path=config_path,
                config_fingerprint=one_click._config_fingerprint(config_path),
                project_name=cfg.project_name,
                workspace_root=cfg.workspace_root,
                record_dir=cfg.stage_record_dir,
            )
            with patch.object(one_click, "run_python_script", side_effect=first_run):
                self.assertEqual(
                    5,
                    one_click.run_one_click_pipeline(
                        cfg,
                        state_path=state_path,
                        state=state,
                    ),
                )

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", saved["status"])
            self.assertIn("main-4", saved["completed_stages"])
            self.assertNotIn("main-5", saved["completed_stages"])

            second_calls: list[tuple[str, tuple[str, ...]]] = []

            def second_run(script: Path, args: list[str], _label: str) -> int:
                second_calls.append((script.name, tuple(args)))
                return 0

            resumed = one_click._prepare_run_state(
                state_path,
                config_path=config_path,
                config_fingerprint=one_click._config_fingerprint(config_path),
                project_name=cfg.project_name,
                workspace_root=cfg.workspace_root,
                record_dir=cfg.stage_record_dir,
            )
            with patch.object(one_click, "run_python_script", side_effect=second_run):
                self.assertEqual(
                    0,
                    one_click.run_one_click_pipeline(
                        cfg,
                        state_path=state_path,
                        state=resumed,
                    ),
                )

            self.assertEqual(
                ("main.py", ("--run-step", "5")),
                second_calls[0],
            )
            self.assertNotIn(("resource_menu.py", ("export-all",)), second_calls)
            completed = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual("completed", completed["status"])
            self.assertFalse(completed["resumable"])

    def test_config_change_invalidates_old_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / ".translate" / "run-state.json"
            config_path = state_path.with_name("config.json")
            config_path.parent.mkdir(parents=True)
            config_path.write_text('{"version":1}', encoding="utf-8")
            state = one_click._prepare_run_state(
                state_path,
                config_path=config_path,
                config_fingerprint=one_click._config_fingerprint(config_path),
                project_name="Game-A",
                workspace_root=root / "workspaceGame-A",
                record_dir=root / "records",
            )
            state["completed_stages"] = ["export", "main-0"]
            state["status"] = "failed"
            one_click._save_run_state(state_path, state)

            config_path.write_text('{"version":2}', encoding="utf-8")
            reset = one_click._prepare_run_state(
                state_path,
                config_path=config_path,
                config_fingerprint=one_click._config_fingerprint(config_path),
                project_name="Game-A",
                workspace_root=root / "workspaceGame-A",
                record_dir=root / "records",
            )
            self.assertEqual([], reset["completed_stages"])
            self.assertEqual("", reset["resumed_at"])


if __name__ == "__main__":
    unittest.main()
