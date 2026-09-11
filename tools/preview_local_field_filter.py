from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from pipeline.local_field_policy import classify_local_string_field


_FIELD_LINE_RE = re.compile(r"^field:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class ReviewBlock:
    field: str
    normalized_field: str
    sibling_schema: str
    samples: tuple[str, ...]
    text: str


def _metadata_value(block: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*(.*)$", block, re.MULTILINE)
    return match.group(1).strip() if match else ""


def _parse_samples(block: str) -> tuple[str, ...]:
    marker = re.search(r"^sample_values:.*$", block, re.MULTILINE)
    if marker is None:
        return ()
    result: list[str] = []
    for line in block[marker.end() :].splitlines():
        if line.startswith("- "):
            result.append(line[2:])
        elif line.strip():
            break
    return tuple(result)


def parse_review(path: Path) -> tuple[str, list[ReviewBlock]]:
    text = path.read_text(encoding="utf-8")
    first = re.search(r"^field:\s*", text, re.MULTILINE)
    if first is None:
        return text, []
    header = text[: first.start()]
    starts = [match.start() for match in _FIELD_LINE_RE.finditer(text)]
    starts.append(len(text))
    blocks: list[ReviewBlock] = []
    for index in range(len(starts) - 1):
        raw = text[starts[index] : starts[index + 1]].rstrip() + "\n"
        field = _metadata_value(raw, "field")
        normalized = _metadata_value(raw, "normalized_field") or field.split("@@context_", 1)[0]
        blocks.append(
            ReviewBlock(
                field=field,
                normalized_field=normalized,
                sibling_schema=_metadata_value(raw, "sibling_schema"),
                samples=_parse_samples(raw),
                text=raw,
            )
        )
    return header, blocks


def classify(block: ReviewBlock) -> tuple[str, str]:
    result = classify_local_string_field(
        block.normalized_field,
        block.samples,
        block.sibling_schema,
    )
    return result.decision, result.reason


def _filtered_header(original: str) -> str:
    lines = []
    for line in original.rstrip().splitlines():
        if line.startswith("# 高召回规则:"):
            lines.append("# 安全规则: 只有存在明确玩家可见文本证据时才返回；不确定时不要返回。")
        else:
            lines.append(line)
    marker = "# 本文件已完成本地三态过滤，只包含仍需 AI 判断的 unknown 字段。"
    if marker not in lines:
        lines.insert(0, marker)
    return "\n".join(lines).rstrip() + "\n\n"


def build_preview(review_path: Path, output_path: Path, report_path: Path) -> dict[str, object]:
    header, blocks = parse_review(review_path)
    classified: list[dict[str, object]] = []
    unknown_blocks: list[ReviewBlock] = []
    counts: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    for block in blocks:
        decision, reason = classify(block)
        counts[decision] += 1
        reasons[f"{decision}: {reason}"] += 1
        classified.append(
            {
                "field": block.field,
                "normalized_field": block.normalized_field,
                "decision": decision,
                "reason": reason,
                "samples": list(block.samples),
            }
        )
        if decision == "unknown":
            unknown_blocks.append(block)

    output_text = _filtered_header(header) + "\n".join(block.text.rstrip() for block in unknown_blocks) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output_text, encoding="utf-8")

    report: dict[str, object] = {
        "source": str(review_path),
        "output": str(output_path),
        "source_bytes": review_path.stat().st_size,
        "output_bytes": output_path.stat().st_size,
        "candidate_count_before": len(blocks),
        "candidate_count_after": len(unknown_blocks),
        "decision_counts": dict(sorted(counts.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "fields": classified,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Preview deterministic local filtering for AI string-field review.")
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    report = build_preview(args.review, args.output, args.report)
    print(json.dumps({key: report[key] for key in (
        "source_bytes",
        "output_bytes",
        "candidate_count_before",
        "candidate_count_after",
        "decision_counts",
    )}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
