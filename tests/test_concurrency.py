"""并发与一致性回归测试（DB 层，用可控时序复现多 worker 交错）。

覆盖：
* 详情读取中途另一事务提交 -> 核实清单与结果必须来自同一次提交；
* 新核实重算期间 -> 读到最近已提交的一版；
* 重传已生效核实与另一条核实提交交错 -> 不许把收窄的结果改回去；
* 并发登记同一条核实 -> 只生效一次，另一次按重传返回；
* 两个真实进程共用同一 SQLite 文件时的上述交错；
* 旧库 results 被重传覆写过 -> init_db 升级修复到最后一条有效核实的结果。
"""

import multiprocessing
import threading
import time

import pytest

from app import database as db
from app.engine import OrderEdge, ScoreEvent, SubEvent, compute

STARTERS = ["A1", "A2", "A3", "A4", "A5"]


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()

# 三个同秒组，每组 1 换人 + 1 得分：初始合法顺序 2*2*2=8，
# 每生效一条核实对应组收敛为 1：8 -> 4 -> 2 -> 1。
EDGE1 = OrderEdge(1, 600, "sub", 1, "score", 1)
EDGE2 = OrderEdge(1, 500, "sub", 2, "score", 2)


def _new_game() -> int:
    subs = [
        SubEvent(1, 600, "A5", "B1", 1),
        SubEvent(1, 500, "A4", "B2", 2),
        SubEvent(1, 400, "A3", "B3", 3),
    ]
    scores = [
        ScoreEvent(1, 600, "own", 2, "A1", 1),
        ScoreEvent(1, 500, "own", 2, "A2", 2),
        ScoreEvent(1, 400, "own", 2, "A1", 3),
    ]
    result = compute(STARTERS, subs, scores, [])
    assert result["total_legal_orders"] == 8
    return db.create_game(
        name="交错测试",
        starters=STARTERS,
        subs=subs,
        scores=scores,
        sub_csv="",
        score_csv="",
        result=result,
    )


def _slow_compute(started: threading.Event, delay: float = 0.4):
    """包一层可控制时序的重算：进入即发信号，sleep 期间写事务尚未提交。"""

    def slow(starters, subs, scores, edges):
        started.set()
        time.sleep(delay)
        return compute(starters, subs, scores, edges)

    return slow


def test_detail_reads_single_committed_snapshot(monkeypatch):
    """读取详情的过程中另一连接提交新核实：清单与结果不许混搭。"""
    gid = _new_game()
    orig_load_edges = db.load_edges
    fired = {"done": False}

    def injecting_load_edges(conn, game_id):
        edges = orig_load_edges(conn, game_id)
        if not fired["done"]:
            fired["done"] = True
            # 在详情读取的两条 SELECT 之间，另一连接完成一次有效核实提交。
            db.add_verification(game_id, EDGE1, "", compute)
        return edges

    monkeypatch.setattr(db, "load_edges", injecting_load_edges)
    detail = db.get_game_detail(gid)
    # 同一快照：要么全是提交前（0 条核实 + 8 种顺序），要么全是提交后。
    assert detail["edges"] == []
    assert detail["result_row"]["total_legal_orders"] == 8


def test_reads_see_last_committed_while_recompute_in_flight():
    """新核实还在重算（写事务未提交）时，读到最近已提交的一版。"""
    gid = _new_game()
    db.add_verification(gid, EDGE1, "", compute)  # 已提交：1 条核实，4 种顺序
    started = threading.Event()
    release = threading.Event()

    def gated_compute(starters, subs, scores, edges):
        started.set()
        assert release.wait(5)
        return compute(starters, subs, scores, edges)

    errors = []

    def submit_second():
        try:
            db.add_verification(gid, EDGE2, "", gated_compute)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=submit_second)
    t.start()
    assert started.wait(5)
    during = db.get_game_detail(gid)
    assert len(during["edges"]) == 1
    assert during["result_row"]["total_legal_orders"] == 4
    release.set()
    t.join(5)
    assert errors == []
    after = db.get_game_detail(gid)
    assert len(after["edges"]) == 2
    assert after["result_row"]["total_legal_orders"] == 2


