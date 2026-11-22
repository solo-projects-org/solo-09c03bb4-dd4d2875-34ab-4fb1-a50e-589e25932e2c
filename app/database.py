"""SQLite 持久化层。

并发模型（多 worker 进程共用同一个 SQLite 文件）：

* 每次有效核实都在一个 ``BEGIN IMMEDIATE`` 事务里完成「查重 → 插入 → 重读全部
  已生效核实 → 重算 → 写结果 + 快照」，跨进程串行；WAL 下读者不被写事务阻塞，
  重算进行期间读到的始终是最近已提交的一版。
* 重传（已生效核实的重复提交）命中唯一约束，只回读已提交结果，不重算、不写库，
  因此不会把别的进程刚收窄的结果改回旧值。
* 详情 / 列表的读取包在单个只读事务里，核实清单、正负值区间与合法顺序数始终
  来自同一次提交。
* 旧库可能被历史重传 bug 覆写过 ``results``：``init_db`` 启动时以每场最新
  快照（最后一条有效核实生效时的结果）为准修复。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Optional

from .engine import OrderEdge, ScoreEvent, SubEvent

DB_PATH = os.environ.get("PM_DB_PATH", "/data/plusminus.db")
_write_lock = threading.RLock()


def get_conn() -> sqlite3.Connection:
    path = DB_PATH
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def connect():
    """事务 + 连接一体的上下文：正常退出提交，异常回滚，最终关闭连接。"""
    conn = get_conn()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def read_connect():
    """只读事务：整批 SELECT 落在同一个已提交快照上。

    WAL 下读事务不阻塞写、也不被写阻塞；事务内的多次读取不会因为别的进程
    恰好提交而拼出「旧核实清单 + 新结果」这类混搭。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                starters TEXT NOT NULL,
                sub_csv TEXT NOT NULL,
                score_csv TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                table_name TEXT NOT NULL CHECK (table_name IN ('sub','score')),
                row_no INTEGER NOT NULL,
                period INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE (game_id, table_name, row_no)
            );
            CREATE TABLE IF NOT EXISTS verifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                period INTEGER NOT NULL,
                remaining INTEGER NOT NULL,
                first_table TEXT NOT NULL CHECK (first_table IN ('sub','score')),
                first_row INTEGER NOT NULL,
                second_table TEXT NOT NULL CHECK (second_table IN ('sub','score')),
                second_row INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE (game_id, period, remaining,
                        first_table, first_row, second_table, second_row)
            );
            CREATE TABLE IF NOT EXISTS results (
                game_id INTEGER PRIMARY KEY
                    REFERENCES games(id) ON DELETE CASCADE,
                feasible INTEGER NOT NULL,
                ranges_json TEXT NOT NULL,
                groups_json TEXT NOT NULL,
                total_legal_orders INTEGER NOT NULL,
                infeasible_reason TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS result_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
                feasible INTEGER NOT NULL,
                ranges_json TEXT NOT NULL,
                groups_json TEXT NOT NULL,
                total_legal_orders INTEGER NOT NULL,
                verification_count INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )
        _repair_results_from_snapshots(conn)


def _repair_results_from_snapshots(conn) -> int:
    """用每场最新快照修复被旧版本重传 bug 覆写的 results 行。

    快照只在「有效核实提交」时写入（含建赛首版），因此每场 id 最大的快照就是
    最后一条有效核实生效时的结果；results 行若与它不一致，说明曾被重传覆写，
    按快照恢复。幂等，可随每个 worker 启动重复执行。
    """
    rows = conn.execute(
        """
        SELECT s.game_id AS game_id, s.feasible AS feasible,
               s.ranges_json AS ranges_json, s.groups_json AS groups_json,
               s.total_legal_orders AS total_legal_orders,
               s.created_at AS created_at,
               r.feasible AS cur_feasible, r.ranges_json AS cur_ranges,
               r.groups_json AS cur_groups,
               r.total_legal_orders AS cur_total
        FROM result_snapshots s
        JOIN results r ON r.game_id = s.game_id
        WHERE s.id = (SELECT MAX(id) FROM result_snapshots
                      WHERE game_id = s.game_id)
        """
    ).fetchall()
    repaired = 0
    for row in rows:
        current = (
            row["cur_feasible"],
            row["cur_ranges"],
            row["cur_groups"],
            row["cur_total"],
        )
        snapshot = (
            row["feasible"],
            row["ranges_json"],
            row["groups_json"],
            row["total_legal_orders"],
        )
        if current != snapshot:
            _execute_with_retry(
                conn,
                "UPDATE results SET feasible=?, ranges_json=?, groups_json=?, "
                "total_legal_orders=?, infeasible_reason=NULL, updated_at=? "
                "WHERE game_id=?",
                (
                    row["feasible"],
                    row["ranges_json"],
                    row["groups_json"],
                    row["total_legal_orders"],
                    row["created_at"],
                    row["game_id"],
                ),
            )
            repaired += 1
    return repaired


