from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline.resource_staging import (
    _write_split_parts_next_to_merged,
    _build_remote_downloads,
    inspect_and_download_catalog_resources,
    prepare_split_sync_outputs,
    prepare_unified_resource_source,
    print_final_addressables_sync_reminder,
    remote_resource_report_path,
    resource_source_map_path,
    restore_imported_resource_paths,
)
from support.config import load_config


class ResourceStagingTests(unittest.TestCase):
    def test_split_output_preserves_fixed_boundaries_and_verifies_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merged = root / "large.bundle"
            merged.write_bytes(b"ABCDEFGHI")
            rows = [
                {"name": "large.bundle.split0", "size": 4},
                {"name": "large.bundle.split1", "size": 4},
                {"name": "large.bundle.split2", "size": 2},
            ]

            outputs = _write_split_parts_next_to_merged(merged, rows)

            self.assertEqual(len(outputs), 3)
            self.assertEqual([path.stat().st_size for path in outputs], [4, 4, 1])
            self.assertEqual(b"".join(path.read_bytes() for path in outputs), merged.read_bytes())

    def test_split_output_rejects_shrink_across_original_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merged = root / "large.bundle"
            merged.write_bytes(b"ABCDEFGH")
            rows = [
                {"name": "large.bundle.split0", "size": 4},
                {"name": "large.bundle.split1", "size": 4},
                {"name": "large.bundle.split2", "size": 2},
            ]

            with self.assertRaisesRegex(RuntimeError, "跨越原分卷边界"):
                _write_split_parts_next_to_merged(merged, rows)
            self.assertFalse(any(root.glob("*.split_tmp")))

    def test_split_output_adds_minimum_required_part_without_exceeding_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            merged = root / "large.bundle"
            merged.write_bytes(b"ABCDEFGHIJKLM")
            rows = [
                {"name": "large.bundle.split0", "size": 4},
                {"name": "large.bundle.split1", "size": 4},
                {"name": "large.bundle.split2", "size": 2},
            ]

            outputs = _write_split_parts_next_to_merged(merged, rows)

            self.assertFalse(any(root.glob("*.split_tmp")))
            self.assertEqual(len(outputs), 4)
            self.assertEqual([path.stat().st_size for path in outputs], [4, 4, 4, 1])
            self.assertEqual(b"".join(path.read_bytes() for path in outputs), merged.read_bytes())

    def test_actual_download_modifies_source_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            catalog_path = game_root / "assets" / "aa" / "catalog.json"
            catalog_path.parent.mkdir(parents=True)
            remote_id = "https://cdn.example/game/Android/downloaded.bundle"
            catalog_path.write_text(
                json.dumps({"m_InternalIds": [remote_id]}),
                encoding="utf-8",
            )
            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_root_dir=project_root,
                project_name="demo",
                catalog_source_subpath=Path("game-name/game/assets/aa/catalog.json"),
                result_dir=tool_root / "workspace" / "output",
            )

            def fake_download(task, _timeout):
                task.destination.parent.mkdir(parents=True, exist_ok=True)
                task.destination.write_bytes(b"downloaded")
                return True, ""

            with patch(
                "pipeline.resource_staging._download_remote_file",
                side_effect=fake_download,
            ):
                self.assertTrue(inspect_and_download_catalog_resources(cfg))

            localized_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(
                localized_catalog["m_InternalIds"],
                [
                    "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
                    "/Android/downloaded.bundle"
                ],
            )
            report = json.loads(
                remote_resource_report_path(cfg).read_text(encoding="utf-8")
            )
            self.assertEqual(report["success_count"], 1)
            self.assertTrue(report["source_catalog_modified"])

    def test_staging_restore_and_split_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            project_dir = project_root / "demo"
            game_root = project_dir / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            android_root = game_root / "assets" / "aa" / "Android"

            data_root.mkdir(parents=True)
            android_root.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"data")
            (android_root / "remote.bundle.split0").write_bytes(b"AB")
            (android_root / "remote.bundle.split1").write_bytes(b"CD")
            (android_root / "downloaded.bundle").write_bytes(b"LOCAL")
            catalog_path = game_root / "assets" / "aa" / "catalog.json"
            remote_id = "https://cdn.example/game/Android/downloaded.bundle"
            catalog_path.write_text(
                json.dumps({"m_InternalIds": [remote_id]}),
                encoding="utf-8",
            )

            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_root_dir=project_root,
                project_name="demo",
                resource_source_subpath=Path("game-name/game/assets/bin/Data"),
                resource_managed_subpath=Path("game-name/game/assets/bin/Data/Managed"),
                catalog_source_subpath=Path("game-name/game/assets/aa/catalog.json"),
                resource_staging_root=tool_root / "workspace" / "input_sources",
                resource_input_root=tool_root / "workspace" / "input",
                result_dir=tool_root / "workspace" / "output",
                record_dir=tool_root / "workspace" / "records",
                log_dir=tool_root / "workspace" / "logs",
                import_overlay_dir=tool_root / "workspace" / "output" / "Font" / "SDF" / "ToImport",
                image_import_dir=tool_root / "workspace" / "output" / "Image" / "ToImport",
            )

            staging_root = prepare_unified_resource_source(cfg)
            self.assertEqual(staging_root, cfg.resource_staging_root)
            self.assertEqual((staging_root / "aa" / "Android" / "remote.bundle").read_bytes(), b"ABCD")
            self.assertTrue((android_root / "remote.bundle.split0").is_file())
            self.assertTrue((android_root / "remote.bundle.split1").is_file())
            localized_catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(
                localized_catalog["m_InternalIds"],
                [
                    "{UnityEngine.AddressableAssets.Addressables.RuntimePath}"
                    "/Android/downloaded.bundle"
                ],
            )

            backup_root = project_dir / "game-name" / "bak" / "aa_before_resource_export"
            backup_catalog = json.loads((backup_root / "catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(backup_catalog["m_InternalIds"], [remote_id])
            self.assertEqual((backup_root / "Android" / "downloaded.bundle").read_bytes(), b"LOCAL")

            remote_report = json.loads(
                remote_resource_report_path(cfg).read_text(encoding="utf-8")
            )
            self.assertEqual(remote_report["success_count"], 0)
            self.assertTrue(remote_report["source_catalog_modified"])
            self.assertEqual(remote_report["localized_internal_id_count"], 1)
            self.assertEqual(len(remote_report["localized_internal_ids"]), 1)
            self.assertEqual(
                remote_report["localized_internal_ids"][0]["remote_internal_id"],
                remote_id,
            )

            state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8"))
            self.assertEqual(state["state_version"], 2)
            self.assertIn("source_fingerprint", state)
            split_entries = [entry for entry in state["entries"] if entry.get("split_parts")]
            self.assertEqual(len(split_entries), 1)

            with (
                patch("pipeline.resource_staging.backup_game_addressables") as backup_mock,
                patch("pipeline.resource_staging.inspect_and_download_catalog_resources") as inspect_mock,
            ):
                reused_root = prepare_unified_resource_source(cfg)
            self.assertEqual(reused_root, staging_root)
            backup_mock.assert_not_called()
            inspect_mock.assert_not_called()

            (data_root / "globalgamemanagers").write_bytes(b"changed-source")
            with (
                patch("pipeline.resource_staging.backup_game_addressables") as backup_mock,
                patch(
                    "pipeline.resource_staging.inspect_and_download_catalog_resources",
                    return_value=True,
                ) as inspect_mock,
            ):
                rebuilt_root = prepare_unified_resource_source(cfg)
            self.assertEqual(rebuilt_root, staging_root)
            backup_mock.assert_called_once()
            inspect_mock.assert_called_once()
            self.assertEqual(
                (staging_root / "bin" / "Data" / "globalgamemanagers").read_bytes(),
                b"changed-source",
            )

            final_root = tool_root / "workspace" / "FinalResult"
            staged_bundle_result = final_root / "Bundle" / "Android" / "aa" / "Android" / "remote.bundle"
            staged_data_result = final_root / "bin" / "Data" / "globalgamemanagers"
            staged_bundle_result.parent.mkdir(parents=True)
            staged_data_result.parent.mkdir(parents=True)
            staged_bundle_result.write_bytes(b"WXYZ")
            staged_data_result.write_bytes(b"changed-data")

            restored = restore_imported_resource_paths(cfg, final_root)
            self.assertEqual((final_root / "Bundle" / "Android" / "remote.bundle").read_bytes(), b"WXYZ")
            self.assertEqual((final_root / "Data" / "globalgamemanagers").read_bytes(), b"changed-data")

            self.assertEqual(prepare_split_sync_outputs(cfg, final_root, restored), 1)
            split_root = final_root / "Bundle" / "Android"
            self.assertEqual((split_root / "remote.bundle.split0").read_bytes(), b"WX")
            self.assertEqual((split_root / "remote.bundle.split1").read_bytes(), b"YZ")
            self.assertFalse((split_root / "remote.bundle.split2").exists())
            self.assertFalse((split_root / "remote.bundle").exists())
            self.assertFalse((final_root / "SplitBundles").exists())

            relative_catalog = {"m_InternalIds": ["http/missing.bundle"]}
            downloads, unresolved = _build_remote_downloads(cfg, relative_catalog)
            self.assertEqual(downloads, [])
            self.assertEqual(len(unresolved), 1)

            full_url_catalog = {
                "m_InternalIds": ["https://cdn.example/game/Android/missing.bundle"]
            }
            downloads, unresolved = _build_remote_downloads(cfg, full_url_catalog)
            self.assertEqual(len(downloads), 1)
            self.assertEqual(unresolved, [])
            self.assertEqual(downloads[0].url, "https://cdn.example/game/Android/missing.bundle")

            reminder_output = io.StringIO()
            with redirect_stdout(reminder_output):
                print_final_addressables_sync_reminder(cfg, final_root)
            self.assertIn("远程资源文件已存在", reminder_output.getvalue())
            self.assertIn("1 个 catalog 远程路径", reminder_output.getvalue())

            remote_report["success_count"] = 2
            remote_resource_report_path(cfg).write_text(
                json.dumps(remote_report),
                encoding="utf-8",
            )
            reminder_output = io.StringIO()
            with redirect_stdout(reminder_output):
                print_final_addressables_sync_reminder(cfg, final_root)
            self.assertIn("本次实际下载 2 个", reminder_output.getvalue())


if __name__ == "__main__":
    unittest.main()
