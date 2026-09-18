/* 智能研究助手 Agent 前端逻辑（ES6+）
 * SPA 路由：#new（新建研究）/#history（历史）/#stats（统计）/#task/{id}（任务详情）
 * 对应《产品设计方案》3.2 节交互定义 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const show = (el) => el.classList.remove("hidden");
const hide = (el) => el.classList.add("hidden");

/* 任何拼进 innerHTML 的外部文本都必须先转义：
 * 主题/子问题来自用户输入，报告正文来自不可信网页的模型摘录。 */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* Markdown 渲染：marked 解析 + DOMPurify 净化。
 * 缺任意一个都拒绝渲染 HTML，退回纯文本 —— 绝不把未净化的 HTML 送进 DOM。 */
function renderMarkdown(md) {
  if (window.marked && window.DOMPurify) {
    return window.DOMPurify.sanitize(window.marked.parse(md));
  }
  return null;
}

function setReportHtml(el, md) {
  const html = renderMarkdown(md);
  if (html !== null) { el.innerHTML = html; return; }
  const pre = document.createElement("pre");
  pre.textContent = md;                  // 降级路径：纯文本插入，不解析任何标签
  el.replaceChildren(pre);
}

const state = {
  payload: null,       // 本次研究的请求参数（供失败重试）
  taskId: null,
  es: null,            // 进度流句柄（fetch+ReadableStream，见 listen()）
  tokens: { in: 0, out: 0 },
  timer: null,
  startedAt: 0,
  finished: false,
  report: null,        // 当前任务结果（重写后同步更新）
  taskTopic: "",
  qaBusy: false,
  rewriteBusy: false,
  historyOffset: 0,
  historySeq: 0,
  viewingVersion: null,
};

/* ================= 基础设施：API / 令牌 / toast ================= */

function authHeaders(extra = {}) {
  const t = localStorage.getItem("ra_token");
  return t ? { "X-API-Key": t, ...extra } : extra;
}

async function api(path, body, method) {
  const res = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: authHeaders(body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    toast("需要访问令牌，请在右上角 ⚙ 设置中填写", "error");
    openSettings();
    throw new Error("未授权");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `请求失败（${res.status}）`);
  return data;
}

async function downloadFile(name) {
  const res = await fetch(`/api/reports/${encodeURIComponent(name)}`,
    { headers: authHeaders() });
  if (!res.ok) throw new Error("下载失败");
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

function toast(text, kind = "info") {
  const div = document.createElement("div");
  div.className = `toast ${kind}`;
  div.textContent = text;
  $("#toastBox").appendChild(div);
  setTimeout(() => div.classList.add("show"), 10);
  setTimeout(() => { div.classList.remove("show"); setTimeout(() => div.remove(), 300); }, 3200);
}

/* ================= 路由 ================= */

const VIEWS = ["new", "task", "history", "stats"];

function switchView(name) {
  VIEWS.forEach((v) => {
    $(`#view-${v}`).classList.toggle("hidden", v !== name);
  });
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.view === name));
}

function route() {
  const h = location.hash || "#new";
  if (h.startsWith("#task/")) { switchView("task"); showTask(h.slice(6)); return; }
  if (h === "#history") { switchView("history"); loadHistory(true); return; }
  if (h === "#stats") { switchView("stats"); loadStats(); return; }
  switchView("new");
}
window.addEventListener("hashchange", route);

/* ================= 视图：新建研究 ================= */

async function makePlan() {
  const topic = $("#topicInput").value.trim();
  const err = $("#setupError");
  hide(err);
  try {
    const payload = {
      topic,
      depth: $("#depthSelect").value,
      length: $("#lengthSelect").value,
      angle: $("#angleInput").value.trim(),
    };
    const data = await api("/api/plan", payload);
    state.payload = payload;
    renderPlanEditor(data.subquestions);
    if (data.mock) show($("#modeBadge"));
    hide($("#setupCard"));
    show($("#planCard"));
  } catch (e) {
    err.textContent = e.message;              // L0 校验失败即时提示
    show(err);
  }
}

