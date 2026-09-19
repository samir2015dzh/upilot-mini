/* UPilot-mini 前端：概览 / 任务明细 / 设置（零依赖，纯原生 JS） */
"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (b) => {
  b = Number(b || 0);
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"]; let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return (b >= 100 || i === 0 ? b.toFixed(0) : b.toFixed(1)) + " " + u[i];
};
const fmtAgo = (ts) => {
  if (!ts) return "–";
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 60) return Math.floor(d) + " 秒前";
  if (d < 3600) return Math.floor(d / 60) + " 分钟前";
  if (d < 86400) return Math.floor(d / 3600) + " 小时前";
  return Math.floor(d / 86400) + " 天前";
};
const secsLeft = (ts) => Math.max(0, Math.round((ts || 0) - Date.now() / 1000));
const fmtAbs = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "–";
const base = (p) => String(p || "").split(/[\\/]/).pop();
const cls = (s) => "b-" + String(s || "").replace(/\s/g, "");
const shortSha = (s) => s ? String(s).slice(0, 12) + "…" : "–";

const S = {
  tab: "overview",
  auto: true, refreshMs: 5000, lastFetch: 0, paused: false, overview: null,
  tasks: { status: "全部", q: "", page: 1, limit: 50, total: 0, rows: [], current: null },
  cfg: null,
  STATUSES: ["全部", "进行中", "待上传", "待重试", "已完成", "错误"],
};
const INTERVALS = [3000, 5000, 10000, 30000];

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("on"), 2800);
}
async function api(path, opt) {
  const r = await fetch(path, Object.assign({ cache: "no-store" }, opt || {}));
  if (r.status === 401) { location.href = "/login"; throw new Error("未登录"); }
  const j = await r.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
  if (!r.ok && j.error) throw new Error(j.error);
  return j;
}
async function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}

/* ---------------- 标签切换 ---------------- */
function switchTab(name) {
  S.tab = name;
  for (const t of ["overview", "tasks", "settings"]) {
    $("#view-" + t).hidden = (t !== name);
    $("#tab-" + t).classList.toggle("on", t === name);
  }
  if (name === "tasks") loadTasks();
  if (name === "settings") loadConfig();
}

/* ---------------- 概览 ---------------- */
function ring(pct) {
  const C = 2 * Math.PI * 40;
  $("#ring-fg").setAttribute("stroke-dashoffset", String(C * (1 - Math.max(0, Math.min(1, pct)))));
  $("#ring-val").textContent = Math.round(pct * 100) + "%";
}

