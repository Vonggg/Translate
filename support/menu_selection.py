from __future__ import annotations


def parse_number_ranges(raw: str, valid_numbers: set[int]) -> list[str]:
    normalized = raw.replace("，", ",").strip()
    if not normalized:
        raise ValueError("没有输入脚本编号")

    selected: list[str] = []
    for token in (part.strip() for part in normalized.split(",")):
        if not token:
            continue
        if "-" in token:
            parts = [part.strip() for part in token.split("-", 1)]
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValueError(f"无效范围: {token}")
            start, end = map(int, parts)
            if start > end:
                raise ValueError(f"范围起点不能大于终点: {token}")
            numbers = range(start, end + 1)
        elif token.isdigit():
            numbers = (int(token),)
        else:
            raise ValueError(f"无效编号: {token}")

        for number in numbers:
            if number not in valid_numbers:
                raise ValueError(f"脚本编号超出范围: {number}")
            value = str(number)
            if value not in selected:
                selected.append(value)
    if not selected:
        raise ValueError("没有输入有效脚本编号")
    return selected