function renderPlanEditor(subquestions) {
  const box = $("#subqList");
  box.innerHTML = "";
  subquestions.forEach((sq) => box.appendChild(subqItem(sq)));
  $("#addSubqBtn").onclick = () => {
    if (box.children.length >= 8) return;
    box.appendChild(subqItem(""));
    box.lastChild.querySelector("input").focus();
  };
}

function subqItem(text) {
  const div = document.createElement("div");
  div.className = "subq-item";
  const idx = document.createElement("span");
  idx.className = "idx";
  const input = document.createElement("input");
  input.type = "text";
  input.value = text;
  input.placeholder = "输入研究子问题…";
  const del = document.createElement("button");
  del.className = "del";
  del.textContent = "✕";
  del.title = "删除该子问题";
  del.onclick = () => div.remove();
  div.append(idx, input, del);
  return div;
}

new MutationObserver(() => {
  $$("#subqList .subq-item").forEach((el, i) => (el.querySelector(".idx").textContent = i + 1));
}).observe(document.getElementById("subqList"), { childList: true });

function collectSubquestions() {
  return [...$("#subqList").querySelectorAll("input")]
    .map((el) => el.value.trim())
    .filter(Boolean);
}

async function startResearch() {
  const subquestions = collectSubquestions();
  if (!subquestions.length) {
    toast("至少需要一个子问题", "error");
    return;
  }
  state.payload.subquestions = subquestions;
  state.pollCount = 0;
  hide($("#planCard"));
  show($("#progressCard"));
  setStep("research", true);
  startClock();

  const { task_id } = await api("/api/research", state.payload);
  state.taskId = task_id;
  listen(task_id);
}

/* SSE 走 fetch + ReadableStream 而非原生 EventSource：
 * EventSource 无法携带自定义请求头，启用鉴权后会被 401 静默打回轮询，
 * 导致实时轨迹与流式预览直接消失。 */
function listen(taskId) {
  const ctrl = new AbortController();
  state.es = { close: () => ctrl.abort() };     // 与原有 close() 调用点保持兼容
  state.finished = false;

  (async () => {
    try {
      const res = await fetch(`/api/tasks/${taskId}/events`, {
        headers: authHeaders(),
        signal: ctrl.signal,
      });
      if (res.status === 401) {
        toast("需要访问令牌，请在右上角 ⚙ 设置中填写", "error");
        openSettings();
        return;
      }
      if (!res.ok || !res.body) throw new Error(`进度流不可用（${res.status}）`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const blocks = buf.split("\n\n");
        buf = blocks.pop();
        for (const block of blocks) {
          const line = block.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          let msg;
          try { msg = JSON.parse(line.slice(6)); } catch { continue; }
          handleEvent(msg);
          if (msg.type === "done") {
            state.finished = true; ctrl.abort(); onTaskDone(); return;
          }
          if (msg.type === "error") {
            state.finished = true; ctrl.abort(); showError(msg); return;
          }
        }
      }
      pollUntilDone();                     // 流结束但未见终态事件：交给轮询确认
    } catch (e) {
      if (ctrl.signal.aborted) return;     // 主动关闭不算异常
      pollUntilDone();                     // 断线兜底：轮询任务状态
    }
  })();
}

async function pollUntilDone() {
  if (state.finished) return;
  state.pollCount = (state.pollCount || 0) + 1;
  if (state.pollCount > 60) {          // 轮询上限：约 2 分钟无果转失败提示
    state.finished = true;
    showError({ message: "进度获取超时，请稍后在历史记录中查看任务结果" });
    return;
  }
  try {
    const { status } = await api(`/api/tasks/${state.taskId}`);
    if (status === "done" || status === "partial") { state.finished = true; onTaskDone(); return; }
    if (status === "failed") { state.finished = true; showError({ message: "任务失败，详见结果面板" }); return; }
  } catch (e) { /* 网络抖动，继续轮询 */ }
  setTimeout(pollUntilDone, 2000);
}

function onTaskDone() {
  stopClock();
  location.hash = `#task/${state.taskId}`;   // 统一经由任务详情视图渲染结果
}

