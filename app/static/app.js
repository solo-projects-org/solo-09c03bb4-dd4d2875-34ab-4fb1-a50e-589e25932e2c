// 篮球正负值复核前端（无框架，原生 JS）。

let currentGameId = null;

async function api(url, options) {
  const resp = await fetch(url, options);
  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    body = { detail: "服务器返回无法解析" };
  }
  if (!resp.ok) {
    const err = new Error(body.detail || `请求失败（${resp.status}）`);
    err.status = resp.status;
    throw err;
  }
  return body;
}

function showMsg(el, text, ok) {
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

function fmtClock(remaining) {
  const m = Math.floor(remaining / 60);
  const s = remaining % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

async function loadGames() {
  const games = await api("/api/games");
  const tbody = document.querySelector("#game-table tbody");
  tbody.innerHTML = "";
  if (!games.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="hint">暂无比赛，请先上传。</td></tr>`;
    return;
  }
  for (const g of games) {
    const tr = document.createElement("tr");
    const status =
      g.feasible === null
        ? `<span class="pill warn">未计算</span>`
        : g.feasible
          ? `<span class="pill ok">可排序</span>`
          : `<span class="pill bad">无合法顺序</span>`;
    tr.innerHTML = `
      <td>${g.id}</td>
      <td>${escapeHtml(g.name)}</td>
      <td>${g.verification_count}</td>
      <td>${g.total_legal_orders ?? "-"}</td>
      <td>${status}</td>
      <td>${g.updated_at ?? "-"}</td>
      <td><button data-id="${g.id}">打开</button></td>`;
    tr.querySelector("button").addEventListener("click", () => openGame(g.id));
    tbody.appendChild(tr);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function openGame(id) {
  currentGameId = id;
  const detail = await api(`/api/games/${id}`);
  document.getElementById("detail-card").classList.remove("hidden");
  renderTimeline(detail);
  renderEdges(detail.verifications);
  renderRanges(detail.result);
  document.getElementById("detail-card").scrollIntoView({ behavior: "smooth" });
}

function renderTimeline(detail) {
  const box = document.getElementById("timeline");
  box.innerHTML = "";
  for (const grp of detail.timeline) {
    const div = document.createElement("div");
    const ambiguous = grp.subs.length && grp.scores.length;
    div.className = `group-block${ambiguous ? " ambiguous" : ""}`;
    let html = `<div class="group-title">第 ${grp.period} 节 · 剩余 ${fmtClock(grp.remaining)}
      ${ambiguous ? '<span class="pill warn">同秒换人+得分，顺序待定</span>' : ""}</div>`;
    for (const ev of grp.subs) {
      html += `<div class="event-line"><span class="tag sub">换人 第${ev.row_no}行</span>
        下：${escapeHtml(ev.outgoing)} ／ 上：${escapeHtml(ev.incoming)}</div>`;
    }
    for (const ev of grp.scores) {
      const who = ev.team === "own"
        ? `本方 ${escapeHtml(ev.scorer)} +${ev.points}`
        : `对方 +${ev.points}`;
      html += `<div class="event-line"><span class="tag score">得分 第${ev.row_no}行</span>${who}</div>`;
    }
    div.innerHTML = html;
    box.appendChild(div);
  }
}

function tableLabel(t) {
  return t === "sub" ? "换人表" : "得分表";
}

function renderEdges(edges) {
  const ul = document.getElementById("edge-list");
  ul.innerHTML = "";
  if (!edges.length) {
    ul.innerHTML = `<li class="hint">暂无核实记录。</li>`;
    return;
  }
  for (const e of edges) {
    const li = document.createElement("li");
    li.textContent = `第 ${e.period} 节剩余 ${e.remaining} 秒：` +
      `${tableLabel(e.first_table)}第 ${e.first_row} 行 先于 ` +
      `${tableLabel(e.second_table)}第 ${e.second_row} 行`;
    ul.appendChild(li);
  }
}

function renderRanges(result) {
  const tbody = document.querySelector("#range-table tbody");
  tbody.innerHTML = "";
  if (!result || !result.feasible) {
    document.getElementById("order-summary").textContent =
      result && result.infeasible_reason
        ? `无合法顺序：${result.infeasible_reason}`
        : "暂无结果";
    return;
  }
  const players = Object.keys(result.ranges).sort();
  for (const p of players) {
    const { min, max } = result.ranges[p];
    const ambiguous = min !== max;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(p)}</td>
      <td class="${min < 0 ? "neg" : ""}">${min}</td>
      <td class="${max < 0 ? "neg" : "pos"}">${max}</td>
      <td>${ambiguous ? '<span class="pill warn">有歧义</span>'
                      : '<span class="pill ok">已确定</span>'}</td>`;
    tbody.appendChild(tr);
  }
  const capped = result.total_legal_orders >= 10_000_000;
  document.getElementById("order-summary").textContent =
    `当前约束下全场合法交错顺序数：${result.total_legal_orders}${capped ? "（已达上限截断）" : ""}。`;
}

// ---------- 上传 ----------

document.getElementById("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = document.getElementById("upload-msg");
  const fd = new FormData(e.target);
  try {
    const body = await api("/api/games", { method: "POST", body: fd });
    showMsg(msg, `上传成功，比赛 ID=${body.id}，已计算初始正负值范围。`, true);
    await loadGames();
    await openGame(body.id);
  } catch (err) {
    showMsg(msg, `上传被拒绝：${err.message}`, false);
  }
});

// ---------- 样例数据 ----------

document.getElementById("load-sample").addEventListener("click", async () => {
  const form = document.getElementById("upload-form");
  const [subResp, scoreResp] = await Promise.all([
    fetch("/samples/subs.csv"),
    fetch("/samples/scores.csv"),
  ]);
  const subText = await subResp.text();
  const scoreText = await scoreResp.text();
  form.elements.name.value = "样例比赛：同秒换人复核";
  form.elements.starters.value = "A1,A2,A3,A4,A5";
  form.elements.sub_file.files = makeFileList(sampleFile(subText, "subs.csv"));
  form.elements.score_file.files = makeFileList(sampleFile(scoreText, "scores.csv"));
  showMsg(document.getElementById("upload-msg"), "样例已载入表单，点击“上传并计算”。", true);
});

function sampleFile(text, name) {
  return new File([text], name, { type: "text/csv" });
}

function makeFileList(file) {
  const dt = new DataTransfer();
  dt.items.add(file);
  return dt.files;
}

// ---------- 核实登记 ----------

document.getElementById("verify-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!currentGameId) {
    return;
  }
  const msg = document.getElementById("verify-msg");
  const f = e.target;
  const payload = {
    period: Number(f.period.value),
    remaining: Number(f.remaining.value),
    first_table: f.first_table.value,
    first_row: Number(f.first_row.value),
    second_table: f.second_table.value,
    second_row: Number(f.second_row.value),
    note: f.note.value,
  };
  try {
    const body = await api(
      `/api/games/${currentGameId}/verifications`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    showMsg(
      msg,
      body.duplicate ? "该核实已存在，结果未变化。" : "核实已生效，结果已重算并写入数据库。",
      true
    );
    await openGame(currentGameId);
    await loadGames();
  } catch (err) {
    showMsg(
      msg,
      err.status === 409
        ? `登记被拒绝（已有记录与结果保持不变）：${err.message}`
        : `提交失败：${err.message}`,
      false
    );
  }
});

loadGames().catch((err) => {
  showMsg(document.getElementById("upload-msg"), `加载比赛列表失败：${err.message}`, false);
});
