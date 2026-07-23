from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pipeline.resource_staging import (
    _build_remote_downloads,
    prepare_split_sync_outputs,
    prepare_unified_resource_source,
    remote_resource_report_path,
    resource_source_map_path,
    restore_imported_resource_paths,
)
from support.config import load_config


class ResourceStagingTests(unittest.TestCase):
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
            self.assertEqual(len(remote_report["localized_internal_ids"]), 1)
            self.assertEqual(
                remote_report["localized_internal_ids"][0]["remote_internal_id"],
                remote_id,
            )

            state = json.loads(resource_source_map_path(cfg).read_text(encoding="utf-8"))
            split_entries = [entry for entry in state["entries"] if entry.get("split_parts")]
            self.assertEqual(len(split_entries), 1)

            final_root = tool_root / "workspace" / "FinalResult"
            staged_bundle_result = final_root / "Bundle" / "Android" / "aa" / "Android" / "remote.bundle"
            staged_data_result = final_root / "bin" / "Data" / "globalgamemanagers"
            staged_bundle_result.parent.mkdir(parents=True)
            staged_data_result.parent.mkdir(parents=True)
            staged_bundle_result.write_bytes(b"WXYZ12")
            staged_data_result.write_bytes(b"changed-data")

            restored = restore_imported_resource_paths(cfg, final_root)
            self.assertEqual((final_root / "Bundle" / "Android" / "remote.bundle").read_bytes(), b"WXYZ12")
            self.assertEqual((final_root / "Data" / "globalgamemanagers").read_bytes(), b"changed-data")

            self.assertEqual(prepare_split_sync_outputs(cfg, final_root, restored), 1)
            split_root = final_root / "SplitBundles" / "Parts" / "assets" / "aa" / "Android"
            self.assertEqual((split_root / "remote.bundle.split0").read_bytes(), b"WX")
            self.assertEqual((split_root / "remote.bundle.split1").read_bytes(), b"YZ12")

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


if __name__ == "__main__":
    unittest.main()
