"""Read-only comparison of Super Z original and imported resources."""
import json
from collections import Counter
from pathlib import Path
import UnityPy

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / 'workspace超级Z机器'
GAME = Path('D:/user/von/Workbench/data/projects/超级Z机器/game-name/game/assets/bin/Data')

def diff(a, b, path=''):
    if isinstance(a, dict) and isinstance(b, dict):
        for k in a.keys() | b.keys():
            if k not in a or k not in b:
                yield path+'.'+k, a.get(k), b.get(k)
            else:
                yield from diff(a[k], b[k], path+'.'+k)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            yield path+'.length', len(a), len(b)
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff(x, y, path+f'[{i}]')
    elif a != b:
        yield path, a, b

counts = Counter()
examples = {}
for file in (WORK/'output/Text').rglob('*.json'):
    orig = WORK/'input'/file.relative_to(WORK/'output/Text')
    if not orig.exists():
        continue
    for field, a, b in diff(json.loads(orig.read_text('utf-8-sig')), json.loads(file.read_text('utf-8-sig'))):
        import re
        key = re.sub(r'\[\d+\]', '[]', field)
        counts[key] += 1
        examples.setdefault(key, [])
        if len(examples[key]) < 3:
            examples[key].append([str(file.relative_to(WORK)), a, b])
print('JSON_CHANGES', json.dumps({'counts': counts, 'examples': examples}, ensure_ascii=False), flush=True)

for final in (WORK/'FinalResult/Data').iterdir():
    if '.split' in final.name and not final.name.endswith('.split0'):
        continue
    original = GAME/final.name
    if not original.is_file() or final.suffix in ('.resS', '.resource'):
        continue
    try:
        before = UnityPy.load(str(original))
        after = UnityPy.load(str(final))
        old = {o.path_id: o for o in before.objects}
        new = {o.path_id: o for o in after.objects}
        if final.name == 'sharedassets1.assets.split0':
            for pid, english, chinese in [(49505,'CarThrow','汽车投掷'),(49506,'Drone','无人机'),(49507,'ElectroGlove','电能手套'),(50826,'Flame','火焰'),(50855,'Cheer','欢呼')]:
                a, b = old[pid].get_raw_data(), new[pid].get_raw_data()
                print('KEY_CHECK', pid, english, chinese, 'source_en', english.encode() in a, 'final_zh', chinese.encode() in b, 'sizes', len(a), len(b), flush=True)
        changed = Counter()
        suspicious = []
        detail = []
        for pid in old.keys() & new.keys():
            a, b = old[pid], new[pid]
            if a.get_raw_data() != b.get_raw_data():
                changed[a.type.name] += 1
                try:
                    da, db = a.read_typetree(), b.read_typetree()
                    changes = list(diff(da, db))
                    if a.type.name == 'Texture2D':
                        detail.append([pid, da.get('m_Name'), {k: [da.get(k), db.get(k)] for k in ('m_Width','m_Height','m_TextureFormat','m_MipCount','m_StreamData','m_CompleteImageSize')}])
                    elif a.type.name == 'MonoBehaviour':
                        for field, va, vb in changes:
                            if not isinstance(va, str) and 'm_Effect' not in field and field not in ('.m_Enabled', '.m_UseGraphicAlpha'):
                                detail.append([pid, field, str(va)[:100], str(vb)[:100]])
                except Exception as exc:
                    detail.append([pid, 'TYPETREE_ERROR', str(exc)[:160]])
                if a.type.name == 'MonoBehaviour' and (b.byte_size < 40 or b.byte_size < a.byte_size / 2):
                    suspicious.append([pid, a.byte_size, b.byte_size])
        print('ASSET', final.name, 'objects', len(old), len(new), 'changes', dict(changed), 'truncated', suspicious[:30], flush=True)
        if detail:
            print('DETAIL', json.dumps(detail[:30], ensure_ascii=False), flush=True)
    except Exception as exc:
        print('ERROR', final.name, str(exc), flush=True)
