"""CSV 解析与上传校验。

换人 CSV 表头：period,remaining,outgoing,incoming
得分 CSV 表头：period,remaining,team,points,scorer

两表各自的行序就是比赛进程顺序：解析时校验 (节次升序、同节剩余秒数不增)，
不做重排，以保留原始行序。
"""

from __future__ import annotations

import csv
import io

from .engine import ScoreEvent, SubEvent

SUB_HEADER = ["period", "remaining", "outgoing", "incoming"]
SCORE_HEADER = ["period", "remaining", "team", "points", "scorer"]
TEAM_ALIASES = {
    "own": "own",
    "opp": "opp",
    "本方": "own",
    "对方": "opp",
}


class CsvError(ValueError):
    """CSV 内容不合法，信息可直接展示给用户。"""


def _read_rows(raw: bytes, expected: list[str], label: str):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvError(f"{label} CSV 必须是 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if row and any(cell.strip() for cell in row)]
    if not rows:
        raise CsvError(f"{label} CSV 为空")
    header = [cell.strip() for cell in rows[0]]
    if header != expected:
        raise CsvError(
            f"{label} CSV 表头应为 {','.join(expected)}，实际为 {','.join(header)}"
        )
    return rows[1:]


def _as_int(value: str, label: str, row_no: int) -> int:
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        raise CsvError(f"{label} CSV 第 {row_no} 行的 {value!r} 不是整数")


def parse_subs(raw: bytes) -> list[SubEvent]:
    rows = _read_rows(raw, SUB_HEADER, "换人")
    events: list[SubEvent] = []
    last_key: tuple[int, int] | None = None
    for idx, row in enumerate(rows, start=1):
        if len(row) != len(SUB_HEADER):
            raise CsvError(f"换人 CSV 第 {idx} 行列数不是 {len(SUB_HEADER)}")
        period = _as_int(row[0], "换人", idx)
        remaining = _as_int(row[1], "换人", idx)
        outgoing = row[2].strip()
        incoming = row[3].strip()
        if period < 1:
            raise CsvError(f"换人 CSV 第 {idx} 行节次非法：{period}")
        if remaining < 0 or remaining > 3600:
            raise CsvError(f"换人 CSV 第 {idx} 行剩余秒数越界：{remaining}")
        if not outgoing or not incoming:
            raise CsvError(f"换人 CSV 第 {idx} 行缺少上下场球员")
        if outgoing == incoming:
            raise CsvError(f"换人 CSV 第 {idx} 行上下场球员不能相同")
        key = (period, -remaining)
        if last_key is not None and key < last_key:
            raise CsvError(
                f"换人 CSV 第 {idx} 行的比赛时刻早于上一行，"
                "两表行序须按比赛进程排列，不能自动重排"
            )
        last_key = key
        events.append(
            SubEvent(
                period=period,
                remaining=remaining,
                outgoing=outgoing,
                incoming=incoming,
                row_no=idx,
            )
        )
    return events


def parse_scores(raw: bytes) -> list[ScoreEvent]:
    rows = _read_rows(raw, SCORE_HEADER, "得分")
    events: list[ScoreEvent] = []
    last_key: tuple[int, int] | None = None
    for idx, row in enumerate(rows, start=1):
        if len(row) != len(SCORE_HEADER):
            raise CsvError(f"得分 CSV 第 {idx} 行列数不是 {len(SCORE_HEADER)}")
        period = _as_int(row[0], "得分", idx)
        remaining = _as_int(row[1], "得分", idx)
        team_raw = row[2].strip()
        points = _as_int(row[3], "得分", idx)
        scorer = row[4].strip() or None
        if period < 1:
            raise CsvError(f"得分 CSV 第 {idx} 行节次非法：{period}")
        if remaining < 0 or remaining > 3600:
            raise CsvError(f"得分 CSV 第 {idx} 行剩余秒数越界：{remaining}")
        team = TEAM_ALIASES.get(team_raw)
        if team is None:
            raise CsvError(
                f"得分 CSV 第 {idx} 行队伍必须是 own/opp（或 本方/对方）"
            )
        if points not in (1, 2, 3):
            raise CsvError(f"得分 CSV 第 {idx} 行分值必须为 1、2 或 3")
        if team == "own" and not scorer:
            raise CsvError(f"得分 CSV 第 {idx} 行本方得分必须填写得分者")
        if team == "opp" and scorer:
            raise CsvError(f"得分 CSV 第 {idx} 行对方得分不应填写得分者")
        key = (period, -remaining)
        if last_key is not None and key < last_key:
            raise CsvError(
                f"得分 CSV 第 {idx} 行的比赛时刻早于上一行，"
                "两表行序须按比赛进程排列，不能自动重排"
            )
        last_key = key
        events.append(
            ScoreEvent(
                period=period,
                remaining=remaining,
                team=team,
                points=points,
                scorer=scorer,
                row_no=idx,
            )
        )
    return events


def parse_starters(raw: str) -> list[str]:
    names = [part.strip() for part in raw.replace("，", ",").split(",")]
    names = [n for n in names if n]
    if len(names) != 5:
        raise CsvError("首发必须填写 5 名球员（用英文逗号分隔）")
    if len(set(names)) != 5:
        raise CsvError("首发五人不能重名")
    return names