function renderOverview(d) {
  if (!d || !d.stats) return;
  S.overview = d; S.paused = !!d.paused;
  const st = d.stats || {}, by = st.by_status || {};
  const done = by["已上传"]?.count || 0;
  const activeCount = (by["处理中"]?.count || 0) + (by["秒传中"]?.count || 0)
    + (by["上传中"]?.count || 0) + (by["待重试"]?.count || 0);

  $("#env-pill").innerHTML = d.dry_run
    ? '<span class="dot amber"></span>dry-run 离线自检 · 数字为模拟'
    : '<span class="dot"></span>在线运行';
  const au = d.auth || {};
  const useSession = !!(au.enabled && au.mode === "form");
  $("#btn-logout").style.display = useSession ? "" : "none";
  if (useSession) $("#logout-label").textContent = "退出 " + (au.user || "");

  $("#s-total").textContent = st.total?.count ?? 0;
  $("#s-total-size").textContent = "合计 " + fmtBytes(st.total?.bytes);
  $("#s-done").textContent = done;
  $("#s-done-size").textContent = "分块 " + fmtBytes(st.normal?.bytes) + " 走了流量 · 其中秒传 " + (st.rapid?.count ?? 0) + " 个";
  $("#s-rapid").textContent = st.rapid?.count ?? 0;
  $("#s-rapid-size").textContent = "省下上行 " + fmtBytes(st.rapid?.bytes);
  $("#s-active").textContent = activeCount;
  $("#s-retry").textContent = "秒传重试累计 " + (st.retry_total ?? 0) + " 次";

  const finished = (st.rapid?.count || 0) + (st.normal?.count || 0);
  ring(finished ? (st.rapid?.count || 0) / finished : 0);
  $("#l-rapid").textContent = st.rapid?.count ?? 0;
  $("#l-done").textContent = finished;
  $("#l-saved").textContent = fmtBytes(st.rapid?.bytes);
  $("#l-normal").textContent = fmtBytes(st.normal?.bytes);
  $("#l-retry").textContent = st.retry_total ?? 0;

  const act = d.active || [];
  const interval = Number(d.config?.retry_interval || 60);
  $("#active-list").innerHTML = act.length ? act.map(f => {
    const pct = Number(f.progress || 0);
    let barCls = "", width = Math.max(pct, 2), note = fmtBytes(f.size);
    if (f.status === "待重试") {
      const left = secsLeft(f.next_retry_at);
      barCls = "retry";
      width = Math.max(4, Math.min(1, left / interval) * 100);
      note = `倒计时 ${left}s · 第 ${f.retry_count} 次秒传重试`;
    } else if (f.status === "秒传中") {
      barCls = "rapid"; width = 100; note = "只发校验请求，不重传数据";
    } else if (f.status === "上传中") {
      barCls = "upload"; width = Math.max(pct, 4); note = `分块上传 ${pct.toFixed(1)}%`;
    } else if (f.status === "处理中") {
      width = Math.max(pct, 4); note = `计算 SHA1 ${pct.toFixed(0)}%`;
    }
    return `<div class="row">
      <div class="name">
        <div class="t">${esc(base(f.path))}</div>
        <div class="p" title="${esc(f.target_dir || f.path)}">${esc(f.target_dir || "…")}</div>
        <div class="bar"><i class="${barCls}" style="width:${width.toFixed(1)}%"></i></div>
      </div>
      <div class="meta">${esc(note)}</div>
      <div class="badge ${cls(f.status)}">${esc(f.status)}</div>
    </div>`;
  }).join("") : '<div class="empty">当前没有在处理的任务 · 静静等着新文件出现</div>';

  $("#pause-label").textContent = S.paused ? "恢复调度" : "暂停调度";
  $("#btn-pause").style.color = S.paused ? "var(--amber)" : "";
  const q = d.queue || {};
  $("#queue-list").innerHTML = ["待处理", "待秒传", "待重试", "待上传", "错误"]
    .map(k => `<div class="kv"><span>${k}</span><span>${q[k] || 0} 个</span></div>`).join("");
  const hints = [];
  if (S.paused) hints.push('<div class="hint warn">调度已暂停：不再派发新任务（在途任务会跑完），点「恢复调度」继续</div>');
  if (d.dry_run) hints.push('<div class="hint">dry-run 模式：不联网、不消耗流量，只验证扫描与目录映射</div>');
  if ((q["错误"] || 0) > 0) hints.push(`<div class="hint err">有 ${q["错误"]} 个文件上传失败，原因见「任务明细」，可点「失败项重新入队」重试</div>`);
  if (!hints.length) hints.push('<div class="hint">一切正常：队列为空，没有报错</div>');
  $("#hints").innerHTML = hints.join("");

  const rec = d.recent || [];
  $("#recent-body").innerHTML = rec.length ? rec.map(f => {
    const msg = String(f.message || "");
    let [note, dirPart] = msg.includes("→") ? msg.split("→").map(s => s.trim()) : [msg, ""];
    note = note.replace(/将在\s*\d+\s*s?\s*后/, "已排入队列");
    const dir = f.target_dir || dirPart || "–";
    return `<tr>
      <td class="mono" title="${esc(fmtAbs(f.updated_at))}">${fmtAgo(f.updated_at)}</td>
      <td title="${esc(f.path)}">${esc(base(f.path))}</td>
      <td><span class="badge ${cls(f.status)}">${esc(f.status)}</span></td>
      <td class="mono" title="${esc(dir)}">${esc(dir)}</td>
      <td style="color:var(--muted)" title="${esc(msg)}">${esc(note)}</td>
    </tr>`;
  }).join("") : '<tr><td colspan="5" class="empty">还没有记录</td></tr>';

  const order = ["待处理", "待秒传", "秒传中", "待重试", "待上传", "上传中", "已上传", "错误"];
  const colors = { "待处理": "#94a3b8", "待秒传": "#94a3b8", "秒传中": "#60a5fa", "待重试": "#fbbf24",
                   "待上传": "#94a3b8", "上传中": "#a78bfa", "已上传": "#34d399", "错误": "#fb7185" };
  const maxv = Math.max(1, ...order.map(k => by[k]?.count || 0));
  const totalCount = st.total?.count || 0;
  $("#status-bars").innerHTML = order.map(k => {
    const c = by[k]?.count || 0;
    const share = totalCount ? Math.round(c / totalCount * 100) : 0;
    return `<div class="b1"><span>${k}</span>
      <div class="track"><i style="width:${(c / maxv * 100).toFixed(1)}%;background:${colors[k]}"></i></div>
      <span style="text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap"><span style="color:var(--fg)">${c}</span><span style="color:var(--dim);font-size:11px"> ${share}%</span></span></div>`;
  }).join("");

  $("#last-scan").textContent = fmtAgo(d.last_scan);
  $("#scan-rounds").textContent = d.scan_rounds ?? 0;
  const c = d.config || {};
  const kv = [
    ["监控目录", c.source],
    ["115 目标根", c.target_root],
    ["扫描间隔", c.scan_interval + " 秒"],
    ["稳定判定", c.stable_rounds + " 轮"],
    ["并发", c.concurrency + " 个文件"],
    ["小文件阈值", c.small_file_mb + " MB"],
    ["分块大小", c.partsize_mb + " MB"],
    ["秒传重试", `${c.retry_interval} 秒 / ${c.retry_max_count} 次 / ${c.retry_max_hours} 小时`],
    ["初始化节流", c.init_min_interval + " 秒"],
  ];
  $("#cfg-list").innerHTML = kv.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><span title="${esc(v)}">${esc(v)}</span></div>`).join("");
  $("#f-db").textContent = c.db || "/config/state.db";

  // 标签上的计数
  $("#tab-tasks-cnt").textContent = (st.total?.count ?? 0) ? `(${st.total.count})` : "";
}

async function refreshOverview() {
  try {
    const d = await api("/api/status");
    renderOverview(d);
    $("#live-dot").classList.remove("off");
    $("#live-text").textContent = (S.auto ? `每 ${S.refreshMs / 1000} 秒自动刷新` : "自动刷新已关闭") + " · 刚刚同步";
  } catch (e) {
    $("#live-dot").classList.add("off");
    $("#live-text").textContent = "连接断开，重试中…";
  }
}

async function act(path, label) {
  try {
    const j = await post(path);
    toast(j.message || label);
    refreshOverview();
  } catch (e) { toast("操作失败：" + e.message); }
}

/* ---------------- 任务明细 ---------------- */
async function loadTasks() {
  const t = S.tasks;
  try {
    const q = new URLSearchParams({ status: t.status, q: t.q, limit: String(t.limit), offset: String((t.page - 1) * t.limit) });
    const j = await api("/api/tasks?" + q.toString());
    t.rows = j.rows || []; t.total = j.total || 0;
    if (!t.rows.length && t.page > 1) { t.page = 1; return loadTasks(); }
    renderTasks();
  } catch (e) { toast("加载任务失败：" + e.message); }
}

function renderTasks() {
  const t = S.tasks;
  $("#t-status").innerHTML = S.STATUSES.map(s =>
    `<button class="${s === t.status ? "on" : ""}" data-status="${esc(s)}">${esc(s)}</button>`).join("");
  $("#t-status").querySelectorAll("button").forEach(b => {
    b.onclick = () => { t.status = b.dataset.status; t.page = 1; loadTasks(); };
  });
  $("#t-limit").value = String(t.limit);
  $("#t-q").value = t.q;

  const rows = t.rows.map(f => {
    const pct = Number(f.progress || 0);
    const prog = ["上传中", "处理中", "秒传中"].includes(f.status)
      ? `<div class="prog-wrap"><div class="prog"><i style="width:${Math.max(2, Math.min(100, pct)).toFixed(0)}%"></i></div><span>${pct.toFixed(0)}%</span></div>`
      : "–";
    const mode = f.upload_mode === "rapid" ? "秒传" : (f.upload_mode === "normal" ? "分块" : "–");
    return `<tr>
      <td title="${esc(f.path)}">
        <div style="overflow:hidden;text-overflow:ellipsis">${esc(base(f.path))}</div>
        <div class="mono" style="font-size:11px" title="${esc(f.target_dir || f.path)}">${esc(f.target_dir || "…")}</div>
      </td>
      <td class="mono">${fmtBytes(f.size)}</td>
      <td><span class="badge ${cls(f.status)}">${esc(f.status)}</span></td>
      <td>${prog}</td>
      <td class="mono">${mode}</td>
      <td class="mono">${f.retry_count || 0}</td>
      <td class="mono" title="${esc(f.sha1 || "")}">${shortSha(f.sha1)}</td>
      <td class="mono" title="${esc(fmtAbs(f.updated_at))}">${fmtAgo(f.updated_at)}</td>
      <td>
        <div class="ops">
          <button data-act="detail" data-path="${esc(f.path)}">详情</button>
          <button data-act="upload-now" data-path="${esc(f.path)}">立即处理</button>
          <button data-act="retry" data-path="${esc(f.path)}">重新排队</button>
        </div>
      </td>
    </tr>`;
  }).join("");

  $("#tasks-body").innerHTML = rows || '<tr><td colspan="9" class="empty">这个筛选下没有文件</td></tr>';
  $("#tasks-body").querySelectorAll("button").forEach(b => {
    b.onclick = () => {
      const p = b.dataset.path, a = b.dataset.act;
      if (a === "detail") openTask(p); else taskAction(a, p);
    };
  });

  const from = t.total ? (t.page - 1) * t.limit + 1 : 0;
  const to = Math.min(t.page * t.limit, t.total);
  $("#t-range").textContent = `共 ${t.total} 个 · 当前 ${from}-${to}`;
  $("#t-pageinfo").textContent = `第 ${t.page} / ${Math.max(1, Math.ceil(t.total / t.limit))} 页`;
  $("#t-prev").disabled = t.page <= 1;
  $("#t-next").disabled = t.page >= Math.ceil(t.total / t.limit);
}

async function taskAction(action, path) {
  try {
    const j = await post("/api/task", { path, action });
    toast(j.message || "已完成");
    if ($("#drawer").classList.contains("on")) openTask(path); else loadTasks();
  } catch (e) { toast("操作失败：" + e.message); }
}

async function openTask(path) {
  const t = S.tasks;
  try {
    const j = await api("/api/task?path=" + encodeURIComponent(path));
    const f = j.task || {};
    t.current = f.path;
    $("#d-title").textContent = base(f.path);
    $("#d-path").textContent = f.path;
    const dl = [
      ["状态", f.status],
      ["大小", fmtBytes(f.size) + "（" + (f.size || 0) + " 字节）"],
      ["115 目标目录", f.target_dir || "–"],
      ["上传方式", f.upload_mode === "rapid" ? "秒传命中" : (f.upload_mode === "normal" ? "分块上传" : "–")],
      ["SHA1", f.sha1 || "还没算"],
      ["进度", (Number(f.progress || 0)).toFixed(1) + "%"],
      ["秒传重试次数", f.retry_count ?? 0],
      ["下次重试", f.next_retry_at ? fmtAbs(f.next_retry_at) + "（" + secsLeft(f.next_retry_at) + "s 后）" : "–"],
      ["稳定判定", f.stable_count ?? 0],
      ["本地大小/修改时间", (f.size || 0) + " 字节 · " + fmtAbs(f.mtime)],
      ["云端 pid / file_id", `${f.pid ?? "–"} / ${f.file_id ?? "–"}`],
      ["断点续传", f.upload_id ? `有（分块 ${f.upload_partsize || "?"} 字节）` : "无"],
      ["消息", f.message || "–"],
      ["入队时间", fmtAbs(f.created_at)],
      ["最后更新", fmtAbs(f.updated_at)],
    ];
    $("#d-body").innerHTML = dl.map(([k, v]) => `<div class="dl"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    $("#drawer").classList.add("on");
  } catch (e) { toast("读取详情失败：" + e.message); }
}

