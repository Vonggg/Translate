from __future__ import annotations

import copy
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence, Tuple, Union


OBB_SOURCE_MAP_VERSION = 1
_ALLOWED_RESOURCE_PREFIXES = ("assets/aa/", "assets/bin/Data/")
_COPY_BUFFER_SIZE = 1024 * 1024
_NEW_ENTRY_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_SPLIT_ENTRY_RE = re.compile(r"^(?P<prefix>.+\.split)(?P<index>\d+)$")

Replacement = Union[bytes, bytearray, memoryview, str, os.PathLike]


class UnsafeObbEntryError(ValueError):
    """Raised when a ZIP entry cannot be mapped to a safe local path."""


@dataclass(frozen=True)
class ObbEntryMetadata:
    """Persistent mapping between one extracted resource and its OBB entry."""

    container_path: str
    entry_name: str
    extracted_relative: str
    file_size: int
    compressed_size: int
    crc32: int
    compress_type: int
    date_time: Tuple[int, int, int, int, int, int]
    external_attr: int
    internal_attr: int
    create_system: int
    flag_bits: int

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ObbEntryMetadata":
        date_time = value.get("date_time")
        if not isinstance(date_time, (list, tuple)) or len(date_time) != 6:
            raise ValueError("OBB source map contains an invalid date_time")
        return cls(
            container_path=str(value["container_path"]),
            entry_name=str(value["entry_name"]),
            extracted_relative=str(value["extracted_relative"]),
            file_size=int(value["file_size"]),
            compressed_size=int(value["compressed_size"]),
            crc32=int(value["crc32"]),
            compress_type=int(value["compress_type"]),
            date_time=tuple(int(part) for part in date_time),  # type: ignore[arg-type]
            external_attr=int(value["external_attr"]),
            internal_attr=int(value["internal_attr"]),
            create_system=int(value["create_system"]),
            flag_bits=int(value["flag_bits"]),
        )


def discover_obb_files(root: Path) -> list[Path]:
    """Find every ``*.obb`` below a path segment named ``assets/obb``."""

    root = Path(root)
    if not root.is_dir():
        return []

    discovered: list[Path] = []
    for candidate in root.rglob("*"):
        if not candidate.is_file() or candidate.suffix.casefold() != ".obb":
            continue
        lowered_parts = [part.casefold() for part in candidate.parts]
        if any(
            lowered_parts[index : index + 2] == ["assets", "obb"]
            for index in range(len(lowered_parts) - 1)
        ):
            discovered.append(candidate)
    return sorted(discovered, key=lambda path: str(path).casefold())


def list_obb_resource_entries(obb_path: Path) -> list[ObbEntryMetadata]:
    """List safe file entries below ``assets/aa`` and ``assets/bin/Data``.

    Every archive member is path-validated before results are returned.  This
    prevents a malformed member from being overlooked merely because it is not
    one of the resource entries requested by the caller.
    """

    source = Path(obb_path)
    container_path = str(source.resolve())
    entries: list[ObbEntryMetadata] = []
    extracted_names: set[str] = set()

    with zipfile.ZipFile(source, "r") as archive:
        for info in archive.infolist():
            normalized_name = _validated_entry_name(info.filename)
            if not _is_resource_entry(normalized_name) or info.is_dir():
                continue
            if _is_symlink(info):
                raise UnsafeObbEntryError(
                    f"OBB resource entry is a symbolic link: {info.filename!r}"
                )

            extracted_relative = _resource_relative(normalized_name)
            collision_key = extracted_relative.casefold()
            if collision_key in extracted_names:
                raise UnsafeObbEntryError(
                    "OBB contains resource paths that collide on a "
                    f"case-insensitive filesystem: {extracted_relative!r}"
                )
            extracted_names.add(collision_key)
            entries.append(
                ObbEntryMetadata(
                    container_path=container_path,
                    entry_name=normalized_name,
                    extracted_relative=extracted_relative,
                    file_size=info.file_size,
                    compressed_size=info.compress_size,
                    crc32=info.CRC,
                    compress_type=info.compress_type,
                    date_time=tuple(info.date_time),
                    external_attr=info.external_attr,
                    internal_attr=info.internal_attr,
                    create_system=info.create_system,
                    flag_bits=info.flag_bits,
                )
            )
    return entries


