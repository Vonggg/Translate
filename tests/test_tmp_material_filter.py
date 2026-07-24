from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.translation import _is_tmp_sdf_material_json, _remove_stale_non_tmp_global_material_overlays


def material_with_properties(*names: str) -> dict:
    return {"m_SavedProperties": {"m_Floats": {"Array": [{"first": name, "second": 1.0} for name in names]}}}


class TmpMaterialFilterTests(unittest.TestCase):
    def test_generic_outline_material_is_not_tmp(self) -> None:
        self.assertFalse(_is_tmp_sdf_material_json(material_with_properties("_OutlineWidth", "_OutlineColor")))

    def test_tmp_sdf_material_requires_dedicated_markers(self) -> None:
        self.assertTrue(_is_tmp_sdf_material_json(material_with_properties("_FaceColor", "_GradientScale", "_ScaleRatioA")))

    def test_stale_global_cleanup_keeps_tmp_and_removes_generic_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            stage_dir = root / "output"
            record_dir = root / "records"
            generic_relative = Path("bundle") / "Material" / "scene.json"
            tmp_relative = Path("bundle") / "Material" / "font.json"
            for relative, data in (
                (generic_relative, material_with_properties("_OutlineWidth", "_OutlineColor")),
                (tmp_relative, material_with_properties("_FaceColor", "_GradientScale", "_ScaleRatioA", "_OutlineWidth")),
            ):
                source_path = input_root / relative
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(json.dumps(data), encoding="utf-8")
                output_path = stage_dir / "Text" / relative
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(data), encoding="utf-8")
            report = {
                "materials": [
                    {"file": str(generic_relative), "output_file": str(Path("Text") / generic_relative), "used_by": [{"text_file": "<runtime-binding fallback>"}]},
                    {"file": str(tmp_relative), "output_file": str(Path("Text") / tmp_relative), "used_by": [{"text_file": "<runtime-binding fallback>"}]},
                ]
            }
            record_dir.mkdir()
            (record_dir / "disabled.json").write_text(json.dumps(report), encoding="utf-8")
            cfg = SimpleNamespace(
                resource_input_root=input_root,
                stage_dir=stage_dir,
                stage_record_dir=record_dir,
                output_disabled_effect_components_json="disabled.json",
            )
            self.assertEqual(_remove_stale_non_tmp_global_material_overlays(cfg), 1)
            self.assertFalse((stage_dir / "Text" / generic_relative).exists())
            self.assertTrue((stage_dir / "Text" / tmp_relative).exists())


if __name__ == "__main__":
    unittest.main()
