"""Optional resource transforms. Algorithms and key discovery are independent.

Never transform source-game files: export operates on staging; import on results.
Unknown schemes remain untouched. Recognized but invalid bundles stop the export.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def xor_repeat(data: bytes, key: bytes) -> bytes:
    if not key or len(key) > 4096:
        raise ValueError("XOR key must contain 1..4096 bytes")
    result = bytearray(data)
    for index, value in enumerate(key):
        result[index::len(key)] = data[index::len(key)].translate(bytes(x ^ value for x in range(256)))
    return bytes(result)


ALGORITHMS = {"xor_repeat": xor_repeat}


def settings_path(workspace: Path) -> Path:
    return workspace / "resource_state" / "resource_crypto.json"


def load_candidates(game: Path, workspace: Path) -> list[dict]:
    path = settings_path(workspace)
    settings = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError(f"资源加密配置必须是对象: {path}")
    if settings.get("enabled", True) is False:
        return []
    candidates = []
    for row in settings.get("profiles", []):
        algorithm = row["algorithm"]
        if algorithm not in ALGORITHMS:
            raise ValueError(f"不支持的资源加密算法: {algorithm}")
        key = bytes.fromhex(row["key_hex"])
        if not key or len(key) > 4096:
            raise ValueError("资源加密密钥长度无效")
        candidates.append({"algorithm": algorithm, "key_hex": key.hex(), "key_source": "configured"})
    if settings.get("auto_detect", True):
        try:
            package = ET.parse(game / "AndroidManifest.xml").getroot().get("package", "")
        except (OSError, ET.ParseError):
            package = ""
        if package:
            # BitConverter.ToString(MD5(ASCII(Application.identifier))).Replace("-", "")
            key = hashlib.md5(package.encode("ascii", errors="replace")).hexdigest().upper().encode("ascii")
            candidates.append({"algorithm": "xor_repeat", "key_hex": key.hex(),
                               "key_source": "android_package_md5_upper_ascii"})
    return candidates


def validate_bundle(data: bytes) -> int:
    if not data.startswith(b"UnityFS\0"):
        raise ValueError("不是标准 UnityFS 文件")
    position = 12
    for _ in range(2):
        end = data.find(b"\0", position, position + 256)
        if end < 0:
            raise ValueError("UnityFS 版本头损坏")
        position = end + 1
    if len(data) < position + 20 or struct.unpack_from(">Q", data, position)[0] != len(data):
        raise ValueError("UnityFS 文件大小校验失败")
    import UnityPy
    from UnityPy.files import BundleFile
    env = UnityPy.load(data)
    if not any(isinstance(file, BundleFile) for file in env.files.values()):
        raise ValueError("UnityFS 解压/目录解析失败")
    return len(env.objects)


def atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def decrypt_staged(path: Path, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    with path.open("rb") as stream:
        header = stream.read(8)
    if header == b"UnityFS\0" or len(header) < 8:
        return None
    for profile in candidates:
        transform = ALGORITHMS[profile["algorithm"]]
        key = bytes.fromhex(profile["key_hex"])
        if transform(header, key) != b"UnityFS\0":
            continue
        original = path.read_bytes()
        decoded = transform(original, key)
        count = validate_bundle(decoded)
        if transform(decoded, key) != original:
            raise ValueError(f"资源加密往返校验失败: {path}")
        metadata = dict(profile, encrypted_sha256=hashlib.sha256(original).hexdigest(),
                        plaintext_sha256=hashlib.sha256(decoded).hexdigest(), object_count=count)
        atomic_write(path, decoded)
        return metadata
    return None


def encrypt_results(entries: list[dict], restored: dict[str, Path]) -> int:
    count = 0
    for entry in entries:
        metadata = entry.get("resource_crypto")
        path = restored.get(str(entry.get("staged_relative", "")))
        if not metadata or path is None:
            continue
        # Use the export-time key, never a newly changed package/configuration.
        transform = ALGORITHMS[metadata["algorithm"]]
        key = bytes.fromhex(metadata["key_hex"])
        original = path.read_bytes()
        validate_bundle(original)
        encrypted = transform(original, key)
        if transform(encrypted, key) != original:
            raise ValueError(f"导入资源重新加密校验失败: {path}")
        atomic_write(path, encrypted)
        count += 1
    if count:
        print(f"[资源加密] 已按导出时的算法和密钥重新加密 {count} 个修改包；往返字节校验通过。")
    return count
