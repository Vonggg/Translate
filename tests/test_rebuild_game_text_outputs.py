from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from pipeline.dynamic_translation_dictionary import (
    DYNAMIC_DICTIONARY_CPP_FILENAME,
    DYNAMIC_DICTIONARY_OUTPUT_SUBDIR,
    STRINGLITERAL_TRANSLATIONS_FILENAME,
)
from pipeline.translation import rebuild_game_text_outputs


class RebuildGameTextOutputsTests(unittest.TestCase):
    def _cfg(self, record_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(
            stage_record_dir=record_dir,
            stage_dir=record_dir.parent / "output",
            output_trans_json="trans.json",
            output_game_txt="game.txt",
            output_game_chars_txt="game_chars.txt",
        )

    def test_rebuild_merges_static_and_effective_dynamic_dictionary_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            (record_dir / "trans.json").write_text(
                json.dumps(
                    {"Static source": "静态译文", "Static identity": "Static identity"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (record_dir / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps(
                    {
                        "ΩRuntime source": "动态译文",
                        "Dynamic identity Z": "Dynamic identity Z",
                        "Pending Q": "",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rebuild_game_text_outputs(self._cfg(record_dir))

            game_text = (record_dir / "game.txt").read_text(encoding="utf-8")
            game_chars = (record_dir / "game_chars.txt").read_text(encoding="utf-8")
            self.assertIn("静态译文", game_text)
            self.assertIn("Static source", game_text)
            self.assertIn("动态译文", game_text)
            self.assertNotIn("Dynamic identity Z", game_text)
            self.assertIn("Ω", game_chars)
            self.assertIn("动", game_chars)
            self.assertNotIn("Z", game_chars)
            self.assertNotIn("Q", game_chars)

    def test_rebuild_uses_actual_hook_cpp_dictionary_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary) / "records"
            record_dir.mkdir()
            cfg = self._cfg(record_dir)
            (record_dir / "trans.json").write_text(
                json.dumps({"Static source": "静态译文"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cpp_path = (
                cfg.stage_dir / DYNAMIC_DICTIONARY_OUTPUT_SUBDIR / DYNAMIC_DICTIONARY_CPP_FILENAME
            )
            cpp_path.parent.mkdir(parents=True)
            cpp_path.write_text(
                "const NativeUnityTranslationEntry kWholeTextDictionary[] = {\n"
                '    {u"Runtime \\"literal\\"", u"动态词典译文"},\n'
                "    {nullptr, nullptr},\n"
                "};\n\n"
                "const NativeUnityTranslationEntry kSubstringDictionary[] = {\n"
                '    {u"Level {0}", u"等级 {0}"},\n'
                "    {nullptr, nullptr},\n"
                "};\n",
                encoding="utf-8",
            )

            rebuild_game_text_outputs(cfg)

            game_text = (record_dir / "game.txt").read_text(encoding="utf-8")
            game_chars = (record_dir / "game_chars.txt").read_text(encoding="utf-8")
            for value in ("Static source", "静态译文", 'Runtime "literal"', "动态词典译文", "Level {0}", "等级 {0}"):
                self.assertIn(value, game_text)
                self.assertTrue(set(value).issubset(set(game_chars)))

    def test_rebuild_remains_compatible_when_dynamic_dictionary_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            (record_dir / "trans.json").write_text(
                json.dumps({"Play": "开始"}, ensure_ascii=False),
                encoding="utf-8",
            )

            rebuild_game_text_outputs(self._cfg(record_dir))

            self.assertEqual(
                "Play\n开始",
                (record_dir / "game.txt").read_text(encoding="utf-8"),
            )

    def test_static_translation_stage_can_rebuild_without_stale_dynamic_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            record_dir = Path(temporary)
            (record_dir / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps({"Runtime": "动态"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg = self._cfg(record_dir)

            rebuild_game_text_outputs(
                cfg,
                {"Static": "静态"},
                include_dynamic=False,
            )

            self.assertEqual(
                "Static\n静态",
                (record_dir / "game.txt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
