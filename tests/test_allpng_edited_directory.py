from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


def load_tool_module():
    script = Path(__file__).resolve().parents[1] / "工具脚本.py"
    spec = importlib.util.spec_from_file_location("tools_for_allpng_directory_test", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载工具脚本.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TOOLS = load_tool_module()


class AllPNGEditedDirectoryTests(unittest.TestCase):
    def test_default_edited_directory_is_inside_allpng(self) -> None:
        self.assertEqual(
            TOOLS.DEFAULT_EDITED_IMAGE_ROOT,
            TOOLS.DEFAULT_ALL_IMAGE_ROOT / "修改后的图片目录",
        )

    def test_refresh_preserves_user_work_directories_and_removes_generated_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            allpng = Path(temporary) / "AllPNG"
            edited = allpng / "修改后的图片目录"
            block_images = allpng / "屏蔽object"
            generated = allpng / "PNG" / "source.png"
            edited_file = edited / "changed.png"
            block_file = block_images / "blocked.png"
            generated.parent.mkdir(parents=True)
            edited.mkdir(parents=True)
            block_images.mkdir(parents=True)
            generated.write_bytes(b"generated")
            edited_file.write_bytes(b"edited")
            block_file.write_bytes(b"blocked")
            (allpng / "_allpng_map.json").write_text("{}", encoding="utf-8")

            TOOLS.reset_allpng_generated_content(allpng, edited, block_images)

            self.assertTrue(edited_file.is_file())
            self.assertTrue(block_file.is_file())
            self.assertFalse(generated.exists())
            self.assertFalse((allpng / "_allpng_map.json").exists())

    def test_refresh_creates_both_user_work_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            allpng = Path(temporary) / "AllPNG"
            edited = allpng / "修改后的图片目录"
            block_images = allpng / "屏蔽object"

            TOOLS.reset_allpng_generated_content(allpng, edited, block_images)

            self.assertTrue(edited.is_dir())
            self.assertTrue(block_images.is_dir())


if __name__ == "__main__":
    unittest.main()