# ---------- 事件序列化 ----------


def _sub_payload(ev: SubEvent) -> str:
    return json.dumps(
        {
            "outgoing": ev.outgoing,
            "incoming": ev.incoming,
        },
        ensure_ascii=False,
    )


def _score_payload(ev: ScoreEvent) -> str:
    return json.dumps(
        {"team": ev.team, "points": ev.points, "scorer": ev.scorer},
        ensure_ascii=False,
    )


def load_subs(conn: sqlite3.Connection, game_id: int) -> list[SubEvent]:
    rows = conn.execute(
        "SELECT row_no, period, remaining, payload FROM events "
        "WHERE game_id=? AND table_name='sub' ORDER BY row_no",
        (game_id,),
    ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        out.append(
            SubEvent(
                period=r["period"],
                remaining=r["remaining"],
                outgoing=payload["outgoing"],
                incoming=payload["incoming"],
                row_no=r["row_no"],
            )
        )
    return out


def load_scores(conn: sqlite3.Connection, game_id: int) -> list[ScoreEvent]:
    rows = conn.execute(
        "SELECT row_no, period, remaining, payload FROM events "
        "WHERE game_id=? AND table_name='score' ORDER BY row_no",
        (game_id,),
    ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"])
        out.append(
            ScoreEvent(
                period=r["period"],
                remaining=r["remaining"],
                team=payload["team"],
                points=payload["points"],
                scorer=payload["scorer"],
                row_no=r["row_no"],
            )
        )
    return out


def load_edges(conn: sqlite3.Connection, game_id: int) -> list[OrderEdge]:
    rows = conn.execute(
        "SELECT period, remaining, first_table, first_row, second_table, second_row "
        "FROM verifications WHERE game_id=? ORDER BY id",
        (game_id,),
    ).fetchall()
    return [
        OrderEdge(
            period=r["period"],
            remaining=r["remaining"],
            first_table=r["first_table"],
            first_row=r["first_row"],
            second_table=r["second_table"],
            second_row=r["second_row"],
        )
        for r in rows
    ]


# ---------- 写入（全部在写锁 + 事务内完成） ----------


def _execute_with_retry(conn, sql, params=()):
    for attempt in range(5):
        try:
            return conn.execute(sql, params)
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) and attempt < 4:
                time.sleep(0.1 * (attempt + 1))
                continue
            raise


def create_game(
    name: str,
    starters: list[str],
    subs: list[SubEvent],
    scores: list[ScoreEvent],
    sub_csv: str,
    score_csv: str,
    result: dict,
) -> int:
    """创建比赛并写入首版结果，返回 game_id。"""
    with _write_lock, connect() as conn:
        cur = _execute_with_retry(
            conn,
            "INSERT INTO games (name, starters, sub_csv, score_csv) "
            "VALUES (?,?,?,?)",
            (name, json.dumps(starters, ensure_ascii=False), sub_csv, score_csv),
        )
        game_id = cur.lastrowid
        for ev in subs:
            _execute_with_retry(
                conn,
                "INSERT INTO events (game_id, table_name, row_no, period, "
                "remaining, payload) VALUES (?,?,?,?,?,?)",
                (
                    game_id,
                    "sub",
                    ev.row_no,
                    ev.period,
                    ev.remaining,
                    _sub_payload(ev),
                ),
            )
        for ev in scores:
            _execute_with_retry(
                conn,
                "INSERT INTO events (game_id, table_name, row_no, period, "
                "remaining, payload) VALUES (?,?,?,?,?,?)",
                (
                    game_id,
                    "score",
                    ev.row_no,
                    ev.period,
                    ev.remaining,
                    _score_payload(ev),
                ),
            )
        _persist_result(conn, game_id, result)
        return game_id


def add_verification(
    game_id: int,
    edge: OrderEdge,
    note: str,
    recompute_fn,
) -> dict:
    """登记一条核实并重算。

    整个「查重 → 插入 → 重读全部已生效核实 → 重算 → 写结果 + 快照」放在一个
    ``BEGIN IMMEDIATE`` 事务里，跨进程串行：

    * 命中唯一约束（同一核实已生效）视为重传：不重算、不写库，只回读已提交
      结果返回 ``duplicate=True``，因此并发重传不会覆写别人刚收窄的结果；
      并发登记同一条新核实时也只有一次生效，其余按重传返回。
    * recompute_fn(starters, subs, scores, edges) 抛 InfeasibleError 时整个
      事务回滚，已有核实记录与结果保持不变。

    返回 {"status", "result", "duplicate"}。
    """
    with _write_lock, connect() as conn:
        # 立即拿数据库写锁：本进程外的其他 worker 的核实事务要么先于本事务
        # 提交（随后可见），要么排在本事务之后，杜绝跨进程交错。
        _execute_with_retry(conn, "BEGIN IMMEDIATE")
        game = conn.execute(
            "SELECT starters FROM games WHERE id=?", (game_id,)
        ).fetchone()
        if game is None:
            raise KeyError(game_id)
        starters = json.loads(game["starters"])

        cur = _execute_with_retry(
            conn,
            "INSERT OR IGNORE INTO verifications (game_id, period, remaining, "
            "first_table, first_row, second_table, second_row, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                game_id,
                edge.period,
                edge.remaining,
                edge.first_table,
                edge.first_row,
                edge.second_table,
                edge.second_row,
                note,
            ),
        )
        if cur.rowcount == 0:
            # 重传：只回读已提交结果（同一事务内，必然与已生效核实清单对应）。
            row = conn.execute(
                "SELECT * FROM results WHERE game_id=?", (game_id,)
            ).fetchone()
            if row is not None:
                return {
                    "status": "ok",
                    "result": _row_to_result(row),
                    "duplicate": True,
                }
            # 防御性兜底：建赛时一定会写首版结果，正常不会走到这里。
            result = recompute_fn(
                starters,
                load_subs(conn, game_id),
                load_scores(conn, game_id),
                load_edges(conn, game_id),
            )
            return {"status": "ok", "result": result, "duplicate": True}

        # 新核实：重新读取提交时已生效的全部核实（含本次插入）再计算。
        subs = load_subs(conn, game_id)
        scores = load_scores(conn, game_id)
        edges = load_edges(conn, game_id)
        # recompute 抛异常时 connect() 上下文会自动回滚，已有记录与结果不变。
        result = recompute_fn(starters, subs, scores, edges)
        _persist_result(conn, game_id, result)
        return {"status": "ok", "result": result, "duplicate": False}