function closeDrawer() { $("#drawer").classList.remove("on"); }

/* ---------------- 设置 ---------------- */
async function loadConfig() {
  try {
    const j = await api("/api/config");
    S.cfg = j;
    renderSettings(j);
  } catch (e) { toast("读取配置失败：" + e.message); }
}

function renderSettings(payload) {
  const groups = payload.groups || [];
  $("#settings-groups").innerHTML = groups.map(g => `
    <div class="fg">
      <h3>${esc(g.title)}</h3>
      ${(g.fields || []).map(f => fieldHtml(f)).join("")}
    </div>`).join("");
  $("#cfg-path").textContent = payload.path || "/config/config.yaml";
  $("#settings-msg").innerHTML = "";
}

function fieldHtml(f) {
  const key = esc(f.key), label = esc(f.label || f.key);
  const hint = f.hint ? `<div class="hintline">${esc(f.hint)}</div>` : "";
  const tag = f.restart ? ' <span class="tag">需重建容器</span>' : "";
  if (f.type === "bool") {
    return `<div class="f">
      <label><span class="switch ${f.value ? "on" : ""}" data-switch><i></i></span>
        ${label}${tag}<input type="checkbox" data-key="${key}" data-type="bool" ${f.value ? "checked" : ""} hidden></label>${hint}
    </div>`;
  }
  if (f.type === "select") {
    const opts = (f.options || []).map(o =>
      `<option value="${esc(o)}" ${String(f.value) === String(o) ? "selected" : ""}>${esc(o)}</option>`).join("");
    return `<div class="f"><label>${label}${tag}</label>
      <select data-key="${key}" data-type="select">${opts}</select>${hint}</div>`;
  }
  if (f.type === "secret") {
    const state = f.set
      ? `<span class="secret-state">已设置${f.length ? " · " + f.length + " 字符" : ""}</span>`
      : '<span class="secret-state none">未设置</span>';
    return `<div class="f"><label>${label} ${state}</label>
      <div class="rowline">
        <input type="password" data-key="${key}" data-type="secret" value="" placeholder="留空 = 不修改" autocomplete="new-password" />
        <label class="clear"><input type="checkbox" data-clear="${key}" />清除</label>
      </div>${hint}</div>`;
  }
  const type = f.type === "int" || f.type === "float" ? "number" : "text";
  const extra = f.type === "float" ? ' step="0.1"' : "";
  return `<div class="f"><label>${label}${tag}</label>
    <input type="${type}"${extra} data-key="${key}" data-type="${esc(f.type)}" value="${esc(f.value ?? "")}" />${hint}</div>`;
}