def test_retransmit_does_not_revert_concurrent_narrowing():
    """重传已生效核实赶上另一条核实提交：只回读，不许覆写收窄后的结果。"""
    gid = _new_game()
    db.add_verification(gid, EDGE1, "", compute)  # 已生效：4 种顺序
    started = threading.Event()
    new_outcome, dup_outcome = {}, {}

    def submit_new():
        new_outcome.update(
            db.add_verification(gid, EDGE2, "", _slow_compute(started))
        )

    def retransmit_old():
        dup_outcome.update(db.add_verification(gid, EDGE1, "", compute))

    t_new = threading.Thread(target=submit_new)
    t_new.start()
    assert started.wait(5)  # 新核实已插入、重算未提交
    t_dup = threading.Thread(target=retransmit_old)
    t_dup.start()
    t_new.join(5)
    t_dup.join(5)

    assert new_outcome["duplicate"] is False
    assert new_outcome["result"]["total_legal_orders"] == 2
    # 重传只回读已提交结果：排到新核实提交之后，读到收窄后的版本。
    assert dup_outcome["duplicate"] is True
    assert dup_outcome["result"]["total_legal_orders"] == 2
    # 库里最终结果必须是收窄后的，不能被重传改回 4。
    final = db.get_game_detail(gid)
    assert len(final["edges"]) == 2
    assert final["result_row"]["total_legal_orders"] == 2


def test_concurrent_same_verification_takes_effect_once():
    """并发登记同一条合法核实：一次生效，另一次按重传返回，无 500。"""
    gid = _new_game()
    barrier = threading.Barrier(2)
    outcomes, errors = [], []

    def worker(recompute_fn):
        try:
            barrier.wait(5)
            outcomes.append(db.add_verification(gid, EDGE1, "", recompute_fn))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    started = threading.Event()
    t1 = threading.Thread(target=worker, args=(_slow_compute(started, 0.2),))
    t2 = threading.Thread(target=worker, args=(compute,))
    t1.start()
    t2.start()
    t1.join(5)
    t2.join(5)

    assert errors == []
    assert sorted(o["duplicate"] for o in outcomes) == [False, True]
    detail = db.get_game_detail(gid)
    assert len(detail["edges"]) == 1
    assert detail["result_row"]["total_legal_orders"] == 4


def test_conflicting_verification_keeps_committed_data():
    """矛盾核实拒绝并回滚：已提交的核实与结果保持不变。"""
    gid = _new_game()
    db.add_verification(gid, EDGE1, "", compute)
    before = db.get_game_detail(gid)

    from app.engine import InfeasibleError

    reverse = OrderEdge(1, 600, "score", 1, "sub", 1)
    try:
        db.add_verification(gid, reverse, "", compute)
        raised = None
    except InfeasibleError as exc:
        raised = exc
    assert raised is not None

    after = db.get_game_detail(gid)
    assert len(after["edges"]) == 1
    assert after["edges"] == before["edges"]
    assert after["result_row"]["total_legal_orders"] == 4


def _submit_in_process(gid, edge, delay, slow_seconds, queue):
    """子进程入口：独立连接同一 SQLite 文件，模拟另一个 worker。"""
    try:
        time.sleep(delay)

        def wrapped(starters, subs, scores, edges):
            if slow_seconds:
                time.sleep(slow_seconds)  # 拉长写事务未提交的窗口
            return compute(starters, subs, scores, edges)

        out = db.add_verification(gid, edge, "", wrapped)
        queue.put(("ok", out["duplicate"], out["result"]["total_legal_orders"]))
    except Exception as exc:  # pragma: no cover
        queue.put(("err", repr(exc)))


