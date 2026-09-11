from __future__ import annotations

import unittest

from pipeline.local_field_policy import classify_local_string_field


class LocalFieldPolicyTests(unittest.TestCase):
    def assert_decision(
        self,
        expected: str,
        field: str,
        samples: tuple[str, ...] = ("Player text",),
        sibling_schema: tuple[str, ...] = (),
    ) -> None:
        result = classify_local_string_field(field, samples, sibling_schema)
        self.assertEqual(expected, result.decision, result.reason)

    def test_known_display_and_localization_values_are_allowed(self) -> None:
        self.assert_decision("allow", "m_Text", ("0",))
        self.assert_decision("allow", "m_text", (">>>",))
        self.assert_decision("allow", "table.Array[].m_Localized", ("Start",))
        self.assert_decision(
            "allow",
            "localizations.Array[].GDPRDescription",
            ("Privacy details",),
        )

    def test_runtime_names_and_structures_are_protected(self) -> None:
        for field in (
            "_textFormat",
            "m_RegexValue",
            "analyticsId",
            "_config._apiKey",
            "items.Array[].assetGUID",
            "items.Array[].<SoundId>k__BackingField",
        ):
            with self.subTest(field=field):
                self.assert_decision("protect", field)

        self.assert_decision(
            "protect",
            "shopProducts.Array[].productName",
            ("coins_100",),
            (
                "productName=string",
                "idGooglePlay=string",
                "idAmazon=string",
                "idIos=string",
            ),
        )

    def test_parent_names_do_not_protect_an_unrelated_text_leaf(self) -> None:
        for field in (
            "itemsById.Array[].description",
            "displayType.title",
            "monkey",
        ):
            with self.subTest(field=field):
                self.assert_decision("unknown", field)

    def test_empty_and_mixed_samples_remain_unknown(self) -> None:
        self.assert_decision("unknown", "caption", ())
        self.assert_decision(
            "unknown",
            "caption",
            (
                "https://one.invalid",
                "https://two.invalid",
                "https://three.invalid",
                "https://four.invalid",
                "https://five.invalid",
                "https://six.invalid",
                "Visible seventh sample",
            ),
        )

    def test_only_uniform_machine_or_non_language_samples_are_protected(self) -> None:
        self.assert_decision(
            "protect",
            "endpoint",
            ("https://one.invalid", "https://two.invalid"),
        )
        self.assert_decision("protect", "counter", ("0", "42", "---"))


if __name__ == "__main__":
    unittest.main()
