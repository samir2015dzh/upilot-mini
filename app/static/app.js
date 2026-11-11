/* UPilot-mini 前端：概览 / 任务明细 / 设置（零依赖，纯原生 JS）
   手机按钮与操作表（只借布局，不引入框架）：
   - 点击区 ≥ 44px：Apple HIG / WCAG 2.5.5 Target Size
   - 底部操作表、安全区、取消键单独一组：
     https://github.com/ionic-team/ionic-framework/blob/main/core/src/components/action-sheet/action-sheet.ios.vars.scss
     （iOS 按钮高度 56px；本页常规按钮 44px，操作表行 52px）
   - 主按钮语义参考 Pico CSS（不引入）：
     https://github.com/picocss/pico/blob/main/scss/content/_button.scss
   - 视觉令牌还参考：Pico CSS 变量 https://picocss.com/docs/css-variables
     Open Props 圆角与阴影 https://github.com/argyleink/open-props
     shadcn/ui zinc 中性色 https://ui.shadcn.com/docs/theming
     Linear 强调色 #5E6AD2；底栏图标路径来自 Lucide https://lucide.dev
*/

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
const fmtWait = (secs) => {
  const s = Math.max(0, Math.round(Number(secs) || 0));
  if (s < 90) return s + " 秒";
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + " 分钟";
  if (s < 48 * 3600) return Math.max(1, Math.round(s / 3600)) + " 小时";
  return Math.max(1, Math.round(s / 86400)) + " 天";
};
const fmtAbs = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false }) : "–";
const base = (p) => String(p || "").split(/[\\/]/).pop();
const cls = (s) => "b-" + String(s || "").replace(/\s/g, "");
const shortSha = (s) => s ? String(s).slice(0, 12) + "…" : "–";

const S = {
  tab: "overview",
  auto: true, refreshMs: 5000, lastFetch: 0, paused: false, overview: null,
  tasks: { status: "全部", q: "", page: 1, limit: 50, total: 0, rows: [], current: null },
  cfg: null, cfgSnap: null, cfgDirty: 0,
  STATUSES: ["全部", "进行中", "待上传", "待重试", "已完成", "错误"],
};
const INTERVALS = [3000, 5000, 10000, 30000];
const THEMES = ["auto", "dark", "light"];
const THEME_LABEL = { auto: "自动", dark: "深色", light: "浅色" };
const curTheme = () => document.documentElement.dataset.theme || "auto";

function setBusy(el, on) {
  if (!el) return;
  el.disabled = !!on;
  if (on) el.setAttribute("aria-busy", "true");
  else el.removeAttribute("aria-busy");
}

let _confirmWait = null;
function closeConfirm(result) {
  const el = $("#confirm");
  if (!el) return;
  el.classList.remove("on");
  el.hidden = true;
  const fn = _confirmWait;
  _confirmWait = null;
  if (fn) fn(!!result);
}
function askConfirm({ title, body, ok = "确定" }) {
  return new Promise((resolve) => {
    closeConfirm(false);
    $("#confirm-title").textContent = title || "确认";
    $("#confirm-body").textContent = body || "";
    $("#confirm-ok").textContent = ok;
    _confirmWait = resolve;
    const el = $("#confirm");
    el.hidden = false;
    el.classList.add("on");
    $("#confirm-cancel").focus();
  });
}

let _sheetPick = null;
function closeSheet() {
  const el = $("#sheet");
  if (!el) return;
  el.classList.remove("on");
  el.hidden = true;
  _sheetPick = null;
}
function openSheet(items, onPick, title) {
  const el = $("#sheet");
  const main = $("#sheet-actions");
  const danger = $("#sheet-danger");
  $("#sheet-title").textContent = title || "操作";
  const mk = (i) => `<button type="button" data-id="${esc(i.id)}" class="${i.kind === "primary" ? "primary" : (i.kind === "danger" ? "danger" : "")}">${esc(i.label)}</button>`;
  const normal = (items || []).filter(i => i.kind !== "danger");
  const bad = (items || []).filter(i => i.kind === "danger");
  main.innerHTML = normal.map(mk).join("");
  danger.innerHTML = bad.map(mk).join("");
  danger.hidden = !bad.length;
  _sheetPick = onPick;
  el.hidden = false;
  el.classList.add("on");
}
function onSheetClick(id) {
  const fn = _sheetPick;
  closeSheet();
  if (fn && id) fn(id);
}

