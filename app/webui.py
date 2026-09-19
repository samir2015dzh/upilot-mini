"""Web 界面：单页仪表盘 + JSON API（纯标准库，无外部依赖）。

GET  /              单页界面（app/static/index.html）
GET  /api/status    状态：统计、正在处理、最近活动、配置摘要
GET  /health        健康检查（供 Docker HEALTHCHECK 用）
POST /api/scan      立刻触发一轮扫描
POST /api/retry-now 把「待重试」的文件提前到期（马上再试秒传）
POST /api/requeue   把「错误」的文件重新入队
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("upilot.web")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def auth_cfg(cfg: dict) -> dict:
    return (cfg.get("http") or {}).get("auth") or {}


def check_auth(cfg: dict, headers, query: str) -> bool:
    """HTTP Basic 或 ?token=… 任一通过即放行；未开启鉴权（或没设密码）时恒真。

    /health 不走这里（Docker HEALTHCHECK 在容器内直连，不需要凭据）。
    """
    a = auth_cfg(cfg)
    pwd = str(a.get("password") or "")
    if not a.get("enabled") or not pwd:
        return True
    if parse_qs(query).get("token", [""])[0] == pwd:
        return True
    raw = headers.get("Authorization", "")
    if raw.startswith("Basic "):
        try:
            user, _, given = base64.b64decode(raw[6:]).decode("utf-8", "replace").partition(":")
        except Exception:  # noqa: BLE001
            return False
        want_user = str(a.get("username") or "")
        return hmac.compare_digest(given, pwd) and (not want_user or user == want_user)
    return False


def _payload(cfg: dict, store, state: dict) -> dict:
    up, rt, sc = cfg["upload"], cfg["retry"], cfg["scan"]
    stats = store.stats()
    return {
        "ok": True,
        "dry_run": bool(state.get("dry_run")),
        "paused": bool(state.get("paused")),
        "scan_rounds": state.get("scan_rounds", 0),
        "last_scan": state.get("last_scan"),
        "server_time": time.time(),
        "stats": stats,
        "queue": store.counts(),
        "active": store.active(50),
        "recent": store.recent(25),
        "config": {
            "source": cfg["paths"]["source"],
            "target_root": cfg["paths"]["target_root"],
            "scan_interval": sc["interval_seconds"],
            "stable_rounds": sc["stable_rounds"],
            "concurrency": up["concurrency"],
            "small_file_mb": up["small_file_mb"],
            "partsize_mb": up["partsize_mb"],
            "init_min_interval": up["init_min_interval"],
            "retry_interval": rt["interval_seconds"],
            "retry_max_count": rt["max_count"],
            "retry_max_hours": rt["max_hours"],
            "db": cfg["state"]["db"],
        },
    }


def serve(cfg: dict, store, state: dict) -> ThreadingHTTPServer | None:
    http_cfg = cfg.get("http") or {}
    if not http_cfg.get("enabled", True):
        return None
    index_file = os.path.join(STATIC_DIR, "index.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "upilot-mini"

        # ---- 工具 ----
        def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # ---- 鉴权 ----
        def _guard(self) -> bool:
            """开启 http.auth 后，所有页面与 API 都需要凭据。"""
            if check_auth(cfg, self.headers, urlparse(self.path).query):
                return True
            self._send(401, b"auth required", "text/plain",
                       extra={"WWW-Authenticate": 'Basic realm="UPilot-mini"'})
            return False

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _index(self) -> None:
            try:
                with open(index_file, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"index.html missing", "text/plain")

        # ---- 路由 ----
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            if path.startswith("/health"):             # 健康检查豁免：容器内 HEALTHCHECK 直连
                return self._send(200, b"ok", "text/plain")
            if not self._guard():
                return
            if path in ("/", "/index.html"):
                return self._index()
            if path in ("/api/status", "/status.json"):
                return self._json(_payload(cfg, store, state))
            if path == "/status":                      # 兼容老路径：给人看的 JSON
                return self._json(_payload(cfg, store, state))
            self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if not self._guard():
                return
            path = urlparse(self.path).path
            if path == "/api/scan":
                state["scan_now"] = True
                return self._json({"ok": True, "action": "scan", "message": "已触发扫描"})
            if path == "/api/retry-now":
                n = store.retry_now()
                state["scan_now"] = True
                return self._json({"ok": True, "action": "retry-now", "affected": n,
                                   "message": f"{n} 个待重试文件已提前到期"})
            if path == "/api/requeue":
                n = store.requeue()
                return self._json({"ok": True, "action": "requeue", "affected": n,
                                   "message": f"{n} 个失败项已重新入队"})
            if path in ("/api/pause", "/api/resume"):
                state["paused"] = path.endswith("pause")
                return self._json({
                    "ok": True, "action": path.rsplit("/", 1)[-1], "paused": state["paused"],
                    "message": "已暂停调度（在途任务继续跑完）" if state["paused"] else "已恢复调度",
                })
            self._send(404, b"not found", "text/plain")

        def log_message(self, *args):  # 静音访问日志
            return

    srv = ThreadingHTTPServer((http_cfg.get("host", "0.0.0.0"), int(http_cfg.get("port", 9844))), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a = auth_cfg(cfg)
    logger.info("界面已启动：http://<host>:%s/（鉴权：%s）", http_cfg.get("port", 9844),
                "已开启" if (a.get("enabled") and a.get("password")) else "关闭")
    return srv