function handleEvent(msg) {
  switch (msg.type) {
    case "stage": {
      $("#stageDetail").textContent = msg.detail || "";
      if (msg.stage) setStep(msg.stage, true);
      if (msg.stage === "write") { setStep("research", false, true); show($("#livePreview")); }
      if (msg.stage === "review") setStep("write", false, true);
      if (msg.skipped) addTrace("skipped", `⊘ ${msg.detail}`);
      break;
    }
    case "tool": {
      const icon = { ok: "✓", failed: "✗", running: "…" }[msg.status] || "·";
      const target = msg.name === "web_search" ? msg.query : msg.url;
      addTrace(msg.status, `${icon} [${msg.name}] ${target || ""} ${msg.detail || ""}`);
      break;
    }
    case "llm": {
      state.tokens.in += msg.tokens_in || 0;
      state.tokens.out += msg.tokens_out || 0;
      $("#tokenCount").textContent =
        `Token ${state.tokens.in + state.tokens.out}（入 ${state.tokens.in} / 出 ${state.tokens.out}）`;
      break;
    }
    case "chunk": {
      $("#livePreview").textContent += msg.text || "";
      $("#livePreview").scrollTop = 1e9;
      break;
    }
  }
}

function addTrace(status, text) {
  const li = document.createElement("li");
  li.className = status;
  li.textContent = text;
  $("#traceList").prepend(li);
}

const STEP_ORDER = ["plan", "research", "write", "review"];
function setStep(name, active, done) {
  const idx = STEP_ORDER.indexOf(name);
  $$(".step").forEach((el, i) => {
    el.classList.toggle("done", i < idx || (i === idx && done));
    el.classList.toggle("active", i === idx && active && !done);
  });
}

function startClock() {
  state.startedAt = Date.now();
  state.timer = setInterval(() => {
    $("#elapsed").textContent = `耗时 ${Math.round((Date.now() - state.startedAt) / 1000)}s`;
  }, 1000);
}
function stopClock() { clearInterval(state.timer); }

/* ================= 视图：任务详情（结果交付 3.2④） ================= */

async function showTask(taskId) {
  try {
    const { status, result } = await api(`/api/tasks/${taskId}`);
    if (!result) { toast("未获取到任务数据", "error"); return; }
    state.taskId = taskId;
    state.report = result;
    state.finished = ["done", "partial", "failed"].includes(status);
    state.viewingVersion = null;
    renderTaskView(status, result);
  } catch (e) {
    toast(e.message, "error");
  }
}

function renderTaskView(status, result) {
  $("#taskTopic").textContent = result.topic || "研究报告";
  const badge = $("#taskStatusBadge");
  badge.textContent = { done: "已完成", partial: "部分交付（L3）", failed: "失败", running: "进行中" }[status] || status;
  badge.className = `status-badge ${status}`;

  // 失败任务：无报告正文，展示失败原因与失败子问题
  if (!result.report_md) {
    hide($("#partialBanner"));
    const note = $("#reviewNote");
    note.textContent = "该任务未能生成报告" +
      (result.stats && result.stats.error ? `：${result.stats.error}` : "");
    show(note);
    $("#reportBody").innerHTML = `<p class="empty">此任务为失败记录（L4），可查看失败子问题后在历史中删除。</p>`;
    $("#sourceList").innerHTML = "";
    renderSubqInfo(result);
    renderStats(result.stats);
    hide($("#qaPanel"));
    hide($("#rewritePanel"));
    hide($("#versionsPanel"));
    return;
  }

  // 失败子问题与子问题清单展示（v1.3）
  renderSubqInfo(result);

  result.deliver_mode === "partial" ? show($("#partialBanner")) : hide($("#partialBanner"));
  const reasons = (result.review && result.review.reasons) || [];
  const note = $("#reviewNote");
  if (reasons.length) { note.textContent = "自检说明：" + reasons.join("；"); show(note); }
  else hide(note);

  setReportHtml($("#reportBody"), result.report_md);

  const list = $("#sourceList");
  list.innerHTML = "";
  (result.sources || []).forEach((s) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = s.url; a.target = "_blank"; a.rel = "noopener"; a.textContent = s.title || s.url;
    li.appendChild(a);
    list.appendChild(li);
  });

  renderStats(result.stats);
  bindTaskActions();

  const full = result.deliver_mode === "full";
  full ? show($("#qaPanel")) : hide($("#qaPanel"));
  full ? show($("#rewritePanel")) : hide($("#rewritePanel"));
  $("#qaList").innerHTML = "";
  if (full) { bindQa(); bindRewrite(); loadSections(); loadVersions(); }
  else hide($("#versionsPanel"));
}

