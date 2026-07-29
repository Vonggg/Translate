from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.catalog_tools import auto_patch_and_repack_catalog_after_import


class CatalogSourceSyncTests(unittest.TestCase):
    def test_source_catalog_is_replaced_after_remote_path_localization(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_catalog = root / "project" / "assets" / "aa" / "catalog.json"
            source_catalog.parent.mkdir(parents=True)
            source_catalog.write_text('{"source":"original"}', encoding="utf-8")

            output_dir = root / "workspace" / "output" / "catalog"
            output_dir.mkdir(parents=True)
            expanded_path = output_dir / "Output.json"
            expanded_path.write_text("{}", encoding="utf-8")

            final_root = root / "workspace" / "FinalResult"
            (final_root / "Bundle" / "Android").mkdir(parents=True)
            report_path = (
                root
                / "workspace"
                / "resource_state"
                / "addressables_remote_resources.json"
            )
            report_path.parent.mkdir(parents=True)

            cfg = SimpleNamespace(
                catalog_source_path=source_catalog,
                result_dir=root / "workspace" / "output",
                root_dir=root,
            )

            def fake_repack(_source: Path, destination: Path) -> Path:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text('{"source":"final"}', encoding="utf-8")
                return destination

            common_patches = (
                patch("pipeline.catalog_tools.collect_final_bundle_files", return_value=[Path("changed.bundle")]),
                patch("pipeline.catalog_tools.validate_catalog_crc_algorithm", return_value=True),
                patch(
                    "pipeline.catalog_tools.patch_expanded_catalog_from_final_bundles",
                    return_value=(1, 1, expanded_path.with_suffix(".backup")),
                ),
                patch("pipeline.catalog_tools.repack_expanded_catalog", side_effect=fake_repack),
            )

            report_path.write_text(
                json.dumps(
                    {
                        "success_count": 0,
                        "localized_internal_ids": [],
                        "source_catalog_modified": False,
                    }
                ),
                encoding="utf-8",
            )
            with common_patches[0], common_patches[1], common_patches[2], common_patches[3]:
                result = auto_patch_and_repack_catalog_after_import(cfg, final_root)
            self.assertEqual(result, final_root / "Bundle" / "catalog.json")
            self.assertEqual(source_catalog.read_text(encoding="utf-8"), '{"source":"original"}')

            report_path.write_text(
                json.dumps(
                    {
                        "success_count": 0,
                        "localized_internal_ids": [
                            {
                                "remote_internal_id": "https://cdn.example/a.bundle",
                                "local_internal_id": "{RuntimePath}/Android/a.bundle",
                            }
                        ],
                        "source_catalog_modified": True,
                    }
                ),
                encoding="utf-8",
            )
            with patch(
                "pipeline.catalog_tools.collect_final_bundle_files",
                return_value=[Path("changed.bundle")],
            ), patch(
                "pipeline.catalog_tools.validate_catalog_crc_algorithm",
                return_value=True,
            ), patch(
                "pipeline.catalog_tools.patch_expanded_catalog_from_final_bundles",
                return_value=(1, 1, expanded_path.with_suffix(".backup")),
            ), patch(
                "pipeline.catalog_tools.repack_expanded_catalog",
                side_effect=fake_repack,
            ):
                auto_patch_and_repack_catalog_after_import(cfg, final_root)
            self.assertEqual(source_catalog.read_text(encoding="utf-8"), '{"source":"final"}')

    def test_binary_catalog_and_hash_follow_the_same_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_catalog = root / "project" / "assets" / "aa" / "catalog.bin"
            source_hash = source_catalog.with_suffix(".hash")
            source_catalog.parent.mkdir(parents=True)
            source_catalog.write_bytes(b"original-bin")
            source_hash.write_text("original-hash", encoding="ascii")

            output_dir = root / "workspace" / "output" / "catalog"
            output_dir.mkdir(parents=True)
            expanded_path = output_dir / "Output.json"
            expanded_path.write_text("{}", encoding="utf-8")
            final_root = root / "workspace" / "FinalResult"
            (final_root / "Bundle" / "Android").mkdir(parents=True)
            report_path = (
                root
                / "workspace"
                / "resource_state"
                / "addressables_remote_resources.json"
            )
            report_path.parent.mkdir(parents=True)
            cfg = SimpleNamespace(
                catalog_source_path=source_catalog,
                result_dir=root / "workspace" / "output",
                root_dir=root,
            )

            def fake_binary_repack(
                _source: Path,
                _output: Path,
                destination: Path,
                destination_hash: Path,
            ) -> dict[str, object]:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"final-bin")
                destination_hash.write_text("final-hash", encoding="ascii")
                return {
                    "internal_id_updates": 0,
                    "bundle_option_updates": 1,
                    "catalog_hash": "final-hash",
                }

            def run_with_localized_count(count: int) -> None:
                report_path.write_text(
                    json.dumps(
                        {
                            "success_count": 0,
                            "localized_internal_ids": [
                                {
                                    "remote_internal_id": f"https://cdn.example/{index}.bundle",
                                    "local_internal_id": f"{{RuntimePath}}/Android/{index}.bundle",
                                }
                                for index in range(count)
                            ],
                            "source_catalog_modified": count > 0,
                        }
                    ),
                    encoding="utf-8",
                )
                with patch(
                    "pipeline.catalog_tools.collect_final_bundle_files",
                    return_value=[Path("changed.bundle")],
                ), patch(
                    "pipeline.catalog_tools.validate_catalog_crc_algorithm",
                    return_value=True,
                ), patch(
                    "pipeline.catalog_tools.patch_expanded_catalog_from_final_bundles",
                    return_value=(1, 1, expanded_path.with_suffix(".backup")),
                ), patch(
                    "pipeline.catalog_tools.repack_binary_catalog_from_legacy_output",
                    side_effect=fake_binary_repack,
                ):
                    auto_patch_and_repack_catalog_after_import(cfg, final_root)

            run_with_localized_count(0)
            self.assertEqual(source_catalog.read_bytes(), b"original-bin")
            self.assertEqual(source_hash.read_text(encoding="ascii"), "original-hash")
            self.assertEqual(
                (final_root / "Bundle" / "catalog.bin").read_bytes(),
                b"final-bin",
            )
            self.assertEqual(
                (final_root / "Bundle" / "catalog.hash").read_text(encoding="ascii"),
                "final-hash",
            )

            run_with_localized_count(1)
            self.assertEqual(source_catalog.read_bytes(), b"final-bin")
            self.assertEqual(source_hash.read_text(encoding="ascii"), "final-hash")


if __name__ == "__main__":
    unittest.main()
