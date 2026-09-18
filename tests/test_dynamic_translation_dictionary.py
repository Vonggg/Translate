from __future__ import annotations

from collections import OrderedDict
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from pipeline.dynamic_translation_dictionary import (
    DYNAMIC_DICTIONARY_CPP_FILENAME,
    RUNTIME_ENUM_TRANSLATIONS_FILENAME,
    STRINGLITERAL_DISPLAY_ANALYSIS_FILENAME,
    STRINGLITERAL_FILTER_REPORT_FILENAME,
    STRINGLITERAL_FILTERED_OUT_FILENAME,
    STRINGLITERAL_TRANSLATIONS_FILENAME,
    cpp_utf16_literal,
    extract_stringliteral_candidates,
    generate_dynamic_translation_dictionary,
    render_dynamic_translation_dictionary,
    select_whole_text_dictionary_entries,
    write_trans_json_to_whole_text_dictionary,
)


class DynamicTranslationDictionaryTests(unittest.TestCase):
    def test_mission_wording_does_not_replace_display_evidence(self) -> None:
        payload = [
            {"value": "Roll 10 times in the Left lane", "address": "0x1"},
            {"value": "Jump over 10 obstacles", "address": "0x2"},
            {"value": "Run 30 second in the Center lane", "address": "0x3"},
            {"value": "Buy 1 Flash Deals item", "address": "0x4"},
            {"value": "Complete 3 Quests", "address": "0x5"},
            {"value": "Collect 2 Coin Magnet", "address": "0x6"},
            {"value": "Use 3 roller skates", "address": "0x7"},
            {"value": "RollerController", "address": "0x8"},
            {"value": "Retry 10 times in the Left lane", "address": "0x9"},
        ]
        candidates, filtered, reasons = extract_stringliteral_candidates(
            payload, exact_display_addresses=[]
        )
        self.assertEqual([], candidates)
        self.assertEqual(len(payload), reasons["not_proven_display"])
        self.assertEqual({x["value"] for x in payload}, {x["value"] for x in filtered})

    def test_trans_json_replaces_only_whole_text_dictionary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trans_path = root / "trans.json"
            cpp_path = root / DYNAMIC_DICTIONARY_CPP_FILENAME
            trans_path.write_text(
                json.dumps(
                    OrderedDict(
                        [
                            ('Play "now"', "立即开始"),
                            ("Line\nBreak", "换行"),
                            ("Pending", ""),
                            ("Same", "Same"),
                        ]
                    ),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            substring_block = (
                "const NativeUnityTranslationEntry kSubstringDictionary[] = {\n"
                '    {u"Score: ", u"得分："},\n'
                "    {nullptr, nullptr},\n"
                "};\n"
            )
            cpp_path.write_text(
                "// keep header\n"
                "const NativeUnityTranslationEntry kWholeTextDictionary[] = {\n"
                '    {u"Old", u"旧"},\n'
                "    {nullptr, nullptr},\n"
                "};\n\n"
                + substring_block,
                encoding="utf-8",
            )

            report = write_trans_json_to_whole_text_dictionary(trans_path, cpp_path)
            rendered = cpp_path.read_text(encoding="utf-8")

            self.assertEqual(4, report["source_count"])
            self.assertEqual(2, report["entry_count"])
            self.assertEqual({"empty": 1, "unchanged": 1}, report["skipped"])
            self.assertIn('{u"Play \\"now\\"", u"立即开始"}', rendered)
            self.assertIn('{u"Line\\nBreak", u"换行"}', rendered)
            self.assertNotIn('u"Old"', rendered)
            self.assertTrue(rendered.endswith(substring_block))

    def test_same_literal_with_both_roles_is_only_one_translation_candidate(self) -> None:
        candidates, filtered, reasons = extract_stringliteral_candidates(
            [{"value": "Play", "address": "0x10"}],
            exact_display_addresses=["0x10"],
            derived_display_addresses=["0x10"],
        )

        self.assertEqual(["Play"], candidates)
        self.assertEqual([], filtered)
        self.assertEqual({}, reasons)

    def test_optional_static_display_value_corroborates_runtime_literal(self) -> None:
        candidates, filtered, reasons = extract_stringliteral_candidates(
            [{"value": "Get Spins", "address": "0x10"}],
            exact_display_addresses=[],
            static_display_values=["Get Spins"],
        )

        self.assertEqual(["Get Spins"], candidates)
        self.assertEqual([], filtered)
        self.assertEqual({}, reasons)

    def test_explicit_unicode_escape_is_filtered_but_real_unicode_is_kept(self) -> None:
        payload = [
            {"value": r"prefix \u4F60 suffix", "address": "0x1"},
            {"value": r"emoji \U0001F600 tail", "address": "bad-address"},
            {"value": "正常 Unicode 文本：你好😀", "address": "0x3"},
        ]

        candidates, filtered, reasons = extract_stringliteral_candidates(
            payload,
            exact_display_addresses=["0x3"],
        )

        self.assertEqual(["正常 Unicode 文本：你好😀"], candidates)
        self.assertEqual(2, reasons["explicit_unicode_escape"])
        self.assertEqual(
            {"0x1", "bad-address"},
            {
                item["address"]
                for item in filtered
                if item["reason"] == "explicit_unicode_escape"
            },
        )

    def test_candidate_filter_is_conservative_and_preserves_order(self) -> None:
        payload = [
            {"value": " Play ", "address": "0x1"},
            {"value": "PlayButton", "address": "0x2"},
            {"value": "PLAYBUTTON", "address": "0x2A"},
            {"value": "Hello {0}\nagain", "address": "0x3"},
            {"value": " Play ", "address": "0x4"},
            {"value": "  \t", "address": "0x5"},
            {"value": "A\0B", "address": "0x6"},
            {"value": "A\x01B", "address": "0x7"},
            {"value": "123 --", "address": "0x8"},
            {"value": "a" * 1001, "address": "0x9"},
            {"value": "https://example.com", "address": "0xA"},
            {"value": "System.String", "address": "0xB"},
            {"value": "bad\ud800", "address": "0xC"},
            {"value": "\n {\"key\": \"value\"} \n", "address": "0xD"},
            {"value": "Reach level {0}", "address": "0xE"},
            {"value": "AnalyticsEvent", "address": "0xF"},
            {"value": "_SkeletonData", "address": "0x10"},
        ]

        candidates, filtered, reasons = extract_stringliteral_candidates(
            payload,
            exact_display_addresses=[
                *[f"0x{address:X}" for address in range(1, 14)],
                "0x2A",
                "0x10",
            ],
            derived_display_addresses=["0xE"],
        )

        self.assertEqual([" Play ", "PlayButton", "Hello {0}\nagain", "Reach level {0}"], candidates)
        self.assertEqual(13, len(filtered))
        self.assertEqual(1, reasons["duplicate"])
        self.assertEqual(1, reasons["ascii_case_alias"])
        self.assertEqual(1, reasons["empty_or_whitespace"])
        self.assertEqual(1, reasons["contains_nul"])
        self.assertEqual(1, reasons["control_character"])
        self.assertEqual(1, reasons["no_language_text"])
        self.assertEqual(1, reasons["too_long"])
        self.assertEqual(1, reasons["machine_uri"])
        self.assertEqual(1, reasons["machine_qualified_identifier"])
        self.assertEqual(1, reasons["invalid_surrogate"])
        self.assertEqual(1, reasons["machine_json"])
        self.assertEqual(1, reasons["machine_private_identifier"])
        self.assertEqual(1, reasons["not_proven_display"])
        invalid = next(item for item in filtered if item["reason"] == "invalid_surrogate")
        self.assertEqual("bad\\ud800", invalid["escaped_value"])

    def test_ascii_case_alias_keeps_first_canonical_source(
        self,
    ) -> None:
        candidates, filtered, reasons = extract_stringliteral_candidates(
            [
                {"value": "May", "address": "0x1"},
                {"value": "may", "address": "0x2"},
                {"value": "Echo", "address": "0x3"},
                {"value": "Echo", "address": "0x4"},
            ],
            exact_display_addresses=["0x1", "0x2", "0x3", "0x4"],
        )

        self.assertEqual(["May", "Echo"], candidates)
        self.assertEqual(1, reasons["ascii_case_alias"])
        self.assertEqual(1, reasons["duplicate"])
        self.assertEqual(
            {"may"},
            {
                item["value"]
                for item in filtered
                if item["reason"] == "ascii_case_alias"
            },
        )

    def test_ascii_case_alias_is_detected_across_exact_and_derived_roles(self) -> None:
        candidates, filtered, reasons = extract_stringliteral_candidates(
            [
                {"value": "Play", "address": "0x1"},
                {"value": "PLAY", "address": "0x2"},
                {"value": "Safe", "address": "0x3"},
            ],
            exact_display_addresses=["0x1", "0x3"],
            derived_display_addresses=["0x2"],
        )

        self.assertEqual(["Play", "Safe"], candidates)
        self.assertEqual(1, reasons["ascii_case_alias"])
        self.assertEqual(
            {"PLAY"},
            {
                item["value"]
                for item in filtered
                if item["reason"] == "ascii_case_alias"
            },
        )

    def test_derived_numeric_placeholders_bypass_only_no_language_filter(self) -> None:
        payload = [
            {"value": "{0}", "address": "0x1"},
            {"value": "{0:N2}", "address": "0x2"},
            {"value": "{0,5}", "address": "0x3"},
            {"value": "--- 123", "address": "0x4"},
            {"value": r"{0} \u4F60", "address": "0x5"},
            {"value": "{0}", "address": "0x6"},
            {"value": "{{0}}", "address": "0x7"},
            {"value": "{{{0}}}", "address": "0x8"},
        ]

        candidates, _filtered, reasons = extract_stringliteral_candidates(
            payload,
            exact_display_addresses=["0x6"],
            derived_display_addresses=[
                "0x1",
                "0x2",
                "0x3",
                "0x4",
                "0x5",
                "0x7",
                "0x8",
            ],
        )

        self.assertEqual(["{0}", "{0:N2}", "{0,5}", "{{{0}}}"], candidates)
        self.assertEqual(2, reasons["no_language_text"])
        self.assertEqual(1, reasons["explicit_unicode_escape"])
        self.assertEqual(1, reasons["duplicate"])

    def test_cpp_literal_preserves_whitespace_and_safely_splits_hex_escape(self) -> None:
        value = ' A"\\\t\r\n\x01B😀 '

        literal = cpp_utf16_literal(value)

        self.assertEqual(
            'u" A\\"\\\\\\t\\r\\n" u"\\x0001" u"B\\U0001F600 "',
            literal,
        )

    def test_dictionary_keeps_first_ascii_case_canonical_source(self) -> None:
        translations = OrderedDict(
            [
                ("Play", "开始"),
                ("PLAY", "开始"),
                ("Quit", "退出"),
                ("Same", "Same"),
                ("", "空"),
                ("Nul", "坏\0值"),
            ]
        )

        entries, skipped = select_whole_text_dictionary_entries(translations)
        rendered = render_dynamic_translation_dictionary(translations)

        self.assertEqual([("Play", "开始"), ("Quit", "退出")], entries)
        self.assertEqual(1, skipped["ascii_case_alias"])
        self.assertEqual(1, skipped["unchanged"])
        self.assertEqual(1, skipped["empty"])
        self.assertEqual(1, skipped["contains_nul"])
        self.assertIn('{u"Quit", u"退出"}', rendered)
        self.assertIn('{u"Play", u"开始"}', rendered)
        self.assertNotIn('u"PLAY"', rendered)
        self.assertIn(
            "const NativeUnityTranslationEntry kSubstringDictionary[] = {\n"
            "    {nullptr, nullptr},\n"
            "};",
            rendered,
        )

        conflicting_entries, conflicting_skipped = select_whole_text_dictionary_entries(
            OrderedDict([("Play", "开始"), ("PLAY", "播放")])
        )
        self.assertEqual([("Play", "开始")], conflicting_entries)
        self.assertEqual(1, conflicting_skipped["ascii_case_conflict"])

    def test_render_writes_dual_roles_to_both_arrays_and_sorts_substrings_stably(self) -> None:
        translations = OrderedDict(
            [
                ("AB", "短"),
                ("A😀", "表情"),
                ("ABC", "三字"),
                ("Dual", "双重"),
                ("Pending", ""),
                ("Same", "Same"),
            ]
        )

        rendered = render_dynamic_translation_dictionary(
            translations,
            whole_sources=["Dual"],
            substring_sources=["AB", "A😀", "ABC", "Dual", "Pending", "Same"],
        )

        self.assertEqual(2, rendered.count("    {nullptr, nullptr},"))
        self.assertEqual(2, rendered.count('{u"Dual", u"双重"}'))
        self.assertNotIn('u"Pending"', rendered)
        self.assertNotIn('u"Same"', rendered)
        self.assertLess(rendered.index('u"Dual"', rendered.index("kSubstringDictionary")),
                        rendered.index('u"A\\U0001F600"'))
        self.assertLess(rendered.index('u"A\\U0001F600"'), rendered.index('u"ABC"'))
        self.assertLess(rendered.index('u"ABC"'), rendered.index('u"AB"'))

    def test_render_derives_runtime_fragments_from_display_format_strings(self) -> None:
        translations = OrderedDict(
            [
                ("'{0}' starting.", "“{0}”开始。"),
                ("+{0} seconds", "+{0} 秒"),
                ("{0}$", "{0}美元"),
            ]
        )

        rendered = render_dynamic_translation_dictionary(
            translations,
            whole_sources=[],
            substring_sources=list(translations),
        )

        self.assertIn('{u" starting.", u"开始。"}', rendered)
        self.assertIn('{u" seconds", u" 秒"}', rendered)
        self.assertNotIn('{u"$", u"美元"}', rendered)

    def test_generator_uses_independent_resume_cache_and_writes_reports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            source_path = root / "stringliteral.json"
            source_path.write_text(
                json.dumps(
                    [
                        {"value": "Resume", "address": "0x1"},
                        {"value": "Game_Title", "address": "0x2"},
                        {"value": "Score {0}", "address": "0x3"},
                        {"value": "{0,5}", "address": "0x4"},
                        {"value": "{0}", "address": "0x5"},
                        {"value": "123", "address": "0x6"},
                        {"value": "Static only", "address": "0x7"},
                    ]
                ),
                encoding="utf-8",
            )
            records.mkdir()
            (records / "trans.json").write_text(
                json.dumps({"Static only": "静态译文"}, ensure_ascii=False),
                encoding="utf-8",
            )
            (records / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps(
                    {"Resume": "继续", "stale": "旧值"},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )
            calls: list[tuple[list[str], dict[str, object]]] = []

            def analyzer(**_kwargs):
                return {
                    "schema_version": 1,
                    "inputs": {},
                    "exact_literals": [
                        {"address": "0x1", "value": "Resume", "evidence": []},
                        {"address": "0x2", "value": "Game_Title", "evidence": []},
                    ],
                    "derived_influence": [
                        {"address": "0x1", "value": "Resume", "evidence": []},
                        {"address": "0x3", "value": "Score {0}", "evidence": []},
                        {"address": "0x4", "value": "{0,5}", "evidence": []},
                        {"address": "0x5", "value": "{0}", "evidence": []},
                        {"address": "0x6", "value": "123", "evidence": []},
                    ],
                    "unresolved": [],
                    "display_sinks": [],
                    "render_sinks": [],
                    "stats": {"fixture": True},
                }

            def builder(source_texts, _cfg, **kwargs):
                calls.append((list(source_texts), kwargs))
                resumed = json.loads(kwargs["cache_path"].read_text(encoding="utf-8"))
                self.assertEqual("继续", resumed["Resume"])
                self.assertNotIn("stale", resumed)
                return {
                    "Resume": "继续",
                    "Game_Title": "游戏标题",
                    "Score {0}": "得分 {0}",
                    "{0,5}": "{0,5}",
                }

            output_path = generate_dynamic_translation_dictionary(
                cfg,
                translation_builder=builder,
                usage_analyzer=analyzer,
            )

            self.assertEqual(
                root / "output" / "Hook_Translate" / DYNAMIC_DICTIONARY_CPP_FILENAME,
                output_path,
            )
            self.assertEqual(
                [["Resume", "Game_Title", "Score {0}", "{0,5}", "{0}"]],
                [call[0] for call in calls],
            )
            self.assertEqual(False, calls[0][1]["exclude_identifier_like"])
            self.assertIn("libil2cpp.so", calls[0][1]["source_label"])
            self.assertEqual(
                ["whole", "substring"],
                calls[0][1]["source_contexts"]["Resume"]["dictionary_role"],
            )
            self.assertEqual(
                {
                    "Resume": "继续",
                    "Game_Title": "游戏标题",
                    "Score {0}": "得分 {0}",
                    "{0,5}": "{0,5}",
                    "{0}": "",
                },
                json.loads(
                    (records / STRINGLITERAL_TRANSLATIONS_FILENAME).read_text(encoding="utf-8")
                ),
            )
            generated = output_path.read_text(encoding="utf-8")
            self.assertIn('{u"Resume", u"继续"}', generated)
            self.assertIn('{u"Game_Title", u"游戏标题"}', generated)
            self.assertIn('{u"Score {0}", u"得分 {0}"}', generated)
            self.assertEqual(2, generated.count('{u"Resume", u"继续"}'))
            self.assertNotIn('{u"{0,5}"', generated)
            self.assertNotIn('{u"{0}"', generated)
            self.assertNotIn("Static only", generated)
            self.assertEqual(2, generated.count("    {nullptr, nullptr},"))
            self.assertTrue((records / STRINGLITERAL_FILTERED_OUT_FILENAME).is_file())
            self.assertTrue((records / STRINGLITERAL_DISPLAY_ANALYSIS_FILENAME).is_file())
            report = json.loads(
                (records / STRINGLITERAL_FILTER_REPORT_FILENAME).read_text(encoding="utf-8")
            )
            self.assertEqual(5, report["candidate_count"])
            self.assertEqual(2, report["exact_candidate_count"])
            self.assertEqual(4, report["substring_candidate_count"])
            self.assertEqual(1, report["dual_role_candidate_count"])
            self.assertEqual(2, report["filtered_count"])
            self.assertEqual(2, report["whole_dictionary_entry_count"])
            self.assertEqual(3, report["substring_dictionary_entry_count"])

    def test_complete_cache_does_not_require_translation_builder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text('[{"value": "Quit", "address": "0x1"}]', encoding="utf-8")
            (records / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps({"Quit": "退出"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )

            def analyzer(**_kwargs):
                return {
                    "schema_version": 1,
                    "inputs": {},
                    "exact_literals": [
                        {"address": "0x1", "value": "Quit", "evidence": []}
                    ],
                    "derived_influence": [],
                    "unresolved": [],
                    "display_sinks": [],
                    "render_sinks": [],
                    "stats": {},
                }

            output_path = generate_dynamic_translation_dictionary(
                cfg,
                usage_analyzer=analyzer,
            )

            self.assertIn("退出", output_path.read_text(encoding="utf-8"))

    def test_builtin_analysis_delegates_cache_validation_to_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Unused","address":"0x10"}]',
                encoding="utf-8",
            )
            analysis_cache = records / "stringliteral_display_analysis.cache.json"
            analysis_cache.write_text('{"stale":true}', encoding="utf-8")
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )
            analysis = {
                "schema_version": 1,
                "inputs": {},
                "exact_literals": [],
                "derived_influence": [],
                "unresolved": [],
                "display_sinks": [],
                "render_sinks": [],
                "stats": {},
            }

            with patch(
                "pipeline.il2cpp_display_usage.analyze_il2cpp_display_usage",
                return_value=analysis,
            ) as analyzer:
                generate_dynamic_translation_dictionary(cfg)

            self.assertEqual(analysis_cache.read_text(encoding="utf-8"), '{"stale":true}')
            self.assertEqual(analysis_cache, analyzer.call_args.kwargs["cache_path"])
            self.assertIs(analyzer.call_args.kwargs["use_cache"], True)

    def test_generator_skips_static_heuristic_when_records_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Get Spins","address":"0x10"}]',
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )

            def analyzer(**_kwargs):
                return {
                    "schema_version": 1,
                    "inputs": {},
                    "exact_literals": [],
                    "derived_influence": [],
                    "unresolved": [],
                    "display_sinks": [],
                    "render_sinks": [],
                    "stats": {},
                }

            generate_dynamic_translation_dictionary(cfg, usage_analyzer=analyzer)

            report = json.loads(
                (records / STRINGLITERAL_FILTER_REPORT_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(0, report["candidate_count"])
            self.assertEqual(0, report["heuristic_static_display_value_count"])

    def test_generator_reuses_static_translation_when_records_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Get Spins","address":"0x10"}]',
                encoding="utf-8",
            )
            (records / "records.json").write_text(
                json.dumps([{"field": "m_Text", "source_text": "Get Spins"}]),
                encoding="utf-8",
            )
            (records / "trans.json").write_text(
                json.dumps({"Get Spins": "获取抽奖次数"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )

            analyzer_calls = []

            def analyzer(**kwargs):
                analyzer_calls.append(kwargs)
                return {
                    "schema_version": 1,
                    "inputs": {},
                    "exact_literals": [],
                    "derived_influence": [],
                    "unresolved": [],
                    "display_sinks": [],
                    "render_sinks": [],
                    "stats": {},
                }

            output_path = generate_dynamic_translation_dictionary(
                cfg,
                usage_analyzer=analyzer,
            )

            self.assertIn(
                '{u"Get Spins", u"获取抽奖次数"}',
                output_path.read_text(encoding="utf-8"),
            )
            report = json.loads(
                (records / STRINGLITERAL_FILTER_REPORT_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(1, report["heuristic_static_display_value_count"])
            self.assertEqual(1, report["heuristic_static_display_candidate_count"])
            self.assertEqual([], analyzer_calls)

    def test_records_only_text_never_enters_dynamic_dictionary_or_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Shared text","address":"0x10"}]',
                encoding="utf-8",
            )
            (records / "records.json").write_text(
                json.dumps(
                    [
                        {"field": "m_text", "source_text": "Shared text"},
                        {"field": "m_text", "source_text": "Records only"},
                    ]
                ),
                encoding="utf-8",
            )
            (records / "trans.json").write_text(
                json.dumps(
                    {
                        "Shared text": "交集文本",
                        "Records only": "仅静态记录",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )

            output_path = generate_dynamic_translation_dictionary(
                cfg,
                usage_analyzer=lambda **_kwargs: {
                    "exact_literals": [],
                    "derived_influence": [],
                    "unresolved": [],
                    "stats": {},
                },
            )

            generated = output_path.read_text(encoding="utf-8")
            cache = json.loads(
                (records / STRINGLITERAL_TRANSLATIONS_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn('{u"Shared text", u"交集文本"}', generated)
            self.assertNotIn("Records only", generated)
            self.assertEqual({"Shared text": "交集文本"}, cache)

    def test_records_intersection_is_excluded_from_native_chain_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Shared","address":"0x10"},'
                '{"value":"Needs chain","address":"0x20"}]',
                encoding="utf-8",
            )
            (records / "records.json").write_text(
                json.dumps([{"field": "m_text", "source_text": "Shared"}]),
                encoding="utf-8",
            )
            (records / "trans.json").write_text(
                json.dumps({"Shared": "共有"}, ensure_ascii=False),
                encoding="utf-8",
            )
            captured = {}

            def analyzer(**kwargs):
                captured.update(kwargs)
                return {
                    "exact_literals": [],
                    "derived_influence": [],
                    "unresolved": [],
                    "stats": {},
                }

            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )
            generate_dynamic_translation_dictionary(cfg, usage_analyzer=analyzer)

            self.assertEqual({0x10}, captured["exclude_literal_addresses"])

    def test_missing_stringliteral_is_an_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = SimpleNamespace(
                stringliteral_json_path=root / "missing.json",
                stage_record_dir=root / "records",
            )

            with self.assertRaisesRegex(FileNotFoundError, "未找到 stringliteral.json"):
                generate_dynamic_translation_dictionary(cfg, translation_builder=lambda *_a, **_k: {})

    def test_generator_ignores_project_and_previous_output_dictionaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Quit","address":"0x1"}]', encoding="utf-8"
            )
            (records / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps({"Quit": "退出"}, ensure_ascii=False), encoding="utf-8"
            )
            project_cpp = root / "game-name" / "cpp"
            project_cpp.mkdir(parents=True)
            (project_cpp / DYNAMIC_DICTIONARY_CPP_FILENAME.replace(".generated", "")).write_text(
                "const NativeUnityTranslationEntry kWholeTextDictionary[] = {\n"
                "    {u\"Manual whole\", u\"手工整句\"},\n"
                "    {nullptr, nullptr},\n};\n"
                "const NativeUnityTranslationEntry kSubstringDictionary[] = {\n"
                "    {u\"Manual {0}\", u\"手工 {0}\"},\n"
                "    {nullptr, nullptr},\n};\n",
                encoding="utf-8",
            )
            previous_output = root / "output" / "Hook_Translate"
            previous_output.mkdir(parents=True)
            (previous_output / DYNAMIC_DICTIONARY_CPP_FILENAME).write_text(
                "const NativeUnityTranslationEntry kWholeTextDictionary[] = {\n"
                "    {u\"Previous whole\", u\"上次整句\"},\n"
                "    {nullptr, nullptr},\n};\n"
                "const NativeUnityTranslationEntry kSubstringDictionary[] = {\n"
                "    {u\"Previous {0}\", u\"上次 {0}\"},\n"
                "    {nullptr, nullptr},\n};\n",
                encoding="utf-8",
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
                project_dir=root,
            )

            output = generate_dynamic_translation_dictionary(
                cfg,
                usage_analyzer=lambda **_kwargs: {
                    "exact_literals": [{"address": "0x1", "value": "Quit", "evidence": []}],
                    "derived_influence": [],
                    "unresolved": [],
                    "stats": {},
                },
            ).read_text(encoding="utf-8")

            self.assertIn('{u"Quit", u"退出"}', output)
            self.assertNotIn("Manual whole", output)
            self.assertNotIn("Manual {0}", output)
            self.assertNotIn("Previous whole", output)
            self.assertNotIn("Previous {0}", output)

    def test_proven_enum_tostring_members_use_separate_cache_and_dictionary_role(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = root / "records"
            records.mkdir()
            source_path = root / "stringliteral.json"
            source_path.write_text(
                '[{"value":"Quit","address":"0x1"}]', encoding="utf-8"
            )
            (records / STRINGLITERAL_TRANSLATIONS_FILENAME).write_text(
                json.dumps({"Quit": "退出"}, ensure_ascii=False), encoding="utf-8"
            )
            cfg = SimpleNamespace(
                stringliteral_json_path=source_path,
                stage_record_dir=records,
                stage_dir=root / "output",
            )

            (records / RUNTIME_ENUM_TRANSLATIONS_FILENAME).write_text(
                json.dumps({"cal12": "12号口径"}, ensure_ascii=False), encoding="utf-8"
            )

            def builder(candidates, *_args, **_kwargs):
                self.assertEqual(["Quit", "cal12", "Rocket"], list(candidates))
                resume = json.loads(Path(_kwargs["cache_path"]).read_text(encoding="utf-8"))
                self.assertEqual(resume["cal12"], "12号口径")
                self.assertEqual(resume["Quit"], "退出")
                self.assertEqual(resume["Rocket"], "")
                return {"Quit": "退出", "cal12": "12号口径", "Rocket": "火箭弹"}

            generated = generate_dynamic_translation_dictionary(
                cfg,
                translation_builder=builder,
                usage_analyzer=lambda **_kwargs: {
                    "exact_literals": [
                        {"address": "0x1", "value": "Quit", "evidence": []}
                    ],
                    "derived_influence": [],
                    "display_enum_types": [
                        {
                            "enum_type": "Game.Weapons.AmmoTypes",
                            "members": ["cal12", "Rocket"],
                            "roles": ["derived"],
                            "evidence": [],
                        }
                    ],
                    "unresolved": [],
                    "stats": {},
                },
            ).read_text(encoding="utf-8")

            self.assertIn('{u"Quit", u"退出"}', generated)
            self.assertIn('{u"cal12", u"12号口径"}', generated)
            self.assertIn('{u"Rocket", u"火箭弹"}', generated)
            literal_cache = json.loads(
                (records / STRINGLITERAL_TRANSLATIONS_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            enum_cache = json.loads(
                (records / RUNTIME_ENUM_TRANSLATIONS_FILENAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual({"Quit": "退出"}, literal_cache)
            self.assertEqual(
                {"cal12": "12号口径", "Rocket": "火箭弹"}, enum_cache
            )


if __name__ == "__main__":
    unittest.main()
