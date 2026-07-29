from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path
from typing import Any


UINT32_MAX = 0xFFFFFFFF
UNICODE_STRING_FLAG = 0x80000000
DYNAMIC_STRING_FLAG = 0x40000000
CLEAR_FLAGS_MASK = 0x3FFFFFFF
CATALOG_MAGIC = 0x0DE38942
CATALOG_VERSION = 2


class CatalogFormatError(ValueError):
    pass


class BinaryCatalogReader:
    def __init__(self, data: bytes):
        self.data = data
        self._string_cache: dict[tuple[int, str], str | None] = {}
        self._type_cache: dict[int, dict[str, Any] | None] = {}
        self._object_cache: dict[int, Any] = {}

    def _require(self, offset: int, size: int, label: str) -> None:
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise CatalogFormatError(
                f"{label} 越界: offset={offset}, size={size}, file_size={len(self.data)}"
            )

    def u32(self, offset: int) -> int:
        self._require(offset, 4, "uint32")
        return struct.unpack_from("<I", self.data, offset)[0]

    def i32(self, offset: int) -> int:
        self._require(offset, 4, "int32")
        return struct.unpack_from("<i", self.data, offset)[0]

    def prefixed_array_offsets(self, offset: int, item_size: int) -> tuple[int, int]:
        if offset == UINT32_MAX:
            return 0, 0
        self._require(offset - 4, 4, "数组长度")
        byte_size = self.u32(offset - 4)
        if byte_size % item_size:
            raise CatalogFormatError(
                f"数组字节数无法整除元素大小: offset={offset}, bytes={byte_size}, item_size={item_size}"
            )
        self._require(offset, byte_size, "数组数据")
        return byte_size // item_size, byte_size

    def read_auto_string(self, string_id: int) -> str | None:
        if string_id == UINT32_MAX:
            return None
        is_unicode = bool(string_id & UNICODE_STRING_FLAG)
        offset = string_id & CLEAR_FLAGS_MASK
        cache_key = (string_id, "\0")
        if cache_key in self._string_cache:
            return self._string_cache[cache_key]
        self._require(offset - 4, 4, "字符串长度")
        byte_size = self.u32(offset - 4)
        self._require(offset, byte_size, "字符串")
        encoding = "utf-16-le" if is_unicode else "ascii"
        value = self.data[offset : offset + byte_size].decode(encoding, errors="replace")
        self._string_cache[cache_key] = value
        return value

    def read_string(self, string_id: int, separator: str = "") -> str | None:
        if string_id == UINT32_MAX:
            return None
        cache_key = (string_id, separator)
        if cache_key in self._string_cache:
            return self._string_cache[cache_key]
        if not separator or not (string_id & DYNAMIC_STRING_FLAG):
            value = self.read_auto_string(string_id)
            self._string_cache[cache_key] = value
            return value

        chunks: list[str] = []
        next_id = string_id
        visited: set[int] = set()
        while next_id != UINT32_MAX:
            dynamic_offset = next_id & CLEAR_FLAGS_MASK
            if dynamic_offset in visited:
                raise CatalogFormatError(f"动态字符串形成循环: offset={dynamic_offset}")
            visited.add(dynamic_offset)
            self._require(dynamic_offset, 8, "动态字符串")
            chunk_id, next_id = struct.unpack_from("<II", self.data, dynamic_offset)
            chunks.append(self.read_auto_string(chunk_id) or "")
        value = separator.join(reversed(chunks))
        self._string_cache[cache_key] = value
        return value

    def read_type(self, offset: int) -> dict[str, Any] | None:
        if offset == UINT32_MAX:
            return None
        if offset in self._type_cache:
            return self._type_cache[offset]
        self._require(offset, 8, "类型数据")
        assembly_id, class_id = struct.unpack_from("<II", self.data, offset)
        value = {
            "assembly": self.read_string(assembly_id, "."),
            "class": self.read_string(class_id, "."),
            "_offset": offset,
        }
        self._type_cache[offset] = value
        return value

    @staticmethod
    def _hash128_text(raw: bytes) -> str:
        return raw.hex()

    def read_hash128(self, offset: int) -> dict[str, str]:
        self._require(offset, 16, "Hash128")
        raw = self.data[offset : offset + 16]
        return {
            "value": self._hash128_text(raw),
            "raw_hex": raw.hex(),
        }

    def read_object(self, offset: int) -> Any:
        if offset == UINT32_MAX:
            return None
        if offset in self._object_cache:
            return self._object_cache[offset]
        self._require(offset, 8, "对象类型数据")
        type_id, object_id = struct.unpack_from("<II", self.data, offset)
        type_info = self.read_type(type_id)
        class_name = str((type_info or {}).get("class") or "")

        if class_name == "System.String":
            self._require(object_id, 8, "字符串对象")
            string_id = self.u32(object_id)
            separator_code = struct.unpack_from("<H", self.data, object_id + 4)[0]
            separator = chr(separator_code) if separator_code else ""
            value: Any = self.read_string(string_id, separator)
        elif class_name == "System.Int32":
            value = self.i32(object_id)
        elif class_name == "System.Int64":
            self._require(object_id, 8, "Int64")
            value = struct.unpack_from("<q", self.data, object_id)[0]
        elif class_name == "System.Boolean":
            self._require(object_id, 1, "Boolean")
            value = bool(self.data[object_id])
        elif class_name == "UnityEngine.Hash128":
            value = self.read_hash128(object_id)["value"]
        elif class_name.endswith(".AssetBundleRequestOptions"):
            value = self.read_asset_bundle_options(object_id)
        else:
            value = {
                "_type": type_info,
                "_object_offset": object_id,
                "_note": "当前只保留未知对象的类型和偏移",
            }
        self._object_cache[offset] = value
        return value

    def read_asset_bundle_options(self, offset: int) -> dict[str, Any]:
        self._require(offset, 20, "AssetBundleRequestOptions")
        hash_id, bundle_name_id, crc, bundle_size, common_id = struct.unpack_from(
            "<IIIII", self.data, offset
        )
        self._require(common_id, 8, "AssetBundleRequestOptions.Common")
        timeout, redirect_limit, retry_count, flags = struct.unpack_from(
            "<hBBi", self.data, common_id
        )
        hash_value = self.read_hash128(hash_id)
        return {
            "_type": "AssetBundleRequestOptions",
            "_offset": offset,
            "_crc_offset": offset + 8,
            "_bundle_size_offset": offset + 12,
            "_hash_offset": hash_id,
            "hash": hash_value["value"],
            "hash_raw_hex": hash_value["raw_hex"],
            "bundle_name": self.read_string(bundle_name_id, "_"),
            "crc": crc,
            "bundle_size": bundle_size,
            "timeout": timeout,
            "redirect_limit": redirect_limit,
            "retry_count": retry_count,
            "asset_load_mode": (
                "AllPackedAssetsAndDependencies"
                if flags & 1
                else "RequestedAssetAndDependencies"
            ),
            "chunked_transfer": bool(flags & 2),
            "use_crc_for_cached_bundle": bool(flags & 4),
            "use_unity_web_request_for_local_bundles": bool(flags & 8),
            "clear_other_cached_versions_when_loaded": bool(flags & 16),
            "_flags": flags,
        }

    def read_object_initialization(self, offset: int) -> dict[str, Any] | None:
        if offset == UINT32_MAX:
            return None
        self._require(offset, 12, "ObjectInitializationData")
        id_offset, type_offset, data_offset = struct.unpack_from(
            "<III", self.data, offset
        )
        return {
            "m_Id": self.read_string(id_offset),
            "m_ObjectType": self._legacy_type(self.read_type(type_offset)),
            "m_Data": self.read_string(data_offset) or "",
            "_binary_offset": offset,
        }

    def read_object_initialization_array(self, offset: int) -> list[dict[str, Any]]:
        if offset == UINT32_MAX:
            return []
        count, _ = self.prefixed_array_offsets(offset, 4)
        object_offsets = struct.unpack_from(f"<{count}I", self.data, offset)
        return [
            value
            for value in (
                self.read_object_initialization(object_offset)
                for object_offset in object_offsets
            )
            if value is not None
        ]

    @staticmethod
    def _legacy_type(type_info: dict[str, Any] | None) -> dict[str, str]:
        return {
            "m_AssemblyName": str((type_info or {}).get("assembly") or ""),
            "m_ClassName": str((type_info or {}).get("class") or ""),
        }

    def read_location(self, offset: int) -> dict[str, Any]:
        self._require(offset, 28, "ResourceLocation")
        (
            primary_key_offset,
            internal_id_offset,
            provider_offset,
            dependency_set_offset,
            dependency_hash,
            extra_data_offset,
            type_id,
        ) = struct.unpack_from("<IIIIiII", self.data, offset)
        dependency_offsets: list[int] = []
        if dependency_set_offset != UINT32_MAX:
            count, _ = self.prefixed_array_offsets(dependency_set_offset, 4)
            dependency_offsets = list(
                struct.unpack_from(f"<{count}I", self.data, dependency_set_offset)
            )
        return {
            "_offset": offset,
            "_internal_id_string_id": internal_id_offset,
            "primary_key": self.read_string(primary_key_offset, "/"),
            "internal_id": self.read_string(internal_id_offset, "/"),
            "provider_id": self.read_string(provider_offset, "."),
            "resource_type": self.read_type(type_id),
            "dependency_hash": dependency_hash,
            "dependency_offsets": dependency_offsets,
            "data": self.read_object(extra_data_offset),
            "keys": [],
        }

    def parse(self, progress=None) -> dict[str, Any]:
        self._require(0, 32, "catalog header")
        (
            magic,
            version,
            keys_offset,
            id_offset,
            instance_provider,
            scene_provider,
            init_objects_array,
            build_result_hash,
        ) = struct.unpack_from("<iiIIIIII", self.data, 0)
        if magic != CATALOG_MAGIC:
            raise CatalogFormatError(
                f"magic 不匹配: 0x{magic & UINT32_MAX:08X}，预期 0x{CATALOG_MAGIC:08X}"
            )
        if version != CATALOG_VERSION:
            raise CatalogFormatError(
                f"catalog 版本不支持: {version}，当前解析器支持 {CATALOG_VERSION}"
            )

        key_count, key_byte_size = self.prefixed_array_offsets(keys_offset, 8)
        location_keys: dict[int, list[Any]] = {}
        key_records: list[dict[str, Any]] = []
        for index in range(key_count):
            key_name_offset, location_set_offset = struct.unpack_from(
                "<II", self.data, keys_offset + index * 8
            )
            key_value = self.read_object(key_name_offset)
            location_count, _ = self.prefixed_array_offsets(location_set_offset, 4)
            location_offsets = list(
                struct.unpack_from(f"<{location_count}I", self.data, location_set_offset)
            )
            key_records.append(
                {
                    "key": key_value,
                    "location_offsets": location_offsets,
                }
            )
            for location_offset in location_offsets:
                location_keys.setdefault(location_offset, []).append(key_value)
            if progress and (
                index == 0 or (index + 1) % 10000 == 0 or index + 1 == key_count
            ):
                progress(index + 1, key_count, len(location_keys))

        locations: list[dict[str, Any]] = []
        location_by_offset: dict[int, dict[str, Any]] = {}
        for location_offset, keys in location_keys.items():
            location = self.read_location(location_offset)
            location["keys"] = keys
            location_by_offset[location_offset] = location
            locations.append(location)

        for location in locations:
            dependencies = []
            for dependency_offset in location.pop("dependency_offsets"):
                dependency = location_by_offset.get(dependency_offset)
                dependencies.append(
                    {
                        "_offset": dependency_offset,
                        "primary_key": (
                            dependency.get("primary_key") if dependency else None
                        ),
                    }
                )
            location["dependencies"] = dependencies

        bundle_locations = [
            location
            for location in locations
            if isinstance(location.get("data"), dict)
            and location["data"].get("_type") == "AssetBundleRequestOptions"
        ]
        return {
            "format": "Unity Addressables Binary Content Catalog",
            "magic": f"0x{magic & UINT32_MAX:08X}",
            "version": version,
            "file_size": len(self.data),
            "header": {
                "keys_offset": keys_offset,
                "id_offset": id_offset,
                "instance_provider_offset": instance_provider,
                "scene_provider_offset": scene_provider,
                "init_objects_array_offset": init_objects_array,
                "build_result_hash_offset": build_result_hash,
                "locator_id": self.read_string(id_offset),
                "build_result_hash": self.read_string(build_result_hash),
                "instance_provider_data": self.read_object_initialization(
                    instance_provider
                ),
                "scene_provider_data": self.read_object_initialization(
                    scene_provider
                ),
                "resource_provider_data": self.read_object_initialization_array(
                    init_objects_array
                ),
            },
            "summary": {
                "key_count": key_count,
                "key_table_bytes": key_byte_size,
                "unique_location_count": len(locations),
                "asset_bundle_location_count": len(bundle_locations),
            },
            "locations": locations,
            "keys": key_records,
        }


