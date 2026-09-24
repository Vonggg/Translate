"""High precision IL2CPP string-literal to display-sink analysis.

The analyser is deliberately conservative.  A literal is considered exact only
when its value can be followed into the string argument of a statically resolved
display setter.  Merely sharing a method (or a call path) with a display setter
is not sufficient.

The public entry point returns plain JSON-serialisable dictionaries so its
result can be persisted or consumed by the dynamic dictionary generator.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
import hashlib
import json
import os
import tempfile
import time
from importlib.metadata import version as package_version
from pathlib import Path
import re
import struct
from typing import Any, Callable, Iterable, Mapping, Sequence


AARCH64_RELATIVE_RELOCATION = 1027
SCHEMA_VERSION = 11
# Large UI refresh methods frequently merge dozens of independent labels before
# reaching a shared setter.  A budget of 32 dropped most of those values at CFG
# joins (and made the later sink list look much smaller than the real UI).  Keep
# the analysis bounded, but high enough for menu/shop/inventory screens.
MAX_ABSTRACT_VALUES = 128
MAX_ABSTRACT_TRANSFORM_STEPS = 64
MAX_UNRESOLVED_SOURCE_SAMPLES = 16
IL2CPP64_VTABLE_OFFSET = 0x138
MAX_CACHED_DISASSEMBLED_INSTRUCTIONS = 250_000


class _BoundedInstructionCache:
    """Reuse recent disassembly without retaining an entire game's instructions."""

    def __init__(self, max_instructions: int = MAX_CACHED_DISASSEMBLED_INSTRUCTIONS):
        self.max_instructions = max(1, int(max_instructions))
        self.instruction_count = 0
        self.eviction_count = 0
        self._entries: OrderedDict[int, tuple[Any, ...]] = OrderedDict()

    def get(self, key: int) -> tuple[Any, ...] | None:
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
        return value

    def __setitem__(self, key: int, value: tuple[Any, ...]) -> None:
        previous = self._entries.pop(key, None)
        if previous is not None:
            self.instruction_count -= len(previous)
        if len(value) > self.max_instructions:
            return
        self._entries[key] = value
        self.instruction_count += len(value)
        while self.instruction_count > self.max_instructions and self._entries:
            _, removed = self._entries.popitem(last=False)
            self.instruction_count -= len(removed)
            self.eviction_count += 1


class Il2CppDisplayAnalysisError(RuntimeError):
    """Raised when the supplied IL2CPP analysis inputs cannot be consumed."""


@dataclass(frozen=True)
class MethodRecord:
    address: int
    end: int
    name: str
    signature: str
    aliases: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class AbstractValue:
    """One independently traceable contribution to an ARM64 value."""

    kind: str  # address, cell, exact, derived, param, stack_address
    source: int
    origin: int
    transforms: tuple[str, ...] = ()


@dataclass(frozen=True)
class SinkSpec:
    address: int
    name: str
    signature: str
    argument_index: int
    argument_register: str
    path: tuple[str, ...]
    wrapper_depth: int = 0
    argument_transform: str | None = None
    container_contents: bool = False


@dataclass(frozen=True)
class TransformSpec:
    name: str
    argument_registers: tuple[str, ...]


@dataclass
class AbstractState:
    registers: dict[str, frozenset[AbstractValue]] = field(default_factory=dict)
    stack: dict[int, frozenset[AbstractValue]] = field(default_factory=dict)
    container_elements: dict[
        tuple[str, int, tuple[str, ...]], frozenset[AbstractValue]
    ] = field(default_factory=dict)
    sp_offset: int = 0

    def copy(self) -> "AbstractState":
        return AbstractState(
            dict(self.registers),
            dict(self.stack),
            dict(self.container_elements),
            self.sp_offset,
        )


@dataclass
class MethodAnalysis:
    exact_hits: list[dict[str, Any]] = field(default_factory=list)
    derived_hits: list[dict[str, Any]] = field(default_factory=list)
    probable_hits: list[dict[str, Any]] = field(default_factory=list)
    enum_display_hits: list[dict[str, Any]] = field(default_factory=list)
    parameter_hits: list[tuple[int, SinkSpec, int, tuple[str, ...]]] = field(
        default_factory=list
    )
    carrier_field_hits: list[
        tuple[int, tuple[int, ...], SinkSpec, int]
    ] = field(default_factory=list)
    returned_carrier_field_hits: list[
        tuple[int, tuple[int, ...], SinkSpec, int]
    ] = field(default_factory=list)
    parameter_field_writes: list[tuple[int, tuple[int, ...], int]] = field(
        default_factory=list
    )
    literal_field_writes: list[tuple[AbstractValue, tuple[int, ...], int]] = field(
        default_factory=list
    )
    static_field_hits: list[
        tuple[tuple[int, tuple[str, ...]], SinkSpec, int]
    ] = field(default_factory=list)
    static_parameter_field_writes: list[
        tuple[int, tuple[int, tuple[str, ...]], int, tuple[str, ...]]
    ] = field(default_factory=list)
    static_literal_field_writes: list[
        tuple[AbstractValue, tuple[int, tuple[str, ...]], int]
    ] = field(default_factory=list)
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    referenced_literals: set[int] = field(default_factory=set)
    delegate_subscriptions: dict[tuple[int, tuple[str, ...]], set[int]] = field(
        default_factory=lambda: defaultdict(set)
    )
    delegate_parameter_field_writes: list[
        tuple[int, tuple[int, ...], int]
    ] = field(default_factory=list)
    delegate_invocations: list[
        tuple[
            tuple[int, tuple[str, ...]],
            int,
            dict[str, tuple[AbstractValue, ...]],
        ]
    ] = field(
        default_factory=list
    )
    trace_states: dict[int, dict[str, frozenset[AbstractValue]]] = field(
        default_factory=dict
    )
    return_values: set[AbstractValue] = field(default_factory=set)


_DISPLAY_FIELD_TYPE_ALIASES = {
    "Text": "UnityEngine.UI.Text",
    "UnityEngine.UI.Text": "UnityEngine.UI.Text",
    "TMP_Text": "TMPro.TMP_Text",
    "TMPro.TMP_Text": "TMPro.TMP_Text",
    "TextMeshPro": "TMPro.TMP_Text",
    "TMPro.TextMeshPro": "TMPro.TMP_Text",
    "TextMeshProUGUI": "TMPro.TMP_Text",
    "TMPro.TextMeshProUGUI": "TMPro.TMP_Text",
    "UILabel": "UILabel",
    "TextMesh": "UnityEngine.TextMesh",
    "UnityEngine.TextMesh": "UnityEngine.TextMesh",
    "InputField": "UnityEngine.UI.InputField",
    "UnityEngine.UI.InputField": "UnityEngine.UI.InputField",
    "TMP_InputField": "TMPro.TMP_InputField",
    "TMPro.TMP_InputField": "TMPro.TMP_InputField",
    "TextElement": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.TextElement": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.Label": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.Button": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.Foldout": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.GroupBox": "UnityEngine.UIElements.TextElement",
    "UnityEngine.UIElements.HelpBox": "UnityEngine.UIElements.TextElement",
    "FairyGUI.GTextField": "FairyGUI.GTextField",
    "FairyGUI.GRichTextField": "FairyGUI.GTextField",
}


def _normalise_display_field_type(type_name: str) -> str | None:
    """Map dump.cs component field types to a known text component family."""

    compact = type_name.replace("global::", "").strip()
    return _DISPLAY_FIELD_TYPE_ALIASES.get(compact)


def _return_display_component_type(signature: str) -> str | None:
    compact = _normalise_signature(signature).replace(" ", "")
    aliases = {
        "UnityEngine_UI_Text_o*": "UnityEngine.UI.Text",
        "TMPro_TMP_Text_o*": "TMPro.TMP_Text",
        "TMPro_TextMeshPro_o*": "TMPro.TMP_Text",
        "TMPro_TextMeshProUGUI_o*": "TMPro.TMP_Text",
        "UILabel_o*": "UILabel",
        "UnityEngine_TextMesh_o*": "UnityEngine.TextMesh",
        "UnityEngine_UI_InputField_o*": "UnityEngine.UI.InputField",
        "TMPro_TMP_InputField_o*": "TMPro.TMP_InputField",
        "UnityEngine_UIElements_TextElement_o*": "UnityEngine.UIElements.TextElement",
        "UnityEngine_UIElements_Label_o*": "UnityEngine.UIElements.TextElement",
        "UnityEngine_UIElements_Button_o*": "UnityEngine.UIElements.TextElement",
        "FairyGUI_GTextField_o*": "FairyGUI.GTextField",
        "FairyGUI_GRichTextField_o*": "FairyGUI.GTextField",
    }
    return next(
        (component for prefix, component in aliases.items() if compact.startswith(prefix)),
        None,
    )


def _is_erased_get_component_factory(method: MethodRecord | None) -> bool:
    if method is None:
        return False
    owner, separator, member = method.name.partition("$$")
    if not separator or owner not in {"UnityEngine.GameObject", "UnityEngine.Component"}:
        return False
    return member.startswith((
        "GetComponent<", "GetComponentInChildren<", "GetComponentInParent<"
    )) and (
        "object" in member.casefold()
        or _normalise_signature(method.signature).replace(" ", "").startswith(
            "Il2CppObject*"
        )
    )


def _hex(value: int) -> str:
    return f"0x{value:X}"


def _parse_address(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)
    raise Il2CppDisplayAnalysisError(f"Unsupported address value: {value!r}")


@lru_cache(maxsize=None)
def _normalise_signature(signature: str) -> str:
    return " ".join(str(signature).split())


@lru_cache(maxsize=None)
def _signature_parameters(signature: str) -> list[str]:
    normalised = _normalise_signature(signature)
    left = normalised.find("(")
    right = normalised.rfind(")")
    if left < 0 or right <= left:
        return []
    content = normalised[left + 1 : right].strip()
    if not content or content == "void":
        return []

    parameters: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(content):
        if char in "(<[":
            depth += 1
        elif char in ")>]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parameters.append(content[start:index].strip())
            start = index + 1
    parameters.append(content[start:].strip())
    return parameters


def _is_string_parameter(parameter: str) -> bool:
    compact = parameter.replace(" ", "")
    return "System_String_o*" in compact


@lru_cache(maxsize=None)
def _string_parameter_indices(signature: str) -> tuple[int, ...]:
    return tuple(
        index
        for index, parameter in enumerate(_signature_parameters(signature))
        if _is_string_parameter(parameter)
    )


def _is_object_pointer_parameter(parameter: str) -> bool:
    compact = parameter.replace(" ", "")
    return (
        "*" in compact
        and "MethodInfo" not in compact
        and not _is_string_parameter(parameter)
        and "intptr_t" not in compact
        and "Il2CppMethodPointer" not in compact
    )


def _family_parts(value: str) -> tuple[str, ...]:
    return tuple(
        part.casefold()
        for part in re.split(r"[._+:/]+", value)
        if part
    )


def _parameter_object_family(parameter: str) -> tuple[str, ...] | None:
    """Return a conservative top-level carrier family from an IL2CPP type."""

    compact = re.sub(r"\b(?:const|volatile)\b", "", parameter).strip()
    type_part = compact.split("*", 1)[0].strip()
    type_part = re.sub(r"_o$", "", type_part)
    if not type_part or type_part in {"Il2CppObject", "void"}:
        return None
    family = _family_parts(type_part)
    return family or None


def _method_object_family(method: MethodRecord) -> tuple[str, ...] | None:
    owner = method.name.split("$$", 1)[0]
    owner = owner.split("<", 1)[0]
    if not owner:
        return None
    family = _family_parts(owner)
    return family or None


def _family_is_same_or_nested(
    candidate: tuple[str, ...], family: tuple[str, ...]
) -> bool:
    return candidate[: len(family)] == family


def _event_family_root(family: tuple[str, ...] | None) -> int | None:
    """Return a deterministic negative key for an instance event owner."""

    if not family:
        return None
    digest = hashlib.sha1(".".join(family).encode("utf-8")).digest()
    return -int.from_bytes(digest[:8], "little", signed=False) - 1