function cycleTheme() {
  const next = THEMES[(THEMES.indexOf(curTheme()) + 1) % THEMES.length];
  if (next === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = next;
  try { localStorage.setItem("upilot-theme", next); } catch (e) { /* 隐私模式写不了就算了 */ }
  const lab = $("#theme-label");
  if (lab) lab.textContent = THEME_LABEL[next];
  return next;
}
function cycleAuto() {
  const i = INTERVALS.indexOf(S.refreshMs);
  if (!S.auto) { S.auto = true; S.refreshMs = INTERVALS[0]; }
  else if (i === INTERVALS.length - 1) { S.auto = false; }
  else { S.refreshMs = INTERVALS[i + 1]; }
  const sw = $("#auto");
  if (sw) sw.classList.toggle("on", S.auto);
  const lab = $("#auto-label");
  if (lab) lab.textContent = S.auto ? (S.refreshMs / 1000) + "s" : "关";
  refreshOverview();
}
function setTaskCount(n) {
  const text = n ? `(${n})` : "";
  const a = $("#tab-tasks-cnt"), b = $("#dock-tasks-cnt");
  if (a) a.textContent = text;
  if (b) b.textContent = text;
}
function syncDock(name) {
  document.querySelectorAll(".dock-item").forEach(btn => {
    btn.classList.toggle("on", btn.dataset.tab === name);
  });
}


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
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  for (const t of ["overview", "tasks", "settings"]) {
    $("#view-" + t).hidden = (t !== name);
    $("#tab-" + t).classList.toggle("on", t === name);
  }
  syncDock(name);
  if (name === "tasks") loadTasks();
  // 有未保存的修改时切回来不重新拉配置，免得把刚填的内容冲掉
  if (name === "settings" && !S.cfgDirty) loadConfig();
}

/* ---------------- 概览 ---------------- */
function ring(pct) {
  const C = 2 * Math.PI * 40;
  $("#ring-fg").setAttribute("stroke-dashoffset", String(C * (1 - Math.max(0, Math.min(1, pct)))));
  $("#ring-val").textContent = Math.round(pct * 100) + "%";
}


function seasonLabel(dir) {
  const parts = String(dir || "").replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length >= 2) return parts[parts.length - 2] + " / " + parts[parts.length - 1];
  return parts[parts.length - 1] || "未分类";
}

function renderSeasons(d) {
  const box = $("#season-list");
  if (!box) return;
  const pending = d.retry_pending || {};
  const byDir = {};
  (pending.by_dir || []).forEach(row => {
    const key = row.dir || "";
    byDir[key] = {dir: key, count: row.count || 0, due: row.due || 0, reps: []};
  });
  (d.replacements || []).forEach(r => {
    const key = r.dir || "";
    if (!byDir[key]) byDir[key] = {dir: key, count: 0, due: 0, reps: []};
    if (byDir[key].reps.length < 3) byDir[key].reps.push(r);
  });
  const rows = Object.values(byDir).sort((a, b) => (b.count - a.count) || (b.reps.length - a.reps.length) || a.dir.localeCompare(b.dir));
  if (!rows.length) {
    const total = pending.total || 0;
    box.innerHTML = total
      ? `<div class="empty">有 ${total} 个文件在等秒传，但还没对上季度目录</div>`
      : '<div class="empty">还没有待重试或洗板记录</div>';
    return;
  }
  const head = (pending.total || 0)
    ? `<div class="sub" style="margin-bottom:8px">待重试 ${pending.total} 个${pending.due ? "，其中 " + pending.due + " 个到点了" : ""}</div>`
    : "";
  box.innerHTML = head + rows.map(row => {
    const wait = row.count
      ? (row.due ? `待重试 ${row.count} 个，其中 ${row.due} 个到点了` : `待重试 ${row.count} 个`)
      : "没有待重试";
    const reps = row.reps.map(r => {
      const gone = (r.removed || []).map(esc).join("、");
      const when = fmtAgo(r.at);
      return r.dry
        ? `仅记录 ${when}：留 ${esc(r.kept)}，本该删 ${gone}`
        : `洗板 ${when}：留 ${esc(r.kept)}，旧版 ${gone} 已进回收站`;
    }).map(s => `<div class="rep">${s}</div>`).join("");
    return `<div class="season">
      <div class="lab" title="${esc(row.dir)}">${esc(seasonLabel(row.dir))}</div>
      <div class="sub">${wait}</div>
      ${reps}
    </div>`;
  }).join("");
}

