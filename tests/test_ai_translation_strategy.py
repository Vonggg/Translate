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

    def test_prompt_warns_about_json_property_delimiters(self) -> None:
        cfg = SimpleNamespace(
            ai_translation_batch_max_chars=1200000,
            ai_translation_max_output_chars=384000,
        )
        expected_rule = "绝不能误写成 >、=，也不能漏掉冒号"

        self.assertIn(expected_rule, DefaultAITranslationStrategy(cfg).system_prompt())
        self.assertIn(expected_rule, DeepSeekTranslationStrategy(cfg).system_prompt())

    def test_batches_are_split_by_dynamic_output_budget(self) -> None:
        cfg = SimpleNamespace(
            ai_translation_batch_max_chars=1200000,
            ai_translation_max_output_chars=384000,
            ai_translation_output_safety_divisor=12,
        )
        strategy = DeepSeekTranslationStrategy(cfg)
        batches = strategy.build_batches([(index, "Name") for index in range(901)])

        self.assertGreater(len(batches), 1)
        self.assertTrue(
            all(
                sum(strategy.estimate_output_chars(text) for _index, text in batch)
                <= strategy.batch_output_budget_chars
                for batch in batches
            )
        )
        self.assertGreater(len(batches[0]), 300)

    def test_known_json_delimiter_typo_is_repaired(self) -> None:
        cfg = SimpleNamespace(
            ai_translation_batch_max_chars=1200000,
            ai_translation_max_output_chars=384000,
        )
        parsed = DeepSeekTranslationStrategy(cfg).parse_response(
            '{"items":[{"id":6789,"translation":"琳迪"},{"id">6790,"translation":"琳迪"}]}'
        )

        self.assertEqual(parsed, {6789: "琳迪", 6790: "琳迪"})


if __name__ == "__main__":
    unittest.main()