async function saveSettings() {
  const values = {}, clear = [];
  $("#settings-groups").querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key, type = el.dataset.type;
    if (type === "bool") values[key] = el.checked;
    else values[key] = el.value;
  });
  $("#settings-groups").querySelectorAll("[data-clear]").forEach(el => {
    if (el.checked) clear.push(el.dataset.clear);
  });
  try {
    const j = await post("/api/config", { values, clear });
    const msg = j.message || "已保存";
    $("#settings-msg").innerHTML = `<div class="hint ok">${esc(msg)}${j.changed?.length ? " · 变更：" + esc(j.changed.join(", ")) : " · 没有字段变化"}</div>`;
    toast(msg);
    loadConfig();
    refreshOverview();
  } catch (e) {
    $("#settings-msg").innerHTML = `<div class="hint err">保存失败：${esc(e.message)}</div>`;
    toast("保存失败：" + e.message);
  }
}

async function refreshLogs() {
  try {
    const j = await api("/api/logs?limit=200");
    const logs = (j.logs || []).slice().reverse();
    $("#logbox").innerHTML = logs.length ? logs.map(l => {
      const time = new Date(l.t * 1000).toLocaleTimeString("zh-CN", { hour12: false });
      return `<span class="lv-${esc(l.level)}">${time} ${esc(l.level.padEnd(7))} ${esc(l.name)} | ${esc(l.msg)}</span>`;
    }).join("\n") : "（还没有日志）";
  } catch (e) { $("#logbox").textContent = "读取日志失败：" + e.message; }
}

