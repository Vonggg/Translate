"""Read missing atlas metadata from original assets without changing export files."""
from functools import lru_cache
from pathlib import Path


def _export_shape(value):
    # UnityPy represents serialized maps as pairs; the CLI uses first/second
    # records. Keep both readers compatible, including the GUID/long map key.
    if isinstance(value, tuple) and len(value) == 2:
        return {"first": _export_shape(value[0]), "second": _export_shape(value[1])}
    if isinstance(value, list):
        return [_export_shape(item) for item in value]
    if isinstance(value, dict):
        return {key: _export_shape(item) for key, item in value.items()}
    return value


@lru_cache(maxsize=4)
def _read_atlases(source: str, size: int, modified_ns: int):
    import UnityPy

    env = UnityPy.load(source)
    result = {}
    for obj in env.objects:
        if obj.type.name == "SpriteAtlas":
            key = (obj.assets_file.name.replace("\\", "/"), obj.path_id)
            result[key] = _export_shape(obj.read_typetree())
    return result


def recover_sprite_atlas(source: Path, bundle_entry: str, path_id: int):
    """Use exact source, serialized-file name and PathID; never guess by name."""
    stat = source.stat()
    atlases = _read_atlases(str(source.resolve()), stat.st_size, stat.st_mtime_ns)
    entry = bundle_entry.replace("\\", "/") or source.name
    return atlases.get((entry, path_id))
