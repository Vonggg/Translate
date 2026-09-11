"""Filter exported Sprite images by one or more HSV colour ranges."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import shutil
from typing import Iterable


IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tga", ".bmp", ".webp"})


@dataclass(frozen=True)
class HsvRange:
    """An HSV range using H=0..359 and S/V=0..100.

    ``hue_min > hue_max`` means the range crosses red's 0-degree boundary.
    """

    label: str
    hue_min: int
    hue_max: int
    saturation_min: int = 0
    saturation_max: int = 100
    value_min: int = 0
    value_max: int = 100

    def contains(self, hue: int, saturation: int, value: int) -> bool:
        hue_matches = self.hue_matches(hue)
        return (
            hue_matches
            and self.saturation_min <= saturation <= self.saturation_max
            and self.value_min <= value <= self.value_max
        )

    def hue_matches(self, hue: int) -> bool:
        return (
            self.hue_min <= hue <= self.hue_max
            if self.hue_min <= self.hue_max
            else hue >= self.hue_min or hue <= self.hue_max
        )


COMMON_COLOR_RANGES: tuple[HsvRange, ...] = (
    HsvRange("红色", 345, 15, 45, 100, 20, 100),
    HsvRange("橙色", 16, 45, 45, 100, 20, 100),
    HsvRange("黄色", 46, 65, 40, 100, 25, 100),
    HsvRange("绿色", 66, 165, 35, 100, 15, 100),
    HsvRange("青色", 166, 195, 35, 100, 15, 100),
    HsvRange("蓝色", 196, 260, 35, 100, 15, 100),
    HsvRange("紫色", 261, 300, 35, 100, 15, 100),
    HsvRange("粉色", 301, 344, 25, 100, 35, 100),
    HsvRange("白色", 0, 359, 0, 20, 80, 100),
    HsvRange("黑色", 0, 359, 0, 100, 0, 20),
    HsvRange("灰色", 0, 359, 0, 15, 20, 80),
)


@dataclass(frozen=True)
class ColorFilterResult:
    scanned_count: int
    matched_count: int
    unreadable_count: int
    destination: Path
    high_coverage_count: int
    medium_coverage_count: int
    low_coverage_count: int


def _hsv_percent(raw: int) -> int:
    return raw * 100 // 255


@lru_cache(maxsize=64)
def _range_luts(color_range: HsvRange) -> tuple[list[int], list[int], list[int]]:
    """Build Pillow ``point`` lookup tables once per selected colour range."""

    hue_lut = [255 if color_range.hue_matches(raw * 360 // 256) else 0 for raw in range(256)]
    saturation_lut = [
        255
        if color_range.saturation_min <= _hsv_percent(raw) <= color_range.saturation_max
        else 0
        for raw in range(256)
    ]
    value_lut = [
        255 if color_range.value_min <= _hsv_percent(raw) <= color_range.value_max else 0
        for raw in range(256)
    ]
    return hue_lut, saturation_lut, value_lut


@lru_cache(maxsize=256)
def _alpha_lut(min_alpha: int) -> list[int]:
    return [255 if raw >= min_alpha else 0 for raw in range(256)]


def image_hsv_coverage_percent(
    path: Path,
    ranges: Iterable[HsvRange],
    *,
    min_alpha: int = 16,
) -> float:
    """Return selected colours' combined coverage as a percentage of all pixels.

    A transparent pixel never counts as a selected colour, but it still belongs
    to the denominator because the requested percentage is relative to the
    complete Sprite canvas.  Overlapping custom ranges are unioned so a pixel
    cannot inflate the result above 100%.
    """

    from PIL import Image, ImageChops

    selected = tuple(ranges)
    if not selected:
        return 0.0
    with Image.open(path) as source:
        rgba = source.convert("RGBA")
        hue, saturation, value = rgba.convert("HSV").split()
        visible = rgba.getchannel("A").point(_alpha_lut(max(0, min_alpha)))
        combined = None
        for color_range in selected:
            hue_lut, saturation_lut, value_lut = _range_luts(color_range)
            # point/multiply/lighter/histogram run in Pillow's native code.
            mask = ImageChops.multiply(hue.point(hue_lut), saturation.point(saturation_lut))
            mask = ImageChops.multiply(mask, value.point(value_lut))
            combined = mask if combined is None else ImageChops.lighter(combined, mask)
        assert combined is not None
        hits = ImageChops.multiply(combined, visible).histogram()[255]
        return hits * 100.0 / (rgba.width * rgba.height)


def filter_images_by_hsv_ranges(
    source_root: Path,
    destination: Path,
    ranges: Iterable[HsvRange],
    *,
    min_alpha: int = 16,
) -> ColorFilterResult:
    """Copy matches into 51-100, 10-50 and 1-10 percent directories."""

    source_root = source_root.resolve()
    destination = destination.resolve()
    selected = tuple(ranges)
    if not source_root.is_dir():
        raise FileNotFoundError(f"拆分后的 Sprite 图片目录不存在: {source_root}")
    if not selected:
        raise ValueError("至少需要选择一个颜色区间。")

    scanned = matched = unreadable = 0
    high = medium = low = 0
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        scanned += 1
        try:
            coverage = image_hsv_coverage_percent(
                path,
                selected,
                min_alpha=min_alpha,
            )
        except (OSError, ValueError):
            unreadable += 1
            continue
        if coverage > 50.0:
            tier = "51-100%"
            high += 1
        elif coverage >= 10.0:
            tier = "10-50%"
            medium += 1
        elif coverage >= 1.0:
            tier = "1-10%"
            low += 1
        else:
            continue
        target = destination / tier / path.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        matched += 1
    return ColorFilterResult(scanned, matched, unreadable, destination, high, medium, low)


__all__ = [
    "COMMON_COLOR_RANGES",
    "ColorFilterResult",
    "HsvRange",
    "filter_images_by_hsv_ranges",
    "image_hsv_coverage_percent",
]
