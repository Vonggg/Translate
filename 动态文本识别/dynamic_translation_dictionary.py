from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Callable, Collection, Mapping, Sequence
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Optional

from shared import atomic_write_json, read_json

# 此副本仅供独立“动态文本识别”工具使用。完整流水线配置只出现在
# 类型标注中；工具本身不会加载或读取主项目的 config.json。
PipelineConfig = Any


STRINGLITERAL_TRANSLATIONS_FILENAME = "stringliteral_trans.json"
DYNAMIC_DICTIONARY_CPP_FILENAME = "native_unity_translation_dictionary.generated.cpp"
DYNAMIC_DICTIONARY_OUTPUT_SUBDIR = "Hook_Translate"
STRINGLITERAL_FILTER_REPORT_FILENAME = "stringliteral_filter_report.json"
STRINGLITERAL_FILTERED_OUT_FILENAME = "stringliteral_filtered_out.json"
STRINGLITERAL_DISPLAY_ANALYSIS_FILENAME = "stringliteral_display_analysis.json"
STRINGLITERAL_MAYBE_TITLE_FILENAME = "stringliteral_trans_maybe_title.json"
DYNAMIC_TRANSLATION_ARTIFACT_PREFIX = "ai_stringliteral_translation"
RUNTIME_ENUM_TRANSLATIONS_FILENAME = "runtime_enum_trans.json"
MAX_STRINGLITERAL_CANDIDATE_CHARS = 1000
_WHOLE_TEXT_DICTIONARY_PATTERN = re.compile(
    r"^const\s+NativeUnityTranslationEntry\s+kWholeTextDictionary\[\]\s*=\s*\{\r?\n"
    r".*?^\};[ \t]*",
    re.MULTILINE | re.DOTALL,
)
_CPP_UTF16_LITERAL_TOKEN_PATTERN = re.compile(r'u"((?:\\.|[^"\\])*)"')


def _cpp_dictionary_array_pattern(array_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^const\s+NativeUnityTranslationEntry\s+{re.escape(array_name)}\[\]\s*=\s*\{{\r?\n"
        r"(?P<body>.*?)^\};[ \t]*",
        re.MULTILINE | re.DOTALL,
    )


TranslationBuilder = Callable[..., Optional[Mapping[str, str]]]


_URI_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://\S+$")
_EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/][^\r\n]+$")
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(r"^/(?:[^/\r\n]+/)*[^/\r\n]*$")
_GUID_PATTERN = re.compile(
    r"^(?:\{)?[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?:\})?$"
)
_HEX_BLOB_PATTERN = re.compile(r"^(?:0x)?[0-9A-Fa-f]{16,}$")
_KNOWN_QUALIFIED_IDENTIFIER_PATTERN = re.compile(
    r"^(?:(?:System|UnityEngine|UnityEditor|Microsoft|TMPro|Android|java|javax|kotlin|com)\.)"
    r"[A-Za-z_$][\w$]*(?:[.+][A-Za-z_$][\w$]*)*(?:,\s*[A-Za-z_$][\w.$-]*)?$"
)
_ASSEMBLY_QUALIFIED_IDENTIFIER_PATTERN = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+,\s*[A-Za-z_$][\w.$-]*$"
)
_METHOD_SIGNATURE_PATTERN = re.compile(
    r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+(?:::|\.)"
    r"[A-Za-z_$][\w$]*\([^\r\n]*\)$"
)
_PRIVATE_CODE_IDENTIFIER_PATTERN = re.compile(r"^_[A-Za-z][A-Za-z0-9_]*$")
_EXPLICIT_UNICODE_ESCAPE_PATTERN = re.compile(
    r"\\(?:u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})"
)
_DOTNET_NUMERIC_PLACEHOLDER_PATTERN = re.compile(
    r"\{\d+(?:\s*,\s*-?\d+)?(?::[^{}]*)?\}"
)
_DIRECT_STATIC_DISPLAY_FIELDS = {"m_Text", "m_text", "mText"}


def _has_invalid_surrogate(text: str) -> bool:
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= len(text) or not 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                return True
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            return True
        index += 1
    return False


def _has_dotnet_numeric_placeholder(text: str) -> bool:
    index = 0
    while index < len(text):
        if text[index] != "{":
            index += 1
            continue
        if index + 1 < len(text) and text[index + 1] == "{":
            index += 2
            continue
        match = _DOTNET_NUMERIC_PLACEHOLDER_PATTERN.match(text, index)
        if match is not None:
            return True
        index += 1
    return False


def _machine_structure_reason(text: str) -> str | None:
    stripped = text.strip()
    if _URI_PATTERN.fullmatch(stripped):
        return "machine_uri"
    if _EMAIL_PATTERN.fullmatch(stripped):
        return "machine_email"
    if _WINDOWS_ABSOLUTE_PATH_PATTERN.fullmatch(stripped) or _POSIX_ABSOLUTE_PATH_PATTERN.fullmatch(stripped):
        return "machine_absolute_path"
    if _GUID_PATTERN.fullmatch(stripped):
        return "machine_guid"
    if _HEX_BLOB_PATTERN.fullmatch(stripped):
        return "machine_hex_blob"
    if _KNOWN_QUALIFIED_IDENTIFIER_PATTERN.fullmatch(stripped):
        return "machine_qualified_identifier"
    if _ASSEMBLY_QUALIFIED_IDENTIFIER_PATTERN.fullmatch(stripped):
        return "machine_assembly_identifier"
    if _METHOD_SIGNATURE_PATTERN.fullmatch(stripped):
        return "machine_method_signature"
    if _PRIVATE_CODE_IDENTIFIER_PATTERN.fullmatch(stripped):
        return "machine_private_identifier"
    if stripped.startswith("<?xml") or stripped.startswith("<!DOCTYPE"):
        return "machine_xml"
    if stripped[:1] in {"{", "["} and stripped[-1:] in {"}", "]"}:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeError):
            pass
        else:
            if isinstance(parsed, (dict, list)):
                return "machine_json"
    return None


