from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from pipeline.localization_binding_detection import (
    extract_component_descriptor,
    infer_localization_bindings,
    protection_index,
)
from pipeline.translation import apply_translations_to_json, scan_translation_inputs
from support.config import load_config


def _component(
    file: str,
    path_id: int,
    game_object_path_id: int,
    script_path_id: int,
    **fields: object,
) -> dict[str, object]:
    data: dict[str, object] = {
        "m_GameObject": {"m_FileID": 0, "m_PathID": game_object_path_id},
        "m_Script": {"m_FileID": 1, "m_PathID": script_path_id},
        **fields,
    }
    descriptor = extract_component_descriptor(
        data,
        relative_file=f"bundle/MonoBehaviour/{file}.json",
        asset="bundle",
        asset_path_id=path_id,
    )
    assert descriptor is not None
    return descriptor


class LocalizationBindingDetectionTests(unittest.TestCase):
    def test_scanner_removes_only_structurally_bound_key_positions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            mono_root = input_root / "bundle" / "MonoBehaviour"
            mono_root.mkdir(parents=True)
            for index, key in enumerate(("title", "settings", "quit"), start=1):
                (mono_root / f"localizer_{index}.json").write_text(
                    json.dumps(
                        {
                            "m_GameObject": {"m_FileID": 0, "m_PathID": index},
                            "m_Script": {"m_FileID": 1, "m_PathID": 900},
                            "lookupCode": key,
                            "renderPattern": "{0}",
                        }
                    ),
                    encoding="utf-8",
                )
                (mono_root / f"text_{index}.json").write_text(
                    json.dumps(
                        {
                            "m_GameObject": {"m_FileID": 0, "m_PathID": index},
                            "m_Script": {"m_FileID": 1, "m_PathID": 100},
                            "m_text": key,
                        }
                    ),
                    encoding="utf-8",
                )
            (mono_root / "plain_text.json").write_text(
                json.dumps(
                    {
                        "m_GameObject": {"m_FileID": 0, "m_PathID": 99},
                        "m_Script": {"m_FileID": 1, "m_PathID": 100},
                        "m_text": "title",
                    }
                ),
                encoding="utf-8",
            )
            cfg = replace(
                load_config(quiet=True),
                resource_input_root=input_root,
                record_dir=root / "records",
                enable_ai_field_review=True,
                max_scan_workers=1,
            )
            records, _ids, _fonts, _refs = scan_translation_inputs(cfg)
            title_records = [item for item in records if item.source_text == "title"]
            self.assertEqual(1, len(title_records))
            self.assertTrue(title_records[0].file_path.endswith("plain_text.json"))

    def test_custom_structure_protects_plain_keys_but_not_unrelated_text(self) -> None:
        descriptors: list[dict[str, object]] = []
        values = [("title", "title"), ("settings", "settings"), ("quit", "Quit game")]
        for index, (key, visible_text) in enumerate(values, start=1):
            descriptors.append(
                _component(
                    f"localizer_{index}",
                    100 + index,
                    index,
                    900,
                    lookupCode=key,
                    renderPattern="{0}",
                )
            )
            descriptors.append(
                _component(
                    f"text_{index}",
                    200 + index,
                    index,
                    100,
                    m_text=visible_text,
                )
            )
        descriptors.append(_component("plain_text", 300, 99, 100, m_text="title"))

        report = infer_localization_bindings(descriptors)
        protected = protection_index(report)

        self.assertIn(
            ("lookupCode", "title"),
            protected["bundle/MonoBehaviour/localizer_1.json"],
        )
        self.assertIn(
            ("m_text", "title"),
            protected["bundle/MonoBehaviour/text_1.json"],
        )
        self.assertNotIn(
            ("m_text", "Quit game"),
            protected.get("bundle/MonoBehaviour/text_3.json", set()),
        )
        self.assertNotIn(
            ("m_text", "title"),
            protected.get("bundle/MonoBehaviour/plain_text.json", set()),
        )

    def test_i2_empty_term_protects_same_gameobject_text(self) -> None:
        descriptors = [
            _component(
                "i2_localize",
                401,
                50,
                901,
                mTerm="",
                mTermSecondary="",
                mLocalizeTargetName="I2.Loc.LocalizeTarget_UnityUI_Text",
            ),
            _component("i2_text", 402, 50, 100, m_Text="title"),
        ]
        report = infer_localization_bindings(descriptors)
        protected = protection_index(report)
        self.assertIn(
            ("m_Text", "title"),
            protected["bundle/MonoBehaviour/i2_text.json"],
        )

    def test_duplicate_visible_caption_without_key_structure_is_not_protected(self) -> None:
        descriptors: list[dict[str, object]] = []
        for index, text in enumerate(("Play", "Options", "Exit"), start=1):
            descriptors.append(
                _component(f"caption_{index}", 500 + index, index, 902, caption=text)
            )
            descriptors.append(
                _component(f"caption_text_{index}", 600 + index, index, 100, m_text=text)
            )
        report = infer_localization_bindings(descriptors)
        self.assertEqual(0, report["summary"]["protected_positions"])

    def test_final_writer_respects_exact_protection_even_if_translation_exists(self) -> None:
        data = {"m_text": "title", "description": "title"}
        result = apply_translations_to_json(
            data,
            load_config(),
            {"title": "标题"},
            protected_field_values={("m_text", "title")},
        )
        self.assertEqual("title", result["m_text"])
        self.assertEqual("标题", result["description"])


if __name__ == "__main__":
    unittest.main()