def _unique_preserve_order(values) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for value in values:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def build_legacy_output_view(parsed: dict[str, Any]) -> dict[str, Any]:
    locations = parsed.get("locations", [])
    keys = parsed.get("keys", [])
    header = parsed.get("header", {})
    internal_ids = _unique_preserve_order(
        location.get("internal_id")
        for location in locations
        if isinstance(location, dict) and location.get("internal_id") is not None
    )
    provider_ids = _unique_preserve_order(
        location.get("provider_id")
        for location in locations
        if isinstance(location, dict) and location.get("provider_id") is not None
    )
    resource_types = _unique_preserve_order(
        BinaryCatalogReader._legacy_type(location.get("resource_type"))
        for location in locations
        if isinstance(location, dict)
    )

    option_rows: list[dict[str, Any]] = []
    location_rows: list[dict[str, Any]] = []
    for location in locations:
        if not isinstance(location, dict):
            continue
        location_rows.append(
            {
                "_binary_offset": location.get("_offset"),
                "_binary_internal_id_string_id": location.get(
                    "_internal_id_string_id"
                ),
                "PrimaryKey": location.get("primary_key"),
                "InternalId": location.get("internal_id"),
                "ProviderId": location.get("provider_id"),
                "ResourceType": BinaryCatalogReader._legacy_type(
                    location.get("resource_type")
                ),
                "Keys": location.get("keys", []),
                "Dependencies": location.get("dependencies", []),
            }
        )
        data = location.get("data")
        if not isinstance(data, dict) or data.get("_type") != "AssetBundleRequestOptions":
            continue
        option_rows.append(
            {
                "view": "binary_catalog",
                "binary_location_offset": location.get("_offset"),
                "binary_options_offset": data.get("_offset"),
                "binary_hash_offset": data.get("_hash_offset"),
                "binary_crc_offset": data.get("_crc_offset"),
                "binary_bundle_size_offset": data.get("_bundle_size_offset"),
                "InternalId": location.get("internal_id"),
                "PrimaryKey": location.get("primary_key"),
                "m_Hash": data.get("hash"),
                "m_Crc": data.get("crc"),
                "m_Timeout": data.get("timeout"),
                "m_ChunkedTransfer": data.get("chunked_transfer"),
                "m_RedirectLimit": data.get("redirect_limit"),
                "m_RetryCount": data.get("retry_count"),
                "m_BundleName": data.get("bundle_name"),
                "m_AssetLoadMode": data.get("asset_load_mode"),
                "m_BundleSize": data.get("bundle_size"),
                "m_UseCrcForCachedBundles": data.get("use_crc_for_cached_bundle"),
                "m_UseUWRForLocalBundles": data.get(
                    "use_unity_web_request_for_local_bundles"
                ),
                "m_ClearOtherCachedVersionsWhenLoaded": data.get(
                    "clear_other_cached_versions_when_loaded"
                ),
            }
        )

    key_rows = [
        {
            "key": row.get("key"),
            "location_offsets": row.get("location_offsets", []),
        }
        for row in keys
        if isinstance(row, dict)
    ]
    return {
        "m_LocatorId": header.get("locator_id") or "",
        "m_BuildResultHash": header.get("build_result_hash") or "",
        "m_InstanceProviderData": header.get("instance_provider_data"),
        "m_SceneProviderData": header.get("scene_provider_data"),
        "m_ResourceProviderData": header.get("resource_provider_data", []),
        "m_ProviderIds": provider_ids,
        "m_InternalIds": internal_ids,
        "m_KeyDataString": {
            "_binary_catalog_compat_view": True,
            "keys": key_rows,
        },
        "m_BucketDataString": {
            "_binary_catalog_compat_view": True,
            "key_location_sets": key_rows,
        },
        "m_EntryDataString": {
            "_binary_catalog_compat_view": True,
            "locations": location_rows,
        },
        "m_ExtraDataString": {
            "_binary_catalog_compat_view": True,
            "AssetBundleRequestOptions": option_rows,
        },
        "m_resourceTypes": resource_types,
        "m_InternalIdPrefixes": [],
    }