def stringliteral_exclusion_reason(
    value: Any,
    *,
    max_chars: int = MAX_STRINGLITERAL_CANDIDATE_CHARS,
) -> str | None:
    """Return a conservative reason for excluding a runtime string literal."""
    if not isinstance(value, str):
        return "non_string_value"
    if not value or not value.strip():
        return "empty_or_whitespace"
    if "\0" in value:
        return "contains_nul"
    if _has_invalid_surrogate(value):
        return "invalid_surrogate"
    if _EXPLICIT_UNICODE_ESCAPE_PATTERN.search(value):
        return "explicit_unicode_escape"
    if any(unicodedata.category(char) == "Cc" and char not in "\t\r\n" for char in value):
        return "control_character"
    if len(value) > max_chars:
        return "too_long"
    if not any(unicodedata.category(char).startswith("L") for char in value):
        return "no_language_text"
    return _machine_structure_reason(value)


def _safe_report_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"value_type": type(value).__name__}
    if _has_invalid_surrogate(value):
        return {
            "value": None,
            "escaped_value": value.encode("unicode_escape", errors="backslashreplace").decode("ascii"),
        }
    return {"value": value}


def _literal_address(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value.strip(), 0)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _address_set(values: Collection[int | str]) -> set[int]:
    addresses: set[int] = set()
    for value in values:
        address = _literal_address(value)
        if address is None:
            raise ValueError(f"IL2CPP 显示分析返回了无效字符串地址: {value!r}")
        addresses.add(address)
    return addresses


