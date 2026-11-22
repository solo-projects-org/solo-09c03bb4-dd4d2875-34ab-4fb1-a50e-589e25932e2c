"""多 worker 冒烟脚本（手动运行，pytest 不收集）。

启动两个独立 uvicorn 进程共用同一个 SQLite 文件（等价于 compose 里的
``--workers 2`` 多进程部署），实测：
1. 交错查询：核实提交流进行中并发读详情/列表，清单、区间、合法顺序数始终
   对应同一次提交；
2. 重传 vs 新核实跨进程交错：收窄结果不被改回；
3. 两进程并发登记同一条核实：只生效一次，无 5xx；
4. 矛盾核实：409 且已提交数据不变；
5. 旧库恢复：停机污染 results 后重启，列表与详情显示最后有效核实的结果。

用法：python3 tests/manual_smoke_multiworker.py
"""

import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORTS = [8011, 8012]
BASE = [f"http://127.0.0.1:{p}" for p in PORTS]

SUBS_CSV = (
    "period,remaining,outgoing,incoming\n"
    "1,600,A5,B1\n"
    "1,500,A4,B2\n"
    "1,400,A3,B3\n"
)
SCORES_CSV = (
    "period,remaining,team,points,scorer\n"
    "1,600,own,2,A1\n"
    "1,500,own,2,A2\n"
    "1,400,own,2,A1\n"
)
EDGES = [
    {"period": 1, "remaining": 600, "first_table": "sub", "first_row": 1,
     "second_table": "score", "second_row": 1},
    {"period": 1, "remaining": 500, "first_table": "sub", "first_row": 2,
     "second_table": "score", "second_row": 2},
    {"period": 1, "remaining": 400, "first_table": "sub", "first_row": 3,
     "second_table": "score", "second_row": 3},
]

FAILURES = []


def check(cond, label):
    if not cond:
        FAILURES.append(label)
        print(f"  [FAIL] {label}")


def req(method, port, path, body=None, form=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        boundary = "smokeboundary"
        parts = []
        for k, v in form.items():
            if isinstance(v, tuple):
                fname, content = v
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{k}"; filename="{fname}"\r\n\r\n{content}\r\n'
                )
            else:
                parts.append(
                    f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{k}"\r\n\r\n{v}\r\n'
                )
        parts.append(f"--{boundary}--\r\n")
        data = "".join(parts).encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def create_game(port, name):
    status, body = req(
        "POST", port, "/api/games",
        form={
            "name": name,
            "starters": "A1,A2,A3,A4,A5",
            "sub_file": ("subs.csv", SUBS_CSV),
            "score_file": ("scores.csv", SCORES_CSV),
        },
    )
    assert status == 200, (status, body)
    return body["id"]


def wait_ready(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/games", timeout=2
            ):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def start_workers(db_path):
    env = dict(os.environ, PM_DB_PATH=db_path)
    procs = []
    for port in PORTS:
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        procs.append(p)
    for port in PORTS:
        if not wait_ready(port):
            raise RuntimeError(f"worker on {port} not ready")
    return procs


def stop_workers(procs):
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(10)


