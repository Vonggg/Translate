"""Offline cold/warm analysis comparison; never exports resources or calls AI."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from support.config import activate_config_path, load_config
from pipeline.dynamic_translation_dictionary import _load_optional_static_display_values, stringliteral_exclusion_reason
from pipeline.il2cpp_display_usage import analyze_il2cpp_display_usage
from pipeline.shared import atomic_write_json
import pipeline.il2cpp_display_usage as analysis_module


def semantic_projection(result):
    """Evidence is a set of paths; order inside each path remains significant."""
    projection = {}
    for key in ("exact_literals", "derived_influence", "probable_display_literals", "display_enum_types"):
        rows = []
        for original in result.get(key, []):
            row = dict(original)
            if "evidence" in row:
                row["evidence"] = sorted(row["evidence"], key=lambda v: json.dumps(v, sort_keys=True))
            rows.append(row)
        projection[key] = sorted(rows, key=lambda v: json.dumps(v, sort_keys=True))
    return projection


def benchmark_metadata(script_path: Path, output: Path):
    parsers = (analysis_module._load_script, analysis_module._load_script_metadata_method_targets,
               analysis_module._load_script_metadata_type_names,
               analysis_module._load_script_metadata_component_factory_types)
    measurements = []
    for repeat in range(2):
        start = time.perf_counter()
        before = [parser(script_path) for parser in parsers]
        before_seconds = time.perf_counter() - start
        start = time.perf_counter()
        payload = analysis_module._read_script_payload(script_path)
        after = [parser(script_path, payload=payload) for parser in parsers]
        after_seconds = time.perf_counter() - start
        measurements.append(dict(repeat=repeat, separate_read_seconds=before_seconds,
            shared_read_seconds=after_seconds, separate_reads=4, shared_reads=1, equal=before == after))
        del before, after, payload
    atomic_write_json(output, measurements)
    return measurements


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Refuse to overwrite an existing benchmark, even partially completed.
    args.output.mkdir(parents=True, exist_ok=False)
    activate_config_path(args.config)
    cfg = load_config(quiet=True)
    values = _load_optional_static_display_values(cfg.stage_record_dir / "records.json")
    literals = json.loads(cfg.stringliteral_json_path.read_text(encoding="utf-8"))
    excluded = [x["address"] for x in literals if x.get("value") in values
                and stringliteral_exclusion_reason(x["value"]) is None]
    rows = []
    baseline_path = cfg.stage_record_dir / "stringliteral_display_analysis.json"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8")) if baseline_path.exists() else None
    semantic_keys = ("exact_literals", "derived_influence", "probable_display_literals", "display_enum_types")
    for scenario in ("A_empty_analysis_cache", "B_unchanged", "B_unchanged_repeat"):
        start = time.perf_counter()
        result = analyze_il2cpp_display_usage(
            libil2cpp_path=cfg.libil2cpp_arm64_path,
            script_json_path=cfg.il2cpp_script_json_path,
            stringliteral_json_path=cfg.stringliteral_json_path,
            dump_cs_path=cfg.il2cpp_dump_cs_path,
            exclude_literal_addresses=excluded,
            cache_path=args.output / "analysis.cache.json",
            progress_callback=lambda text: print(text, flush=True),
        )
        row = {"scenario": scenario, "wall_seconds": time.perf_counter() - start,
               "stats": result["stats"],
               "ordered_equal_to_baseline": baseline is not None and all(
                   result.get(k) == baseline.get(k) for k in semantic_keys),
               "semantic_equal_to_baseline": baseline is not None and
                   semantic_projection(result) == semantic_projection(baseline)}
        rows.append(row)
        atomic_write_json(args.output / "measurements.json", rows)
        atomic_write_json(args.output / (scenario + ".json"), result)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        if baseline is None:
            baseline = result


if __name__ == "__main__":
    main()