def write_legacy_output_view(
    parsed: dict[str, Any], output: Path
) -> dict[str, Any]:
    legacy = build_legacy_output_view(parsed)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return legacy


def calculate_catalog_hash(data: bytes) -> str:
    try:
        import spookyhash
    except ImportError as exc:
        raise RuntimeError(
            "缺少 spookyhash；请按 requirements.txt 安装依赖后重试"
        ) from exc
    value = spookyhash.hash128(data)
    return value.to_bytes(16, "little").hex()


def inspect_catalog_hash(source: Path, data: bytes) -> dict[str, Any]:
    hash_path = source.with_suffix(".hash")
    calculated = calculate_catalog_hash(data)
    recorded = (
        hash_path.read_text(encoding="ascii", errors="ignore").strip().lower()
        if hash_path.is_file()
        else None
    )
    return {
        "algorithm": "Unity Scriptable Build Pipeline SpookyHash128",
        "hash_file": str(hash_path),
        "recorded": recorded,
        "calculated": calculated,
        "matches": recorded == calculated if recorded is not None else None,
    }


def write_catalog_hash(catalog_path: Path, hash_path: Path | None = None) -> Path:
    target = hash_path or catalog_path.with_suffix(".hash")
    value = calculate_catalog_hash(catalog_path.read_bytes())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(value, encoding="ascii")
    return target