def _field_paths_compatible(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    """Match exact fields and typed Builder paths that add an owner prefix."""
    if not left or not right:
        return False
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return longer[-len(shorter) :] == shorter


def _is_scalar_float_parameter(parameter: str) -> bool:
    """Return whether an IL2CPP parameter is passed in an AAPCS64 SIMD register."""

    compact = re.sub(r"\b(?:const|volatile)\b", "", parameter).strip()
    if "*" in compact or "[" in compact:
        return False
    parameter_type = compact.rsplit(" ", 1)[0] if " " in compact else compact
    return parameter_type in {"float", "double", "float32_t", "float64_t"}


@lru_cache(maxsize=None)
def _parameter_registers(signature: str) -> tuple[str | None, ...]:
    """Allocate scalar parameters according to the AAPCS64 GPR/SIMD banks.

    Integer and pointer arguments consume ``x0`` through ``x7``.  Scalar
    ``float``/``double`` arguments independently consume ``v0`` through
    ``v7`` and therefore do not shift a later string into the next GPR.
    Parameters beyond either supported register bank are deliberately left
    unresolved rather than guessed as stack arguments.
    """

    gpr_index = 0
    simd_index = 0
    result: list[str | None] = []
    for parameter in _signature_parameters(signature):
        if _is_scalar_float_parameter(parameter):
            result.append(f"v{simd_index}" if simd_index < 8 else None)
            simd_index += 1
        else:
            result.append(f"x{gpr_index}" if gpr_index < 8 else None)
            gpr_index += 1
    return tuple(result)


def _has_method_info_tail(parameters: Sequence[str]) -> bool:
    if not parameters:
        return False
    compact = parameters[-1].replace(" ", "")
    # Il2CppDumper often suffixes a concrete generic MethodInfo type/address,
    # for example ``const MethodInfo_26A4B00*``.  It is still the hidden ABI
    # argument and must not make real Action<T>/Func<T> constructors invisible.
    return re.search(r"MethodInfo(?:_[0-9A-Fa-f]+)?\*", compact) is not None


@lru_cache(maxsize=None)
def _match_display_sink(name: str, signature: str) -> bool:
    """Match a display sink by its complete name and parsed full signature."""

    parameters = _signature_parameters(signature)
    if not _has_method_info_tail(parameters):
        return False

    if name == "TMPro.TMP_Text$$set_text":
        return (
            len(parameters) == 3
            and "TMPro_TMP_Text_o*" in parameters[0].replace(" ", "")
            and _is_string_parameter(parameters[1])
        )
    if name == "TMPro.TMP_Text$$SetText":
        return (
            len(parameters) >= 3
            and "TMPro_TMP_Text_o*" in parameters[0].replace(" ", "")
            and _is_string_parameter(parameters[1])
        )
    if name == "UnityEngine.UI.Text$$set_text":
        return (
            len(parameters) == 3
            and "UnityEngine_UI_Text_o*" in parameters[0].replace(" ", "")
            and _is_string_parameter(parameters[1])
        )
    if name == "UILabel$$set_text":
        return (
            len(parameters) == 3
            and "UILabel_o*" in parameters[0].replace(" ", "")
            and _is_string_parameter(parameters[1])
        )
    instance_string_sinks = {
        "UnityEngine.TextMesh$$set_text",
        "UnityEngine.UI.InputField$$set_text",
        "UnityEngine.UI.InputField$$SetText",
        "UnityEngine.UI.InputField$$SetTextWithoutNotify",
        "TMPro.TMP_InputField$$set_text",
        "TMPro.TMP_InputField$$SetText",
        "TMPro.TMP_InputField$$SetTextWithoutNotify",
        "UnityEngine.UIElements.TextElement$$set_text",
        "UnityEngine.UIElements.TextElement$$set_renderedText",
        "UnityEngine.UIElements.TextElement$$UnityEngine.UIElements.INotifyValueChanged<System.String>.set_value",
        "UnityEngine.UIElements.TextElement$$UnityEngine.UIElements.INotifyValueChanged<System.String>.SetValueWithoutNotify",
        "UnityEngine.UIElements.TextField$$set_value",
        "UnityEngine.UIElements.TextField$$SetValueWithoutNotify",
        "UnityEngine.UIElements.AbstractProgressBar$$set_title",
        "UnityEngine.UIElements.BaseBoolField$$set_text",
        "UnityEngine.UIElements.Column$$set_title",
        "UnityEngine.UIElements.Foldout$$set_text",
        "UnityEngine.UIElements.GroupBox$$set_text",
        "UnityEngine.UIElements.HelpBox$$set_text",
        "UnityEngine.UIElements.TooltipEvent$$set_tooltip",
        "UnityEngine.UIElements.VisualElement$$set_tooltip",
        "UIInput$$set_value",
        "UIInput$$SetValue",
        "FairyGUI.GTextField$$set_text",
        "FairyGUI.GTextField$$set_htmlText",
        "FairyGUI.GRichTextField$$set_text",
        "FairyGUI.GRichTextField$$set_htmlText",
    }
    if name in instance_string_sinks:
        return len(parameters) >= 3 and _is_string_parameter(parameters[1])

    # UI Toolkit exposes visible labels through many concrete controls instead
    # of routing every call through TextElement.set_text.  Treat only explicit
    # label/text/title/message constructors and setters as sinks; style names,
    # USS classes and persistence keys remain excluded.
    owner, separator, member = name.partition("$$")
    if separator and owner.startswith("UnityEngine.UIElements."):
        visible_setters = {
            "set_text",
            "set_label",
            "set_title",
            "set_message",
            "set_tooltip",
            "set_value",
            "SetValueWithoutNotify",
        }
        if member in visible_setters:
            return len(parameters) >= 3 and _is_string_parameter(parameters[1])
        visible_constructor_owners = (
            "UnityEngine.UIElements.Label",
            "UnityEngine.UIElements.Button",
            "UnityEngine.UIElements.Toggle",
            "UnityEngine.UIElements.TextField",
            "UnityEngine.UIElements.HelpBox",
            "UnityEngine.UIElements.GroupBox",
            "UnityEngine.UIElements.RadioButton",
            "UnityEngine.UIElements.RadioButtonGroup",
            "UnityEngine.UIElements.DropdownField",
            "UnityEngine.UIElements.BaseField<",
            "UnityEngine.UIElements.BaseSlider<",
        )
        if member == ".ctor" and owner.startswith(visible_constructor_owners):
            return any(_is_string_parameter(item) for item in parameters[1:-1])

    immediate_gui_sinks = {
        "UnityEngine.GUI$$Label",
        "UnityEngine.GUI$$Button",
        "UnityEngine.GUI$$Box",
        "UnityEngine.GUI$$Toggle",
        "UnityEngine.GUI$$TextField",
        "UnityEngine.GUI$$TextArea",
        "UnityEngine.GUILayout$$Label",
        "UnityEngine.GUILayout$$Button",
        "UnityEngine.GUILayout$$Box",
        "UnityEngine.GUILayout$$Toggle",
        "UnityEngine.GUILayout$$TextField",
        "UnityEngine.GUILayout$$TextArea",
    }
    if name in immediate_gui_sinks:
        return any(_is_string_parameter(parameter) for parameter in parameters[:-1])
    return False


def _display_sink_argument_transform(name: str, signature: str) -> str | None:
    """Return the transform applied to the source string by a display setter."""

    if name != "TMPro.TMP_Text$$SetText":
        return None
    parameters = _signature_parameters(signature)
    content_parameters = parameters[2:-1] if _has_method_info_tail(parameters) else parameters[2:]
    if any("float" in parameter.lower() for parameter in content_parameters):
        return "SetTextFormat"
    return None


def _is_safe_wrapper_candidate(method: MethodRecord) -> bool:
    """Return whether a project-owned method may forward text to a display.

    Framework measurement APIs such as TextMeshPro.GetTextInfo temporarily call
    SetText internally but do not display their input.  Treating those methods
    as wrappers turns any caller literal into a false display hit.
    """
    framework_prefixes = (
        "TMPro.",
        "UnityEngine.",
        "Unity.",
        "System.",
        "Microsoft.",
        "Mono.",
    )
    return not method.name.startswith(framework_prefixes)


def _is_project_owned_method(method: MethodRecord) -> bool:
    """Exclude framework delegate plumbing from subscription discovery."""

    return not method.name.startswith(
        (
            "System.",
            "UnityEngine.",
            "Unity.",
            "TMPro.",
            "Microsoft.",
            "Mono.",
        )
    )


def _is_delegate_constructor(method: MethodRecord) -> bool:
    """Recognise generic and game-defined delegate constructors by ABI.

    IL2CPP emits the same ``(this, target object, intptr_t method, MethodInfo)``
    constructor shape for ``Action<T>``, ``Func<T>`` and user-defined delegate
    types.  Class names are not reliable after code generation, whereas this
    signature is specific enough to avoid treating normal constructors as
    delegate subscriptions.
    """

    if not method.name.endswith("$$.ctor"):
        return False
    parameters = _signature_parameters(method.signature)
    if len(parameters) != 4 or not _normalise_signature(method.signature).startswith("void "):
        return False
    target = parameters[1].replace(" ", "")
    pointer = parameters[2].replace(" ", "")
    return (
        ("Il2CppObject*" in target or "System_Object_o*" in target)
        and "intptr_t" in pointer
        and _has_method_info_tail(parameters)
    )


@lru_cache(maxsize=None)
def _match_render_sink(name: str, signature: str) -> bool:
    """Render callbacks are reported but are not treated as value sinks."""

    parameters = _signature_parameters(signature)
    if not _normalise_signature(signature).startswith("void "):
        return False
    if not _has_method_info_tail(parameters):
        return False
    if name == "TMPro.TextMeshProUGUI$$GenerateTextMesh":
        return bool(parameters and "TMPro_TextMeshProUGUI_o*" in parameters[0].replace(" ", ""))
    if name == "TMPro.TextMeshPro$$GenerateTextMesh":
        return bool(parameters and "TMPro_TextMeshPro_o*" in parameters[0].replace(" ", ""))
    if name == "UnityEngine.UI.Text$$OnPopulateMesh":
        return (
            len(parameters) == 3
            and "UnityEngine_UI_Text_o*" in parameters[0].replace(" ", "")
            and "UnityEngine_UI_VertexHelper_o*" in parameters[1].replace(" ", "")
        )
    return False


def _match_string_transform(name: str, signature: str) -> TransformSpec | None:
    parameters = _signature_parameters(signature)
    compact_signature = _normalise_signature(signature).replace(" ", "")
    argument_count = len(parameters) - (1 if _has_method_info_tail(parameters) else 0)
    argument_registers = tuple(
        register
        for register in _parameter_registers(signature)[:argument_count]
        if register is not None and register.startswith("x")
    )
    if name == "System.String$$Format" and any(_is_string_parameter(item) for item in parameters):
        return TransformSpec("Format", argument_registers)
    if name == "System.String$$Concat" and any(_is_string_parameter(item) for item in parameters):
        return TransformSpec("Concat", argument_registers)
    if compact_signature.startswith("System_String_o*") and name in {
        "System.String$$Join",
        "System.String$$Replace",
        "System.String$$Insert",
        "System.String$$Remove",
        "System.String$$Substring",
        "System.String$$ToUpper",
        "System.String$$ToUpperInvariant",
        "System.String$$ToLower",
        "System.String$$ToLowerInvariant",
        "System.String$$Trim",
        "System.String$$TrimStart",
        "System.String$$TrimEnd",
        "System.String$$PadLeft",
        "System.String$$PadRight",
    }:
        return TransformSpec(name.rsplit("$$", 1)[-1], argument_registers)
    if name in {
        "System.Text.StringBuilder$$Append",
        "System.Text.StringBuilder$$AppendLine",
        "System.Text.StringBuilder$$Insert",
        "System.Text.StringBuilder$$Replace",
        "System.Text.StringBuilder$$ToString",
    }:
        return TransformSpec(name.rsplit("$$", 1)[-1], argument_registers)
    return None


def _container_operation(method: MethodRecord | None) -> tuple[str, tuple[int, ...]] | None:
    """Describe a conservative index-insensitive collection operation."""

    if method is None:
        return None
    compact = _normalise_signature(method.signature).replace(" ", "")
    owner = method.name.split("$$", 1)[0]
    member = method.name.split("$$", 1)[-1]
    if owner == "System.Linq.Enumerable" and member.split("<", 1)[0] in {"ToList", "ToArray", "AsEnumerable"}:
        return "copy", ()
    is_collection = any(
        token in owner
        for token in (
            "System.Collections.Generic.List",
            "System.Collections.Generic.Dictionary",
            "System_Collections_Generic_List",
            "System_Collections_Generic_Dictionary",
        )
    )
    if not is_collection:
        return None
    parameters = _signature_parameters(method.signature)
    string_indices = _string_parameter_indices(method.signature)
    if member in {"Add", "AddWithResize", "set_Item"}:
        # IL2CPP generic sharing commonly erases both the key and the value to
        # object.  Either side can be the displayed string (for example a
        # Dictionary<string, Sprite> used to build a reward row), so inspect
        # both; store_container_elements keeps only proven string provenance.
        if "Dictionary" in owner and len(parameters) > 2:
            return "write", (1, 2)
        if string_indices:
            return "write", string_indices
        if "List" in owner and len(parameters) > 1 and "Il2CppObject*" in parameters[1]:
            return "write", (1,)
    if member in {"ToArray", "ToList"}:
        return "copy", ()
    if member in {"Insert", "Enqueue", "Push"} and string_indices:
        return "write", string_indices
    if member in {"get_Item", "Peek", "Dequeue", "Pop"} and (
        compact.startswith("System_String_o*")
        or compact.startswith("Il2CppObject*")
    ):
        return "read", ()
    if member == "GetEnumerator":
        return "enumerate", ()
    return None


def _container_parameter_indices(signature: str) -> tuple[int, ...]:
    """Return managed parameters that carry a supported string collection."""

    return tuple(
        index
        for index, parameter in enumerate(_signature_parameters(signature))
        if any(
            token in parameter
            for token in (
                "System_Collections_Generic_Dictionary",
                "System_Collections_Generic_List",
                "System.Collections.Generic.Dictionary",
                "System.Collections.Generic.List",
            )
        )
        and "MethodInfo" not in parameter
    )


def _display_container_operation(
    method: MethodRecord,
) -> tuple[str, tuple[int, ...]] | None:
    """Recognise GUIContent/OptionData producers and proven GUI consumers."""

    parameters = _signature_parameters(method.signature)
    member = method.name.split("$$", 1)[-1]
    owner = method.name.split("$$", 1)[0]
    is_container = owner in {
        "UnityEngine.GUIContent",
        "UnityEngine.UI.Dropdown.OptionData",
        "TMPro.TMP_Dropdown.OptionData",
    }
    string_indices = _string_parameter_indices(method.signature)
    if is_container and member in {".ctor", "set_text"} and string_indices:
        return "write", string_indices
    if owner in {"UnityEngine.GUI", "UnityEngine.GUILayout"}:
        content_indices = tuple(
            index
            for index, parameter in enumerate(parameters)
            if "UnityEngine_GUIContent_o*" in parameter.replace(" ", "")
        )
        if member in {
            "Label",
            "Button",
            "Box",
            "Toggle",
            "TextField",
            "TextArea",
        } and content_indices:
            return "display", content_indices
    return None


def _load_literal_file(path: Path) -> dict[int, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read literal JSON {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise Il2CppDisplayAnalysisError(f"Literal JSON must be a list: {path}")
    result: dict[int, str] = {}
    for item in payload:
        if not isinstance(item, Mapping) or "address" not in item:
            continue
        result[_parse_address(item["address"])] = str(item.get("value", ""))
    return result


def _read_script_payload(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read script JSON {path}: {exc}") from exc


def _load_script(path: Path, *, payload=None) -> tuple[list[dict[str, Any]], list[int], dict[int, str]]:
    if payload is None:
        payload = _read_script_payload(path)
    if not isinstance(payload, Mapping):
        raise Il2CppDisplayAnalysisError(f"script.json must contain an object: {path}")
    methods = [dict(item) for item in payload.get("ScriptMethod", []) if isinstance(item, Mapping)]
    addresses = sorted({_parse_address(item) for item in payload.get("Addresses", [])})
    literals: dict[int, str] = {}
    for item in payload.get("ScriptString", []):
        if isinstance(item, Mapping) and "Address" in item:
            literals[_parse_address(item["Address"])] = str(item.get("Value", ""))
    return methods, addresses, literals


def _load_script_metadata_type_names(path: Path, *, payload=None) -> dict[int, str]:
    """Return ScriptMetadata TypeInfo cell -> managed type name."""

    if payload is None:
        payload = _read_script_payload(path)
    if not isinstance(payload, Mapping):
        return {}
    result: dict[int, str] = {}
    for item in payload.get("ScriptMetadata", []):
        if not isinstance(item, Mapping) or "Address" not in item:
            continue
        name = item.get("Name")
        if not isinstance(name, str) or not name.endswith("_TypeInfo"):
            continue
        try:
            address = _parse_address(item["Address"])
        except (TypeError, ValueError, Il2CppDisplayAnalysisError):
            continue
        result[address] = name[: -len("_TypeInfo")]
    return result


def _parse_dump_enum_members(path: Path) -> dict[str, tuple[str, ...]]:
    """Parse managed enum type names and declared member names from dump.cs."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    namespace_pattern = re.compile(r"^// Namespace:\s*(?P<name>.*)$")
    enum_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*enum\s+(?P<name>[\w`]+)"
    )
    member_pattern = re.compile(
        r"^\s*public\s+const\s+(?P<type>[\w.`]+)\s+"
        r"(?P<name>[\w@]+)\s*=\s*[^;]+;"
    )
    namespace = ""
    current_type: str | None = None
    result: dict[str, list[str]] = defaultdict(list)
    for line in lines:
        namespace_match = namespace_pattern.match(line)
        if namespace_match:
            namespace = namespace_match.group("name").strip()
            continue
        enum_match = enum_pattern.match(line)
        if enum_match:
            simple_name = enum_match.group("name")
            current_type = f"{namespace}.{simple_name}" if namespace else simple_name
            result.setdefault(current_type, [])
            continue
        if current_type is None:
            continue
        if line.strip() == "}":
            current_type = None
            continue
        member_match = member_pattern.match(line)
        if member_match:
            member_type = member_match.group("type")
            if member_type.rsplit(".", 1)[-1] == current_type.rsplit(".", 1)[-1]:
                result[current_type].append(member_match.group("name").lstrip("@"))
    return {
        enum_type: tuple(dict.fromkeys(members))
        for enum_type, members in result.items()
        if members
    }


def _parse_dump_display_fields(path: Path) -> dict[int, dict[int, str]]:
    """Return method RVA -> ``this`` field offset -> text component type.

    Il2CppDumper's ``dump.cs`` tells us the type of fields loaded from ``this``.
    That is the missing static fact needed for calls compiled as ``blr`` through
    a component vtable: direct-call indexing cannot see those calls at all.
    """

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    field_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+(?:static\s+)?"
        r"(?P<type>[\w.]+)\s+[\w<>]+\s*;\s*//\s*0x(?P<offset>[0-9A-Fa-f]+)"
    )
    rva_pattern = re.compile(r"^\s*//\s*RVA:\s*0x(?P<rva>[0-9A-Fa-f]+)")
    class_pattern = re.compile(r"^\s*(?:public|private|protected|internal)?\s*(?:abstract\s+|sealed\s+|static\s+)*class\s+[\w`.<>]+")
    method_pattern = re.compile(r"^\s*(?:public|private|protected|internal)\s+(?:static\s+)?[\w.<>\[\], *&]+\s+[\w<>]+(?:<[^>]+>)?\s*\(")

    result: dict[int, dict[int, str]] = {}
    fields: dict[int, str] = {}
    pending_rva: int | None = None
    in_methods = False
    for line in lines:
        if class_pattern.match(line):
            fields = {}
            pending_rva = None
            in_methods = False
            continue
        if "// Fields" in line:
            in_methods = False
            continue
        if "// Methods" in line:
            in_methods = True
            continue
        if not in_methods:
            field_match = field_pattern.match(line)
            if field_match and not re.search(r"\bstatic\b", line):
                component_type = _normalise_display_field_type(field_match.group("type"))
                if component_type:
                    fields[int(field_match.group("offset"), 16)] = component_type
            continue
        rva_match = rva_pattern.match(line)
        if rva_match:
            pending_rva = int(rva_match.group("rva"), 16)
            continue
        if pending_rva is not None and method_pattern.match(line):
            if fields:
                result[pending_rva] = dict(fields)
            pending_rva = None
    return result


def _parse_dump_virtual_text_slots(path: Path) -> dict[str, frozenset[int]]:
    """Resolve display ``set_text`` vtable offsets from dump.cs Slot metadata."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    namespace_pattern = re.compile(r"^// Namespace:\s*(?P<name>.*)$")
    class_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*"
        r"(?:abstract\s+|sealed\s+|static\s+|partial\s+)*class\s+"
        r"(?P<name>[\w`.<>]+)"
    )
    slot_pattern = re.compile(r"\bSlot:\s*(?P<slot>\d+)\b")
    setter_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+"
        r"(?:virtual\s+|override\s+|final\s+)*void\s+set_text\s*\(\s*string\s+\w+\s*\)"
    )
    namespace = ""
    component_type: str | None = None
    pending_slot: int | None = None
    result: dict[str, set[int]] = defaultdict(set)
    for line in lines:
        namespace_match = namespace_pattern.match(line)
        if namespace_match:
            namespace = namespace_match.group("name").strip()
            continue
        class_match = class_pattern.match(line)
        if class_match:
            simple_name = class_match.group("name")
            full_name = f"{namespace}.{simple_name}" if namespace else simple_name
            component_type = (
                _normalise_display_field_type(full_name)
                or _normalise_display_field_type(simple_name)
            )
            pending_slot = None
            continue
        slot_match = slot_pattern.search(line)
        if slot_match:
            pending_slot = int(slot_match.group("slot"))
            continue
        if pending_slot is not None and setter_pattern.match(line):
            if component_type is not None:
                result[component_type].add(
                    IL2CPP64_VTABLE_OFFSET + pending_slot * 16
                )
            pending_slot = None
        elif line.lstrip().startswith("// RVA:"):
            pending_slot = None
    return {key: frozenset(values) for key, values in result.items()}


def _parse_dump_carrier_field_types(path: Path) -> dict[tuple[str, ...], dict[int, tuple[str, ...]]]:
    """Resolve instance reference fields to unambiguous concrete class layouts."""
    layouts: dict[str, dict[int, str]] = {}
    namespace = ""
    owner = None
    in_fields = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("// Namespace:"):
            namespace = line.partition(":")[2].strip()
            owner = None
            in_fields = False
        match = re.match(r"\s*(?:(?:public|private|protected|internal|abstract|sealed|static|partial)\s+)*class\s+([\w.]+)(?=\s|:|$)", line)
        if match:
            owner = f"{namespace}.{match[1]}" if namespace else match[1]
            layouts.setdefault(owner, {})
            in_fields = False
        elif "// Fields" in line:
            in_fields = True
        elif "// Methods" in line or "// Properties" in line:
            in_fields = False
        elif owner and in_fields:
            match = re.match(r"\s*(?:public|private|protected|internal)\s+(?:readonly\s+)?([\w.<>\[\],]+)\s+[\w<>]+;\s*//\s*0x([0-9A-Fa-f]+)", line)
            if match:
                layouts[owner][int(match[2], 16)] = match[1]
    names: dict[str, set[str]] = defaultdict(set)
    for name in layouts:
        parts = name.split('.')
        for index in range(len(parts)):
            names['.'.join(parts[index:])].add(name)
    result = {}
    for owner, fields in layouts.items():
        resolved = {}
        for offset, name in fields.items():
            if name in {"string[]", "String[]", "System.String[]", "List<string>", "List<System.String>"}:
                resolved[offset] = ("__string_collection__",)
                continue
            candidates = names.get(name, set())
            if len(candidates) == 1:
                resolved[offset] = _family_parts(next(iter(candidates)))
        if resolved:
            result[_family_parts(owner)] = resolved
    return result


def _parse_dump_class_display_fields(path: Path) -> dict[str, dict[int, str]]:
    """Return concrete class name -> direct display-component field layout."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    namespace_pattern = re.compile(r"^// Namespace:\s*(?P<name>.*)$")
    class_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*"
        r"(?:abstract\s+|sealed\s+|static\s+|partial\s+)*class\s+"
        r"(?P<name>[\w`.<>]+)"
    )
    field_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+"
        r"(?P<static>static\s+)?(?P<type>[\w.`<>\[\],]+)\s+[\w<>]+\s*;"
        r"\s*//\s*0x(?P<offset>[0-9A-Fa-f]+)"
    )
    namespace = ""
    current_class: str | None = None
    in_fields = False
    layouts: dict[str, dict[int, str]] = {}
    for line in lines:
        namespace_match = namespace_pattern.match(line)
        if namespace_match:
            namespace = namespace_match.group("name").strip()
            continue
        class_match = class_pattern.match(line)
        if class_match:
            simple_name = class_match.group("name")
            current_class = f"{namespace}.{simple_name}" if namespace else simple_name
            layouts.setdefault(current_class, {})
            in_fields = False
            continue
        if current_class is None:
            continue
        if "// Fields" in line:
            in_fields = True
            continue
        if "// Methods" in line:
            in_fields = False
            continue
        if not in_fields:
            continue
        field_match = field_pattern.match(line)
        if field_match and not field_match.group("static"):
            display_type = _normalise_display_field_type(field_match.group("type"))
            if display_type:
                layouts[current_class][int(field_match.group("offset"), 16)] = display_type
    return {name: fields for name, fields in layouts.items() if fields}


def _parse_dump_return_object_display_fields(
    path: Path,
) -> dict[int, dict[int, str]]:
    """Map factory-method RVAs to direct text fields on their return objects.

    IL2CPP UI helpers commonly return a project-owned row/view object and then
    immediately write ``returned.TextField.text``.  The old analyser only knew
    factories that returned Text/TMP/UILabel themselves, so this perfectly
    ordinary indirection broke wrapper discovery.
    """

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    namespace_pattern = re.compile(r"^// Namespace:\s*(?P<name>.*)$")
    class_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*"
        r"(?:abstract\s+|sealed\s+|static\s+|partial\s+)*class\s+"
        r"(?P<name>[\w`.<>]+)"
    )
    field_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+"
        r"(?P<static>static\s+)?(?P<type>[\w.`<>\[\],]+)\s+[\w<>]+\s*;"
        r"\s*//\s*0x(?P<offset>[0-9A-Fa-f]+)"
    )
    rva_pattern = re.compile(r"^\s*//\s*RVA:\s*0x(?P<rva>[0-9A-Fa-f]+)")
    method_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+"
        r"(?:static\s+|virtual\s+|override\s+|abstract\s+)*"
        r"(?P<return>[\w.`<>\[\],]+)\s+\w+(?:<[^>]+>)?\s*\("
    )

    layouts: dict[str, dict[int, str]] = {}
    namespace = ""
    current_class: str | None = None
    in_fields = False
    for line in lines:
        namespace_match = namespace_pattern.match(line)
        if namespace_match:
            namespace = namespace_match.group("name").strip()
            continue
        class_match = class_pattern.match(line)
        if class_match:
            simple_name = class_match.group("name")
            current_class = f"{namespace}.{simple_name}" if namespace else simple_name
            layouts.setdefault(current_class, {})
            in_fields = False
            continue
        if current_class is None:
            continue
        if "// Fields" in line:
            in_fields = True
            continue
        if "// Methods" in line:
            in_fields = False
            continue
        if not in_fields:
            continue
        field_match = field_pattern.match(line)
        if field_match and not field_match.group("static"):
            display_type = _normalise_display_field_type(field_match.group("type"))
            if display_type:
                layouts[current_class][int(field_match.group("offset"), 16)] = display_type

    aliases: dict[str, list[str]] = defaultdict(list)
    for class_name in layouts:
        aliases[class_name].append(class_name)
        aliases[class_name.rsplit(".", 1)[-1]].append(class_name)

    result: dict[int, dict[int, str]] = {}
    pending_rva: int | None = None
    for line in lines:
        rva_match = rva_pattern.match(line)
        if rva_match:
            pending_rva = int(rva_match.group("rva"), 16)
            continue
        if pending_rva is None:
            continue
        method_match = method_pattern.match(line)
        if method_match:
            return_type = method_match.group("return").replace("global::", "")
            candidates = aliases.get(return_type, [])
            if len(candidates) == 1 and layouts.get(candidates[0]):
                result[pending_rva] = dict(layouts[candidates[0]])
            pending_rva = None
        elif line.lstrip().startswith("// RVA:"):
            pending_rva = None
    return result


def _parse_dump_nested_display_fields(
    path: Path,
    *,
    max_depth: int = 3,
) -> dict[int, dict[tuple[int, ...], str]]:
    """Resolve high-confidence ``this.child.label`` component field paths."""

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read dump.cs {path}: {exc}") from exc

    namespace_pattern = re.compile(r"^// Namespace:\s*(?P<name>.*)$")
    class_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)?\s*"
        r"(?:abstract\s+|sealed\s+|static\s+|partial\s+)*class\s+"
        r"(?P<name>[\w`.<>]+)"
    )
    field_pattern = re.compile(
        r"^\s*(?:public|private|protected|internal)\s+"
        r"(?P<static>static\s+)?(?P<type>[\w.`<>\[\],]+)\s+[\w<>]+\s*;"
        r"\s*//\s*0x(?P<offset>[0-9A-Fa-f]+)"
    )
    rva_pattern = re.compile(r"^\s*//\s*RVA:\s*0x(?P<rva>[0-9A-Fa-f]+)")

    layouts: dict[str, dict[int, str]] = {}
    methods_by_class: dict[str, set[int]] = defaultdict(set)
    namespace = ""
    current_class: str | None = None
    in_fields = False
    for line in lines:
        namespace_match = namespace_pattern.match(line)
        if namespace_match:
            namespace = namespace_match.group("name").strip()
            continue
        class_match = class_pattern.match(line)
        if class_match:
            simple_name = class_match.group("name")
            current_class = f"{namespace}.{simple_name}" if namespace else simple_name
            layouts.setdefault(current_class, {})
            in_fields = False
            continue
        if current_class is None:
            continue
        if "// Fields" in line:
            in_fields = True
            continue
        if "// Methods" in line:
            in_fields = False
            continue
        if in_fields:
            field_match = field_pattern.match(line)
            if field_match and not field_match.group("static"):
                layouts[current_class][int(field_match.group("offset"), 16)] = (
                    field_match.group("type")
                )
            continue
        rva_match = rva_pattern.match(line)
        if rva_match:
            methods_by_class[current_class].add(int(rva_match.group("rva"), 16))

    aliases: dict[str, list[str]] = defaultdict(list)
    for class_name in layouts:
        aliases[class_name].append(class_name)
        simple_name = class_name.rsplit(".", 1)[-1]
        if simple_name != class_name:
            aliases[simple_name].append(class_name)

    def resolve_layout(type_name: str) -> str | None:
        compact = type_name.replace("global::", "").strip()
        candidates = aliases.get(compact, [])
        return candidates[0] if len(candidates) == 1 else None

    paths_by_class: dict[str, dict[tuple[int, ...], str]] = {}
    for root_class in layouts:
        found: dict[tuple[int, ...], str] = {}

        def walk(class_name: str, prefix: tuple[int, ...], seen: frozenset[str]) -> None:
            if len(prefix) >= max_depth or class_name in seen:
                return
            for offset, field_type in layouts.get(class_name, {}).items():
                path_key = prefix + (offset,)
                display_type = _normalise_display_field_type(field_type)
                if display_type:
                    if len(path_key) > 1:
                        found[path_key] = display_type
                    continue
                nested_class = resolve_layout(field_type)
                if nested_class is not None:
                    walk(nested_class, path_key, seen | {class_name})

        walk(root_class, (), frozenset())
        if found:
            paths_by_class[root_class] = found

    result: dict[int, dict[tuple[int, ...], str]] = {}
    for class_name, paths in paths_by_class.items():
        for method_address in methods_by_class.get(class_name, ()):
            result[method_address] = dict(paths)
    return result


def _collect_relative_string_slots(elf: Any, literal_cells: set[int]) -> dict[int, int]:
    """Return relocation offset -> stringliteral cell for AArch64 RELATIVE entries."""

    section = elf.get_section_by_name(".rela.dyn")
    if section is None:
        return {}
    result: dict[int, int] = {}
    for relocation in section.iter_relocations():
        try:
            relocation_type = int(relocation["r_info_type"])
            addend = int(relocation["r_addend"])
            offset = int(relocation["r_offset"])
        except (KeyError, TypeError, ValueError):
            continue
        if relocation_type == AARCH64_RELATIVE_RELOCATION and addend in literal_cells:
            result[offset] = addend
    return result


def _collect_relative_slots(elf: Any) -> dict[int, int]:
    """Return every AArch64 RELATIVE relocation slot and its resolved target.

    String literals are only one kind of pointer materialised through
    ``.rela.dyn``.  Delegate subscription code also loads an Il2Cpp method
    metadata cell through such a slot, so preserve the generic relation here.
    """

    section = elf.get_section_by_name(".rela.dyn")
    if section is None:
        return {}
    result: dict[int, int] = {}
    for relocation in section.iter_relocations():
        try:
            relocation_type = int(relocation["r_info_type"])
            addend = int(relocation["r_addend"])
            offset = int(relocation["r_offset"])
        except (KeyError, TypeError, ValueError):
            continue
        if relocation_type == AARCH64_RELATIVE_RELOCATION:
            result[offset] = addend
    return result


def _load_script_metadata_method_targets(path: Path, *, payload=None) -> dict[int, int]:
    """Map Il2CppDumper ``ScriptMetadataMethod`` cells to native method RVAs."""

    if payload is None:
        payload = _read_script_payload(path)
    if not isinstance(payload, Mapping):
        return {}
    result: dict[int, int] = {}
    for item in payload.get("ScriptMetadataMethod", []):
        if not isinstance(item, Mapping) or "Address" not in item or "MethodAddress" not in item:
            continue
        try:
            cell = _parse_address(item["Address"])
            target = _parse_address(item["MethodAddress"])
        except (TypeError, ValueError, Il2CppDisplayAnalysisError):
            continue
        if cell > 0 and target > 0:
            result[cell] = target
    return result


def _load_script_metadata_component_factory_types(path: Path, *, payload=None) -> dict[int, str]:
    """Map generic GetComponent MethodInfo cells to their concrete result type.

    Generic sharing makes the native method appear as ``GetComponent<object>``.
    Il2CppDumper still records the concrete generic instantiation in
    ScriptMetadataMethod, which lets callers recover the layout of the object
    returned by that otherwise erased factory.
    """

    if payload is None:
        payload = _read_script_payload(path)
    if not isinstance(payload, Mapping):
        return {}
    pattern = re.compile(
        r"^Method\$UnityEngine\.(?:GameObject|Component)\."
        r"(?:GetComponent|GetComponentInChildren|GetComponentInParent)"
        r"<(?P<type>[^>]+)>"
    )
    result: dict[int, str] = {}
    for item in payload.get("ScriptMetadataMethod", []):
        if not isinstance(item, Mapping) or "Address" not in item:
            continue
        match = pattern.match(str(item.get("Name", "")))
        if match is None:
            continue
        try:
            cell = _parse_address(item["Address"])
        except (TypeError, ValueError, Il2CppDisplayAnalysisError):
            continue
        if cell > 0:
            result[cell] = match.group("type").replace("global::", "")
    return result


def _prepare_methods(
    method_payload: Sequence[Mapping[str, Any]],
    addresses: Sequence[int],
    section_ends: Sequence[int],
) -> tuple[dict[int, MethodRecord], list[int]]:
    address_set = {_parse_address(value) for value in addresses}
    method_data: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for item in method_payload:
        if "Address" not in item:
            continue
        address = _parse_address(item["Address"])
        alias = (str(item.get("Name", "")), str(item.get("Signature", "")))
        if alias not in method_data[address]:
            method_data[address].append(alias)
        address_set.add(address)
    all_addresses = sorted(address_set)
    maximum_end = max(section_ends, default=(all_addresses[-1] + 4 if all_addresses else 4))
    result: dict[int, MethodRecord] = {}
    def alias_score(alias: tuple[str, str]) -> tuple[int, int, int]:
        name, signature = alias
        record = MethodRecord(0, 4, name, signature)
        score = 0
        if _match_display_sink(name, signature):
            score += 1000
        if _is_delegate_constructor(record) or name == "System.Delegate$$Combine":
            score += 500
        if _normalise_signature(signature).startswith("System_String_o*"):
            score += 200
        score += len(_string_parameter_indices(signature)) * 50
        if _is_project_owned_method(record):
            score += 10
        return score, len(signature), len(name)

    for address, aliases in method_data.items():
        index = bisect_right(all_addresses, address)
        end = all_addresses[index] if index < len(all_addresses) else maximum_end
        if end <= address:
            end = address + 4
        name, signature = max(aliases, key=alias_score)
        result[address] = MethodRecord(address, end, name, signature, tuple(aliases))
    return result, all_addresses


def _sign_extend(value: int, bits: int) -> int:
    sign_bit = 1 << (bits - 1)
    return value - (1 << bits) if value & sign_bit else value


def _decode_direct_branch(word: int, pc: int) -> tuple[str, int] | None:
    if word & 0xFC000000 == 0x94000000:
        return "bl", pc + (_sign_extend(word & 0x03FFFFFF, 26) << 2)
    if word & 0xFC000000 == 0x14000000:
        return "b", pc + (_sign_extend(word & 0x03FFFFFF, 26) << 2)
    return None


def _method_for_pc(addresses: Sequence[int], pc: int) -> int | None:
    index = bisect_right(addresses, pc) - 1
    return addresses[index] if index >= 0 else None


def _build_call_index(
    code_sections: Sequence[tuple[int, bytes]],
    methods: Mapping[int, MethodRecord],
    addresses: Sequence[int],
) -> dict[int, set[int]]:
    """Index direct BL and external tail-B calls by target."""

    result: dict[int, set[int]] = defaultdict(set)
    method_addresses = set(methods)
    for base, data in code_sections:
        limit = len(data) - (len(data) % 4)
        for word_index, (word,) in enumerate(struct.iter_unpack("<I", memoryview(data)[:limit])):
            offset = word_index * 4
            decoded = _decode_direct_branch(word, base + offset)
            if decoded is None:
                continue
            kind, target = decoded
            if target not in method_addresses:
                continue
            caller_address = _method_for_pc(addresses, base + offset)
            caller = methods.get(caller_address) if caller_address is not None else None
            if caller is None:
                continue
            if kind == "b" and caller.address <= target < caller.end:
                continue
            result[target].add(caller.address)
    return result


def _literal_reference_methods(
    code_sections: Sequence[tuple[int, bytes]],
    addresses: Sequence[int],
    methods: Mapping[int, MethodRecord],
    slot_to_cell: Mapping[int, int],
) -> set[int]:
    """Cheaply find methods that load a relocated string-literal slot.

    This intentionally decodes only the common ``ADRP; LDR`` materialisation
    sequence.  It narrows virtual-display analysis to project methods that both
    own a text component field and actually reference a string literal, instead
    of disassembling every method in a large libil2cpp binary.
    """

    found: set[int] = set()
    known_methods = set(methods)
    for base, data in code_sections:
        limit = len(data) - (len(data) % 4)
        words = [word for (word,) in struct.iter_unpack("<I", memoryview(data)[:limit])]
        for index, word in enumerate(words):
            if word & 0x9F000000 != 0x90000000:  # ADRP
                continue
            pc = base + index * 4
            destination = word & 0x1F
            immediate = ((word >> 5) & 0x7FFFF) << 2 | ((word >> 29) & 0x3)
            page = (pc & ~0xFFF) + (_sign_extend(immediate, 21) << 12)
            for following in words[index + 1 : min(index + 5, len(words))]:
                # 64-bit unsigned-immediate LDR Xt, [Xn, #imm].
                if following & 0xFFC00000 != 0xF9400000:
                    continue
                if ((following >> 5) & 0x1F) != destination:
                    continue
                slot = page + (((following >> 10) & 0xFFF) * 8)
                if slot not in slot_to_cell:
                    continue
                owner = _method_for_pc(addresses, pc)
                if owner in known_methods:
                    found.add(owner)
                break
    return found


def _methods_with_indirect_calls(
    code_sections: Sequence[tuple[int, bytes]],
    addresses: Sequence[int],
    methods: Mapping[int, MethodRecord],
) -> set[int]:
    """Return methods containing an ARM64 indirect call or tail branch.

    This is intentionally only a cheap candidate filter.  A method becomes a
    delegate-display hit later only after both the delegate object layout and
    its ``Delegate.Combine`` subscription have been proved.
    """

    result: set[int] = set()
    known_methods = set(methods)
    for base, data in code_sections:
        limit = len(data) - (len(data) % 4)
        for index, (word,) in enumerate(struct.iter_unpack("<I", memoryview(data)[:limit])):
            # BLR Xn: 1101011000111111000000 nnnnn 00000
            if word & 0xFFFFFC1F not in {0xD63F0000, 0xD61F0000}:
                continue
            owner = _method_for_pc(addresses, base + index * 4)
            if owner in known_methods:
                result.add(owner)
    return result


def _scan_code_indexes(
    code_sections: Sequence[tuple[int, bytes]],
    addresses: Sequence[int],
    methods: Mapping[int, MethodRecord],
    tracked_slots: set[int],
) -> tuple[dict[int, set[int]], set[int], dict[int, set[int]]]:
    """Build call, BLR and relocation-reference indexes in one text pass."""

    call_index: dict[int, set[int]] = defaultdict(set)
    indirect_methods: set[int] = set()
    references_by_method: dict[int, set[int]] = defaultdict(set)
    method_addresses = set(methods)

    for base, data in code_sections:
        limit = len(data) - (len(data) % 4)
        words = [word for (word,) in struct.iter_unpack("<I", memoryview(data)[:limit])]
        for index, word in enumerate(words):
            pc = base + index * 4
            owner = _method_for_pc(addresses, pc)
            if owner not in method_addresses:
                continue

            decoded = _decode_direct_branch(word, pc)
            if decoded is not None:
                kind, target = decoded
                if target in method_addresses:
                    caller = methods[owner]
                    if kind != "b" or not caller.address <= target < caller.end:
                        call_index[target].add(owner)
            if word & 0xFFFFFC1F in {0xD63F0000, 0xD61F0000}:
                indirect_methods.add(owner)

            # LDR literal: target is PC + sign_extend(imm19 << 2).
            if word & 0x3B000000 == 0x18000000:
                target = pc + (_sign_extend((word >> 5) & 0x7FFFF, 19) << 2)
                if target in tracked_slots:
                    references_by_method[owner].add(target)

            if word & 0x9F000000 != 0x90000000:  # ADRP
                continue
            register = word & 0x1F
            immediate = ((word >> 5) & 0x7FFFF) << 2 | ((word >> 29) & 0x3)
            current_address = (pc & ~0xFFF) + (_sign_extend(immediate, 21) << 12)
            current_register = register
            for following in words[index + 1 : min(index + 9, len(words))]:
                # ADD Xd, Xn, #imm{, LSL #12}
                if following & 0x7F000000 == 0x11000000:
                    source = (following >> 5) & 0x1F
                    if source == current_register:
                        shift = 12 if ((following >> 22) & 1) else 0
                        current_address += ((following >> 10) & 0xFFF) << shift
                        current_register = following & 0x1F
                        if current_address in tracked_slots:
                            references_by_method[owner].add(current_address)
                        continue
                # 64-bit unsigned immediate LDR Xt,[Xn,#imm].
                if following & 0xFFC00000 == 0xF9400000:
                    source = (following >> 5) & 0x1F
                    if source == current_register:
                        slot = current_address + (((following >> 10) & 0xFFF) * 8)
                        if slot in tracked_slots:
                            references_by_method[owner].add(slot)
                        current_register = following & 0x1F
                        current_address = slot
                        continue
                # Stop once the tracked address register is overwritten by a
                # shape this cheap scanner cannot safely model.
                destination = following & 0x1F
                if destination == current_register:
                    break
    return call_index, indirect_methods, references_by_method


def _find_code_slice(
    code_sections: Sequence[tuple[int, bytes]], method: MethodRecord
) -> tuple[int, bytes] | None:
    for base, data in code_sections:
        section_end = base + len(data)
        if base <= method.address < section_end:
            end = min(method.end, section_end)
            return method.address, data[method.address - base : end - base]
    return None


def _canonical_register(md: Any, register_id: int) -> str:
    name = md.reg_name(register_id)
    if re.fullmatch(r"w\d+", name):
        return "x" + name[1:]
    return name


def _is_native_reference_compare_exchange(code_sections, target: int, md: Any) -> bool:
    """Prove the ARM64 IL2CPP reference CAS wrapper and its atomic callee.

    No symbol/name/address heuristic: reject wrappers whose instructions or
    argument permutation differ. The second call is the GC write barrier.
    """
    def read(address, count):
        part = _find_code_slice(code_sections, MethodRecord(address, address + count * 4, "", ""))
        return list(md.disasm(part[1], address)) if part else []

    ins = read(target, 16)
    expected = [
        ("str", "x30, [sp, #-0x20]!"), ("stp", "x20, x19, [sp, #0x10]"),
        ("mov", "x20, x0"), ("mov", "x19, x2"), ("mov", "x0, x2"),
        ("mov", "x2, x20"), ("bl", None), ("cmp", "x0, x19"),
        ("dmb", "ish"), ("csel", "x19, x19, x0, eq"),
        ("mov", "x0, x20"), ("bl", None), ("mov", "x0, x19"),
        ("ldp", "x20, x19, [sp, #0x10]"), ("ldr", "x30, [sp], #0x20"), ("ret", ""),
    ]
    if len(ins) != len(expected) or any(i.mnemonic != name or (args is not None and i.op_str != args)
                                           for i, (name, args) in zip(ins, expected)):
        return False
    atomic = read(ins[6].operands[0].imm, 13)
    if len(atomic) != 13:
        return False
    # Runtime CPU feature check selects LSE CAS or the equivalent exclusive loop.
    fixed = {0: ("bti", "c"), 4: ("casal", "x0, x1, [x2]"), 5: ("ret", ""),
             6: ("mov", "x16, x0"), 7: ("ldaxr", "x0, [x2]"),
             8: ("cmp", "x0, x16"), 10: ("stlxr", "w17, x1, [x2]"), 12: ("ret", "")}
    return (all((atomic[n].mnemonic, atomic[n].op_str) == pair for n, pair in fixed.items())
            and atomic[1].mnemonic == "adrp" and atomic[1].op_str.startswith("x16, ")
            and atomic[2].mnemonic == "ldrb" and atomic[2].op_str.startswith("w16, [x16")
            and atomic[3].mnemonic == "cbz" and atomic[3].op_str.startswith("w16, ")
            and atomic[3].operands[-1].imm == atomic[6].address
            and atomic[9].mnemonic == "b.ne" and atomic[9].operands[-1].imm == atomic[12].address
            and atomic[11].mnemonic == "cbnz" and atomic[11].op_str.startswith("w17, ")
            and atomic[11].operands[-1].imm == atomic[7].address)


def _create_arm64_disassembler() -> Any:
    try:
        from capstone import CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN, Cs
    except ImportError as exc:  # pragma: no cover - deployment failure
        raise Il2CppDisplayAnalysisError(
            "capstone is required for IL2CPP display analysis"
        ) from exc
    disassembler = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
    disassembler.detail = True
    return disassembler


def _state_values(state: AbstractState, register: str) -> frozenset[AbstractValue]:
    return state.registers.get(register, frozenset())


def _limit_values(values: Iterable[AbstractValue]) -> frozenset[AbstractValue]:
    """Bound path-union growth while keeping deterministic representatives."""

    # Origin is evidence, not abstract semantics.  Keeping one value per
    # (kind, source, transforms) prevents the same literal from consuming the
    # entire budget merely because it arrived along several CFG paths.
    semantic: dict[tuple[str, int, tuple[str, ...]], AbstractValue] = {}
    for value in values:
        if len(value.transforms) > MAX_ABSTRACT_TRANSFORM_STEPS:
            # Cyclic parsers/formatters can otherwise append an unbounded path
            # and prevent the data-flow fixed point from converging.  Field
            # compatibility is suffix-based, so retain the most recent steps.
            value = AbstractValue(
                value.kind,
                value.source,
                value.origin,
                value.transforms[-MAX_ABSTRACT_TRANSFORM_STEPS:],
            )
        key = (value.kind, value.source, value.transforms)
        previous = semantic.get(key)
        if previous is None or value.origin < previous.origin:
            semantic[key] = value
    unique = set(semantic.values())
    if len(unique) <= MAX_ABSTRACT_VALUES:
        return frozenset(unique)
    priority = {
        "exact": 0,
        "derived": 1,
        "probable": 2,
        "probable_derived": 3,
        "param": 4,
        "derived_param": 5,
        "container_param": 5,
        "delegate_target": 4,
        "delegate_combined": 5,
        "display_receiver": 6,
        "display_vtable": 7,
        "display_virtual_function": 8,
        "memory_path": 9,
        "pointer_slot": 10,
        "method_pointer": 11,
        "metadata_method_cell": 12,
        "cell": 13,
        "address": 14,
        "this": 15,
        "stack_address": 16,
        "call_result": 99,
        "typed_carrier_object": 18,
    }
    return frozenset(
        sorted(
            unique,
            key=lambda value: (
                priority.get(value.kind, 50),
                value.kind,
                value.source,
                value.origin,
                value.transforms,
            ),
        )[:MAX_ABSTRACT_VALUES]
    )


def _set_register(state: AbstractState, register: str, values: Iterable[AbstractValue]) -> None:
    if register in {"sp", "xzr", "wzr", ""}:
        return
    frozen = _limit_values(values)
    if frozen:
        state.registers[register] = frozen
    else:
        state.registers.pop(register, None)


def _merge_state(
    existing: AbstractState,
    incoming: AbstractState,
    *,
    on_truncate: Any | None = None,
) -> tuple[AbstractState, bool]:
    # Register alternatives are intentionally retained at a join so each
    # independently proven literal path can continue to a display sink.
    changed = False
    merged = existing.copy()
    for register, values in incoming.registers.items():
        raw = merged.registers.get(register, frozenset()) | values
        combined = _limit_values(raw)
        semantic_count = len({
            (value.kind, value.source, value.transforms) for value in raw
        })
        if on_truncate is not None and len(combined) < semantic_count:
            on_truncate(register, semantic_count, len(combined))
        if combined != merged.registers.get(register, frozenset()):
            merged.registers[register] = combined
            changed = True
    if existing.sp_offset == incoming.sp_offset:
        for offset, values in incoming.stack.items():
            raw = merged.stack.get(offset, frozenset()) | values
            combined = _limit_values(raw)
            semantic_count = len({
                (value.kind, value.source, value.transforms) for value in raw
            })
            if on_truncate is not None and len(combined) < semantic_count:
                on_truncate(f"stack:{offset:+#x}", semantic_count, len(combined))
            if combined != merged.stack.get(offset, frozenset()):
                merged.stack[offset] = combined
                changed = True
    elif merged.stack:
        merged.stack = {}
        changed = True
    for key, values in incoming.container_elements.items():
        raw = merged.container_elements.get(key, frozenset()) | values
        combined = _limit_values(raw)
        semantic_count = len({
            (value.kind, value.source, value.transforms) for value in raw
        })
        if on_truncate is not None and len(combined) < semantic_count:
            on_truncate(
                f"container:{key[0]}:{key[1]:#x}", semantic_count, len(combined)
            )
        if combined != merged.container_elements.get(key, frozenset()):
            merged.container_elements[key] = combined
            changed = True
    return merged, changed


def _clear_caller_saved(state: AbstractState) -> None:
    for index in range(19):
        state.registers.pop(f"x{index}", None)


def _sink_evidence(
    method: MethodRecord,
    callsite: int,
    sink: SinkSpec,
    value: AbstractValue,
) -> dict[str, Any]:
    return {
        "method_address": _hex(method.address),
        "method_name": method.name,
        "callsite": _hex(callsite),
        "sink_address": _hex(sink.address),
        "sink_name": sink.name,
        "sink_signature": sink.signature,
        "source_instruction": _hex(value.origin),
        "path": list(sink.path),
        "transforms": list(value.transforms),
    }


def _dedupe_dicts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _analyse_method(
    *,
    method: MethodRecord,
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None = None,
    literal_cells: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    sinks_by_address: Mapping[int, Sequence[SinkSpec]],
    transforms_by_address: Mapping[int, TransformSpec],
    initialise_parameters: bool,
    display_fields: Mapping[int, str] | None = None,
    nested_display_fields: Mapping[tuple[int, ...], str] | None = None,
    virtual_text_slots_by_component: Mapping[str, frozenset[int]] | None = None,
    pointer_slots: Mapping[int, int] | None = None,
    metadata_method_targets: Mapping[int, int] | None = None,
    delegate_constructor_addresses: frozenset[int] = frozenset(),
    delegate_combine_addresses: frozenset[int] = frozenset(),
    delegate_add_sinks_by_address: Mapping[
        int, Sequence[tuple[str, tuple[int, ...]]]
    ] | None = None,
    display_container_operations: Mapping[
        int, tuple[str, tuple[int, ...]]
    ] | None = None,
    return_summaries_by_address: Mapping[int, Sequence[AbstractValue]] | None = None,
    methods_by_address: Mapping[int, MethodRecord] | None = None,
    return_object_display_fields: Mapping[int, Mapping[int, str]] | None = None,
    enum_type_by_pointer_slot: Mapping[int, str] | None = None,
    disassembler: Any | None = None,
    instruction_cache: dict[int, tuple[Any, ...]] | None = None,
    trace_addresses: frozenset[int] = frozenset(),
    track_object_fields: bool = False,
    string_collection_fields: frozenset[int] = frozenset(),
) -> MethodAnalysis:
    try:
        from capstone import CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN, Cs
        from capstone.arm64 import ARM64_OP_IMM, ARM64_OP_MEM, ARM64_OP_REG
    except ImportError as exc:  # pragma: no cover - exercised in deployment failures
        raise Il2CppDisplayAnalysisError(
            "capstone is required for IL2CPP display analysis"
        ) from exc

    code_slice = _find_code_slice(code_sections, method)
    if code_slice is None:
        return MethodAnalysis()
    start, data = code_slice
    md = disassembler
    if md is None:
        md = Cs(CS_ARCH_ARM64, CS_MODE_LITTLE_ENDIAN)
        md.detail = True
    cached_instructions = (
        instruction_cache.get(method.address) if instruction_cache is not None else None
    )
    if cached_instructions is None:
        instructions = list(md.disasm(data, start))
        if instruction_cache is not None:
            instruction_cache[method.address] = tuple(instructions)
    else:
        instructions = list(cached_instructions)
    if not instructions:
        return MethodAnalysis()
    instruction_map = {instruction.address: instruction for instruction in instructions}
    instruction_indexes = {
        instruction.address: index for index, instruction in enumerate(instructions)
    }

    entry = AbstractState()
    pointer_slots = pointer_slots or {}
    metadata_method_targets = metadata_method_targets or {}
    return_summaries_by_address = return_summaries_by_address or {}
    methods_by_address = methods_by_address or {}
    return_object_display_fields = return_object_display_fields or {}
    enum_type_by_pointer_slot = enum_type_by_pointer_slot or {}
    delegate_add_sinks_by_address = delegate_add_sinks_by_address or {}
    display_container_operations = display_container_operations or {}
    virtual_text_slots_by_component = virtual_text_slots_by_component or {}
    # For project methods with a known display component field, x0 is ``this``.
    # This lets a later ``ldr xN, [x0, #field]`` retain the component type even
    # when the eventual setter is a virtual ``blr`` rather than a direct ``bl``.
    if display_fields:
        entry.registers["x0"] = frozenset({AbstractValue("this", 0, method.address)})
    elif nested_display_fields:
        entry.registers["x0"] = frozenset({AbstractValue("this", 0, method.address)})
    if initialise_parameters:
        parameters = _signature_parameters(method.signature)
        parameter_registers = _parameter_registers(method.signature)
        if track_object_fields:
            for parameter_index, parameter in enumerate(parameters):
                if not _is_object_pointer_parameter(parameter):
                    continue
                argument_register = parameter_registers[parameter_index]
                if argument_register is None or not argument_register.startswith("x"):
                    continue
                object_value = AbstractValue(
                    "object_this" if parameter_index == 0 else "object_param",
                    parameter_index,
                    method.address,
                )
                # A class can own both a Text component field and unrelated
                # model/container fields. Keep both views of x0 so resolving a
                # virtual text receiver does not suppress ordinary instance
                # field tracking (for example InGameLogManager.messagePresets).
                entry.registers[argument_register] = frozenset(
                    set(entry.registers.get(argument_register, frozenset()))
                    | {object_value}
                )
        for parameter_index in _string_parameter_indices(method.signature):
            argument_register = parameter_registers[parameter_index]
            if argument_register is not None and argument_register.startswith("x"):
                entry.registers[argument_register] = frozenset(
                    {AbstractValue("param", parameter_index, method.address)}
                )
        for parameter_index in _container_parameter_indices(method.signature):
            argument_register = parameter_registers[parameter_index]
            if argument_register is None or not argument_register.startswith("x"):
                continue
            entry.registers[argument_register] = frozenset(
                set(entry.registers.get(argument_register, frozenset()))
                | {AbstractValue("container_param", parameter_index, method.address)}
            )

    states: dict[int, AbstractState] = {instructions[0].address: entry}
    queue: deque[int] = deque([instructions[0].address])
    queued = {instructions[0].address}
    result = MethodAnalysis()
    # Keep the original generous budget for complicated UI builders.  Cyclic
    # transformation growth is bounded by ``MAX_ABSTRACT_TRANSFORM_STEPS``;
    # lowering this iteration budget would risk dropping otherwise valid paths.
    iteration_limit = max(1024, len(instructions) * 64)
    iterations = 0

    def operand_register(operand: Any) -> str:
        return _canonical_register(md, operand.reg) if operand.type == ARM64_OP_REG else ""

    def operand_width(operand: Any) -> int:
        if operand.type != ARM64_OP_REG:
            return 8
        name = md.reg_name(operand.reg)
        if name.startswith(("x", "d")) or name in {"sp", "fp", "lr"}:
            return 8
        if name.startswith(("w", "s")):
            return 4
        if name.startswith("q"):
            return 16
        if name.startswith("h"):
            return 2
        if name.startswith("b"):
            return 1
        return 8

    def read_code_integer(address: int, width: int, signed: bool) -> int | None:
        for section_base, section_data in (memory_sections or code_sections):
            offset = address - section_base
            if 0 <= offset and offset + width <= len(section_data):
                return int.from_bytes(
                    section_data[offset : offset + width],
                    "little",
                    signed=signed,
                )
        return None

    def computed_branch_targets(branch_instruction: Any) -> list[int]:
        """Decode Clang's compact ARM64 switch-table sequence.

        Typical IL2CPP output is ``adr base; ldrb/ldrh offset,[table,index];
        add base,base,offset,lsl #2; br base``.  Treat only targets that land on
        decoded instructions inside the current method as successors.
        """

        if not branch_instruction.operands:
            return []
        branch_register = operand_register(branch_instruction.operands[0])
        branch_index = instruction_indexes.get(branch_instruction.address, -1)
        window = instructions[max(0, branch_index - 16) : branch_index]
        add_instruction = next(
            (
                item
                for item in reversed(window)
                if item.mnemonic == "add"
                and len(item.operands) >= 3
                and operand_register(item.operands[0]) == branch_register
                and operand_register(item.operands[1]) == branch_register
                and item.operands[2].type == ARM64_OP_REG
            ),
            None,
        )
        if add_instruction is None:
            return []
        offset_register = operand_register(add_instruction.operands[2])
        shift = int(getattr(getattr(add_instruction.operands[2], "shift", None), "value", 0) or 0)
        load_instruction = next(
            (
                item
                for item in reversed(window)
                if item.mnemonic in {"ldrb", "ldrsb", "ldrh", "ldrsh", "ldr", "ldrsw"}
                and len(item.operands) >= 2
                and operand_register(item.operands[0]) == offset_register
                and item.operands[1].type == ARM64_OP_MEM
                and item.operands[1].mem.index
            ),
            None,
        )
        if load_instruction is None:
            return []
        table_register = _canonical_register(md, load_instruction.operands[1].mem.base)
        index_register = _canonical_register(md, load_instruction.operands[1].mem.index)

        branch_base = next(
            (
                int(item.operands[1].imm)
                for item in reversed(window)
                if item.mnemonic == "adr"
                and len(item.operands) >= 2
                and operand_register(item.operands[0]) == branch_register
                and item.operands[1].type == ARM64_OP_IMM
            ),
            None,
        )
        table_base: int | None = None
        for index, item in enumerate(window):
            if (
                item.mnemonic == "adrp"
                and len(item.operands) >= 2
                and operand_register(item.operands[0]) == table_register
                and item.operands[1].type == ARM64_OP_IMM
            ):
                candidate = int(item.operands[1].imm)
                for following in window[index + 1 :]:
                    if (
                        following.address >= load_instruction.address
                        or following.mnemonic != "add"
                        or len(following.operands) < 3
                        or operand_register(following.operands[0]) != table_register
                        or operand_register(following.operands[1]) != table_register
                        or following.operands[2].type != ARM64_OP_IMM
                    ):
                        continue
                    candidate += int(following.operands[2].imm)
                table_base = candidate
        if branch_base is None or table_base is None:
            return []

        maximum_index: int | None = None
        for item in reversed(window):
            if item.address >= load_instruction.address:
                continue
            if (
                item.mnemonic == "cmp"
                and len(item.operands) >= 2
                and operand_register(item.operands[0]) == index_register
                and item.operands[1].type == ARM64_OP_IMM
            ):
                maximum_index = int(item.operands[1].imm)
                break
            if (item.mnemonic == "mov" and len(item.operands) >= 2
                    and operand_register(item.operands[0]) == index_register
                    and item.operands[1].type == ARM64_OP_REG):
                # Clang may compare w22, then use a copied w8 as table index.
                index_register = operand_register(item.operands[1])
                continue
            _reads, writes = item.regs_access()
            if any(_canonical_register(md, register) == index_register for register in writes):
                # Arithmetic/clobbers need their own range proof. Do not reuse
                # an older comparison for a different index value.
                break
        if maximum_index is None or maximum_index < 0 or maximum_index > 4095:
            return []

        mnemonic = load_instruction.mnemonic
        width = 1 if mnemonic in {"ldrb", "ldrsb"} else 2 if mnemonic in {"ldrh", "ldrsh"} else 4
        signed = mnemonic in {"ldrsb", "ldrsh", "ldrsw"}
        targets: list[int] = []
        for table_index in range(maximum_index + 1):
            offset = read_code_integer(table_base + table_index * width, width, signed)
            if offset is None:
                continue
            target = branch_base + (offset << shift)
            if method.address <= target < method.end and target in instruction_map:
                targets.append(target)
        return list(dict.fromkeys(targets))

    def indexed_literal_table_values(instruction: Any) -> set[AbstractValue]:
        """Resolve a bounded native pointer table indexed by a copied enum/int.

        IL2CPP emits helpers such as ``ShowName`` as ``cmp index, #N`` followed
        by ``ldr result, [table, index, lsl #3]``.  These are immutable native
        pointer tables, not managed collections; each relocation target is a
        concrete string literal cell.
        """
        if len(instruction.operands) < 2 or instruction.operands[1].type != ARM64_OP_MEM:
            return set()
        memory = instruction.operands[1].mem
        if not memory.index:
            return set()
        base_register = _canonical_register(md, memory.base)
        index_register = _canonical_register(md, memory.index)
        instruction_index = instruction_indexes.get(instruction.address, -1)
        window = instructions[max(0, instruction_index - 20):instruction_index]
        maximum_index: int | None = None
        for item in reversed(window):
            if (item.mnemonic == "cmp" and len(item.operands) >= 2
                    and operand_register(item.operands[0]) == index_register
                    and item.operands[1].type == ARM64_OP_IMM):
                maximum_index = int(item.operands[1].imm)
                break
            if (item.mnemonic == "mov" and len(item.operands) >= 2
                    and operand_register(item.operands[0]) == index_register
                    and item.operands[1].type == ARM64_OP_REG):
                index_register = operand_register(item.operands[1])
                continue
            _reads, writes = item.regs_access()
            if any(_canonical_register(md, register) == index_register for register in writes):
                break
        if maximum_index is None or not 0 <= maximum_index <= 4095:
            return set()
        shift = int(getattr(getattr(instruction.operands[1], "shift", None), "value", 0) or 0)
        stride = 1 << shift
        if stride not in {4, 8}:
            return set()
        base_addresses = {
            value.source for value in _state_values(state, base_register)
            if value.kind == "address"
        }
        values: set[AbstractValue] = set()
        for base_address in base_addresses:
            for table_index in range(maximum_index + 1):
                target = pointer_slots.get(base_address + table_index * stride)
                if target in literal_cells:
                    values.add(AbstractValue("cell", target, instruction.address))
                    result.referenced_literals.add(target)
        return values

    def memory_offsets(instruction: Any, memory_operand_index: int) -> tuple[int, int | None]:
        memory = instruction.operands[memory_operand_index].mem
        post_index = bool(getattr(instruction, "post_index", False))
        writeback = bool(getattr(instruction, "writeback", False))
        access_displacement = 0 if post_index else int(memory.disp)
        if not writeback:
            return access_displacement, None
        if post_index:
            trailing = instruction.operands[memory_operand_index + 1 :]
            immediate = next(
                (int(operand.imm) for operand in trailing if operand.type == ARM64_OP_IMM),
                None,
            )
            return access_displacement, immediate
        return access_displacement, int(memory.disp)

    def load_memory_values(
        state: AbstractState,
        base_register: str,
        displacement: int,
    ) -> frozenset[AbstractValue]:
        values: set[AbstractValue] = set()
        if base_register == "sp":
            values.update(state.stack.get(state.sp_offset + displacement, frozenset()))
            return frozenset(values)
        for value in _state_values(state, base_register):
            if value.kind == "address":
                target = value.source + displacement
                cell = slot_to_cell.get(target)
                if cell is not None:
                    values.add(AbstractValue("cell", cell, pc))
                    result.referenced_literals.add(cell)
                elif target in literal_cells:
                    values.add(AbstractValue("exact", target, pc))
                    result.referenced_literals.add(target)
                elif target in pointer_slots:
                    pointer_target = pointer_slots[target]
                    if pointer_target in metadata_method_targets:
                        values.add(AbstractValue("metadata_method_cell", pointer_target, pc))
                    else:
                        values.add(AbstractValue("pointer_slot", target, pc))
            elif value.kind == "cell" and displacement == 0:
                values.add(AbstractValue("exact", value.source, value.origin))
                result.referenced_literals.add(value.source)
            elif value.kind == "metadata_method_cell" and displacement == 0:
                method_target = metadata_method_targets.get(value.source)
                if method_target is not None:
                    values.add(AbstractValue("method_pointer", method_target, pc))
            elif value.kind == "pointer_slot":
                values.add(AbstractValue("memory_path", value.source, pc, (f"{displacement:X}",)))
            elif value.kind == "memory_path":
                values.add(
                    AbstractValue(
                        "memory_path",
                        value.source,
                        pc,
                        value.transforms + (f"{displacement:X}",),
                    )
                )
            elif value.kind == "stack_address":
                values.update(state.stack.get(value.source + displacement, frozenset()))
            elif value.kind in {"object_param", "object_this", "call_result", "typed_carrier_return", "typed_carrier_object"}:
                values.add(
                    AbstractValue(
                        value.kind,
                        value.source,
                        pc,
                        value.transforms + (f"{displacement:X}",),
                    )
                )
            elif value.kind == "return_object":
                component_type = return_object_display_fields.get(value.source, {}).get(
                    displacement
                )
                if component_type:
                    values.add(
                        AbstractValue(
                            "display_receiver",
                            displacement,
                            pc,
                            (component_type, "factory return", f"{displacement:X}"),
                        )
                    )
            elif value.kind == "this" and display_fields:
                # ``this`` can have been moved through a pre/post-indexed
                # store/load sequence.  Keep its accumulated offset so
                # ``str ..., [this, #0x48]!`` followed by
                # ``ldur ..., [this, #-0x18]`` still resolves field 0x30.
                component_type = display_fields.get(value.source + displacement)
                if component_type:
                    values.add(
                        AbstractValue(
                            "display_receiver",
                            value.source + displacement,
                            pc,
                            (component_type,),
                        )
                    )
                if nested_display_fields:
                    first_offset = value.source + displacement
                    matching_paths = [
                        path for path in nested_display_fields if path[0] == first_offset
                    ]
                    if any(len(path) > 1 for path in matching_paths):
                        values.add(
                            AbstractValue(
                                "object_receiver",
                                first_offset,
                                pc,
                                (f"{first_offset:X}",),
                            )
                        )
            elif value.kind == "this" and nested_display_fields:
                first_offset = value.source + displacement
                matching_paths = [
                    path for path in nested_display_fields if path[0] == first_offset
                ]
                if any(len(path) > 1 for path in matching_paths):
                    values.add(
                        AbstractValue(
                            "object_receiver",
                            first_offset,
                            pc,
                            (f"{first_offset:X}",),
                        )
                    )
            elif value.kind == "object_receiver" and nested_display_fields:
                current_path = tuple(int(item, 16) for item in value.transforms)
                next_path = current_path + (displacement,)
                component_type = nested_display_fields.get(next_path)
                if component_type:
                    values.add(
                        AbstractValue(
                            "display_receiver",
                            hash(next_path),
                            pc,
                            (component_type, *(f"{item:X}" for item in next_path)),
                        )
                    )
                elif any(
                    path[: len(next_path)] == next_path and len(path) > len(next_path)
                    for path in nested_display_fields
                ):
                    values.add(
                        AbstractValue(
                            "object_receiver",
                            value.source,
                            pc,
                            tuple(f"{item:X}" for item in next_path),
                        )
                    )
            elif value.kind == "display_receiver" and displacement == 0:
                # A C++ object starts with its klass/vtable pointer.  Keeping
                # the receiver identity lets the later ``ldr [vtable,#slot]``
                # prove that a BLR is specifically the text property setter.
                values.add(
                    AbstractValue(
                        "display_vtable",
                        value.source,
                        pc,
                        value.transforms,
                    )
                )
            elif value.kind == "display_vtable":
                values.add(
                    AbstractValue(
                        "display_virtual_function",
                        value.source,
                        pc,
                        value.transforms + (f"{displacement:X}",),
                    )
                )
        return frozenset(values)

    def store_memory_values(
        state: AbstractState,
        base_register: str,
        displacement: int,
        values: frozenset[AbstractValue],
    ) -> None:
        if base_register == "sp":
            state.stack[state.sp_offset + displacement] = values
            return
        for value in _state_values(state, base_register):
            if value.kind == "stack_address":
                state.stack[value.source + displacement] = values
            elif value.kind in {"pointer_slot", "memory_path"}:
                key = (
                    value.source,
                    value.transforms + (f"{displacement:X}",),
                )
                for stored in values:
                    if stored.kind in {"delegate_combined", "delegate_target"}:
                        result.delegate_subscriptions[key].add(stored.source)
                    elif stored.kind == "delegate_combined_param":
                        result.delegate_parameter_field_writes.append(
                            (
                                stored.source,
                                tuple(int(item, 16) for item in key[1]),
                                pc,
                            )
                        )
                    elif stored.kind in {"param", "derived_param"}:
                        result.static_parameter_field_writes.append(
                            (stored.source, key, pc, stored.transforms)
                        )
                    elif stored.kind in {"exact", "derived"}:
                        result.static_literal_field_writes.append((stored, key, pc))
            elif value.kind == "call_result" and string_collection_fields:
                # Allocated object storage belongs to that allocation, never to
                # the method's declaring class. Only a proven collection field
                # may later expose these slots as array elements.
                key = ("heap_field", value.source, value.transforms + (f"{displacement:X}",))
                state.container_elements[key] = _limit_values(values)
            elif value.kind in {"object_param", "object_this"}:
                field_path = tuple(
                    int(item, 16)
                    for item in value.transforms + (f"{displacement:X}",)
                )
                for stored in values:
                    if stored.kind in {"param", "derived_param", "container_param"}:
                        result.parameter_field_writes.append(
                            (stored.source, field_path, pc)
                        )
                    elif stored.kind in {"exact", "derived"}:
                        result.literal_field_writes.append((stored, field_path, pc))
                    elif stored.kind == "delegate_combined_param":
                        result.delegate_parameter_field_writes.append(
                            (stored.source, field_path, pc)
                        )
                    elif stored.kind in {"call_result", "object_param", "object_this"}:
                        # Constructors commonly fill a Dictionary/List and then
                        # persist that container on ``this``. Preserve its
                        # string contents as a field-write summary so a later
                        # method can read the same field and display an item.
                        for content in load_display_container_contents(
                            state, (stored,),
                            include_heap_fields=(value.kind == "object_this" and field_path in {(offset,) for offset in string_collection_fields}),
                        ):
                            if content.kind not in {"exact", "derived"}:
                                continue
                            result.literal_field_writes.append(
                                (
                                    AbstractValue(
                                        "derived",
                                        content.source,
                                        content.origin,
                                        content.transforms
                                        + ("index-insensitive persisted container",),
                                    ),
                                    field_path,
                                    pc,
                                )
                            )

    def event_key_for_value(
        value: AbstractValue,
        extra_path: Sequence[int] = (),
        *,
        strip_last: bool = False,
    ) -> tuple[int, tuple[str, ...]] | None:
        transforms = value.transforms[:-1] if strip_last else value.transforms
        path = transforms + tuple(f"{item:X}" for item in extra_path)
        if value.kind in {"pointer_slot", "memory_path"}:
            return value.source, path
        if value.kind == "object_this":
            root = _event_family_root(_method_object_family(method))
            return (root, path) if root is not None else None
        if value.kind == "object_param":
            parameters = _signature_parameters(method.signature)
            family = (
                _parameter_object_family(parameters[value.source])
                if 0 <= value.source < len(parameters)
                else None
            )
            root = _event_family_root(family)
            return (root, path) if root is not None else None
        return None

    def record_delegate_add(target: int, state: AbstractState) -> None:
        for delegate_register, field_path in delegate_add_sinks_by_address.get(target, ()):
            callbacks = {v.source for v in _state_values(state, delegate_register)
                         if v.kind in {"delegate_target", "delegate_combined"}}
            if not callbacks:
                continue
            callee = methods_by_address.get(target)
            for receiver in _state_values(state, "x0"):
                # For a typed instance call, the field belongs to the callee's
                # receiver type, not the view which stores that receiver.
                if receiver.kind in {"object_this", "object_param"} and callee is not None:
                    root = _event_family_root(_method_object_family(callee))
                    key = (root, tuple(f"{n:X}" for n in field_path)) if root is not None else None
                else:
                    key = event_key_for_value(receiver, field_path)
                if key is not None:
                    result.delegate_subscriptions[key].update(callbacks)

    def container_keys(
        values: Iterable[AbstractValue],
    ) -> tuple[tuple[str, int, tuple[str, ...]], ...]:
        return tuple(
            dict.fromkeys(
                (value.kind, value.source, value.transforms)
                for value in values
                if value.kind
                in {
                    "call_result",
                    "object_this",
                    "object_param",
                    "container_param",
                    "memory_path",
                    "pointer_slot",
                }
            )
        )

    def store_container_elements(
        state: AbstractState,
        base_values: Iterable[AbstractValue],
        values: Iterable[AbstractValue],
    ) -> None:
        content = {
            value
            for value in values
            if value.kind in {"exact", "derived", "param", "derived_param", "object_this", "object_param", "typed_carrier_return", "container_param"}
        }
        if not content:
            return
        for key in container_keys(base_values):
            state.container_elements[key] = _limit_values(
                state.container_elements.get(key, frozenset()) | content
            )

    def load_container_elements(
        state: AbstractState,
        base_values: Iterable[AbstractValue],
        origin: int,
    ) -> frozenset[AbstractValue]:
        result_values: set[AbstractValue] = set()
        base_values = tuple(base_values)
        for key in container_keys(base_values):
            for value in state.container_elements.get(key, frozenset()):
                result_values.add(
                    AbstractValue(
                        {
                            "exact": "probable",
                            "derived": "probable_derived",
                            "param": "derived_param",
                            "derived_param": "derived_param",
                        }.get(value.kind, value.kind),
                        value.source,
                        value.origin,
                        value.transforms + ("index-insensitive container read",),
                    )
                )
        if not result_values:
            # The container may have been populated in a constructor and
            # stored on the instance. Keep the instance-field provenance;
            # carrier discovery joins it with the producer summary.
            result_values.update(
                value
                for value in base_values
                if (value.kind in {"object_this", "object_param"} and value.transforms)
                or value.kind == "typed_carrier_return"
            )
        return _limit_values(result_values)

    def load_display_container_contents(
        state: AbstractState,
        base_values: Iterable[AbstractValue],
        *,
        include_heap_fields: bool = False,
    ) -> frozenset[AbstractValue]:
        base_values = tuple(base_values)
        values: set[AbstractValue] = set()
        for key in container_keys(base_values):
            values.update(state.container_elements.get(key, frozenset()))
            if include_heap_fields and key[0] == "call_result":
                for (kind, source, path), contents in state.container_elements.items():
                    if kind == "heap_field" and source == key[1] and len(path) == 1 and int(path[0], 16) >= 0x20:
                        values.update(contents)
        values.update(value for value in base_values if value.kind == "container_param")
        if not values:
            values.update(value for value in base_values if
                          (value.kind in {"object_this", "object_param"} and value.transforms)
                          or value.kind == "typed_carrier_return")
        return _limit_values(values)

    def apply_memory_writeback(
        state: AbstractState,
        base_register: str,
        displacement: int | None,
    ) -> None:
        if displacement is None:
            return
        if base_register == "sp":
            state.sp_offset += displacement
            return
        updated: set[AbstractValue] = set()
        for value in _state_values(state, base_register):
            if value.kind == "object_this":
                updated.add(AbstractValue("instance_field_address", value.source, value.origin,
                                          value.transforms + (f"{displacement:X}",)))
            if value.kind in {"address", "stack_address", "this"}:
                updated.add(
                    AbstractValue(
                        value.kind,
                        value.source + displacement,
                        value.origin,
                        value.transforms,
                    )
                )
        _set_register(state, base_register, updated)

    def replace_object_aliases(
        state: AbstractState,
        object_values: frozenset[AbstractValue],
        replacement: AbstractValue,
    ) -> None:
        """Attach delegate metadata to every alias of an allocated object."""

        if not object_values:
            return
        for register, values in list(state.registers.items()):
            if values & object_values:
                _set_register(state, register, (values - object_values) | {replacement})
        for offset, values in list(state.stack.items()):
            if values & object_values:
                state.stack[offset] = _limit_values(
                    (values - object_values) | {replacement}
                )

    def boxed_enum_types(values: Iterable[AbstractValue]) -> set[tuple[int, str]]:
        """Recover an enum TypeInfo loaded into an IL2CPP boxed stack value."""

        headers: set[AbstractValue] = set()
        for value in values:
            if value.kind == "stack_address":
                headers.update(state.stack.get(value.source, frozenset()))
            elif value.kind in {"pointer_slot", "memory_path"}:
                headers.add(value)
        return {
            (value.source, enum_type_by_pointer_slot[value.source])
            for value in headers
            if value.kind in {"pointer_slot", "memory_path"}
            and value.source in enum_type_by_pointer_slot
        }

    def record_sink(callsite: int, sink: SinkSpec, values: Iterable[AbstractValue]) -> None:
        if sink.container_contents:
            values = load_display_container_contents(state, values)
        for value in values:
            if sink.argument_transform and value.kind not in {
                "object_param",
                "object_this",
                "typed_carrier_return",
                "derived_object_param",
                "derived_object_this",
                "derived_typed_carrier_return",
            }:
                transformed_kind = {
                    "exact": "derived",
                    "derived": "derived",
                    "param": "derived_param",
                    "derived_param": "derived_param",
                }.get(value.kind, value.kind)
                value = AbstractValue(
                    transformed_kind,
                    value.source,
                    value.origin,
                    value.transforms + (sink.argument_transform,),
                )
            if value.kind == "exact":
                result.referenced_literals.add(value.source)
                result.exact_hits.append(
                    {
                        "literal": value.source,
                        "evidence": _sink_evidence(method, callsite, sink, value),
                    }
                )
            elif value.kind == "derived":
                result.referenced_literals.add(value.source)
                result.derived_hits.append(
                    {
                        "literal": value.source,
                        "evidence": _sink_evidence(method, callsite, sink, value),
                    }
                )
            elif value.kind in {"probable", "probable_derived"}:
                result.referenced_literals.add(value.source)
                result.probable_hits.append(
                    {
                        "literal": value.source,
                        "evidence": _sink_evidence(method, callsite, sink, value),
                    }
                )
            elif value.kind == "param":
                result.parameter_hits.append(
                    (value.source, sink, callsite, value.transforms)
                )
            elif value.kind == "derived_param":
                result.parameter_hits.append(
                    (value.source, sink, callsite, value.transforms)
                )
            elif value.kind == "container_param":
                result.parameter_hits.append(
                    (
                        value.source,
                        sink,
                        callsite,
                        value.transforms + ("index-insensitive container enumeration",),
                    )
                )
            elif value.kind in {"object_param", "object_this", "derived_object_param", "derived_object_this"} and value.transforms:
                result.carrier_field_hits.append(
                    (
                        value.source,
                        tuple(int(item, 16) for item in value.transforms),
                        replace(sink, argument_transform="string transform on carrier field")
                        if value.kind.startswith("derived_") else sink,
                        callsite,
                    )
                )
            elif value.kind in {"typed_carrier_return", "derived_typed_carrier_return", "typed_carrier_object"}:
                result.returned_carrier_field_hits.append(
                    (value.source, tuple(int(item, 16) for item in value.transforms),
                     replace(sink, argument_transform="string transform on carrier field")
                     if value.kind.startswith("derived_") else sink, callsite)
                )
            elif value.kind in {"pointer_slot", "memory_path"}:
                result.static_field_hits.append(
                    ((value.source, value.transforms), sink, callsite)
                )
            elif value.kind == "enum_text" and value.transforms:
                semantic_transforms = tuple(value.transforms[1:])
                role = (
                    "derived"
                    if any(item != "Enum.ToString" for item in semantic_transforms)
                    else "exact"
                )
                result.enum_display_hits.append(
                    {
                        "enum_type": value.transforms[0],
                        "type_pointer_slot": _hex(value.source),
                        "role": role,
                        "evidence": _sink_evidence(method, callsite, sink, value),
                    }
                )

    while queue:
        pc = queue.popleft()
        queued.discard(pc)
        instruction = instruction_map.get(pc)
        if instruction is None:
            continue
        iterations += 1
        if iterations > iteration_limit:
            result.unresolved.append(
                {
                    "method_address": _hex(method.address),
                    "method_name": method.name,
                    "reason": "cfg_iteration_limit",
                }
            )
            break

        state = states[pc].copy()
        if pc in trace_addresses:
            result.trace_states[pc] = dict(state.registers)
        operands = instruction.operands
        mnemonic = instruction.mnemonic
        handled_register_writes = False

        if mnemonic == "adrp" and len(operands) >= 2 and operands[1].type == ARM64_OP_IMM:
            destination = operand_register(operands[0])
            _set_register(
                state,
                destination,
                {AbstractValue("address", int(operands[1].imm), pc)},
            )
            handled_register_writes = True
        elif mnemonic == "adr" and len(operands) >= 2 and operands[1].type == ARM64_OP_IMM:
            destination = operand_register(operands[0])
            _set_register(
                state,
                destination,
                {AbstractValue("address", int(operands[1].imm), pc)},
            )
            handled_register_writes = True
        elif mnemonic in {"add", "sub"} and len(operands) >= 3:
            destination = operand_register(operands[0])
            source_register = operand_register(operands[1])
            if operands[2].type == ARM64_OP_IMM:
                immediate = int(operands[2].imm)
                if mnemonic == "sub":
                    immediate = -immediate
                shift = getattr(operands[2], "shift", None)
                shift_value = int(getattr(shift, "value", 0) or 0)
                if shift_value:
                    immediate <<= shift_value
                if destination == "sp" and source_register == "sp":
                    state.sp_offset += immediate
                else:
                    values: set[AbstractValue] = set()
                    if source_register == "sp":
                        values.add(
                            AbstractValue(
                                "stack_address", state.sp_offset + immediate, pc
                            )
                        )
                    for value in _state_values(state, source_register):
                        if value.kind == "address":
                            target = value.source + immediate
                            if target in literal_cells:
                                values.add(AbstractValue("cell", target, pc))
                                result.referenced_literals.add(target)
                            else:
                                values.add(AbstractValue("address", target, pc))
                        elif value.kind in {"stack_address", "this"}:
                            values.add(
                                AbstractValue(
                                    value.kind,
                                    value.source + immediate,
                                    pc,
                                    value.transforms,
                                )
                            )
                    _set_register(state, destination, values)
                handled_register_writes = True
        elif mnemonic == "mov" and len(operands) >= 2:
            destination = operand_register(operands[0])
            source_register = operand_register(operands[1])
            if source_register == "sp":
                _set_register(
                    state,
                    destination,
                    {AbstractValue("stack_address", state.sp_offset, pc)},
                )
            else:
                _set_register(state, destination, _state_values(state, source_register))
            handled_register_writes = True
        elif mnemonic in {"csel", "csinc", "csinv", "csneg"} and len(operands) >= 3:
            destination = operand_register(operands[0])
            values = _state_values(state, operand_register(operands[1])) | _state_values(
                state, operand_register(operands[2])
            )
            _set_register(state, destination, values)
            handled_register_writes = True
        elif mnemonic in {"ldr", "ldur"} and len(operands) >= 2:
            destination = operand_register(operands[0])
            if operands[1].type == ARM64_OP_MEM:
                memory = operands[1].mem
                base_register = _canonical_register(md, memory.base)
                if memory.index:
                    values = indexed_literal_table_values(instruction)
                    if not values:
                        values = load_container_elements(
                            state, _state_values(state, base_register), pc
                        )
                    writeback_displacement = None
                else:
                    displacement, writeback_displacement = memory_offsets(instruction, 1)
                    values = load_memory_values(state, base_register, displacement)
                _set_register(state, destination, values)
                apply_memory_writeback(state, base_register, writeback_displacement)
            elif mnemonic == "ldr" and operands[1].type == ARM64_OP_IMM:
                target = int(operands[1].imm)
                cell = slot_to_cell.get(target)
                if cell is not None:
                    values = {AbstractValue("cell", cell, pc)}
                    result.referenced_literals.add(cell)
                elif target in literal_cells:
                    values = {AbstractValue("exact", target, pc)}
                    result.referenced_literals.add(target)
                elif target in pointer_slots:
                    pointer_target = pointer_slots[target]
                    if pointer_target in metadata_method_targets:
                        values = {
                            AbstractValue("metadata_method_cell", pointer_target, pc)
                        }
                    else:
                        values = {AbstractValue("pointer_slot", target, pc)}
                else:
                    values = set()
                _set_register(state, destination, values)
            else:
                _set_register(state, destination, ())
            handled_register_writes = True
        elif mnemonic in {"str", "stur"} and len(operands) >= 2 and operands[1].type == ARM64_OP_MEM:
            memory = operands[1].mem
            base_register = _canonical_register(md, memory.base)
            source_values = _state_values(state, operand_register(operands[0]))
            if memory.index:
                store_container_elements(
                    state, _state_values(state, base_register), source_values
                )
            else:
                displacement, writeback_displacement = memory_offsets(instruction, 1)
                store_memory_values(state, base_register, displacement, source_values)
                apply_memory_writeback(state, base_register, writeback_displacement)
        elif mnemonic in {"stp", "ldp"} and len(operands) >= 3 and operands[2].type == ARM64_OP_MEM:
            memory = operands[2].mem
            base_register = _canonical_register(md, memory.base)
            if memory.index:
                if mnemonic == "ldp":
                    _set_register(state, operand_register(operands[0]), ())
                    _set_register(state, operand_register(operands[1]), ())
                    handled_register_writes = True
            else:
                displacement, writeback_displacement = memory_offsets(instruction, 2)
                pair_step = operand_width(operands[0])
                if mnemonic == "stp":
                    store_memory_values(
                        state,
                        base_register,
                        displacement,
                        _state_values(state, operand_register(operands[0])),
                    )
                    store_memory_values(
                        state,
                        base_register,
                        displacement + pair_step,
                        _state_values(state, operand_register(operands[1])),
                    )
                else:
                    first_values = load_memory_values(state, base_register, displacement)
                    second_values = load_memory_values(
                        state, base_register, displacement + pair_step
                    )
                    _set_register(state, operand_register(operands[0]), first_values)
                    _set_register(state, operand_register(operands[1]), second_values)
                    handled_register_writes = True
                apply_memory_writeback(state, base_register, writeback_displacement)
            if mnemonic == "ldp":
                handled_register_writes = True
        elif mnemonic in {"bl", "blr"} or (
            mnemonic == "br" and not computed_branch_targets(instruction)
        ):
            # IL2CPP tail-dispatches virtual setters with BR after restoring
            # the stack. Apply the same receiver/vtable checks as BLR. Local
            # computed branches (e.g. switch tables) must remain CFG edges.
            target = (
                int(operands[0].imm)
                if mnemonic == "bl" and operands and operands[0].type == ARM64_OP_IMM
                else None
            )
            if target is not None:
                call_x0 = _state_values(state, "x0")
                for sink in sinks_by_address.get(target, ()):
                    record_sink(pc, sink, _state_values(state, sink.argument_register))
                record_delegate_add(target, state)

                transform = transforms_by_address.get(target)
                transformed_values: set[AbstractValue] = set()
                target_method_record = methods_by_address.get(target)
                container_operation = _container_operation(target_method_record)
                if (track_object_fields and target_method_record is not None
                        and _is_project_owned_method(target_method_record)
                        and target not in delegate_constructor_addresses
                        and target_method_record.name.rsplit("$$", 1)[-1] == ".ctor"):
                    replace_object_aliases(
                        state, call_x0,
                        AbstractValue("typed_carrier_object", target, pc),
                    )
                if container_operation and container_operation[0] == "copy":
                    # Collection conversion preserves its source provenance.
                    transformed_values.update(call_x0)
                elif container_operation and container_operation[0] == "write":
                    target_registers = _parameter_registers(
                        target_method_record.signature
                    )
                    store_container_elements(
                        state,
                        call_x0,
                        (
                            value
                            for parameter_index in container_operation[1]
                            if parameter_index < len(target_registers)
                            for register in [target_registers[parameter_index]]
                            if register is not None
                            for value in _state_values(state, register)
                        ),
                    )
                elif container_operation and container_operation[0] == "read":
                    transformed_values.update(
                        load_container_elements(state, call_x0, pc)
                    )
                elif container_operation and container_operation[0] == "enumerate":
                    enumerated_values = load_display_container_contents(state, call_x0)
                    transformed_values.update(enumerated_values)
                    # ARM64 returns Dictionary.Enumerator as a value type via
                    # the hidden x8 result buffer.  Seed the complete small
                    # result area: subsequent compiler-generated vector copies
                    # preserve the provenance until Current.Key/Value is read.
                    for destination in _state_values(state, "x8"):
                        if destination.kind != "stack_address":
                            continue
                        for offset in range(0, 64, 8):
                            stack_offset = destination.source + offset
                            state.stack[stack_offset] = _limit_values(
                                state.stack.get(stack_offset, frozenset())
                                | enumerated_values
                            )
                display_container = display_container_operations.get(target)
                if display_container and display_container[0] == "write":
                    target_registers = _parameter_registers(
                        target_method_record.signature
                    ) if target_method_record is not None else ()
                    store_container_elements(
                        state,
                        call_x0,
                        (
                            value
                            for parameter_index in display_container[1]
                            if parameter_index < len(target_registers)
                            for register in [target_registers[parameter_index]]
                            if register is not None
                            for value in _state_values(state, register)
                        ),
                    )
                elif display_container and display_container[0] == "display":
                    target_registers = _parameter_registers(
                        target_method_record.signature
                    ) if target_method_record is not None else ()
                    for parameter_index in display_container[1]:
                        if parameter_index >= len(target_registers):
                            continue
                        register = target_registers[parameter_index]
                        if register is None:
                            continue
                        container_sink = SinkSpec(
                            target,
                            target_method_record.name,
                            target_method_record.signature,
                            parameter_index,
                            register,
                            (target_method_record.name, "GUIContent.text"),
                        )
                        record_sink(
                            pc,
                            container_sink,
                            load_display_container_contents(
                                state, _state_values(state, register)
                            ),
                        )
                if transform:
                    for argument_register in transform.argument_registers:
                        for value in _state_values(state, argument_register):
                            if value.kind in {
                                "exact",
                                "derived",
                                "param",
                                "derived_param",
                                "object_param",
                                "object_this",
                                "derived_object_param",
                                "derived_object_this",
                                "typed_carrier_return",
                                "derived_typed_carrier_return",
                                "enum_text",
                            }:
                                if value.kind in {"object_param", "object_this", "typed_carrier_return", "derived_object_param", "derived_object_this", "derived_typed_carrier_return"}:
                                    transformed_values.add(replace(value, kind=value.kind if value.kind.startswith("derived_") else "derived_" + value.kind))
                                    continue
                                transformed_values.add(
                                    AbstractValue(
                                        (
                                            "derived_param"
                                            if value.kind in {"param", "derived_param"}
                                            else value.kind
                                            if value.kind == "enum_text"
                                            else "derived"
                                        ),
                                        value.source,
                                        value.origin,
                                        value.transforms + (transform.name,),
                                    )
                                )
                if (
                    target_method_record is not None
                    and target_method_record.name == "System.Enum$$ToString"
                ):
                    for pointer_slot, enum_type in boxed_enum_types(call_x0):
                        transformed_values.add(
                            AbstractValue(
                                "enum_text",
                                pointer_slot,
                                pc,
                                (enum_type, "Enum.ToString"),
                            )
                        )
                # Delegate constructors take the bound target method in x2.
                # The generic type is irrelevant here: Action<string>,
                # Action<int,string>, Func<...>, and custom delegate types all
                # share the System.Delegate target/method representation.
                if target in delegate_constructor_addresses:
                    delegate_method_targets = [
                        value
                        for value in _state_values(state, "x2")
                        if value.kind == "method_pointer"
                    ]
                    for value in delegate_method_targets:
                        delegate_value = AbstractValue(
                            "delegate_target", value.source, value.origin
                        )
                        transformed_values.add(delegate_value)
                        replace_object_aliases(state, call_x0, delegate_value)
                # System.Delegate.Combine returns the accumulated delegate in
                # x0.  Store tracking below ties it to the precise delegate
                # field, rather than trusting a class or field name.
                if target in delegate_combine_addresses:
                    for value in _state_values(state, "x1"):
                        if value.kind == "delegate_target":
                            transformed_values.add(
                                AbstractValue("delegate_combined", value.source, value.origin)
                            )
                        elif value.kind == "object_param":
                            transformed_values.add(
                                AbstractValue(
                                    "delegate_combined_param",
                                    value.source,
                                    value.origin,
                                )
                            )
                combined_parameters = [v for v in _state_values(state, "x1")
                                       if v.kind == "delegate_combined_param"]
                field_addresses = [v for v in call_x0 if v.kind == "instance_field_address"]
                if combined_parameters and field_addresses and _is_native_reference_compare_exchange(code_sections, target, md):
                    for address in field_addresses:
                        path = tuple(int(part, 16) for part in address.transforms)
                        for value in combined_parameters:
                            result.delegate_parameter_field_writes.append((value.source, path, pc))
                for returned in return_summaries_by_address.get(target, ()):
                    if returned.kind in {"exact", "derived", "enum_text"}:
                        transformed_values.add(
                            AbstractValue(
                                returned.kind,
                                returned.source,
                                returned.origin,
                                returned.transforms,
                            )
                        )
                    elif returned.kind in {"param", "derived_param"}:
                        target_method = methods_by_address.get(target)
                        if target_method is None:
                            continue
                        registers = _parameter_registers(target_method.signature)
                        if returned.source >= len(registers):
                            continue
                        source_register = registers[returned.source]
                        if source_register is None:
                            continue
                        for caller_value in _state_values(state, source_register):
                            if caller_value.kind not in {
                                "exact",
                                "derived",
                                "param",
                                "derived_param",
                            }:
                                continue
                            transformed_values.add(
                                AbstractValue(
                                    (
                                        "derived"
                                        if caller_value.kind in {"exact", "derived"}
                                        else "derived_param"
                                    ),
                                    caller_value.source,
                                    caller_value.origin,
                                    caller_value.transforms + returned.transforms,
                                )
                            )
                returned_component = (
                    _return_display_component_type(target_method_record.signature)
                    if target_method_record is not None
                    else None
                )
                if returned_component:
                    transformed_values.add(
                        AbstractValue(
                            "display_receiver",
                            pc,
                            pc,
                            (returned_component, "return"),
                        )
                    )
                elif _is_erased_get_component_factory(target_method_record):
                    typed_sources = {
                        value.source
                        for value in _state_values(state, "x1")
                        if value.kind in {"pointer_slot", "memory_path"}
                        and value.source in return_object_display_fields
                    }
                    if typed_sources:
                        transformed_values.update(
                            AbstractValue("return_object", source, pc)
                            for source in typed_sources
                        )
                    else:
                        transformed_values.add(
                            AbstractValue(
                                "display_receiver",
                                pc,
                                pc,
                                ("erased GetComponent<T>", "return"),
                            )
                        )
                elif target in return_object_display_fields:
                    transformed_values.add(AbstractValue("return_object", target, pc))
                if track_object_fields and target_method_record is not None and not container_operation:
                    return_type = target_method_record.signature.split("(", 1)[0].strip().rsplit(" ", 1)[0].strip()
                    if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*_o\s*\*", return_type) and not _is_string_parameter(return_type):
                        transformed_values.add(AbstractValue("typed_carrier_return", target, pc))
                        transformed_values.add(AbstractValue("call_result", pc, pc))
                _clear_caller_saved(state)
                # Unknown native/IL2CPP allocators still produce a stable
                # abstract object identity.  Real delegate construction moves
                # this result to a callee-saved register before invoking the
                # void .ctor; retaining it is required to follow that alias.
                if not transformed_values and target not in delegate_constructor_addresses:
                    transformed_values.update(
                        value
                        for value in call_x0
                        if value.kind in {"delegate_combined", "delegate_target", "delegate_combined_param"}
                    )
                    transformed_values.add(AbstractValue("call_result", pc, pc))
                _set_register(state, "x0", transformed_values)
            else:
                # A delegate Invoke is an indirect ``blr``.  Recognise the
                # standard object layout only when the same object provides
                # the invocation target (x0/+0x40), method info (x3/+0x28),
                # and function pointer (+0x18).  This prevents arbitrary
                # virtual calls from being treated as events.
                branch_register = operand_register(operands[0]) if operands else ""
                function_values = _state_values(state, branch_register)
                delegate_keys = {
                    key
                    for value in function_values
                    if value.kind in {"memory_path", "object_this", "object_param"}
                    and value.transforms
                    and value.transforms[-1] == "18"
                    for key in [event_key_for_value(value, strip_last=True)]
                    if key is not None
                }
                target_keys = {
                    key
                    for value in _state_values(state, "x0")
                    if value.kind in {"memory_path", "object_this", "object_param"}
                    and value.transforms
                    and value.transforms[-1] == "40"
                    for key in [event_key_for_value(value, strip_last=True)]
                    if key is not None
                }
                # MethodInfo is the final managed argument, so its register
                # depends on the delegate arity.  Action<string> uses x2 and
                # tail-branches through x3, while wider delegates commonly
                # place MethodInfo in x3 or later.  Prove the +0x28 layout in
                # any argument register instead of hard-coding x3.
                method_info_keys = {
                    key
                    for register in ("x1", "x2", "x3", "x4", "x5", "x6", "x7")
                    for value in _state_values(state, register)
                    if value.kind in {"memory_path", "object_this", "object_param"}
                    and value.transforms
                    and value.transforms[-1] == "28"
                    for key in [event_key_for_value(value, strip_last=True)]
                    if key is not None
                }
                proven_delegate_keys = delegate_keys & target_keys & method_info_keys
                delegate_values = {
                    register: tuple(
                        value
                        for value in _state_values(state, register)
                        if value.kind
                        in {
                            "exact",
                            "derived",
                            "probable",
                            "probable_derived",
                            "param",
                            "derived_param",
                        }
                    )
                    for register in ("x1", "x2", "x3", "x4", "x5", "x6", "x7")
                }
                delegate_values = {
                    register: values
                    for register, values in delegate_values.items()
                    if values
                }
                if proven_delegate_keys and delegate_values:
                    for key in proven_delegate_keys:
                        result.delegate_invocations.append((key, pc, delegate_values))
                elif delegate_values:
                    result.unresolved.append(
                        {
                            "method_address": _hex(method.address),
                            "method_name": method.name,
                            "callsite": _hex(pc),
                            "reason": "delegate_layout_unproven",
                            "function_key_count": len(delegate_keys),
                            "target_key_count": len(target_keys),
                            "method_info_key_count": len(method_info_keys),
                            "function_value_kinds": sorted(
                                {value.kind for value in function_values}
                            ),
                            "target_value_kinds": sorted(
                                {value.kind for value in _state_values(state, "x0")}
                            ),
                            "method_info_value_kinds": sorted(
                                {value.kind for value in _state_values(state, "x3")}
                            ),
                        }
                    )
                receivers = [
                    value
                    for value in _state_values(state, "x0")
                    if value.kind == "display_receiver" and value.transforms
                ]
                virtual_functions = [
                    value
                    for value in function_values
                    if value.kind == "display_virtual_function"
                    and value.transforms
                ]
                virtual_method_infos = [
                    value
                    for value in _state_values(state, "x2")
                    if value.kind == "display_virtual_function" and value.transforms
                ]

                def has_vtable_entry_layout(function: AbstractValue) -> bool:
                    try:
                        function_offset = int(function.transforms[-1], 16)
                    except ValueError:
                        return False
                    return any(
                        method_info.source == function.source
                        and method_info.transforms[:-1] == function.transforms[:-1]
                        and int(method_info.transforms[-1], 16) == function_offset + 8
                        for method_info in virtual_method_infos
                    )

                proven_pairs: list[tuple[AbstractValue, AbstractValue]] = []
                probable_pairs: list[tuple[AbstractValue, AbstractValue]] = []
                for receiver in receivers:
                    component_type = receiver.transforms[0]
                    configured_slots = virtual_text_slots_by_component.get(
                        component_type, frozenset()
                    )
                    for function in virtual_functions:
                        if (
                            function.source != receiver.source
                            or function.transforms[:-1] != receiver.transforms
                        ):
                            continue
                        try:
                            function_offset = int(function.transforms[-1], 16)
                        except ValueError:
                            continue
                        layout_proven = has_vtable_entry_layout(function)
                        if configured_slots:
                            target = (
                                proven_pairs
                                if layout_proven and function_offset in configured_slots
                                else probable_pairs if layout_proven else []
                            )
                        else:
                            # Low-level callers may not supply dump.cs.  Keep
                            # the previous known Unity/Text slots operational,
                            # while all other ABI-only matches remain probable.
                            target = (
                                proven_pairs
                                if function_offset in {0x558, 0x5E8}
                                else probable_pairs if layout_proven else []
                            )
                        target.append((receiver, function))

                for receiver, _function in proven_pairs:
                        component_type = receiver.transforms[0]
                        virtual_sink = SinkSpec(
                            address=0,
                            name=f"{component_type}$$virtual_set_text",
                            signature="virtual component text setter (receiver proven from dump.cs)",
                            argument_index=1,
                            argument_register="x1",
                            path=("virtual component text setter",),
                        )
                        record_sink(pc, virtual_sink, _state_values(state, "x1"))
                for receiver, _function in probable_pairs:
                    component_type = receiver.transforms[0]
                    virtual_sink = SinkSpec(
                        address=0,
                        name=f"{component_type}$$probable_virtual_text_call",
                        signature="virtual component call (vtable slot not set_text-verified)",
                        argument_index=1,
                        argument_register="x1",
                        path=("unverified virtual component string call",),
                    )
                    probable_values = {
                        AbstractValue(
                            "probable" if value.kind == "exact" else "probable_derived",
                            value.source,
                            value.origin,
                            value.transforms + ("unverified virtual slot",),
                        )
                        for value in _state_values(state, "x1")
                        if value.kind in {"exact", "derived"}
                    }
                    record_sink(pc, virtual_sink, probable_values)
                interesting = [
                    value
                    for value in _state_values(state, "x1")
                    if value.kind in {"exact", "derived", "param"}
                ]
                if interesting and not proven_pairs and not probable_pairs and not proven_delegate_keys:
                    result.unresolved.append(
                        {
                            "method_address": _hex(method.address),
                            "method_name": method.name,
                            "callsite": _hex(pc),
                            "reason": "virtual_receiver_type_unproven",
                            "sources": [
                                {
                                    "kind": value.kind,
                                    "source": (
                                        _hex(value.source)
                                        if value.kind != "param"
                                        else f"parameter:{value.source}"
                                    ),
                                }
                                for value in interesting[:MAX_UNRESOLVED_SOURCE_SAMPLES]
                            ],
                            "source_count": len(interesting),
                        }
                    )
                carrier_return_values = {
                    value
                    for value in _state_values(state, "x0")
                    if value.kind in {"object_this", "object_param"}
                    and value.transforms
                }
                _clear_caller_saved(state)
                if (
                    carrier_return_values
                    and not proven_delegate_keys
                    and not proven_pairs
                    and not probable_pairs
                ):
                    # Interface dispatch (blr) often hides a shared-generic
                    # Dictionary.get_Item target. Preserve only the receiver's
                    # instance-field provenance. It becomes evidence only if
                    # the returned value subsequently reaches a display sink.
                    _set_register(state, "x0", carrier_return_values)
            handled_register_writes = True

        if not handled_register_writes and mnemonic not in {
            "str",
            "stur",
            "stp",
            # Capstone exposes alias operands for CMP/CMN/TST as if the source
            # GPR were written.  Architecturally only NZCV changes.
            "cmp",
            "cmn",
            "tst",
        }:
            try:
                _, written = instruction.regs_access()
            except Exception:  # pragma: no cover - defensive for capstone edge cases
                written = ()
            for register_id in written:
                register = _canonical_register(md, register_id)
                if register.startswith("x") and register != "xzr":
                    state.registers.pop(register, None)

        next_pc = pc + instruction.size
        successors: list[int]
        if mnemonic == "ret":
            result.return_values.update(
                value
                for value in _state_values(state, "x0")
                if value.kind in {
                    "exact",
                    "derived",
                    "param",
                    "derived_param",
                    "enum_text",
                    "object_this",
                    "object_param",
                }
            )
            successors = []
        elif mnemonic == "br":
            successors = computed_branch_targets(instruction)
            if not successors:
                result.unresolved.append(
                    {
                        "method_address": _hex(method.address),
                        "method_name": method.name,
                        "callsite": _hex(pc),
                        "reason": "computed_branch_unresolved",
                    }
                )
        elif mnemonic == "b" and operands and operands[0].type == ARM64_OP_IMM:
            target = int(operands[0].imm)
            if target in instruction_map:
                successors = [target]
            else:
                record_delegate_add(target, state)
                for sink in sinks_by_address.get(target, ()):
                    record_sink(pc, sink, _state_values(state, sink.argument_register))
                target_method = methods_by_address.get(target)
                target_registers = (
                    _parameter_registers(target_method.signature)
                    if target_method is not None
                    else ()
                )
                for returned in return_summaries_by_address.get(target, ()):
                    if returned.kind in {"exact", "derived", "enum_text"}:
                        result.return_values.add(returned)
                        continue
                    if (
                        returned.kind not in {"param", "derived_param"}
                        or returned.source >= len(target_registers)
                    ):
                        continue
                    source_register = target_registers[returned.source]
                    if source_register is None:
                        continue
                    for caller_value in _state_values(state, source_register):
                        if caller_value.kind not in {
                            "exact", "derived", "param", "derived_param"
                        }:
                            continue
                        result.return_values.add(
                            AbstractValue(
                                (
                                    "derived"
                                    if caller_value.kind in {"exact", "derived"}
                                    else "derived_param"
                                ),
                                caller_value.source,
                                caller_value.origin,
                                caller_value.transforms + returned.transforms,
                            )
                        )
                successors = []
        elif mnemonic.startswith("b.") or mnemonic in {"cbz", "cbnz", "tbz", "tbnz"}:
            branch_target = next(
                (int(operand.imm) for operand in reversed(operands) if operand.type == ARM64_OP_IMM),
                None,
            )
            successors = [next_pc]
            if branch_target is not None:
                successors.append(branch_target)
        else:
            successors = [next_pc]

        for successor in successors:
            if successor not in instruction_map:
                continue
            previous = states.get(successor)
            if previous is None:
                states[successor] = state.copy()
                changed = True
            else:
                def report_truncation(
                    location: str, candidate_count: int, kept_count: int
                ) -> None:
                    result.unresolved.append(
                        {
                            "method_address": _hex(method.address),
                            "method_name": method.name,
                            "callsite": _hex(pc),
                            "reason": "abstract_value_truncated",
                            "location": location,
                            "candidates": candidate_count,
                            "kept": kept_count,
                            "discarded": candidate_count - kept_count,
                        }
                    )

                states[successor], changed = _merge_state(
                    previous,
                    state,
                    on_truncate=report_truncation,
                )
            if changed and successor not in queued:
                queue.append(successor)
                queued.add(successor)

    result.exact_hits = _dedupe_dicts(result.exact_hits)
    result.derived_hits = _dedupe_dicts(result.derived_hits)
    result.enum_display_hits = _dedupe_dicts(result.enum_display_hits)
    result.unresolved = _dedupe_dicts(result.unresolved)
    return result


def _discover_carrier_field_sinks(
    *,
    methods: Mapping[int, MethodRecord],
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None,
    literal_cells: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    base_sinks: Sequence[SinkSpec],
    transforms_by_address: Mapping[int, TransformSpec],
    display_fields_by_method: Mapping[int, Mapping[int, str]],
    nested_display_fields_by_method: Mapping[int, Mapping[tuple[int, ...], str]],
    virtual_text_slots_by_component: Mapping[str, frozenset[int]],
    display_container_operations: Mapping[int, tuple[str, tuple[int, ...]]],
    disassembler: Any,
    instruction_cache: dict[int, tuple[Any, ...]],
    consumer_methods: set[int] | None = None,
    carrier_field_types: Mapping[tuple[str, ...], Mapping[int, tuple[str, ...]]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> tuple[
    list[SinkSpec],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    """Prove strings persisted in a UI model field before later display.

    Many games construct a popup model through a Builder, enqueue it, then
    read its string fields in a UI consumer.  There is no direct call edge
    between the write and read.  We connect the two only when a consumer proves
    that a field of a concrete object parameter reaches a display setter, and a
    project method in the same top-level model family writes a string parameter
    to that exact field offset.
    """

    sinks_by_address: dict[int, list[SinkSpec]] = defaultdict(list)
    for sink in base_sinks:
        sinks_by_address[sink.address].append(sink)

    displayed_paths: dict[
        tuple[str, ...], dict[tuple[int, ...], list[SinkSpec]]
    ] = defaultdict(
        lambda: defaultdict(list)
    )
    unresolved: list[dict[str, Any]] = []
    ordered_methods = sorted(methods.values(), key=lambda item: item.address)
    consumer_count = 0
    def add_displayed_path(family, field_path, sink):
        displayed_paths[family][field_path].append(sink)
        # A UI field may hold the model until a later refresh. Walk only
        # dump-proven reference types; matching numeric offsets alone is unsafe.
        remaining = field_path
        while carrier_field_types and len(remaining) > 1:
            nested_family = carrier_field_types.get(family, {}).get(remaining[0])
            if nested_family is None:
                break
            family = nested_family
            remaining = remaining[1:]
            displayed_paths[family][remaining].append(sink)
    for method in ordered_methods:
        fields = display_fields_by_method.get(method.address)
        nested_fields = nested_display_fields_by_method.get(method.address)
        parameters = _signature_parameters(method.signature)
        if not _is_project_owned_method(method):
            continue
        if not (fields or nested_fields or (consumer_methods and method.address in consumer_methods)):
            continue
        if not any(_is_object_pointer_parameter(item) for item in parameters):
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literal_cells,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            display_fields=fields,
            nested_display_fields=nested_fields,
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            display_container_operations=display_container_operations,
            methods_by_address=methods,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            track_object_fields=True,
            string_collection_fields=frozenset(
                offset for offset, field_type in (carrier_field_types or {}).get(_method_object_family(method), {}).items()
                if field_type == ("__string_collection__",)
            ),
        )
        consumer_count += 1
        if progress_callback and consumer_count % 250 == 0:
            progress_callback(f"载体字段：已分析显示读取方法 {consumer_count}")
        unresolved.extend(analysis.unresolved)
        for parameter_index, field_path, downstream, _callsite in analysis.carrier_field_hits:
            if not field_path or parameter_index >= len(parameters):
                continue
            family = (
                _method_object_family(method)
                if parameter_index == 0
                else _parameter_object_family(parameters[parameter_index])
            )
            if family:
                add_displayed_path(family, field_path, downstream)

        for target, field_path, downstream, callsite in analysis.returned_carrier_field_hits:
            producer = methods.get(target)
            if producer is None:
                continue
            if not field_path:
                # A returned list can feed get_Item before the text is displayed.
                # Resolve the getter's actual returned instance field rather than
                # identifying a list solely by its generic type.
                getter = _analyse_method(
                    method=producer, code_sections=code_sections,
                    memory_sections=memory_sections, literal_cells=literal_cells,
                    slot_to_cell=slot_to_cell, sinks_by_address={},
                    transforms_by_address=transforms_by_address,
                    initialise_parameters=True, track_object_fields=True,
                    methods_by_address=methods, disassembler=disassembler,
                    instruction_cache=instruction_cache,
                )
                for returned in getter.return_values:
                    if returned.kind == "object_this" and returned.transforms:
                        family = _method_object_family(producer)
                        if family:
                            add_displayed_path(family, tuple(int(v, 16) for v in returned.transforms),
                                               replace(downstream, path=(producer.name, *downstream.path)))
                continue
            is_constructor = producer.name.rsplit("$$", 1)[-1] == ".ctor"
            return_type = producer.signature.split("(", 1)[0].strip().rsplit(" ", 1)[0].strip()
            family = _method_object_family(producer) if is_constructor else _parameter_object_family(return_type)
            if family:
                # The concrete return type joins a later UI read to writes on
                # that same model. Offsets alone must never connect unrelated models.
                add_displayed_path(family, field_path,
                    SinkSpec(
                        address=downstream.address, name=downstream.name,
                        signature=downstream.signature,
                        argument_index=downstream.argument_index,
                        argument_register=downstream.argument_register,
                        path=(f"{producer.name} constructs {'.'.join(family)}" if is_constructor
                              else f"{producer.name} returns {return_type}",
                              f"{method.name} reads at {_hex(callsite)}", *downstream.path),
                        wrapper_depth=downstream.wrapper_depth,
                        argument_transform=downstream.argument_transform,
                    )
                )

    carrier_sinks: list[SinkSpec] = []
    exact_hits: list[dict[str, Any]] = []
    derived_hits: list[dict[str, Any]] = []
    displayed_families = set(displayed_paths)
    producer_count = 0
    for method in ordered_methods:
        if not _is_project_owned_method(method):
            continue
        method_family = _method_object_family(method)
        if not method_family:
            continue
        # A matching display family is necessarily a prefix of the method
        # family.  Checking those few prefixes is equivalent to scanning every
        # display family, without O(method_count * family_count) behaviour.
        matching_families = [
            method_family[:length]
            for length in range(1, len(method_family) + 1)
            if method_family[:length] in displayed_families
            and (length == len(method_family)
                 or any("builder" in part for part in method_family[length:]))
        ]
        if not matching_families:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literal_cells,
            slot_to_cell=slot_to_cell,
            sinks_by_address={},
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            methods_by_address=methods,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            track_object_fields=True,
            string_collection_fields=frozenset(
                offset for offset, field_type in (carrier_field_types or {}).get(method_family, {}).items()
                if field_type == ("__string_collection__",)
            ),
        )
        producer_count += 1
        if progress_callback and producer_count % 250 == 0:
            progress_callback(f"载体字段：已分析写入方法 {producer_count}")
        parameter_registers = _parameter_registers(method.signature)
        for parameter_index, field_path, _store_pc in analysis.parameter_field_writes:
            if parameter_index >= len(parameter_registers):
                continue
            argument_register = parameter_registers[parameter_index]
            if argument_register is None or not argument_register.startswith("x"):
                continue
            for family in matching_families:
                for displayed_path, downstream_sinks in displayed_paths[family].items():
                    if not _field_paths_compatible(field_path, displayed_path):
                        continue
                    for downstream in downstream_sinks:
                        family_name = ".".join(family)
                        path_text = ".".join(f"+0x{offset:X}" for offset in field_path)
                        spec = SinkSpec(
                            address=method.address,
                            name=method.name,
                            signature=method.signature,
                            argument_index=parameter_index,
                            argument_register=argument_register,
                            path=(
                                f"persisted {family_name} field {path_text}",
                                downstream.name,
                                *downstream.path,
                            ),
                            wrapper_depth=1,
                            argument_transform=downstream.argument_transform,
                            container_contents=parameter_index in _container_parameter_indices(method.signature),
                        )
                        if spec not in carrier_sinks:
                            carrier_sinks.append(spec)

        for value, field_path, store_pc in analysis.literal_field_writes:
            for family in matching_families:
                for displayed_path, downstream_sinks in displayed_paths[family].items():
                    if not _field_paths_compatible(field_path, displayed_path):
                        continue
                    for downstream in downstream_sinks:
                        family_name = ".".join(family)
                        path_text = ".".join(f"+0x{offset:X}" for offset in field_path)
                        evidence = {
                            "literal": value.source,
                            "evidence": {
                                "method_address": _hex(method.address),
                                "method_name": method.name,
                                "callsite": _hex(store_pc),
                                "sink_address": _hex(downstream.address),
                                "sink_name": downstream.name,
                                "sink_signature": downstream.signature,
                                "source_instruction": _hex(value.origin),
                                "path": [
                                    f"persisted {family_name} field {path_text}",
                                    downstream.name,
                                    *downstream.path,
                                ],
                                "transforms": list(value.transforms) + ([downstream.argument_transform] if downstream.argument_transform else []),
                            },
                        }
                        target = exact_hits if value.kind == "exact" and not downstream.argument_transform else derived_hits
                        target.append(evidence)
    return (
        carrier_sinks,
        _dedupe_dicts(exact_hits),
        _dedupe_dicts(derived_hits),
        _dedupe_dicts(unresolved),
    )


def _discover_static_field_sinks(
    *,
    methods: Mapping[int, MethodRecord],
    consumer_methods: set[int],
    producer_methods: set[int],
    references_by_method: Mapping[int, set[int]],
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None,
    literal_cells: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    sinks_by_address: Mapping[int, Sequence[SinkSpec]],
    transforms_by_address: Mapping[int, TransformSpec],
    pointer_slots: Mapping[int, int],
    metadata_method_targets: Mapping[int, int],
    display_fields_by_method: Mapping[int, Mapping[int, str]],
    nested_display_fields_by_method: Mapping[int, Mapping[tuple[int, ...], str]],
    virtual_text_slots_by_component: Mapping[str, frozenset[int]],
    display_container_operations: Mapping[int, tuple[str, tuple[int, ...]]],
    return_summaries_by_address: Mapping[int, Sequence[AbstractValue]],
    disassembler: Any,
    instruction_cache: dict[int, tuple[Any, ...]],
) -> tuple[list[SinkSpec], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Connect global/static string fields to later proven display reads."""

    displayed: dict[tuple[int, tuple[str, ...]], list[SinkSpec]] = defaultdict(list)
    unresolved: list[dict[str, Any]] = []
    for method_address in sorted(consumer_methods):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literal_cells,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=False,
            display_fields=display_fields_by_method.get(method_address),
            nested_display_fields=nested_display_fields_by_method.get(method_address),
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            display_container_operations=display_container_operations,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
        )
        unresolved.extend(analysis.unresolved)
        for key, sink, _callsite in analysis.static_field_hits:
            displayed[key].append(sink)

    static_sinks: list[SinkSpec] = []
    exact_hits: list[dict[str, Any]] = []
    derived_hits: list[dict[str, Any]] = []
    if not displayed:
        return static_sinks, exact_hits, derived_hits, _dedupe_dicts(unresolved)

    displayed_roots = {key[0] for key in displayed}
    for method_address in sorted(producer_methods):
        method = methods.get(method_address)
        if (
            method is None
            or not _is_project_owned_method(method)
            or not (references_by_method.get(method_address, set()) & displayed_roots)
        ):
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literal_cells,
            slot_to_cell=slot_to_cell,
            sinks_by_address={},
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
        )
        unresolved.extend(analysis.unresolved)
        registers = _parameter_registers(method.signature)
        for parameter_index, key, _store_pc, transforms in analysis.static_parameter_field_writes:
            if key not in displayed or parameter_index >= len(registers):
                continue
            register = registers[parameter_index]
            if register is None or not register.startswith("x"):
                continue
            for downstream in displayed[key]:
                spec = SinkSpec(
                    method.address,
                    method.name,
                    method.signature,
                    parameter_index,
                    register,
                    (f"static field {_hex(key[0])}/{'/'.join(key[1])}", *downstream.path),
                    wrapper_depth=1,
                    argument_transform=(" -> ".join(transforms) if transforms else None),
                )
                if spec not in static_sinks:
                    static_sinks.append(spec)
        for value, key, store_pc in analysis.static_literal_field_writes:
            if key not in displayed:
                continue
            for downstream in displayed[key]:
                evidence = {
                    "literal": value.source,
                    "evidence": {
                        "method_address": _hex(method.address),
                        "method_name": method.name,
                        "callsite": _hex(store_pc),
                        "sink_address": _hex(downstream.address),
                        "sink_name": downstream.name,
                        "sink_signature": downstream.signature,
                        "source_instruction": _hex(value.origin),
                        "path": [
                            f"static field {_hex(key[0])}/{'/'.join(key[1])}",
                            *downstream.path,
                        ],
                        "transforms": list(value.transforms),
                    },
                }
                (exact_hits if value.kind == "exact" else derived_hits).append(evidence)
    return (
        static_sinks,
        _dedupe_dicts(exact_hits),
        _dedupe_dicts(derived_hits),
        _dedupe_dicts(unresolved),
    )


def _discover_wrapper_sinks(
    *,
    methods: Mapping[int, MethodRecord],
    call_index: Mapping[int, set[int]],
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None,
    literal_cells: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    base_sinks: Sequence[SinkSpec],
    transforms_by_address: Mapping[int, TransformSpec],
    max_wrapper_depth: int,
    display_fields_by_method: Mapping[int, Mapping[int, str]],
    nested_display_fields_by_method: Mapping[
        int, Mapping[tuple[int, ...], str]
    ],
    virtual_text_slots_by_component: Mapping[str, frozenset[int]],
    display_container_operations: Mapping[int, tuple[str, tuple[int, ...]]],
    disassembler: Any,
    instruction_cache: dict[int, tuple[Any, ...]],
    return_summaries_by_address: Mapping[int, Sequence[AbstractValue]],
    component_factory_callers: set[int],
    return_object_display_fields: Mapping[int, Mapping[int, str]],
    pointer_slots: Mapping[int, int],
) -> tuple[dict[int, list[SinkSpec]], set[int], list[dict[str, Any]]]:
    sinks_by_address: dict[int, list[SinkSpec]] = defaultdict(list)
    for sink in base_sinks:
        sinks_by_address[sink.address].append(sink)

    wrapper_methods: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    frontier = {sink.address for sink in base_sinks}
    frontier.update(
        address
        for address, operation in display_container_operations.items()
        if operation[0] == "display"
    )
    known_wrapper_keys: set[tuple[int, int, str]] = set()

    # A project wrapper may itself end in the normal virtual Text.set_text
    # dispatch and therefore have no direct BL edge to any exported setter.
    # Summarise those methods first so their callers participate in the same
    # interprocedural fixed-point as direct wrappers.
    for method in sorted(methods.values(), key=lambda item: item.address):
        fields = display_fields_by_method.get(method.address)
        nested_fields = nested_display_fields_by_method.get(method.address)
        if (
            not (fields or nested_fields or method.address in component_factory_callers)
            or not _is_safe_wrapper_candidate(method)
            or not (
                _string_parameter_indices(method.signature)
                or _container_parameter_indices(method.signature)
            )
        ):
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literal_cells,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            display_fields=fields,
            nested_display_fields=nested_fields,
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            display_container_operations=display_container_operations,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            return_object_display_fields=return_object_display_fields,
            pointer_slots=pointer_slots,
        )
        unresolved.extend(analysis.unresolved)
        for parameter_index, downstream, _callsite, transforms in analysis.parameter_hits:
            argument_register = _parameter_registers(method.signature)[parameter_index]
            if argument_register is None or not argument_register.startswith("x"):
                continue
            spec = SinkSpec(
                address=method.address,
                name=method.name,
                signature=method.signature,
                argument_index=parameter_index,
                argument_register=argument_register,
                path=(method.name,) + downstream.path,
                wrapper_depth=1,
                argument_transform=(" -> ".join(transforms) if transforms else None),
                container_contents=(
                    "index-insensitive container enumeration" in transforms
                ),
            )
            semantic_key = (method.address, parameter_index, downstream.path[-1])
            if semantic_key not in known_wrapper_keys:
                known_wrapper_keys.add(semantic_key)
                sinks_by_address[method.address].append(spec)
                frontier.add(method.address)
                wrapper_methods.add(method.address)
    while frontier:
        callers = {
            caller
            for target in frontier
            for caller in call_index.get(target, set())
            if caller in methods
        }
        new_frontier: set[int] = set()
        for caller in sorted(callers):
            method = methods[caller]
            if not _is_safe_wrapper_candidate(method) or not _string_parameter_indices(
                method.signature
            ):
                continue
            analysis = _analyse_method(
                method=method,
                code_sections=code_sections,
                memory_sections=memory_sections,
                literal_cells=literal_cells,
                slot_to_cell=slot_to_cell,
                sinks_by_address=sinks_by_address,
                transforms_by_address=transforms_by_address,
                initialise_parameters=True,
                display_fields=display_fields_by_method.get(method.address),
                nested_display_fields=nested_display_fields_by_method.get(method.address),
                virtual_text_slots_by_component=virtual_text_slots_by_component,
                display_container_operations=display_container_operations,
                disassembler=disassembler,
                instruction_cache=instruction_cache,
                return_summaries_by_address=return_summaries_by_address,
                methods_by_address=methods,
                return_object_display_fields=return_object_display_fields,
                pointer_slots=pointer_slots,
            )
            unresolved.extend(analysis.unresolved)
            for parameter_index, downstream, _, transforms in analysis.parameter_hits:
                parameter_registers = _parameter_registers(method.signature)
                argument_register = parameter_registers[parameter_index]
                if argument_register is None or not argument_register.startswith("x"):
                    continue
                spec = SinkSpec(
                    address=method.address,
                    name=method.name,
                    signature=method.signature,
                    argument_index=parameter_index,
                    argument_register=argument_register,
                    path=(method.name,)
                    + downstream.path[: max(1, max_wrapper_depth)],
                    wrapper_depth=downstream.wrapper_depth + 1,
                    argument_transform=(" -> ".join(transforms) if transforms else None),
                    container_contents=(
                        downstream.container_contents
                        or "index-insensitive container enumeration" in transforms
                    ),
                )
                semantic_key = (method.address, parameter_index, downstream.path[-1])
                if semantic_key not in known_wrapper_keys:
                    known_wrapper_keys.add(semantic_key)
                    sinks_by_address[method.address].append(spec)
                    new_frontier.add(method.address)
                    wrapper_methods.add(method.address)
        if not new_frontier:
            break
        frontier = new_frontier
    return sinks_by_address, wrapper_methods, _dedupe_dicts(unresolved)