function renderSubqInfo(result) {
  const box = $("#subqInfo");
  const subqs = result.subquestions || [];
  const failed = result.failed_subqs || [];
  if (!subqs.length && !failed.length) { hide(box); return; }
  const failMap = new Map(failed.map((f) => [f.subq, f.reason]));
  box.innerHTML =
    "<b>研究子问题</b>" +
    subqs.map((sq) => {
      const fr = failMap.get(sq);
      return `<div class="subq-line ${fr ? "bad" : ""}">${fr ? "✗ " : "✓ "}${esc(sq)}` +
             (fr ? `<span class="hint">（${esc(fr)}）</span>` : "") + `</div>`;
    }).join("") +
    failed.filter((f) => !subqs.includes(f.subq))
          .map((f) => `<div class="subq-line bad">✗ ${esc(f.subq)}` +
                      `<span class="hint">（${esc(f.reason)}）</span></div>`)
          .join("");
  show(box);
}

function bindTaskActions() {
  $("#downloadMdBtn").onclick = () =>
    downloadFile(state.report.report_file).catch((e) => toast(e.message, "error"));
  $("#downloadHtmlBtn").onclick = () =>
    downloadFile(state.report.report_file.replace(/\.md$/, ".html"))
      .then(() => toast("HTML 已开始下载", "ok"))
      .catch(() => toast("HTML 版本不存在（可能导出被关闭）", "error"));
  $("#printBtn").onclick = () => window.print();
  $("#copyBtn").onclick = async () => {
    await navigator.clipboard.writeText(state.report.report_md);
    $("#copyBtn").textContent = "已复制 ✓";
    setTimeout(() => ($("#copyBtn").textContent = "复制全文"), 1500);
  };
}

function renderStats(stats) {
  if (!stats) { hide($("#statsBox")); return; }
  const t = stats.total_tokens || {};
  const parts = [
    `耗时 <b>${stats.elapsed_sec}s</b>`,
    `Token <b>${t.in + (t.out || 0)}</b>（入 ${t.in || 0} / 出 ${t.out || 0}，预算 ${t.budget_cap}）`,
    `来源 <b>${stats.sources}</b> 条`,
    `子问题 ${stats.subq_total}（失败 ${stats.subq_failed}）`,
    `重写 ${stats.rewrite_count} 次`,
  ];
  if (stats.cost_yuan != null) parts.push(`成本 ≈ <b>¥${stats.cost_yuan}</b>`);
  $("#statsBox").innerHTML = parts.join(" ｜ ");
}

/* ---------- 追问与局部重写（设计方案 3.2④） ---------- */

async function loadSections() {
  try {
    const { sections } = await api(`/api/tasks/${state.taskId}/sections`);
    const sel = $("#rewriteSection");
    sel.innerHTML = "";
    sections.forEach((s) => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      sel.appendChild(opt);
    });
  } catch (e) { /* 忽略：下拉保持为空 */ }
}

function addQaItem(kind, text) {
  const div = document.createElement("div");
  div.className = `qa-item ${kind}`;
  div.textContent = kind === "q" ? "问：" + text : text;
  $("#qaList").appendChild(div);
  return div;
}

function renderAnswer(answer, sources) {
  const span = document.createElement("span");
  const map = Object.fromEntries(sources.map((s) => [s.index, s]));
  answer.split(/(\[\d+\])/).forEach((p) => {
    const m = p.match(/^\[(\d+)\]$/);
    if (m && map[m[1]]) {
      const a = document.createElement("a");
      a.href = map[m[1]].url;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = p;
      a.title = map[m[1]].title;
      span.appendChild(a);
    } else {
      span.appendChild(document.createTextNode(p));
    }
  });
  return span;
}

