"""引擎单元测试：区间、核实约束、无解原因。"""

import pytest

from app.engine import (
    InfeasibleError,
    OrderEdge,
    ScoreEvent,
    SubEvent,
    compute,
)

STARTERS = ["A1", "A2", "A3", "A4", "A5"]


def sub(period, remaining, outgoing, incoming, row_no):
    return SubEvent(period, remaining, outgoing, incoming, row_no)


def score(period, remaining, team, points, scorer, row_no):
    return ScoreEvent(period, remaining, team, points, scorer, row_no)


def sample_events():
    subs = [
        sub(1, 600, "A5", "B1", 1),
        sub(1, 300, "A4", "B2", 2),
    ]
    scores = [
        score(1, 600, "opp", 2, None, 1),
        score(1, 600, "own", 3, "A1", 2),
        score(1, 300, "own", 2, "A4", 3),
        score(1, 120, "own", 1, "A3", 4),
    ]
    return subs, scores


def test_baseline_ranges_without_verification():
    subs, scores = sample_events()
    result = compute(STARTERS, subs, scores, [])
    ranges = result["ranges"]
    # (1,600) 组：1 换人 + 2 得分共 3 种合法交错（S 为换人，g 为得分）：
    # S g1 g2 / g1 S g2 / g1 g2 S；其余时刻顺序固定。
    assert ranges["A5"] == {"min": -2, "max": 1}
    assert ranges["B1"] == {"min": 3, "max": 6}
    # A1-A4 不参与换人，全部得分都计入：-2+3+2+1 = 4，
    # 其中 (1,600) 组净 +1，(1,300) 组 +2，(1,120) 组 +1。
    for p in ("A1", "A2", "A3"):
        assert ranges[p] == {"min": 4, "max": 4}
    # A4 在 (1,300) 组先得 2 分后被换下：(1,600) 组净 +1、本组 +2、
    # (1,120) 组已下场拿不到，共 3。
    assert ranges["A4"] == {"min": 3, "max": 3}
    # (1,300) 组：A4 投进 2 后被换下（唯一合法顺序）
    assert ranges["B2"] == {"min": 1, "max": 1}
    assert result["total_legal_orders"] == 3
    assert {g.ambiguous for g in result["groups"]} == {True, False}


def test_verification_sub_first_narrows_group():
    subs, scores = sample_events()
    # 换人 CSV 第 1 行 先于 得分 CSV 第 1 行（对方 2 分），
    # 则换人必须在两笔得分之前，组内只剩 1 种顺序。
    edges = [OrderEdge(1, 600, "sub", 1, "score", 1)]
    result = compute(STARTERS, subs, scores, edges)
    ranges = result["ranges"]
    assert ranges["A5"] == {"min": 0, "max": 0}
    assert ranges["B1"] == {"min": 4, "max": 4}
    assert result["total_legal_orders"] == 1


def test_verification_score_first_narrows_group():
    subs, scores = sample_events()
    # 得分 CSV 第 2 行（A1 三分）先于换人第 1 行：
    # 唯一顺序 g1 g2 S，A5 承担两笔得分（-2+3=1），B1 得 0。
    edges = [OrderEdge(1, 600, "score", 2, "sub", 1)]
    result = compute(STARTERS, subs, scores, edges)
    ranges = result["ranges"]
    assert ranges["A5"] == {"min": 1, "max": 1}
    assert ranges["B1"] == {"min": 3, "max": 3}
    assert result["total_legal_orders"] == 1


def test_two_consistent_edges_fix_full_order():
    subs, scores = sample_events()
    edges = [
        OrderEdge(1, 600, "sub", 1, "score", 1),
        OrderEdge(1, 600, "sub", 1, "score", 2),
    ]
    result = compute(STARTERS, subs, scores, edges)
    assert result["total_legal_orders"] == 1
    assert result["ranges"]["A5"] == {"min": 0, "max": 0}


def test_contradicting_edges_raise():
    subs, scores = sample_events()
    edges = [
        OrderEdge(1, 600, "sub", 1, "score", 1),
        OrderEdge(1, 600, "score", 1, "sub", 1),
    ]
    with pytest.raises(InfeasibleError) as exc:
        compute(STARTERS, subs, scores, edges)
    assert "互斥" in exc.value.reason


