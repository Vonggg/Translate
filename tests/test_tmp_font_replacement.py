import unittest

from pipeline.tmp_pipeline import _build_tmp_font_replacement


class TmpFontReplacementTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
