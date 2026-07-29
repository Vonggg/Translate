from __future__ import annotations

import json
import os
import unicodedata
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    tmp_path: Path | None = None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), suffix=".tmp") as tmp_file:
        tmp_file.write(payload)
        tmp_path = Path(tmp_file.name)
    try:
        os.replace(tmp_path, path)
    except PermissionError:
        path.write_text(payload, encoding="utf-8")
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def unique_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def is_visible_char(char: str) -> bool:
    if not char:
        return False
    return not unicodedata.category(char).startswith("C") and not char.isspace()


def flatten_texts(items: Iterable[str], include_ascii: bool = True) -> str:
    text = "".join(items)
    if include_ascii:
        text += " " + "".join(chr(i) for i in range(0x21, 0x7F))
    text = text.replace("\r", "").replace("\n", "").replace("\t", "")
    return "".join(unique_preserve_order(text))


OBJECT_INDEX_JSON_DIRS = {
    "GameObject",
    "Transform",
    "RectTransform",
    "Sprite",
    "SpriteRenderer",
    "Mesh",
    "MeshFilter",
    "SkinnedMeshRenderer",
}
OBJECT_INDEX_JSON_DIRS_LOWER = {name.lower() for name in OBJECT_INDEX_JSON_DIRS}


def collect_json_files(root: Path, *, include_object_index: bool = False) -> list[Path]:
    if not root.exists():
        return []
    paths: list[Path] = []
    for current_root, dir_names, file_names in os.walk(root, topdown=True):
        dir_names.sort()
        if not include_object_index:
            dir_names[:] = [
                name for name in dir_names
                if name.lower() not in OBJECT_INDEX_JSON_DIRS_LOWER
            ]
        for file_name in sorted(file_names):
            if file_name.lower() == "manifest.json" or not file_name.lower().endswith(".json"):
                continue
            paths.append(Path(current_root) / file_name)
    return paths


@dataclass
class ScanRecord:
    file_path: str
    field: str
    source_text: str
    translated_text: str = ""
    path_id: int | None = None
    font_path_id: int | None = None
