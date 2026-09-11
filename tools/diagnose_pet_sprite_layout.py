"""Read-only inspection of Pet World native sprite serialization."""
import struct
from pathlib import Path

import UnityPy
from UnityPy.helpers import TypeTreeHelper
from UnityPy.helpers.Tpk import get_typetree_node
from UnityPy.helpers.UnityVersion import UnityVersion

ROOT = Path(__file__).resolve().parents[1]
env = UnityPy.load(str(ROOT / 'workspace宠物世界-我的动物救援/input_sources/bin/Data/data.unity3d'))
for obj in env.objects:
    if not ((obj.assets_file.name == 'level0' and obj.path_id == 68)
            or (obj.assets_file.name == 'sharedassets0.assets' and obj.path_id == 20)):
        continue
    print('OBJECT', obj.assets_file.name, obj.path_id, obj.byte_size, flush=True)
    raw = obj.get_raw_data()
    node = get_typetree_node(obj.type.value, UnityVersion.from_str('6000.5.10f1'))
    obj.reset()
    for child in node.m_Children:
        start = obj.reader.Position - obj.byte_start
        try:
            value = TypeTreeHelper.read_typetree(child, obj.reader, as_dict=True,
                                               assetsfile=obj.assets_file, check_read=False)
            end = obj.reader.Position - obj.byte_start
            print('FIELD', start, end, child.m_Name, str(value)[:250], flush=True)
        except Exception as exc:
            print('ERROR', start, child.m_Name, str(exc), flush=True)
            break
    for offset in range(0, len(raw) - 3, 4):
        chunk = raw[offset:offset + 4]
        print(offset, chunk.hex(), struct.unpack('<I', chunk)[0], struct.unpack('<f', chunk)[0])
