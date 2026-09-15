from pipeline.il2cpp_display_usage import (
    _parse_dump_display_fields, _parse_dump_class_display_fields, _parse_dump_nested_display_fields,
)


def test_generated_names_do_not_merge_with_outer_class(tmp_path):
    dump = tmp_path / 'dump.cs'
    dump.write_text('''// Namespace:
public class MainScene
{
// Fields
public TextMeshProUGUI title; // 0x28
// Methods
// RVA: 0x100
public void Update() { }
}
// Namespace:
private sealed class MainScene.<ScheduleChest>d__149
{
// Fields
public MainScene <>4__this; // 0x20
private TextMeshProUGUI <text>5__2; // 0x28
private static TextMeshProUGUI cached; // 0x30
// Methods
// RVA: 0x200
private bool MoveNext() { }
}
// Namespace:
private sealed class MainScene.<>c__DisplayClass1_0
{
// Fields
public TextMeshProUGUI <Label>k__BackingField; // 0x38
// Methods
// RVA: 0x300
public void <Start>b__0() { }
}
''', encoding='utf-8')
    fields = _parse_dump_display_fields(dump)
    assert 0x28 in fields[0x200]
    assert 0x30 not in fields[0x200]
    assert 0x38 in fields[0x300] and 0x28 not in fields[0x300]
    classes = _parse_dump_class_display_fields(dump)
    assert set(classes['MainScene']) == {0x28}
    assert set(classes['MainScene.<>c__DisplayClass1_0']) == {0x38}
    nested = _parse_dump_nested_display_fields(dump)
    assert (0x20, 0x28) in nested[0x200]
