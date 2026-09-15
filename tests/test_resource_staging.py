from __future__ import annotations

import json
import io
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline.resource_staging import (
    _write_split_parts_next_to_merged,
    _build_remote_downloads,
    finalize_obb_outputs,
    inspect_and_download_catalog_resources,
    load_prepared_resource_source,
    prepare_split_sync_outputs,
    prepare_unified_resource_source,
    print_final_addressables_sync_reminder,
    remote_resource_report_path,
    resource_source_map_path,
    restore_imported_resource_paths,
)
from support.config import load_config


class ResourceStagingTests(unittest.TestCase):
    def test_pad_only_staging_invalidation_and_original_path_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            game = root / "projects" / "demo" / "game-name" / "game"
            pad = game / "assets" / "assetpack"
            (pad / "nested").mkdir(parents=True)
            source = pad / "nested" / "clothes"
            source.write_bytes(b"UnityFS-test")
            cfg = replace(load_config(), root_dir=root / "tool",
                project_root_dir=root / "projects", project_name="demo",
                resource_source_subpath=Path("game-name/game/assets/bin/Data"),
                catalog_source_subpath=Path("game-name/game/assets/aa/catalog.json"),
                resource_staging_root=root / "staging")
            with patch("pipeline.resource_staging.inspect_and_download_catalog_resources", return_value=True):
                staged = prepare_unified_resource_source(cfg)
                self.assertEqual((staged / "assetpack/nested/clothes").read_bytes(), b"UnityFS-test")
                with patch("pipeline.resource_staging._copy_source_tree", side_effect=AssertionError("must reuse")):
                    self.assertEqual(prepare_unified_resource_source(cfg), staged)
                source.write_bytes(b"UnityFS-new-content")
                self.assertEqual(prepare_unified_resource_source(cfg), staged)
            self.assertEqual((staged / "assetpack/nested/clothes").read_bytes(), source.read_bytes())
            state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8"))
            entry = next(e for e in state["entries"] if e["category"] == "assetpack")
            self.assertEqual(Path(entry["source_relative_game"]), Path("assets/assetpack/nested/clothes"))
            raw, final = root / "raw", root / "final"
            (raw / "assetpack/nested").mkdir(parents=True)
            (raw / "assetpack/nested/clothes").write_bytes(b"translated")
            restore_imported_resource_paths(cfg, final, raw)
            self.assertEqual((final / "assetpack/nested/clothes").read_bytes(), b"translated")
            self.assertFalse((final / "Data/nested/clothes").exists())
            self.assertEqual(source.read_bytes(), b"UnityFS-new-content")

    def test_old_absolute_staging_map_rebases_to_project_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            old_staging_root = tool_root / "workspace" / "input_sources"
            current_staging_root = tool_root / "workspaceDemo" / "input_sources"
            staged_relative = Path("aa") / "Android" / "demo.bundle"
            current_file = current_staging_root / staged_relative
            current_file.parent.mkdir(parents=True)
            current_file.write_bytes(b"bundle")

            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_name="Demo",
                resource_staging_root=current_staging_root,
            )
            map_path = resource_source_map_path(cfg)
            map_path.parent.mkdir(parents=True)
            map_path.write_text(
                json.dumps(
                    {
                        "state_version": 2,
                        "staging_root": str(old_staging_root),
                        "entries": [
                            {
                                "staged_relative": str(staged_relative),
                                "staged_path": str(old_staging_root / staged_relative),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                resolved = load_prepared_resource_source(cfg)

            self.assertEqual(resolved, current_staging_root)
            migrated = json.loads(map_path.read_text(encoding="utf-8"))
            self.assertEqual(Path(migrated["staging_root"]), current_staging_root)
            self.assertEqual(
                Path(migrated["entries"][0]["staged_path"]),
                current_file,
            )
            self.assertIn("旧工作区路径映射迁移到当前项目", output.getvalue())

    def test_project_workspace_does_not_fall_back_to_other_workspace_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            old_staging_root = tool_root / "workspace" / "input_sources"
            old_staging_root.mkdir(parents=True)
            (old_staging_root / "old.bundle").write_bytes(b"other-project")
            current_staging_root = tool_root / "workspaceDemo" / "input_sources"

            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_name="Demo",
                resource_staging_root=current_staging_root,
            )
            map_path = resource_source_map_path(cfg)
            map_path.parent.mkdir(parents=True)
            map_path.write_text(
                json.dumps(
                    {
                        "state_version": 2,
                        "staging_root": str(old_staging_root),
                        "entries": [],
                    }
                ),
                encoding="utf-8",
            )

            output = io.StringIO()
            with redirect_stdout(output):
                resolved = load_prepared_resource_source(cfg)

            self.assertIsNone(resolved)
            self.assertIn(str(current_staging_root), output.getvalue())
            self.assertNotIn("旧工作区路径映射迁移到当前项目", output.getvalue())

    def test_failed_obb_rebuild_invalidates_previous_resource_map(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            data_root.mkdir(parents=True)
            source_data = data_root / "globalgamemanagers"
            source_data.write_bytes(b"initial data")

            source_obb = (
                game_root
                / "assets"
                / "obb"
                / "com.example.game"
                / "main.1.com.example.game.obb"
            )
            source_obb.parent.mkdir(parents=True)
            with zipfile.ZipFile(source_obb, "w") as archive:
                archive.writestr("assets/aa/Android/content.bundle", b"bundle")

            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_root_dir=project_root,
                project_name="demo",
                resource_source_subpath=Path("game-name/game/assets/bin/Data"),
                resource_managed_subpath=Path(
                    "game-name/game/assets/bin/Data/Managed"
                ),
                catalog_source_subpath=Path(
                    "game-name/game/assets/aa/catalog.json"
                ),
                resource_staging_root=tool_root / "workspace" / "input_sources",
                resource_input_root=tool_root / "workspace" / "input",
                result_dir=tool_root / "workspace" / "output",
                record_dir=tool_root / "workspace" / "records",
                log_dir=tool_root / "workspace" / "logs",
            )

            prepared = prepare_unified_resource_source(cfg)
            self.assertEqual(prepared, cfg.resource_staging_root)
            map_path = resource_source_map_path(cfg)
            self.assertTrue(map_path.is_file())
            self.assertEqual(load_prepared_resource_source(cfg), prepared)

            source_data.write_bytes(b"changed data invalidates the fingerprint")
            with patch(
                "pipeline.resource_staging.extract_obb_resources",
                side_effect=RuntimeError("simulated OBB extraction failure"),
            ):
                self.assertIsNone(prepare_unified_resource_source(cfg))

            reloaded = load_prepared_resource_source(cfg)
            self.assertEqual(
                (map_path.exists(), reloaded),
                (False, None),
                "OBB 重建失败后必须删除旧路径映射，不能把半成品暂存区当作可复用导出源",
            )

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
            self.assertEqual(state["state_version"], 6)
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
            staged_bundle_result = final_root / "aa" / "Android" / "remote.bundle"
            staged_data_result = final_root / "bin" / "Data" / "globalgamemanagers"
            staged_bundle_result.parent.mkdir(parents=True)
            staged_data_result.parent.mkdir(parents=True)
            staged_bundle_result.write_bytes(b"WXYZ")
            staged_data_result.write_bytes(b"changed-data")

            restored = restore_imported_resource_paths(cfg, final_root)
            self.assertEqual((final_root / "aa" / "Android" / "remote.bundle").read_bytes(), b"WXYZ")
            self.assertEqual((final_root / "Data" / "globalgamemanagers").read_bytes(), b"changed-data")

            self.assertEqual(prepare_split_sync_outputs(cfg, final_root, restored), 1)
            split_root = final_root / "aa" / "Android"
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

    def test_obb_resources_are_namespaced_and_rebuilt_to_final_obb(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            data_root.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"outer data")

            obb_relative = Path("com.example.game") / "main.1.com.example.game.obb"
            source_obb = game_root / "assets" / "obb" / obb_relative
            source_obb.parent.mkdir(parents=True)
            with zipfile.ZipFile(source_obb, "w") as archive:
                archive.writestr("assets/aa/Android/content.bundle", b"old bundle")
                archive.writestr("assets/bin/Data/inside.assets", b"inside data")
                archive.writestr("assets/unrelated.txt", b"keep me")

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
            )

            staging_root = prepare_unified_resource_source(cfg)
            prefix = (
                Path("obb")
                / "com.example.game"
                / "main.1.com.example.game.obb.contents"
            )
            self.assertEqual(
                (staging_root / prefix / "aa" / "Android" / "content.bundle").read_bytes(),
                b"old bundle",
            )
            self.assertEqual(
                (staging_root / prefix / "bin" / "Data" / "inside.assets").read_bytes(),
                b"inside data",
            )

            state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8"))
            self.assertEqual(state["state_version"], 6)
            self.assertEqual(state["obb_container_count"], 1)
            obb_entries = [
                entry for entry in state["entries"] if entry.get("origin_kind") == "obb"
            ]
            self.assertEqual(len(obb_entries), 2)
            self.assertTrue(all(entry.get("archive_entry") for entry in obb_entries))

            raw_root = tool_root / "workspace" / "temp" / "import_result_raw"
            modified = raw_root / prefix / "aa" / "Android" / "content.bundle"
            modified.parent.mkdir(parents=True)
            modified.write_bytes(b"new bundle")
            final_root = tool_root / "workspace" / "FinalResult"

            restored = restore_imported_resource_paths(cfg, final_root, raw_root)
            self.assertIn(str(prefix / "aa" / "Android" / "content.bundle"), restored)
            self.assertEqual(finalize_obb_outputs(cfg, final_root, raw_root), 1)

            output_obb = final_root / "obb" / obb_relative
            with zipfile.ZipFile(output_obb, "r") as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(
                    archive.read("assets/aa/Android/content.bundle"),
                    b"new bundle",
                )
                self.assertEqual(
                    archive.read("assets/bin/Data/inside.assets"),
                    b"inside data",
                )
                self.assertEqual(archive.read("assets/unrelated.txt"), b"keep me")
            self.assertFalse((final_root / "Bundle").exists())
            self.assertFalse((raw_root / prefix).exists())

    def test_obb_split_growth_adds_new_archive_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            data_root.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"outer data")

            obb_relative = Path("game") / "main.1.demo.obb"
            source_obb = game_root / "assets" / "obb" / obb_relative
            source_obb.parent.mkdir(parents=True)
            with zipfile.ZipFile(source_obb, "w") as archive:
                archive.writestr("assets/aa/Android/large.bundle.split0", b"ABCD")
                archive.writestr("assets/aa/Android/large.bundle.split1", b"EF")

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
            )

            staging_root = prepare_unified_resource_source(cfg)
            self.assertIsNotNone(staging_root)
            assert staging_root is not None
            prefix = Path("obb") / "game" / "main.1.demo.obb.contents"
            staged_relative = prefix / "aa" / "Android" / "large.bundle"
            self.assertEqual((staging_root / staged_relative).read_bytes(), b"ABCDEF")

            raw_root = tool_root / "workspace" / "temp" / "import_result_raw"
            modified = raw_root / staged_relative
            modified.parent.mkdir(parents=True)
            modified.write_bytes(b"ABCDEFGHIJ")
            final_root = tool_root / "workspace" / "FinalResult"
            restored = restore_imported_resource_paths(cfg, final_root, raw_root)
            self.assertEqual(prepare_split_sync_outputs(cfg, final_root, restored), 1)
            self.assertEqual(finalize_obb_outputs(cfg, final_root, raw_root), 1)

            output_obb = final_root / "obb" / obb_relative
            with zipfile.ZipFile(output_obb, "r") as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "assets/aa/Android/large.bundle.split0",
                        "assets/aa/Android/large.bundle.split1",
                        "assets/aa/Android/large.bundle.split2",
                    ],
                )
                self.assertEqual(
                    b"".join(
                        archive.read(f"assets/aa/Android/large.bundle.split{index}")
                        for index in range(3)
                    ),
                    b"ABCDEFGHIJ",
                )

    def test_multi_obb_failure_does_not_publish_partial_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            data_root.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"outer data")

            obb_root = game_root / "assets" / "obb"
            relatives = [Path("base") / "main.1.demo.obb", Path("patch") / "main.1.demo.obb"]
            for index, relative in enumerate(relatives):
                source_obb = obb_root / relative
                source_obb.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(source_obb, "w") as archive:
                    archive.writestr("assets/aa/Android/content.bundle", f"old-{index}".encode())

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
            )
            self.assertIsNotNone(prepare_unified_resource_source(cfg))

            raw_root = tool_root / "workspace" / "temp" / "import_result_raw"
            for index, relative in enumerate(relatives):
                prefix = Path("obb") / relative.parent / f"{relative.name}.contents"
                modified = raw_root / prefix / "aa" / "Android" / "content.bundle"
                modified.parent.mkdir(parents=True, exist_ok=True)
                modified.write_bytes(f"new-{index}".encode())
            final_root = tool_root / "workspace" / "FinalResult"
            restore_imported_resource_paths(cfg, final_root, raw_root)

            (obb_root / relatives[1]).write_bytes(b"source changed after export")
            with self.assertRaisesRegex(RuntimeError, "源 OBB 已变化"):
                finalize_obb_outputs(cfg, final_root, raw_root)
            final_obb_root = final_root / "obb"
            self.assertFalse(
                final_obb_root.exists() and any(final_obb_root.rglob("*.obb"))
            )

    def test_same_archive_entry_in_multiple_obbs_uses_isolated_staging_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tool_root = root / "tool"
            project_root = root / "projects"
            game_root = project_root / "demo" / "game-name" / "game"
            data_root = game_root / "assets" / "bin" / "Data"
            data_root.mkdir(parents=True)
            (data_root / "globalgamemanagers").write_bytes(b"outer data")

            archive_entry = "assets/aa/Android/shared.bundle"
            obb_root = game_root / "assets" / "obb"
            first_relative = Path("base") / "main.1.demo.obb"
            second_relative = Path("patch") / "main.1.demo.obb"
            for relative, content in (
                (first_relative, b"base bundle"),
                (second_relative, b"patch bundle"),
            ):
                source_obb = obb_root / relative
                source_obb.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(source_obb, "w") as archive:
                    archive.writestr(archive_entry, content)

            cfg = replace(
                load_config(),
                root_dir=tool_root,
                project_root_dir=project_root,
                project_name="demo",
                resource_source_subpath=Path("game-name/game/assets/bin/Data"),
                resource_managed_subpath=Path(
                    "game-name/game/assets/bin/Data/Managed"
                ),
                catalog_source_subpath=Path(
                    "game-name/game/assets/aa/catalog.json"
                ),
                resource_staging_root=tool_root / "workspace" / "input_sources",
                resource_input_root=tool_root / "workspace" / "input",
                result_dir=tool_root / "workspace" / "output",
                record_dir=tool_root / "workspace" / "records",
                log_dir=tool_root / "workspace" / "logs",
            )

            staging_root = prepare_unified_resource_source(cfg)
            self.assertIsNotNone(staging_root)
            assert staging_root is not None
            first_staged = (
                staging_root
                / "obb"
                / "base"
                / "main.1.demo.obb.contents"
                / "aa"
                / "Android"
                / "shared.bundle"
            )
            second_staged = (
                staging_root
                / "obb"
                / "patch"
                / "main.1.demo.obb.contents"
                / "aa"
                / "Android"
                / "shared.bundle"
            )

            self.assertNotEqual(first_staged, second_staged)
            self.assertEqual(first_staged.read_bytes(), b"base bundle")
            self.assertEqual(second_staged.read_bytes(), b"patch bundle")

            state = json.loads(
                resource_source_map_path(cfg).read_text(encoding="utf-8")
            )
            obb_entries = [
                entry
                for entry in state["entries"]
                if entry.get("origin_kind") == "obb"
                and entry.get("archive_entry") == archive_entry
            ]
            self.assertEqual(len(obb_entries), 2)
            self.assertEqual(
                {
                    entry["container_relative_assets_obb"]
                    for entry in obb_entries
                },
                {first_relative.as_posix(), second_relative.as_posix()},
            )
            self.assertEqual(
                len({entry["staged_relative"] for entry in obb_entries}), 2
            )


if __name__ == "__main__":
    unittest.main()