def _discover_return_summaries(
    *,
    candidate_addresses: set[int],
    call_index: Mapping[int, set[int]],
    methods: Mapping[int, MethodRecord],
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None,
    literals: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    transforms_by_address: Mapping[int, TransformSpec],
    disassembler: Any,
    instruction_cache: dict[int, tuple[Any, ...]],
    max_depth: int,
    pointer_slots: Mapping[int, int] | None = None,
    enum_type_by_pointer_slot: Mapping[int, str] | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[int, tuple[AbstractValue, ...]]:
    """Summarise project string-return helpers used by literal-owning methods."""

    summaries: dict[int, tuple[AbstractValue, ...]] = {}
    del max_depth  # propagation reaches a fixed point; only displayed paths are capped
    queue: deque[int] = deque(sorted(candidate_addresses, reverse=True))
    queued = set(candidate_addresses)
    processed_count = 0
    while queue:
        address = queue.popleft()
        queued.discard(address)
        method = methods.get(address)
        if method is None:
            continue
        if progress_callback and method.end - method.address >= 256 * 1024:
            progress_callback(
                f"字符串返回链超大方法：序号={processed_count + 1}，"
                f"地址={_hex(method.address)}，机器码={method.end - method.address} 字节，"
                f"名称={method.name}"
            )
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address={},
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=summaries,
            pointer_slots=pointer_slots,
            methods_by_address=methods,
            enum_type_by_pointer_slot=enum_type_by_pointer_slot,
        )
        processed_count += 1
        if progress_callback and processed_count % 25 == 0:
            progress_callback(
                f"字符串返回链进度：已分析={processed_count}，待处理={len(queue)}，"
                f"已生成摘要={len(summaries)}"
            )
        values = tuple(
            sorted(
                analysis.return_values,
                key=lambda value: (
                    value.kind,
                    value.source,
                    value.origin,
                    value.transforms,
                ),
            )
        )
        if not values or summaries.get(address) == values:
            continue
        summaries[address] = values
        for caller in call_index.get(address, set()):
            if caller in candidate_addresses and caller not in queued:
                queue.append(caller)
                queued.add(caller)
    return summaries


