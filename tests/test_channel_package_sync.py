from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from support.channel_package_sync import (
    CHANNEL_PACKAGE_DIR_NAME_ENV,
    EXPLICIT_CONFIG_ENV,
    explicit_channel_sync_request,
    resolve_channel_package_target,
    sync_import_result_to_channel_package,
)


class ChannelPackageSyncTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[SimpleNamespace, Path, Path, Path]:
        project_dir = root / "projects" / "Game A"
        source_aa = project_dir / "game-name" / "game" / "assets" / "aa"
        source_aa.mkdir(parents=True)
        catalog = source_aa / "catalog.json"
        catalog.write_text("source catalog", encoding="utf-8")

        channel = project_dir / "game-name" / "GAME_hongtu_L"
        (channel / "assets").mkdir(parents=True)
        (channel / "AndroidManifest.xml").write_text("manifest", encoding="utf-8")

        workspace = root / "workspaceGame A"
        final_root = workspace / "FinalResult"
        cfg = SimpleNamespace(
            project_dir=project_dir,
            catalog_source_path=catalog,
            workspace_root=workspace,
        )
        return cfg, source_aa, channel, final_root

    def test_remote_resources_copy_full_source_aa_before_final_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg, source_aa, channel, final_root = self._fixture(Path(temporary))
            (source_aa / "settings.json").write_text("source settings", encoding="utf-8")
            (source_aa / "Android").mkdir()
            (source_aa / "Android" / "shared.bundle").write_text("source bundle", encoding="utf-8")

            report_path = cfg.workspace_root / "resource_state" / "addressables_remote_resources.json"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                json.dumps({"localized_internal_ids": [{"source": "https://example.invalid/a"}]}),
                encoding="utf-8",
            )

            (final_root / "Data").mkdir(parents=True)
            (final_root / "Data" / "globalgamemanagers").write_text("new data", encoding="utf-8")
            (final_root / "aa" / "Android").mkdir(parents=True)
            (final_root / "aa" / "Android" / "shared.bundle").write_text("new bundle", encoding="utf-8")
            (final_root / "aa" / "catalog.json").write_text("new catalog", encoding="utf-8")

            summary = sync_import_result_to_channel_package(cfg, final_root, "GAME_hongtu_L")

            target_assets = channel / "assets"
            self.assertEqual((target_assets / "aa" / "settings.json").read_text(), "source settings")
            self.assertEqual((target_assets / "aa" / "Android" / "shared.bundle").read_text(), "new bundle")
            self.assertEqual((target_assets / "aa" / "catalog.json").read_text(), "new catalog")
            self.assertEqual((target_assets / "bin" / "Data" / "globalgamemanagers").read_text(), "new data")
            self.assertEqual(summary.source_aa_files, 3)
            self.assertEqual(summary.final_result_files, 3)

    def test_without_remote_resources_only_final_result_is_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg, source_aa, channel, final_root = self._fixture(Path(temporary))
            (source_aa / "settings.json").write_text("source settings", encoding="utf-8")
            (final_root / "aa" / "Android").mkdir(parents=True)
            (final_root / "aa" / "Android" / "changed.bundle").write_text("changed", encoding="utf-8")

            summary = sync_import_result_to_channel_package(cfg, final_root, "GAME_hongtu_L")

            target_aa = channel / "assets" / "aa"
            self.assertFalse((target_aa / "settings.json").exists())
            self.assertEqual((target_aa / "Android" / "changed.bundle").read_text(), "changed")
            self.assertEqual(summary.source_aa_files, 0)
            self.assertEqual(summary.final_result_files, 1)

    def test_obb_result_preserves_assets_obb_relative_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg, _source_aa, channel, final_root = self._fixture(Path(temporary))
            obb_relative = Path("com.example.game") / "main.1.com.example.game.obb"
            output_obb = final_root / "obb" / obb_relative
            output_obb.parent.mkdir(parents=True)
            output_obb.write_bytes(b"rebuilt obb")

            summary = sync_import_result_to_channel_package(cfg, final_root, "GAME_hongtu_L")

            self.assertEqual(
                (channel / "assets" / "obb" / obb_relative).read_bytes(),
                b"rebuilt obb",
            )
            self.assertEqual(summary.final_result_files, 1)

    def test_invalid_or_incomplete_channel_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg, _source_aa, channel, _final_root = self._fixture(Path(temporary))
            with self.assertRaises(ValueError):
                resolve_channel_package_target(cfg, "../GAME_hongtu_L")

            (channel / "AndroidManifest.xml").unlink()
            with self.assertRaises(FileNotFoundError):
                resolve_channel_package_target(cfg, "GAME_hongtu_L")

    def test_final_result_from_another_workspace_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cfg, _source_aa, _channel, _final_root = self._fixture(Path(temporary))
            wrong_final = Path(temporary) / "workspaceOther" / "FinalResult"
            (wrong_final / "Data").mkdir(parents=True)
            (wrong_final / "Data" / "data.unity3d").write_text("wrong", encoding="utf-8")

            with self.assertRaises(ValueError):
                sync_import_result_to_channel_package(cfg, wrong_final, "GAME_hongtu_L")

    def test_environment_requires_explicit_config_and_channel(self) -> None:
        with patch.dict(
            os.environ,
            {
                EXPLICIT_CONFIG_ENV: "1",
                CHANNEL_PACKAGE_DIR_NAME_ENV: "GAME_hongtu_L",
            },
            clear=False,
        ):
            self.assertEqual(explicit_channel_sync_request(), (True, "GAME_hongtu_L"))

        with patch.dict(
            os.environ,
            {
                EXPLICIT_CONFIG_ENV: "0",
                CHANNEL_PACKAGE_DIR_NAME_ENV: "GAME_hongtu_L",
            },
            clear=False,
        ):
            self.assertEqual(explicit_channel_sync_request(), (False, "GAME_hongtu_L"))


if __name__ == "__main__":
    unittest.main()