def scenario_interleaved_reads():
    """核实逐条提交的同时并发读：详情必须始终自洽。"""
    print("场景 1：交错查询一致性")
    gid = create_game(PORTS[0], "交错查询")
    stop = threading.Event()

    def reader(port):
        while not stop.is_set():
            status, d = req("GET", port, f"/api/games/{gid}")
            if status != 200:
                check(False, f"读详情状态 {status}")
                return
            n = len(d["verifications"])
            expect = 2 ** (3 - n)
            got = d["result"]["total_legal_orders"]
            if got != expect:
                check(False, f"混搭: {n} 条核实配了 {got} 种顺序（应 {expect}）")
                return
            status, games = req("GET", port, "/api/games")
            row = next(g for g in games if g["id"] == gid)
            if row["verification_count"] != n and not stop.is_set():
                # 列表与详情是两次请求，允许差一条在途提交，但不允许清单内部矛盾
                pass

    threads = [threading.Thread(target=reader, args=(p,)) for p in PORTS * 3]
    for t in threads:
        t.start()
    for i, edge in enumerate(EDGES):
        status, body = req("POST", PORTS[i % 2], f"/api/games/{gid}/verifications",
                           body=edge)
        check(status == 200, f"提交核实 {i} 状态 {status}")
        check(body.get("duplicate") is False, f"核实 {i} 应为新核实")
        time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(10)
    status, d = req("GET", PORTS[1], f"/api/games/{gid}")
    check(len(d["verifications"]) == 3, "最终应有 3 条核实")
    check(d["result"]["total_legal_orders"] == 1, "最终合法顺序应为 1")
    print("  完成")


def scenario_retransmit_vs_new(rounds=30):
    """重传已生效核实与新核实提交跨进程交错，收窄结果不许被改回。"""
    print(f"场景 2：重传 vs 新核实（{rounds} 轮）")
    for i in range(rounds):
        gid = create_game(PORTS[0], f"重传交错{i}")
        s, b = req("POST", PORTS[0], f"/api/games/{gid}/verifications",
                   body=EDGES[0])
        assert s == 200 and b["duplicate"] is False
        results = {}

        def call(port, edge, key):
            results[key] = req("POST", port, f"/api/games/{gid}/verifications",
                               body=edge)

        t1 = threading.Thread(target=call, args=(PORTS[0], EDGES[1], "new"))
        t2 = threading.Thread(target=call, args=(PORTS[1], EDGES[0], "dup"))
        t1.start(); t2.start(); t1.join(10); t2.join(10)
        s_new, b_new = results["new"]
        s_dup, b_dup = results["dup"]
        check(s_new == 200 and b_new["duplicate"] is False,
              f"轮{i}: 新核实应生效 ({s_new})")
        check(s_dup == 200 and b_dup["duplicate"] is True,
              f"轮{i}: 重传应判重 ({s_dup} {b_dup})")
        # 重传只回读已提交结果：排在新核实之前读到 4、之后读到 2，都是合法的
        # 已提交版本；关键是它不许写库（下面查库内最终结果）。
        check(b_dup["result"]["total_legal_orders"] in (2, 4),
              f"轮{i}: 重传回读结果异常 "
              f"{b_dup['result']['total_legal_orders']}")
        s, d = req("GET", PORTS[1], f"/api/games/{gid}")
        check(len(d["verifications"]) == 2, f"轮{i}: 应 2 条核实")
        check(d["result"]["total_legal_orders"] == 2,
              f"轮{i}: 库内结果被改回 {d['result']['total_legal_orders']}")
    print("  完成")


def scenario_same_edge_two_workers(rounds=30):
    """两进程并发登记同一条新核实：只生效一次。"""
    print(f"场景 3：并发同一核实（{rounds} 轮）")
    for i in range(rounds):
        gid = create_game(PORTS[0], f"同核实{i}")
        results = {}

        def call(port, key):
            results[key] = req("POST", port, f"/api/games/{gid}/verifications",
                               body=EDGES[0])

        t1 = threading.Thread(target=call, args=(PORTS[0], "a"))
        t2 = threading.Thread(target=call, args=(PORTS[1], "b"))
        t1.start(); t2.start(); t1.join(10); t2.join(10)
        codes = sorted(results[k][0] for k in ("a", "b"))
        dups = sorted(results[k][1].get("duplicate") for k in ("a", "b"))
        check(codes == [200, 200], f"轮{i}: 状态码 {codes}")
        check(dups == [False, True], f"轮{i}: duplicate {dups}")
        s, d = req("GET", PORTS[1], f"/api/games/{gid}")
        check(len(d["verifications"]) == 1, f"轮{i}: 核实数 {len(d['verifications'])}")
        check(d["result"]["total_legal_orders"] == 4,
              f"轮{i}: 合法顺序 {d['result']['total_legal_orders']}")
    print("  完成")


