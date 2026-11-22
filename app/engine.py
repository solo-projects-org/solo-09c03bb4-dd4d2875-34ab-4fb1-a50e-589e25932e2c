"""正负值区间计算引擎。

一场比赛的事件按 (节次, 剩余秒数) 确定的比赛时刻排序；同一时刻（同节同秒）
的多条换人 / 得分记录在两份 CSV 内各自保持原始行序，但两表之间可以交错。
引擎在所有满足规则的交错顺序上，用动态规划求每名本方球员正负值的可达最小值
和最大值。

规则：
* 某时刻之前的换人必须先于该时刻之后的任何事件（时刻之间顺序固定）。
* 同一时刻内，换人之间、得分之间保持各自表内行序；换人与得分可交错。
* 某笔换人执行时，下场球员必须在场、上场球员必须在场外，否则数据本身不合法。
* 本方得分执行时，得分者必须在场（对方得分不记录得分者）。
* 核实记录给同一时刻内的一笔换人、一笔得分附加确定的先后约束。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class SubEvent:
    """一笔换人。row_no 是换人 CSV 内从 1 开始的行号（不含表头）。"""

    period: int
    remaining: int
    outgoing: str
    incoming: str
    row_no: int


@dataclass(frozen=True)
class ScoreEvent:
    """一笔得分。row_no 是得分 CSV 内从 1 开始的行号（不含表头）。"""

    period: int
    remaining: int
    team: str  # 'own' 或 'opp'
    points: int
    scorer: Optional[str]
    row_no: int


@dataclass(frozen=True)
class OrderEdge:
    """一条同节同秒的先后核实：first 行先于 second 行。

    first/second 为 (表名, 行号)，表名为 'sub' 或 'score'。
    """

    period: int
    remaining: int
    first_table: str
    first_row: int
    second_table: str
    second_row: int


@dataclass
class GroupInfo:
    """同一时刻分组的说明信息，供页面展示。"""

    period: int
    remaining: int
    sub_rows: list[int] = field(default_factory=list)
    score_rows: list[int] = field(default_factory=list)
    legal_orders: int = 0
    ambiguous: bool = False


@dataclass
class InfeasibleError(Exception):
    """不存在满足全部规则与核实约束的合法交错顺序。"""

    reason: str
    period: Optional[int] = None
    remaining: Optional[int] = None

    def __str__(self) -> str:  # pragma: no cover - 仅为调试
        return self.reason


WAY_CAP = 10_000_000


def _group_events(subs: list[SubEvent], scores: list[ScoreEvent]):
    """按比赛时刻把两表事件分组，时刻顺序固定；组内保留两表各自行序。"""
    buckets: dict[tuple[int, int], dict[str, list]] = defaultdict(
        lambda: {"subs": [], "scores": []}
    )
    for ev in subs:
        buckets[(ev.period, ev.remaining)]["subs"].append(ev)
    for ev in scores:
        buckets[(ev.period, ev.remaining)]["scores"].append(ev)
    # 剩余秒数越大比赛时刻越早。
    keys = sorted(buckets.keys(), key=lambda k: (k[0], -k[1]))
    return [(k, buckets[k]["subs"], buckets[k]["scores"]) for k in keys]


def _local_edges(
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: list[OrderEdge],
) -> list[tuple[str, int, str, int]]:
    """把全局行号的核实约束翻译为组内局部下标，统一成两种形式：

    * ('sub', i, 'score', j)：换人 i 必须先于得分 j；
    * ('score', j, 'sub', i)：得分 j 必须先于换人 i。

    传入的 edges 已按 (节次, 剩余秒) 过滤到本组；这里再依据行号做一次防御性
    过滤，避免其他时刻的约束被错误套用到本组。
    """
    sub_index = {ev.row_no: i for i, ev in enumerate(subs)}
    score_index = {ev.row_no: j for j, ev in enumerate(scores)}
    local: list[tuple[str, int, str, int]] = []
    for e in edges:
        if e.first_table == "sub":
            if e.first_row not in sub_index or e.second_row not in score_index:
                continue
            local.append(
                ("sub", sub_index[e.first_row], "score", score_index[e.second_row])
            )
        else:
            if e.first_row not in score_index or e.second_row not in sub_index:
                continue
            local.append(
                ("score", score_index[e.first_row], "sub", sub_index[e.second_row])
            )
    return local


def _contradicting_edges(local_edges) -> Optional[tuple]:
    """同一对换人与得分若被双向约束，直接冲突。"""
    directed = set(local_edges)
    for edge in local_edges:
        ta, ia, tb, ib = edge
        if (tb, ib, ta, ia) in directed:
            return edge
    return None


def _blocked_frontier_reason(
    reachable: set[tuple[int, int]],
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    local_edges,
    courts: list[frozenset[str]],
) -> str:
    """在可达前沿找一个推进不了的事件并说明原因。"""
    n_sub = len(subs)
    for i, j in sorted(reachable):
        if i < n_sub:
            sub = subs[i]
            if sub.outgoing not in courts[i]:
                return (
                    f"下场球员 {sub.outgoing}（换人 CSV 第 {sub.row_no} 行）"
                    f"在该时刻已不在场上"
                )
            if sub.incoming in courts[i]:
                return (
                    f"上场球员 {sub.incoming}（换人 CSV 第 {sub.row_no} 行）"
                    f"在该时刻已在场上"
                )
            for ta, ia, _tb, jb in local_edges:
                # 要求得分 ia 先于换人 jb=i，而当前只完成了 j 笔得分。
                if ta == "score" and jb == i and ia >= j:
                    return (
                        f"核实要求得分 CSV 第 {scores[ia].row_no} 行先于 "
                        f"换人 CSV 第 {sub.row_no} 行，但该得分此前尚未发生"
                    )
        if j < len(scores):
            sc = scores[j]
            for ta, ia, _tb, jb in local_edges:
                # 要求换人 ia 先于得分 jb=j，而当前只完成了 i 笔换人。
                if ta == "sub" and jb == j and ia >= i:
                    return (
                        f"核实要求换人 CSV 第 {subs[ia].row_no} 行先于 "
                        f"得分 CSV 第 {sc.row_no} 行，但该换人此前尚未发生"
                    )
            if sc.team == "own" and sc.scorer not in courts[i]:
                return (
                    f"本方得分者 {sc.scorer}（得分 CSV 第 {sc.row_no} 行）"
                    f"在该时刻任何已构造的合法顺序中都无法处于场上"
                )
    return "换人与得分的先后约束相互矛盾，找不到合法交错顺序"


def _apply_score(vec: dict[str, int], sc: ScoreEvent, court: frozenset[str]):
    out = dict(vec)
    delta = sc.points if sc.team == "own" else -sc.points
    for p in court:
        out[p] = out.get(p, 0) + delta
    return out


def _solve_group(
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: list[OrderEdge],
    court: frozenset[str],
    players: list[str],
):
    """求解单个时刻分组。

    返回 (组信息, {球员: (最小增量, 最大增量)})；无解时抛 InfeasibleError。
    换人本身的合法性与交错顺序无关，先做静态检查。
    """
    period, remaining = (
        (subs[0].period, subs[0].remaining)
        if subs
        else (scores[0].period, scores[0].remaining)
    )
    n_sub, n_score = len(subs), len(scores)
    local_edges = _local_edges(subs, scores, edges)

    contradiction = _contradicting_edges(local_edges)
    if contradiction is not None:
        ta, ia, tb, ib = contradiction
        row_a = subs[ia].row_no if ta == "sub" else scores[ia].row_no
        row_b = subs[ib].row_no if tb == "sub" else scores[ib].row_no
        raise InfeasibleError(
            f"第 {period} 节剩余 {remaining} 秒：换人/得分被同时登记了互斥的"
            f"先后顺序（换人/得分 CSV 第 {row_a} 行与第 {row_b} 行）",
            period,
            remaining,
        )

    # 静态检查换人：换人之间顺序固定，逐笔执行时上下场状态必须合法。
    cur = set(court)
    for ev in subs:
        if ev.outgoing not in cur:
            raise InfeasibleError(
                f"第 {period} 节剩余 {remaining} 秒：下场球员 {ev.outgoing}"
                f"（换人 CSV 第 {ev.row_no} 行）此时不在场上",
                period,
                remaining,
            )
        if ev.incoming in cur:
            raise InfeasibleError(
                f"第 {period} 节剩余 {remaining} 秒：上场球员 {ev.incoming}"
                f"（换人 CSV 第 {ev.row_no} 行）此时已在场上",
                period,
                remaining,
            )
        cur.discard(ev.outgoing)
        cur.add(ev.incoming)

    # courts[i]：执行完前 i 笔换人后的场上阵容（阵容只由 i 决定）。
    courts: list[frozenset[str]] = [frozenset(court)]
    cur = set(court)
    for ev in subs:
        cur.discard(ev.outgoing)
        cur.add(ev.incoming)
        courts.append(frozenset(cur))

    zero = {p: 0 for p in players}
    # states[(i,j)] = (min 向量, max 向量)；键存在即状态可达。
    states: dict[tuple[int, int], tuple[dict, dict]] = {
        (0, 0): (dict(zero), dict(zero))
    }
    ways: dict[tuple[int, int], int] = defaultdict(int)
    ways[(0, 0)] = 1

    # 从已完成 total 个事件的状态推进到 total+1 个；最后一层（终点前）也要跑。
    # i 的可行范围：0 <= i <= n_sub、0 <= j=total-i <= n_score。
    for total in range(n_sub + n_score):
        for i in range(max(0, total - n_score), min(n_sub, total) + 1):
            j = total - i
            if (i, j) not in states:
                continue
            min_v, max_v = states[(i, j)]
            count = ways[(i, j)]

            # 推进一笔换人 i。
            if i < n_sub:
                # ('score', ia, 'sub', i) 要求得分 ia 先于换人 i。
                # 当前已完成 j 笔得分（下标 0..j-1），待执行的是第 j 笔。
                # ia < j 才表示要求的得分已完成；ia == j 时若先换人，
                # 待执行得分就被排到换人后面，违反约束，故 ia >= j 即阻塞。
                blocked = any(
                    ta == "score" and jb == i and ia >= j
                    for ta, ia, _tb, jb in local_edges
                )
                if not blocked:
                    key = (i + 1, j)
                    ways[key] = min(WAY_CAP, ways[key] + count)
                    if key in states:
                        old_min, old_max = states[key]
                        states[key] = (
                            {p: min(old_min[p], min_v[p]) for p in players},
                            {p: max(old_max[p], max_v[p]) for p in players},
                        )
                    else:
                        states[key] = (dict(min_v), dict(max_v))

            # 推进一笔得分 j。
            if j < n_score:
                sc = scores[j]
                # ('sub', ia, 'score', j) 要求换人 ia 先于得分 j，
                # 已完成 i 笔换人；ia >= i 说明要求的换人尚未完成，阻塞。
                edge_blocked = any(
                    ta == "sub" and jb == j and ia >= i
                    for ta, ia, _tb, jb in local_edges
                )
                scorer_blocked = (
                    sc.team == "own" and sc.scorer not in courts[i]
                )
                if not edge_blocked and not scorer_blocked:
                    new_min = _apply_score(min_v, sc, courts[i])
                    new_max = _apply_score(max_v, sc, courts[i])
                    key = (i, j + 1)
                    ways[key] = min(WAY_CAP, ways[key] + count)
                    if key in states:
                        old_min, old_max = states[key]
                        states[key] = (
                            {p: min(old_min[p], new_min[p]) for p in players},
                            {p: max(old_max[p], new_max[p]) for p in players},
                        )
                    else:
                        states[key] = (new_min, new_max)

    final_key = (n_sub, n_score)
    if final_key not in states:
        reason = _blocked_frontier_reason(
            set(states.keys()), subs, scores, local_edges, courts
        )
        raise InfeasibleError(
            f"第 {period} 节剩余 {remaining} 秒：{reason}", period, remaining
        )

    min_v, max_v = states[final_key]
    legal_orders = ways[final_key]
    info = GroupInfo(
        period=period,
        remaining=remaining,
        sub_rows=[ev.row_no for ev in subs],
        score_rows=[ev.row_no for ev in scores],
        legal_orders=legal_orders,
        ambiguous=(n_sub > 0 and n_score > 0 and legal_orders > 1),
    )
    return info, {p: (min_v[p], max_v[p]) for p in players}


def compute(
    starters: list[str],
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    edges: Optional[list[OrderEdge]] = None,
):
    """计算全场正负值区间。

    返回 {"feasible", "ranges", "groups", "total_legal_orders", "players"}；
    无解时抛 InfeasibleError。
    """
    edges = edges or []
    starters = list(starters)
    if len(starters) != 5 or len(set(starters)) != 5:
        raise ValueError("首发必须是 5 名不同球员")

    players = sorted(
        set(starters)
        | {ev.outgoing for ev in subs}
        | {ev.incoming for ev in subs}
        | {ev.scorer for ev in scores if ev.scorer}
    )

    groups = _group_events(subs, scores)
    edges_by_group: dict[tuple[int, int], list[OrderEdge]] = defaultdict(list)
    for e in edges:
        edges_by_group[(e.period, e.remaining)].append(e)

    court = frozenset(starters)
    total_min = {p: 0 for p in players}
    total_max = {p: 0 for p in players}
    group_infos: list[GroupInfo] = []
    total_orders = 1

    for (period, remaining), g_subs, g_scores in groups:
        info, deltas = _solve_group(
            g_subs,
            g_scores,
            edges_by_group.get((period, remaining), []),
            court,
            players,
        )
        group_infos.append(info)
        for p in players:
            lo, hi = deltas[p]
            total_min[p] += lo
            total_max[p] += hi
        if info.legal_orders >= WAY_CAP or total_orders >= WAY_CAP:
            total_orders = WAY_CAP
        else:
            total_orders *= info.legal_orders
        # 分组结束后阵容固定（组内换人全部完成，与交错方式无关）。
        for ev in g_subs:
            court = frozenset((set(court) - {ev.outgoing}) | {ev.incoming})

    return {
        "feasible": True,
        "ranges": {
            p: {"min": total_min[p], "max": total_max[p]} for p in players
        },
        "groups": group_infos,
        "total_legal_orders": total_orders,
        "players": players,
    }