def analyze_arm64_display_usage(
    *,
    code_sections: Sequence[tuple[int, bytes]],
    memory_sections: Sequence[tuple[int, bytes]] | None = None,
    method_payload: Sequence[Mapping[str, Any]],
    addresses: Sequence[int],
    literals: Mapping[int, str],
    slot_to_cell: Mapping[int, int],
    inputs: Mapping[str, str | None] | None = None,
    max_wrapper_depth: int = 4,
    display_fields_by_method: Mapping[int, Mapping[int, str]] | None = None,
    nested_display_fields_by_method: Mapping[
        int, Mapping[tuple[int, ...], str]
    ] | None = None,
    virtual_text_slots_by_component: Mapping[str, frozenset[int]] | None = None,
    pointer_slots: Mapping[int, int] | None = None,
    metadata_method_targets: Mapping[int, int] | None = None,
    return_object_display_fields: Mapping[int, Mapping[int, str]] | None = None,
    enum_type_by_pointer_slot: Mapping[int, str] | None = None,
    enum_members_by_type: Mapping[str, Sequence[str]] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    carrier_field_types: Mapping[tuple[str, ...], Mapping[int, tuple[str, ...]]] | None = None,
) -> dict[str, Any]:
    """Analyse already loaded ARM64 sections.

    This lower-level API is useful for deterministic unit tests.  Production
    callers should normally use :func:`analyze_il2cpp_display_usage`.
    """

    # Invocation-local memoisation only: the launcher can process several
    # games in one Python process, so never retain one project's signatures.
    for cached_parser in (
        _normalise_signature,
        _signature_parameters,
        _string_parameter_indices,
        _parameter_registers,
        _match_display_sink,
        _match_render_sink,
    ):
        cached_parser.cache_clear()

    def report(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    section_ends = [base + len(data) for base, data in code_sections]
    methods, all_addresses = _prepare_methods(method_payload, addresses, section_ends)
    report(f"方法索引完成：方法={len(methods)}，待分析字符串={len(literals)}")
    disassembler = _create_arm64_disassembler()
    instruction_cache = _BoundedInstructionCache()
    pointer_slots = pointer_slots or {}
    metadata_method_targets = metadata_method_targets or {}
    return_object_display_fields = return_object_display_fields or {}
    enum_type_by_pointer_slot = enum_type_by_pointer_slot or {}
    enum_members_by_type = enum_members_by_type or {}
    display_fields_by_method = display_fields_by_method or {}
    nested_display_fields_by_method = nested_display_fields_by_method or {}
    virtual_text_slots_by_component = virtual_text_slots_by_component or {}
    display_sinks: list[SinkSpec] = []
    render_sinks: list[dict[str, Any]] = []
    transforms_by_address: dict[int, TransformSpec] = {}
    display_container_operations: dict[int, tuple[str, tuple[int, ...]]] = {}
    for method in methods.values():
        aliases = method.aliases or ((method.name, method.signature),)
        for alias_name, alias_signature in aliases:
            if _match_display_sink(alias_name, alias_signature):
                # Some immediate-mode/UI Toolkit APIs expose more than one
                # visible string (for example label + current value).  Track
                # every string argument instead of silently retaining only the
                # first one.
                registers = _parameter_registers(alias_signature)
                for string_argument_index in _string_parameter_indices(alias_signature):
                    argument_register = registers[string_argument_index]
                    if argument_register is not None and argument_register.startswith("x"):
                        display_sinks.append(
                            SinkSpec(
                                method.address,
                                alias_name,
                                alias_signature,
                                string_argument_index,
                                argument_register,
                                (alias_name,),
                                argument_transform=_display_sink_argument_transform(
                                    alias_name, alias_signature
                                ),
                            )
                        )
            if _match_render_sink(alias_name, alias_signature):
                render_sinks.append(
                    {
                        "address": _hex(method.address),
                        "name": alias_name,
                        "signature": alias_signature,
                    }
                )
            transform = _match_string_transform(alias_name, alias_signature)
            if transform:
                transforms_by_address[method.address] = transform
            container_operation = _display_container_operation(
                MethodRecord(method.address, method.end, alias_name, alias_signature)
            )
            if container_operation:
                display_container_operations[method.address] = container_operation
    report(
        f"显示入口识别完成：文本入口={len(display_sinks)}，渲染入口={len(render_sinks)}"
    )

    delegate_constructor_addresses = frozenset(
        method.address
        for method in methods.values()
        if _is_delegate_constructor(method)
    )
    delegate_combine_addresses = frozenset(
        method.address
        for method in methods.values()
        if method.name == "System.Delegate$$Combine"
    )

    report("正在单遍扫描 libil2cpp 代码索引……")
    call_index, indirect_call_methods, references_by_method = _scan_code_indexes(
        code_sections,
        all_addresses,
        methods,
        set(slot_to_cell)
        | set(literals)
        | set(pointer_slots)
        | set(enum_type_by_pointer_slot),
    )
    literal_location_set = set(slot_to_cell) | set(literals)
    literal_reference_methods = {
        method_address
        for method_address, referenced_slots in references_by_method.items()
        if referenced_slots & literal_location_set
    }
    enum_reference_methods = {
        method_address
        for method_address, referenced_slots in references_by_method.items()
        if referenced_slots & set(enum_type_by_pointer_slot)
    }
    report(
        f"代码索引完成：调用目标={len(call_index)}，"
        f"字符串引用方法={len(literal_reference_methods)}"
    )
    string_return_methods = {
        method.address
        for method in methods.values()
        if _is_project_owned_method(method)
        and _normalise_signature(method.signature).startswith("System_String_o*")
    }
    # Find methods on a direct call path to a real display sink.  A common
    # getter does not itself reference a setter: Refresh() calls GetTitle(),
    # then forwards the return value to Text.set_text.  Seeding only from
    # literal-owning callers misses exactly that shape.
    display_reachable_methods: set[int] = set()
    reach_frontier = {sink.address for sink in display_sinks}
    while reach_frontier:
        callers = {
            caller
            for target in reach_frontier
            for caller in call_index.get(target, set())
            if caller in methods and _is_project_owned_method(methods[caller])
        } - display_reachable_methods
        if not callers:
            break
        display_reachable_methods.update(callers)
        reach_frontier = callers

    relevant_return_callers = set(display_reachable_methods)
    return_summary_candidates: set[int] = string_return_methods & (
        literal_reference_methods | enum_reference_methods
    )
    relevant_return_callers.update(return_summary_candidates)
    while True:
        discovered = {
            target
            for target in string_return_methods
            if call_index.get(target, set()) & relevant_return_callers
        } - return_summary_candidates
        if not discovered:
            break
        return_summary_candidates.update(discovered)
        relevant_return_callers.update(discovered)
    report(f"正在分析字符串返回链：候选方法={len(return_summary_candidates)}")
    return_summaries_by_address = _discover_return_summaries(
        candidate_addresses=return_summary_candidates,
        call_index=call_index,
        methods=methods,
        code_sections=code_sections,
        memory_sections=memory_sections,
        literals=literals,
        slot_to_cell=slot_to_cell,
        transforms_by_address=transforms_by_address,
        disassembler=disassembler,
        instruction_cache=instruction_cache,
        max_depth=max_wrapper_depth,
        pointer_slots=pointer_slots,
        enum_type_by_pointer_slot=enum_type_by_pointer_slot,
        progress_callback=progress_callback,
    )
    report(f"字符串返回链完成：摘要方法={len(return_summaries_by_address)}")
    component_factory_addresses = {
        method.address
        for method in methods.values()
        if _return_display_component_type(method.signature) is not None
        or _is_erased_get_component_factory(method)
    } | {address for address in return_object_display_fields if address in methods}
    component_factory_callers = {
        caller
        for target in component_factory_addresses
        for caller in call_index.get(target, set())
    }
    # Discover ordinary wrappers first. Carrier consumers often read an
    # instance container and pass the selected text through such a wrapper
    # instead of calling Unity's setter directly.
    pre_carrier_sinks_by_address, _, _ = _discover_wrapper_sinks(
        methods=methods,
        call_index=call_index,
        code_sections=code_sections,
        memory_sections=memory_sections,
        literal_cells=literals,
        slot_to_cell=slot_to_cell,
        base_sinks=display_sinks,
        transforms_by_address=transforms_by_address,
        max_wrapper_depth=max_wrapper_depth,
        display_fields_by_method=display_fields_by_method,
        nested_display_fields_by_method=nested_display_fields_by_method,
        virtual_text_slots_by_component=virtual_text_slots_by_component,
        display_container_operations=display_container_operations,
        disassembler=disassembler,
        instruction_cache=instruction_cache,
        return_summaries_by_address=return_summaries_by_address,
        component_factory_callers=component_factory_callers,
        return_object_display_fields=return_object_display_fields,
        pointer_slots=pointer_slots,
    )
    pre_carrier_sinks = list(
        dict.fromkeys(
            sink
            for sink_group in pre_carrier_sinks_by_address.values()
            for sink in sink_group
        )
    )
    (
        carrier_field_sinks,
        carrier_exact_hits,
        carrier_derived_hits,
        carrier_unresolved,
    ) = _discover_carrier_field_sinks(
        methods=methods,
        code_sections=code_sections,
        memory_sections=memory_sections,
        literal_cells=literals,
        slot_to_cell=slot_to_cell,
        base_sinks=pre_carrier_sinks,
        transforms_by_address=transforms_by_address,
        display_fields_by_method=display_fields_by_method,
        nested_display_fields_by_method=nested_display_fields_by_method,
        virtual_text_slots_by_component=virtual_text_slots_by_component,
        display_container_operations=display_container_operations,
        disassembler=disassembler,
        instruction_cache=instruction_cache,
        consumer_methods=display_reachable_methods,
        carrier_field_types=carrier_field_types,
        progress_callback=progress_callback,
    )
    # A displayed model can itself be populated from another persisted model
    # or container. Follow newly proven writer arguments upstream to a fixed
    # point, analysing only their callers in subsequent rounds.
    def carrier_key(sink):
        return sink.address, sink.argument_index, sink.argument_register, sink.argument_transform

    known_carriers = {carrier_key(sink) for sink in carrier_field_sinks}
    frontier = list(carrier_field_sinks)
    while frontier:
        consumers = {caller for sink in frontier for caller in call_index.get(sink.address, set())}
        if not consumers:
            break
        extra_sinks, extra_exact, extra_derived, extra_unresolved = _discover_carrier_field_sinks(
            methods=methods, code_sections=code_sections, memory_sections=memory_sections,
            literal_cells=literals, slot_to_cell=slot_to_cell,
            base_sinks=[*pre_carrier_sinks, *carrier_field_sinks],
            transforms_by_address=transforms_by_address,
            display_fields_by_method={key: value for key, value in display_fields_by_method.items() if key in consumers},
            nested_display_fields_by_method={key: value for key, value in nested_display_fields_by_method.items() if key in consumers},
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            display_container_operations=display_container_operations,
            disassembler=disassembler, instruction_cache=instruction_cache,
            consumer_methods=consumers, carrier_field_types=carrier_field_types,
        )
        carrier_exact_hits.extend(extra_exact)
        carrier_derived_hits.extend(extra_derived)
        carrier_unresolved.extend(extra_unresolved)
        frontier = []
        for sink in extra_sinks:
            if carrier_key(sink) not in known_carriers:
                known_carriers.add(carrier_key(sink))
                carrier_field_sinks.append(sink)
                frontier.append(sink)
        report(f"载体字段上游追踪：消费者={len(consumers)}，新增入口={len(frontier)}")
    report(f"载体字段链完成：新增入口={len(carrier_field_sinks)}")
    report("正在分析显示包装函数……")
    sinks_by_address, wrapper_methods, wrapper_unresolved = _discover_wrapper_sinks(
        methods=methods,
        call_index=call_index,
        code_sections=code_sections,
        memory_sections=memory_sections,
        literal_cells=literals,
        slot_to_cell=slot_to_cell,
        base_sinks=[*display_sinks, *carrier_field_sinks],
        transforms_by_address=transforms_by_address,
        max_wrapper_depth=max_wrapper_depth,
        display_fields_by_method=display_fields_by_method,
        nested_display_fields_by_method=nested_display_fields_by_method,
        virtual_text_slots_by_component=virtual_text_slots_by_component,
        display_container_operations=display_container_operations,
        disassembler=disassembler,
        instruction_cache=instruction_cache,
        return_summaries_by_address=return_summaries_by_address,
        component_factory_callers=component_factory_callers,
        return_object_display_fields=return_object_display_fields,
        pointer_slots=pointer_slots,
    )
    report(f"显示包装函数完成：包装方法={len(wrapper_methods)}")

    candidate_methods: set[int] = set(wrapper_methods)
    for target in sinks_by_address:
        candidate_methods.update(call_index.get(target, set()))
    for target, operation in display_container_operations.items():
        if operation[0] == "display":
            candidate_methods.update(call_index.get(target, set()))

    # A UI refresh may only call a string-return helper: the enum TypeInfo or
    # literal then lives in that helper, not in the refresh's own instructions.
    # Include callers of proven source-return summaries; actual setter argument
    # tracking below still decides whether those values are displayed.
    returned_source_callers = {
        caller
        for target, values in return_summaries_by_address.items()
        if any(value.kind in {"exact", "derived", "enum_text"} for value in values)
        for caller in call_index.get(target, set())
    }
    virtual_component_candidates = {
        address
        for address in literal_reference_methods | enum_reference_methods | returned_source_callers
        if display_fields_by_method.get(address)
        or nested_display_fields_by_method.get(address)
    }
    candidate_methods.update(virtual_component_candidates)
    component_factory_candidates = (
        component_factory_callers
        & (literal_reference_methods | enum_reference_methods | returned_source_callers)
    )
    candidate_methods.update(component_factory_candidates)

    static_producer_methods = set(literal_reference_methods)
    pointer_slot_locations = set(pointer_slots)
    static_producer_methods.update(
        address
        for address, referenced in references_by_method.items()
        if referenced & pointer_slot_locations
        and address in methods
        and _string_parameter_indices(methods[address].signature)
    )
    report(
        f"正在分析静态字段链：消费者={len(candidate_methods)}，"
        f"生产者={len(static_producer_methods)}"
    )
    (
        static_field_sinks,
        static_exact_hits,
        static_derived_hits,
        static_unresolved,
    ) = _discover_static_field_sinks(
        methods=methods,
        consumer_methods=candidate_methods,
        producer_methods=static_producer_methods,
        references_by_method=references_by_method,
        code_sections=code_sections,
        memory_sections=memory_sections,
        literal_cells=literals,
        slot_to_cell=slot_to_cell,
        sinks_by_address=sinks_by_address,
        transforms_by_address=transforms_by_address,
        pointer_slots=pointer_slots,
        metadata_method_targets=metadata_method_targets,
        display_fields_by_method=display_fields_by_method,
        nested_display_fields_by_method=nested_display_fields_by_method,
        virtual_text_slots_by_component=virtual_text_slots_by_component,
        display_container_operations=display_container_operations,
        return_summaries_by_address=return_summaries_by_address,
        disassembler=disassembler,
        instruction_cache=instruction_cache,
    )
    report(f"静态字段链完成：新增入口={len(static_field_sinks)}")
    if static_field_sinks:
        sinks_by_address, wrapper_methods, additional_wrapper_unresolved = (
            _discover_wrapper_sinks(
                methods=methods,
                call_index=call_index,
                code_sections=code_sections,
                memory_sections=memory_sections,
                literal_cells=literals,
                slot_to_cell=slot_to_cell,
                base_sinks=[*display_sinks, *carrier_field_sinks, *static_field_sinks],
                transforms_by_address=transforms_by_address,
                max_wrapper_depth=max_wrapper_depth,
                display_fields_by_method=display_fields_by_method,
                nested_display_fields_by_method=nested_display_fields_by_method,
                virtual_text_slots_by_component=virtual_text_slots_by_component,
                display_container_operations=display_container_operations,
                disassembler=disassembler,
                instruction_cache=instruction_cache,
                return_summaries_by_address=return_summaries_by_address,
                component_factory_callers=component_factory_callers,
                return_object_display_fields=return_object_display_fields,
                pointer_slots=pointer_slots,
            )
        )
        wrapper_unresolved.extend(additional_wrapper_unresolved)
        candidate_methods.update(wrapper_methods)
        for target in sinks_by_address:
            candidate_methods.update(call_index.get(target, set()))

    # First recover each event field's subscription target from the standard
    # Delegate ctor + Delegate.Combine sequence.  Do not yet assume that the
    # callback displays anything.
    delegate_subscriptions: dict[tuple[int, tuple[str, ...]], set[int]] = defaultdict(set)
    combine_callers = {
        caller
        for target in delegate_combine_addresses
        for caller in call_index.get(target, set())
        if caller in methods and _is_project_owned_method(methods[caller])
    }
    # A normal C# event subscription is split across methods: the generated
    # ``add_Event`` method performs Delegate.Combine and stores the result,
    # while a caller constructs the callback and invokes that add method.
    # Summarise the delegate parameter -> instance field write first so the
    # caller can be connected to the same field used by a later Invoke.
    delegate_add_sinks_by_address: dict[
        int, tuple[tuple[str, tuple[int, ...]], ...]
    ] = {}
    for method_address in sorted(combine_callers):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            track_object_fields=True,
        )
        parameter_registers = _parameter_registers(method.signature)
        discovered: list[tuple[str, tuple[int, ...]]] = []
        for parameter_index, field_path, _store_pc in analysis.delegate_parameter_field_writes:
            if parameter_index >= len(parameter_registers):
                continue
            parameter_register = parameter_registers[parameter_index]
            if parameter_register is not None and field_path:
                discovered.append((parameter_register, field_path))
        if discovered:
            delegate_add_sinks_by_address[method_address] = tuple(
                dict.fromkeys(discovered)
            )
    delegate_constructor_callers = {
        caller
        for target in delegate_constructor_addresses
        for caller in call_index.get(target, set())
    }
    direct_subscription_candidates = combine_callers & delegate_constructor_callers
    add_subscription_candidates = delegate_constructor_callers & {
        caller
        for target in delegate_add_sinks_by_address
        for caller in call_index.get(target, set())
    }
    subscription_candidates = direct_subscription_candidates | add_subscription_candidates
    for method_address in sorted(subscription_candidates):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=method_address in add_subscription_candidates,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            delegate_add_sinks_by_address=delegate_add_sinks_by_address,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            track_object_fields=method_address in add_subscription_candidates,
        )
        for key, targets in analysis.delegate_subscriptions.items():
            delegate_subscriptions[key].update(targets)

    # Then prove only the actually subscribed callback targets.  This keeps a
    # large game from having to abstract-interpret every method that happens to
    # own a Text/TMP/UILabel field.
    delegate_display_sinks: dict[
        int, tuple[tuple[int, SinkSpec, tuple[str, ...]], ...]
    ] = {
        address: tuple((sink.argument_index, sink, ()) for sink in sinks)
        for address, sinks in sinks_by_address.items()
        if address in methods and _string_parameter_indices(methods[address].signature)
    }
    subscribed_targets = {
        target for targets in delegate_subscriptions.values() for target in targets
    }
    for method_address in sorted(subscribed_targets):
        fields = display_fields_by_method.get(method_address)
        nested_fields = nested_display_fields_by_method.get(method_address)
        method = methods.get(method_address)
        if (
            method is None
            or not _string_parameter_indices(method.signature)
        ):
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=True,
            display_fields=fields,
            nested_display_fields=nested_fields,
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            display_container_operations=display_container_operations,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
        )
        reached = tuple(
            (parameter, sink, transforms)
            for parameter, sink, _callsite, transforms in analysis.parameter_hits
        )
        if reached:
            delegate_display_sinks[method_address] = tuple(
                dict.fromkeys((*delegate_display_sinks.get(method_address, ()), *reached))
            )

    delegate_subscriptions = {
        key: {target for target in targets if target in delegate_display_sinks}
        for key, targets in delegate_subscriptions.items()
    }
    delegate_subscriptions = {
        key: targets for key, targets in delegate_subscriptions.items() if targets
    }

    subscribed_root_slots = {key[0] for key in delegate_subscriptions}
    if subscribed_root_slots:
        delegate_invocation_candidates = {
            method_address
            for method_address in indirect_call_methods
            if (
                references_by_method.get(method_address, set()) & subscribed_root_slots
                or _event_family_root(_method_object_family(methods[method_address]))
                in subscribed_root_slots
            )
        }
    else:
        delegate_invocation_candidates = set()

    # Treat a method which forwards one of its parameters through a proven
    # delegate event as a normal display sink.  The literal often lives in its
    # caller (for example: Format(...) -> ShowFadeableNotice(message) ->
    # Action<string> -> TMP), so restricting event analysis to methods that
    # themselves reference a literal loses the real display chain.
    delegate_forward_sinks: list[SinkSpec] = []
    for method_address in sorted(delegate_invocation_candidates):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            display_container_operations=display_container_operations,
            initialise_parameters=True,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            enum_type_by_pointer_slot=enum_type_by_pointer_slot,
            track_object_fields=True,
        )
        caller_registers = _parameter_registers(method.signature)
        for key, _callsite, values_by_register in analysis.delegate_invocations:
            for target in delegate_subscriptions.get(key, set()):
                callback = methods[target]
                callback_registers = _parameter_registers(callback.signature)
                for callback_parameter, downstream, parameter_transforms in delegate_display_sinks[target]:
                    if callback_parameter >= len(callback_registers):
                        continue
                    callback_register = callback_registers[callback_parameter]
                    if callback_register is None:
                        continue
                    for value in values_by_register.get(callback_register, ()):
                        if value.kind not in {"param", "derived_param"}:
                            continue
                        if value.source >= len(caller_registers):
                            continue
                        caller_register = caller_registers[value.source]
                        if caller_register is None:
                            continue
                        delegate_forward_sinks.append(
                            SinkSpec(
                                method.address,
                                method.name,
                                method.signature,
                                value.source,
                                caller_register,
                                (
                                    "delegate invocation",
                                    callback.name,
                                    *downstream.path,
                                ),
                                argument_transform=(
                                    "delegate parameter transform"
                                    if value.kind == "derived_param" or parameter_transforms
                                    else None
                                ),
                            )
                        )
    if delegate_forward_sinks:
        delegate_sink_map: dict[int, list[SinkSpec]] = defaultdict(list)
        for sink in delegate_forward_sinks:
            delegate_sink_map[sink.address].append(sink)
        sinks_by_address = {
            **sinks_by_address,
            **{
                address: tuple(dict.fromkeys((*sinks_by_address.get(address, ()), *items)))
                for address, items in delegate_sink_map.items()
            },
        }
        delegate_sinks_by_address, delegate_wrappers, delegate_unresolved = (
            _discover_wrapper_sinks(
                methods=methods,
                call_index=call_index,
                code_sections=code_sections,
                memory_sections=memory_sections,
                literal_cells=literals,
                slot_to_cell=slot_to_cell,
                base_sinks=[
                    sink
                    for sink_group in sinks_by_address.values()
                    for sink in sink_group
                ],
                transforms_by_address=transforms_by_address,
                max_wrapper_depth=max_wrapper_depth,
                display_fields_by_method=display_fields_by_method,
                nested_display_fields_by_method=nested_display_fields_by_method,
                virtual_text_slots_by_component=virtual_text_slots_by_component,
                display_container_operations=display_container_operations,
                disassembler=disassembler,
                instruction_cache=instruction_cache,
                return_summaries_by_address=return_summaries_by_address,
                component_factory_callers=component_factory_callers,
                return_object_display_fields=return_object_display_fields,
                pointer_slots=pointer_slots,
            )
        )
        sinks_by_address = delegate_sinks_by_address
        wrapper_unresolved.extend(delegate_unresolved)
        candidate_methods.update(delegate_wrappers)
        for target in sinks_by_address:
            candidate_methods.update(call_index.get(target, set()))

    exact_by_literal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    derived_by_literal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    probable_by_literal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for hit in carrier_exact_hits:
        exact_by_literal[int(hit["literal"])].append(hit["evidence"])
    for hit in carrier_derived_hits:
        derived_by_literal[int(hit["literal"])].append(hit["evidence"])
    for hit in static_exact_hits:
        exact_by_literal[int(hit["literal"])].append(hit["evidence"])
    for hit in static_derived_hits:
        derived_by_literal[int(hit["literal"])].append(hit["evidence"])
    unresolved = [*carrier_unresolved, *wrapper_unresolved, *static_unresolved]
    referenced_literals: set[int] = set()
    enum_display_hits: list[dict[str, Any]] = []
    analysed_count = 0
    ordered_candidate_methods = sorted(candidate_methods)
    report(f"正在汇总直接显示候选：方法={len(ordered_candidate_methods)}")
    for candidate_index, method_address in enumerate(ordered_candidate_methods, start=1):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            initialise_parameters=False,
            display_fields=display_fields_by_method.get(method_address),
            nested_display_fields=nested_display_fields_by_method.get(method_address),
            virtual_text_slots_by_component=virtual_text_slots_by_component,
            display_container_operations=display_container_operations,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            enum_type_by_pointer_slot=enum_type_by_pointer_slot,
        )
        analysed_count += 1
        if candidate_index % 250 == 0:
            report(f"直接显示候选进度：{candidate_index}/{len(ordered_candidate_methods)}")
        referenced_literals.update(analysis.referenced_literals)
        unresolved.extend(analysis.unresolved)
        for hit in analysis.exact_hits:
            exact_by_literal[int(hit["literal"])].append(hit["evidence"])
        for hit in analysis.derived_hits:
            derived_by_literal[int(hit["literal"])].append(hit["evidence"])
        for hit in analysis.probable_hits:
            probable_by_literal[int(hit["literal"])].append(hit["evidence"])
        enum_display_hits.extend(analysis.enum_display_hits)

    delegate_invocation_count = 0
    ordered_delegate_candidates = sorted(delegate_invocation_candidates)
    report(f"正在汇总委托显示候选：方法={len(ordered_delegate_candidates)}")
    for delegate_index, method_address in enumerate(ordered_delegate_candidates, start=1):
        method = methods.get(method_address)
        if method is None:
            continue
        analysis = _analyse_method(
            method=method,
            code_sections=code_sections,
            memory_sections=memory_sections,
            literal_cells=literals,
            slot_to_cell=slot_to_cell,
            sinks_by_address=sinks_by_address,
            transforms_by_address=transforms_by_address,
            display_container_operations=display_container_operations,
            initialise_parameters=True,
            pointer_slots=pointer_slots,
            metadata_method_targets=metadata_method_targets,
            delegate_constructor_addresses=delegate_constructor_addresses,
            delegate_combine_addresses=delegate_combine_addresses,
            disassembler=disassembler,
            instruction_cache=instruction_cache,
            return_summaries_by_address=return_summaries_by_address,
            methods_by_address=methods,
            enum_type_by_pointer_slot=enum_type_by_pointer_slot,
            track_object_fields=True,
        )
        analysed_count += 1
        if delegate_index % 250 == 0:
            report(f"委托显示候选进度：{delegate_index}/{len(ordered_delegate_candidates)}")
        referenced_literals.update(analysis.referenced_literals)
        unresolved.extend(analysis.unresolved)
        for key, callsite, values_by_register in analysis.delegate_invocations:
            targets = delegate_subscriptions.get(key, set())
            if not targets:
                continue
            delegate_invocation_count += 1
            for target in targets:
                callback = methods[target]
                callback_registers = _parameter_registers(callback.signature)
                for parameter_index, downstream, parameter_transforms in delegate_display_sinks[target]:
                    if parameter_index >= len(callback_registers):
                        continue
                    argument_register = callback_registers[parameter_index]
                    if argument_register is None:
                        continue
                    for value in values_by_register.get(argument_register, ()):
                        evidence = {
                            "method_address": _hex(method.address),
                            "method_name": method.name,
                            "callsite": _hex(callsite),
                            "sink_address": _hex(callback.address),
                            "sink_name": callback.name,
                            "sink_signature": callback.signature,
                            "source_instruction": _hex(value.origin),
                            "path": [
                                "delegate invocation",
                                callback.name,
                                *downstream.path,
                            ],
                            "transforms": [*value.transforms, *parameter_transforms],
                        }
                        if value.kind in {"probable", "probable_derived"}:
                            probable_by_literal[value.source].append(evidence)
                        elif value.kind == "exact" and not parameter_transforms:
                            exact_by_literal[value.source].append(evidence)
                        else:
                            derived_by_literal[value.source].append(evidence)

        enum_display_hits.extend(analysis.enum_display_hits)

    enum_roles: dict[str, set[str]] = defaultdict(set)
    enum_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in enum_display_hits:
        enum_type = hit.get("enum_type")
        role = hit.get("role")
        evidence = hit.get("evidence")
        if not isinstance(enum_type, str) or role not in {"exact", "derived"}:
            continue
        enum_roles[enum_type].add(role)
        if isinstance(evidence, dict):
            enum_evidence[enum_type].append(evidence)
    display_enum_types = [
        {
            "enum_type": enum_type,
            "members": list(enum_members_by_type.get(enum_type, ())),
            "roles": sorted(enum_roles[enum_type]),
            "evidence": _dedupe_dicts(enum_evidence[enum_type]),
        }
        for enum_type in sorted(enum_roles)
        if enum_members_by_type.get(enum_type)
    ]

    # If a source reaches a sink both unchanged and through a transform, exact
    # remains valid for the unchanged path and both evidence sets are retained.
    exact_literals = [
        {
            "address": _hex(address),
            "value": literals.get(address, ""),
            "evidence": _dedupe_dicts(evidence),
        }
        for address, evidence in sorted(exact_by_literal.items())
    ]
    derived_influence = [
        {
            "address": _hex(address),
            "value": literals.get(address, ""),
            "evidence": _dedupe_dicts(evidence),
        }
        for address, evidence in sorted(derived_by_literal.items())
    ]
    probable_display_literals = [
        {
            "address": _hex(address),
            "value": literals.get(address, ""),
            "evidence": _dedupe_dicts(evidence),
        }
        for address, evidence in sorted(probable_by_literal.items())
        if address not in exact_by_literal and address not in derived_by_literal
    ]

    display_sink_payload = [
        {
            "address": _hex(sink.address),
            "name": sink.name,
            "signature": sink.signature,
            "string_argument_index": sink.argument_index,
            "string_argument_register": sink.argument_register,
            "argument_transform": sink.argument_transform,
        }
        for sink in sorted(display_sinks, key=lambda item: (item.address, item.signature))
    ]
    unresolved = _dedupe_dicts(unresolved)
    report(
        f"链路分析完成：精确={len(exact_literals)}，派生={len(derived_influence)}，"
        f"可能显示={len(probable_display_literals)}，缓存淘汰="
        f"{instruction_cache.eviction_count}"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "inputs": dict(inputs or {}),
        "exact_literals": exact_literals,
        "derived_influence": derived_influence,
        "probable_display_literals": probable_display_literals,
        "display_enum_types": display_enum_types,
        "unresolved": unresolved,
        "display_sinks": display_sink_payload,
        "display_containers": [
            {
                "address": _hex(address),
                "name": methods[address].name,
                "operation": operation,
                "argument_indices": list(indices),
            }
            for address, (operation, indices) in sorted(
                display_container_operations.items()
            )
        ],
        "render_sinks": sorted(render_sinks, key=lambda item: item["address"]),
        "stats": {
            "literal_count": len(literals),
            "shared_rva_alias_count": sum(
                max(0, len(method.aliases) - 1) for method in methods.values()
            ),
            "relative_relocation_slot_count": len(slot_to_cell),
            "display_sink_count": len(display_sinks),
            "display_container_operation_count": len(display_container_operations),
            "render_sink_count": len(render_sinks),
            "wrapper_sink_count": len(wrapper_methods),
            "carrier_field_sink_count": len(carrier_field_sinks),
            "static_field_sink_count": len(static_field_sinks),
            "static_field_literal_count": len(static_exact_hits)
            + len(static_derived_hits),
            "string_return_summary_count": len(return_summaries_by_address),
            "candidate_method_count": len(candidate_methods),
            "virtual_component_candidate_method_count": len(virtual_component_candidates),
            "verified_virtual_text_slot_count": sum(
                len(offsets) for offsets in virtual_text_slots_by_component.values()
            ),
            "nested_component_method_count": len(nested_display_fields_by_method),
            "component_factory_candidate_method_count": len(component_factory_candidates),
            "delegate_display_callback_method_count": len(delegate_display_sinks),
            "delegate_add_method_count": len(delegate_add_sinks_by_address),
            "delegate_subscription_candidate_method_count": len(subscription_candidates),
            "delegate_subscription_field_count": len(delegate_subscriptions),
            "delegate_invocation_candidate_method_count": len(delegate_invocation_candidates),
            "delegate_invocation_count": delegate_invocation_count,
            "analysed_method_count": analysed_count,
            "referenced_literal_count": len(referenced_literals),
            "exact_literal_count": len(exact_literals),
            "derived_literal_count": len(derived_influence),
            "probable_literal_count": len(probable_display_literals),
            "display_enum_type_count": len(display_enum_types),
            "display_enum_member_count": sum(
                len(item["members"]) for item in display_enum_types
            ),
            "unresolved_count": len(unresolved),
        },
    }


