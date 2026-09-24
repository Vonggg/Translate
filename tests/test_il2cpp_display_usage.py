from __future__ import annotations

from dataclasses import dataclass

import pytest

import pipeline.il2cpp_display_usage as DISPLAY_USAGE

from pipeline.il2cpp_display_usage import (
    AARCH64_RELATIVE_RELOCATION,
    _collect_relative_string_slots,
    _load_script_metadata_component_factory_types,
    _match_display_sink,
    _parse_dump_class_display_fields,
    _parse_dump_nested_display_fields,
    _parse_dump_enum_members,
    _parse_dump_return_object_display_fields,
    _parse_dump_virtual_text_slots,
    analyze_arm64_display_usage,
)


NOP = 0xD503201F
RET = 0xD65F03C0


def test_abstract_transform_history_is_bounded_to_recent_steps() -> None:
    transforms = tuple(str(index) for index in range(40))
    value = DISPLAY_USAGE.AbstractValue("derived", 7, 11, transforms)

    limited = DISPLAY_USAGE._limit_values([value])

    assert len(limited) == 1
    retained = next(iter(limited))
    assert retained.transforms == transforms[-DISPLAY_USAGE.MAX_ABSTRACT_TRANSFORM_STEPS :]


def test_disassembly_cache_evicts_old_entries_by_instruction_count() -> None:
    cache = DISPLAY_USAGE._BoundedInstructionCache(max_instructions=3)
    cache[1] = ("a", "b")
    cache[2] = ("c", "d")

    assert cache.get(1) is None
    assert cache.get(2) == ("c", "d")
    assert cache.instruction_count == 2
    assert cache.eviction_count == 1


def _words(*values: int) -> bytes:
    return b"".join((value & 0xFFFFFFFF).to_bytes(4, "little") for value in values)


def _adrp(pc: int, target: int, register: int) -> int:
    page_delta = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    encoded = page_delta & ((1 << 21) - 1)
    immlo = encoded & 0x3
    immhi = (encoded >> 2) & 0x7FFFF
    return 0x90000000 | (immlo << 29) | (immhi << 5) | register