/* ---------------- 115 扫码登录 ---------------- */
let qrTimer = null;
const QR_TEXT = { idle: "未开始", waiting: "等待扫码", scanned: "待确认", done: "已登录",
                  expired: "已过期", error: "出错" };
const QR_CLASS = { idle: "", waiting: "amber", scanned: "amber", done: "ok", expired: "err", error: "err" };

function qrSetState(st, msg) {
  const pill = $("#qr-state");
  pill.textContent = QR_TEXT[st] || st;
  pill.className = "pill" + (QR_CLASS[st] ? " " + QR_CLASS[st] : "");
  if (msg) $("#qr-msg").textContent = msg;
  $("#btn-qr-start").textContent = (st === "waiting" || st === "scanned") ? "刷新二维码" : "获取二维码";
}

async function qrStart() {
  clearTimeout(qrTimer);
  $("#qr-state").textContent = "申请中…";
  $("#qr-msg").textContent = "正在向 115 申请二维码…";
  try {
    await post("/api/p115/qrcode");
    $("#qr-canvas").innerHTML = `<img src="/api/p115/qrcode.png?t=${Date.now()}" alt="115 登录二维码" />`;
    qrSetState("waiting", "请用 115 官方 App 扫码（二维码约 5 分钟内有效）");
    qrKick();
  } catch (e) {
    $("#qr-state").textContent = "出错";
    $("#qr-msg").textContent = "申请二维码失败：" + e.message;
  }
}

