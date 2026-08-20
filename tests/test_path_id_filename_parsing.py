import unittest
from pathlib import Path

from pipeline.translation import _extract_asset_path_id_from_json_path


class PathIdFilenameParsingTests(unittest.TestCase):
    def test_positive_path_id(self) -> None:
        path = Path("MonoBehaviour/SR__4383.json")
        self.assertEqual(_extract_asset_path_id_from_json_path(path), 4383)

    def test_negative_path_id(self) -> None:
        path = Path(
            "Material/open-sansButtonAdd_-7016150626872025121.json"
        )
        self.assertEqual(
            _extract_asset_path_id_from_json_path(path),
            -7016150626872025121,
        )

    def test_negative_path_id_is_not_mistaken_for_type_id(self) -> None:
        path = Path(
            "MonoBehaviour/114_-2458844240491055788_-2458844240491055788.json"
        )
        self.assertEqual(
            _extract_asset_path_id_from_json_path(path),
            -2458844240491055788,
        )


if __name__ == "__main__":
    unittest.main()
