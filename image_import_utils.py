from __future__ import annotations

import shutil
from pathlib import Path


def copy_image_for_import(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)