def extract_obb_resources(
    obb_path: Path,
    destination_root: Path,
    *,
    source_map_path: Path | None = None,
) -> list[ObbEntryMetadata]:
    """Safely extract resource entries and optionally persist their source map.

    ``assets/aa/x`` is written as ``destination_root/aa/x`` and
    ``assets/bin/Data/x`` as ``destination_root/bin/Data/x``.
    """

    source = Path(obb_path)
    entries = list_obb_resource_entries(source)
    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()

    # Resolve every target before writing the first file, so a bad existing
    # symlink cannot leave a partially extracted container behind.
    targets: dict[str, Path] = {}
    for entry in entries:
        target = (resolved_destination / Path(entry.extracted_relative)).resolve()
        try:
            target.relative_to(resolved_destination)
        except ValueError as exc:
            raise UnsafeObbEntryError(
                f"OBB entry escapes extraction root: {entry.entry_name!r}"
            ) from exc
        targets[entry.entry_name] = target

    with zipfile.ZipFile(source, "r") as archive:
        for entry in entries:
            target = targets[entry.entry_name]
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry.entry_name, "r") as source_stream, target.open(
                "wb"
            ) as destination_stream:
                shutil.copyfileobj(
                    source_stream, destination_stream, length=_COPY_BUFFER_SIZE
                )

    if source_map_path is not None:
        write_obb_source_map(source_map_path, entries)
    return entries