function renderOverview(d) {
  if (!d || !d.stats) return;
  S.overview = d; S.paused = !!d.paused;
  const st = d.stats || {}, by = st.by_status || {};
  const done = by["已上传"]?.count || 0;
  const activeCount = (by["处理中"]?.count || 0) + (by["秒传中"]?.count || 0)
    + (by["上传中"]?.count || 0) + (by["待重试"]?.count || 0);

  const env = $("#env-pill");
  const envLabel = d.dry_run ? "dry-run 离线自检 · 数字为模拟" : "在线运行";
  env.className = d.dry_run ? "env-dot amber" : "env-dot";
  env.title = envLabel;
  env.setAttribute("aria-label", envLabel);
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
      width = Math.max(4, Math.min(1, left / Math.max(interval, left, 1)) * 100);
      const asked = Number(f.retry_count || 0);
      note = asked
        ? `约 ${fmtWait(left)} 后再问 · 已问 ${asked} 次（不会真上传）`
        : `约 ${fmtWait(left)} 后再问`;
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
  if (d.auth_failed) hints.push(`<div class="hint err">115 登录失效：已暂停向 115 发请求（不算重试）。到「设置 → 115 账号」换新 cookie 就会自动继续</div>`);
  if (d.rate_limited) {
    const m = Math.max(1, Math.ceil(Number(d.rate_limited.retry_in || 0) / 60));
    hints.push(`<div class="hint warn">115 暂时限制访问：已暂停请求（不算重试），约 ${m} 分钟后自动再试</div>`);
  }
  if (S.paused) hints.push('<div class="hint warn">调度已暂停：不再派发新任务（在途任务会跑完），点「恢复调度」继续</div>');
  if (d.dry_run) hints.push('<div class="hint">dry-run 模式：不联网、不消耗流量，只验证扫描与目录映射</div>');
  if ((q["错误"] || 0) > 0) hints.push(`<div class="hint err">有 ${q["错误"]} 个文件上传失败，原因见「任务明细」，可点「失败项重新入队」重试</div>`);
  const nRetry = q["待重试"] || 0;
  const nPrep = (q["待处理"] || 0) + (q["待秒传"] || 0) + (q["处理中"] || 0);
  if (nRetry > 0) {
    const rapidOnly = (d.config || {}).upload_mode === "rapid_only";
    hints.push(rapidOnly
      ? `<div class="hint">有 ${nRetry} 个文件在等秒传：云端还没有相同文件，会自动再问，不会真上传</div>`
      : `<div class="hint">有 ${nRetry} 个文件在等下一次秒传</div>`);
  }
  if (nPrep > 0) hints.push(`<div class="hint">有 ${nPrep} 个文件正在准备或计算校验</div>`);
  if (!hints.length) hints.push('<div class="hint">一切正常：没有待处理文件，也没有报错</div>');
  // 最近的洗板替换（同一集只留最新那份）：保留了谁、把谁移进了回收站、凭什么判新旧
  (d.replacements || []).slice(0, 5).forEach(r => {
    const gone = (r.removed || []).map(esc).join("、");
    hints.push(r.dry
      ? `<div class="hint warn" title="判据：${esc(r.basis)}">洗板（仅记录）${fmtAgo(r.at)}：留 ${esc(r.kept)}，本该删 ${gone}</div>`
      : `<div class="hint" title="判据：${esc(r.basis)}">洗板替换 ${fmtAgo(r.at)}：留 ${esc(r.kept)}，旧版 ${gone} 已进回收站</div>`);
  });
  $("#hints").innerHTML = hints.join("");
  renderSeasons(d);

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
    ["上传模式", c.upload_mode_label || c.upload_mode],
    ["小文件阈值", c.small_file_mb + " MB"],
    ["分块大小", c.partsize_mb + " MB"],
    ["完成后通知", c.notify_enabled ? "已开启 · " + (c.notify_url || "还没填地址") : "关闭"],
    ["秒传重试", c.upload_mode === "rapid_only"
      ? `${c.retry_interval} 秒 ×${c.retry_max_count} 次 → 之后每 ${c.retry_long_interval} 秒，一直等`
      : `${c.retry_interval} 秒 / ${c.retry_max_count} 次 / ${c.retry_max_hours} 小时`],
    ["初始化节流", c.init_min_interval + " 秒"],
  ];
  $("#cfg-list").innerHTML = kv.map(([k, v]) =>
    `<div class="kv"><span>${k}</span><span title="${esc(v)}">${esc(v)}</span></div>`).join("");
  $("#f-db").textContent = c.db || "/config/state.db";

  // 标签上的计数
  setTaskCount(st.total?.count ?? 0);
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

async function act(path, label, btn) {
  setBusy(btn, true);
  try {
    const j = await post(path);
    toast(j.message || label);
    refreshOverview();
  } catch (e) { toast("操作失败：" + e.message); }
  finally { setBusy(btn, false); }
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
  renderLimitDd(t.limit);
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
      if (a === "detail") openTask(p); else taskAction(a, p, b);
    };
  });
  renderTaskCards(t.rows);

  const from = t.total ? (t.page - 1) * t.limit + 1 : 0;
  const to = Math.min(t.page * t.limit, t.total);
  $("#t-range").textContent = `共 ${t.total} 个 · 当前 ${from}-${to}`;
  $("#t-pageinfo").textContent = `第 ${t.page} / ${Math.max(1, Math.ceil(t.total / t.limit))} 页`;
  $("#t-prev").disabled = t.page <= 1;
  $("#t-next").disabled = t.page >= Math.ceil(t.total / t.limit);
}

