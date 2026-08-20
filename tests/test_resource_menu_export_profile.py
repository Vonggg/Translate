from __future__ import annotations

import unittest
from unittest.mock import patch

import resource_menu


class ResourceMenuExportProfileTests(unittest.TestCase):
    def _select(self, raw: str) -> str | None:
        with patch.object(resource_menu, "prompt_input", return_value=raw):
            return resource_menu.prompt_export_profile()

    def test_single_profile(self) -> None:
        self.assertEqual(self._select("1"), "basic")

    def test_range_profiles(self) -> None:
        self.assertEqual(self._select("1-3"), "basic+objects+mesh")

    def test_comma_separated_profiles(self) -> None:
        self.assertEqual(self._select("1,3"), "basic+mesh")

    def test_all_profiles(self) -> None:
        self.assertEqual(self._select("a"), "all")

    def test_cancel(self) -> None:
        self.assertIsNone(self._select("q"))


if __name__ == "__main__":
    unittest.main()