function bindQa() {
  $("#qaAskBtn").onclick = async () => {
    const input = $("#qaInput");
    const q = input.value.trim();
    if (!q || state.qaBusy) return;
    state.qaBusy = true;
    input.value = "";
    addQaItem("q", q);
    const a = addQaItem("a", "思考中…");
    try {
      const { answer, sources } = await api(`/api/tasks/${state.taskId}/followup`, { question: q });
      a.innerHTML = "";
      a.appendChild(renderAnswer(answer, sources || []));
    } catch (e) {
      a.textContent = "回答失败：" + e.message;
    } finally {
      state.qaBusy = false;
    }
  };
  $("#qaInput").onkeydown = (e) => e.key === "Enter" && $("#qaAskBtn").click();
}

function bindRewrite() {
  $("#rewriteBtn").onclick = async () => {
    if (state.rewriteBusy) return;
    const section = $("#rewriteSection").value;
    if (!section) return;
    state.rewriteBusy = true;
    $("#rewriteBtn").disabled = true;
    const msg = $("#rewriteMsg");
    msg.textContent = "正在重写，请稍候…";
    show(msg);
    try {
      const { report_md } = await api(`/api/tasks/${state.taskId}/rewrite`,
        { section, feedback: $("#rewriteFeedback").value.trim() });
      state.report.report_md = report_md;
      setReportHtml($("#reportBody"), report_md);
      await loadSections();
      msg.textContent = `「${section}」已重写 ✓`;
      toast(`「${section}」已重写`, "ok");
    } catch (e) {
      msg.textContent = "重写失败：" + e.message;
    } finally {
      state.rewriteBusy = false;
      $("#rewriteBtn").disabled = false;
      setTimeout(() => hide(msg), 4000);
    }
  };
}

/* ---------- 报告版本历史（v1.3：重写快照 + 恢复） ---------- */

async function loadVersions() {
  try {
    const { versions } = await api(`/api/tasks/${state.taskId}/versions`);
    const panel = $("#versionsPanel");
    if (!versions.length) { hide(panel); return; }
    const sel = $("#versionSelect");
    sel.innerHTML = "";
    versions.forEach((v) => {
      const opt = document.createElement("option");
      opt.value = v.version;
      const t = (v.created_at || "").replace("T", " ").slice(5, 16);
      opt.textContent = `v${v.version} · ${v.section || "重写前"} · ${t}`;
      sel.appendChild(opt);
    });
    show(panel);
    hide($("#versionBackBtn"));
  } catch (e) { /* 静默：版本面板非关键功能 */ }
}

async function viewVersion() {
  const v = parseInt($("#versionSelect").value, 10);
  if (!v) return;
  try {
    const { report_md } = await api(`/api/tasks/${state.taskId}/versions/${v}`);
    state.viewingVersion = v;
    setReportHtml($("#reportBody"), report_md);
    show($("#versionBackBtn"));
    hide($("#qaPanel"));
    hide($("#rewritePanel"));
    toast(`正在查看 v${v}（只读）`, "info");
  } catch (e) {
    toast(e.message, "error");
  }
}

/* 结果视图的状态：L3 部分交付不能被当成 done 展示（否则横幅与徽标会不一致） */
const viewStatus = (result) =>
  result && result.deliver_mode === "partial" ? "partial" : "done";

async function restoreVersion() {
  const v = parseInt($("#versionSelect").value, 10);
  if (!v || state.rewriteBusy) return;
  if (!confirm(`确认恢复到 v${v}？当前版本会先自动存档。`)) return;
  state.rewriteBusy = true;
  try {
    const { report_md } = await api(`/api/tasks/${state.taskId}/restore`, { version: v });
    state.report.report_md = report_md;
    state.viewingVersion = null;
    renderTaskView(viewStatus(state.report), state.report);
    await loadVersions();
    toast(`已恢复到 v${v}`, "ok");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    state.rewriteBusy = false;
  }
}

function backToLatest() {
  state.viewingVersion = null;
  renderTaskView(viewStatus(state.report), state.report);
}

/* ================= 视图：异常态（L4 + 部分交付原则） ================= */