function renderTaskCards(rows) {
  const box = $("#tasks-cards");
  if (!box) return;
  if (!rows || !rows.length) {
    box.innerHTML = '<div class="empty">这个筛选下没有文件</div>';
    return;
  }
  box.innerHTML = rows.map(f => {
    const pct = Number(f.progress || 0);
    const prog = ["上传中", "处理中", "秒传中"].includes(f.status)
      ? `<div class="prog-wrap"><div class="prog"><i style="width:${Math.max(2, Math.min(100, pct)).toFixed(0)}%"></i></div><span>${pct.toFixed(0)}%</span></div>`
      : "";
    const mode = f.upload_mode === "rapid" ? "秒传" : (f.upload_mode === "normal" ? "分块" : "");
    const bits = [fmtBytes(f.size), mode, (f.retry_count ? "重试 " + f.retry_count : ""), fmtAgo(f.updated_at)].filter(Boolean);
    return `<article class="task-card" data-path="${esc(f.path)}" tabindex="0">
      <div class="top">
        <div class="name" title="${esc(f.path)}">${esc(base(f.path))}</div>
        <span class="badge ${cls(f.status)}">${esc(f.status)}</span>
      </div>
      <div class="path" title="${esc(f.target_dir || f.path)}">${esc(f.target_dir || "…")}</div>
      <div class="meta-row">
        <span>${esc(bits.join(" · "))}</span>
        <button type="button" class="ghost act" data-act="menu" data-path="${esc(f.path)}" aria-haspopup="dialog">操作</button>
      </div>
      ${prog}
    </article>`;
  }).join("");
  box.querySelectorAll(".task-card").forEach(card => {
    const open = () => openTask(card.dataset.path);
    card.addEventListener("click", (e) => { if (!e.target.closest("[data-act]")) open(); });
    card.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
  });
  box.querySelectorAll("[data-act=menu]").forEach(b => {
    b.onclick = (e) => { e.stopPropagation(); openTaskMenu(b.dataset.path, b); };
  });
}
function openTaskMenu(path) {
  openSheet([
    { id: "detail", label: "查看详情" },
    { id: "upload-now", label: "立即处理", kind: "primary" },
    { id: "retry", label: "重新排队" },
  ], (id) => {
    if (id === "detail") openTask(path);
    else taskAction(id, path);
  }, "文件操作");
}

async function taskAction(action, path, btn) {
  setBusy(btn, true);
  try {
    const j = await post("/api/task", { path, action });
    toast(j.message || "已完成");
    if ($("#drawer").classList.contains("on")) openTask(path); else loadTasks();
  } catch (e) { toast("操作失败：" + e.message); }
  finally { setBusy(btn, false); }
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
  $("#settings-groups").innerHTML = groups.map(g => {
    const fields = g.fields || [];
    const main = fields.filter(f => !f.advanced);      // 平时只需要看这几项
    const adv = fields.filter(f => f.advanced);        // 带高级标记的默认折起来
    return `
    <div class="fg">
      <h3>${esc(g.title)}</h3>
      ${main.map(f => fieldHtml(f)).join("")}
      ${adv.length ? `<details class="adv"><summary>高级选项</summary>
        ${adv.map(f => fieldHtml(f)).join("")}
      </details>` : ""}
    </div>`;
  }).join("");
  $("#cfg-path").textContent = payload.path || "/config/config.yaml";
  $("#settings-msg").innerHTML = "";
  document.querySelectorAll("#settings-groups textarea[data-type=map]").forEach(checkMap);
  S.cfgSnap = collectValues();
  updateDirty();
}

/* ---- 未保存修改：和渲染时的快照逐项比对，改回原值就不算 ---- */
function collectValues() {
  const values = {}, clear = [];
  const root = $("#settings-groups");
  if (!root) return { values, clear };
  root.querySelectorAll("[data-key]").forEach(el => {
    const key = el.dataset.key, type = el.dataset.type;
    if (type === "bool") values[key] = el.checked;
    else if (el.classList.contains("dd")) values[key] = el.dataset.value;
    else values[key] = el.value;
  });
  root.querySelectorAll("[data-clear]").forEach(el => { if (el.checked) clear.push(el.dataset.clear); });
  return { values, clear };
}

function dirtyKeys() {
  if (!S.cfgSnap) return [];
  const now = collectValues(), was = S.cfgSnap.values;
  const keys = Object.keys(now.values).filter(k => String(now.values[k]) !== String(was[k]));
  return keys.concat(now.clear.filter(k => !keys.includes(k)));
}

