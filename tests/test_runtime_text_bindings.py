from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.shared import ScanRecord
from pipeline.translation import write_runtime_text_binding_report


class RuntimeTextBindingReportTests(unittest.TestCase):
    def test_reports_i2_and_unbound_text_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            input_root = root / "input"
            records_dir = root / "records"
            i2_file = input_root / "bundle" / "MonoBehaviour" / "I2Languages_1.json"
            unbound_file = input_root / "sharedassets0" / "MonoBehaviour" / "Settings_2.json"
            i2_file.parent.mkdir(parents=True)
            unbound_file.parent.mkdir(parents=True)
            i2_file.write_text(
                json.dumps({"mSource": {"mTerms": {"Array": [{"Languages": ["Bank", "银行"]}]}}}),
                encoding="utf-8",
            )
            unbound_file.write_text(json.dumps({"m_Name": "Settings"}), encoding="utf-8")
            cfg = SimpleNamespace(
                resource_input_root=input_root,
                stage_record_dir=records_dir,
                output_runtime_text_binding_report_json="runtime_text_binding_report.json",
            )
            records = [
                ScanRecord(str(i2_file), "mSource.mTerms.Array[0].Languages.Array[1]", "银行", path_id=0),
                ScanRecord(str(unbound_file), "loadingText", "Loading", path_id=None),
                ScanRecord(str(unbound_file), "m_text", "Static", path_id=12),
            ]

            report_path = write_runtime_text_binding_report(cfg, records)
            report = json.loads(report_path.read_text(encoding="utf-8"))

            self.assertEqual(report["summary"]["i2_language_tables"], 1)
            self.assertEqual(report["summary"]["runtime_or_scriptable_sources"], 1)
            self.assertEqual(report["summary"]["runtime_bound_record_count"], 2)
            kinds = {source["kind"] for source in report["sources"]}
            self.assertEqual(kinds, {"i2_language_table", "runtime_or_scriptable_text"})


if __name__ == "__main__":
    unittest.main()