def _ldr(register: int, base: int, displacement: int = 0) -> int:
    assert displacement % 8 == 0
    return 0xF9400000 | ((displacement // 8) << 10) | (base << 5) | register


def _ldr_literal(register: int, pc: int, target: int) -> int:
    displacement = target - pc
    assert displacement % 4 == 0
    immediate = (displacement // 4) & 0x7FFFF
    return 0x58000000 | (immediate << 5) | register


def _str(register: int, base: int, displacement: int = 0) -> int:
    assert displacement % 8 == 0
    return 0xF9000000 | ((displacement // 8) << 10) | (base << 5) | register


def _ldr_indexed(register: int, base: int, index: int) -> int:
    return 0xF8606800 | (index << 16) | (base << 5) | register


def _str_indexed(register: int, base: int, index: int) -> int:
    return 0xF8206800 | (index << 16) | (base << 5) | register


def _str_pre(register: int, base: int, displacement: int) -> int:
    assert -256 <= displacement <= 255
    return 0xF8000C00 | ((displacement & 0x1FF) << 12) | (base << 5) | register


def _ldr_post(register: int, base: int, displacement: int) -> int:
    assert -256 <= displacement <= 255
    return 0xF8400400 | ((displacement & 0x1FF) << 12) | (base << 5) | register


def _stp(first: int, second: int, base: int, displacement: int = 0) -> int:
    assert displacement % 8 == 0
    return (
        0xA9000000
        | (((displacement // 8) & 0x7F) << 15)
        | (second << 10)
        | (base << 5)
        | first
    )


def _ldp(first: int, second: int, base: int, displacement: int = 0) -> int:
    assert displacement % 8 == 0
    return (
        0xA9400000
        | (((displacement // 8) & 0x7F) << 15)
        | (second << 10)
        | (base << 5)
        | first
    )


def _stp_pre(first: int, second: int, base: int, displacement: int) -> int:
    assert displacement % 8 == 0
    return (
        0xA9800000
        | (((displacement // 8) & 0x7F) << 15)
        | (second << 10)
        | (base << 5)
        | first
    )


def _ldp_post(first: int, second: int, base: int, displacement: int) -> int:
    assert displacement % 8 == 0
    return (
        0xA8C00000
        | (((displacement // 8) & 0x7F) << 15)
        | (second << 10)
        | (base << 5)
        | first
    )


def _add_immediate(destination: int, source: int, immediate: int) -> int:
    assert 0 <= immediate < 0x1000
    return 0x91000000 | (immediate << 10) | (source << 5) | destination


def _mov(destination: int, source: int) -> int:
    return 0xAA0003E0 | (source << 16) | destination


def _csel(destination: int, when_true: int, when_false: int, condition: int = 0) -> int:
    return (
        0x9A800000
        | (when_false << 16)
        | ((condition & 0xF) << 12)
        | (when_true << 5)
        | destination
    )


def _bl(pc: int, target: int) -> int:
    return 0x94000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def _blr(register: int) -> int:
    return 0xD63F0000 | (register << 5)


def _b(pc: int, target: int) -> int:
    return 0x14000000 | (((target - pc) >> 2) & 0x03FFFFFF)


def _make_section(method_code: dict[int, bytes], *, base: int = 0x1000, end: int = 0x1300) -> bytes:
    data = bytearray(_words(*([NOP] * ((end - base) // 4))))
    for address, code in method_code.items():
        offset = address - base
        data[offset : offset + len(code)] = code
    return bytes(data)


def _method(address: int, name: str, signature: str) -> dict[str, object]:
    return {"Address": address, "Name": name, "Signature": signature}


TMP_SET_TEXT = _method(
    0x9000,
    "TMPro.TMP_Text$$SetText",
    "void TMPro_TMP_Text__SetText (TMPro_TMP_Text_o* __this, System_String_o* sourceText, const MethodInfo* method);",
)

TMP_SET_TEXT_BOOL = _method(
    0x9010,
    "TMPro.TMP_Text$$SetText",
    "void TMPro_TMP_Text__SetText (TMPro_TMP_Text_o* __this, System_String_o* sourceText, bool syncTextInputBox, const MethodInfo* method);",
)

TMP_SET_TEXT_FLOAT = _method(
    0x9020,
    "TMPro.TMP_Text$$SetText",
    "void TMPro_TMP_Text__SetText (TMPro_TMP_Text_o* __this, System_String_o* sourceText, float arg0, const MethodInfo* method);",
)

NGUI_LABEL_SET_TEXT = _method(
    0x9030,
    "UILabel$$set_text",
    "void UILabel__set_text (UILabel_o* __this, System_String_o* value, const MethodInfo* method);",
)

TEXT_MESH_SET_TEXT = _method(
    0x9040,
    "UnityEngine.TextMesh$$set_text",
    "void UnityEngine_TextMesh__set_text (UnityEngine_TextMesh_o* __this, System_String_o* value, const MethodInfo* method);",
)

GUI_BUTTON = _method(
    0x9050,
    "UnityEngine.GUI$$Button",
    "bool UnityEngine_GUI__Button (UnityEngine_Rect_o position, System_String_o* text, const MethodInfo* method);",
)


@dataclass
class _FakeRelocation:
    values: dict[str, int]

    def __getitem__(self, key: str) -> int:
        return self.values[key]


class _FakeRelocationSection:
    def __init__(self, relocations: list[_FakeRelocation]) -> None:
        self._relocations = relocations

    def iter_relocations(self):
        return iter(self._relocations)


class _FakeElf:
    def __init__(self, section: _FakeRelocationSection | None) -> None:
        self._section = section

    def get_section_by_name(self, name: str):
        return self._section if name == ".rela.dyn" else None


def test_relative_relocations_map_usage_slots_to_literal_cells() -> None:
    section = _FakeRelocationSection(
        [
            _FakeRelocation(
                {
                    "r_info_type": AARCH64_RELATIVE_RELOCATION,
                    "r_addend": 0x6000,
                    "r_offset": 0x5000,
                }
            ),
            _FakeRelocation(
                {
                    "r_info_type": 999,
                    "r_addend": 0x6010,
                    "r_offset": 0x5008,
                }
            ),
            _FakeRelocation(
                {
                    "r_info_type": AARCH64_RELATIVE_RELOCATION,
                    "r_addend": 0xDEAD,
                    "r_offset": 0x5010,
                }
            ),
        ]
    )

    assert _collect_relative_string_slots(_FakeElf(section), {0x6000, 0x6010}) == {
        0x5000: 0x6000
    }


def test_unrelated_literal_in_same_method_is_not_selected() -> None:
    start = 0x1000
    slot_unrelated = 0x5010
    slot_displayed = 0x5020
    words = [
        _adrp(start, slot_unrelated, 3),
        _ldr(3, 3, slot_unrelated & 0xFFF),
        _ldr(3, 3),
        _adrp(start + 12, slot_displayed, 8),
        _ldr(8, 8, slot_displayed & 0xFFF),
        _ldr(1, 8),
        _bl(start + 24, 0x9000),
        RET,
    ]
    section = _make_section({start: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[0x1000, 0x1100, 0x9000],
        literals={0x6000: "AnalyticsEvent", 0x6010: "Victory!"},
        slot_to_cell={slot_unrelated: 0x6000, slot_displayed: 0x6010},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Victory!"]
    assert result["derived_influence"] == []


def test_ngui_uilabel_set_text_is_a_display_sink() -> None:
    start = 0x1000
    slot = 0x5010
    words = [
        _adrp(start, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(start + 12, 0x9030),
        RET,
    ]
    section = _make_section({start: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.Hud$$ShowMoney", "void Game_Hud__ShowMoney (Game_Hud_o* __this, const MethodInfo* method);"),
            NGUI_LABEL_SET_TEXT,
        ],
        addresses=[start, 0x1100, 0x9030],
        literals={0x6000: "50"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["50"]
    sink = next(item for item in result["display_sinks"] if item["name"] == "UILabel$$set_text")
    assert sink["string_argument_register"] == "x1"


@pytest.mark.parametrize(
    ("name", "signature"),
    [
        (
            "UnityEngine.UI.InputField$$SetText",
            "void UnityEngine_UI_InputField__SetText (UnityEngine_UI_InputField_o* __this, System_String_o* value, bool sendCallback, const MethodInfo* method);",
        ),
        (
            "TMPro.TMP_InputField$$SetText",
            "void TMPro_TMP_InputField__SetText (TMPro_TMP_InputField_o* __this, System_String_o* value, bool sendCallback, const MethodInfo* method);",
        ),
        (
            "UnityEngine.UIElements.Foldout$$set_text",
            "void UnityEngine_UIElements_Foldout__set_text (UnityEngine_UIElements_Foldout_o* __this, System_String_o* value, const MethodInfo* method);",
        ),
        (
            "UnityEngine.UIElements.BaseField<object>$$set_label",
            "void UnityEngine_UIElements_BaseField_object___set_label (UnityEngine_UIElements_BaseField_TValueType__o* __this, System_String_o* value, const MethodInfo_2AC21FC* method);",
        ),
        (
            "UnityEngine.UIElements.Label$$.ctor",
            "void UnityEngine_UIElements_Label___ctor (UnityEngine_UIElements_Label_o* __this, System_String_o* text, const MethodInfo* method);",
        ),
        (
            "FairyGUI.GTextField$$set_text",
            "void FairyGUI_GTextField__set_text (FairyGUI_GTextField_o* __this, System_String_o* value, const MethodInfo* method);",
        ),
        (
            "UIInput$$set_value",
            "void UIInput__set_value (UIInput_o* __this, System_String_o* value, const MethodInfo* method);",
        ),
    ],
)
def test_extended_runtime_text_sinks_are_recognised(name: str, signature: str) -> None:
    assert _match_display_sink(name, signature)


def test_ui_toolkit_style_string_is_not_a_display_sink() -> None:
    assert not _match_display_sink(
        "UnityEngine.UIElements.VisualElement$$AddToClassList",
        "void UnityEngine_UIElements_VisualElement__AddToClassList (UnityEngine_UIElements_VisualElement_o* __this, System_String_o* className, const MethodInfo* method);",
    )


@pytest.mark.parametrize("display_sink", [TEXT_MESH_SET_TEXT, GUI_BUTTON])
def test_additional_code_display_sinks_are_recognised(display_sink) -> None:
    start = 0x1000
    slot = 0x5010
    words = [
        _adrp(start, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(start + 12, display_sink["Address"]),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({start: _words(*words)}))],
        method_payload=[
            _method(
                start,
                "Game.Screen$$OnGUI",
                "void Game_Screen__OnGUI (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            display_sink,
        ],
        addresses=[start, 0x1100, display_sink["Address"]],
        literals={0x6000: "Visible label"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Visible label"]


def test_custom_delegate_subscription_chain_reaches_display_callback() -> None:
    """A custom delegate Invoke -> Combine callback -> Text setter is proven."""

    subscribe = 0x1000
    invoke = 0x1100
    callback = 0x1200
    action_ctor = 0x9100
    delegate_combine = 0x9110
    object_slot = 0x5000
    method_slot = 0x5010
    text_slot = 0x5020
    metadata_cell = 0x7100

    subscribe_words = [
        _adrp(subscribe, object_slot, 23),
        _ldr(23, 23, object_slot & 0xFFF),
        _ldr(23, 23),
        _ldr(20, 23, 0x40),
        _adrp(subscribe + 16, method_slot, 21),
        _ldr(21, 21, method_slot & 0xFFF),
        _ldr(2, 21),
        _bl(subscribe + 28, action_ctor),
        _mov(1, 0),
        _mov(0, 20),
        _bl(subscribe + 40, delegate_combine),
        _str(0, 23, 0x40),
        RET,
    ]
    invoke_words = [
        _adrp(invoke, object_slot, 8),
        _ldr(8, 8, object_slot & 0xFFF),
        _ldr(8, 8),
        _ldr(8, 8, 0x40),
        _ldr(9, 8, 0x18),
        _ldr(0, 8, 0x40),
        _ldr(3, 8, 0x28),
        _adrp(invoke + 28, text_slot, 2),
        _ldr(2, 2, text_slot & 0xFFF),
        _ldr(2, 2),
        _blr(9),
        RET,
    ]
    callback_words = [_mov(1, 2), _bl(callback + 4, 0x9000), RET]
    section = _make_section(
        {
            subscribe: _words(*subscribe_words),
            invoke: _words(*invoke_words),
            callback: _words(*callback_words),
        }
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(subscribe, "Game.PlayUI$$Start", "void Game_PlayUI__Start (Game_PlayUI_o* __this, const MethodInfo* method);"),
            _method(invoke, "Game.Grass$$FixedUpdate", "void Game_Grass__FixedUpdate (Game_Grass_o* __this, const MethodInfo* method);"),
            _method(callback, "Game.PlayUI$$ShowCount", "void Game_PlayUI__ShowCount (Game_PlayUI_o* __this, int32_t count, System_String_o* type, const MethodInfo* method);"),
            _method(action_ctor, "Game.ShowCountDelegate$$.ctor", "void Game_ShowCountDelegate___ctor (Game_ShowCountDelegate_o* __this, Il2CppObject* target, intptr_t method, const MethodInfo* method);"),
            _method(delegate_combine, "System.Delegate$$Combine", "System_Delegate_o* System_Delegate__Combine (System_Delegate_o* a, System_Delegate_o* b, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[subscribe, invoke, callback, 0x1300, action_ctor, delegate_combine, 0x9000],
        literals={0x6000: "Slice"},
        slot_to_cell={text_slot: 0x6000},
        pointer_slots={object_slot: 0x7000, method_slot: metadata_cell},
        metadata_method_targets={metadata_cell: callback},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Slice"]
    evidence = result["exact_literals"][0]["evidence"]
    assert evidence[0]["path"][0] == "delegate invocation"
    assert result["stats"]["delegate_subscription_field_count"] == 1


@pytest.mark.parametrize("atomic_subscription", ["store", "cas", "cas_tail", "not_cas"])
def test_generated_add_event_method_is_followed_across_methods(atomic_subscription) -> None:
    """Delegate.Combine inside add_Event is connected to its subscribing caller."""

    add_event = 0x1000
    subscribe = 0x1100
    invoke = 0x1200
    callback = 0x1300
    action_ctor = 0x9100
    delegate_combine = 0x9110
    object_slot = 0x5000
    method_slot = 0x5010
    text_slot = 0x5020
    metadata_cell = 0x7100

    add_words = [
        _mov(19, 0),
        _ldr(0, 0, 0x40),
        _bl(add_event + 8, delegate_combine),
        _str(0, 19, 0x40),
        RET,
    ]
    wrapper, atomic = 0xA000, 0xA100
    native_sections = []
    if atomic_subscription != "store":
        # ldr x21, [x19, #0x40]! leaves a field ADDRESS in x19,
        # not the field value. A native cast follows Delegate.Combine.
        add_words = [
            _mov(19, 0), 0xF8440E75, _mov(0, 21),
            _bl(add_event + 12, delegate_combine),
            _bl(add_event + 16, 0xB000), _mov(1, 0),
            _mov(0, 19), _mov(2, 21), _bl(add_event + 32, wrapper), RET,
        ]
        native_sections = [
            (wrapper, _words(0xF81E0FFE, 0xA9014FF4, _mov(20, 0), _mov(19, 2),
                             _mov(0, 2), _mov(2, 20), _bl(wrapper + 24, atomic),
                             0xEB13001F, 0xD5033BBF, 0x9A800273, _mov(0, 20),
                             _bl(wrapper + 44, 0xB100), _mov(0, 19),
                             0xA9414FF4, 0xF84207FE, RET)),
            (atomic, _words(0xD503245F, _adrp(atomic + 4, 0xC000, 16),
                            0x39600210, 0x34000070,
                            0xC8E0FC41 if atomic_subscription != "not_cas" else 0xD503201F,
                            RET, _mov(16, 0), 0xC85FFC40, 0xEB10001F,
                            0x54000061, 0xC811FC41, 0x35FFFF91, RET)),
        ]
    subscribe_words = [
        _adrp(subscribe, object_slot, 23),
        _ldr(23, 23, object_slot & 0xFFF),
        _ldr(23, 23),
        _ldr(21, 23, 0x50),
        _mov(0, 21),
        _adrp(subscribe + 20, method_slot, 8),
        _ldr(8, 8, method_slot & 0xFFF),
        _ldr(2, 8),
        _bl(subscribe + 32, action_ctor),
        _mov(1, 21),
        _mov(0, 23),
        _bl(subscribe + 44, add_event),
        RET,
    ]
    invoke_words = [
        _adrp(invoke, object_slot, 8),
        _ldr(8, 8, object_slot & 0xFFF),
        _ldr(8, 8),
        _ldr(8, 8, 0x40),
        _ldr(9, 8, 0x18),
        _ldr(0, 8, 0x40),
        _ldr(3, 8, 0x28),
        _adrp(invoke + 28, text_slot, 1),
        _ldr(1, 1, text_slot & 0xFFF),
        _ldr(1, 1),
        _blr(9),
        RET,
    ]
    if atomic_subscription == "cas_tail":
        # UI.this -> handler field -> tail add_Message; Raise receives the
        # handler itself. The owning UI type must not become the event key.
        subscribe_words[:3] = [_mov(23, 0), _ldr(23, 23, 0x40), 0xD503201F]
        subscribe_words[11] = _b(subscribe + 44, add_event)
        invoke_words[:3] = [_mov(8, 0), 0xD503201F, 0xD503201F]
    callback_words = [_bl(callback, 0x9000), RET]
    section = _make_section(
        {
            add_event: _words(*add_words),
            subscribe: _words(*subscribe_words),
            invoke: _words(*invoke_words),
            callback: _words(*callback_words),
        },
        end=0x1400,
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section), *native_sections],
        method_payload=[
            _method(add_event, "Game.EventBus$$add_Message", "void Game_EventBus__add_Message (Game_EventBus_o* __this, Game_MessageDelegate_o* value, const MethodInfo* method);"),
            _method(subscribe, "Game.PlayUI$$Start", "void Game_PlayUI__Start (Game_PlayUI_o* __this, const MethodInfo* method);"),
            _method(invoke, "Game.EventBus$$Raise", "void Game_EventBus__Raise (Game_EventBus_o* __this, const MethodInfo* method);"),
            _method(callback, "Game.PlayUI$$Show", "void Game_PlayUI__Show (Game_PlayUI_o* __this, System_String_o* value, const MethodInfo* method);"),
            _method(action_ctor, "Game.MessageDelegate$$.ctor", "void Game_MessageDelegate___ctor (Game_MessageDelegate_o* __this, Il2CppObject* target, intptr_t method, const MethodInfo* method);"),
            _method(delegate_combine, "System.Delegate$$Combine", "System_Delegate_o* System_Delegate__Combine (System_Delegate_o* a, System_Delegate_o* b, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[add_event, subscribe, invoke, callback, 0x1400, action_ctor, delegate_combine, 0x9000],
        literals={0x6000: "Cross-method event"},
        slot_to_cell={text_slot: 0x6000},
        pointer_slots={object_slot: 0x7000, method_slot: metadata_cell},
        metadata_method_targets={metadata_cell: callback},
    )

    if atomic_subscription == "not_cas":
        assert not result["exact_literals"]
        assert result["stats"]["delegate_add_method_count"] == 0
        return
    assert [item["value"] for item in result["exact_literals"]] == [
        "Cross-method event"
    ]
    assert result["stats"]["delegate_add_method_count"] == 1


def test_static_delegate_field_behind_typeinfo_is_followed_across_cast() -> None:
    """A static Action field survives type-info indirection and castclass."""

    subscribe = 0x1000
    caller = 0x1080
    invoke = 0x1100
    callback = 0x1200
    action_ctor = 0x9100
    delegate_combine = 0x9110
    castclass = 0x9120
    typeinfo_slot = 0x5000
    delegate_class_slot = 0x5010
    method_slot = 0x5020
    text_slot = 0x5030
    typeinfo = 0x7000
    delegate_class = 0x7010
    metadata_cell = 0x7020

    subscribe_words = [
        _adrp(subscribe, typeinfo_slot, 23),
        _ldr(23, 23, typeinfo_slot & 0xFFF),
        _adrp(subscribe + 8, delegate_class_slot, 27),
        _ldr(27, 27, delegate_class_slot & 0xFFF),
        _ldr(8, 23),
        _ldr(8, 8, 0xB8),
        _ldr(20, 8, 0x70),
        _ldr(0, 27),
        _bl(subscribe + 32, 0x9200),
        _adrp(subscribe + 36, method_slot, 8),
        _ldr(8, 8, method_slot & 0xFFF),
        _mov(1, 19),
        _mov(21, 0),
        _ldr(2, 8),
        _bl(subscribe + 56, action_ctor),
        _mov(0, 20),
        _mov(1, 21),
        _bl(subscribe + 68, delegate_combine),
        _mov(20, 0),
        _ldr(1, 27),
        _bl(subscribe + 80, castclass),
        _ldr(8, 23),
        _ldr(21, 8, 0xB8),
        _str(0, 21, 0x70),
        RET,
    ]
    caller_words = [
        _adrp(caller, text_slot, 0),
        _ldr(0, 0, text_slot & 0xFFF),
        _ldr(0, 0),
        _bl(caller + 12, invoke),
        RET,
    ]
    invoke_words = [
        _mov(19, 0),
        _adrp(invoke, typeinfo_slot, 21),
        _ldr(21, 21, typeinfo_slot & 0xFFF),
        _ldr(8, 21),
        _ldr(8, 8, 0xB8),
        _ldr(8, 8, 0x70),
        _ldr(3, 8, 0x18),
        _ldr(0, 8, 0x40),
        _ldr(2, 8, 0x28),
        _mov(1, 19),
        _blr(3),
        RET,
    ]
    callback_words = [_bl(callback, 0x9000), RET]
    section = _make_section(
        {
            subscribe: _words(*subscribe_words),
            caller: _words(*caller_words),
            invoke: _words(*invoke_words),
            callback: _words(*callback_words),
        },
        end=0x1300,
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(subscribe, "Game.MainScene$$Awake", "void Game_MainScene__Awake (Game_MainScene_o* __this, const MethodInfo* method);"),
            _method(caller, "Game.MainScene$$OpenLevel", "void Game_MainScene__OpenLevel (Game_MainScene_o* __this, const MethodInfo* method);"),
            _method(invoke, "Game.Controller$$ShowNotice", "void Game_Controller__ShowNotice (System_String_o* message, const MethodInfo* method);"),
            _method(callback, "Game.MainScene$$OnNotice", "void Game_MainScene__OnNotice (Game_MainScene_o* __this, System_String_o* message, const MethodInfo* method);"),
            _method(action_ctor, "System.Action<object>$$.ctor", "void System_Action_object____ctor (System_Action_object__o* __this, Il2CppObject* target, intptr_t method, const MethodInfo* method);"),
            _method(delegate_combine, "System.Delegate$$Combine", "System_Delegate_o* System_Delegate__Combine (System_Delegate_o* a, System_Delegate_o* b, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[subscribe, caller, invoke, callback, 0x1300, action_ctor, delegate_combine, castclass, 0x9200, 0x9000],
        literals={0x6000: "UNLOCK AT LEVEL {0}"},
        slot_to_cell={text_slot: 0x6000},
        pointer_slots={
            typeinfo_slot: typeinfo,
            delegate_class_slot: delegate_class,
            method_slot: metadata_cell,
        },
        metadata_method_targets={metadata_cell: callback},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "UNLOCK AT LEVEL {0}"
    ]


def test_csel_and_cfg_backedge_preserve_both_displayed_literals() -> None:
    start = 0x1000
    cold = 0x1020
    slot_win = 0x5010
    slot_lose = 0x5020
    prefix = [
        _b(start, cold),
        _ldr(1, 8),
        _bl(start + 8, 0x9000),
        RET,
        NOP,
        NOP,
        NOP,
        NOP,
    ]
    cold_words = [
        _adrp(cold, slot_win, 9),
        _ldr(9, 9, slot_win & 0xFFF),
        _adrp(cold + 8, slot_lose, 10),
        _ldr(10, 10, slot_lose & 0xFFF),
        _csel(8, 9, 10),
        _b(cold + 20, start + 4),
    ]
    section = _make_section({start: _words(*(prefix + cold_words))})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.EndScreen$$Show", "void Game_EndScreen__Show (Game_EndScreen_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[0x1000, 0x1100, 0x9000],
        literals={0x6000: "Victory!", 0x6010: "You lose!"},
        slot_to_cell={slot_win: 0x6000, slot_lose: 0x6010},
    )

    assert {item["value"] for item in result["exact_literals"]} == {
        "Victory!",
        "You lose!",
    }


def test_format_result_is_derived_and_never_exact() -> None:
    start = 0x1000
    format_method = 0x8000
    slot = 0x5010
    words = [
        _adrp(start, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(0, 8),
        _bl(start + 12, format_method),
        _mov(1, 0),
        _bl(start + 20, 0x9000),
        RET,
    ]
    section = _make_section({start: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.Counter$$Show", "void Game_Counter__Show (Game_Counter_o* __this, const MethodInfo* method);"),
            _method(
                format_method,
                "System.String$$Format",
                "System_String_o* System_String__Format (System_String_o* format, System_Object_o* arg0, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[0x1000, 0x1100, format_method, 0x9000],
        literals={0x6000: "Level {0}"},
        slot_to_cell={slot: 0x6000},
    )

    assert result["exact_literals"] == []
    assert [item["value"] for item in result["derived_influence"]] == ["Level {0}"]
    assert result["derived_influence"][0]["evidence"][0]["transforms"] == ["Format"]


def test_direct_wrapper_parameter_summary_handles_stack_round_trip() -> None:
    caller = 0x1000
    wrapper = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _str(1, 31, 8),
        _ldr(3, 31, 8),
        _stp(3, 2, 31, 16),
        _ldp(1, 2, 31, 16),
        _bl(wrapper + 16, 0x9000),
        RET,
    ]
    section = _make_section(
        {
            caller: _words(*caller_words),
            wrapper: _words(*wrapper_words),
        }
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            _method(
                wrapper,
                "Game.TextHelpers$$SetLabel",
                "void Game_TextHelpers__SetLabel (TMPro_TMP_Text_o* label, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, 0x1200, 0x9000],
        literals={0x6000: "Wrapped text"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Wrapped text"]
    assert result["stats"]["wrapper_sink_count"] == 1
    assert result["exact_literals"][0]["evidence"][0]["path"] == [
        "Game.TextHelpers$$SetLabel",
        "TMPro.TMP_Text$$SetText",
    ]


def test_wrapper_string_after_float_still_uses_next_gpr() -> None:
    caller = 0x1000
    wrapper = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _bl(wrapper, 0x9000),
        RET,
    ]
    section = _make_section({caller: _words(*caller_words), wrapper: _words(*wrapper_words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            _method(
                wrapper,
                "Game.TextHelpers$$SetLabel",
                "void Game_TextHelpers__SetLabel (TMPro_TMP_Text_o* label, float amount, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, 0x1200, 0x9000],
        literals={0x6000: "Float-safe wrapper"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Float-safe wrapper"
    ]
    wrapper_sink = next(
        item for item in result["display_sinks"] if item["name"] == "TMPro.TMP_Text$$SetText"
    )
    assert wrapper_sink["string_argument_register"] == "x1"


def test_transform_reads_allocated_gprs_and_excludes_method_info_register() -> None:
    start = 0x1000
    format_method = 0x8000
    format_slot = 0x5010
    suffix_slot = 0x5020
    unrelated_slot = 0x5030
    words = [
        _adrp(start, format_slot, 8),
        _ldr(8, 8, format_slot & 0xFFF),
        _ldr(0, 8),
        _adrp(start + 12, suffix_slot, 8),
        _ldr(8, 8, suffix_slot & 0xFFF),
        _ldr(1, 8),
        _adrp(start + 24, unrelated_slot, 8),
        _ldr(8, 8, unrelated_slot & 0xFFF),
        _ldr(2, 8),
        _bl(start + 36, format_method),
        _mov(1, 0),
        _bl(start + 44, 0x9000),
        RET,
    ]
    section = _make_section({start: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.Score$$Show", "void Game_Score__Show (Game_Score_o* __this, const MethodInfo* method);"),
            _method(
                format_method,
                "System.String$$Format",
                "System_String_o* System_String__Format (System_String_o* format, float amount, System_String_o* suffix, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[start, 0x1100, format_method, 0x9000],
        literals={
            0x6000: "Score: {0}",
            0x6010: " points",
            0x6020: "MethodInfo lookalike",
        },
        slot_to_cell={
            format_slot: 0x6000,
            suffix_slot: 0x6010,
            unrelated_slot: 0x6020,
        },
    )

    assert {item["value"] for item in result["derived_influence"]} == {
        "Score: {0}",
        " points",
    }


def test_preindexed_stp_writeback_preserves_wrapper_parameter() -> None:
    caller = 0x1000
    wrapper = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _stp_pre(1, 2, 31, -16),
        _ldr(1, 31),
        _ldp_post(1, 2, 31, 16),
        _bl(wrapper + 12, 0x9000),
        RET,
    ]
    section = _make_section({caller: _words(*caller_words), wrapper: _words(*wrapper_words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            _method(
                wrapper,
                "Game.TextHelpers$$SetLabel",
                "void Game_TextHelpers__SetLabel (TMPro_TMP_Text_o* label, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, 0x1200, 0x9000],
        literals={0x6000: "Pre-indexed stack"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Pre-indexed stack"
    ]


def test_single_store_and_load_writeback_preserve_wrapper_parameter() -> None:
    caller = 0x1000
    wrapper = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _str_pre(1, 31, -16),
        _ldr_post(1, 31, 16),
        _bl(wrapper + 8, 0x9000),
        RET,
    ]
    section = _make_section({caller: _words(*caller_words), wrapper: _words(*wrapper_words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            _method(
                wrapper,
                "Game.TextHelpers$$SetLabel",
                "void Game_TextHelpers__SetLabel (TMPro_TMP_Text_o* label, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, 0x1200, 0x9000],
        literals={0x6000: "Single writeback stack"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Single writeback stack"
    ]


def test_frame_pointer_relative_stack_round_trip_preserves_wrapper_parameter() -> None:
    caller = 0x1000
    wrapper = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _stp_pre(29, 30, 31, -16),
        _add_immediate(29, 31, 0),
        _str(1, 29, 8),
        _ldr(1, 29, 8),
        _bl(wrapper + 16, 0x9000),
        _ldp_post(29, 30, 31, 16),
        RET,
    ]
    section = _make_section({caller: _words(*caller_words), wrapper: _words(*wrapper_words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            _method(
                wrapper,
                "Game.TextHelpers$$SetLabel",
                "void Game_TextHelpers__SetLabel (TMPro_TMP_Text_o* label, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, 0x1200, 0x9000],
        literals={0x6000: "Frame pointer stack"},
        slot_to_cell={slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Frame pointer stack"
    ]


def test_non_sp_ldp_resolves_adjacent_relocation_slots() -> None:
    start = 0x1000
    first_slot = 0x5010
    second_slot = first_slot + 8
    words = [
        _adrp(start, first_slot, 8),
        _add_immediate(8, 8, first_slot & 0xFFF),
        _ldp(20, 21, 8),
        _ldr(1, 20),
        _bl(start + 16, 0x9000),
        _ldr(1, 21),
        _bl(start + 24, 0x9000),
        RET,
    ]
    section = _make_section({start: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(start, "Game.Screen$$Show", "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[start, 0x1100, 0x9000],
        literals={0x6000: "First pair", 0x6010: "Second pair"},
        slot_to_cell={first_slot: 0x6000, second_slot: 0x6010},
    )

    assert {item["value"] for item in result["exact_literals"]} == {
        "First pair",
        "Second pair",
    }


@pytest.mark.parametrize(
    ("method_name", "receiver_type"),
    [
        ("TMPro.TextMeshPro$$GetTextInfo", "TMPro_TextMeshPro_o*"),
        ("TMPro.TextMeshProUGUI$$GetTextInfo", "TMPro_TextMeshProUGUI_o*"),
    ],
)
def test_framework_measurement_api_is_not_promoted_to_display_wrapper(
    method_name: str, receiver_type: str
) -> None:
    caller = 0x1000
    get_text_info = 0x1100
    slot = 0x5010
    caller_words = [
        _adrp(caller, slot, 8),
        _ldr(8, 8, slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, get_text_info),
        RET,
    ]
    measurement_words = [
        _bl(get_text_info, 0x9000),
        RET,
    ]
    section = _make_section(
        {
            caller: _words(*caller_words),
            get_text_info: _words(*measurement_words),
        }
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(caller, "Game.Layout$$Measure", "void Game_Layout__Measure (Game_Layout_o* __this, const MethodInfo* method);"),
            _method(
                get_text_info,
                method_name,
                f"TMPro_TMP_TextInfo_o* GetTextInfo ({receiver_type} __this, System_String_o* text, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, get_text_info, 0x1200, 0x9000],
        literals={0x6000: "Measurement only"},
        slot_to_cell={slot: 0x6000},
    )

    assert result["exact_literals"] == []
    assert result["derived_influence"] == []
    assert result["stats"]["wrapper_sink_count"] == 0


def test_set_text_float_overload_is_derived_but_bool_overload_is_exact() -> None:
    float_caller = 0x1000
    bool_caller = 0x1100
    float_slot = 0x5010
    bool_slot = 0x5020
    float_words = [
        _adrp(float_caller, float_slot, 8),
        _ldr(8, 8, float_slot & 0xFFF),
        _ldr(1, 8),
        _bl(float_caller + 12, 0x9020),
        RET,
    ]
    bool_words = [
        _adrp(bool_caller, bool_slot, 8),
        _ldr(8, 8, bool_slot & 0xFFF),
        _ldr(1, 8),
        _bl(bool_caller + 12, 0x9010),
        RET,
    ]
    section = _make_section(
        {
            float_caller: _words(*float_words),
            bool_caller: _words(*bool_words),
        }
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(float_caller, "Game.Score$$Show", "void Game_Score__Show (Game_Score_o* __this, const MethodInfo* method);"),
            _method(bool_caller, "Game.Label$$Show", "void Game_Label__Show (Game_Label_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT_BOOL,
            TMP_SET_TEXT_FLOAT,
        ],
        addresses=[float_caller, bool_caller, 0x1200, 0x9010, 0x9020],
        literals={0x6000: "Score: {0}", 0x6010: "Exact label"},
        slot_to_cell={float_slot: 0x6000, bool_slot: 0x6010},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Exact label"]
    assert [item["value"] for item in result["derived_influence"]] == ["Score: {0}"]
    assert result["derived_influence"][0]["evidence"][0]["transforms"] == [
        "SetTextFormat"
    ]


def test_virtual_text_component_call_is_traced_from_dump_field_type() -> None:
    """IL2CPP commonly invokes Text.set_text through a vtable ``blr`` call."""

    caller = 0x1000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _ldr(20, 0, 0x28),  # this.ScoreText: UnityEngine.UI.Text
        _ldr(9, 20),
        _ldr(9, 9, 0x5E8),
        _mov(0, 20),
        _blr(9),
        RET,
    ]
    section = _make_section({caller: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                caller,
                "Game.ScoreView$$Refresh",
                "void Game_ScoreView__Refresh (Game_ScoreView_o* __this, const MethodInfo* method);",
            )
        ],
        addresses=[caller, 0x1100],
        literals={0x6000: "Score: "},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={caller: {0x28: "UnityEngine.UI.Text"}},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Score: "]
    evidence = result["exact_literals"][0]["evidence"][0]
    assert evidence["sink_name"] == "UnityEngine.UI.Text$$virtual_set_text"
    assert result["stats"]["virtual_component_candidate_method_count"] == 1


@pytest.mark.parametrize("tail_call", [False, True])
def test_virtual_text_component_slot_is_not_fixed_to_one_tmp_version(tail_call) -> None:
    caller = 0x1000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _ldr(20, 0, 0x28),
        _ldr(8, 20),
        _ldr(9, 8, 0x558),
        _ldr(2, 8, 0x560),  # adjacent IL2CPP MethodInfo cell
        _mov(0, 20),
        (0xD61F0000 | (9 << 5)) if tail_call else _blr(9),
        RET,
    ]
    section = _make_section({caller: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                caller,
                "Game.ScoreView$$Refresh",
                "void Game_ScoreView__Refresh (Game_ScoreView_o* __this, const MethodInfo* method);",
            )
        ],
        addresses=[caller, 0x1100],
        literals={0x6000: "Events unlocked"},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={caller: {0x28: "TMPro.TMP_Text"}},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Events unlocked"
    ]


@pytest.mark.parametrize("slot,expected", [(0x558, True), (0x578, False)])
def test_virtual_tail_setter_requires_verified_slot(slot, expected):
    caller, literal_slot = 0x1000, 0x5010
    section = _make_section({caller: _words(
        _adrp(caller, literal_slot, 8), _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8), _ldr(0, 0, 0x30), _ldr(8, 0),
        _ldr(3, 8, slot), _ldr(2, 8, slot + 8),
        0xD61F0000 | (3 << 5),  # BR x3, no return/fallthrough
    )})
    result = analyze_arm64_display_usage(
        code_sections=[(caller, section)],
        method_payload=[_method(caller, "Game.Popup$$Refresh",
            "void Game_Popup__Refresh (Game_Popup_o* __this, const MethodInfo* method);")],
        addresses=[caller, 0x1100], literals={0x6000: "GATLING GUN"},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={caller: {0x30: "TMPro.TMP_Text"}},
        virtual_text_slots_by_component={"TMPro.TMP_Text": frozenset({0x558})},
    )
    assert bool(result["exact_literals"]) is expected


@pytest.mark.parametrize('clobbered', [False, True])
def test_switch_table_index_copy_preserves_bound_but_arithmetic_does_not(clobbered) -> None:
    start, case, slot = 0x1000, 0x1040, 0x5010
    adr_pc = start + 20
    adr_delta = case - adr_pc
    section = _make_section({
        start: _words(
            0x7100001F | (1 << 10) | (22 << 5),  # cmp w22, #1
            0x2A0003E0 | (22 << 16) | 8,       # mov w8, w22
            (0x11000000 | (1 << 10) | (8 << 5) | 8) if clobbered else NOP,
            _adrp(start + 12, 0x7000, 9), _add_immediate(9, 9, 0),
            0x10000000 | ((adr_delta & 3) << 29) | ((adr_delta >> 2) << 5) | 10,
            0x38606800 | (8 << 16) | (9 << 5) | 11,  # ldrb w11, [x9,x8]
            0x8B000000 | (11 << 16) | (2 << 10) | (10 << 5) | 10,
            0xD61F0000 | (10 << 5), RET,
        ),
        case: _words(_adrp(case, slot, 8), _ldr(8, 8, slot & 0xFFF), _ldr(1, 8), _bl(case + 12, 0x9000), RET),
    }, end=0x1100)
    result = analyze_arm64_display_usage(
        code_sections=[(start, section)], memory_sections=[(start, section), (0x7000, b'\0\0')],
        method_payload=[_method(start, 'Panel$$Show', 'void Panel__Show (Panel_o* __this, const MethodInfo* method);'), TMP_SET_TEXT],
        addresses=[start, 0x1100, 0x9000], literals={0x6000: 'Copper'}, slot_to_cell={slot: 0x6000},
    )
    assert bool(result['exact_literals']) is not clobbered


def test_bounded_native_string_pointer_table_reaches_display() -> None:
    caller, table = 0x1000, 0x7000
    words = _words(
        _ldr(8, 0, 0x10),
        0x7100001F | (3 << 10) | (8 << 5),  # cmp w8, #3
        _adrp(caller + 8, table, 9),
        _add_immediate(9, 9, table & 0xFFF),
        _ldr_indexed(1, 9, 8) | 0x1000,  # ldr x1, [x9, x8, lsl #3]
        _ldr(1, 1),
        _bl(caller + 24, 0x9000),
        RET,
    )
    result = analyze_arm64_display_usage(
        code_sections=[(caller, _make_section({caller: words}, end=0x1100))],
        method_payload=[
            _method(caller, 'PlayerData$$Show', 'void PlayerData__Show (PlayerData_o* __this, const MethodInfo* method);'),
            TMP_SET_TEXT,
        ],
        addresses=[caller, 0x1100, 0x9000],
        literals={0x6000: 'Selina', 0x6010: 'Devil', 0x6020: 'Lifeline', 0x6030: 'Sunshine'},
        slot_to_cell={},
        pointer_slots={table: 0x6000, table + 8: 0x6010, table + 16: 0x6020, table + 24: 0x6030},
    )
    assert {row['value'] for row in result['exact_literals']} == {'Selina', 'Devil', 'Lifeline', 'Sunshine'}


def test_freshly_constructed_concrete_model_field_connects_to_its_writer() -> None:
    ui, constructor, writer, allocator, slot = 0x1000, 0x1100, 0x1200, 0x1300, 0x5010
    section = _make_section({
        ui: _words(_bl(ui, allocator), _mov(20, 0), _mov(0, 20), _bl(ui + 12, constructor),
                   _ldr(1, 20, 0x18), _bl(ui + 20, 0x9000), RET),
        constructor: _words(RET),
        writer: _words(_adrp(writer, slot, 8), _ldr(8, 8, slot & 0xFFF), _ldr(1, 8),
                       _str(1, 0, 0x18), RET),
    }, end=0x1400)
    result = analyze_arm64_display_usage(
        code_sections=[(ui, section)],
        method_payload=[
            _method(ui, 'Panel$$Show', 'void Panel__Show (Panel_o* __this, const MethodInfo* method);'),
            _method(constructor, 'Data.Skin$$.ctor', 'void Data_Skin___ctor (Data_Skin_o* __this, const MethodInfo* method);'),
            _method(writer, 'Data.Skin$$SetDefaults', 'void Data_Skin__SetDefaults (Data_Skin_o* __this, const MethodInfo* method);'),
            _method(allocator, 'Native$$Allocate', 'void* Native__Allocate (const MethodInfo* method);'),
            TMP_SET_TEXT,
        ],
        addresses=[ui, constructor, writer, allocator, 0x1400, 0x9000],
        literals={0x6000: 'Devil'}, slot_to_cell={slot: 0x6000},
    )
    assert [row['value'] for row in result['exact_literals']] == ['Devil']


def test_carrier_layout_parser_rejects_ambiguous_and_static_fields(tmp_path) -> None:
    dump = tmp_path / 'dump.cs'
    dump.write_text('''// Namespace: A
public class Record // TypeDefIndex: 1
{
    // Fields
    public string text; // 0x18
}
// Namespace: B
public class Record // TypeDefIndex: 2
{
    // Fields
    public string text; // 0x18
}
// Namespace:
public class Panel // TypeDefIndex: 3
{
    // Fields
    private A.Record model; // 0x60
    private Record ambiguous; // 0x68
    private static A.Record global; // 0x0
    private string[] names; // 0x70
    private List<string> selected; // 0x78
    private static string[] globalNames; // 0x8
}
''', encoding='utf-8')
    layouts = DISPLAY_USAGE._parse_dump_carrier_field_types(dump)
    assert layouts[('panel',)] == {
        0x60: ('a', 'record'),
        0x70: ('__string_collection__',),
        0x78: ('__string_collection__',),
    }


@pytest.mark.parametrize('through_field', [False, True])
@pytest.mark.parametrize('transformed', [False, True])
@pytest.mark.parametrize('unrelated_owner', ['Internal.Record', 'Data.Record.Internal'])
def test_typed_lookup_return_connects_model_constructor_to_virtual_text_setter(through_field, transformed, unrelated_owner) -> None:
    consumer, writer, caller, lookup, unrelated = 0x1000, 0x1100, 0x1200, 0x1300, 0x1400
    slot = 0x5010
    section = _make_section({
        consumer: _words(
            _mov(19, 0), _ldr(0, 19, 0x60) if through_field else _bl(consumer + 4, lookup), _mov(20, 0),
            _ldr(0, 19, 0x58), _ldr(8, 0), _ldr(9, 8, 0x558),
            _ldr(1, 20, 0x18),
            *([_bl(consumer + 28, 0x1600), _mov(1, 0), _ldr(0, 19, 0x58), _ldr(8, 0), _ldr(9, 8, 0x558)] if transformed else []),
            _blr(9), RET),
        writer: _words(_str_pre(2, 0, 0x18), RET),
        unrelated: _words(_str(2, 0, 0x18), RET),
        caller: _words(
            _adrp(caller, slot, 8), _ldr(8, 8, slot & 0xFFF), _ldr(2, 8),
            _bl(caller + 12, writer),
            _adrp(caller + 16, slot + 8, 8), _ldr(8, 8, (slot + 8) & 0xFFF), _ldr(2, 8),
            _bl(caller + 28, unrelated), RET),
        lookup: _words(RET),
    }, end=0x1500)
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(consumer, 'Panel$$Refresh', 'void Panel__Refresh (Panel_o* __this, const MethodInfo* method);'),
            _method(writer, 'Data.Record$$.ctor', 'void Data_Record___ctor (Data_Record_o* __this, int32_t id, System_String_o* text, const MethodInfo* method);'),
            _method(unrelated, unrelated_owner + '$$.ctor', f'void Internal_Record___ctor ({unrelated_owner.replace(".", "_")}_o* __this, int32_t id, System_String_o* text, const MethodInfo* method);'),
            _method(caller, 'Data$$Load', 'void Data__Load (Data_o* __this, const MethodInfo* method);'),
            _method(lookup, 'Data$$Lookup', 'Data_Record_o* Data__Lookup (Data_o* __this, const MethodInfo* method);'),
            _method(0x1600, 'System.String$$Concat', 'System_String_o* System_String__Concat (System_String_o* a, System_String_o* b, const MethodInfo* method);'),
        ], addresses=[consumer, writer, caller, lookup, unrelated, 0x1500, 0x1600],
        literals={0x6000: 'An arbitrary objective without a number', 0x6008: 'internal_key'},
        slot_to_cell={slot: 0x6000, slot + 8: 0x6008},
        display_fields_by_method={consumer: {0x58: 'UnityEngine.UI.Text'}},
        carrier_field_types={('panel',): {0x60: ('data', 'record')}},
    )
    rows = result['derived_influence' if transformed else 'exact_literals']
    assert [row['value'] for row in rows] == ['An arbitrary objective without a number']
    if not through_field:
        assert any('Data$$Lookup returns Data_Record_o*' in e['path']
                   for e in rows[0]['evidence'])


def test_persisted_model_string_field_reaches_later_popup_consumer() -> None:
    consumer = 0x1000
    writer = 0x1100
    wrapper = 0x1200
    caller = 0x1300
    literal_slot = 0x5010
    section = _make_section(
        {
            consumer: _words(
                _mov(21, 1),
                _ldr(20, 0, 0x48),
                _ldr(8, 20),
                _ldr(9, 8, 0x558),
                _ldr(2, 8, 0x560),
                _ldr(1, 21, 0x38),
                _mov(0, 20),
                _blr(9),
                RET,
            ),
            writer: _words(
                _ldr(0, 0, 0x18),
                _str(1, 0, 0x38),
                RET,
            ),
            wrapper: _words(
                _mov(1, 0),
                _bl(wrapper + 4, writer),
                RET,
            ),
            caller: _words(
                _adrp(caller, literal_slot, 8),
                _ldr(8, 8, literal_slot & 0xFFF),
                _ldr(0, 8),
                _bl(caller + 12, wrapper),
                RET,
            ),
        },
        end=0x1500,
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                consumer,
                "PopupUI$$Open",
                "void PopupUI__Open (PopupUI_o* __this, Confirmable_o* model, const MethodInfo* method);",
            ),
            _method(
                writer,
                "Confirmable.BaseBuilder<object, object>$$SetDescription",
                "Il2CppObject* Confirmable_BaseBuilder__SetDescription (Confirmable_BaseBuilder_o* __this, System_String_o* description, const MethodInfo* method);",
            ),
            _method(
                wrapper,
                "PopupProvider$$Single",
                "void PopupProvider__Single (System_String_o* description, const MethodInfo* method);",
            ),
            _method(
                caller,
                "MainMenu$$OpenEvents",
                "void MainMenu__OpenEvents (MainMenu_o* __this, const MethodInfo* method);",
            ),
        ],
        addresses=[consumer, writer, wrapper, caller, 0x1400],
        literals={0x6000: "Events unlock after level "},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={consumer: {0x48: "TMPro.TMP_Text"}},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Events unlock after level "
    ]
    evidence = result["exact_literals"][0]["evidence"][0]
    assert "persisted confirmable field +0x18.+0x38" in evidence["path"]
    assert result["stats"]["carrier_field_sink_count"] == 1


def test_literal_written_directly_to_model_field_reaches_consumer() -> None:
    consumer = 0x1000
    producer = 0x1100
    literal_slot = 0x5010
    section = _make_section(
        {
            consumer: _words(
                _mov(21, 1),
                _ldr(20, 0, 0x48),
                _ldr(8, 20),
                _ldr(9, 8, 0x558),
                _ldr(2, 8, 0x560),
                _ldr(1, 21, 0x38),
                _mov(0, 20),
                _blr(9),
                RET,
            ),
            producer: _words(
                _adrp(producer, literal_slot, 8),
                _ldr(8, 8, literal_slot & 0xFFF),
                _ldr(8, 8),
                _str(8, 0, 0x38),
                RET,
            ),
        }
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                consumer,
                "PopupUI$$Open",
                "void PopupUI__Open (PopupUI_o* __this, Confirmable_o* model, const MethodInfo* method);",
            ),
            _method(
                producer,
                "Confirmable$$Build",
                "void Confirmable__Build (Confirmable_o* __this, const MethodInfo* method);",
            ),
        ],
        addresses=[consumer, producer, 0x1200],
        literals={0x6000: "Connection failed"},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={consumer: {0x48: "TMPro.TMP_Text"}},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Connection failed"
    ]
    assert "persisted confirmable field +0x38" in result["exact_literals"][0][
        "evidence"
    ][0]["path"]


def test_list_getter_to_second_model_field_reaches_display() -> None:
    ui, writer, loader, getter, get_item, producer, add, allocate = range(0x1000, 0x1800, 0x100)
    slot = 0x5010
    code = _make_section({
        ui: _words(_ldr(1, 1, 0x18), _bl(ui + 4, 0x9000), RET),
        writer: _words(_str(1, 0, 0x18), RET),
        loader: _words(_bl(loader, getter), _bl(loader + 4, get_item), _mov(1, 0), _bl(loader + 12, writer), RET),
        getter: _words(_ldr(0, 0, 0x60), RET),
        producer: _words(_mov(19, 0), _bl(producer + 4, allocate), _mov(20, 0),
                         _adrp(producer + 12, slot, 8), _ldr(8, 8, slot & 0xFFF), _ldr(1, 8),
                         _mov(0, 20), _bl(producer + 28, add), _str(20, 19, 0x60), RET),
    }, end=0x1800)
    result = analyze_arm64_display_usage(
        code_sections=[(ui, code)],
        method_payload=[
            _method(ui, 'Panel$$Show', 'void Panel__Show (Panel_o* __this, Row_o* row, const MethodInfo* method);'),
            _method(writer, 'Row$$.ctor', 'void Row___ctor (Row_o* __this, System_String_o* name, const MethodInfo* method);'),
            _method(loader, 'Store$$Load', 'void Store__Load (Store_o* __this, const MethodInfo* method);'),
            _method(getter, 'Store$$Names', 'System_Collections_Generic_List_string__o* Store__Names (Store_o* __this, const MethodInfo* method);'),
            _method(get_item, 'System.Collections.Generic.List<string>$$get_Item', 'System_String_o* List__get_Item (System_Collections_Generic_List_string__o* __this, int32_t index, const MethodInfo* method);'),
            _method(producer, 'Store$$.ctor', 'void Store___ctor (Store_o* __this, const MethodInfo* method);'),
            _method(add, 'System.Collections.Generic.List<string>$$Add', 'void List__Add (System_Collections_Generic_List_string__o* __this, System_String_o* value, const MethodInfo* method);'),
            _method(allocate, 'Native$$Allocate', 'void* Native__Allocate (const MethodInfo* method);'), TMP_SET_TEXT,
        ], addresses=[ui, writer, loader, getter, get_item, producer, add, allocate, 0x1800, 0x9000],
        literals={0x6000: 'Brittany'}, slot_to_cell={slot: 0x6000},
    )
    assert 'Brittany' in {r['value'] for k in ('exact_literals', 'derived_influence') for r in result[k]}


def test_array_to_random_list_to_model_display_preserves_owner() -> None:
    ui, writer, loader, getter, get_item, producer, add, allocate, setter, reset, to_list = range(0x1000, 0x1B00, 0x100)
    slot, noise_slot = 0x5010, 0x5020
    code = _make_section({
        ui: _words(_ldr(1, 1, 0x18), _bl(ui + 4, 0x9000), RET),
        writer: _words(_str(1, 0, 0x18), RET),
        loader: _words(_bl(loader, getter), _bl(loader + 4, get_item), _mov(1, 0), _bl(loader + 12, writer), RET),
        getter: _words(_ldr(0, 0, 0x60), RET),
        setter: _words(_str(1, 0, 0x60), RET),
        producer: _words(_mov(19, 0), _bl(producer + 4, allocate), _mov(20, 0),
                         _adrp(producer + 12, slot, 8), _ldr(8, 8, slot & 0xFFF), _ldr(1, 8),
                         _str(1, 20, 0x20), _str(20, 19, 0x50),
                         _bl(producer + 32, allocate), _mov(20, 0),
                         _adrp(producer + 40, noise_slot, 8), _ldr(8, 8, noise_slot & 0xFFF), _ldr(1, 8),
                         _str(1, 20, 0x60), RET),
        reset: _words(_mov(19, 0), _ldr(0, 19, 0x50), _bl(reset + 8, to_list),
                      _bl(reset + 12, get_item), _mov(21, 0), _bl(reset + 20, allocate),
                      _mov(20, 0), _mov(1, 21), _bl(reset + 32, add),
                      _mov(0, 19), _mov(1, 20), _bl(reset + 44, setter), RET),
    }, end=0x1B00)
    result = analyze_arm64_display_usage(
        code_sections=[(ui, code)],
        method_payload=[
            _method(ui, 'Panel$$Show', 'void Panel__Show (Panel_o* __this, Row_o* row, const MethodInfo* method);'),
            _method(writer, 'Row$$.ctor', 'void Row___ctor (Row_o* __this, System_String_o* name, const MethodInfo* method);'),
            _method(loader, 'Store$$Load', 'void Store__Load (Store_o* __this, const MethodInfo* method);'),
            _method(getter, 'Store$$Names', 'System_Collections_Generic_List_string__o* Store__Names (Store_o* __this, const MethodInfo* method);'),
            _method(setter, 'Store$$SetNames', 'void Store__SetNames (Store_o* __this, System_Collections_Generic_List_string__o* value, const MethodInfo* method);'),
            _method(reset, 'Store$$Reset', 'void Store__Reset (Store_o* __this, const MethodInfo* method);'),
            _method(get_item, 'System.Collections.Generic.List<object>$$get_Item', 'Il2CppObject* List__get_Item (System_Collections_Generic_List_object__o* __this, int32_t index, const MethodInfo* method);'),
            _method(producer, 'Store$$.ctor', 'void Store___ctor (Store_o* __this, const MethodInfo* method);'),
            _method(add, 'System.Collections.Generic.List<object>$$AddWithResize', 'void List__AddWithResize (System_Collections_Generic_List_object__o* __this, Il2CppObject* value, const MethodInfo* method);'),
            _method(to_list, 'System.Linq.Enumerable$$ToList<object>', 'System_Collections_Generic_List_object__o* Enumerable__ToList (System_Collections_Generic_IEnumerable_object__o* source, const MethodInfo* method);'),
            _method(allocate, 'Native$$Allocate', 'void* Native__Allocate (const MethodInfo* method);'), TMP_SET_TEXT,
        ], addresses=[*range(ui, 0x1C00, 0x100), 0x9000],
        literals={0x6000: 'Brittany', 0x6010: 'InternalKey'}, slot_to_cell={slot: 0x6000, noise_slot: 0x6010},
        carrier_field_types={('store',): {0x50: ('__string_collection__',)}},
    )
    assert {r['value'] for k in ('exact_literals', 'derived_influence') for r in result[k]} == {'Brittany'}


def test_instance_dictionary_contents_reach_display_across_methods() -> None:
    producer = 0x1000
    consumer = 0x1100
    allocator = 0x1200
    dictionary_add = 0x1300
    dictionary_get = 0x1400
    literal_slot = 0x5010
    section = _make_section(
        {
            producer: _words(
                _mov(19, 0),
                _bl(producer + 4, allocator),
                _mov(20, 0),
                _adrp(producer + 12, literal_slot, 8),
                _ldr(8, 8, literal_slot & 0xFFF),
                _ldr(2, 8),
                _mov(0, 20),
                _bl(producer + 28, dictionary_add),
                _str(20, 19, 0x60),
                RET,
            ),
            consumer: _words(
                _ldr(0, 0, 0x60),
                _blr(8),
                _mov(1, 0),
                _bl(consumer + 12, 0x9000),
                RET,
            ),
        },
        end=0x1600,
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                producer,
                "Game.GlobalComponent.InGameLogManager$$.ctor",
                "void Game_GlobalComponent_InGameLogManager___ctor (Game_GlobalComponent_InGameLogManager_o* __this, const MethodInfo* method);",
            ),
            _method(
                consumer,
                "Game.GlobalComponent.InGameLogManager$$RegisterNewMessage",
                "void Game_GlobalComponent_InGameLogManager__RegisterNewMessage (Game_GlobalComponent_InGameLogManager_o* __this, int32_t type, const MethodInfo* method);",
            ),
            _method(
                allocator,
                "System.Collections.Generic.Dictionary<MessageType,string>$$.ctor",
                "System_Collections_Generic_Dictionary_MessageType_string__o* System_Collections_Generic_Dictionary_MessageType_string____ctor (const MethodInfo* method);",
            ),
            _method(
                dictionary_add,
                "System.Collections.Generic.Dictionary<Int32Enum, object>$$Add",
                "void System_Collections_Generic_Dictionary_Int32Enum_object___Add (System_Collections_Generic_Dictionary_Int32Enum_object__o* __this, int32_t key, Il2CppObject* value, const MethodInfo* method);",
            ),
            _method(
                dictionary_get,
                "System.Collections.Generic.Dictionary<MessageType,string>$$get_Item",
                "System_String_o* System_Collections_Generic_Dictionary_MessageType_string___get_Item (System_Collections_Generic_Dictionary_MessageType_string__o* __this, int32_t key, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[producer, consumer, allocator, dictionary_add, dictionary_get, 0x1500, 0x9000],
        literals={0x6000: "{0} starting."},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["derived_influence"]] == [
        "{0} starting."
    ]
    evidence = result["derived_influence"][0]["evidence"][0]
    assert "persisted game.globalcomponent.ingamelogmanager field +0x60" in evidence["path"]


def test_virtual_text_component_survives_this_pointer_writeback() -> None:
    """A pre-indexed access may carry ``this`` to a later field offset."""

    caller = 0x1000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _str_pre(31, 0, 0x30),  # x0 now represents this + 0x30
        _ldr(20, 0),  # this.LevelText
        _ldr(9, 20),
        _ldr(9, 9, 0x5E8),
        _mov(0, 20),
        _blr(9),
        RET,
    ]
    section = _make_section({caller: _words(*words)})
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, section)],
        method_payload=[
            _method(
                caller,
                "Game.LevelView$$Refresh",
                "void Game_LevelView__Refresh (Game_LevelView_o* __this, const MethodInfo* method);",
            )
        ],
        addresses=[caller, 0x1100],
        literals={0x6000: "LVL {0}"},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={caller: {0x30: "UnityEngine.UI.Text"}},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["LVL {0}"]


def test_nested_view_text_component_is_traced() -> None:
    caller = 0x1000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _ldr(20, 0, 0x20),
        _ldr(20, 20, 0x28),
        _ldr(9, 20),
        _ldr(9, 9, 0x5E8),
        _mov(0, 20),
        _blr(9),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(
                caller,
                "Game.Screen$$Refresh",
                "void Game_Screen__Refresh (Game_Screen_o* __this, const MethodInfo* method);",
            )
        ],
        addresses=[caller, 0x1100],
        literals={0x6000: "Nested label"},
        slot_to_cell={literal_slot: 0x6000},
        nested_display_fields_by_method={
            caller: {(0x20, 0x28): "UnityEngine.UI.Text"}
        },
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Nested label"]


def test_global_namespace_nested_display_field_is_not_treated_as_ambiguous(
    tmp_path,
) -> None:
    dump_path = tmp_path / "dump.cs"
    dump_path.write_text(
        """
// Namespace:
public class FortuneWheel : MonoBehaviour
{
    // Fields
    public TimerForSpin timer4Spin; // 0xA0
    // Methods
    // RVA: 0x1113F8C Offset: 0x0 VA: 0x1113F8C
    private void CheckSpinAvailable() { }
}
// Namespace:
public class TimerForSpin : MonoBehaviour
{
    // Fields
    public Text timerText; // 0x20
    // Methods
    // RVA: 0x11171F4 Offset: 0x0 VA: 0x11171F4
    private void OnEnable() { }
}
""",
        encoding="utf-8",
    )

    parsed = _parse_dump_nested_display_fields(dump_path)

    assert parsed[0x1113F8C] == {(0xA0, 0x20): "UnityEngine.UI.Text"}


def test_custom_string_return_helper_flows_to_display() -> None:
    caller = 0x1000
    helper = 0x1100
    literal_slot = 0x5010
    caller_words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, helper),
        _mov(1, 0),
        _bl(caller + 20, 0x9000),
        RET,
    ]
    helper_words = [_mov(0, 1), RET]
    result = analyze_arm64_display_usage(
        code_sections=[
            (
                0x1000,
                _make_section(
                    {caller: _words(*caller_words), helper: _words(*helper_words)}
                ),
            )
        ],
        method_payload=[
            _method(
                caller,
                "Game.Screen$$Show",
                "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            _method(
                helper,
                "Game.TextHelper$$Normalise",
                "System_String_o* Game_TextHelper__Normalise (Game_TextHelper_o* __this, System_String_o* value, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, helper, 0x1200, 0x9000],
        literals={0x6000: "Returned label"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["derived_influence"]] == [
        "Returned label"
    ]
    assert result["stats"]["string_return_summary_count"] == 1


def test_ldr_literal_form_reaches_display_sink() -> None:
    caller = 0x1000
    literal_slot = 0x1080
    words = [
        _ldr_literal(8, caller, literal_slot),
        _ldr(1, 8),
        _bl(caller + 8, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(
                caller,
                "Game.Screen$$Show",
                "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, 0x1100, 0x9000],
        literals={0x6000: "LDR literal label"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "LDR literal label"
    ]


def test_literal_returning_getter_flows_to_displaying_caller() -> None:
    caller = 0x1000
    getter = 0x1100
    literal_slot = 0x5010
    caller_words = [
        _bl(caller, getter),
        _mov(1, 0),
        _bl(caller + 8, 0x9000),
        RET,
    ]
    getter_words = [
        _adrp(getter, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(0, 8),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[
            (0x1000, _make_section({caller: _words(*caller_words), getter: _words(*getter_words)}))
        ],
        method_payload=[
            _method(
                caller,
                "Game.Screen$$Refresh",
                "void Game_Screen__Refresh (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            _method(
                getter,
                "Game.Screen$$GetTitle",
                "System_String_o* Game_Screen__GetTitle (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, getter, 0x1200, 0x9000],
        literals={0x6000: "Getter title"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Getter title"]


def test_non_void_and_transformed_wrapper_is_propagated() -> None:
    caller = 0x1000
    wrapper = 0x1100
    transform = 0x1200
    literal_slot = 0x5010
    caller_words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    wrapper_words = [
        _mov(0, 1),
        _bl(wrapper + 4, transform),
        _mov(1, 0),
        _bl(wrapper + 12, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[
            (0x1000, _make_section({caller: _words(*caller_words), wrapper: _words(*wrapper_words)}))
        ],
        method_payload=[
            _method(
                caller,
                "Game.Screen$$Show",
                "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);",
            ),
            _method(
                wrapper,
                "Game.Popup$$TryShow",
                "bool Game_Popup__TryShow (Game_Popup_o* __this, System_String_o* value, const MethodInfo* method);",
            ),
            _method(
                transform,
                "System.String$$ToUpperInvariant",
                "System_String_o* System_String__ToUpperInvariant (System_String_o* __this, const MethodInfo* method);",
            ),
            TMP_SET_TEXT,
        ],
        addresses=[caller, wrapper, transform, 0x1300, 0x9000],
        literals={0x6000: "offline"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["derived_influence"]] == ["offline"]
    assert "ToUpperInvariant" in result["derived_influence"][0]["evidence"][0]["transforms"][0]


def test_wrapper_propagation_reaches_fixed_point_beyond_four_layers() -> None:
    caller = 0x1000
    wrappers = [0x1100, 0x1200, 0x1300, 0x1400, 0x1500, 0x1600]
    literal_slot = 0x5010
    code = {
        caller: _words(
            _adrp(caller, literal_slot, 8),
            _ldr(8, 8, literal_slot & 0xFFF),
            _ldr(1, 8),
            _bl(caller + 12, wrappers[0]),
            RET,
        )
    }
    methods = [
        _method(
            caller,
            "Game.Screen$$Show",
            "void Game_Screen__Show (Game_Screen_o* __this, const MethodInfo* method);",
        )
    ]
    for index, address in enumerate(wrappers):
        target = wrappers[index + 1] if index + 1 < len(wrappers) else 0x9000
        code[address] = _words(_bl(address, target), RET)
        methods.append(
            _method(
                address,
                f"Game.Layer{index}$$Forward",
                f"void Game_Layer{index}__Forward (Game_Layer{index}_o* __this, System_String_o* value, const MethodInfo* method);",
            )
        )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section(code, end=0x1800))],
        method_payload=[*methods, TMP_SET_TEXT],
        addresses=[caller, *wrappers, 0x1700, 0x9000],
        literals={0x6000: "Deep wrapper label"},
        slot_to_cell={literal_slot: 0x6000},
        max_wrapper_depth=4,
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Deep wrapper label"
    ]


def test_indexed_array_text_is_reported_as_probable_not_exact() -> None:
    caller = 0x1000
    allocator = 0x1100
    literal_slot = 0x5010
    words = [
        _bl(caller, allocator),
        _mov(19, 0),
        _adrp(caller + 8, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _str_indexed(1, 19, 2),
        _ldr_indexed(1, 19, 3),
        _bl(caller + 28, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Menu$$Show", "void Game_Menu__Show (Game_Menu_o* __this, const MethodInfo* method);"),
            _method(allocator, "Game.Factory$$CreateLabels", "System_String_array* Game_Factory__CreateLabels (const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[caller, allocator, 0x1200, 0x9000],
        literals={0x6000: "Hard"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert result["exact_literals"] == []
    assert [item["value"] for item in result["probable_display_literals"]] == [
        "Hard"
    ]
    assert "index-insensitive container read" in result[
        "probable_display_literals"
    ][0]["evidence"][0]["transforms"]


def test_shared_rva_keeps_display_sink_alias() -> None:
    caller = 0x1000
    shared_sink = 0x9000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, shared_sink),
        RET,
    ]
    unrelated_alias = _method(
        shared_sink,
        "Game.SharedGeneric$$Consume",
        "void Game_SharedGeneric__Consume (Game_SharedGeneric_o* __this, Il2CppObject* value, const MethodInfo* method);",
    )
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Menu$$Show", "void Game_Menu__Show (Game_Menu_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT,
            unrelated_alias,
        ],
        addresses=[caller, 0x1100, shared_sink],
        literals={0x6000: "Shared body label"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Shared body label"
    ]
    assert result["stats"]["shared_rva_alias_count"] == 1


def test_dump_virtual_set_text_slot_is_parsed(tmp_path) -> None:
    dump = tmp_path / "dump.cs"
    dump.write_text(
        """// Namespace: UnityEngine.UI
public class Text
{
    // RVA: 0x1234 Offset: 0x1234 VA: 0x1234 Slot: 75
    public virtual void set_text(string value) { }
}
""",
        encoding="utf-8",
    )

    assert _parse_dump_virtual_text_slots(dump) == {
        "UnityEngine.UI.Text": frozenset({0x5E8})
    }


def test_factory_return_object_text_field_is_followed_through_wrapper(tmp_path) -> None:
    dump = tmp_path / "dump.cs"
    dump.write_text(
        """// Namespace: Game.UI
public class LineContent
{
    // Fields
    public UnityEngine.UI.Text TextSample; // 0x28
    // Methods
}
// Namespace: Game.UI
public class Panel
{
    // Methods
    // RVA: 0x1200 Offset: 0x1200 VA: 0x1200
    private LineContent CreateLine() { }
}
""",
        encoding="utf-8",
    )
    return_fields = _parse_dump_return_object_display_fields(dump)
    assert return_fields == {0x1200: {0x28: "UnityEngine.UI.Text"}}

    caller = 0x1000
    wrapper = 0x1100
    factory = 0x1200
    literal_slot = 0x5010
    wrapper_words = [
        _mov(19, 1),
        _bl(wrapper + 4, factory),
        _ldr(0, 0, 0x28),
        _ldr(8, 0),
        _ldr(9, 8, 0x5E8),
        _ldr(2, 8, 0x5F0),
        _mov(1, 19),
        _blr(9),
        RET,
    ]
    caller_words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _bl(caller + 12, wrapper),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[
            (
                0x1000,
                _make_section(
                    {caller: _words(*caller_words), wrapper: _words(*wrapper_words)}
                ),
            )
        ],
        method_payload=[
            _method(caller, "Game.UI.Panel$$Show", "void Game_UI_Panel__Show (Game_UI_Panel_o* __this, const MethodInfo* method);"),
            _method(wrapper, "Game.UI.Panel$$AddText", "void Game_UI_Panel__AddText (Game_UI_Panel_o* __this, System_String_o* text, const MethodInfo* method);"),
            _method(factory, "Game.UI.Panel$$CreateLine", "Game_UI_LineContent_o* Game_UI_Panel__CreateLine (Game_UI_Panel_o* __this, const MethodInfo* method);"),
        ],
        addresses=[caller, wrapper, factory, 0x1300],
        literals={0x6000: "Experience: +"},
        slot_to_cell={literal_slot: 0x6000},
        virtual_text_slots_by_component={"UnityEngine.UI.Text": frozenset({0x5E8})},
        return_object_display_fields=return_fields,
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Experience: +"]


def test_enum_tostring_members_are_selected_only_after_reaching_display(tmp_path) -> None:
    dump = tmp_path / "dump.cs"
    dump.write_text(
        """// Namespace: Game.Weapons
public enum AmmoTypes
{
    // Fields
    public int value__; // 0x0
    public const AmmoTypes cal12 = 4;
    public const AmmoTypes Rocket = 5;
}
""",
        encoding="utf-8",
    )
    enum_members = _parse_dump_enum_members(dump)
    assert enum_members == {"Game.Weapons.AmmoTypes": ("cal12", "Rocket")}

    caller = 0x1000
    enum_to_string = 0x1100
    concat = 0x1200
    enum_slot = 0x5010
    words = [
        _adrp(caller, enum_slot, 8),
        _ldr(8, 8, enum_slot & 0xFFF),
        _ldr(8, 8),
        _str(8, 31),
        _add_immediate(0, 31, 0),
        _bl(caller + 20, enum_to_string),
        _mov(1, 0),
        _bl(caller + 28, concat),
        _mov(1, 0),
        _bl(caller + 36, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(caller, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Shop$$ShowAmmo", "void Game_Shop__ShowAmmo (Game_Shop_o* __this, const MethodInfo* method);"),
            _method(enum_to_string, "System.Enum$$ToString", "System_String_o* System_Enum__ToString (System_Enum_o* __this, const MethodInfo* method);"),
            _method(concat, "System.String$$Concat", "System_String_o* System_String__Concat (System_String_o* str0, System_String_o* str1, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[caller, enum_to_string, concat, 0x1300, 0x9000],
        literals={},
        slot_to_cell={},
        pointer_slots={enum_slot: 0x7000},
        enum_type_by_pointer_slot={enum_slot: "Game.Weapons.AmmoTypes"},
        enum_members_by_type=enum_members,
    )

    assert result["display_enum_types"] == [
        {
            "enum_type": "Game.Weapons.AmmoTypes",
            "members": ["cal12", "Rocket"],
            "roles": ["derived"],
            "evidence": result["display_enum_types"][0]["evidence"],
        }
    ]
    assert result["display_enum_types"][0]["evidence"]


@pytest.mark.parametrize('displayed', [True, False])
def test_enum_return_helper_preserves_type_until_virtual_display(displayed) -> None:
    caller, helper, enum_to_string, enum_slot = 0x1000, 0x1100, 0x1200, 0x5010
    caller_words = [
        _mov(19, 0), _bl(caller + 4, helper), _mov(1, 0),
        _ldr(0, 19, 0x58), _ldr(8, 0), _ldr(9, 8, 0x558),
        _blr(9) if displayed else NOP, RET,
    ]
    helper_words = [
        _adrp(helper, enum_slot, 8), _ldr(8, 8, enum_slot & 0xFFF),
        _ldr(8, 8), _str(8, 31), _add_immediate(0, 31, 0),
        _bl(helper + 20, enum_to_string), RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(caller, _make_section({caller: _words(*caller_words), helper: _words(*helper_words)}, end=0x1300))],
        method_payload=[
            _method(caller, 'Panel$$Refresh', 'void Panel__Refresh (Panel_o* __this, const MethodInfo* method);'),
            _method(helper, 'Panel$$Title', 'System_String_o* Panel__Title (Panel_o* __this, const MethodInfo* method);'),
            _method(enum_to_string, 'System.Enum$$ToString', 'System_String_o* System_Enum__ToString (System_Enum_o* __this, const MethodInfo* method);'),
        ], addresses=[caller, helper, enum_to_string, 0x1300],
        literals={}, slot_to_cell={}, pointer_slots={enum_slot: 0x7000},
        enum_type_by_pointer_slot={enum_slot: 'Ability'},
        enum_members_by_type={'Ability': ('SuperSpeed', 'DoubleCoin')},
        display_fields_by_method={caller: {0x58: 'UnityEngine.UI.Text'}},
    )
    assert [r['enum_type'] for r in result['display_enum_types']] == (['Ability'] if displayed else [])


def test_enum_tostring_not_reaching_display_is_not_selected() -> None:
    caller = 0x1000
    enum_to_string = 0x1100
    enum_slot = 0x5010
    words = [
        _adrp(caller, enum_slot, 8),
        _ldr(8, 8, enum_slot & 0xFFF),
        _ldr(8, 8),
        _str(8, 31),
        _add_immediate(0, 31, 0),
        _bl(caller + 20, enum_to_string),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(caller, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Analytics$$LogAmmo", "void Game_Analytics__LogAmmo (const MethodInfo* method);"),
            _method(enum_to_string, "System.Enum$$ToString", "System_String_o* System_Enum__ToString (System_Enum_o* __this, const MethodInfo* method);"),
        ],
        addresses=[caller, enum_to_string, 0x1200],
        literals={},
        slot_to_cell={},
        pointer_slots={enum_slot: 0x7000},
        enum_type_by_pointer_slot={enum_slot: "Game.Weapons.AmmoTypes"},
        enum_members_by_type={"Game.Weapons.AmmoTypes": ("cal12",)},
    )

    assert result["display_enum_types"] == []


def test_unverified_virtual_string_slot_is_only_probable() -> None:
    caller = 0x1000
    literal_slot = 0x5010
    words = [
        _adrp(caller, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _ldr(20, 0, 0x28),
        _ldr(9, 20),
        _ldr(9, 9, 0x5E8),
        _ldr(2, 20),
        _ldr(2, 2, 0x5F0),
        _mov(0, 20),
        _blr(9),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.View$$Refresh", "void Game_View__Refresh (Game_View_o* __this, const MethodInfo* method);")
        ],
        addresses=[caller, 0x1100],
        literals={0x6000: "Maybe visible"},
        slot_to_cell={literal_slot: 0x6000},
        display_fields_by_method={caller: {0x28: "UnityEngine.UI.Text"}},
        virtual_text_slots_by_component={
            "UnityEngine.UI.Text": frozenset({0x558})
        },
    )

    assert result["exact_literals"] == []
    assert [item["value"] for item in result["probable_display_literals"]] == [
        "Maybe visible"
    ]


def test_guicontent_requires_later_gui_display_call() -> None:
    caller = 0x1000
    allocator = 0x1100
    gui_content_ctor = 0x1200
    gui_label = 0x1300
    literal_slot = 0x5010
    words = [
        _bl(caller, allocator),
        _mov(19, 0),
        _adrp(caller + 8, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _mov(0, 19),
        _bl(caller + 24, gui_content_ctor),
        _mov(0, 19),
        _bl(caller + 32, gui_label),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Menu$$OnGUI", "void Game_Menu__OnGUI (Game_Menu_o* __this, const MethodInfo* method);"),
            _method(allocator, "Game.Factory$$NewGUIContent", "UnityEngine_GUIContent_o* Game_Factory__NewGUIContent (const MethodInfo* method);"),
            _method(gui_content_ctor, "UnityEngine.GUIContent$$.ctor", "void UnityEngine_GUIContent___ctor (UnityEngine_GUIContent_o* __this, System_String_o* text, const MethodInfo* method);"),
            _method(gui_label, "UnityEngine.GUI$$Label", "void UnityEngine_GUI__Label (UnityEngine_GUIContent_o* content, const MethodInfo* method);"),
        ],
        addresses=[caller, allocator, gui_content_ctor, gui_label, 0x1400],
        literals={0x6000: "GUI content label"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "GUI content label"
    ]
    assert result["stats"]["display_container_operation_count"] == 2


def test_list_string_roundtrip_is_probable() -> None:
    caller = 0x1000
    allocator = 0x1100
    list_add = 0x1200
    list_get = 0x1300
    literal_slot = 0x5010
    words = [
        _bl(caller, allocator),
        _mov(19, 0),
        _adrp(caller + 8, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _mov(0, 19),
        _bl(caller + 24, list_add),
        _mov(0, 19),
        _bl(caller + 32, list_get),
        _mov(1, 0),
        _bl(caller + 40, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(caller, "Game.Menu$$Show", "void Game_Menu__Show (Game_Menu_o* __this, const MethodInfo* method);"),
            _method(allocator, "Game.Factory$$NewList", "System_Collections_Generic_List_string__o* Game_Factory__NewList (const MethodInfo* method);"),
            _method(list_add, "System.Collections.Generic.List<string>$$Add", "void System_Collections_Generic_List_string___Add (System_Collections_Generic_List_string__o* __this, System_String_o* item, const MethodInfo* method);"),
            _method(list_get, "System.Collections.Generic.List<string>$$get_Item", "System_String_o* System_Collections_Generic_List_string___get_Item (System_Collections_Generic_List_string__o* __this, int32_t index, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[caller, allocator, list_add, list_get, 0x1400, 0x9000],
        literals={0x6000: "Normal"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert result["exact_literals"] == []
    assert [item["value"] for item in result["probable_display_literals"]] == [
        "Normal"
    ]


def test_literal_static_field_write_reaches_later_display_reader() -> None:
    producer = 0x1000
    consumer = 0x1100
    global_slot = 0x5000
    literal_slot = 0x5010
    producer_words = [
        _adrp(producer, global_slot, 8),
        _ldr(8, 8, global_slot & 0xFFF),
        _adrp(producer + 8, literal_slot, 9),
        _ldr(9, 9, literal_slot & 0xFFF),
        _ldr(9, 9),
        _str(9, 8, 0x20),
        RET,
    ]
    consumer_words = [
        _adrp(consumer, global_slot, 8),
        _ldr(8, 8, global_slot & 0xFFF),
        _ldr(1, 8, 0x20),
        _bl(consumer + 12, 0x9000),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[
            (0x1000, _make_section({producer: _words(*producer_words), consumer: _words(*consumer_words)}))
        ],
        method_payload=[
            _method(producer, "Game.Messages$$Initialise", "void Game_Messages__Initialise (const MethodInfo* method);"),
            _method(consumer, "Game.Menu$$Refresh", "void Game_Menu__Refresh (Game_Menu_o* __this, const MethodInfo* method);"),
            TMP_SET_TEXT,
        ],
        addresses=[producer, consumer, 0x1200, 0x9000],
        literals={0x6000: "Saved message"},
        slot_to_cell={literal_slot: 0x6000},
        pointer_slots={global_slot: 0x7000},
    )

    assert [item["value"] for item in result["exact_literals"]] == [
        "Saved message"
    ]
    assert result["stats"]["static_field_literal_count"] == 1


@pytest.mark.parametrize("factory", ["GetComponent", "GetComponentInChildren", "GetComponentInParent"])
@pytest.mark.parametrize("owner", ["UnityEngine.GameObject", "UnityEngine.Component"])
def test_erased_get_component_is_accepted_only_after_verified_text_slot(factory, owner) -> None:
    caller = 0x1000
    get_component = 0x1100
    literal_slot = 0x5010
    words = [
        _bl(caller, get_component),
        _mov(20, 0),
        _ldr(8, 20),
        _ldr(9, 8, 0x5E8),
        _ldr(2, 8, 0x5F0),
        _adrp(caller + 20, literal_slot, 1),
        _ldr(1, 1, literal_slot & 0xFFF),
        _ldr(1, 1),
        _mov(0, 20),
        _blr(9),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[(0x1000, _make_section({caller: _words(*words)}))],
        method_payload=[
            _method(
                caller,
                "Game.Log$$Show",
                "void Game_Log__Show (Game_Log_o* __this, const MethodInfo* method);",
            ),
            _method(
                get_component,
                f"{owner}$${factory}<object>",
                "Il2CppObject* UnityEngine_GameObject__GetComponent_object (UnityEngine_GameObject_o* __this, const MethodInfo* method);",
            ),
        ],
        addresses=[caller, get_component, 0x1200],
        literals={0x6000: "Runtime label"},
        slot_to_cell={literal_slot: 0x6000},
    )

    assert [item["value"] for item in result["exact_literals"]] == ["Runtime label"]
    assert "virtual component text setter" in result["exact_literals"][0]["evidence"][0]["path"]


def test_dictionary_key_crosses_container_wrapper_and_typed_get_component() -> None:
    producer = 0x1000
    consumer = 0x1200
    allocator = 0x1300
    dictionary_add = 0x1400
    get_enumerator = 0x1500
    get_component = 0x1600
    literal_slot = 0x5010
    component_metadata_slot = 0x5020
    producer_words = [
        _bl(producer, allocator),
        _mov(19, 0),
        _adrp(producer + 8, literal_slot, 8),
        _ldr(8, 8, literal_slot & 0xFFF),
        _ldr(1, 8),
        _mov(2, 31),
        _mov(0, 19),
        _bl(producer + 28, dictionary_add),
        _mov(1, 19),
        _bl(producer + 36, consumer),
        RET,
    ]
    consumer_words = [
        _mov(0, 1),
        _bl(consumer + 4, get_enumerator),
        _mov(20, 0),
        _adrp(consumer + 12, component_metadata_slot, 8),
        _ldr(8, 8, component_metadata_slot & 0xFFF),
        _ldr(1, 8),
        _mov(0, 31),
        _bl(consumer + 28, get_component),
        _ldr(0, 0, 0x28),
        _ldr(8, 0),
        _ldr(9, 8, 0x5E8),
        _ldr(2, 8, 0x5F0),
        _mov(1, 20),
        _blr(9),
        RET,
    ]
    result = analyze_arm64_display_usage(
        code_sections=[
            (
                0x1000,
                _make_section(
                    {
                        producer: _words(*producer_words),
                        consumer: _words(*consumer_words),
                    },
                    end=0x1700,
                ),
            )
        ],
        method_payload=[
            _method(producer, "Game.Rewards$$Build", "void Game_Rewards__Build (Game_Rewards_o* __this, const MethodInfo* method);"),
            _method(consumer, "Game.Rewards$$Show", "void Game_Rewards__Show (Game_Rewards_o* __this, System_Collections_Generic_Dictionary_string__Sprite__o* output, const MethodInfo* method);"),
            _method(allocator, "Game.Factory$$NewDictionary", "System_Collections_Generic_Dictionary_string__Sprite__o* Game_Factory__NewDictionary (const MethodInfo* method);"),
            _method(dictionary_add, "System.Collections.Generic.Dictionary<object, object>$$Add", "void Dictionary_object_object__Add (Dictionary_object_object_o* __this, Il2CppObject* key, Il2CppObject* value, const MethodInfo* method);"),
            _method(get_enumerator, "System.Collections.Generic.Dictionary<object, object>$$GetEnumerator", "Dictionary_Enumerator_o Dictionary_object_object__GetEnumerator (Dictionary_object_object_o* __this, const MethodInfo* method);"),
            _method(get_component, "UnityEngine.GameObject$$GetComponent<object>", "Il2CppObject* UnityEngine_GameObject__GetComponent_object (UnityEngine_GameObject_o* __this, const MethodInfo* method);"),
        ],
        addresses=[producer, consumer, allocator, dictionary_add, get_enumerator, get_component, 0x1700],
        literals={0x6000: "Money: "},
        slot_to_cell={literal_slot: 0x6000},
        pointer_slots={component_metadata_slot: 0x7000},
        return_object_display_fields={
            component_metadata_slot: {0x28: "UnityEngine.UI.Text"}
        },
    )

    assert [item["value"] for item in result["derived_influence"]] == ["Money: "]
    evidence = result["derived_influence"][0]["evidence"][0]
    assert "index-insensitive container enumeration" in evidence["transforms"]


def test_generic_get_component_metadata_and_class_layout_are_parsed(tmp_path) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        '{"ScriptMetadataMethod":[{"Address":4660,"Name":"Method$UnityEngine.GameObject.GetComponent<BonusInfoPrimitive>()","MethodAddress":0}]}',
        encoding="utf-8",
    )
    dump_path = tmp_path / "dump.cs"
    dump_path.write_text(
        """// Namespace: Game.UI
public class BonusInfoPrimitive : MonoBehaviour
{
    // Fields
    public Image IconImage; // 0x20
    public Text InfoText; // 0x28
    // Methods
}
""",
        encoding="utf-8",
    )

    assert _load_script_metadata_component_factory_types(script_path) == {
        4660: "BonusInfoPrimitive"
    }
    assert _parse_dump_class_display_fields(dump_path) == {
        "Game.UI.BonusInfoPrimitive": {0x28: "UnityEngine.UI.Text"}
    }