function updateDirty() {
  const keys = dirtyKeys();
  S.cfgDirty = keys.length;
  const root = $("#settings-groups");
  if (root) root.querySelectorAll(".f").forEach(f => {
    const el = f.querySelector("[data-key]");
    f.classList.toggle("dirty", !!el && keys.includes(el.dataset.key));
  });
  [$("#btn-save-settings"), $("#btn-save-settings-bottom")].forEach(b => {
    if (!b) return;
    b.classList.toggle("dirty", keys.length > 0);
    const n = b.querySelector(".n");
    if (n) n.textContent = keys.length ? String(keys.length) : "";
  });
  const st = $("#save-state");
  if (st) st.textContent = keys.length ? `${keys.length} 项修改未保存` : "所有修改已保存";
}

function fieldLabel(key) {
  for (const g of (S.cfg && S.cfg.groups) || []) {
    for (const f of g.fields || []) if (f.key === key) return f.label || key;
  }
  return key;
}

/* ---- 目录映射：按后端同样的规则即时检查，保存前就能看到哪一行有问题 ---- */
function checkMap(ta) {
  const out = ta.closest(".f").querySelector(".map-check");
  if (!out) return;
  const lines = ta.value.split(/\r?\n/);
  let n = 0, err = "";
  for (let i = 0; i < lines.length && !err; i++) {
    const t = lines[i].trim();
    if (!t || t.startsWith("#")) continue;
    const sep = t.includes("=>") ? "=>" : (t.includes("->") ? "->" : "");
    if (!sep) { err = `第 ${i + 1} 行缺少「=>」`; break; }
    const at = t.indexOf(sep);
    const src = t.slice(0, at).trim(), dst = t.slice(at + sep.length).trim();
    if (!dst) { err = `第 ${i + 1} 行缺少 115 目录`; break; }
    if (`${src}/${dst}`.split(/[\/\\]/).includes("..")) { err = `第 ${i + 1} 行不能包含 ..`; break; }
    n++;
  }
  out.classList.toggle("err", !!err);
  out.textContent = err || (n ? `${n} 条规则` : "暂无规则，全部按原目录结构上传");
}

/* 自定义下拉：原生 <select> 的弹层由系统绘制（Windows 上是白底），配上深色主题的浅色文字就看不清，
   所以整个换成自己的按钮 + 面板，颜色完全可控。 */
const DD_CHEV = '<svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9l6 6 6-6"/></svg>';

function limitOptions() { return [20, 50, 100, 200].map(n => ({ value: String(n), label: n + " 条/页" })); }

function renderLimitDd(value) {
  const wrap = $("#t-limit");
  if (!wrap) return;
  wrap.dataset.value = String(value);
  wrap.innerHTML = ddInner(limitOptions(), String(value));
}

/* 面板默认向下弹；下方空间不够、上方更宽裕时改成向上弹，并按可视空间收高度，
   免得展开后被页面底边或下面的卡片吃掉。 */
function openDd(dd) {
  const btn = dd.querySelector(".dd-btn");
  const panel = dd.querySelector(".dd-panel");
  dd.classList.remove("up");
  if (panel) panel.style.maxHeight = "";
  dd.classList.add("on");
  if (!btn || !panel) return;
  const r = btn.getBoundingClientRect();
  const below = window.innerHeight - r.bottom - 16;
  const above = r.top - 16;
  const want = Math.min(panel.scrollHeight + 12, 560);
  const goUp = below < want && above > below;
  dd.classList.toggle("up", goUp);
  panel.style.maxHeight = Math.max(150, Math.min(560, goUp ? above : below)) + "px";
}

function ddLabel(options, value) {
  const cur = (options || []).find(o => String((o && typeof o === "object") ? o.value : o) === String(value ?? ""));
  if (!cur) return value || "–";
  return (typeof cur === "object") ? (cur.label || cur.value) : cur;
}

function ddInner(options, value) {
  const items = (options || []).map(o => {
    const v = (o && typeof o === "object") ? o.value : o;
    const lb = (o && typeof o === "object") ? (o.label || o.value) : o;
    const on = String(value ?? "") === String(v) ? " on" : "";
    return `<button type="button" class="dd-opt${on}" data-value="${esc(v)}" data-label="${esc(lb)}"><span class="lb">${esc(lb)}</span></button>`;
  }).join("");
  return `<button type="button" class="dd-btn"><span class="lb">${esc(ddLabel(options, value))}</span>${DD_CHEV}</button>
    <div class="dd-panel">${items}</div>`;
}

