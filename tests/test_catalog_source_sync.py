from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.catalog_tools import (
    auto_patch_and_repack_catalog_after_import,
    patch_and_repack_embedded_catalog_after_import,
    patch_expanded_catalog_from_final_bundles,
    validate_catalog_crc_algorithm,
)


class CatalogSourceSyncTests(unittest.TestCase):
    def test_embedded_catalog_restarts_from_source_on_each_import(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            catalog_source = root / "source" / "aa" / "catalog.json"
            source_bundle_root = catalog_source.parent / "Android"
            final_bundle_root = root / "raw" / "aa" / "Android"
            output_dir = root / "output" / "catalog" / "obb"
            final_catalog_root = root / "raw" / "aa"
            catalog_source.parent.mkdir(parents=True)
            source_bundle_root.mkdir(parents=True)
            final_bundle_root.mkdir(parents=True)
            catalog_source.write_text("{}", encoding="utf-8")

            first_name = "first.bundle"
            second_name = "second.bundle"
            (source_bundle_root / first_name).write_bytes(b"first")
            (source_bundle_root / second_name).write_bytes(b"second")
            first_original_size = len(b"first")
            second_original_size = len(b"second")
            baseline_catalog = {
                "m_ExtraDataString": {
                    "AssetBundleRequestOptions": [
                        {
                            "m_Hash": "first-hash",
                            "PrimaryKey": first_name,
                            "m_BundleName": "first",
                            "m_Crc": 101,
                            "m_BundleSize": first_original_size,
                        },
                        {
                            "m_Hash": "second-hash",
                            "PrimaryKey": second_name,
                            "m_BundleName": "second",
                            "m_Crc": 202,
                            "m_BundleSize": second_original_size,
                        },
                    ]
                }
            }

            def fake_parse(_cfg, _source, destination):
                expanded_path = destination / "Output.json"
                destination.mkdir(parents=True, exist_ok=True)
                expanded_path.write_text(
                    json.dumps(baseline_catalog),
                    encoding="utf-8",
                )
                return destination / "catalog.json", expanded_path

            captured_catalogs: list[dict] = []

            def fake_repack(expanded_path: Path, destination: Path) -> Path:
                payload = json.loads(expanded_path.read_text(encoding="utf-8"))
                captured_catalogs.append(payload)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(json.dumps(payload), encoding="utf-8")
                return destination

            def fake_crc(path: Path) -> int:
                return 101 if path.name == first_name else 202

            cfg = SimpleNamespace(root_dir=root, enable_sample_collection=False)
            first_modified_size = len(b"first-import-result")
            (final_bundle_root / first_name).write_bytes(b"first-import-result")

            with patch(
                "pipeline.catalog_tools.parse_catalog_to_output",
                side_effect=fake_parse,
            ) as parse_mock, patch(
                "pipeline.catalog_tools.validate_catalog_crc_algorithm",
                return_value=True,
            ), patch(
                "pipeline.catalog_tools.calculate_unityfs_uncompressed_crc",
                side_effect=fake_crc,
            ), patch(
                "pipeline.catalog_tools.repack_expanded_catalog",
                side_effect=fake_repack,
            ):
                patch_and_repack_embedded_catalog_after_import(
                    cfg,
                    catalog_source,
                    source_bundle_root,
                    final_bundle_root,
                    output_dir,
                    final_catalog_root,
                )

                (final_bundle_root / first_name).unlink()
                second_modified_size = len(b"second-import-result-is-different")
                (final_bundle_root / second_name).write_bytes(
                    b"second-import-result-is-different"
                )
                patch_and_repack_embedded_catalog_after_import(
                    cfg,
                    catalog_source,
                    source_bundle_root,
                    final_bundle_root,
                    output_dir,
                    final_catalog_root,
                )

            self.assertEqual(parse_mock.call_count, 2)
            first_rows = captured_catalogs[0]["m_ExtraDataString"][
                "AssetBundleRequestOptions"
            ]
            self.assertEqual(first_rows[0]["m_BundleSize"], first_modified_size)
            self.assertEqual(first_rows[0]["m_Crc"], 0)
            self.assertEqual(first_rows[1]["m_BundleSize"], second_original_size)

            second_rows = captured_catalogs[1]["m_ExtraDataString"][
                "AssetBundleRequestOptions"
            ]
            self.assertEqual(second_rows[0]["m_BundleSize"], first_original_size)
            self.assertEqual(second_rows[0]["m_Crc"], 101)
            self.assertEqual(second_rows[1]["m_BundleSize"], second_modified_size)
            self.assertEqual(second_rows[1]["m_Crc"], 0)

    def test_repeated_import_uses_original_catalog_metadata_for_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_path = root / "Output.json"
            baseline_path = output_path.with_suffix(
                output_path.suffix + ".bak_before_catalog_auto_patch"
            )
            source_bundle_root = root / "source" / "Android"
            final_bundle_root = root / "final" / "aa" / "Android"
            source_bundle_root.mkdir(parents=True)
            final_bundle_root.mkdir(parents=True)
            bundle_name = "_generatedisolationlocal_assets_all.bundle"
            source_bundle = source_bundle_root / bundle_name
            final_bundle = final_bundle_root / bundle_name
            source_bundle.write_bytes(b"original")
            final_bundle.write_bytes(b"second-import-result")

            base_row = {
                "InternalId": f"{{RuntimePath}}/Android/{bundle_name}",
                "PrimaryKey": "generated_0123456789abcdef.bundle",
                "m_Hash": "0123456789abcdef",
                "m_BundleName": "generated",
                "m_Crc": 123456789,
                "m_BundleSize": len(b"original"),
            }
            baseline_catalog = {
                "m_ExtraDataString": {"AssetBundleRequestOptions": [base_row]}
            }
            current_row = dict(base_row, m_Crc=0, m_BundleSize=999999)
            current_catalog = {
                "m_ExtraDataString": {"AssetBundleRequestOptions": [current_row]}
            }
            baseline_path.write_text(json.dumps(baseline_catalog), encoding="utf-8")
            output_path.write_text(json.dumps(current_catalog), encoding="utf-8")
            cfg = SimpleNamespace(enable_sample_collection=False, root_dir=root)

            with patch(
                "pipeline.catalog_tools.calculate_unityfs_uncompressed_crc",
                return_value=123456789,
            ):
                self.assertTrue(
                    validate_catalog_crc_algorithm(
                        cfg,
                        baseline_path,
                        source_bundle_root,
                        final_bundle_root,
                        root,
                    )
                )
                size_updates, crc_updates, returned_backup = (
                    patch_expanded_catalog_from_final_bundles(
                        output_path,
                        final_bundle_root,
                        source_bundle_root=source_bundle_root,
                        zero_crc=True,
                        reference_catalog_path=baseline_path,
                    )
                )

            patched = json.loads(output_path.read_text(encoding="utf-8"))
            patched_row = patched["m_ExtraDataString"]["AssetBundleRequestOptions"][0]
            self.assertEqual(patched_row["m_BundleSize"], len(b"second-import-result"))
            self.assertEqual(patched_row["m_Crc"], 0)
            self.assertEqual(size_updates, 1)
            self.assertEqual(crc_updates, 0)
            self.assertEqual(returned_backup, baseline_path)

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
            (final_root / "aa" / "Android").mkdir(parents=True)
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
            self.assertEqual(result, final_root / "aa" / "catalog.json")
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
            (final_root / "aa" / "Android").mkdir(parents=True)
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
                (final_root / "aa" / "catalog.bin").read_bytes(),
                b"final-bin",
            )
            self.assertEqual(
                (final_root / "aa" / "catalog.hash").read_text(encoding="ascii"),
                "final-hash",
            )

            run_with_localized_count(1)
            self.assertEqual(source_catalog.read_bytes(), b"final-bin")
            self.assertEqual(source_hash.read_text(encoding="ascii"), "final-hash")


if __name__ == "__main__":
    unittest.main()