def _content_fingerprint(paths: Sequence[Path], options: Mapping[str, Any]) -> str:
    """Hash bytes, not mtimes: same-size replacements must invalidate analysis."""
    digest = hashlib.sha256(json.dumps(options, sort_keys=True).encode("utf-8"))
    for path in paths:
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(b"\0")
        content = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(chunk)
        except OSError as exc:
            raise Il2CppDisplayAnalysisError(f"Unable to fingerprint {path}: {exc}") from exc
        digest.update(content.digest())
    return digest.hexdigest()


def _result_digest(result: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_analysis_cache(path: Path, fingerprint: str):
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cached, dict) or cached.get("fingerprint") != fingerprint:
            return None, "input_or_analyzer_changed"
        result = cached.get("result")
        if (not isinstance(result, dict)
                or not isinstance(result.get("stats"), dict)
                or any(not isinstance(result.get(k), list) for k in (
                    "exact_literals", "derived_influence", "unresolved"
                ))
                or cached.get("result_sha256") != _result_digest(result)):
            return None, "invalid_result"
        return result, "content_verified"
    except FileNotFoundError:
        return None, "not_found"
    except (OSError, ValueError):
        return None, "unreadable_or_corrupt"


def _write_analysis_cache(path: Path, fingerprint: str, result: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"fingerprint": fingerprint, "result_sha256": _result_digest(result),
                       "result": result}, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def analyze_il2cpp_display_usage(
    *,
    libil2cpp_path: str | Path,
    script_json_path: str | Path,
    stringliteral_json_path: str | Path | None = None,
    dump_cs_path: str | Path | None = None,
    max_wrapper_depth: int = 4,
    cache_path: str | Path | None = None,
    use_cache: bool = True,
    exclude_literal_addresses: Collection[int | str] = (),
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Analyse an Il2CppDumper output set and its ARM64 ``libil2cpp.so``."""

    try:
        from elftools.elf.elffile import ELFFile
    except ImportError as exc:  # pragma: no cover - exercised in deployment failures
        raise Il2CppDisplayAnalysisError(
            "pyelftools is required for IL2CPP display analysis"
        ) from exc

    started = time.perf_counter()
    timings: dict[str, float] = {}
    so_path = Path(libil2cpp_path).resolve()
    script_path = Path(script_json_path).resolve()
    literal_path = Path(stringliteral_json_path).resolve() if stringliteral_json_path else None
    dump_path = Path(dump_cs_path).resolve() if dump_cs_path else None
    cache_target = (
        Path(cache_path).resolve()
        if cache_path is not None
        else script_path.with_name(".il2cpp_display_usage.cache.json")
    )

    input_paths = [so_path, script_path]
    if literal_path is not None:
        input_paths.append(literal_path)
    if dump_path is not None:
        input_paths.append(dump_path)
    excluded_addresses: set[int] = set()
    for raw_address in exclude_literal_addresses:
        try:
            address = int(raw_address, 0) if isinstance(raw_address, str) else int(raw_address)
        except (TypeError, ValueError) as exc:
            raise Il2CppDisplayAnalysisError(
                f"Invalid excluded literal address: {raw_address!r}"
            ) from exc
        if address < 0:
            raise Il2CppDisplayAnalysisError(
                f"Invalid excluded literal address: {raw_address!r}"
            )
        excluded_addresses.add(address)
    fingerprint_options = {
        "schema_version": SCHEMA_VERSION, "max_wrapper_depth": max_wrapper_depth,
        "exclude_literal_addresses": sorted(excluded_addresses),
        "capstone": package_version("capstone"), "pyelftools": package_version("pyelftools"),
        "max_values": MAX_ABSTRACT_VALUES, "max_transforms": MAX_ABSTRACT_TRANSFORM_STEPS,
    }
    fingerprint_paths = [*input_paths, Path(__file__)]
    fingerprint = _content_fingerprint(fingerprint_paths, fingerprint_options)
    timings["fingerprint_seconds"] = time.perf_counter() - started
    cache_reason = "disabled"
    if use_cache:
        result, cache_reason = _read_analysis_cache(cache_target, fingerprint)
        if result is not None:
            timings["total_seconds"] = time.perf_counter() - started
            result["stats"].update(cache_hit=True, cache_reason=cache_reason, performance=timings)
            if progress_callback:
                progress_callback(f"内容校验通过，复用分析缓存；耗时={timings['total_seconds']:.3f}s")
            return result
    if progress_callback:
        progress_callback(f"分析缓存未复用：{cache_reason}")
    phase_started = time.perf_counter()
    if progress_callback:
        progress_callback("正在读取 script.json 与方法元数据……")
    script_payload = _read_script_payload(script_path)
    methods, addresses, script_literals = _load_script(script_path, payload=script_payload)
    metadata_method_targets = _load_script_metadata_method_targets(script_path, payload=script_payload)
    metadata_type_names = _load_script_metadata_type_names(script_path, payload=script_payload)
    metadata_component_factory_types = _load_script_metadata_component_factory_types(
        script_path, payload=script_payload
    )
    del script_payload
    source_literals = _load_literal_file(literal_path) if literal_path else script_literals
    if not source_literals:
        raise Il2CppDisplayAnalysisError("No string literals were found in the supplied JSON")
    literals = {
        address: value
        for address, value in source_literals.items()
        if address not in excluded_addresses
    }

    if progress_callback:
        progress_callback(
            f"元数据读取完成：方法记录={len(methods)}，待分析字符串={len(literals)}；"
            "正在读取 ELF 与重定位表……"
        )

    timings["metadata_seconds"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    try:
        with so_path.open("rb") as stream:
            elf = ELFFile(stream)
            machine = elf["e_machine"]
            if machine not in {"EM_AARCH64", 183}:
                raise Il2CppDisplayAnalysisError(
                    f"Only ARM64 IL2CPP binaries are supported, got {machine!r}"
                )
            pointer_slots = _collect_relative_slots(elf)
            literal_addresses = set(literals)
            slot_to_cell = {
                slot: target
                for slot, target in pointer_slots.items()
                if target in literal_addresses
            }
            code_sections: list[tuple[int, bytes]] = []
            memory_sections: list[tuple[int, bytes]] = []
            for section in elf.iter_sections():
                flags = int(section["sh_flags"])
                size = int(section["sh_size"])
                if size <= 0:
                    continue
                if flags & 0x2 and section["sh_type"] != "SHT_NOBITS":  # SHF_ALLOC
                    section_payload = section.data()
                    memory_sections.append((int(section["sh_addr"]), section_payload))
                    if flags & 0x4:  # SHF_EXECINSTR
                        code_sections.append((int(section["sh_addr"]), section_payload))
    except OSError as exc:
        raise Il2CppDisplayAnalysisError(f"Unable to read {so_path}: {exc}") from exc

    if not code_sections:
        raise Il2CppDisplayAnalysisError(f"No executable ARM64 sections found in {so_path}")

    timings["elf_seconds"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    display_fields_by_method = _parse_dump_display_fields(dump_path) if dump_path else {}
    nested_display_fields_by_method = (
        _parse_dump_nested_display_fields(dump_path) if dump_path else {}
    )
    virtual_text_slots_by_component = (
        _parse_dump_virtual_text_slots(dump_path) if dump_path else {}
    )
    enum_members_by_type = _parse_dump_enum_members(dump_path) if dump_path else {}
    enum_type_by_pointer_slot = {
        slot: enum_type
        for slot, target in pointer_slots.items()
        for enum_type in [metadata_type_names.get(target)]
        if enum_type in enum_members_by_type
    }
    return_object_factory_display_fields = (
        _parse_dump_return_object_display_fields(dump_path) if dump_path else {}
    )
    class_display_fields = (
        _parse_dump_class_display_fields(dump_path) if dump_path else {}
    )
    class_layout_aliases: dict[str, list[dict[int, str]]] = defaultdict(list)
    for class_name, fields in class_display_fields.items():
        class_layout_aliases[class_name].append(fields)
        class_layout_aliases[class_name.rsplit(".", 1)[-1]].append(fields)
    metadata_component_fields_by_slot: dict[int, dict[int, str]] = {}
    for slot, target in pointer_slots.items():
        component_type = metadata_component_factory_types.get(target)
        if component_type is None:
            continue
        layouts = class_layout_aliases.get(component_type, [])
        unique_layouts = {
            tuple(sorted(layout.items())): layout for layout in layouts
        }
        if len(unique_layouts) == 1:
            metadata_component_fields_by_slot[slot] = dict(
                next(iter(unique_layouts.values()))
            )
    return_object_display_fields = {
        **return_object_factory_display_fields,
        **metadata_component_fields_by_slot,
    }
    if progress_callback:
        progress_callback("ELF 与 dump.cs 索引完成，开始 ARM64 调用链分析。")

    timings["dump_indexes_seconds"] = time.perf_counter() - phase_started
    phase_started = time.perf_counter()
    result = analyze_arm64_display_usage(
        carrier_field_types=_parse_dump_carrier_field_types(dump_path) if dump_path else {},
        code_sections=code_sections,
        memory_sections=memory_sections,
        method_payload=methods,
        addresses=addresses,
        literals=literals,
        slot_to_cell=slot_to_cell,
        inputs={
            "libil2cpp": str(so_path),
            "script_json": str(script_path),
            "stringliteral_json": str(literal_path) if literal_path else None,
            "dump_cs": str(dump_path) if dump_path else None,
        },
        max_wrapper_depth=max_wrapper_depth,
        display_fields_by_method=display_fields_by_method,
        nested_display_fields_by_method=nested_display_fields_by_method,
        virtual_text_slots_by_component=virtual_text_slots_by_component,
        pointer_slots=pointer_slots,
        metadata_method_targets=metadata_method_targets,
        return_object_display_fields=return_object_display_fields,
        enum_type_by_pointer_slot=enum_type_by_pointer_slot,
        enum_members_by_type=enum_members_by_type,
        progress_callback=progress_callback,
    )
    timings["native_analysis_seconds"] = time.perf_counter() - phase_started
    result_stats = dict(result.get("stats", {}))
    result_stats["cache_hit"] = False
    result_stats["source_literal_count"] = len(source_literals)
    result_stats["preclassified_records_match_count"] = (
        len(source_literals) - len(literals)
    )
    result["stats"] = result_stats
    timings["total_seconds"] = time.perf_counter() - started
    result_stats.update(cache_reason=cache_reason, performance=timings)
    if use_cache:
        if _content_fingerprint(fingerprint_paths, fingerprint_options) != fingerprint:
            raise Il2CppDisplayAnalysisError("Analysis inputs changed while running; result was not cached")
        try:
            _write_analysis_cache(cache_target, fingerprint, result)
        except OSError as exc:
            if progress_callback:
                progress_callback(f"分析已完成，但缓存写入失败：{exc}")
    return result


__all__ = [
    "AARCH64_RELATIVE_RELOCATION",
    "Il2CppDisplayAnalysisError",
    "analyze_arm64_display_usage",
    "analyze_il2cpp_display_usage",
]
