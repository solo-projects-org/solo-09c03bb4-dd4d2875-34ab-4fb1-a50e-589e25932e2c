"""SQLite 持久化层。

并发登记在同一进程内用一把写锁串行化，每次提交都重新读取当时已生效的全部
核实记录再重算；SQLite 打开 WAL 并对 database is locked 做重试，保证多 worker
部署时也不会丢提交。
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

    recompute_fn(starters, subs, scores, edges) 执行引擎计算；抛 InfeasibleError
    时整个事务回滚，已有核实记录与结果保持不变。返回 {"status", "result",
    "duplicate"}。
    """
    with _write_lock, connect() as conn:
        game = conn.execute(
            "SELECT starters FROM games WHERE id=?", (game_id,)
        ).fetchone()
        if game is None:
            raise KeyError(game_id)
        starters = json.loads(game["starters"])
        subs = load_subs(conn, game_id)
        scores = load_scores(conn, game_id)

        existing = {
            (
                r["period"],
                r["remaining"],
                r["first_table"],
                r["first_row"],
                r["second_table"],
                r["second_row"],
            )
            for r in conn.execute(
                "SELECT period, remaining, first_table, first_row, "
                "second_table, second_row FROM verifications WHERE game_id=?",
                (game_id,),
            )
        }
        edge_key = (
            edge.period,
            edge.remaining,
            edge.first_table,
            edge.first_row,
            edge.second_table,
            edge.second_row,
        )
        duplicate = edge_key in existing
        if not duplicate:
            # 同一对事件的反向约束已经存在 -> 必然冲突，交由引擎给出统一报错。
            _execute_with_retry(
                conn,
                "INSERT INTO verifications (game_id, period, remaining, "
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

        # 重新读取提交时已生效的全部核实（含本次插入）再计算。
        edges = load_edges(conn, game_id)
        # recompute 抛异常时 connect() 上下文会自动回滚，已有记录与结果不变。
        result = recompute_fn(starters, subs, scores, edges)
        _persist_result(conn, game_id, result, snapshot=not duplicate)
        return {"status": "ok", "result": result, "duplicate": duplicate}


def _persist_result(conn, game_id: int, result: dict, snapshot: bool = True) -> None:
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
    if snapshot:
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
    with connect() as conn:
        return conn.execute(
            "SELECT g.id, g.name, g.created_at, r.feasible, "
            "r.total_legal_orders, r.updated_at, "
            "(SELECT COUNT(*) FROM verifications v WHERE v.game_id=g.id) "
            "AS verification_count "
            "FROM games g LEFT JOIN results r ON r.game_id=g.id "
            "ORDER BY g.id DESC"
        ).fetchall()


def get_game_detail(game_id: int) -> Optional[dict]:
    with connect() as conn:
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
