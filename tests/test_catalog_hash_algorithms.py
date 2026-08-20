from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from tools.catalog_bin_tool import (
    CATALOG_HASH_MD5,
    CATALOG_HASH_SPOOKY128,
    calculate_catalog_hash,
    inspect_catalog_hash,
    write_catalog_hash,
)


class CatalogHashAlgorithmTests(unittest.TestCase):
    DATA = b"binary catalog test data"

    def test_detects_md5_catalog_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.bin"
            catalog.write_bytes(self.DATA)
            catalog.with_suffix(".hash").write_text(
                hashlib.md5(self.DATA).hexdigest(),
                encoding="ascii",
            )

            result = inspect_catalog_hash(catalog, self.DATA)

            self.assertTrue(result["matches"])
            self.assertEqual(result["algorithm_id"], CATALOG_HASH_MD5)
            self.assertEqual(result["algorithm"], "MD5")

    def test_detects_spookyhash128_catalog_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.bin"
            catalog.write_bytes(self.DATA)
            expected = calculate_catalog_hash(self.DATA, CATALOG_HASH_SPOOKY128)
            catalog.with_suffix(".hash").write_text(expected, encoding="ascii")

            result = inspect_catalog_hash(catalog, self.DATA)

            self.assertTrue(result["matches"])
            self.assertEqual(result["algorithm_id"], CATALOG_HASH_SPOOKY128)

    def test_reports_both_candidates_when_hash_does_not_match(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.bin"
            catalog.write_bytes(self.DATA)
            catalog.with_suffix(".hash").write_text("0" * 32, encoding="ascii")

            result = inspect_catalog_hash(catalog, self.DATA)

            self.assertFalse(result["matches"])
            self.assertIsNone(result["algorithm_id"])
            self.assertEqual(
                result["calculated_by_algorithm"][CATALOG_HASH_MD5],
                hashlib.md5(self.DATA).hexdigest(),
            )
            self.assertIn(
                CATALOG_HASH_SPOOKY128,
                result["calculated_by_algorithm"],
            )

    def test_write_catalog_hash_uses_requested_algorithm(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            catalog = Path(temp_dir) / "catalog.bin"
            catalog.write_bytes(self.DATA)

            hash_path = write_catalog_hash(catalog, algorithm=CATALOG_HASH_MD5)

            self.assertEqual(
                hash_path.read_text(encoding="ascii"),
                hashlib.md5(self.DATA).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