function showError(msg) {
  stopClock();
  show($("#errorCard"));
  $("#errorMsg").textContent = `[${msg.level || "L4"}] ${msg.message || "研究失败"}`;

  const box = $("#partialSources");
  const list = $("#errorSourceList");
  list.innerHTML = "";
  api(`/api/tasks/${state.taskId}`)
    .then(({ result }) => {
      const sources = (result && result.sources) || [];
      if (sources.length) {
        show(box);
        sources.forEach((s) => {
          const li = document.createElement("li");
          const a = document.createElement("a");
          a.href = s.url; a.target = "_blank"; a.rel = "noopener";
          a.textContent = s.title || s.url;
          li.appendChild(a);
          list.appendChild(li);
        });
      } else hide(box);
    })
    .catch(() => hide(box));
}

/* ================= 视图：历史记录 ================= */

async function loadHistory(reset) {
  if (reset) state.historyOffset = 0;
  const seq = ++state.historySeq;          // 竞态守卫：丢弃过期响应（v1.3）
  try {
    const q = $("#historySearch").value.trim();
    const data = await api(
      `/api/history?limit=20&offset=${state.historyOffset}&q=${encodeURIComponent(q)}`);
    if (seq !== state.historySeq) return;
    const { total, items } = data;
    $("#historyTotal").textContent = `共 ${total} 条`;
    const body = $("#historyBody");
    if (reset) body.innerHTML = "";
    hide($("#historyEmpty"));
    if (!items.length) show($("#historyEmpty"));

    items.forEach((it) => body.appendChild(historyRow(it)));
    state.historyOffset += items.length;
    $("#historyMoreBtn").classList.toggle("hidden",
      state.historyOffset >= total || !items.length);
  } catch (e) {
    toast(e.message, "error");
  }
}

function historyRow(it) {
  const tr = document.createElement("tr");
  const time = (it.created_at || "").replace("T", " ").slice(0, 16);
  const statusText = { done: "完成", partial: "部分交付", failed: "失败" }[it.status] || it.status;
  const cost = it.cost_yuan != null ? `¥${it.cost_yuan}` : "—";
  tr.innerHTML = `
    <td>${time}</td>
    <td class="topic-cell" title="${esc(it.topic)}">${esc(it.topic)}</td>
    <td><span class="status-badge ${esc(it.status)}">${esc(statusText)}</span></td>
    <td>${it.sources_count}</td>
    <td>${it.elapsed_sec ?? "—"}s</td>
    <td>${cost}</td>
    <td class="actions">
      <button class="btn ghost small act-view">查看</button>
      <button class="btn ghost small act-md">MD</button>
      <button class="btn ghost small act-del">删除</button>
    </td>`;
  tr.querySelector(".act-view").onclick = () => { location.hash = `#task/${it.id}`; };
  if (!it.report_file) tr.querySelector(".act-md").classList.add("hidden");   // 失败任务无报告
  tr.querySelector(".act-md").onclick = () =>
    downloadFile(it.report_file).catch((e) => toast(e.message, "error"));
  tr.querySelector(".act-del").onclick = async () => {
    if (!confirm(`删除「${it.topic}」的记录与报告文件？`)) return;
    try {
      await api(`/api/tasks/${it.id}`, null, "DELETE");
      toast("已删除", "ok");
      tr.remove();
      if (!$("#historyBody").children.length) show($("#historyEmpty"));
    } catch (e) {
      toast(e.message, "error");
    }
  };
  return tr;
}

/* ================= 视图：统计 ================= */

async function loadStats() {
  try {
    const s = await api("/api/stats");
    const t = s.total_tokens || {};
    const cards = [
      ["总任务数", s.total_tasks, `成功 ${s.success_tasks} · 成功率 ${s.success_rate == null ? "—" : Math.round(s.success_rate * 100) + "%"}`],
      ["累计 Token", t.in + t.out, `输入 ${t.in} · 输出 ${t.out}`],
      ["累计成本", s.total_cost_yuan ? `¥${s.total_cost_yuan}` : "—", "按配置单价估算"],
      ["平均耗时", s.avg_elapsed_sec + "s", `共引用来源 ${s.total_sources} 条`],
      ["运行中任务", s.running_tasks, `服务已运行 ${Math.round(s.uptime_sec / 60)} 分钟`],
    ];
    $("#statsCards").innerHTML = cards.map(([label, val, sub]) => `
      <div class="stat-card">
        <div class="stat-val">${val}</div>
        <div class="stat-label">${label}</div>
        <div class="stat-sub">${sub}</div>
      </div>`).join("");
  } catch (e) {
    toast(e.message, "error");
  }
}