def _append_binary_string(buffer: bytearray, value: str) -> int:
    if all(ord(char) <= 0x7F for char in value):
        encoded = value.encode("ascii")
        flag = 0
    else:
        encoded = value.encode("utf-16-le")
        flag = UNICODE_STRING_FLAG
    prefix_offset = len(buffer)
    buffer.extend(struct.pack("<I", len(encoded)))
    value_offset = len(buffer)
    buffer.extend(encoded)
    return value_offset | flag


def repack_binary_catalog_from_legacy_output(
    source_catalog: Path,
    output_json: Path,
    destination_catalog: Path,
    destination_hash: Path | None = None,
) -> dict[str, Any]:
    legacy = json.loads(output_json.read_text(encoding="utf-8-sig"))
    if not isinstance(legacy, dict):
        raise CatalogFormatError(f"Output.json 不是对象: {output_json}")
    entry_field = legacy.get("m_EntryDataString")
    extra_field = legacy.get("m_ExtraDataString")
    if not (
        isinstance(entry_field, dict)
        and entry_field.get("_binary_catalog_compat_view") is True
        and isinstance(extra_field, dict)
        and extra_field.get("_binary_catalog_compat_view") is True
    ):
        raise CatalogFormatError("Output.json 不是 binary catalog 兼容视图")

    source_data = source_catalog.read_bytes()
    source_reader = BinaryCatalogReader(source_data)
    buffer = bytearray(source_data)
    internal_id_updates = 0
    option_updates = 0

    locations = entry_field.get("locations")
    if not isinstance(locations, list):
        raise CatalogFormatError("m_EntryDataString.locations 不存在")
    for row in locations:
        if not isinstance(row, dict):
            continue
        location_offset = row.get("_binary_offset")
        new_internal_id = row.get("InternalId")
        if not isinstance(location_offset, int) or not isinstance(new_internal_id, str):
            continue
        source_reader._require(location_offset, 28, "ResourceLocation 回写")
        old_string_id = source_reader.u32(location_offset + 4)
        old_internal_id = source_reader.read_string(old_string_id, "/")
        if old_internal_id == new_internal_id:
            continue
        new_string_id = _append_binary_string(buffer, new_internal_id)
        struct.pack_into("<I", buffer, location_offset + 4, new_string_id)
        internal_id_updates += 1

    options = extra_field.get("AssetBundleRequestOptions")
    if not isinstance(options, list):
        raise CatalogFormatError(
            "m_ExtraDataString.AssetBundleRequestOptions 不存在"
        )
    for row in options:
        if not isinstance(row, dict):
            continue
        hash_offset = row.get("binary_hash_offset")
        crc_offset = row.get("binary_crc_offset")
        size_offset = row.get("binary_bundle_size_offset")
        hash_value = row.get("m_Hash")
        crc = row.get("m_Crc")
        bundle_size = row.get("m_BundleSize")
        if not all(isinstance(value, int) for value in (hash_offset, crc_offset, size_offset)):
            continue
        if not isinstance(hash_value, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", hash_value):
            raise CatalogFormatError(f"无效 m_Hash: {hash_value!r}")
        if not isinstance(crc, int) or not 0 <= crc <= UINT32_MAX:
            raise CatalogFormatError(f"无效 m_Crc: {crc!r}")
        if not isinstance(bundle_size, int) or not 0 <= bundle_size <= UINT32_MAX:
            raise CatalogFormatError(f"无效 m_BundleSize: {bundle_size!r}")
        source_reader._require(hash_offset, 16, "Bundle Hash 回写")
        source_reader._require(crc_offset, 4, "Bundle CRC 回写")
        source_reader._require(size_offset, 4, "BundleSize 回写")
        new_hash = bytes.fromhex(hash_value)
        changed = (
            bytes(buffer[hash_offset : hash_offset + 16]) != new_hash
            or struct.unpack_from("<I", buffer, crc_offset)[0] != crc
            or struct.unpack_from("<I", buffer, size_offset)[0] != bundle_size
        )
        if not changed:
            continue
        buffer[hash_offset : hash_offset + 16] = new_hash
        struct.pack_into("<I", buffer, crc_offset, crc)
        struct.pack_into("<I", buffer, size_offset, bundle_size)
        option_updates += 1

    destination_catalog.parent.mkdir(parents=True, exist_ok=True)
    destination_catalog.write_bytes(buffer)
    hash_path = write_catalog_hash(
        destination_catalog,
        destination_hash or destination_catalog.with_suffix(".hash"),
    )
    return {
        "source": str(source_catalog),
        "destination": str(destination_catalog),
        "hash_path": str(hash_path),
        "internal_id_updates": internal_id_updates,
        "bundle_option_updates": option_updates,
        "source_size": len(source_data),
        "destination_size": len(buffer),
        "catalog_hash": hash_path.read_text(encoding="ascii").strip(),
    }


def parse_catalog_file(source: Path, output: Path, progress=None) -> dict[str, Any]:
    source_data = source.read_bytes()
    reader = BinaryCatalogReader(source_data)
    result = reader.parse(progress=progress)
    result["source_file"] = str(source)
    result["catalog_hash"] = inspect_catalog_hash(source, source_data)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="解析 Unity Addressables catalog.bin")
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument(
        "--legacy-output",
        type=Path,
        help="另存为旧 Output.json 顶层结构兼容视图",
    )
    args = parser.parse_args()
    source = args.source.resolve()
    output = (
        args.output.resolve()
        if args.output
        else source.with_name(source.stem + "_parsed.json")
    )

    def report(done: int, total: int, locations: int) -> None:
        print(
            f"[catalog.bin] 解析 key: {done}/{total}，唯一 location={locations}",
            flush=True,
        )

    result = parse_catalog_file(source, output, progress=report)
    if args.legacy_output:
        legacy_path = args.legacy_output.resolve()
        write_legacy_output_view(result, legacy_path)
        print(f"[catalog.bin] Output.json 兼容视图: {legacy_path}")
    print(f"[catalog.bin] 解析完成: {output}")
    print(
        "[catalog.bin] "
        f"key={result['summary']['key_count']}，"
        f"location={result['summary']['unique_location_count']}，"
        f"bundle={result['summary']['asset_bundle_location_count']}"
    )
    hash_info = result["catalog_hash"]
    if hash_info["matches"] is True:
        print(
            f"[catalog.bin] catalog.hash 校验通过: {hash_info['calculated']}"
        )
    elif hash_info["matches"] is False:
        print(
            "[catalog.bin] catalog.hash 校验失败: "
            f"记录={hash_info['recorded']}，计算={hash_info['calculated']}"
        )
    else:
        print(
            f"[catalog.bin] 未找到 catalog.hash，计算值: {hash_info['calculated']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