def test_illegal_substitution_outgoing_not_on_court():
    subs = [sub(1, 600, "X9", "B1", 1)]  # X9 从不在场上
    scores = [score(1, 600, "own", 2, "A1", 1)]
    with pytest.raises(InfeasibleError) as exc:
        compute(STARTERS, subs, scores, [])
    assert "不在场上" in exc.value.reason


def test_illegal_substitution_incoming_already_on_court():
    subs = [sub(1, 600, "A5", "A1", 1)]  # A1 已在场上
    scores = [score(1, 600, "own", 2, "A1", 1)]
    with pytest.raises(InfeasibleError) as exc:
        compute(STARTERS, subs, scores, [])
    assert "已在场上" in exc.value.reason


def test_scorer_never_on_court_is_infeasible():
    subs = [sub(1, 600, "A5", "B1", 1)]
    scores = [score(1, 600, "own", 2, "B2", 1)]  # B2 任何顺序都不在场
    with pytest.raises(InfeasibleError) as exc:
        compute(STARTERS, subs, scores, [])
    assert "无法处于场上" in exc.value.reason


def test_scorer_only_on_court_after_sub_has_unique_order():
    subs = [sub(1, 600, "A5", "B1", 1)]
    scores = [score(1, 600, "own", 2, "B1", 1)]
    result = compute(STARTERS, subs, scores, [])
    # 只有“先换人后得分”合法
    assert result["total_legal_orders"] == 1
    assert result["ranges"]["B1"] == {"min": 2, "max": 2}
    assert result["ranges"]["A5"] == {"min": 0, "max": 0}


def test_opponent_points_count_negative():
    subs = []
    scores = [score(1, 600, "opp", 3, None, 1)]
    result = compute(STARTERS, subs, scores, [])
    for p in STARTERS:
        assert result["ranges"][p] == {"min": -3, "max": -3}


def test_multiple_subs_and_scores_same_second():
    # 同秒两笔换人、两笔得分（得分者始终在场），验证行序保持与区间枚举：
    # 合法交错共 6 种（C(4,2)），换人/得分各自保持行序。
    subs = [
        sub(1, 600, "A5", "B1", 1),
        sub(1, 600, "A4", "B2", 2),
    ]
    scores = [
        score(1, 600, "own", 2, "A1", 1),
        score(1, 600, "own", 3, "A1", 2),
    ]
    result = compute(STARTERS, subs, scores, [])
    assert result["total_legal_orders"] == 6
    assert result["groups"][0].ambiguous is True
    # A5/B1 的分界是第 1 次换人：A5 最少 0、最多拿全部 5 分；B1 对称。
    assert result["ranges"]["A5"] == {"min": 0, "max": 5}
    assert result["ranges"]["B1"] == {"min": 0, "max": 5}
    # A4/B2 的分界是第 2 次换人，A4 至少经历第 1 次换人前的全部得分位置。
    assert result["ranges"]["A4"]["min"] == 0
    assert result["ranges"]["A4"]["max"] == 5
    assert result["ranges"]["B2"]["min"] == 0
    assert result["ranges"]["B2"]["max"] == 5
    # 始终在场的 A1 一定拿到全部 5 分
    assert result["ranges"]["A1"] == {"min": 5, "max": 5}


def test_two_subs_one_score_unique_order_when_scorer_enters_late():
    # B2 要等第 2 次换人后才在场，他的得分强制两笔换人先发生，顺序唯一。
    subs = [
        sub(1, 600, "A5", "B1", 1),
        sub(1, 600, "A4", "B2", 2),
    ]
    scores = [
        score(1, 600, "own", 2, "A5", 1),
        score(1, 600, "own", 3, "B2", 2),
    ]
    result = compute(STARTERS, subs, scores, [])
    assert result["total_legal_orders"] == 1
    assert result["groups"][0].ambiguous is False
    # 得分者约束决定唯一交错：g1(A5 的 2 分) 必须在 S1 之前（否则 A5 已下场），
    # g2(B2 的 3 分) 必须在 S2 之后，即唯一顺序 g1 S1 S2 g2。
    # 因此 A5 拿到自己的 2 分；B1 只经历 g2 得 3；B2 上场后经历 g2 得 3。
    assert result["ranges"]["A5"] == {"min": 2, "max": 2}
    assert result["ranges"]["B1"] == {"min": 3, "max": 3}
    assert result["ranges"]["B2"] == {"min": 3, "max": 3}


def test_starters_must_be_five_distinct():
    with pytest.raises(ValueError):
        compute(["A1", "A2", "A3", "A4", "A4"], [], [], [])
