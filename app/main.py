"""FastAPI 路由：比赛上传、详情查询、核实登记与重算。"""

from __future__ import annotations

import os
from dataclasses import asdict

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles

from . import database as db
from .csvio import CsvError, parse_scores, parse_starters, parse_subs
from .engine import InfeasibleError, OrderEdge, compute

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIR = os.path.join(BASE_DIR, "samples")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="篮球正负值复核", version="1.0.0", lifespan=lifespan)


def _serialize_result(detail: dict) -> dict | None:
    row = detail["result_row"]
    if row is None:
        return None
    return {
        "feasible": bool(row["feasible"]),
        "ranges": _safe_json(row["ranges_json"]),
        "groups": _safe_json(row["groups_json"]),
        "total_legal_orders": row["total_legal_orders"],
        "infeasible_reason": row["infeasible_reason"],
        "updated_at": row["updated_at"],
    }


def _safe_json(raw: str):
    import json

    return json.loads(raw)


def _timeline(subs, scores) -> list[dict]:
    """按比赛时刻合并两表事件；同时刻内两表行序各自保留，仅用于展示。"""
    from .engine import _group_events

    out = []
    for (period, remaining), g_subs, g_scores in _group_events(subs, scores):
        out.append(
            {
                "period": period,
                "remaining": remaining,
                "subs": [asdict(ev) for ev in g_subs],
                "scores": [asdict(ev) for ev in g_scores],
            }
        )
    return out


def _edge_dict(edge: OrderEdge) -> dict:
    return {
        "period": edge.period,
        "remaining": edge.remaining,
        "first_table": edge.first_table,
        "first_row": edge.first_row,
        "second_table": edge.second_table,
        "second_row": edge.second_row,
    }


@app.post("/api/games")
async def create_game(
    name: str = Form(...),
    starters: str = Form(...),
    sub_file: UploadFile = File(...),
    score_file: UploadFile = File(...),
):
    name = name.strip() or "未命名比赛"
    try:
        starter_list = parse_starters(starters)
        sub_raw = await sub_file.read()
        score_raw = await score_file.read()
        subs = parse_subs(sub_raw)
        scores = parse_scores(score_raw)
    except CsvError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = compute(starter_list, subs, scores, [])
    except InfeasibleError as exc:
        # 数据本身排不出合法顺序（如下场球员不在场）：拒绝入库并说明原因。
        raise HTTPException(status_code=400, detail=exc.reason) from exc

    game_id = db.create_game(
        name=name,
        starters=starter_list,
        subs=subs,
        scores=scores,
        sub_csv=sub_raw.decode("utf-8-sig"),
        score_csv=score_raw.decode("utf-8-sig"),
        result=result,
    )
    return {"id": game_id, "result": _public_result(result)}


def _public_result(result: dict) -> dict:
    return {
        "feasible": result["feasible"],
        "ranges": result["ranges"],
        "groups": [asdict(g) for g in result["groups"]],
        "total_legal_orders": result["total_legal_orders"],
        "players": result["players"],
    }


@app.get("/api/games")
def games():
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "feasible": None if r["feasible"] is None else bool(r["feasible"]),
            "total_legal_orders": r["total_legal_orders"],
            "verification_count": r["verification_count"],
            "updated_at": r["updated_at"],
        }
        for r in db.list_games()
    ]


@app.get("/api/games/{game_id}")
def game_detail(game_id: int):
    detail = db.get_game_detail(game_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="比赛不存在")
    game = detail["game"]
    return {
        "id": game["id"],
        "name": game["name"],
        "created_at": game["created_at"],
        "starters": detail["starters"],
        "subs": [asdict(ev) for ev in detail["subs"]],
        "scores": [asdict(ev) for ev in detail["scores"]],
        "timeline": _timeline(detail["subs"], detail["scores"]),
        "verifications": [_edge_dict(e) for e in detail["edges"]],
        "result": _serialize_result(detail),
    }


@app.post("/api/games/{game_id}/verifications")
def add_verification(
    game_id: int,
    payload: dict,
):
    detail = db.get_game_detail(game_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="比赛不存在")

    try:
        period = int(payload["period"])
        remaining = int(payload["remaining"])
        first_table = str(payload["first_table"])
        first_row = int(payload["first_row"])
        second_table = str(payload["second_table"])
        second_row = int(payload["second_row"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="核实参数不完整或类型错误") from exc
    note = str(payload.get("note") or "")

    if first_table not in ("sub", "score") or second_table not in ("sub", "score"):
        raise HTTPException(status_code=400, detail="表名只能是 sub 或 score")
    if first_table == second_table:
        raise HTTPException(
            status_code=400, detail="核实必须是一笔换人与一笔得分之间的先后关系"
        )

    group_subs = {ev.row_no for ev in detail["subs"]
                  if ev.period == period and ev.remaining == remaining}
    group_scores = {ev.row_no for ev in detail["scores"]
                    if ev.period == period and ev.remaining == remaining}
    if not group_subs or not group_scores:
        raise HTTPException(
            status_code=400,
            detail=f"第 {period} 节剩余 {remaining} 秒不存在同节同秒的换人和得分，"
            "无法登记先后",
        )
    referenced = {
        ("sub", first_row): first_row in group_subs,
        ("score", first_row): first_row in group_scores,
        ("sub", second_row): second_row in group_subs,
        ("score", second_row): second_row in group_scores,
    }
    if not referenced[(first_table, first_row)]:
        raise HTTPException(
            status_code=400,
            detail=f"第 {period} 节剩余 {remaining} 秒的 {first_table} 表中"
            f"找不到第 {first_row} 行",
        )
    if not referenced[(second_table, second_row)]:
        raise HTTPException(
            status_code=400,
            detail=f"第 {period} 节剩余 {remaining} 秒的 {second_table} 表中"
            f"找不到第 {second_row} 行",
        )

    edge = OrderEdge(
        period=period,
        remaining=remaining,
        first_table=first_table,
        first_row=first_row,
        second_table=second_table,
        second_row=second_row,
    )
    try:
        outcome = db.add_verification(game_id, edge, note, compute)
    except InfeasibleError as exc:
        # 登记后排不出合法顺序：事务已回滚，已有核实与结果照旧。
        raise HTTPException(status_code=409, detail=exc.reason) from exc

    return {
        "status": "ok",
        "duplicate": outcome["duplicate"],
        "result": _public_result(outcome["result"]),
    }


@app.get("/samples/{filename}")
def sample_file(filename: str):
    if filename not in ("subs.csv", "scores.csv"):
        raise HTTPException(status_code=404, detail="样例不存在")
    return FileResponse(os.path.join(SAMPLE_DIR, filename))


app.mount(
    "/",
    StaticFiles(
        directory=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
        html=True,
    ),
)
