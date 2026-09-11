import unittest

from pipeline.tmp_pipeline import _build_tmp_font_replacement, _apply_generated_sdf_material_floats


class TmpFontReplacementTests(unittest.TestCase):
    def test_bold_strength_halved_without_changing_sources(self) -> None:
        original = {"boldStyle": 0.75, "normalStyle": 0.0}
        for _ in range(2):
            result = _build_tmp_font_replacement({}, original)
            self.assertEqual(result["boldStyle"], 0.375)
            self.assertEqual(result["normalStyle"], 0.0)
        self.assertEqual(original["boldStyle"], 0.75)
        material = {"m_SavedProperties": {"m_Floats": {"Array": [
            {"first": "_WeightBold", "second": 0.6},
            {"first": "_WeightNormal", "second": 0.0},
        ]}}}
        for values, expected in (({"_WeightBold": 0.75}, 0.375), ({}, 0.3)):
            for _ in range(2):
                result, _ = _apply_generated_sdf_material_floats(material, values)
                self.assertEqual(result["m_SavedProperties"]["m_Floats"]["Array"][0]["second"], expected)
        self.assertEqual(material["m_SavedProperties"]["m_Floats"]["Array"][0]["second"], 0.6)

    def test_preserves_nested_fields_missing_from_generated_template(self) -> None:
        original = {
            "m_GlyphTable": {"Array": [{"old": True}]},
            "m_CharacterTable": {"Array": [{"old": True}]},
            "m_FaceInfo": {"m_FamilyName": "Game Font"},
            "m_FontFeatureTable": {
                "m_MultipleSubstitutionRecords": {"Array": [{"original": True}]},
                "m_GlyphPairAdjustmentRecords": {"Array": [{"old": True}]},
            },
        }
        generated = {
            "m_GlyphTable": {"Array": [{"generated": True}]},
            "m_CharacterTable": {"Array": [{"generated": True}]},
            "m_FaceInfo": {"m_FamilyName": "Generated Font"},
            "m_FontFeatureTable": {
                "m_GlyphPairAdjustmentRecords": {"Array": [{"generated": True}]},
            },
        }

        replacement = _build_tmp_font_replacement(generated, original)

        self.assertEqual(
            replacement["m_FontFeatureTable"]["m_MultipleSubstitutionRecords"],
            original["m_FontFeatureTable"]["m_MultipleSubstitutionRecords"],
        )
        self.assertEqual(
            replacement["m_FontFeatureTable"]["m_GlyphPairAdjustmentRecords"],
            generated["m_FontFeatureTable"]["m_GlyphPairAdjustmentRecords"],
        )
        self.assertTrue(replacement["m_GlyphTable"]["Array"][0]["generated"])

    def test_face_info_keeps_original_schema_types_with_generated_values(self) -> None:
        original = {
            "m_GlyphTable": {"Array": []},
            "m_CharacterTable": {"Array": []},
            "m_FaceInfo": {
                "m_FamilyName": "Game Font",
                "m_StyleName": "Regular",
                "m_UnitsPerEM": 1000,
                "m_PointSize": 90,
                "m_Scale": 1.0,
                "m_CapLine": 66.0,
                "m_Baseline": 0.0,
                "m_OriginalOnly": 12.5,
            },
        }
        generated = {
            "m_GlyphTable": {"Array": []},
            "m_CharacterTable": {"Array": []},
            "m_FaceInfo": {
                "m_FamilyName": "Generated Font",
                "m_StyleName": "Generated Style",
                "m_UnitsPerEM": 2048,
                "m_PointSize": 43,
                "m_Scale": 1,
                "m_CapLine": 30,
                "m_Baseline": 0,
                "m_GeneratedOnly": 99,
            },
        }

        replacement = _build_tmp_font_replacement(generated, original)
        face_info = replacement["m_FaceInfo"]

        self.assertEqual(face_info["m_FamilyName"], "Game Font")
        self.assertEqual(face_info["m_StyleName"], "Regular")
        self.assertEqual(face_info["m_UnitsPerEM"], 1000)
        self.assertEqual(face_info["m_PointSize"], 43)
        self.assertEqual(face_info["m_Scale"], 1.0)
        self.assertEqual(face_info["m_CapLine"], 30.0)
        self.assertEqual(face_info["m_Baseline"], 0.0)
        self.assertIs(type(face_info["m_Scale"]), float)
        self.assertIs(type(face_info["m_CapLine"]), float)
        self.assertIs(type(face_info["m_Baseline"]), float)
        self.assertEqual(face_info["m_OriginalOnly"], 12.5)
        self.assertNotIn("m_GeneratedOnly", face_info)


if __name__ == "__main__":
    unittest.main()
