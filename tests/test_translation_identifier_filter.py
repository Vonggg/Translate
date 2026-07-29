from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from pipeline.shared import ScanRecord
from pipeline.translation import (
    _is_identifier_like_translation_key,
    build_translation_map,
)
from support.config import load_config


class TranslationIdentifierFilterTests(unittest.TestCase):
    def test_identifier_like_key_detection(self) -> None:
        accepted = [
            "PPS_PP_desc_10.1",
            "button-ok-title",
            "level_name_1",
            "中文_标题",
        ]
        rejected = [
            "Coming soon",
            "long-term plan",
            "hello",
            "10.1",
            "title_key!",
            " title_key",
        ]

        for text in accepted:
            with self.subTest(text=text):
                self.assertTrue(_is_identifier_like_translation_key(text))
        for text in rejected:
            with self.subTest(text=text):
                self.assertFalse(_is_identifier_like_translation_key(text))

    def test_identifier_like_keys_skip_translation_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_dir = Path(temp_dir) / "records"
            cfg = replace(
                load_config(),
                record_dir=record_dir,
                enable_ai_translation=False,
            )
            records = [
                ScanRecord(
                    file_path="a.json",
                    field="m_Text",
                    source_text="PPS_PP_desc_10.1",
                    translated_text="",
                    path_id=1,
                    font_path_id=None,
                ),
                ScanRecord(
                    file_path="b.json",
                    field="m_Text",
                    source_text="Start Game",
                    translated_text="",
                    path_id=2,
                    font_path_id=None,
                ),
            ]

            with patch(
                "pipeline.translation._translate_one_text_with_provider_retry",
                return_value=("Start Game", "开始游戏"),
            ) as translate_mock:
                result = build_translation_map(records, cfg)

            self.assertNotIn("PPS_PP_desc_10.1", result)
            self.assertEqual(result["Start Game"], "开始游戏")
            translate_mock.assert_called_once_with(cfg, "Start Game", max_attempts=3)
            self.assertTrue((record_dir / "trans_maybe_title.json").is_file())


if __name__ == "__main__":
    unittest.main()
