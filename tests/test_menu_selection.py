from __future__ import annotations

import unittest

from support.menu_selection import parse_number_ranges


class MenuSelectionTests(unittest.TestCase):
    def test_range_can_include_two_digit_script_ten(self) -> None:
        self.assertEqual(
            ["8", "9", "10"],
            parse_number_ranges("8-10", set(range(11))),
        )


if __name__ == "__main__":
    unittest.main()
