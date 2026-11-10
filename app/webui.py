"""Web 界面：单页仪表盘 + JSON API + 可选鉴权（纯标准库，无外部依赖）。

鉴权（http.auth）两种模式，由 http.auth.mode 决定：
  basic  默认。浏览器弹原生 Basic 框；脚本用 -u user:pass 或 ?token=pass
  form   站内登录页 /login，登录后下发签名 Cookie 会话；Basic 与 ?token= 仍
         保留为脚本通道（curl / 监控 / 反代探活不受影响）

GET  /               单页界面（app/static/index.html）
GET  /login          登录页（form 模式）
POST /login          提交登录（application/x-www-form-urlencoded 或 JSON）

所有 POST 都过 CSRF 检查（csrf_reject_reason）：浏览器发起的必须同源；
/api/* 的请求体只收 application/json。
GET  /logout         退出登录，清 Cookie
GET  /api/status     状态：统计、正在处理、最近活动、配置摘要
GET  /api/config     设置页表单结构（密钥字段只回脱敏状态）
POST /api/config     保存设置（写回 config.yaml + 更新内存配置 + 必要时重建客户端）
GET  /api/tasks      任务明细（status/q/limit/offset 翻页）
GET  /api/task       单个文件详情（?path=）
POST /api/task       单文件操作（retry / retry-now / upload-now / delete）
GET  /api/logs       最近运行日志（环形缓冲）
GET  /health         健康检查（供 Docker HEALTHCHECK 用，始终放行；主循环心跳超时回 503）
POST /api/scan       立刻触发一轮扫描
POST /api/retry-now  把「待重试」的文件提前到期（马上再试秒传）
POST /api/requeue    把「错误」的文件重新入队
POST /api/pause /api/resume   暂停 / 恢复调度
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import config as cfgmod

logger = logging.getLogger("upilot.web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# 最近日志环形缓冲：界面「运行日志」用，不落盘、重启即清
LOG_BUFFER: deque = deque(maxlen=400)
_buffer_attached = False

# 请求体硬上限。配置表单最大也就几十 KB，给到 4MB 绰绰有余。
# 没有上限时 `_body_fields()` 会按客户端**自己声明**的 Content-Length 去 read()：
# 实测声明 100MB、只发 3 字节，31 个连接就能让 31 个线程全部阻塞在 read 上
# （ThreadingHTTPServer 每请求一线程、线程数无上限），每个还预备那么大的缓冲。
MAX_BODY_BYTES = 4 * 1024 * 1024

# 单条连接的空闲/读超时（秒）。BaseHTTPRequestHandler 默认 timeout=None：
# 半开连接（发了请求头就不发 body）会**永久**占着一个线程。
# 注意 `/login` 在**校验任何凭据之前**就会读 body，所以未鉴权也能触发这条路径。
HANDLER_TIMEOUT = 30.0


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LOG_BUFFER.append({"t": record.created, "level": record.levelname,
                               "name": record.name, "msg": record.getMessage()})
        except Exception:  # noqa: BLE001
            pass


def attach_log_buffer(level: str = "INFO") -> None:
    """把环形缓冲挂到根 logger（幂等）。"""
    global _buffer_attached
    if _buffer_attached:
        return
    handler = _BufferHandler()
    handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    logging.getLogger().addHandler(handler)
    _buffer_attached = True

COOKIE_NAME = "upilot_session"
DEFAULT_SESSION_HOURS = 72

MAX_LOGIN_FAILURES = 5          # 同一来源连续失败次数上限
LOGIN_LOCK_SECONDS = 60         # 触顶后锁定时长

# 登录失败计数：{来源: [连续失败数, 解锁时间戳]}，仅内存，重启即清
_FAILURES: dict[str, list] = {}
_FAIL_LOCK = threading.Lock()


def _wake(state: dict) -> None:
    """叫醒主循环：让它立刻结束本轮等待，马上重读 state 并执行刚下的指令。

    为什么必须显式叫醒：主循环在等下一轮时是 `stop.wait()` 的分片轮询，它只认
    `scan_now` / `reload` 两个标志。而好几个界面动作只改 store、或只改 `paused`
    / `process_one`：
        /api/requeue  → 提示「N 个失败项已重新入队」
        /api/resume   → 提示「已恢复调度」
        /api/task upload-now → 提示「已跳过等待，立即处理」
    这些提示都写着“已…/立即”，但主循环要睡满一整个 interval 才会轮到它们。
    面板把 `scan.interval_seconds` 上限开放到 86400 秒（24 小时），用户按 hint
    调大之后，这几个按钮等于没点。判据：界面说生效、实际没生效。

    `wake_seq` 是给等待器用的单调计数：以后新增界面动作只要调本函数，就不必
    再去等值器的白名单里补标志名（第 22、23 处就是这么漏掉的）。
    """
    state["wake_seq"] = int(state.get("wake_seq") or 0) + 1
    state["scan_now"] = True


# ---------- 配置读取 ----------
def auth_cfg(cfg: dict) -> dict:
    return (cfg.get("http") or {}).get("auth") or {}


def _enabled(a: dict) -> bool:
    return bool(a.get("enabled")) and bool(str(a.get("password") or ""))


def _password(a: dict) -> str:
    return str(a.get("password") or "")


def _username(a: dict) -> str:
    return str(a.get("username") or "")


def _mode(a: dict) -> str:
    mode = str(a.get("mode") or "basic").strip().lower()
    return mode if mode in ("basic", "form") else "basic"


def _session_hours(a: dict) -> int:
    try:
        return max(1, int(a.get("session_hours", DEFAULT_SESSION_HOURS)))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_HOURS


# ---------- 会话签名（HMAC-SHA256，密钥取自 http.auth.password） ----------
def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_session(user: str, secret: str, hours: int = DEFAULT_SESSION_HOURS,
                 now: float | None = None) -> str:
    exp = int((now if now is not None else time.time()) + hours * 3600)
    payload = f"{user}|{exp}"
    body = base64.urlsafe_b64encode(payload.encode("utf-8")).decode().rstrip("=")
    return f"{body}.{_sign(secret, payload)}"


def parse_session(token: str, secret: str) -> str | None:
    """校验 Cookie 值，通过则返回用户名，否则 None。"""
    if not token or "." not in token:
        return None
    try:
        body, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
        user, _, exp = payload.rpartition("|")
        if int(exp) < time.time():
            return None
        if not hmac.compare_digest(sig, _sign(secret, payload)):
            return None
        return user
    except Exception:  # noqa: BLE001
        return None


def parse_cookie(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k:
            out[k] = v
    return out


# ---------- 凭据校验 ----------
def _secure_eq(a, b) -> bool:
    """常数时间比较两个值（按 UTF-8 编码后比字节）。

    为什么必须编码：`hmac.compare_digest()` 在 **str** 上只接受纯 ASCII，
    遇到中文/emoji 直接抛
    `TypeError: comparing strings with non-ASCII characters is not supported`。
    而 `check_credentials` 的两处调用点（`check_auth` 的 Basic 分支、`_do_login`）
    **都不在 try 块里** —— 实测：面板把密码设成含中文后，Basic 通道每次请求都在
    这里抛 TypeError，socketserver 兜住并打一整段 traceback，客户端拿到的是
    **连接断开**（不是 401、也不是 500，界面表现为「一直转圈」）；
    而 Cookie 通道与 `?token=` 通道却是好的。同一份凭据「一条路能进、
    一条路进不去」，这是最难查的形态。
    编成 bytes 后 compare_digest 接受任意内容，且仍是常数时间比较。

    同时用于替换 `?token=` 那条的 `==` —— `==` 一旦发现首个不同字节就返回，
    耗时与「匹配了多少前缀」相关，构成可被逐字节探测的时序侧信道
    （实测 `==` 在 40 字符密码上耗时随匹配前缀波动，而 compare_digest 稳定）。
    """
    try:
        return hmac.compare_digest(str(a).encode("utf-8"), str(b).encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


def check_credentials(a: dict, user: str, pwd: str) -> bool:
    want_user = _username(a)
    if not _secure_eq(pwd, _password(a)):
        return False
    return (not want_user) or _secure_eq(user, want_user)


def check_auth(cfg: dict, headers, query: str) -> bool:
    """三条通道任一通过即放行：签名 Cookie / ?token=密码 / HTTP Basic。

    未开启鉴权（或没设密码）时恒真；/health 不走这里。
    """
    a = auth_cfg(cfg)
    if not _enabled(a):
        return True
    pwd = _password(a)
    if _secure_eq(parse_qs(query).get("token", [""])[0], pwd):
        return True
    # 必须用 `is not None` 判断：parse_session 用「返回用户名」表示通过，
    # 而用户名可以是**空串**（面板把「用户名」清空，或用户登录时不填）。
    # 写成 `if parse_session(...):` 时 bool('') 为 False → 签名完全正确的会话
    # 被判成「没登录」，浏览器带着刚发的 cookie 又被弹回 /login，无限循环
    # （POST /login 返回 302 + Set-Cookie，日志还打「登录成功」）。
    if parse_session(parse_cookie(headers.get("Cookie", "")).get(COOKIE_NAME, ""), pwd) is not None:
        return True
    raw = headers.get("Authorization", "")
    if raw.startswith("Basic "):
        try:
            user, _, given = base64.b64decode(raw[6:]).decode("utf-8", "replace").partition(":")
        except Exception:  # noqa: BLE001
            return False
        return check_credentials(a, user, given)
    return False


def _safe_next(raw: str) -> str:
    """只允许同源相对路径，挡掉 //evil.com 这类开放跳转。"""
    val = (raw or "").strip()
    if val.startswith("/") and not val.startswith("//") and "\r" not in val and "\n" not in val:
        return val
    return "/"


