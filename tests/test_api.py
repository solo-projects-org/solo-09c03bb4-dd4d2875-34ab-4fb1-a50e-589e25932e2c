"""API 集成测试：上传、核实、冲突登记回滚、并发提交。"""

import os
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app

SUBS_CSV = (
    "period,remaining,outgoing,incoming\n"
    "1,600,A5,B1\n"
    "1,300,A4,B2\n"
)
SCORES_CSV = (
    "period,remaining,team,points,scorer\n"
    "1,600,opp,2,\n"
    "1,600,own,3,A1\n"
    "1,300,own,2,A4\n"
    "1,120,own,1,A3\n"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def upload(client, name="测试比赛"):
    resp = client.post(
        "/api/games",
        data={"name": name, "starters": "A1,A2,A3,A4,A5"},
        files={
            "sub_file": ("subs.csv", SUBS_CSV.encode(), "text/csv"),
            "score_file": ("scores.csv", SCORES_CSV.encode(), "text/csv"),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_upload_and_initial_ranges(client):
    game_id = upload(client, "初始区间")
    detail = client.get(f"/api/games/{game_id}").json()
    assert detail["result"]["feasible"] is True
    assert detail["result"]["ranges"]["A5"] == {"min": -2, "max": 1}
    # 时间线按比赛时刻排列，同时刻两表记录都在
    first_group = detail["timeline"][0]
    assert first_group["period"] == 1 and first_group["remaining"] == 600
    assert len(first_group["subs"]) == 1 and len(first_group["scores"]) == 2


def test_upload_rejects_bad_csv_order(client):
    bad_subs = (
        "period,remaining,outgoing,incoming\n"
        "1,300,A4,B2\n"
        "1,600,A5,B1\n"  # 时刻倒退
    )
    resp = client.post(
        "/api/games",
        data={"name": "坏数据", "starters": "A1,A2,A3,A4,A5"},
        files={
            "sub_file": ("subs.csv", bad_subs.encode(), "text/csv"),
            "score_file": ("scores.csv", SCORES_CSV.encode(), "text/csv"),
        },
    )
    assert resp.status_code == 400
    assert "比赛时刻" in resp.json()["detail"]


def test_verification_narrows_and_is_persisted(client):
    game_id = upload(client, "核实生效")
    resp = client.post(
        f"/api/games/{game_id}/verifications",
        json={
            "period": 1,
            "remaining": 600,
            "first_table": "sub",
            "first_row": 1,
            "second_table": "score",
            "second_row": 1,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["duplicate"] is False
    assert body["result"]["ranges"]["A5"] == {"min": 0, "max": 0}

    detail = client.get(f"/api/games/{game_id}").json()
    assert len(detail["verifications"]) == 1
    assert detail["result"]["ranges"]["A5"] == {"min": 0, "max": 0}

def test_duplicate_verification_is_idempotent(client):
    game_id = upload(client, "重复登记")
    payload = {
        "period": 1,
        "remaining": 600,
        "first_table": "sub",
        "first_row": 1,
        "second_table": "score",
        "second_row": 1,
    }
    first = client.post(f"/api/games/{game_id}/verifications", json=payload)
    second = client.post(f"/api/games/{game_id}/verifications", json=payload)
    assert first.status_code == 200 and second.status_code == 200
    assert second.json()["duplicate"] is True
    detail = client.get(f"/api/games/{game_id}").json()
    assert len(detail["verifications"]) == 1


def test_conflicting_verification_rolls_back_and_keeps_old_result(client):
    game_id = upload(client, "冲突登记")
    ok = client.post(
        f"/api/games/{game_id}/verifications",
        json={
            "period": 1, "remaining": 600,
            "first_table": "sub", "first_row": 1,
            "second_table": "score", "second_row": 1,
        },
    )
    assert ok.status_code == 200
    before = client.get(f"/api/games/{game_id}").json()

    # 反向约束同一对事件 -> 排不出合法顺序
    conflict = client.post(
        f"/api/games/{game_id}/verifications",
        json={
            "period": 1, "remaining": 600,
            "first_table": "score", "first_row": 1,
            "second_table": "sub", "second_row": 1,
        },
    )
    assert conflict.status_code == 409
    assert "互斥" in conflict.json()["detail"]

    after = client.get(f"/api/games/{game_id}").json()
    # 已有核实记录和结果照旧
    assert len(after["verifications"]) == 1
    assert after["verifications"] == before["verifications"]
    assert after["result"]["ranges"] == before["result"]["ranges"]
    assert after["result"]["feasible"] is True


def test_verification_validation_same_second_required(client):
    game_id = upload(client, "跨时刻登记")
    resp = client.post(
        f"/api/games/{game_id}/verifications",
        json={
            "period": 1, "remaining": 999,
            "first_table": "sub", "first_row": 1,
            "second_table": "score", "second_row": 1,
        },
    )
    assert resp.status_code == 400
    assert "同节同秒" in resp.json()["detail"]


def test_concurrent_verifications_all_effective_verions_counted(tmp_path):
    """并发登记：三条互不冲突的核实，最终必须全部生效。"""
    # 构造 3 个同秒组，每组 1 换人 1 得分。
    subs_csv = (
        "period,remaining,outgoing,incoming\n"
        "1,600,A5,B1\n"
        "1,500,A4,B2\n"
        "1,400,A3,B3\n"
    )
    scores_csv = (
        "period,remaining,team,points,scorer\n"
        "1,600,own,2,A1\n"
        "1,500,own,2,A2\n"
        "1,400,own,2,A1\n"
    )
    with TestClient(app) as c:
        gid = c.post(
            "/api/games",
            data={"name": "并发", "starters": "A1,A2,A3,A4,A5"},
            files={
                "sub_file": ("s.csv", subs_csv.encode(), "text/csv"),
                "score_file": ("g.csv", scores_csv.encode(), "text/csv"),
            },
        ).json()["id"]

        payloads = [
            dict(period=1, remaining=600, first_table="sub", first_row=1,
                 second_table="score", second_row=1),
            dict(period=1, remaining=500, first_table="sub", first_row=2,
                 second_table="score", second_row=2),
            dict(period=1, remaining=400, first_table="sub", first_row=3,
                 second_table="score", second_row=3),
        ]
        errors = []

        def worker(payload):
            try:
                with TestClient(app) as local:
                    r = local.post(f"/api/games/{gid}/verifications", json=payload)
                    if r.status_code != 200:
                        errors.append((payload, r.status_code, r.text))
            except Exception as exc:  # pragma: no cover
                errors.append((payload, "exc", repr(exc)))

        threads = [threading.Thread(target=worker, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []

        detail = c.get(f"/api/games/{gid}").json()
        assert len(detail["verifications"]) == 3
        assert detail["result"]["total_legal_orders"] == 1


def test_concurrent_conflicting_registration_preserves_consistency():
    """并发对同一对事件登记正反两个方向：恰好一个成功，结果始终合法。"""
    with TestClient(app) as c:
        gid = upload(c, "并发冲突")
        forward = dict(period=1, remaining=600, first_table="sub", first_row=1,
                       second_table="score", second_row=1)
        backward = dict(period=1, remaining=600, first_table="score", first_row=1,
                        second_table="sub", second_row=1)
        outcomes = []

        def worker(payload):
            with TestClient(app) as local:
                r = local.post(f"/api/games/{gid}/verifications", json=payload)
                outcomes.append((payload["first_table"], r.status_code))

        t1 = threading.Thread(target=worker, args=(forward,))
        t2 = threading.Thread(target=worker, args=(backward,))
        t1.start(); t2.start()
        t1.join(); t2.join()

        statuses = sorted(code for _t, code in outcomes)
        assert statuses == [200, 409]
        detail = c.get(f"/api/games/{gid}").json()
        assert len(detail["verifications"]) == 1
        assert detail["result"]["feasible"] is True


def test_sample_files_served(client):
    for name in ("subs.csv", "scores.csv"):
        resp = client.get(f"/samples/{name}")
        assert resp.status_code == 200
        assert resp.text.strip()