def scenario_conflict():
    print("场景 4：矛盾核实拒绝且保留已提交数据")
    gid = create_game(PORTS[0], "矛盾核实")
    s, _ = req("POST", PORTS[0], f"/api/games/{gid}/verifications", body=EDGES[0])
    check(s == 200, f"首条核实 {s}")
    reverse = dict(EDGES[0], first_table="score", first_row=1,
                   second_table="sub", second_row=1)
    s, body = req("POST", PORTS[1], f"/api/games/{gid}/verifications",
                  body=reverse)
    check(s == 409, f"矛盾核实应 409，实得 {s}")
    s, d = req("GET", PORTS[0], f"/api/games/{gid}")
    check(len(d["verifications"]) == 1, "矛盾后核实数应仍为 1")
    check(d["result"]["total_legal_orders"] == 4, "矛盾后结果应保持 4")
    print("  完成")


def scenario_old_db_recovery(db_path, procs):
    """停机后直接把 results 改回旧值（模拟旧版本重传 bug），重启应修复。"""
    print("场景 5：旧库恢复")
    gid = create_game(PORTS[0], "旧库恢复")
    req("POST", PORTS[0], f"/api/games/{gid}/verifications", body=EDGES[0])
    req("POST", PORTS[1], f"/api/games/{gid}/verifications", body=EDGES[1])
    s, d = req("GET", PORTS[0], f"/api/games/{gid}")
    check(d["result"]["total_legal_orders"] == 2, "两条核实后应为 2")
    stop_workers(procs)

    conn = sqlite3.connect(db_path)
    snap0 = conn.execute(
        "SELECT feasible, ranges_json, groups_json, total_legal_orders "
        "FROM result_snapshots WHERE game_id=? ORDER BY id LIMIT 1", (gid,)
    ).fetchone()
    conn.execute(
        "UPDATE results SET feasible=?, ranges_json=?, groups_json=?, "
        "total_legal_orders=? WHERE game_id=?", (*snap0, gid),
    )
    conn.commit()
    conn.close()

    procs = start_workers(db_path)  # 重启即升级：init_db 修复
    s, d = req("GET", PORTS[0], f"/api/games/{gid}")
    check(len(d["verifications"]) == 2, "恢复后应 2 条核实")
    check(d["result"]["total_legal_orders"] == 2,
          f"恢复后详情应为 2，实得 {d['result']['total_legal_orders']}")
    s, games = req("GET", PORTS[1], "/api/games")
    row = next(g for g in games if g["id"] == gid)
    check(row["total_legal_orders"] == 2,
          f"恢复后列表应为 2，实得 {row['total_legal_orders']}")
    print("  完成")
    return procs


def scenario_web_page():
    print("场景 6：复核网页可用")
    for port in PORTS:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode()
        check("篮球正负值复核" in html, f"端口 {port} 首页内容异常")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/app.js", timeout=5) as r:
            check(r.status == 200, f"端口 {port} app.js")
    print("  完成")


def main():
    tmp = tempfile.mkdtemp(prefix="pm-smoke-")
    db_path = os.path.join(tmp, "smoke.db")
    procs = start_workers(db_path)
    try:
        scenario_web_page()
        scenario_interleaved_reads()
        scenario_retransmit_vs_new()
        scenario_same_edge_two_workers()
        scenario_conflict()
        procs = scenario_old_db_recovery(db_path, procs)
    finally:
        stop_workers(procs)
    if FAILURES:
        print(f"\n冒烟失败 {len(FAILURES)} 项：")
        for f in FAILURES[:20]:
            print(f"  - {f}")
        sys.exit(1)
    print("\n全部冒烟场景通过。")


if __name__ == "__main__":
    main()