function fieldHtml(f) {
  const key = esc(f.key), label = esc(f.label || f.key);
  // 危险提示只在开关打开时出现：关着的时候它只是噪音
  const hint = (f.hint ? `<div class="hintline">${esc(f.hint)}</div>` : "")
    + (f.note ? `<div class="hintline note">${esc(f.note)}</div>` : "")
    + (f.danger_note ? `<div class="hintline danger"${f.value ? "" : " hidden"}>${esc(f.danger_note)}</div>` : "");
  const tag = f.restart ? ' <span class="tag" title="保存后需重启容器才生效">重启生效</span>' : "";
  const ph = f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : "";
  if (f.type === "bool") {
    return `<div class="f f-bool">
      <label data-switch><span class="switch ${f.value ? "on" : ""}"><i></i></span>
        <span>${label}${tag}</span><input type="checkbox" data-key="${key}" data-type="bool" ${f.value ? "checked" : ""} hidden></label>${hint}
    </div>`;
  }
  if (f.type === "select") {
    return `<div class="f"><label>${label}${tag}</label>
      <div class="dd" data-key="${key}" data-type="select" data-value="${esc(f.value ?? "")}">${ddInner(f.options, f.value)}</div>${hint}</div>`;
  }
  if (f.type === "map") {
    const rows = f.rows || 5;
    return `<div class="f wide"><label>${label}${tag}</label>
      <textarea rows="${rows}" spellcheck="false" data-key="${key}" data-type="map" placeholder="TVs => /TVs&#10;Movies/Anime => /Movies/Anime">${esc(f.value ?? "")}</textarea>
      <div class="map-check" aria-live="polite"></div>${hint}</div>`;
  }
  if (f.type === "secret") {
    const state = f.set
      ? `<span class="secret-state">已设置${f.length ? " · " + f.length + " 字符" : ""}</span>`
      : '<span class="secret-state none">未设置</span>';
    return `<div class="f"><label>${label} ${state}</label>
      <div class="rowline">
        <input type="password" data-key="${key}" data-type="secret" value="" placeholder="${f.set ? "留空则保持不变" : "未设置"}" autocomplete="new-password" />
        ${f.set ? `<label class="clear"><input type="checkbox" data-clear="${key}" />清除</label>` : ""}
      </div>${hint}</div>`;
  }
  const type = f.type === "int" || f.type === "float" ? "number" : "text";
  let extra = f.type === "float" ? ' step="any"' : "";
  if (f.min != null) extra += ` min="${esc(f.min)}"`;
  if (f.max != null) extra += ` max="${esc(f.max)}"`;
  if (type === "number") extra += ' inputmode="decimal"';
  const input = `<input type="${type}"${extra}${ph} data-key="${key}" data-type="${esc(f.type)}" title="${esc(f.value ?? "")}" value="${esc(f.value ?? "")}" />`;
  const ctl = f.unit ? `<div class="with-unit">${input}<span class="unit">${esc(f.unit)}</span></div>` : input;
  return `<div class="f${f.inactive ? " inactive" : ""}"><label>${label}${tag}</label>${ctl}${hint}</div>`;
}

