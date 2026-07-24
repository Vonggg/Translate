from __future__ import annotations

import unittest
from types import SimpleNamespace

from pipeline.ai_translation_strategy import DefaultAITranslationStrategy
from pipeline.deepseek_translation_strategy import DeepSeekTranslationStrategy


class AITranslationStrategyTests(unittest.TestCase):
    def test_chinese_source_text_requires_simplified_output_rule(self) -> None:
        cfg = SimpleNamespace(
            ai_translation_batch_max_chars=120000,
            ai_translation_max_output_chars=384000,
        )
        expected_rule = "原始键）本身含有中文，translation 中的所有中文字符也必须是简体中文"

        self.assertIn(expected_rule, DefaultAITranslationStrategy(cfg).system_prompt())
        self.assertIn(expected_rule, DeepSeekTranslationStrategy(cfg).system_prompt())


if __name__ == "__main__":
    unittest.main()