def _extract_stringliteral_candidate_roles(
    payload: Any,
    *,
    exact_display_addresses: Collection[int | str],
    derived_display_addresses: Collection[int | str] = (),
    static_display_values: Collection[str] = (),
    max_chars: int = MAX_STRINGLITERAL_CANDIDATE_CHARS,
) -> tuple[
    list[str],
    list[str],
    list[str],
    list[dict[str, Any]],
    dict[str, int],
]:
    """Classify display literals while keeping a single ordered translation task list."""
    if not isinstance(payload, list):
        raise ValueError("stringliteral.json 顶层必须是 JSON 数组。")

    exact_addresses = _address_set(exact_display_addresses)
    derived_addresses = _address_set(derived_display_addresses)
    static_values = {
        value for value in static_display_values if isinstance(value, str)
    }
    roles_by_source: dict[str, set[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        parsed_address = _literal_address(item.get("address"))
        value = item.get("value")
        if parsed_address is None or not isinstance(value, str):
            continue
        roles = roles_by_source.setdefault(value, set())
        if parsed_address in exact_addresses:
            roles.add("exact")
        if parsed_address in derived_addresses:
            roles.add("derived")
        if value in static_values:
            # Optional heuristic: a runtime literal identical to a serialized
            # Text/TMP/UILabel value is likely to overwrite a visible label
            # even when the native call graph loses it through a virtual call.
            roles.add("exact")

    source_reasons: dict[str, str | None] = {}
    # The native hook matches ASCII case-insensitively.  Keep the first source
    # form (stringliteral.json order is stable) as the canonical translation
    # task, instead of rejecting every variant and losing valid labels such as
    # ``LEVEL `` merely because ``Level `` also exists.
    ascii_canonical_value: dict[str, str] = {}
    for value, roles in roles_by_source.items():
        if not roles:
            continue
        if _EXPLICIT_UNICODE_ESCAPE_PATTERN.search(value):
            reason = "explicit_unicode_escape"
        else:
            reason = stringliteral_exclusion_reason(value, max_chars=max_chars)
            if (
                reason == "no_language_text"
                and "derived" in roles
                and _has_dotnet_numeric_placeholder(value)
            ):
                reason = None
        source_reasons[value] = reason
        if reason is None:
            ascii_canonical_value.setdefault(_ascii_case_key(value), value)

    candidates: list[str] = []
    filtered_out: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    seen: set[str] = set()

    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            value: Any = item
            reason = "non_object_record"
            address = None
        else:
            value = item.get("value")
            address = item.get("address")
            parsed_address = _literal_address(address)
            if isinstance(value, str) and _EXPLICIT_UNICODE_ESCAPE_PATTERN.search(value):
                # This is a source-level machine escape, not an already decoded
                # Unicode character.  Give it precedence over display evidence
                # so the first-pass filter and its report do not depend on the
                # binary analysis result.
                reason = "explicit_unicode_escape"
            elif parsed_address is None:
                reason = "invalid_address"
            elif (
                parsed_address in exact_addresses
                or parsed_address in derived_addresses
                or value in static_values
            ):
                if not isinstance(value, str):
                    reason = stringliteral_exclusion_reason(value, max_chars=max_chars)
                else:
                    reason = source_reasons.get(value)
                if (
                    reason is None
                    and isinstance(value, str)
                    and ascii_canonical_value.get(_ascii_case_key(value)) != value
                ):
                    reason = "ascii_case_alias"
            else:
                reason = "not_proven_display"

        if reason is None:
            if value in seen:
                reason = "duplicate"

        if reason is None:
            seen.add(value)
            candidates.append(value)
            continue

        reason_counts[reason] += 1
        report_item: dict[str, Any] = {"index": index, "reason": reason}
        if isinstance(address, (str, int, float)) or address is None:
            report_item["address"] = address
        report_item.update(_safe_report_value(value))
        filtered_out.append(report_item)

    canonical_roles: dict[str, set[str]] = defaultdict(set)
    for source, roles in roles_by_source.items():
        canonical = ascii_canonical_value.get(_ascii_case_key(source))
        if canonical is not None:
            canonical_roles[canonical].update(roles)
    whole_sources = [
        source for source in candidates if "exact" in canonical_roles.get(source, ())
    ]
    substring_sources = [
        source for source in candidates if "derived" in canonical_roles.get(source, ())
    ]
    return (
        candidates,
        whole_sources,
        substring_sources,
        filtered_out,
        dict(reason_counts),
    )


def extract_stringliteral_candidates(
    payload: Any,
    *,
    exact_display_addresses: Collection[int | str],
    derived_display_addresses: Collection[int | str] = (),
    static_display_values: Collection[str] = (),
    max_chars: int = MAX_STRINGLITERAL_CANDIDATE_CHARS,
) -> tuple[list[str], list[dict[str, Any]], dict[str, int]]:
    """Return the ordered union of exact and derived display-literal candidates."""
    candidates, _whole, _substring, filtered, reasons = (
        _extract_stringliteral_candidate_roles(
            payload,
            exact_display_addresses=exact_display_addresses,
            derived_display_addresses=derived_display_addresses,
            static_display_values=static_display_values,
            max_chars=max_chars,
        )
    )
    return candidates, filtered, reasons


def _iter_unicode_scalars(text: str):
    index = 0
    while index < len(text):
        codepoint = ord(text[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            low = ord(text[index + 1])
            yield 0x10000 + ((codepoint - 0xD800) << 10) + (low - 0xDC00)
            index += 2
            continue
        yield codepoint
        index += 1


def cpp_utf16_literal(text: str) -> str:
    """Encode a Python string as one or more safe adjacent C++ ``u`` literals."""
    if _has_invalid_surrogate(text):
        raise ValueError("无法把包含非法 UTF-16 代理项的文本写入 C++ 字面量。")

    literals: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        if current:
            literals.append('u"' + "".join(current) + '"')
            current.clear()

    for codepoint in _iter_unicode_scalars(text):
        if codepoint == 0x22:
            current.append(r'\"')
        elif codepoint == 0x5C:
            current.append(r"\\")
        elif codepoint == 0x09:
            current.append(r"\t")
        elif codepoint == 0x0A:
            current.append(r"\n")
        elif codepoint == 0x0D:
            current.append(r"\r")
        elif codepoint > 0xFFFF:
            current.append(f"\\U{codepoint:08X}")
        else:
            char = chr(codepoint)
            category = unicodedata.category(char)
            if category.startswith("C") or codepoint in {0x2028, 0x2029}:
                # C++ \x consumes every following hexadecimal digit. Keeping
                # each numeric escape in its own adjacent literal prevents the
                # next source character from changing its value.
                flush_current()
                literals.append(f'u"\\x{codepoint:04X}"')
            else:
                current.append(char)

    flush_current()
    return " ".join(literals) if literals else 'u""'


def _decode_cpp_utf16_literal_expression(expression: str) -> str:
    """Decode adjacent C++ ``u`` string literals emitted by cpp_utf16_literal."""
    literals: list[str] = []
    position = 0
    while position < len(expression):
        while position < len(expression) and expression[position].isspace():
            position += 1
        if position >= len(expression):
            break
        match = _CPP_UTF16_LITERAL_TOKEN_PATTERN.match(expression, position)
        if match is None:
            raise ValueError(f"无法解析 C++ UTF-16 字面量: {expression!r}")
        encoded = match.group(1)
        decoded: list[str] = []
        index = 0
        while index < len(encoded):
            char = encoded[index]
            if char != "\\":
                decoded.append(char)
                index += 1
                continue
            index += 1
            if index >= len(encoded):
                raise ValueError(f"C++ UTF-16 字面量以反斜杠结尾: {expression!r}")
            escape = encoded[index]
            index += 1
            simple = {"\\": "\\", '"': '"', "t": "\t", "n": "\n", "r": "\r"}
            if escape in simple:
                decoded.append(simple[escape])
                continue
            if escape == "x":
                end = index
                while end < len(encoded) and encoded[end] in "0123456789abcdefABCDEF":
                    end += 1
                if end == index:
                    raise ValueError(f"C++ UTF-16 十六进制转义缺少数字: {expression!r}")
                decoded.append(chr(int(encoded[index:end], 16)))
                index = end
                continue
            if escape in {"u", "U"}:
                digits = 4 if escape == "u" else 8
                end = index + digits
                token = encoded[index:end]
                if len(token) != digits or any(char not in "0123456789abcdefABCDEF" for char in token):
                    raise ValueError(f"C++ UTF-16 Unicode 转义无效: {expression!r}")
                decoded.append(chr(int(token, 16)))
                index = end
                continue
            if escape in "01234567":
                end = index
                while end < len(encoded) and end < index + 2 and encoded[end] in "01234567":
                    end += 1
                decoded.append(chr(int(escape + encoded[index:end], 8)))
                index = end
                continue
            raise ValueError(f"不支持的 C++ UTF-16 转义: \\{escape}")
        literals.append("".join(decoded))
        position = match.end()
    if not literals:
        raise ValueError(f"C++ UTF-16 字面量为空: {expression!r}")
    return "".join(literals)


def extract_native_unity_translation_dictionary_entries(
    dictionary_cpp_path: Path,
) -> list[tuple[str, str]]:
    """Return every source/replacement pair from both Hook dictionary arrays."""
    entries_by_role = extract_native_unity_translation_dictionary_entries_by_role(
        dictionary_cpp_path
    )
    return [
        entry
        for role in ("whole", "substring")
        for entry in entries_by_role[role]
    ]


def extract_native_unity_translation_dictionary_entries_by_role(
    dictionary_cpp_path: Path,
) -> dict[str, list[tuple[str, str]]]:
    """Return parsed entries without losing whole/substring matching roles."""
    if not dictionary_cpp_path.is_file():
        raise FileNotFoundError(f"未找到动态词典 C++ 文件: {dictionary_cpp_path}")
    source = dictionary_cpp_path.read_text(encoding="utf-8-sig")
    entries: dict[str, list[tuple[str, str]]] = {
        "whole": [],
        "substring": [],
    }
    entry_pattern = re.compile(
        r"^\s*\{\s*(?P<source>(?:u\"(?:\\.|[^\"\\])*\")(?:\s+u\"(?:\\.|[^\"\\])*\")*)"
        r"\s*,\s*(?P<replacement>(?:u\"(?:\\.|[^\"\\])*\")(?:\s+u\"(?:\\.|[^\"\\])*\")*)"
        r"\s*\},?\s*$"
    )
    for role, array_name in (
        ("whole", "kWholeTextDictionary"),
        ("substring", "kSubstringDictionary"),
    ):
        matches = list(_cpp_dictionary_array_pattern(array_name).finditer(source))
        if len(matches) != 1:
            raise ValueError(
                f"动态词典 C++ 文件必须且只能包含一个 {array_name} 数组，"
                f"实际找到 {len(matches)} 个: {dictionary_cpp_path}"
            )
        for line in matches[0].group("body").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("//") or "nullptr" in stripped:
                continue
            match = entry_pattern.fullmatch(line)
            if match is None:
                raise ValueError(
                    f"动态词典 C++ 数组中存在无法解析的条目: {array_name}: {line!r}"
                )
            entries[role].append(
                (
                    _decode_cpp_utf16_literal_expression(match.group("source")),
                    _decode_cpp_utf16_literal_expression(match.group("replacement")),
                )
            )
    return entries


def _ascii_case_key(text: str) -> str:
    return text.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def select_whole_text_dictionary_entries(
    translations: Mapping[Any, Any],
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    entries: list[tuple[str, str]] = []
    skipped: Counter[str] = Counter()
    # Match the hook's ASCII case-insensitive lookup.  One canonical key must
    # be emitted; retaining case variants would make the selected replacement
    # depend on implementation order.  Preserve mapping order as the explicit
    # tie-breaker and report any suppressed aliases.
    canonical_source_by_key: dict[str, str] = {}
    canonical_replacement_by_key: dict[str, str] = {}
    for source, replacement in translations.items():
        if not isinstance(source, str) or not isinstance(replacement, str):
            continue
        key = _ascii_case_key(source)
        canonical_source_by_key.setdefault(key, source)
        canonical_replacement_by_key.setdefault(key, replacement)

    for source, replacement in translations.items():
        if not isinstance(source, str) or not isinstance(replacement, str):
            skipped["non_string"] += 1
            continue
        if not source or not replacement:
            skipped["empty"] += 1
            continue
        if source == replacement:
            skipped["unchanged"] += 1
            continue
        if "\0" in source or "\0" in replacement:
            skipped["contains_nul"] += 1
            continue
        if _has_invalid_surrogate(source) or _has_invalid_surrogate(replacement):
            skipped["invalid_surrogate"] += 1
            continue

        key = _ascii_case_key(source)
        canonical_source = canonical_source_by_key[key]
        if source != canonical_source:
            if replacement == canonical_replacement_by_key[key]:
                skipped["ascii_case_alias"] += 1
            else:
                skipped["ascii_case_conflict"] += 1
            continue

        entries.append((source, replacement))

    return entries, dict(skipped)


def render_whole_text_dictionary_array(translations: Mapping[Any, Any]) -> tuple[str, int, dict[str, int]]:
    """Render only the whole-text C++ dictionary array."""
    entries, skipped = select_whole_text_dictionary_entries(translations)
    rows = ["const NativeUnityTranslationEntry kWholeTextDictionary[] = {"]
    rows.extend(
        f"    {{{cpp_utf16_literal(source)}, {cpp_utf16_literal(replacement)}}},"
        for source, replacement in entries
    )
    rows.extend(["    {nullptr, nullptr},", "};"])
    return "\n".join(rows), len(entries), skipped


def write_trans_json_to_whole_text_dictionary(
    trans_path: Path,
    dictionary_cpp_path: Path,
) -> dict[str, Any]:
    """Replace kWholeTextDictionary from trans.json and preserve all other C++ code."""
    if not trans_path.is_file():
        raise FileNotFoundError(f"未找到静态翻译缓存: {trans_path}")
    if not dictionary_cpp_path.is_file():
        raise FileNotFoundError(f"未找到动态词典 C++ 文件: {dictionary_cpp_path}")

    translations = read_json(trans_path)
    if not isinstance(translations, Mapping):
        raise ValueError(f"trans.json 必须是 JSON 对象: {trans_path}")

    source = dictionary_cpp_path.read_text(encoding="utf-8-sig")
    matches = list(_WHOLE_TEXT_DICTIONARY_PATTERN.finditer(source))
    if len(matches) != 1:
        raise ValueError(
            "动态词典 C++ 文件必须且只能包含一个 "
            f"kWholeTextDictionary 数组，实际找到 {len(matches)} 个: {dictionary_cpp_path}"
        )

    rendered, entry_count, skipped = render_whole_text_dictionary_array(translations)
    newline = "\r\n" if "\r\n" in source else "\n"
    rendered = rendered.replace("\n", newline)
    match = matches[0]
    updated = source[:match.start()] + rendered + source[match.end():]
    with dictionary_cpp_path.open("w", encoding="utf-8", newline="") as output:
        output.write(updated)
    return {
        "source_count": len(translations),
        "entry_count": entry_count,
        "skipped": skipped,
        "trans_path": str(trans_path),
        "dictionary_cpp_path": str(dictionary_cpp_path),
    }


def _translations_for_sources(
    translations: Mapping[Any, Any],
    sources: Collection[str],
) -> OrderedDict[str, Any]:
    allowed = set(sources)
    return OrderedDict(
        (source, replacement)
        for source, replacement in translations.items()
        if isinstance(source, str) and source in allowed
    )


def _placeholder_runtime_substring_translations(
    translations: Mapping[Any, Any],
    sources: Collection[str],
) -> OrderedDict[str, str]:
    """Derive literal fragments that survive ``String.Format`` expansion.

    A final-render hook cannot match a key such as ``"'{0}' starting."`` after
    the placeholder has already become a mission name.  Keep the original
    format-string entry, but also emit aligned literal portions containing
    meaningful ASCII words.  Very short fragments are deliberately ignored so
    punctuation and generic glue words do not become global replacements.
    """

    derived: OrderedDict[str, str] = OrderedDict()
    for source in sources:
        replacement = translations.get(source)
        if not isinstance(source, str) or not isinstance(replacement, str):
            continue
        source_placeholders = _DOTNET_NUMERIC_PLACEHOLDER_PATTERN.findall(source)
        replacement_placeholders = _DOTNET_NUMERIC_PLACEHOLDER_PATTERN.findall(replacement)
        if not source_placeholders or source_placeholders != replacement_placeholders:
            continue

        source_parts = _DOTNET_NUMERIC_PLACEHOLDER_PATTERN.split(source)
        replacement_parts = _DOTNET_NUMERIC_PLACEHOLDER_PATTERN.split(replacement)
        if len(source_parts) != len(replacement_parts):
            continue

        for index, (source_part, replacement_part) in enumerate(
            zip(source_parts, replacement_parts)
        ):
            # Quotes immediately after a placeholder belong to the placeholder
            # value rather than to the reusable language fragment.  Removing
            # them turns ``'{name}' starting.`` into the safe suffix
            # `` starting.`` -> ``开始。``.
            if index > 0:
                source_part = source_part.lstrip("\"'“”‘’")
                replacement_part = replacement_part.lstrip("\"'“”‘’")
            if source_part == replacement_part or not source_part or not replacement_part:
                continue
            if len(re.findall(r"[A-Za-z]", source_part)) < 3:
                continue
            derived.setdefault(source_part, replacement_part)
    return derived


def _select_role_dictionary_entries(
    translations: Mapping[Any, Any],
    sources: Collection[str],
    *,
    longest_source_first: bool = False,
    derive_placeholder_fragments: bool = False,
) -> tuple[list[tuple[str, str]], dict[str, int]]:
    selected = _translations_for_sources(translations, sources)
    if derive_placeholder_fragments:
        canonical_keys = {_ascii_case_key(source) for source in selected}
        for source, replacement in _placeholder_runtime_substring_translations(
            translations, sources
        ).items():
            key = _ascii_case_key(source)
            if key in canonical_keys:
                continue
            selected[source] = replacement
            canonical_keys.add(key)
    entries, skipped = select_whole_text_dictionary_entries(selected)
    if longest_source_first:
        entries.sort(key=lambda entry: -len(entry[0].encode("utf-16-le")))
    return entries, skipped


def render_dynamic_translation_dictionary(
    translations: Mapping[Any, Any],
    *,
    whole_sources: Collection[str] | None = None,
    substring_sources: Collection[str] = (),
) -> str:
    if whole_sources is None:
        whole_sources = [source for source in translations if isinstance(source, str)]
    whole_entries, _whole_skipped = _select_role_dictionary_entries(
        translations,
        whole_sources,
    )
    substring_entries, _substring_skipped = _select_role_dictionary_entries(
        translations,
        substring_sources,
        longest_source_first=True,
        derive_placeholder_fragments=True,
    )
    rows = [
        "// Generated from stringliteral.json entries proven to reach a Unity display sink.",
        "// Exact literals use whole-text matching; derived literals use substring matching.",
        "// Replace the two dictionary arrays in native_unity_translation_dictionary.cpp with this block.",
        "const NativeUnityTranslationEntry kWholeTextDictionary[] = {",
    ]
    rows.extend(
        f"    {{{cpp_utf16_literal(source)}, {cpp_utf16_literal(replacement)}}},"
        for source, replacement in whole_entries
    )
    rows.extend(
        [
            "    {nullptr, nullptr},",
            "};",
            "",
            "const NativeUnityTranslationEntry kSubstringDictionary[] = {",
        ]
    )
    rows.extend(
        f"    {{{cpp_utf16_literal(source)}, {cpp_utf16_literal(replacement)}}},"
        for source, replacement in substring_entries
    )
    rows.extend(["    {nullptr, nullptr},", "};", ""])
    return "\n".join(rows)


def _load_existing_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.is_file():
        return {}
    try:
        payload = read_json(cache_path)
    except Exception as exc:
        print(f"[动态词库][断点续跑] 缓存读取失败，将重建任务表: {exc}", flush=True)
        return {}
    if not isinstance(payload, dict):
        print(f"[动态词库][断点续跑] 缓存不是 JSON 对象，将重建任务表: {cache_path}", flush=True)
        return {}
    return {
        source: replacement
        for source, replacement in payload.items()
        if isinstance(source, str) and isinstance(replacement, str)
    }


def _load_optional_static_display_values(records_path: Path) -> set[str]:
    """Load optional display-text hints from records.json when it exists."""
    if not records_path.is_file():
        print(
            f"[动态词库][启发式补漏] records.json 不存在，已跳过: {records_path}",
            flush=True,
        )
        return set()
    try:
        payload = read_json(records_path)
    except Exception as exc:
        print(
            f"[动态词库][启发式补漏] records.json 读取失败，已跳过: {exc}",
            flush=True,
        )
        return set()
    if not isinstance(payload, list):
        print(
            f"[动态词库][启发式补漏] records.json 顶层不是数组，已跳过: "
            f"{records_path}",
            flush=True,
        )
        return set()
    values = {
        source
        for row in payload
        if isinstance(row, Mapping)
        and row.get("field") in _DIRECT_STATIC_DISPLAY_FIELDS
        and isinstance((source := row.get("source_text")), str)
        and source
    }
    print(
        f"[动态词库][启发式补漏] records.json 显示文本参照="
        f"{len(values)}（仅用于和 stringliteral.json 求交集，不直接加入字典）。",
        flush=True,
    )
    return values


def _merge_builder_result(
    candidates: Sequence[str],
    before: Mapping[str, str],
    cache_path: Path,
    result: Mapping[str, str] | None,
) -> OrderedDict[str, str]:
    if result is not None and not isinstance(result, Mapping):
        raise TypeError("translation_builder 必须返回字符串映射或 None。")
    on_disk = _load_existing_cache(cache_path)
    returned = result if isinstance(result, Mapping) else {}
    merged: OrderedDict[str, str] = OrderedDict()
    for source in candidates:
        choices = (returned.get(source), on_disk.get(source), before.get(source), "")
        merged[source] = next(
            (choice for choice in choices if isinstance(choice, str) and choice),
            "",
        )
    return merged


def generate_dynamic_translation_dictionary(
    cfg: PipelineConfig,
    *,
    translation_builder: TranslationBuilder | None = None,
    usage_analyzer: Callable[..., Mapping[str, Any]] | None = None,
) -> Path:
    """Translate proven display literals and generate both Hook dictionaries."""
    source_path = cfg.stringliteral_json_path
    if not source_path.is_file():
        raise FileNotFoundError(f"未找到 stringliteral.json: {source_path}")
    try:
        payload = read_json(source_path)
    except Exception as exc:
        raise ValueError(f"stringliteral.json 读取失败: {source_path}: {exc}") from exc

    record_dir = cfg.stage_record_dir
    records_path = record_dir / str(
        getattr(cfg, "output_scan_records_json", "records.json")
    )
    static_display_values = _load_optional_static_display_values(records_path)
    heuristic_direct_addresses: set[int] = set()
    stringliteral_addresses: set[int] = set()
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        address = _literal_address(item.get("address"))
        value = item.get("value")
        if address is None or not isinstance(value, str):
            continue
        stringliteral_addresses.add(address)
        if (
            value in static_display_values
            and stringliteral_exclusion_reason(value) is None
        ):
            heuristic_direct_addresses.add(address)
    print(
        f"[动态词库][启发式补漏] 先求交集命中地址="
        f"{len(heuristic_direct_addresses)}，剩余待链路分析地址="
        f"{len(stringliteral_addresses - heuristic_direct_addresses)}。",
        flush=True,
    )
    script_json_path = Path(
        getattr(cfg, "il2cpp_script_json_path", source_path.with_name("script.json"))
    )
    dump_cs_path = Path(
        getattr(cfg, "il2cpp_dump_cs_path", source_path.with_name("dump.cs"))
    )
    default_libil2cpp_path = (
        source_path.parents[2] / "game" / "lib" / "arm64-v8a" / "libil2cpp.so"
    )
    libil2cpp_path = Path(
        getattr(cfg, "libil2cpp_arm64_path", default_libil2cpp_path)
    )
    use_builtin_analyzer = usage_analyzer is None
    if use_builtin_analyzer:
        from .il2cpp_display_usage import analyze_il2cpp_display_usage

        usage_analyzer = analyze_il2cpp_display_usage
    analysis_cache_path = (
        cfg.stage_record_dir / "stringliteral_display_analysis.cache.json"
    )
    if analysis_cache_path.exists():
        try:
            analysis_cache_path.unlink()
            print(
                f"[动态词库] 已清理本次专用分析缓存: {analysis_cache_path}",
                flush=True,
            )
        except OSError as exc:
            # The built-in analyzer is still called with use_cache=False, so
            # an undeletable stale file can never affect this run.
            print(
                f"[动态词库][提示] 分析缓存无法删除，将强制忽略: {exc}",
                flush=True,
            )
    analysis_kwargs: dict[str, Any] = {
        "libil2cpp_path": libil2cpp_path,
        "script_json_path": script_json_path,
        "stringliteral_json_path": source_path,
        "dump_cs_path": dump_cs_path,
        "exclude_literal_addresses": heuristic_direct_addresses,
    }
    if use_builtin_analyzer:
        analysis_kwargs["cache_path"] = analysis_cache_path
        analysis_kwargs["use_cache"] = False
        analysis_kwargs["progress_callback"] = lambda message: print(
            f"[动态词库][链路分析] {message}", flush=True
        )
    if stringliteral_addresses <= heuristic_direct_addresses:
        analysis = {
            "schema_version": 3,
            "inputs": {
                "libil2cpp": str(libil2cpp_path),
                "script_json": str(script_json_path),
                "stringliteral_json": str(source_path),
                "dump_cs": str(dump_cs_path),
            },
            "exact_literals": [],
            "derived_influence": [],
            "probable_display_literals": [],
            "unresolved": [],
            "display_sinks": [],
            "display_containers": [],
            "render_sinks": [],
            "stats": {
                "literal_count": 0,
                "preclassified_records_match_count": len(
                    heuristic_direct_addresses
                ),
                "analysis_skipped_all_literals_preclassified": True,
            },
        }
    else:
        analysis = usage_analyzer(
            **analysis_kwargs,
        )
    if not isinstance(analysis, Mapping):
        raise TypeError("usage_analyzer 必须返回 IL2CPP 显示分析映射。")
    exact_literals = analysis.get("exact_literals")
    derived_literals = analysis.get("derived_influence")
    probable_literals = analysis.get("probable_display_literals", [])
    display_enum_types = analysis.get("display_enum_types", [])
    unresolved = analysis.get("unresolved")
    if not isinstance(exact_literals, list) or not isinstance(derived_literals, list):
        raise ValueError("IL2CPP 显示分析缺少 exact_literals 或 derived_influence 数组。")
    if not isinstance(unresolved, list):
        raise ValueError("IL2CPP 显示分析缺少 unresolved 数组。")
    if not isinstance(probable_literals, list):
        raise ValueError("IL2CPP 显示分析 probable_display_literals 必须是数组。")
    if not isinstance(display_enum_types, list):
        raise ValueError("IL2CPP 显示分析 display_enum_types 必须是数组。")

    def analysis_addresses(rows: Sequence[Any], label: str) -> list[int | str]:
        addresses: list[int | str] = []
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping) or "address" not in row:
                raise ValueError(f"IL2CPP 显示分析 {label}[{index}] 缺少 address。")
            address = row["address"]
            if not isinstance(address, (int, str)):
                raise ValueError(f"IL2CPP 显示分析 {label}[{index}].address 无效。")
            addresses.append(address)
        return addresses

    (
        candidates,
        whole_sources,
        substring_sources,
        filtered_out,
        reason_counts,
    ) = _extract_stringliteral_candidate_roles(
        payload,
        exact_display_addresses=analysis_addresses(exact_literals, "exact_literals"),
        derived_display_addresses=analysis_addresses(derived_literals, "derived_influence"),
        static_display_values=static_display_values,
    )

    enum_roles_by_source: OrderedDict[str, set[str]] = OrderedDict()
    enum_contexts_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(display_enum_types):
        if not isinstance(row, Mapping):
            raise ValueError(f"IL2CPP 显示分析 display_enum_types[{index}] 无效。")
        enum_type = row.get("enum_type")
        members = row.get("members")
        roles = row.get("roles")
        evidence = row.get("evidence", [])
        if not isinstance(enum_type, str) or not isinstance(members, list):
            raise ValueError(
                f"IL2CPP 显示分析 display_enum_types[{index}] 缺少类型或成员。"
            )
        valid_roles = {
            role for role in roles if role in {"exact", "derived"}
        } if isinstance(roles, list) else set()
        if not valid_roles:
            continue
        for member in members:
            if not isinstance(member, str) or not member:
                continue
            enum_roles_by_source.setdefault(member, set()).update(valid_roles)
            enum_contexts_by_source[member].append(
                {
                    "enum_type": enum_type,
                    "roles": sorted(valid_roles),
                    "display_uses": evidence[:4] if isinstance(evidence, list) else [],
                }
            )
    enum_candidates = list(enum_roles_by_source)
    enum_whole_sources = [
        source
        for source, roles in enum_roles_by_source.items()
        if "exact" in roles
    ]
    enum_substring_sources = [
        source
        for source, roles in enum_roles_by_source.items()
        if "derived" in roles
    ]
    record_dir.mkdir(parents=True, exist_ok=True)
    output_root = Path(getattr(cfg, "stage_dir", record_dir.parent / "output"))
    dictionary_output_dir = output_root / DYNAMIC_DICTIONARY_OUTPUT_SUBDIR
    dictionary_output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = record_dir / STRINGLITERAL_TRANSLATIONS_FILENAME
    enum_cache_path = record_dir / RUNTIME_ENUM_TRANSLATIONS_FILENAME
    output_path = dictionary_output_dir / DYNAMIC_DICTIONARY_CPP_FILENAME
    report_path = record_dir / STRINGLITERAL_FILTER_REPORT_FILENAME
    filtered_path = record_dir / STRINGLITERAL_FILTERED_OUT_FILENAME
    analysis_path = record_dir / STRINGLITERAL_DISPLAY_ANALYSIS_FILENAME
    maybe_title_path = record_dir / STRINGLITERAL_MAYBE_TITLE_FILENAME

    atomic_write_json(analysis_path, dict(analysis))
    atomic_write_json(filtered_path, filtered_out)
    existing = _load_existing_cache(cache_path)
    existing_enum = _load_existing_cache(enum_cache_path)
    static_translations = _load_existing_cache(record_dir / "trans.json")
    # The cache is only a resume source for candidates selected in this run.
    # Stale keys must not survive and become an implicit third candidate source.
    translations: OrderedDict[str, str] = OrderedDict(
        (source, existing.get(source, "")) for source in candidates
    )
    for source in candidates:
        if (
            source in static_display_values
            and not translations[source]
            and static_translations.get(source)
        ):
            translations[source] = static_translations[source]
    enum_translations: OrderedDict[str, str] = OrderedDict(
        (
            source,
            existing_enum.get(source, "") or translations.get(source, ""),
        )
        for source in enum_candidates
    )
    atomic_write_json(cache_path, dict(translations))
    atomic_write_json(enum_cache_path, dict(enum_translations))

    all_candidates = list(dict.fromkeys([*candidates, *enum_candidates]))
    combined_translations: OrderedDict[str, str] = OrderedDict(
        (
            source,
            translations.get(source, "") or enum_translations.get(source, ""),
        )
        for source in all_candidates
    )
    pending_count = sum(
        not combined_translations.get(source) for source in all_candidates
    )
    if pending_count:
        if translation_builder is None:
            raise RuntimeError(
                "动态词库存在待翻译条目，必须由调用方注入 translation_builder。"
            )
        builder = translation_builder
        source_contexts: dict[str, dict[str, Any]] = {}
        for role, rows in (("whole", exact_literals), ("substring", derived_literals)):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                source = row.get("value")
                if not isinstance(source, str) or source not in candidates:
                    continue
                context = source_contexts.setdefault(
                    source,
                    {"dictionary_role": [], "display_uses": []},
                )
                if role not in context["dictionary_role"]:
                    context["dictionary_role"].append(role)
                evidence_rows = row.get("evidence", [])
                if not isinstance(evidence_rows, list):
                    continue
                for evidence in evidence_rows[:4]:
                    if not isinstance(evidence, Mapping):
                        continue
                    use = {
                        "method": evidence.get("method_name"),
                        "sink": evidence.get("sink_name"),
                        "path": evidence.get("path", []),
                        "transforms": evidence.get("transforms", []),
                    }
                    if use not in context["display_uses"]:
                        context["display_uses"].append(use)
        for source, enum_contexts in enum_contexts_by_source.items():
            context = source_contexts.setdefault(
                source,
                {"dictionary_role": [], "display_uses": []},
            )
            for enum_context in enum_contexts:
                for role in enum_context["roles"]:
                    if role not in context["dictionary_role"]:
                        context["dictionary_role"].append(role)
                use = {
                    "enum_type": enum_context["enum_type"],
                    "path": "Enum.ToString -> Unity display sink",
                }
                if use not in context["display_uses"]:
                    context["display_uses"].append(use)
        result = builder(
            all_candidates,
            cfg,
            cache_path=cache_path,
            artifact_prefix=DYNAMIC_TRANSLATION_ARTIFACT_PREFIX,
            maybe_title_path=maybe_title_path,
            exclude_identifier_like=False,
            source_label=(
                "stringliteral.json 与枚举成员"
                "（均经 libil2cpp.so 显示参数验证）"
            ),
            source_contexts=source_contexts,
        )
        translated_candidates = _merge_builder_result(
            all_candidates, combined_translations, cache_path, result
        )
        combined_translations.update(translated_candidates)
        translations = OrderedDict(
            (source, combined_translations.get(source, "")) for source in candidates
        )
        enum_translations = OrderedDict(
            (source, combined_translations.get(source, ""))
            for source in enum_candidates
        )
        atomic_write_json(cache_path, dict(translations))
        atomic_write_json(enum_cache_path, dict(enum_translations))

    render_translations: OrderedDict[str, str] = OrderedDict(translations)
    render_translations.update(enum_translations)
    render_whole_sources = list(
        dict.fromkeys([*whole_sources, *enum_whole_sources])
    )
    render_substring_sources = list(
        dict.fromkeys([*substring_sources, *enum_substring_sources])
    )
    whole_entries, whole_dictionary_skipped = _select_role_dictionary_entries(
        render_translations,
        render_whole_sources,
    )
    substring_entries, substring_dictionary_skipped = _select_role_dictionary_entries(
        translations,
        render_substring_sources,
        longest_source_first=True,
        derive_placeholder_fragments=True,
    )
    _all_entries, dictionary_skipped = select_whole_text_dictionary_entries(
        render_translations
    )
    output_path.write_text(
        render_dynamic_translation_dictionary(
            render_translations,
            whole_sources=render_whole_sources,
            substring_sources=render_substring_sources,
        ),
        encoding="utf-8",
    )

    completed_count = sum(
        bool(combined_translations.get(source)) for source in all_candidates
    )
    report = {
        "source_path": str(source_path),
        "libil2cpp_path": str(libil2cpp_path),
        "script_json_path": str(script_json_path),
        "source_record_count": len(payload),
        "exact_display_literal_count": len(exact_literals),
        "derived_display_influence_count": len(derived_literals),
        "probable_display_literal_count": len(probable_literals),
        "heuristic_static_display_value_count": len(static_display_values),
        "heuristic_static_display_candidate_count": sum(
            source in static_display_values for source in candidates
        ),
        "unresolved_display_path_count": len(unresolved),
        "candidate_count": len(candidates),
        "enum_display_type_count": len(display_enum_types),
        "enum_candidate_count": len(enum_candidates),
        "enum_exact_candidate_count": len(enum_whole_sources),
        "enum_substring_candidate_count": len(enum_substring_sources),
        "enum_translation_cache_path": str(enum_cache_path),
        "exact_candidate_count": len(whole_sources),
        "substring_candidate_count": len(substring_sources),
        "dual_role_candidate_count": len(set(whole_sources) & set(substring_sources)),
        "filtered_count": len(filtered_out),
        "filtered_by_reason": reason_counts,
        "translation_cache_path": str(cache_path),
        "completed_translation_count": completed_count,
        "pending_translation_count": len(all_candidates) - completed_count,
        "whole_dictionary_entry_count": len(whole_entries),
        "substring_dictionary_entry_count": len(substring_entries),
        "dictionary_skipped_by_reason": dictionary_skipped,
        "dictionary_output_path": str(output_path),
        "display_analysis_path": str(analysis_path),
        "display_analysis_stats": analysis.get("stats", {}),
    }
    atomic_write_json(report_path, report)

    heuristic_candidate_count = report["heuristic_static_display_candidate_count"]
    print(
        f"[动态词库][启发式补漏] records.json ∩ stringliteral.json "
        f"有效候选={heuristic_candidate_count}。",
        flush=True,
    )
    print(
        f"[动态词库] 原值直达显示={len(exact_literals)}，"
        f"格式化/拼接后显示={len(derived_literals)}，"
        f"可能显示(未自动翻译)={len(probable_literals)}，"
        f"字符串候选={len(candidates)}，枚举候选={len(enum_candidates)}，"
        f"过滤={len(filtered_out)}，"
        f"已有译文={completed_count}，待翻译={len(all_candidates) - completed_count}",
        flush=True,
    )
    print(
        f"[动态词库] C++ 有效词条: 整句匹配={len(whole_entries)}，"
        f"任意位置替换={len(substring_entries)}，双角色候选="
        f"{len(set(whole_sources) & set(substring_sources))}",
        flush=True,
    )
    nul_skipped_count = whole_dictionary_skipped.get(
        "contains_nul", 0
    ) + substring_dictionary_skipped.get("contains_nul", 0)
    if nul_skipped_count:
        print(
            f"[动态词库] 已跳过 {nul_skipped_count} 个含 NUL 的字典词条，"
            "const char16_t* 无法表达嵌入 NUL。",
            flush=True,
        )
    print(f"[动态词库] C++ 字典已生成: {output_path}", flush=True)
    return output_path
