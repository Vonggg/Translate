"""独立动态文本识别工具所需的最小 JSON 辅助函数。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp"
        ) as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            temporary = Path(stream.name)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