def _rate_limited(ip: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    with _FAIL_LOCK:
        rec = _FAILURES.get(ip)
        if not rec:
            return 0
        if rec[1] > time.time():
            return int(rec[1] - time.time()) + 1
        return 0


def _note_failure(ip: str) -> None:
    with _FAIL_LOCK:
        rec = _FAILURES.setdefault(ip, [0, 0.0])
        rec[0] += 1
        if rec[0] >= MAX_LOGIN_FAILURES:
            rec[1] = time.time() + LOGIN_LOCK_SECONDS
            rec[0] = 0


def _note_success(ip: str) -> None:
    with _FAIL_LOCK:
        _FAILURES.pop(ip, None)


def health_status(cfg: dict, state: dict, now: float | None = None) -> tuple[bool, str]:
    """(是否健康, 说明)。HTTP 线程活着不代表在干活：主循环心跳（main.beat）超过
    http.health_stall_seconds 没更新就判不健康。还没打过心跳（启动中 / 单独起 webui
    的测试）按健康处理。"""
    hb = state.get("heartbeat")
    if hb is None:
        return True, "ok"
    try:
        limit = float((cfg.get("http") or {}).get("health_stall_seconds") or 600)
    except (TypeError, ValueError):
        limit = 600.0
    age = (time.monotonic() if now is None else now) - float(hb)
    if age > max(30.0, limit):
        return False, f"stalled: main loop heartbeat {int(age)}s ago (limit {int(limit)}s)"
    return True, "ok"


# 响应压缩：/api/status 2 万条记录时 44 KB、gzip 后 1 KB，面板每 5 秒拉一次。
# 判断方式参考 https://github.com/epheterson/Zimi/blob/045ac9d7444b1a2e53d4ddabe80be27892a9a9b5/zimi/http.py 的 _maybe_gzip
# （够大、类型可压缩、客户端接受才压）；这里额外处理了 q=0（显式拒绝 gzip）。
GZIP_MIN_BYTES = 1024
GZIP_LEVEL = 5            # 1 KB 级的 JSON 压到 6 和 9 几乎没区别，5 更省 CPU
_COMPRESSIBLE = ("application/json", "text/", "application/javascript")


def accepts_gzip(header: str | None) -> bool:
    """Accept-Encoding 里有 gzip（或 *）且 q 不为 0。"""
    for part in str(header or "").split(","):
        name, _, params = part.strip().partition(";")
        if name.strip().lower() not in ("gzip", "*"):
            continue
        q = 1.0
        for p in params.split(";"):
            k, _, v = p.strip().partition("=")
            if k.strip().lower() == "q":
                try:
                    q = float(v)
                except ValueError:
                    q = 0.0
        return q > 0
    return False


def maybe_gzip(body: bytes, ctype: str, accept: str | None) -> tuple[bytes, bool]:
    if (len(body) >= GZIP_MIN_BYTES and ctype.startswith(_COMPRESSIBLE)
            and accepts_gzip(accept)):
        return gzip.compress(body, compresslevel=GZIP_LEVEL), True
    return body, False


def _auth_failed() -> dict | None:
    """115 登录失效断路器的状态（uploader.auth_status）；取不到就当没有。"""
    try:
        from uploader import auth_status
        return auth_status()
    except Exception:  # noqa: BLE001
        return None


def _replacements() -> list[dict]:
    """最近的洗板替换记录（uploader 里记的，最新在前）；取不到就当没有。"""
    try:
        from uploader import _SHARED
        return list(_SHARED["replaced"])
    except Exception:  # noqa: BLE001
        return []


def _payload(cfg: dict, store, state: dict) -> dict:
    up, rt, sc = cfg["upload"], cfg["retry"], cfg["scan"]
    a = auth_cfg(cfg)
    stats = store.stats()
    return {
        "ok": True,
        "dry_run": bool(state.get("dry_run")),
        "paused": bool(state.get("paused")),
        "auth_failed": _auth_failed(),
        "replacements": _replacements(),
        "scan_rounds": state.get("scan_rounds", 0),
        "last_scan": state.get("last_scan"),
        "server_time": time.time(),
        "stats": stats,
        "queue": store.counts(),
        "active": store.active(50),
        "recent": store.recent(25),
        "auth": {
            "enabled": _enabled(a),
            "mode": _mode(a),
            "user": _username(a) if _enabled(a) else "",
        },
        "config": {
            "source": cfg["paths"]["source"],
            "target_root": cfg["paths"]["target_root"],
            "scan_interval": sc["interval_seconds"],
            "stable_rounds": sc["stable_rounds"],
            "concurrency": up["concurrency"],
            "small_file_mb": up["small_file_mb"],
            "upload_mode": cfgmod.normalize_upload_mode(up.get("mode")),
            "upload_mode_label": cfgmod.UPLOAD_MODE_LABELS.get(
                cfgmod.normalize_upload_mode(up.get("mode")), ""),
            "partsize_mb": up["partsize_mb"],
            "init_min_interval": up["init_min_interval"],
            "retry_interval": rt["interval_seconds"],
            "retry_max_count": rt["max_count"],
            "retry_max_hours": rt["max_hours"],
            "retry_long_interval": rt.get("long_interval_seconds") or 900,
            "notify_enabled": bool((cfg.get("notify") or {}).get("enabled")),
            "notify_url": (cfg.get("notify") or {}).get("url") or "",
            "db": cfg["state"]["db"],
        },
    }


def _fill_login(tpl: str, next_url: str, error: str, user: str, site: str) -> bytes:
    def esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;").replace("'", "&#39;"))
    err_html = ""
    if error:
        err_html = ('<div class="err"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                    'stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/>'
                    '<path d="M12 8v5"/><path d="M12 16h.01"/></svg><span>'
                    + esc(error) + "</span></div>")
    site_html = f'<div class="site">{esc(site)}</div>' if site else ""
    html = (tpl.replace("{{ERROR}}", err_html)
               .replace("{{SITE_BLOCK}}", site_html)
               .replace("{{SITE}}", esc(site))
               .replace("{{NEXT}}", esc(next_url))
               .replace("{{USER}}", esc(user)))
    return html.encode("utf-8")


def _origin_of(url: str) -> str:
    """'https://a.b:8443/x?y' → 'https://a.b:8443'；解析不了返回空串。"""
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.netloc:
        return ""
    return f"{u.scheme}://{u.netloc}".lower()


def _allowed_origins(cfg: dict, headers) -> set[str]:
    """本服务自己的源：请求的 Host、反代转发的 X-Forwarded-Host、配置的 public_url。"""
    out: set[str] = set()
    for raw in (headers.get("Host"), (headers.get("X-Forwarded-Host") or "").split(",")[0]):
        host = str(raw or "").strip().lower()
        if host:
            out.update({f"http://{host}", f"https://{host}"})
    site = _origin_of((cfg.get("http") or {}).get("public_url") or "")
    if site:
        out.add(site)
    return out


def csrf_reject_reason(cfg: dict, headers, path: str, has_body: bool) -> str:
    """改状态的 POST 是否像跨站伪造（CSRF）。放行返回空串，否则返回拒绝原因。

    默认不开鉴权、basic 模式下浏览器又会自动带上缓存的凭据，所以任何网页都能
    让本机浏览器往面板 POST 一个表单（比如打开「传完删本地」）。三道判断：
      1) 浏览器给的 `Sec-Fetch-Site`（网页伪造不了）：只认 same-origin / none；
         反代把 Host 改写了也不影响这一条。
      2) 老浏览器没有 Sec-Fetch-Site 时看 `Origin`，再没有就看 `Referer`：
         必须是本服务自己的源（Host / X-Forwarded-Host / http.public_url）。
      3) 三个头都没有 ⇒ 不是浏览器发起的（curl / 监控脚本），照旧放行。
    另外 `/api/*` 带请求体时只收 `application/json`：跨站表单发不出 JSON，
    想发就得过 CORS 预检，而本服务不回 CORS 头。自带的面板本来就全用 JSON。
    """
    sfs = str(headers.get("Sec-Fetch-Site") or "").strip().lower()
    if sfs:
        if sfs not in ("same-origin", "none"):
            return f"拒绝跨站请求（Sec-Fetch-Site={sfs}）"
    else:
        origin = str(headers.get("Origin") or "").strip()
        referer = str(headers.get("Referer") or "").strip()
        if origin:
            if origin.lower() == "null" or _origin_of(origin) not in _allowed_origins(cfg, headers):
                return f"拒绝跨站请求（Origin={origin[:100]}）"
        elif referer:
            if _origin_of(referer) not in _allowed_origins(cfg, headers):
                return "拒绝跨站请求（Referer 不是本站）"
    if path.startswith("/api/") and has_body:
        ctype = str(headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            return "接口只接受 application/json 请求体"
    return ""


def serve(cfg: dict, store, state: dict) -> ThreadingHTTPServer | None:
    http_cfg = cfg.get("http") or {}
    if not http_cfg.get("enabled", True):
        return None
    attach_log_buffer(str(cfg.get("log_level", "INFO")))

    index_file = os.path.join(STATIC_DIR, "index.html")
    login_file = os.path.join(STATIC_DIR, "login.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "upilot-mini"
        # 半开连接不会永久占着线程（见 HANDLER_TIMEOUT 的说明）
        timeout = HANDLER_TIMEOUT

        # ---- 工具 ----
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> bool:
            try:
                body, zipped = maybe_gzip(body, ctype, self.headers.get("Accept-Encoding"))
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Vary", "Accept-Encoding")
                if zipped:
                    self.send_header("Content-Encoding", "gzip")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError) as exc:
                # 客户端半路断开 / 读超时都属正常（浏览器刷新、健康检查掐连接、
                # curl -m 超时退出）。原来只 catch 前两个，而 Windows 上断开抛的是
                # ConnectionAbortedError(WinError 10053) —— 实测 30 个早断连接就刷了
                # 30 段 traceback 到日志里，把真正的错误淹掉。
                logger.debug("应答中断（%s）：%s", type(exc).__name__, exc)
            return False

        def _json(self, obj, code: int = 200, extra: dict | None = None) -> bool:
            return self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                              "application/json; charset=utf-8", extra=extra)

        def _redirect(self, target: str, extra: dict | None = None) -> bool:
            return self._send(302, b"", "text/plain", extra=dict(extra or {}, Location=target))

        def _read_file(self, path: str) -> str:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()

        def _cookie_header(self, token: str, max_age: int) -> str:
            return (f"{COOKIE_NAME}={token}; Path=/; Max-Age={max_age}; "
                    "HttpOnly; SameSite=Lax")

        # ---- 鉴权 ----
        def _client_ip(self) -> str:
            """取用于限速的来源标识。

            X-Real-IP / X-Forwarded-For 是**客户端可以随便伪造**的。无条件信任它们，
            等于每次请求换一个值就换一个限速 key —— 失败计数永远攒不到上限，
            登录限速形同虚设（实测同一来源换 12 次头、一次都没被锁）。而限速是本项目
            唯一的登录爆破防护。所以只有在「确实挂在反代后面」时才读这两个头：
            http.auth.trust_forwarded_ip = true（面板里一项，默认关）。
            默认直接用 TCP 对端地址，伪造头完全无效。
            """
            a = auth_cfg(cfg)
            if a.get("trust_forwarded_ip"):
                fwd = self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For") or ""
                if fwd:
                    return fwd.split(",")[0].strip()[:64]
            return self.client_address[0] if self.client_address else "?"

        def _guard(self) -> bool:
            """放行返回 True；否则自行应答并返回 False。"""
            a = auth_cfg(cfg)
            if check_auth(cfg, self.headers, urlparse(self.path).query):
                return True
            path = urlparse(self.path).path
            if _mode(a) == "form":
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": "unauthorized",
                                       "mode": "form", "login": "/login"}, 401)
                return self._redirect("/login?next=" + quote(self.path or "/", safe=""))
            return self._send(401, b"auth required", "text/plain",
                              extra={"WWW-Authenticate": 'Basic realm="UPilot-mini"'})

        # ---- 页面 ----
        def _static(self, name: str) -> bool:
            """只放行白名单里的静态资源，避免目录穿越。"""
            if name not in ("app.css", "app.js", "index.html", "login.html"):
                return self._send(404, b"not found", "text/plain")
            ctype = {"css": "text/css", "js": "application/javascript"}.get(
                name.rsplit(".", 1)[-1], "text/html")
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as fh:
                    return self._send(200, fh.read(), ctype + "; charset=utf-8")
            except OSError:
                return self._send(404, b"missing", "text/plain")

        def _index(self) -> bool:
            try:
                return self._send(200, self._read_file(index_file).encode("utf-8"),
                                  "text/html; charset=utf-8")
            except OSError:
                return self._send(500, b"index.html missing", "text/plain")

        def _login_page(self, next_url: str = "/", error: str = "", user: str = "",
                        code: int = 200) -> bool:
            try:
                tpl = self._read_file(login_file)
            except OSError:
                return self._send(500, b"login.html missing", "text/plain")
            site = (cfg.get("http") or {}).get("public_url") or ""
            body = _fill_login(tpl, _safe_next(next_url), error, user, site)
            return self._send(code, body, "text/html; charset=utf-8")

        # ---- 路由 ----
        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            path, query = parsed.path, parsed.query
            if path.startswith("/health"):             # 健康检查豁免：容器内 HEALTHCHECK 直连
                ok, why = health_status(cfg, state)
                return self._send(200 if ok else 503, why.encode(), "text/plain")
            a = auth_cfg(cfg)
            if _mode(a) == "form" and _enabled(a):
                if path == "/login":
                    if check_auth(cfg, self.headers, query):
                        return self._redirect(_safe_next(parse_qs(query).get("next", ["/"])[0]))
                    return self._login_page(parse_qs(query).get("next", ["/"])[0])
                if path == "/logout":
                    return self._redirect("/login", extra={
                        "Set-Cookie": self._cookie_header("", 0)})
            if not self._guard():
                return
            if path in ("/", "/index.html"):
                return self._index()
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path in ("/api/status", "/status.json", "/status"):
                return self._json(_payload(cfg, store, state))
            if path == "/api/config":
                return self._json({"ok": True, **cfgmod.form_payload(cfg)})
            if path == "/api/tasks":
                qs = parse_qs(query)
                try:
                    limit = max(1, min(500, int(qs.get("limit", ["50"])[0] or 50)))
                    offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
                except ValueError:
                    limit, offset = 50, 0
                rows, total = store.list_tasks(status=qs.get("status", ["全部"])[0],
                                               q=qs.get("q", [""])[0],
                                               limit=limit, offset=offset)
                return self._json({"ok": True, "total": total, "rows": rows,
                                   "limit": limit, "offset": offset})
            if path == "/api/task":
                one = store.get(parse_qs(query).get("path", [""])[0])
                if not one:
                    return self._json({"ok": False, "error": "没找到这个文件记录"}, 404)
                return self._json({"ok": True, "task": one})
            if path == "/api/logs":
                try:
                    n = int(parse_qs(query).get("limit", ["200"])[0] or 200)
                except ValueError:
                    n = 200
                return self._json({"ok": True, "logs": list(LOG_BUFFER)[-max(1, min(400, n)):]})
            self._send(404, b"not found", "text/plain")

        def _body_fields(self) -> dict:
            """解析表单或 JSON 请求体，返回字段字典。

            两道保护（原来是裸 `self.rfile.read(Content-Length)`）：
            1) **长度上限** MAX_BODY_BYTES —— length 是客户端自己声明的，不设上限时
               声明 100MB 就能让服务端照单预备缓冲；实测 31 个「声明 100MB 只发 3 字节」
               的连接把 31 个线程全卡在 read 上（每请求一线程、线程数无上限）。
            2) **读不完整要兜住** —— 半开连接会在这里读到 socket 超时（Handler.timeout）；
               原来没 catch，异常直接冒到 socketserver 的线程里刷 traceback。
            另外 `POST /login` 在**校验凭据之前**就调本函数，所以这条路必须自己扛住。
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY_BYTES:
                # 不读、也不预分配：直接断开这条连接（HTTP/1.0 单请求，不会错位后续）
                logger.warning("请求体 %d 字节超过上限 %d，已拒绝：%s",
                               length, MAX_BODY_BYTES, self.command)
                self.close_connection = True
                return {}
            try:
                raw = self.rfile.read(length)
            except (TimeoutError, OSError) as exc:
                # 半开连接 / 客户端中途断开
                logger.debug("读取请求体失败（%s）：%s", type(exc).__name__, exc)
                self.close_connection = True
                return {}
            if len(raw) < length:
                # 提前断开导致只读到一半：宁可当空处理，也别拿半截内容去解析
                logger.debug("请求体只读到 %d/%d 字节，按空处理", len(raw), length)
                self.close_connection = True
                return {}
            text = raw.decode("utf-8", "replace")
            if "application/json" in (self.headers.get("Content-Type") or ""):
                try:
                    obj = json.loads(text or "{}")
                except ValueError:
                    obj = {}
                return obj if isinstance(obj, dict) else {}      # JSON 保留原始类型（嵌套对象要原样收）
            return {k: v[0] for k, v in parse_qs(text, keep_blank_values=True).items()}

        def _do_login(self, parsed) -> bool:
            a = auth_cfg(cfg)
            ip = self._client_ip()
            left = _rate_limited(ip)
            if left:
                q = parse_qs(parsed.query).get("next", ["/"])[0] or "/"
                return self._login_page(_safe_next(q), f"尝试次数过多，请 {left} 秒后再试", code=429)
            fields = self._body_fields()
            nxt = _safe_next(fields.get("next") or parse_qs(parsed.query).get("next", ["/"])[0])
            user = str(fields.get("username") or "")
            pwd = str(fields.get("password") or "")
            if check_credentials(a, user, pwd):
                _note_success(ip)
                # 会话里的 user 必须**非空**：面板把「用户名」留空时，登录页通常
                # 也不会填用户名，这里就会把空串写进 token。虽然 check_auth 现在
                # 用 `is not None` 判定了，存个非空标识更稳（用户名只用来展示，
                # 鉴权靠签名）。
                token = make_session(user or _username(a) or "user", _password(a), _session_hours(a))
                logger.info("登录成功：%s（来源 %s）", user, ip)
                return self._redirect(nxt, extra={
                    "Set-Cookie": self._cookie_header(token, _session_hours(a) * 3600)})
            _note_failure(ip)
            logger.warning("登录失败：%s（来源 %s）", user or "-", ip)
            return self._login_page(nxt, "用户名或密码不对", user, code=401)

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            a = auth_cfg(cfg)
            try:
                has_body = int(self.headers.get("Content-Length") or 0) > 0
            except ValueError:
                has_body = False
            why = csrf_reject_reason(cfg, self.headers, path, has_body)
            if why:
                logger.warning("拦截可疑 POST %s：%s（来源 %s）", path, why, self._client_ip())
                self.close_connection = True          # 请求体没读，别让它错位到下一个请求
                if path.startswith("/api/"):
                    return self._json({"ok": False, "error": why}, 403)
                return self._send(403, why.encode("utf-8"), "text/plain; charset=utf-8")
            if path == "/login" and _mode(a) == "form" and _enabled(a):
                return self._do_login(parsed)
            if not self._guard():
                return
            if path == "/api/config":
                body = self._body_fields()
                values = body.get("values")
                if isinstance(values, str):
                    try:
                        values = json.loads(values)
                    except ValueError:
                        values = {}
                clear = body.get("clear") or body.get("clear_secrets") or []
                if isinstance(clear, str):
                    clear = [x for x in re.split(r"[,\s]+", clear) if x]
                if not isinstance(values, dict):
                    return self._json({"ok": False, "error": "values 需要是对象"}, 400)
                try:
                    result = cfgmod.apply_values(cfg, values, clear)
                except ValueError as exc:
                    return self._json({"ok": False, "error": str(exc)}, 400)
                cfgmod.save(cfg)
                if result["reload"]:
                    state["reload"] = True
                # 保存后立刻叫醒主循环：一是 reload 项要尽快重建 115 客户端，
                # 二是 scan.interval_seconds 变小之后，等待期还在按旧的长间隔睡。
                _wake(state)
                logger.info("界面保存配置：%s", ", ".join(result["changed"]) or "无变化")
                tips = []
                if result["reload"]:
                    tips.append("115 客户端会立即重建")
                if result["restart"]:
                    tips.append("监听地址/端口或日志级别要重建容器才生效")
                return self._json({"ok": True, "changed": result["changed"],
                                   "reload": result["reload"], "restart": result["restart"],
                                   "message": "已保存" + ("；" + "；".join(tips) if tips else "")})
            if path == "/api/task":
                body = self._body_fields()
                target = str(body.get("path") or "")
                action = str(body.get("action") or "")
                if not target or not store.get(target):
                    return self._json({"ok": False, "error": "没找到这个文件记录"}, 404)
                if action == "retry":
                    n = store.reset_one(target)
                    _wake(state)
                    msg = "已重新排队，马上再试"
                elif action == "retry-now":
                    store.set(target, next_retry_at=0)
                    n, msg = 1, "已提前到期，马上重试秒传"
                    _wake(state)
                elif action == "upload-now":
                    if (cfg["upload"].get("mode") or "") == "rapid_only":
                        store.set(target, next_retry_at=0)
                        n, msg = 1, "纯秒传模式：立即重试秒传（不会普通上传）"
                    else:
                        n = store.mark_ready(target)
                        msg = "已跳过等待，立即处理"
                    state["process_one"] = target
                    # 光置 process_one 唤不醒主循环（它只认 scan_now/reload/wake_seq），
                    # 而提示写着「立即处理」—— 必须显式叫醒。
                    _wake(state)
                elif action == "delete":
                    n = store.forget(target)
                    msg = "记录已删除（文件本体没动，下次扫描会重新入队）"
                else:
                    return self._json({"ok": False, "error": f"不认识的动作：{action}"}, 400)
                logger.info("界面操作 %s → %s", action, target)
                return self._json({"ok": True, "affected": n, "message": msg})
            if path == "/api/scan":
                _wake(state)
                state["sweep_now"] = True       # 顺手核一遍云端同集重复（用户诉求：传完去校验）
                return self._json({"ok": True, "action": "scan", "message": "已触发扫描 + 同集核对"})
            if path == "/api/sweep":
                # 用户诉求（原话）：「上传到 115 以后校验 115 的那个文件夹里面有没有相同集数的视频」。
                # 只读不改库的入口：立刻去云端核对同集重复，把旧版移入 115 回收站。
                state["sweep_now"] = True
                _wake(state)
                return self._json({"ok": True, "action": "sweep",
                                   "message": "已触发同集核对：云端同集只留最新那版"})
            if path == "/api/retry-now":
                n = store.retry_now()
                _wake(state)
                return self._json({"ok": True, "action": "retry-now", "affected": n,
                                   "message": f"{n} 个待重试文件已提前到期"})
            if path == "/api/requeue":
                n = store.requeue()
                # 只改 store 不动 state：主循环要睡满整个 interval 才会重新派发，
                # 而面板把 scan.interval_seconds 上限开放到 86400 秒（24 小时），
                # 提示却写着「已重新入队」。
                _wake(state)
                return self._json({"ok": True, "action": "requeue", "affected": n,
                                   "message": f"{n} 个失败项已重新入队"})
            if path in ("/api/pause", "/api/resume"):
                state["paused"] = path.endswith("pause")
                # 恢复调度要立刻生效：主循环只在轮次开头读 paused，
                # 不叫醒的话「已恢复调度」要等满一个间隔才真的恢复。
                _wake(state)
                return self._json({
                    "ok": True, "action": path.rsplit("/", 1)[-1], "paused": state["paused"],
                    "message": "已暂停调度（在途任务继续跑完）" if state["paused"] else "已恢复调度",
                })
            if path == "/logout":
                return self._redirect("/login", extra={"Set-Cookie": self._cookie_header("", 0)})
            self._send(404, b"not found", "text/plain")

        def log_message(self, *args):  # 静音访问日志
            return

    srv = ThreadingHTTPServer((http_cfg.get("host", "0.0.0.0"), int(http_cfg.get("port", 9844))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # 记下**实际绑定**的端口（端口写 0 时是内核分配的真实值），供健康检查兜底：
    # 面板上改了 http.port 但还没重建容器时，真正在跑的还是这个端口。
    try:
        cfgmod.PORT_FILE.write_text(str(srv.server_address[1]), encoding="utf-8")
    except OSError:
        pass
    a = auth_cfg(cfg)
    state_desc = "关闭"
    if _enabled(a):
        state_desc = "站内登录页（form）" if _mode(a) == "form" else "HTTP Basic"
    logger.info("界面已启动：http://<host>:%s/（鉴权：%s）", http_cfg.get("port", 9844), state_desc)
    return srv