/* ================= 设置（访问令牌） ================= */

function openSettings() { show($("#settingsPop")); $("#tokenInput").focus(); }
function closeSettings() { hide($("#settingsPop")); }

function bindSettings() {
  $("#settingsBtn").onclick = (e) => {
    e.stopPropagation();
    $("#settingsPop").classList.contains("hidden") ? openSettings() : closeSettings();
  };
  $("#tokenSaveBtn").onclick = () => {
    localStorage.setItem("ra_token", $("#tokenInput").value.trim());
    toast("令牌已保存", "ok");
    closeSettings();
  };
  $("#tokenClearBtn").onclick = () => {
    localStorage.removeItem("ra_token");
    $("#tokenInput").value = "";
    toast("令牌已清除", "ok");
  };
  document.addEventListener("click", (e) => {
    if (!$("#settingsPop").contains(e.target) && e.target !== $("#settingsBtn")) closeSettings();
  });
}

/* ================= 重置与事件绑定 ================= */

function resetNewFlow(keepTopic) {
  if (state.es) state.es.close();
  stopClock();
  state.taskId = null;
  state.tokens = { in: 0, out: 0 };
  state.finished = false;
  state.pollCount = 0;
  state.report = null;
  state.qaBusy = false;
  state.rewriteBusy = false;
  $("#traceList").innerHTML = "";
  $("#qaList").innerHTML = "";
  $("#livePreview").textContent = "";
  hide($("#livePreview"));
  hide($("#planCard"));
  hide($("#progressCard"));
  hide($("#errorCard"));
  show($("#setupCard"));
  $("#tokenCount").textContent = "Token 0";
  $("#elapsed").textContent = "耗时 0s";
  $$(".step").forEach((el) => el.classList.remove("active", "done"));
  if (!keepTopic) $("#topicInput").value = "";
}

function bindGlobal() {
  $$(".tab").forEach((t) => (t.onclick = () => { location.hash = `#${t.dataset.view}`; }));
  $("#brandHome").onclick = () => { location.hash = "#new"; };
  $("#taskBackBtn").onclick = () => {
    location.hash = state.taskTopic ? "#history" : "#new";
  };

  $("#planBtn").addEventListener("click", makePlan);
  $("#topicInput").addEventListener("keydown", (e) => e.key === "Enter" && makePlan());
  $("#backBtn").addEventListener("click", () => { hide($("#planCard")); show($("#setupCard")); });
  $("#startBtn").addEventListener("click", startResearch);
  $("#errorAgainBtn").addEventListener("click", () => resetNewFlow(false));
  $("#retryBtn").addEventListener("click", async () => {
    hide($("#errorCard"));
    show($("#progressCard"));
    setStep("research", true);
    startClock();
    state.pollCount = 0;
    const { task_id } = await api("/api/research", state.payload);
    state.taskId = task_id;
    listen(task_id);
  });
  $("#historyMoreBtn").addEventListener("click", () => loadHistory(false));
  $("#historySearchBtn").addEventListener("click", () => loadHistory(true));
  $("#historySearch").addEventListener("keydown",
    (e) => e.key === "Enter" && loadHistory(true));
  $("#versionViewBtn").addEventListener("click", viewVersion);
  $("#versionRestoreBtn").addEventListener("click", restoreVersion);
  $("#versionBackBtn").addEventListener("click", backToLatest);

  bindSettings();

  // 启动信息：版本与 Mock 徽标
  api("/api/meta").then((m) => {
    $("#verBadge").textContent = "v" + m.version;
    if (m.mock_llm || m.mock_search) show($("#modeBadge"));
  }).catch(() => {});
}

bindGlobal();
route();
