"""CSV 解析与校验。

表头同时支持中英文：孔号/hole_no、层顶深度/top_depth、
层底深度/bottom_depth、岩性/lithology。
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass

HEADER_ALIASES = {
    "hole_no": {"孔号", "钻孔编号", "钻孔号", "hole", "hole_no", "holeno", "id"},
    "top_depth": {"层顶深度", "层顶", "顶深", "top", "top_depth", "topdepth"},
    "bottom_depth": {"层底深度", "层底", "底深", "bottom", "bottom_depth", "bottomdepth"},
    "lithology": {"岩性", "岩性描述", "lithology", "litho", "rock"},
}
REQUIRED = ["hole_no", "top_depth", "bottom_depth", "lithology"]


class CSVValidationError(ValueError):
    pass


@dataclass
class ParsedRow:
    row_no: int
    hole_no: str
    top_depth: float
    bottom_depth: float
    lithology: str

    def as_dict(self) -> dict:
        return {
            "row_no": self.row_no,
            "hole_no": self.hole_no,
            "top_depth": self.top_depth,
            "bottom_depth": self.bottom_depth,
            "lithology": self.lithology,
        }


def file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_csv(data: bytes) -> list[dict]:
    """解析并校验 CSV，返回行字典列表；非法时抛 CSVValidationError。"""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise CSVValidationError(f"文件编码无法识别（需 UTF-8 或 GB18030）：{exc}")

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CSVValidationError("CSV 为空，缺少表头")

    norm = [h.strip().lstrip("﻿") for h in header]
    col_index: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        for idx, name in enumerate(norm):
            if name.lower() in aliases:
                col_index[field] = idx
                break
    missing = [f for f in REQUIRED if f not in col_index]
    if missing:
        raise CSVValidationError(
            "缺少必需列：" + ", ".join(missing)
            + f"；实际表头：{norm}"
        )

    out: list[dict] = []
    for line_idx, raw in enumerate(reader, start=2):  # 含表头的文件行号
        if not raw or all(not (c or "").strip() for c in raw):
            continue  # 跳过空行
        row_no = len(out) + 1
        try:
            hole = raw[col_index["hole_no"]].strip()
            top_s = raw[col_index["top_depth"]].strip()
            bottom_s = raw[col_index["bottom_depth"]].strip()
            litho = raw[col_index["lithology"]].strip()
        except IndexError:
            raise CSVValidationError(f"第 {line_idx} 行列数不足")

        if not hole:
            raise CSVValidationError(f"第 {line_idx} 行孔号为空")
        if not litho:
            raise CSVValidationError(f"第 {line_idx} 行岩性为空")
        try:
            top = float(top_s)
            bottom = float(bottom_s)
        except ValueError:
            raise CSVValidationError(
                f"第 {line_idx} 行深度无法解析为数字：top={top_s!r}, bottom={bottom_s!r}"
            )
        if bottom <= top:
            raise CSVValidationError(
                f"第 {line_idx} 行层底深度({bottom}) 必须大于层顶深度({top})"
            )
        out.append(ParsedRow(row_no, hole, top, bottom, litho).as_dict())

    if not out:
        raise CSVValidationError("CSV 没有任何有效数据行")
    return out
