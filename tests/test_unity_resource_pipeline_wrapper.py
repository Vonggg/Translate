from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from AssetPipeline_CLI.scripts import unity_resource_pipeline


class UnityResourcePipelineWrapperTests(unittest.TestCase):
    def test_dotnet_build_and_samples_are_isolated_by_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspaceGameA"
            work = workspace / "temp" / "selected_import_work"
            sample_root = root / "样本" / workspace.name
            args = argparse.Namespace(
                mode="import",
                source=root / "source",
                work=work,
                managed=root / "Managed",
                dump_format="json",
                image_format="png",
                quality=90,
                export_profile="all",
                export_workers=0,
                verbose_export_assets=False,
                replacement_root=root / "overlay",
                result_root=workspace / "FinalResult",
                sample_root=sample_root,
                import_workers=0,
                save_samples=True,
                verbose_import_assets=False,
            )

            command = unity_resource_pipeline.build_command(args, root / "AssetPipeline_CLI")

            artifacts_index = command.index("--artifacts-path")
            self.assertEqual(
                Path(command[artifacts_index + 1]),
                workspace / "temp" / "dotnet_artifacts",
            )
            sample_index = command.index("--sample-root")
            self.assertEqual(Path(command[sample_index + 1]), sample_root)
            verbose_index = command.index("--verbose-import-assets")
            self.assertEqual(command[verbose_index + 1], "false")


if __name__ == "__main__":
    unittest.main()