async function qrPoll() {
  try {
    const j = await api("/api/p115/qrcode/status");
    const st = j.status || "idle";
    qrSetState(st, j.message || "");
    if (st === "done") {
      clearTimeout(qrTimer);
      $("#qr-canvas").innerHTML = '<span class="ok">已登录 115</span>';
      toast(j.message || "115 登录成功");
      loadConfig(); refreshOverview();
      return false;
    }
    if (st === "expired" || st === "error") { clearTimeout(qrTimer); return false; }
    return true;
  } catch (e) {
    clearTimeout(qrTimer);
    $("#qr-msg").textContent = "状态查询失败：" + e.message;
    return false;
  }
}

function qrKick() {
  // 串行轮询：上一次请求回来后再等 2.5 秒发下一次（115 的状态接口是长轮询，不能并发压）
  clearTimeout(qrTimer);
  qrTimer = setTimeout(async () => { if (await qrPoll()) qrKick(); }, 2500);
}

function qrCancel() {
  clearTimeout(qrTimer);
  qrSetState("idle", "已取消。需要时点「获取二维码」重新开始。");
  $("#qr-canvas").innerHTML = '<span class="dim">点右侧按钮获取二维码</span>';
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("#tab-overview").onclick = () => switchTab("overview");
  $("#tab-tasks").onclick = () => switchTab("tasks");
  $("#tab-settings").onclick = () => switchTab("settings");

  $("#btn-scan").onclick = () => act("/api/scan", "已触发扫描");
  $("#btn-retry").onclick = () => act("/api/retry-now", "已提前重试");
  $("#btn-requeue").onclick = () => act("/api/requeue", "已重新入队");
  $("#btn-refresh").onclick = () => { S.lastFetch = Date.now(); refreshOverview(); if (S.tab === "tasks") loadTasks(); toast("已刷新"); };
  $("#btn-pause").onclick = () => act(S.paused ? "/api/resume" : "/api/pause", S.paused ? "已恢复调度" : "已暂停调度");
  $("#btn-logout").onclick = () => { location.href = "/logout"; };
  $("#btn-auto").onclick = () => {
    const i = INTERVALS.indexOf(S.refreshMs);
    if (!S.auto) { S.auto = true; S.refreshMs = INTERVALS[0]; }
    else if (i === INTERVALS.length - 1) { S.auto = false; }
    else { S.refreshMs = INTERVALS[i + 1]; }
    $("#auto").classList.toggle("on", S.auto);
    $("#auto-label").textContent = S.auto ? (S.refreshMs / 1000) + "s" : "关";
    refreshOverview();
  };

  // 任务页
  $("#t-refresh").onclick = () => { loadTasks(); refreshOverview(); toast("已刷新任务"); };
  $("#t-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { S.tasks.q = e.target.value.trim(); S.tasks.page = 1; loadTasks(); } });
  $("#t-q").addEventListener("input", (e) => { S.tasks.q = e.target.value.trim(); });
  $("#t-limit").onchange = (e) => { S.tasks.limit = Number(e.target.value); S.tasks.page = 1; loadTasks(); };
  $("#t-prev").onclick = () => { if (S.tasks.page > 1) { S.tasks.page--; loadTasks(); } };
  $("#t-next").onclick = () => { S.tasks.page++; loadTasks(); };
  $("#d-close").onclick = closeDrawer;
  $("#drawer").onclick = (e) => { if (e.target.id === "drawer") closeDrawer(); };
  $("#d-upload").onclick = () => S.tasks.current && taskAction("upload-now", S.tasks.current);
  $("#d-requeue").onclick = () => S.tasks.current && taskAction("retry", S.tasks.current);
  $("#d-retrynow").onclick = () => S.tasks.current && taskAction("retry-now", S.tasks.current);
  $("#d-delete").onclick = () => {
    if (!S.tasks.current) return;
    if (confirm("删掉这条记录？（文件本体不动，下次扫描会重新入队）")) taskAction("delete", S.tasks.current);
  };

  // 设置页
  $("#btn-save-settings").onclick = saveSettings;
  $("#btn-save-settings-bottom").onclick = saveSettings;
  $("#btn-qr-start").onclick = qrStart;
  $("#btn-qr-cancel").onclick = qrCancel;
  $("#btn-reload-config").onclick = () => { loadConfig(); toast("已重新读取配置"); };
  $("#btn-refresh-logs").onclick = refreshLogs;
  document.addEventListener("click", (e) => {
    const sw = e.target.closest("[data-switch]");
    if (sw) {
      const on = sw.classList.toggle("on");
      const box = sw.closest(".f").querySelector('input[type=checkbox][data-key]');
      if (box) box.checked = on;
    }
    const op = e.target.closest("button.primary");
    if (op) op.disabled = false;
  });
}

/* ---------------- 启动 ---------------- */
bind();
switchTab("overview");
refreshOverview();
refreshLogs();
setInterval(() => {
  if (!S.auto) return;
  if (Date.now() - S.lastFetch >= S.refreshMs) {
    S.lastFetch = Date.now();
    if (S.tab === "tasks") loadTasks();
    if (S.tab !== "settings") refreshOverview();
  } else if (S.tab === "overview" && S.overview) {
    renderOverview(S.overview);      // 用缓存重绘，倒计时/相对时间保持鲜活
  }
}, 1000);
setInterval(() => { if (S.tab === "settings") refreshLogs(); }, 15000);