def test_two_process_retransmit_race_keeps_narrowed_result():
    """两个真实进程：重传已生效核实赶上另一进程提交新核实。

    进程 A 登记新核实（重算期间写事务未提交），进程 B 此时重传已生效核实；
    B 只能回读已提交结果，最终库里的收窄结果不许被改回去。
    """
    gid = _new_game()
    db.add_verification(gid, EDGE1, "", compute)  # 已生效：4 种顺序

    queue = multiprocessing.Queue()
    proc_a = multiprocessing.Process(
        target=_submit_in_process, args=(gid, EDGE2, 0.1, 1.0, queue)
    )
    proc_b = multiprocessing.Process(
        target=_submit_in_process, args=(gid, EDGE1, 0.5, 0, queue)
    )
    proc_a.start()
    proc_b.start()
    proc_a.join(15)
    proc_b.join(15)
    assert proc_a.exitcode == 0 and proc_b.exitcode == 0

    outcomes = [queue.get(timeout=5), queue.get(timeout=5)]
    assert all(o[0] == "ok" for o in outcomes)
    dup = next(o for o in outcomes if o[1] is True)
    new = next(o for o in outcomes if o[1] is False)
    assert new[2] == 2  # 新核实把合法顺序收窄到 2
    assert dup[2] == 2  # 重传回读到的是收窄后的已提交结果

    final = db.get_game_detail(gid)
    assert len(final["edges"]) == 2
    assert final["result_row"]["total_legal_orders"] == 2


def test_two_process_same_verification_effective_once():
    """两个真实进程同时登记同一条新核实：只生效一次，无唯一约束报错。"""
    gid = _new_game()
    queue = multiprocessing.Queue()
    procs = [
        multiprocessing.Process(
            target=_submit_in_process, args=(gid, EDGE1, 0.1, slow, queue)
        )
        for slow in (0.6, 0)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(15)
    assert all(p.exitcode == 0 for p in procs)

    outcomes = [queue.get(timeout=5), queue.get(timeout=5)]
    assert all(o[0] == "ok" for o in outcomes), outcomes
    assert sorted(o[1] for o in outcomes) == [False, True]
    detail = db.get_game_detail(gid)
    assert len(detail["edges"]) == 1
    assert detail["result_row"]["total_legal_orders"] == 4


def test_upgrade_repairs_results_overwritten_by_stale_retransmit():
    """旧库 results 被重传覆写回旧值：init_db 修复到最后有效核实的结果。"""
    gid = _new_game()
    db.add_verification(gid, EDGE1, "", compute)  # 有效：8 -> 4，留有快照
    assert db.get_game_detail(gid)["result_row"]["total_legal_orders"] == 4

    # 模拟旧版本 bug：重传把 results 覆写回首版宽区间（快照表不动）。
    with db.connect() as conn:
        first = conn.execute(
            "SELECT feasible, ranges_json, groups_json, total_legal_orders "
            "FROM result_snapshots WHERE game_id=? ORDER BY id LIMIT 1",
            (gid,),
        ).fetchone()
        conn.execute(
            "UPDATE results SET feasible=?, ranges_json=?, groups_json=?, "
            "total_legal_orders=? WHERE game_id=?",
            (
                first["feasible"],
                first["ranges_json"],
                first["groups_json"],
                first["total_legal_orders"],
                gid,
            ),
        )
    assert db.get_game_detail(gid)["result_row"]["total_legal_orders"] == 8

    db.init_db()  # 升级启动 -> 按最新快照修复
    db.init_db()  # 幂等，再跑一次不应改变结果

    detail = db.get_game_detail(gid)
    assert len(detail["edges"]) == 1
    assert detail["result_row"]["total_legal_orders"] == 4
    row = next(r for r in db.list_games() if r["id"] == gid)
    assert row["total_legal_orders"] == 4
    assert row["verification_count"] == 1
