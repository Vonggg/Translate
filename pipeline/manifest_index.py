from __future__ import annotations

from pathlib import Path
from typing import Any

from support.config import PipelineConfig
from .shared import read_json, write_json


TMP_MANIFEST_INDEX_NAME = "tmp_manifest_index.json"
TMP_FONT_MARKERS = ("sdf", "fontasset", "font asset")
INDEXED_RESOURCE_TYPES = {"MonoBehaviour", "TextAsset", "Texture2D", "Material", "Font"}


def tmp_manifest_index_path(cfg: PipelineConfig) -> Path:
    return cfg.stage_record_dir / TMP_MANIFEST_INDEX_NAME


def build_tmp_manifest_index(cfg: PipelineConfig) -> dict[str, Any]:
    manifest_count = 0
    items: list[dict[str, Any]] = []
    for manifest_path in sorted(cfg.resource_input_root.rglob("manifest.json")):
        try:
            manifest = read_json(manifest_path)
        except Exception:
            continue
        manifest_items = manifest.get("Items") if isinstance(manifest, dict) else None
        if not isinstance(manifest_items, list):
            continue
        manifest_count += 1
        manifest_dir = manifest_path.parent
        for item in manifest_items:
            if not isinstance(item, dict):
                continue
            type_name = item.get("TypeName", item.get("typeName"))
            if type_name not in INDEXED_RESOURCE_TYPES:
                continue
            if type_name == "MonoBehaviour":
                asset_name = str(item.get("AssetName", item.get("assetName", "")))
                relative_path = str(item.get("RelativePath", item.get("relativePath", "")))
                marker_text = f"{asset_name} {relative_path}".lower()
                if not any(marker in marker_text for marker in TMP_FONT_MARKERS):
                    continue
            items.append(
                {
                    "manifest_path": str(manifest_path),
                    "manifest_dir": str(manifest_dir),
                    "item": item,
                }
            )

    payload = {
        "resource_input_root": str(cfg.resource_input_root),
        "manifest_count": manifest_count,
        "item_count": len(items),
        "items": items,
    }
    write_json(tmp_manifest_index_path(cfg), payload)
    return payload


def load_tmp_manifest_index(cfg: PipelineConfig) -> list[tuple[Path, Path, dict[str, Any]]] | None:
    path = tmp_manifest_index_path(cfg)
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("resource_input_root", "")) != str(cfg.resource_input_root):
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None

    items: list[tuple[Path, Path, dict[str, Any]]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        manifest_path = raw.get("manifest_path")
        manifest_dir = raw.get("manifest_dir")
        item = raw.get("item")
        if not isinstance(manifest_path, str) or not isinstance(manifest_dir, str) or not isinstance(item, dict):
            continue
        items.append((Path(manifest_path), Path(manifest_dir), item))
    return items
