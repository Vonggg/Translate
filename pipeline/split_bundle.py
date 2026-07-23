from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path


SPLIT_SUFFIX_RE = re.compile(r"^(?P<base>.+)\.split(?P<index>\d+)$", re.IGNORECASE)


@dataclass(frozen=True)
class SplitBundleGroup:
    base_path: Path
    parts: tuple[Path, ...]
    part_indexes: tuple[int, ...]

    @property
    def total_size(self) -> int:
        return sum(path.stat().st_size for path in self.parts if path.is_file())

    @property
    def is_contiguous_from_zero(self) -> bool:
        if not self.part_indexes or self.part_indexes[0] != 0:
            return False
        return self.part_indexes == tuple(range(len(self.part_indexes)))


def find_split_bundle_groups(root: Path) -> list[SplitBundleGroup]:
    grouped: dict[Path, list[tuple[int, Path]]] = {}
    if not root.is_dir():
        return []

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        match = SPLIT_SUFFIX_RE.match(path.name)
        if match is None:
            continue
        base_path = path.with_name(match.group("base"))
        grouped.setdefault(base_path, []).append((int(match.group("index")), path))

    groups: list[SplitBundleGroup] = []
    for base_path, indexed_parts in grouped.items():
        indexed_parts.sort(key=lambda item: item[0])
        groups.append(
            SplitBundleGroup(
                base_path=base_path,
                parts=tuple(path for _index, path in indexed_parts),
                part_indexes=tuple(index for index, _path in indexed_parts),
            )
        )
    groups.sort(key=lambda group: str(group.base_path).lower())
    return groups


def print_split_bundle_report(groups: list[SplitBundleGroup]) -> None:
    if not groups:
        print("[分卷] 未发现 .splitN 分卷文件。")
        return

    print(f"[分卷] 发现 {len(groups)} 组 .splitN 分卷：")
    for group in groups:
        status = "连续" if group.is_contiguous_from_zero else "缺失或序号异常"
        exists = "目标已存在" if group.base_path.exists() else "目标不存在"
        print(
            f"[分卷] {group.base_path} <- {len(group.parts)} 个分卷，"
            f"序号={','.join(str(index) for index in group.part_indexes)}，"
            f"大小={group.total_size / 1024 / 1024:.1f} MB，{status}，{exists}"
        )


def merge_split_bundle_group(group: SplitBundleGroup, overwrite: bool = True, delete_parts: bool = True) -> Path:
    if not group.is_contiguous_from_zero:
        raise ValueError(f"分卷序号不连续，不能合并: {group.base_path}")
    if group.base_path.exists() and not overwrite:
        raise FileExistsError(group.base_path)

    temp_path = group.base_path.with_name(group.base_path.name + ".merge_tmp")
    if temp_path.exists():
        temp_path.unlink()

    group.base_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with temp_path.open("wb") as output:
            for part in group.parts:
                with part.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        temp_path.replace(group.base_path)
        if delete_parts:
            for part in group.parts:
                part.unlink()
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return group.base_path


def merge_split_bundle_groups(groups: list[SplitBundleGroup], overwrite: bool = True, delete_parts: bool = True) -> int:
    merged = 0
    for group in groups:
        if not group.is_contiguous_from_zero:
            print(f"[分卷] 跳过序号异常分卷: {group.base_path}")
            continue
        output_path = merge_split_bundle_group(group, overwrite=overwrite, delete_parts=delete_parts)
        merged += 1
        suffix = "，已删除原分卷" if delete_parts else ""
        print(f"[分卷] 已合并: {output_path} ({group.total_size / 1024 / 1024:.1f} MB){suffix}")
    return merged