def write_obb_source_map(
    destination: Path, entries: Iterable[ObbEntryMetadata]
) -> Path:
    """Atomically save the container/entry mapping needed for later repacking."""

    destination = Path(destination)
    payload = {
        "version": OBB_SOURCE_MAP_VERSION,
        "entries": [asdict(entry) for entry in entries],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path_next_to(destination)
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(str(temporary), str(destination))
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def load_obb_source_map(source_map_path: Path) -> list[ObbEntryMetadata]:
    """Load a source map written by :func:`write_obb_source_map`."""

    payload = json.loads(Path(source_map_path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or payload.get("version") != OBB_SOURCE_MAP_VERSION:
        raise ValueError("Unsupported OBB source map version")
    values = payload.get("entries")
    if not isinstance(values, list):
        raise ValueError("OBB source map is missing its entries list")
    return [ObbEntryMetadata.from_dict(value) for value in values]


def write_obb_from_template(
    source_obb: Path,
    target_obb: Path,
    replacements: Mapping[str, Replacement],
) -> Path:
    """Write a new OBB while replacing selected resource entries.

    Unchanged entries retain their content, order, timestamp, compression type,
    comment, extra fields and platform attributes. New resource entries are
    appended in replacement-map order. A new split part inherits the ZIP
    metadata of the highest existing part in the same group; other new entries
    use deterministic stored-file metadata. The destination is replaced
    atomically only after the complete ZIP has been written successfully.
    """

    source = Path(source_obb)
    target = Path(target_obb)
    if source.resolve() == target.resolve():
        raise ValueError("target_obb must be different from source_obb")

    prepared_replacements = _prepare_replacements(replacements)
    with zipfile.ZipFile(source, "r") as source_archive:
        infos = source_archive.infolist()
        source_names = [info.filename for info in infos]
        seen_names: set[str] = set()
        duplicate_names: set[str] = set()
        for name in source_names:
            if name in seen_names:
                duplicate_names.add(name)
            seen_names.add(name)
        duplicate_targets = duplicate_names.intersection(prepared_replacements)
        if duplicate_targets:
            names = ", ".join(sorted(duplicate_targets))
            raise ValueError(f"Cannot replace duplicate OBB entries: {names}")

        new_entry_names = [
            name for name in prepared_replacements if name not in seen_names
        ]

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = _temporary_path_next_to(target)
        try:
            with zipfile.ZipFile(
                temporary, "w", allowZip64=True, strict_timestamps=False
            ) as target_archive:
                target_archive.comment = source_archive.comment
                for info in infos:
                    output_info = copy.copy(info)
                    replacement = prepared_replacements.get(info.filename)
                    if info.is_dir():
                        target_archive.writestr(output_info, b"")
                        continue

                    if replacement is None:
                        source_stream = source_archive.open(info, "r")
                        output_size = info.file_size
                    elif isinstance(replacement, bytes):
                        source_stream = io.BytesIO(replacement)
                        output_size = len(replacement)
                    else:
                        source_stream = replacement.open("rb")
                        output_size = replacement.stat().st_size

                    with source_stream, target_archive.open(
                        output_info,
                        "w",
                        force_zip64=output_size > zipfile.ZIP64_LIMIT,
                    ) as target_stream:
                        shutil.copyfileobj(
                            source_stream, target_stream, length=_COPY_BUFFER_SIZE
                        )

                for name in new_entry_names:
                    replacement = prepared_replacements[name]
                    output_info = _new_resource_entry_info(name, infos)
                    if isinstance(replacement, bytes):
                        source_stream = io.BytesIO(replacement)
                        output_size = len(replacement)
                    else:
                        source_stream = replacement.open("rb")
                        output_size = replacement.stat().st_size

                    with source_stream, target_archive.open(
                        output_info,
                        "w",
                        force_zip64=output_size > zipfile.ZIP64_LIMIT,
                    ) as target_stream:
                        shutil.copyfileobj(
                            source_stream, target_stream, length=_COPY_BUFFER_SIZE
                        )

            os.replace(str(temporary), str(target))
        finally:
            temporary.unlink(missing_ok=True)
    return target


def _new_resource_entry_info(
    name: str, existing_infos: Sequence[zipfile.ZipInfo]
) -> zipfile.ZipInfo:
    split_match = _SPLIT_ENTRY_RE.fullmatch(name)
    if split_match is not None:
        prefix = split_match.group("prefix")
        split_candidates: list[tuple[int, zipfile.ZipInfo]] = []
        for candidate in existing_infos:
            candidate_match = _SPLIT_ENTRY_RE.fullmatch(candidate.filename)
            if (
                candidate_match is not None
                and candidate_match.group("prefix") == prefix
                and not candidate.is_dir()
            ):
                split_candidates.append(
                    (int(candidate_match.group("index")), candidate)
                )
        if split_candidates:
            template = max(split_candidates, key=lambda value: value[0])[1]
            info = zipfile.ZipInfo(name, template.date_time)
            info.compress_type = template.compress_type
            info.create_system = template.create_system
            info.create_version = template.create_version
            info.extract_version = template.extract_version
            info.internal_attr = template.internal_attr
            info.external_attr = template.external_attr
            return info

    info = zipfile.ZipInfo(name, _NEW_ENTRY_DATE_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.create_version = 20
    info.extract_version = 20
    info.internal_attr = 0
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    return info


def _prepare_replacements(
    replacements: Mapping[str, Replacement],
) -> dict[str, Union[bytes, Path]]:
    prepared: dict[str, Union[bytes, Path]] = {}
    for raw_name, replacement in replacements.items():
        name = _validated_entry_name(str(raw_name))
        if not _is_resource_entry(name):
            raise ValueError(
                "Replacement entry must be below assets/aa or assets/bin/Data: "
                f"{raw_name!r}"
            )
        if name in prepared:
            raise ValueError(f"Duplicate replacement entry: {name}")
        if isinstance(replacement, (bytes, bytearray, memoryview)):
            prepared[name] = bytes(replacement)
            continue
        replacement_path = Path(replacement)
        if not replacement_path.is_file():
            raise FileNotFoundError(replacement_path)
        prepared[name] = replacement_path
    return prepared


def _validated_entry_name(name: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")
    if name.startswith(("/", "//")):
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")

    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts:
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")
    if any(":" in part for part in parts):
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")
    normalized = path.as_posix()
    if normalized != name.rstrip("/"):
        raise UnsafeObbEntryError(f"Unsafe OBB entry path: {name!r}")
    return normalized


def _is_resource_entry(name: str) -> bool:
    return name.startswith(_ALLOWED_RESOURCE_PREFIXES)


def _resource_relative(name: str) -> str:
    return PurePosixPath(name).relative_to("assets").as_posix()


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return info.create_system == 3 and stat.S_ISLNK(info.external_attr >> 16)


def _temporary_path_next_to(destination: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    return Path(name)
