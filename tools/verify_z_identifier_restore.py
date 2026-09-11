"""Verify the narrowly scoped Super Z identifier restoration against its backup."""
import json
from collections import Counter
from pathlib import Path

import UnityPy

WORK = Path(__file__).resolve().parents[1] / 'workspace超级Z机器'
BACKUP = WORK / 'backups/identifier_restore_20260911'
FIELDS = {'abilityName', 'statusEffectName', 'vehicleColorsName'}


def changes(a, b, path=()):
    if isinstance(a, dict) and isinstance(b, dict):
        assert a.keys() == b.keys(), path
        for key in a:
            yield from changes(a[key], b[key], path + (key,))
    elif isinstance(a, list) and isinstance(b, list):
        assert len(a) == len(b), path
        for index, (x, y) in enumerate(zip(a, b)):
            yield from changes(x, y, path + (index,))
    elif a != b:
        yield path, a, b


targets = set()
counts = Counter()
for old in (BACKUP / 'Text').rglob('*.json'):
    relative = old.relative_to(BACKUP / 'Text')
    new = WORK / 'output/Text' / relative
    original = WORK / 'input' / relative
    before = json.loads(old.read_text('utf-8-sig'))
    after = json.loads(new.read_text('utf-8-sig'))
    delta = list(changes(before, after))
    for path, previous, restored in delta:
        assert len(path) == 1 and path[0] in FIELDS, (relative, path)
        source = json.loads(original.read_text('utf-8-sig'))
        assert restored == source[path[0]], (relative, path)
        overlay = json.loads((WORK / 'temp/selected_import_overlay' / relative).read_text('utf-8-sig'))
        assert overlay[path[0]] == restored, relative
        counts[path[0]] += 1
        assert relative.parts[2] == 'sharedassets1', relative
        targets.add(int(old.stem.rsplit('_', 1)[1]))
assert sum(counts.values()) == 37 and len(targets) == 37, counts
print('JSON_RESTORED', dict(counts), flush=True)

old_root = BACKUP / 'FinalResult/Data'
new_root = WORK / 'FinalResult/Data'
assert {p.name for p in old_root.iterdir()} == {p.name for p in new_root.iterdir()}, 'Output file set changed'
seen = set()
asset_count = 0
for old in sorted(old_root.iterdir()):
    new = new_root / old.name
    if '.split' in old.name and not old.name.endswith('.split0'):
        continue
    if old.suffix in {'.resS', '.resource'}:
        assert old.read_bytes() == new.read_bytes(), old.name
        continue
    before = {o.path_id: o for o in UnityPy.load(str(old)).objects}
    after = {o.path_id: o for o in UnityPy.load(str(new)).objects}
    assert before.keys() == after.keys(), old.name
    changed = {pid for pid in before if before[pid].get_raw_data() != after[pid].get_raw_data()}
    if old.name == 'sharedassets1.assets.split0':
        assert changed == targets, ('Unexpected changed objects', changed ^ targets)
        source = {o.path_id: o for o in UnityPy.load(str(WORK / 'input_sources/bin/Data/sharedassets1.assets')).objects}
        for pid in targets:
            assert source[pid].get_raw_data() == after[pid].get_raw_data(), ('Not fully restored', pid)
        seen = changed
    else:
        assert not changed, (old.name, changed)
    asset_count += 1
    print('ASSET_OK', old.name, 'changed', len(changed), flush=True)
assert seen == targets
print('PASS', asset_count, 'assets checked; exactly 37 objects restored byte-for-byte to original; all other object payloads unchanged.', flush=True)
