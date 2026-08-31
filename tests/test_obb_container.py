from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from pipeline.obb_container import (
    UnsafeObbEntryError,
    discover_obb_files,
    extract_obb_resources,
    list_obb_resource_entries,
    load_obb_source_map,
    write_obb_from_template,
)


class ObbContainerTests(unittest.TestCase):
    def test_discovers_obb_files_only_below_assets_obb_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "game" / "assets" / "obb" / "main.1.obb"
            second = root / "game" / "assets" / "obb" / "nested" / "patch.OBB"
            ignored = root / "game" / "assets" / "aa" / "not-an-obb.obb"
            for path in (first, second, ignored):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"file")

            self.assertEqual(discover_obb_files(root), [first, second])

    def test_lists_extracts_and_saves_resource_source_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "assets" / "obb" / "main.obb"
            source.parent.mkdir(parents=True)
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("assets/aa/catalog.bin", b"catalog")
                archive.writestr("assets/aa/Android/content.bundle", b"bundle")
                archive.writestr("assets/bin/Data/globalgamemanagers", b"data")
                archive.writestr("assets/unrelated.txt", b"ignored")

            listed = list_obb_resource_entries(source)
            self.assertEqual(
                [entry.entry_name for entry in listed],
                [
                    "assets/aa/catalog.bin",
                    "assets/aa/Android/content.bundle",
                    "assets/bin/Data/globalgamemanagers",
                ],
            )

            destination = root / "extracted"
            source_map = root / "state" / "obb_source_map.json"
            extracted = extract_obb_resources(
                source, destination, source_map_path=source_map
            )

            self.assertEqual((destination / "aa" / "catalog.bin").read_bytes(), b"catalog")
            self.assertEqual(
                (destination / "aa" / "Android" / "content.bundle").read_bytes(),
                b"bundle",
            )
            self.assertEqual(
                (destination / "bin" / "Data" / "globalgamemanagers").read_bytes(),
                b"data",
            )
            self.assertFalse((destination / "unrelated.txt").exists())
            self.assertEqual(load_obb_source_map(source_map), extracted)
            payload = json.loads(source_map.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(
                payload["entries"][1]["extracted_relative"],
                "aa/Android/content.bundle",
            )
            self.assertEqual(
                Path(payload["entries"][1]["container_path"]), source.resolve()
            )

    def test_rejects_zip_slip_before_extracting_any_resource(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "bad.obb"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("assets/aa/good.bundle", b"good")
                archive.writestr("assets/aa/../../../escaped.bundle", b"bad")

            destination = root / "destination"
            with self.assertRaises(UnsafeObbEntryError):
                extract_obb_resources(source, destination)

            self.assertFalse((root / "escaped.bundle").exists())
            self.assertFalse((destination / "aa" / "good.bundle").exists())

    def test_writes_replacements_from_template_and_preserves_other_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.obb"
            target = root / "output" / "result.obb"
            replacement_bundle = root / "changed.bundle"
            replacement_bundle.write_bytes(b"new bundle")

            with zipfile.ZipFile(source, "w") as archive:
                archive.comment = b"original comment"
                self._write_entry(
                    archive,
                    "assets/aa/catalog.bin",
                    b"old catalog",
                    zipfile.ZIP_DEFLATED,
                    (2022, 3, 4, 5, 6, 8),
                    0o100640 << 16,
                )
                self._write_entry(
                    archive,
                    "assets/aa/Android/content.bundle",
                    b"old bundle",
                    zipfile.ZIP_STORED,
                    (2021, 7, 8, 9, 10, 12),
                    0o100600 << 16,
                )
                self._write_entry(
                    archive,
                    "assets/bin/Data/globalgamemanagers",
                    b"unchanged data",
                    zipfile.ZIP_DEFLATED,
                    (2020, 1, 2, 3, 4, 6),
                    0o100644 << 16,
                )
                self._write_entry(
                    archive,
                    "META-INF/manifest.txt",
                    b"unchanged manifest",
                    zipfile.ZIP_STORED,
                    (2019, 11, 12, 13, 14, 16),
                    0o100444 << 16,
                )

            original_bytes = source.read_bytes()
            write_obb_from_template(
                source,
                target,
                {
                    "assets/aa/catalog.bin": b"new catalog",
                    "assets/aa/Android/content.bundle": replacement_bundle,
                },
            )

            self.assertEqual(source.read_bytes(), original_bytes)
            with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
                target, "r"
            ) as rebuilt:
                self.assertEqual(rebuilt.comment, original.comment)
                self.assertEqual(rebuilt.read("assets/aa/catalog.bin"), b"new catalog")
                self.assertEqual(
                    rebuilt.read("assets/aa/Android/content.bundle"), b"new bundle"
                )
                self.assertEqual(
                    rebuilt.read("assets/bin/Data/globalgamemanagers"),
                    b"unchanged data",
                )
                self.assertEqual(
                    rebuilt.read("META-INF/manifest.txt"), b"unchanged manifest"
                )
                self.assertEqual(original.namelist(), rebuilt.namelist())
                for original_info, rebuilt_info in zip(
                    original.infolist(), rebuilt.infolist()
                ):
                    self.assertEqual(rebuilt_info.date_time, original_info.date_time)
                    self.assertEqual(
                        rebuilt_info.compress_type, original_info.compress_type
                    )
                    self.assertEqual(
                        rebuilt_info.external_attr, original_info.external_attr
                    )
                    self.assertEqual(
                        rebuilt_info.internal_attr, original_info.internal_attr
                    )
                    self.assertEqual(
                        rebuilt_info.create_system, original_info.create_system
                    )

    def test_failed_write_does_not_overwrite_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.obb"
            target = root / "result.obb"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("assets/aa/catalog.bin", b"old")
            target.write_bytes(b"existing target")

            with patch(
                "pipeline.obb_container.shutil.copyfileobj",
                side_effect=OSError("simulated write failure"),
            ), self.assertRaisesRegex(OSError, "simulated write failure"):
                write_obb_from_template(source, target, {})

            self.assertEqual(target.read_bytes(), b"existing target")
            self.assertEqual(list(root.glob(".result.obb.*.tmp")), [])

    def test_appends_new_split_and_data_entries_with_explicit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.obb"
            target = root / "result.obb"
            split_name = "assets/aa/Android/content.bundle.split0"
            new_split_name = "assets/aa/Android/content.bundle.split1"
            new_data_name = "assets/bin/Data/level1"

            with zipfile.ZipFile(source, "w") as archive:
                self._write_entry(
                    archive,
                    split_name,
                    b"first part",
                    zipfile.ZIP_DEFLATED,
                    (2023, 4, 5, 6, 7, 8),
                    0o100640 << 16,
                )
                archive.writestr("META-INF/manifest.txt", b"unchanged")

            original_bytes = source.read_bytes()
            write_obb_from_template(
                source,
                target,
                {
                    new_split_name: b"second part",
                    new_data_name: b"new data",
                },
            )

            self.assertEqual(source.read_bytes(), original_bytes)
            with zipfile.ZipFile(source, "r") as original, zipfile.ZipFile(
                target, "r"
            ) as rebuilt:
                self.assertEqual(
                    rebuilt.namelist(),
                    original.namelist() + [new_split_name, new_data_name],
                )
                self.assertEqual(rebuilt.read(new_split_name), b"second part")
                self.assertEqual(rebuilt.read(new_data_name), b"new data")

                split_template = original.getinfo(split_name)
                new_split = rebuilt.getinfo(new_split_name)
                self.assertEqual(new_split.date_time, split_template.date_time)
                self.assertEqual(
                    new_split.compress_type, split_template.compress_type
                )
                self.assertEqual(new_split.create_system, split_template.create_system)
                self.assertEqual(new_split.internal_attr, split_template.internal_attr)
                self.assertEqual(new_split.external_attr, split_template.external_attr)

                new_data = rebuilt.getinfo(new_data_name)
                self.assertEqual(new_data.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(new_data.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(new_data.create_system, 3)
                self.assertEqual((new_data.external_attr >> 16) & 0o777, 0o644)

    def test_rejects_unsafe_new_replacement_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.obb"
            target = root / "result.obb"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("assets/aa/catalog.bin", b"catalog")

            with self.assertRaises(UnsafeObbEntryError):
                write_obb_from_template(
                    source,
                    target,
                    {"assets/aa/../../escaped.bundle": b"unsafe"},
                )

            self.assertFalse(target.exists())
            self.assertFalse((root / "escaped.bundle").exists())

    @staticmethod
    def _write_entry(
        archive: zipfile.ZipFile,
        name: str,
        content: bytes,
        compression: int,
        date_time: tuple[int, int, int, int, int, int],
        external_attr: int,
    ) -> None:
        info = zipfile.ZipInfo(name, date_time)
        info.compress_type = compression
        info.create_system = 3
        info.external_attr = external_attr
        info.internal_attr = 1
        info.comment = b"entry comment"
        info.extra = b"\xfe\xca\x00\x00"
        archive.writestr(info, content)


if __name__ == "__main__":
    unittest.main()
