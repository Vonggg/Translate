from __future__ import annotations

import unittest

from support.script_output_cleanup import (
    count_script_output_rules,
    expand_script_output_ids,
    load_script_output_manifest,
)


class ScriptOutputCleanupManifestTests(unittest.TestCase):
    def test_all_expands_steps_zero_through_nine_and_counts_their_rules(self) -> None:
        manifest = load_script_output_manifest()
        expected_ids = [str(index) for index in range(10)]

        expanded = expand_script_output_ids(manifest, ["a"])

        self.assertEqual(["a", *expected_ids], expanded)
        self.assertEqual(
            sum(count_script_output_rules(manifest, [script_id]) for script_id in expected_ids),
            count_script_output_rules(manifest, ["a"]),
        )
        self.assertGreater(count_script_output_rules(manifest, ["a"]), 0)

    def test_scan_cleanup_manifest_contains_all_ai_field_context_artifacts(self) -> None:
        manifest = load_script_output_manifest()
        outputs = manifest["scripts"]["0"]["outputs"]
        record_files = {
            rule.get("path")
            for rule in outputs
            if rule.get("base") == "records" and rule.get("kind") == "file"
        }

        self.assertTrue(
            {
                "records_unfiltered.json",
                "string_field_stats.json",
                "string_field_stats.tsv",
                "string_field_review.txt",
                "bitmap_font_detection.json",
            }.issubset(record_files)
        )


if __name__ == "__main__":
    unittest.main()
