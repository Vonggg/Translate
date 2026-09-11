from __future__ import annotations

from PIL import Image

from support.image_color_filter import HsvRange, filter_images_by_hsv_ranges


RED = HsvRange("red", 345, 15, 45, 100, 20, 100)
GREEN = HsvRange("green", 66, 165, 35, 100, 15, 100)


def _write_image(path, pixels: list[tuple[int, int, int, int]]) -> None:
    image = Image.new("RGBA", (len(pixels), 1))
    image.putdata(pixels)
    image.save(path)


def test_filter_accumulates_selected_colors_and_sorts_by_coverage(tmp_path) -> None:
    source = tmp_path / "Sprite" / "PNG"
    nested = source / "nested"
    nested.mkdir(parents=True)
    _write_image(source / "high.png", [(255, 0, 0, 255)] * 55 + [(0, 255, 0, 255)] * 5 + [(0, 0, 0, 0)] * 40)
    _write_image(nested / "medium.png", [(255, 0, 0, 255)] * 20 + [(0, 0, 0, 0)] * 80)
    _write_image(source / "low.png", [(0, 255, 0, 255)] * 2 + [(0, 0, 0, 0)] * 98)
    _write_image(source / "below.png", [(255, 0, 0, 255)] + [(0, 0, 0, 0)] * 199)

    result = filter_images_by_hsv_ranges(source, tmp_path / "out", [RED, GREEN])

    assert result.scanned_count == 4
    assert result.matched_count == 3
    assert result.unreadable_count == 0
    assert result.high_coverage_count == 1
    assert result.medium_coverage_count == 1
    assert result.low_coverage_count == 1
    assert (tmp_path / "out" / "51-100%" / "high.png").is_file()
    assert (tmp_path / "out" / "10-50%" / "nested" / "medium.png").is_file()
    assert (tmp_path / "out" / "1-10%" / "low.png").is_file()
    assert not (tmp_path / "out" / "1-10%" / "below.png").exists()