async function saveSettings() {
  const { values, clear } = collectValues();
  const btns = [$("#btn-save-settings"), $("#btn-save-settings-bottom")];
  btns.forEach(b => setBusy(b, true));
  try {
    const j = await post("/api/config", { values, clear });
    const msg = j.message || "已保存";
    const names = (j.changed || []).map(fieldLabel);
    $("#settings-msg").innerHTML = `<div class="hint ok">${esc(msg)}${names.length ? "：" + esc(names.join("、")) : "（没有修改）"}</div>`;
    toast(msg);
    S.cfgDirty = 0;
    loadConfig();
    refreshOverview();
  } catch (e) {
    $("#settings-msg").innerHTML = `<div class="hint err">保存失败：${esc(e.message)}</div>`;
    toast("保存失败：" + e.message);
  } finally {
    btns.forEach(b => setBusy(b, false));
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

/* ---------------- 事件绑定 ---------------- */
function bind() {
  $("#tab-overview").onclick = () => switchTab("overview");
  $("#tab-tasks").onclick = () => switchTab("tasks");
  $("#tab-settings").onclick = () => switchTab("settings");

  const showTheme = () => { const lab = $("#theme-label"); if (lab) lab.textContent = THEME_LABEL[curTheme()]; };
  showTheme();
  $("#btn-scan").onclick = (e) => act("/api/scan", "已触发扫描", e.currentTarget);
  $("#btn-retry").onclick = (e) => act("/api/retry-now", "已提前重试", e.currentTarget);
  $("#btn-requeue").onclick = (e) => act("/api/requeue", "已重新入队", e.currentTarget);
  $("#btn-sweep").onclick = async (e) => {
    const ok = await askConfirm({
      title: "核对同集？",
      body: "会在 115 里查找同一集的重复文件，只留最新的，旧的进回收站。",
      ok: "开始核对",
    });
    if (ok) act("/api/sweep", "已触发同集核对", e.currentTarget);
  };
  $("#btn-theme").onclick = () => cycleTheme();
  $("#btn-refresh").onclick = () => { S.lastFetch = Date.now(); refreshOverview(); if (S.tab === "tasks") loadTasks(); toast("已刷新"); };
  $("#btn-pause").onclick = (e) => act(S.paused ? "/api/resume" : "/api/pause", S.paused ? "已恢复调度" : "已暂停调度", e.currentTarget);
  $("#btn-logout").onclick = () => { location.href = "/logout"; };
  $("#btn-auto").onclick = () => cycleAuto();
  $("#btn-more").onclick = () => openMore();
  document.querySelectorAll(".dock-item").forEach(btn => {
    btn.onclick = () => switchTab(btn.dataset.tab);
  });
  $("#sheet-cancel").onclick = closeSheet;
  $("#sheet-backdrop").onclick = closeSheet;
  $("#sheet").addEventListener("click", (e) => {
    const b = e.target.closest("[data-id]");
    if (b && $("#sheet").contains(b)) onSheetClick(b.dataset.id);
  });
  $("#confirm-cancel").onclick = () => closeConfirm(false);
  $("#confirm-backdrop").onclick = () => closeConfirm(false);
  $("#confirm-ok").onclick = () => closeConfirm(true);

  // 任务页
  $("#t-refresh").onclick = () => { loadTasks(); refreshOverview(); toast("已刷新任务"); };
  $("#t-q").addEventListener("keydown", (e) => { if (e.key === "Enter") { S.tasks.q = e.target.value.trim(); S.tasks.page = 1; loadTasks(); } });
  $("#t-q").addEventListener("input", (e) => { S.tasks.q = e.target.value.trim(); });
  $("#t-prev").onclick = () => { if (S.tasks.page > 1) { S.tasks.page--; loadTasks(); } };
  $("#t-next").onclick = () => { S.tasks.page++; loadTasks(); };
  $("#d-close").onclick = closeDrawer;
  $("#drawer").onclick = (e) => { if (e.target.id === "drawer") closeDrawer(); };
  $("#d-upload").onclick = (e) => S.tasks.current && taskAction("upload-now", S.tasks.current, e.currentTarget);
  $("#d-requeue").onclick = (e) => S.tasks.current && taskAction("retry", S.tasks.current, e.currentTarget);
  $("#d-retrynow").onclick = (e) => S.tasks.current && taskAction("retry-now", S.tasks.current, e.currentTarget);
  $("#d-delete").onclick = async (e) => {
    if (!S.tasks.current) return;
    const ok = await askConfirm({
      title: "删除这条记录？",
      body: "只删记录，文件还在。下次扫描会重新入队。",
      ok: "删除记录",
    });
    if (ok) taskAction("delete", S.tasks.current, e.currentTarget);
  };

  // 设置页
  $("#btn-save-settings").onclick = saveSettings;
  $("#btn-save-settings-bottom").onclick = saveSettings;
  const reloadCfg = async () => {
    if (S.cfgDirty && !(await askConfirm({
      title: "放弃未保存的修改？",
      body: `有 ${S.cfgDirty} 项修改还没保存，重新读取会以配置文件为准。`,
      ok: "放弃并重新读取",
    }))) return;
    S.cfgDirty = 0;
    loadConfig();
    toast("已重新读取配置");
  };
  $("#btn-reload-config").onclick = reloadCfg;
  $("#btn-reload-config-bottom").onclick = reloadCfg;
  $("#btn-refresh-logs").onclick = refreshLogs;
  const groupsEl = $("#settings-groups");
  groupsEl.addEventListener("input", (e) => {
    if (e.target.matches("textarea[data-type=map]")) checkMap(e.target);
    updateDirty();
  });
  groupsEl.addEventListener("change", updateDirty);
  window.addEventListener("beforeunload", (e) => {
    if (!S.cfgDirty) return;
    e.preventDefault();
    e.returnValue = "";
  });
  document.addEventListener("click", (e) => {
    const opt = e.target.closest(".dd-opt");            // 选一项
    if (opt) {
      const dd = opt.closest(".dd");
      dd.dataset.value = opt.dataset.value;
      dd.querySelectorAll(".dd-opt").forEach(x => x.classList.toggle("on", x === opt));
      const lb = dd.querySelector(".dd-btn .lb");
      if (lb) lb.textContent = opt.dataset.label || "";
      dd.classList.remove("on");
      if (dd.id === "t-limit") { S.tasks.limit = Number(opt.dataset.value); S.tasks.page = 1; loadTasks(); }
      if (dd.dataset.key) updateDirty();
      return;
    }
    const ddBtn = e.target.closest(".dd-btn");          // 开/关面板
    if (ddBtn) {
      const dd = ddBtn.closest(".dd");
      const wasOn = dd.classList.contains("on");
      document.querySelectorAll(".dd.on").forEach(x => x.classList.remove("on"));
      if (!wasOn) openDd(dd);
      return;
    }
    if (!e.target.closest(".dd")) document.querySelectorAll(".dd.on").forEach(x => x.classList.remove("on"));

    const swLabel = e.target.closest("[data-switch]");
    if (swLabel) {
      // label 包着隐藏 checkbox：浏览器点 label 会自动翻一次 checked，
      // 我们再赋值就是翻两次（视觉开、值却是关）。挡掉原生激活，自己当唯一真相源。
      e.preventDefault();
      const sw = swLabel.querySelector(".switch");
      const box = swLabel.closest(".f").querySelector('input[type=checkbox][data-key]');
      const on = !(box && box.checked);
      if (box) box.checked = on;
      if (sw) sw.classList.toggle("on", on);
      const warn = swLabel.closest(".f").querySelector(".hintline.danger");
      if (warn) warn.hidden = !on;
      updateDirty();
    }
    const op = e.target.closest("button.primary");
    if (op) op.disabled = false;
  });
}

/* ---------------- 启动 ---------------- */
bind();
function openMore() {
  const items = [
    { id: "scan", label: "立即扫描", kind: "primary" },
    { id: "pause", label: S.paused ? "恢复调度" : "暂停调度" },
    { id: "refresh", label: "刷新数据" },
    { id: "retry", label: "立即重试秒传" },
    { id: "requeue", label: "重试失败项" },
    { id: "auto", label: S.auto ? ("自动刷新：" + (S.refreshMs / 1000) + " 秒") : "自动刷新：关" },
    { id: "theme", label: "主题：" + THEME_LABEL[curTheme()] },
  ];
  if ($("#btn-logout") && $("#btn-logout").style.display !== "none") {
    items.push({ id: "logout", label: ($("#logout-label") && $("#logout-label").textContent) || "退出登录" });
  }
  items.push({ id: "sweep", label: "核对同集（旧版进回收站）", kind: "danger" });
  openSheet(items, (id) => {
    const mapBtn = { scan: "#btn-scan", pause: "#btn-pause", retry: "#btn-retry", requeue: "#btn-requeue", sweep: "#btn-sweep" };
    if (id === "refresh") { $("#btn-refresh").click(); return; }
    if (id === "auto") { cycleAuto(); return; }
    if (id === "theme") { cycleTheme(); return; }
    if (id === "logout") { location.href = "/logout"; return; }
    if (mapBtn[id]) $(mapBtn[id]).click();
  }, "更多操作");
}

function syncKeyboardInset() {
  const vv = window.visualViewport;
  if (!vv) return;
  const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
  document.documentElement.style.setProperty("--kb", kb + "px");
  const ae = document.activeElement;
  if (kb > 40 && ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) {
    ae.scrollIntoView({ block: "center", behavior: "auto" });
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("#confirm") && !$("#confirm").hidden) { closeConfirm(false); return; }
  if ($("#sheet") && !$("#sheet").hidden) { closeSheet(); return; }
  if ($("#drawer") && $("#drawer").classList.contains("on")) { closeDrawer(); return; }
  document.querySelectorAll(".dd.on").forEach(x => x.classList.remove("on"));
});

renderLimitDd(S.tasks.limit);

const _startTab = (location.hash || "").replace("#", "");
switchTab(["overview", "tasks", "settings"].includes(_startTab) ? _startTab : "overview");
window.addEventListener("hashchange", () => {
  const t = (location.hash || "").replace("#", "");
  if (["overview", "tasks", "settings"].includes(t) && t !== S.tab) switchTab(t);
});
refreshOverview();
refreshLogs();
// 标签页切到后台就不再拉数据（每次 /api/status 都要跑一遍统计查询），切回来立刻刷新一次。
// 做法参考 https://github.com/z3nghoul/z3n1337.dev/blob/0d3c0d6bd4e84e7959e86869e7db464faa6026ba/js/poll.js（visibilitychange + document.hidden）
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.auto) S.lastFetch = 0;     // 下一个 1 秒节拍就会刷新
});
if (window.visualViewport) {
  visualViewport.addEventListener("resize", syncKeyboardInset);
  visualViewport.addEventListener("scroll", syncKeyboardInset);
}
setInterval(() => {
  if (!S.auto || document.hidden) return;
  if (Date.now() - S.lastFetch >= S.refreshMs) {
    S.lastFetch = Date.now();
    if (S.tab === "tasks") loadTasks();
    if (S.tab !== "settings") refreshOverview();
  } else if (S.tab === "overview" && S.overview) {
    renderOverview(S.overview);      // 用缓存重绘，倒计时/相对时间保持鲜活
  }
}, 1000);
setInterval(() => { if (S.tab === "settings" && !document.hidden) refreshLogs(); }, 15000);
