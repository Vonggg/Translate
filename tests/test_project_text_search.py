from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _load_tools():
    path = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_project_text_search", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = _load_tools()


class ProjectTextSearchTests(unittest.TestCase):
    def test_search_roots_include_workspace_and_raw_project_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "workspace" / "input"
            record_root = root / "workspace" / "records"
            assets_root = root / "project" / "game-name" / "game" / "assets"
            channel_root = root / "project" / "game-name" / "GAME_hongtu_L" / "assets"
            backup_root = root / "project" / "game-name" / "bak" / "64"
            for path in (source_root, record_root, assets_root, channel_root, backup_root):
                path.mkdir(parents=True)
            cfg = SimpleNamespace(
                project_dir=root / "project",
                resource_source_root=assets_root / "bin" / "Data",
            )
            cfg.resource_source_root.mkdir(parents=True)
            with patch.object(TOOLS, "DEFAULT_SOURCE_ROOT", source_root), patch.object(
                TOOLS, "DEFAULT_RECORD_ROOT", record_root
            ):
                roots = TOOLS.project_text_search_roots(cfg)

            self.assertEqual(
                [label for label, _path in roots],
                ["工作区导出 input", "工作区记录", "原始游戏 assets", "渠道游戏 assets", "IL2CPP 备份/文本记录"],
            )
            self.assertEqual([path for _label, path in roots], [
                source_root.resolve(), record_root.resolve(), assets_root.resolve(),
                channel_root.resolve(), backup_root.resolve(),
            ])


if __name__ == "__main__":
    unittest.main()