def _row_to_result(row: sqlite3.Row) -> dict:
    """把 results 行还原成 compute() 返回形状的字典（groups 为 dict 列表）。"""
    ranges = json.loads(row["ranges_json"])
    return {
        "feasible": bool(row["feasible"]),
        "ranges": ranges,
        "groups": json.loads(row["groups_json"]),
        "total_legal_orders": row["total_legal_orders"],
        "players": sorted(ranges),
        "infeasible_reason": row["infeasible_reason"],
    }


def _persist_result(conn, game_id: int, result: dict) -> None:
    """写入最新结果并留快照；只在有效核实（或建赛）的事务里调用。"""
    feasible = 1 if result.get("feasible") else 0
    ranges = json.dumps(result.get("ranges", {}), ensure_ascii=False)
    groups = json.dumps(
        [g.__dict__ if hasattr(g, "__dict__") else g for g in result.get("groups", [])],
        ensure_ascii=False,
    )
    total = result.get("total_legal_orders", 0)
    reason = result.get("infeasible_reason")
    _execute_with_retry(
        conn,
        "INSERT INTO results (game_id, feasible, ranges_json, groups_json, "
        "total_legal_orders, infeasible_reason, updated_at) "
        "VALUES (?,?,?,?,?,?,datetime('now')) "
        "ON CONFLICT(game_id) DO UPDATE SET feasible=excluded.feasible, "
        "ranges_json=excluded.ranges_json, groups_json=excluded.groups_json, "
        "total_legal_orders=excluded.total_legal_orders, "
        "infeasible_reason=excluded.infeasible_reason, "
        "updated_at=datetime('now')",
        (game_id, feasible, ranges, groups, total, reason),
    )
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM verifications WHERE game_id=?", (game_id,)
    ).fetchone()["c"]
    _execute_with_retry(
        conn,
        "INSERT INTO result_snapshots (game_id, feasible, ranges_json, "
        "groups_json, total_legal_orders, verification_count) "
        "VALUES (?,?,?,?,?,?)",
        (game_id, feasible, ranges, groups, total, count),
    )


def list_games() -> list[sqlite3.Row]:
    with read_connect() as conn:
        return conn.execute(
            "SELECT g.id, g.name, g.created_at, r.feasible, "
            "r.total_legal_orders, r.updated_at, "
            "(SELECT COUNT(*) FROM verifications v WHERE v.game_id=g.id) "
            "AS verification_count "
            "FROM games g LEFT JOIN results r ON r.game_id=g.id "
            "ORDER BY g.id DESC"
        ).fetchall()


def get_game_detail(game_id: int) -> Optional[dict]:
    # 单个只读事务：核实清单与结果行来自同一次提交，不会读到混搭版本。
    with read_connect() as conn:
        game = conn.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
        if game is None:
            return None
        subs = load_subs(conn, game_id)
        scores = load_scores(conn, game_id)
        edges = load_edges(conn, game_id)
        result_row = conn.execute(
            "SELECT * FROM results WHERE game_id=?", (game_id,)
        ).fetchone()
        return {
            "game": game,
            "starters": json.loads(game["starters"]),
            "subs": subs,
            "scores": scores,
            "edges": edges,
            "result_row": result_row,
        }
