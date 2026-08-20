from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable


TEXTASSET_CSV_KIND = "textasset_csv"
TEXTASSET_SCRIPT_FIELD = "m_Script"

_KEY_COLUMN_NAMES = ("key", "term", "id")
_SOURCE_COLUMN_NAMES = ("en", "english", "source", "source_text", "sourcetext", "text")


def _is_textasset_path(json_path: Path) -> bool:
    return any(part.casefold() == "textasset" for part in json_path.parts)


def _line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _detect_csv(text: str) -> tuple[list[list[str]], csv.Dialect] | None:
    if not text or "\n" not in text and "\r" not in text:
        return None
    try:
        dialect = csv.Sniffer().sniff(text[:32768], delimiters=",\t;")
        rows = list(csv.reader(io.StringIO(text, newline=""), dialect))
    except (csv.Error, UnicodeError):
        return None
    if len(rows) < 2 or len(rows[0]) < 2:
        return None
    return rows, dialect


def _column_index(header: list[str], candidates: Iterable[str]) -> int | None:
    normalized = [value.lstrip("\ufeff").strip().casefold() for value in header]
    for candidate in candidates:
        try:
            return normalized.index(candidate)
        except ValueError:
            continue
    return None


def _key_column_index(header: list[str]) -> int | None:
    """Resolve the localization key column, including NGUI's unnamed first column."""
    key_index = _column_index(header, _KEY_COLUMN_NAMES)
    if key_index is not None:
        return key_index
    if header and not header[0].lstrip("\ufeff").strip():
        return 0
    return None


def extract_textasset_csv_cells(data: Any, json_path: Path) -> list[dict[str, Any]]:
    """Extract translatable source-language cells from a localization TextAsset CSV."""
    if not _is_textasset_path(json_path) or not isinstance(data, dict):
        return []
    script = data.get(TEXTASSET_SCRIPT_FIELD)
    if not isinstance(script, str):
        return []
    detected = _detect_csv(script)
    if detected is None:
        return []
    rows, _dialect = detected
    header = rows[0]
    key_index = _key_column_index(header)
    source_index = _column_index(header, _SOURCE_COLUMN_NAMES)
    if key_index is None or source_index is None or key_index == source_index:
        return []

    key_column = header[key_index].lstrip("\ufeff").strip()
    source_column = header[source_index].lstrip("\ufeff").strip()
    field = f"{TEXTASSET_SCRIPT_FIELD}.csv[].{source_column}"
    cells: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows[1:], start=1):
        if max(key_index, source_index) >= len(row):
            continue
        row_key = row[key_index].strip()
        source_text = row[source_index]
        if not row_key or not source_text.strip():
            continue
        cells.append(
            {
                "field": field,
                "source_text": source_text,
                "locator": {
                    "kind": TEXTASSET_CSV_KIND,
                    "container_field": TEXTASSET_SCRIPT_FIELD,
                    "row_index": row_index,
                    "row_key": row_key,
                    "key_column": key_column,
                    "column": source_column,
                },
            }
        )
    return cells


def _find_target_row(
    rows: list[list[str]],
    locator: dict[str, Any],
    key_index: int,
    source_index: int,
    source_text: str,
) -> int | None:
    row_key = locator.get("row_key")
    row_index = locator.get("row_index")
    if isinstance(row_index, int) and 0 < row_index < len(rows):
        row = rows[row_index]
        if (
            max(key_index, source_index) < len(row)
            and (not isinstance(row_key, str) or row[key_index] == row_key)
            and row[source_index] == source_text
        ):
            return row_index
    if not isinstance(row_key, str) or not row_key:
        return None
    for index, row in enumerate(rows[1:], start=1):
        if max(key_index, source_index) < len(row) and row[key_index] == row_key and row[source_index] == source_text:
            return index
    return None


def apply_textasset_csv_translations(
    data: Any,
    records: Iterable[Any],
    translations: dict[str, str],
) -> int:
    """Apply only selected embedded CSV records and write the rebuilt CSV back to m_Script."""
    if not isinstance(data, dict):
        return 0
    script = data.get(TEXTASSET_SCRIPT_FIELD)
    if not isinstance(script, str):
        return 0
    embedded_records = [
        record
        for record in records
        if isinstance(getattr(record, "embedded_locator", None), dict)
        and record.embedded_locator.get("kind") == TEXTASSET_CSV_KIND
        and record.source_text in translations
    ]
    if not embedded_records:
        return 0
    detected = _detect_csv(script)
    if detected is None:
        return 0
    rows, dialect = detected
    header = rows[0]
    changes = 0
    changed_locations: set[tuple[int, int]] = set()
    for record in embedded_records:
        locator = record.embedded_locator
        key_column = str(locator.get("key_column", ""))
        source_column = str(locator.get("column", ""))
        key_index = (
            _key_column_index(header)
            if not key_column.strip()
            else _column_index(header, (key_column.casefold(),))
        )
        source_index = _column_index(header, (source_column.casefold(),))
        if key_index is None or source_index is None:
            continue
        target_row = _find_target_row(rows, locator, key_index, source_index, record.source_text)
        if target_row is None or (target_row, source_index) in changed_locations:
            continue
        translated = translations.get(record.source_text)
        if not isinstance(translated, str) or translated == rows[target_row][source_index]:
            continue
        rows[target_row][source_index] = translated
        changed_locations.add((target_row, source_index))
        changes += 1

    if not changes:
        return 0
    line_ending = _line_ending(script)
    had_trailing_newline = script.endswith(("\r\n", "\r", "\n"))
    output = io.StringIO(newline="")
    writer = csv.writer(
        output,
        delimiter=dialect.delimiter,
        quotechar=dialect.quotechar or '"',
        escapechar=dialect.escapechar,
        doublequote=dialect.doublequote,
        skipinitialspace=dialect.skipinitialspace,
        quoting=dialect.quoting,
        lineterminator=line_ending,
    )
    writer.writerows(rows)
    rebuilt = output.getvalue()
    if not had_trailing_newline and rebuilt.endswith(line_ending):
        rebuilt = rebuilt[: -len(line_ending)]
    data[TEXTASSET_SCRIPT_FIELD] = rebuilt
    return changes
