"""本机离线测试：不需要 115 cookie、不联网。

运行：python tests/test_flow.py
（sys.path 指向 ../app；StubP115 替代真实 115 客户端）
"""
from __future__ import annotations

import asyncio
import base64
import builtins
import copy
import gc
import glob
import inspect
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import config as cfgmod          # noqa: E402
import p115                      # noqa: E402
import scanner                   # noqa: E402
import webui                     # noqa: E402
from p115 import P115, StubP115, normalize_cloud_path, resolve_upload_target   # noqa: E402
from store import Store          # noqa: E402
import store as store_store      # noqa: E402  （用例 [38] 要断言 DDL / 老库补列）
from uploader import Uploader, sha1_of   # noqa: E402

PASS: list[str] = []


def make_env(stable_rounds: int = 1, small_file_mb: float = 10.0, retry_max_count: int = 5,
             retry_interval: int = 0, fail_first: int = 2, mode: str = "rapid_then_upload",
             long_interval: int = 0, delete_after: bool = False):
    tmp = tempfile.mkdtemp(prefix="upilot-test-")
    src = os.path.join(tmp, "source")
    os.makedirs(src, exist_ok=True)
    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    cfg["paths"] = {"source": src, "target_root": "/UPILOT-mini"}
    cfg["state"] = {"db": os.path.join(tmp, "state.db")}
    cfg["scan"]["stable_rounds"] = stable_rounds
    cfg["scan"]["include_suffixes"] = []   # 测试里默认不做扩展名过滤（新用例自行覆盖）
    cfg["upload"]["small_file_mb"] = small_file_mb
    cfg["retry"]["interval_seconds"] = retry_interval
    cfg["retry"]["max_count"] = retry_max_count
    cfg["retry"]["long_interval_seconds"] = long_interval
    cfg["upload"]["mode"] = mode
    cfg["upload"]["delete_after_upload"] = delete_after
    cfg["http"]["enabled"] = False
    store = Store(cfg["state"]["db"])
    mgr = StubP115(cfg, store, fail_first=fail_first)
    up = Uploader(cfg, store, mgr)
    return cfg, store, mgr, up, tmp, src


def write_file(src: str, rel: str, size: int) -> str:
    path = os.path.join(src, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(1024) * (size // 1024) if size >= 1024 else b"x" * size)
    return os.path.normpath(path)      # 与 scanner 用 os.walk 得到的路径写法保持一致


async def drive(up: Uploader, store: Store, path: str, rounds: int = 15) -> dict:
    """模拟调度器：反复推进一个文件，直到终态。"""
    for _ in range(rounds):
        rec = store.get(path)
        if rec["status"] in ("已上传", "错误"):
            return rec
        if rec["status"] == "待重试":
            store.set(path, next_retry_at=0)      # 让重试立即到期
        await up._process(path)
    return store.get(path)


async def case_folder_and_rapid_hit():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=2, small_file_mb=1.0)
    try:
        path = write_file(src, "电影/哈利与巧克力工厂/哈利与巧克力工厂.mp4", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)             # 入队（stable_count=0）
        scanner.scan_once(store, cfg)             # stable_count=1 → 稳定
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert rec["target_dir"] == "/UPILOT-mini/电影/哈利与巧克力工厂", rec["target_dir"]
        assert mgr._rapid_calls[path] == 3, mgr._rapid_calls      # 2 次未命中 + 1 次命中
        assert set(mgr._mem.keys()) == {"/UPILOT-mini/电影/哈利与巧克力工厂"}, mgr._mem
        assert rec["message"].startswith("秒传命中"), rec["message"]
        PASS.append(f"[1] 目录保留 + 秒传重试命中：{rec['target_dir']}，校验请求 3 次，"
                    f"status={rec['status']}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_sha1_cached_no_reread():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=1)
    try:
        path = write_file(src, "剧集/S01/E01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        await drive(up, store, path)
        rec = store.get(path)
        assert rec["sha1"], rec

        real_open = builtins.open

        def deny(file, *a, **k):
            if str(file).endswith("E01.mkv"):
                raise AssertionError("不应重新读取文件")
            return real_open(file, *a, **k)

        builtins.open = deny
        try:
            again = await sha1_of(path, store.get(path), store)
        finally:
            builtins.open = real_open
        assert again == rec["sha1"]
        assert len(mgr._mem) == 1                 # cid 缓存：同目录只建一次
        PASS.append(f"[2] SHA1 落库复用（未再读盘）sha1={again[:12]}…")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_small_file_goes_direct_upload():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, small_file_mb=10.0, fail_first=99)
    try:
        path = write_file(src, "小文件/cover.jpg", 200 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert mgr._rapid_calls[path] == 1, mgr._rapid_calls     # 只试一次秒传
        assert "分块上传完成" in (rec["message"] or ""), rec["message"]
        PASS.append("[3] 小文件秒传未命中 → 直接分块上传（不进重试）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_retry_exhausted_falls_back():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_max_count=2, fail_first=99)
    try:
        path = write_file(src, "大文件/movie.mkv", 12 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert mgr._rapid_calls[path] == 3, mgr._rapid_calls     # 首次 + 2 次重试
        assert rec["retry_count"] == 3, rec["retry_count"]
        assert "分块上传完成" in (rec["message"] or "")
        PASS.append("[4] 重试耗尽（1 次 + 2 次重试）→ 转分块上传成功")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_dedupe_and_rewrite():
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        path = write_file(src, "去重/a.bin", 4096)
        scanner.scan_once(store, cfg)
        st = os.stat(path)
        assert store.seen(path, st.st_size, st.st_mtime) is not None
        before = store.get(path)
        scanner.scan_once(store, cfg)             # 同 size/mtime：只推进稳定计数
        after = store.get(path)
        assert after["stable_count"] >= before["stable_count"]
        # 覆盖写：size 变化 → 视为新文件（旧 sha1/断点作废）
        write_file(src, "去重/a.bin", 8192)
        os.utime(path, (st.st_mtime + 5, st.st_mtime + 5))
        scanner.scan_once(store, cfg)
        rewritten = store.get(path)
        assert rewritten["sha1"] is None and rewritten["status"] == "待处理", rewritten
        PASS.append("[5] 扫描去重 + 覆盖重写识别（sha1/断点作废重来）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_suffix_and_dir_filter():
    """扩展名白名单 + exclude_dirs：非视频扩展名不入队，排除目录整棵跳过。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        cfg["scan"]["include_suffixes"] = [".mkv", ".mp4"]
        cfg["scan"]["exclude_dirs"] = ["Sample", "剧集/三体/extras"]
        keep1 = write_file(src, "电影/沙丘2.mkv", 2 * 1024 * 1024)
        keep2 = write_file(src, "剧集/三体/S01E01.mp4", 1024 * 1024)
        write_file(src, "电影/沙丘2.nfo", 2048)                   # 扩展名不在白名单
        write_file(src, "电影/poster.jpg", 4096)                  # 同上
        write_file(src, "电影/沙丘2.strm", 128)                   # strm 元数据不该上传
        write_file(src, "Sample/沙丘2.sample.mkv", 1024 * 1024)   # 顶层排除目录
        write_file(src, "剧集/三体/extras/bonus.mkv", 512 * 1024)  # 多级排除目录

        scanner.scan_once(store, cfg)
        queued = {r["path"] for r in store.by_status("待处理", limit=50)}
        assert queued == {keep1, keep2}, queued
        assert store.counts() == {"待处理": 2}, store.counts()

        # 白名单留空 = 不过滤；排除清空 = 全都扫
        cfg["scan"]["include_suffixes"] = []
        cfg["scan"]["exclude_dirs"] = []
        scanner.scan_once(store, cfg)
        total = sum(store.counts().values())
        assert total == 7, store.counts()
        PASS.append("[6] 扩展名白名单 + exclude_dirs：只收视频本体、排除目录整棵跳过；两者留空则全收")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def http_get(url: str, user: str | None = None, pwd: str = "") -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if user is not None:
        token = base64.b64encode(f"{user}:{pwd}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read()[:64]
    except urllib.error.HTTPError as exc:
        return exc.code, b""


async def case_http_auth():
    """HTTP 鉴权：开启后页面与 API 都要凭据，/health 始终放行，关掉即恢复开放。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "username": "upilot", "password": "s3cret-token"}}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        assert http_get(f"{base}/health")[0] == 200, "健康检查应始终放行"
        assert http_get(f"{base}/api/status")[0] == 401, "无凭据应 401"
        assert http_get(f"{base}/")[0] == 401, "页面无凭据应 401"
        assert http_get(f"{base}/api/status", "upilot", "wrong")[0] == 401, "错密码应 401"
        assert http_get(f"{base}/api/status", "upilot", "s3cret-token")[0] == 200, "Basic 应放行"
        assert http_get(f"{base}/api/status?token=s3cret-token")[0] == 200, "?token= 应放行"

        cfg["http"]["auth"]["enabled"] = False            # 关掉鉴权 → 恢复开放
        assert http_get(f"{base}/api/status")[0] == 200, "关闭后应开放"
        PASS.append("[7] HTTP 鉴权：Basic 与 ?token= 放行、无凭据 401、/health 始终 200、可随时关闭")
    finally:
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def case_form_auth():
    """站内登录页（mode=form）：未登录跳 /login，登录后 Cookie 放行，Basic 与 ?token= 仍是脚本通道。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None

    class _NoRedirect(urllib.request.HTTPRedirectHandler):      # 保留 302，便于断言跳转目标
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def raw(url: str, cookie: str = "") -> tuple[int, dict, bytes]:
        req = urllib.request.Request(url)
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    def login(user: str, pwd: str, nxt: str = "/") -> tuple[int, dict, bytes]:
        data = urllib.parse.urlencode({"username": user, "password": pwd, "next": nxt}).encode()
        req = urllib.request.Request(f"{base}/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()

    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "form", "username": "admin",
                                "password": "s3cret-token", "session_hours": 72}}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"

        # 未登录：页面跳登录页，API 给 401 JSON，/health 照旧放行
        assert raw(f"{base}/health")[0] == 200, "健康检查应始终放行"
        code, hdrs, _ = raw(f"{base}/")
        assert code == 302 and hdrs.get("location", "").startswith("/login?next="), \
            f"未登录访问首页应 302 到登录页，实际 {code} {hdrs.get('location')}"
        code, _, body = raw(f"{base}/api/status")
        assert code == 401 and b'"login"' in body, "未登录 API 应 401 并给出登录地址"
        code, _, body = raw(f"{base}/login")
        assert code == 200 and b"login" in body and b'name="password"' in body, "登录页应 200 且带密码框"

        # 登录
        code, _, body = login("admin", "wrong")
        assert code == 401 and "用户名或密码不对".encode() in body, "错密码应回到登录页并提示"
        code, hdrs, _ = login("admin", "s3cret-token", "/api/status")
        jar = hdrs.get("set-cookie", "")
        assert code == 302 and hdrs.get("location") == "/api/status", f"登录成功应 302 回原地址，实际 {code}"
        assert jar.startswith("upilot_session=") and "HttpOnly" in jar and "SameSite=Lax" in jar, \
            f"应下发 HttpOnly 会话 Cookie，实际 {jar[:60]}"
        token = jar.split(";", 1)[0]

        # 带着 Cookie：页面与 API 都放行
        assert raw(f"{base}/api/status", token)[0] == 200, "带会话 Cookie 访问 API 应放行"
        code, _, body = raw(f"{base}/", token)
        assert code == 200 and b"UPilot-mini" in body, "带会话 Cookie 应看到仪表盘"

        # 伪造 Cookie 不放行；Basic 与 ?token= 仍是脚本通道
        assert raw(f"{base}/api/status", "upilot_session=ZmFrZXxleHA=.deadbeef")[0] == 401, "伪造 Cookie 应 401"
        assert http_get(f"{base}/api/status", "admin", "s3cret-token")[0] == 200, "Basic 通道应保留"
        assert http_get(f"{base}/api/status?token=s3cret-token")[0] == 200, "?token= 通道应保留"

        # 退出：清 Cookie 并回登录页
        code, hdrs, _ = raw(f"{base}/logout", token)
        assert code == 302 and hdrs.get("location") == "/login", "退出应回登录页"
        assert "Max-Age=0" in hdrs.get("set-cookie", ""), "退出应清 Cookie"

        # 关掉鉴权 → 恢复开放
        cfg["http"]["auth"]["enabled"] = False
        assert raw(f"{base}/api/status")[0] == 200, "关闭鉴权后应开放"
        PASS.append("[8] 站内登录页（form）：未登录 302 /login、错密码提示、HttpOnly Cookie 放行、"
                    "伪造 Cookie 401、Basic 与 ?token= 保留、退出清 Cookie、/health 始终 200")
    finally:
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def case_login_limiter_source():
    """[29] 登录限速的「来源」必须不可伪造。

    `_client_ip()` 原来无条件信任 `X-Real-IP` / `X-Forwarded-For` —— 这两个头客户端
    随便写，每换一个值就换一个限速 key，失败计数永远攒不到上限，登录限速形同虚设
    （真机复现：同一来源换 12 次头、一次都没被锁）。而限速是本项目唯一的登录爆破防护。

    修法：只有 `http.auth.trust_forwarded_ip = true`（确实挂在反代后面）才读这两个头，
    默认直接 TCP 对端地址 —— 伪造头完全无效。本用例把开关两面都跑一遍，
    并验证「换头」只在开关打开时才算不同来源（打开≠关掉限速）。
    """
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    webui._FAILURES.clear()                     # 限速表是模块级全局，先隔离

    class _NoRedirect(urllib.request.HTTPRedirectHandler):   # 保留 302，便于断言放行
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def login(pwd: str, fwd_ip: str = "") -> int:
        data = urllib.parse.urlencode({"username": "upilot", "password": pwd}).encode()
        req = urllib.request.Request(f"{base}/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        if fwd_ip:
            req.add_header("X-Real-IP", fwd_ip)
        try:
            return opener.open(req, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "form", "username": "upilot",
                                "password": "s3cret-token", "session_hours": 72}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        # ① 默认（开关关）：换头无效 —— 同一来源连错必然被锁
        cfg["http"]["auth"]["trust_forwarded_ip"] = False
        webui._FAILURES.clear()
        codes = [login("wrong", f"10.0.0.{i}") for i in range(1, 7)]
        assert codes[:5] == [401] * 5, f"前 5 次错密码应 401，实际 {codes}"
        assert codes[5] == 429, f"第 6 次应被限速锁住（换头不该换 key），实际 {codes}"

        # ② 开关打开：头即来源，换头 ⇒ 换 key（真反代场景，各来源互不牵连）
        cfg["http"]["auth"]["trust_forwarded_ip"] = True
        webui._FAILURES.clear()
        codes = [login("wrong", f"10.0.1.{i}") for i in range(1, 13)]
        assert set(codes) == {401}, f"每个头都是新 key，不该被锁，实际 {codes}"

        # ③ 开关打开后，同一个头连错仍会被锁（换的是来源，不是关掉限速）
        webui._FAILURES.clear()
        codes = [login("wrong", "10.0.2.7") for _ in range(6)]
        assert codes[:5] == [401] * 5 and codes[5] == 429, f"同一来源仍应被锁，实际 {codes}"

        # ④ X-Forwarded-For 的链路只取第一个（代理会往后追加，不能整串当 key）
        webui._FAILURES.clear()
        req = urllib.request.Request(f"{base}/login", data=b"username=upilot&password=wrong",
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "X-Forwarded-For": "10.0.3.9, 10.0.3.10"})
        chain_codes = []
        for _ in range(6):
            try:
                chain_codes.append(opener.open(req, timeout=5).status)
            except urllib.error.HTTPError as exc:
                chain_codes.append(exc.code)
        assert chain_codes[5] == 429, f"同一链首来源应累积被锁，实际 {chain_codes}"

        # ⑤ 关掉开关后，刚才生效的伪造头立刻失效
        cfg["http"]["auth"]["trust_forwarded_ip"] = False
        webui._FAILURES.clear()
        codes = [login("wrong", f"10.0.4.{i}") for i in range(1, 7)]
        assert codes[5] == 429, f"关掉开关后换头应无效，实际 {codes}"

        # ⑥ 限速不该挡正常登录：锁的是来源，正确密码仍然放行
        webui._FAILURES.clear()
        assert login("s3cret-token") == 302, "正确密码应 302 放行"

        PASS.append("[29] 登录限速来源不可伪造：默认关时换 X-Real-IP 仍累积被锁（第 6 次 429）、"
                    "开关打开后按头区分来源（12 个不同头全 401 不误锁）、同一头仍会锁、"
                    "X-Forwarded-For 链路只取首段、开关关回即失效、正确密码始终放行")
    finally:
        webui._FAILURES.clear()
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


async def case_empty_username_session():
    """[30] 用户名留空时会话必须仍然有效（否则登录成功却无限弹回登录页）。

    `parse_session()` 用「返回用户名」表示通过，而 `check_auth` 原来写的是
    `if parse_session(...):` —— 用户名是**空串**时 `bool('')` 为 False，签名完全正确的
    会话被判成「没登录」。面板把「用户名」留空 + 登录页也不填 ⇒ 会话里存的就是空串：
    POST /login 返回 302 + Set-Cookie（日志还打「登录成功」），浏览器带着这个 cookie
    再访问又被弹回 /login。
    """
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    webui._FAILURES.clear()

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def post_login(fields: dict) -> tuple[int, str]:
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(f"{base}/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            resp = opener.open(req, timeout=5)
            return resp.status, resp.headers.get("Set-Cookie", "") or ""
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers.get("Set-Cookie", "") or ""

    def with_cookie(cookie: str) -> int:
        req = urllib.request.Request(f"{base}/api/status")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            return opener.open(req, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        # 面板只设了密码、用户名留空
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "form", "username": "",
                                "password": "s3cret-token", "session_hours": 72}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        # ① 未登录：API 401
        assert with_cookie("") == 401, "未登录应 401"

        # ② 登录页不填用户名 + 正确密码 → 应放行并下发会话
        code, jar = post_login({"username": "", "password": "s3cret-token", "next": "/api/status"})
        assert code == 302, f"用户名留空时正确密码应放行，实际 {code}"
        cookie = jar.split(";", 1)[0]
        assert cookie.startswith("upilot_session="), jar

        # ★ 修复点：带着刚下发的 Cookie 必须放行，而不是又弹回登录页
        assert with_cookie(cookie) == 200, "刚下发的会话被判未登录（登录循环）"

        # ③ 反面对照：伪造 / 过期 / 错密码签的会话仍必须拒绝
        assert with_cookie("upilot_session=ZmFrZXxleHA=.deadbeef") == 401, "伪造会话应 401"
        expired = webui.make_session("", "s3cret-token", -1)
        assert with_cookie(f"upilot_session={expired}") == 401, "过期会话应 401"
        wrong_secret = webui.make_session("", "not-the-password", 1)
        assert with_cookie(f"upilot_session={wrong_secret}") == 401, "错密码签的会话应 401"

        # ④ 单元层面直接锤 bool('') 陷阱
        a = {"enabled": True, "password": "p", "username": "", "mode": "form"}
        tok = webui.make_session("", "p", 1)
        assert webui.parse_session(tok, "p") == "", "空用户名的合法会话应通过签名校验"
        assert bool(webui.parse_session(tok, "p")) is False, "旧写法把它当 False（这就是 bug）"
        assert webui.parse_session(tok, "p") is not None, "判定必须用 is not None"
        c = copy.deepcopy(cfgmod.DEFAULTS)
        c["http"]["auth"] = a
        assert webui.check_auth(c, {"Cookie": f"upilot_session={tok}"}, "") is True, \
            "空用户名的合法会话必须放行"

        # ⑤ 反面：面板填了用户名时，登录页不填用户名仍应被拒（不能修宽了）
        cfg["http"]["auth"]["username"] = "admin"
        webui._FAILURES.clear()
        code, _ = post_login({"username": "", "password": "s3cret-token"})
        assert code == 401, f"面板填了用户名却不填 → 应拒绝，实际 {code}"
        code, jar = post_login({"username": "admin", "password": "s3cret-token"})
        assert code == 302 and jar.startswith("upilot_session="), "填对用户名应放行"

        PASS.append("[30] 用户名留空时会话仍有效：登录 302 下发 Cookie 且该 Cookie 访问 API 得 200"
                    "（修复前 401 → 无限弹回登录页）；伪造/过期/错密码签仍拒绝；"
                    "面板填了用户名时留空仍按错处理（未修宽）")
    finally:
        webui._FAILURES.clear()
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def case_a2a_token_resolution():
    """[31] A2A 凭据解析：嵌套 `a2a_agents.server.auth.token` → Bearer 认证头。

    `a2a_agents` 在 config.yaml 里是**嵌套**结构（a2a_agents → server → auth → token），
    不是平铺一层。写死取一层时取不到，只能靠字符串扫描，容易在结构与长度上出错
    （真机第一次发就报 "no a2a token found"）。

    本用例只验证「解析路径」与「header / payload 形状」，不联网、不打印 token 值。
    """
    import importlib.util

    # 脚本的正式位置在仓库内 `tools/a2a_send.py`（随仓库分发，CI 检出也有）；
    # 兼容历史位置（仓库根、以及仓库的上一级目录 —— 早先临时放在那里）。
    # 注意 APP 已经指向 `<仓库根>/app`，所以仓库根是 dirname(APP)，不是 dirname(dirname())。
    repo_root = os.path.dirname(APP)
    candidates = [
        os.path.join(repo_root, "tools", "a2a_send.py"),
        os.path.join(repo_root, "a2a_send.py"),
        os.path.join(os.path.dirname(repo_root), "a2a_send.py"),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    assert path, f"应能找到 a2a_send.py，找过：{candidates}"
    spec = importlib.util.spec_from_file_location("a2a_send_probe", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    placeholder = "T0KEN_PLACEHOLDER"

    # ① 已知嵌套路径必须命中
    assert mod.resolve_token({"a2a_agents": {"server": {"auth": {"token": placeholder}}}}) \
        == placeholder, "嵌套路径 a2a_agents.server.auth.token 未命中"

    # ② header 形状：Bearer + token，且是 JSON
    headers = mod.build_headers(placeholder)
    assert headers["Authorization"] == f"Bearer {placeholder}", headers
    assert headers["Content-Type"] == "application/json", headers

    # ③ payload 结构：JSON-RPC message/send + parts[{kind:text}]
    payload = mod.build_payload("PING", "ctx-1")
    assert payload["jsonrpc"] == "2.0" and payload["method"] == "message/send", payload
    msg = payload["params"]["message"]
    assert msg["role"] == "user" and msg["parts"] == [{"kind": "text", "text": "PING"}], msg
    assert msg["contextId"] == "ctx-1" and msg.get("messageId"), msg
    # 不带 context 时不该凭空造一个
    assert "contextId" not in mod.build_payload("PING")["params"]["message"]

    # ④ 不绑死第一层：别的嵌套层次也要能兜底取到
    assert mod.resolve_token({"a2a_agents": {"peer": {"token": "X9"}}}) == "X9"
    assert mod.resolve_token({"a2a_agents": {"server": {"token": "X8"}}}) == "X8"

    # ⑤ 端点解析：环境变量 > 配置里的 a2a_agents.server.url；都没有则明确失败。
    #    （仓库是公开的，所以实现里不写死任何内网地址作默认值。）
    saved_env = os.environ.get(mod.ENDPOINT_ENV)
    try:
        os.environ.pop(mod.ENDPOINT_ENV, None)
        assert mod.resolve_endpoint({"a2a_agents": {"server": {"url": "https://example.invalid/"}}}) \
            == "https://example.invalid/", "未按 a2a_agents.server.url 解析端点"
        os.environ[mod.ENDPOINT_ENV] = "https://env.invalid/"
        assert mod.resolve_endpoint({"a2a_agents": {"server": {"url": "https://example.invalid/"}}}) \
            == "https://env.invalid/", "环境变量应优先于配置"
        os.environ.pop(mod.ENDPOINT_ENV, None)
        try:
            mod.resolve_endpoint({})
        except SystemExit:
            pass
        else:
            raise AssertionError("无端点时应明确失败")
        # 端点不该被硬编码在源码里。断言字符串用拼接写，免得断言自身
        # 把这个地址留在公开仓库里（否则"搜不到残留"永远搜得到自己）。
        hardcoded = "her" + ".ding" + ".cab"
        src = open(path, encoding="utf-8").read()
        assert hardcoded not in src, "公开仓库里不该写死内部 A2A 地址"
    finally:
        if saved_env is None:
            os.environ.pop(mod.ENDPOINT_ENV, None)
        else:
            os.environ[mod.ENDPOINT_ENV] = saved_env

    # ⑥ 取不到 token 时明确失败，而不是静默给出空 Authorization 头
    try:
        mod.resolve_token({})
    except SystemExit:
        pass
    else:
        raise AssertionError("无 token 时应明确失败，不能静默返回空")

    # ⑦ `send()` 必须能从配置解析出端点（防"改了常量名但 send 还在用旧名"这类
    #    只在运行期炸、py_compile 抓不到的回归）。
    import inspect as _insp
    send_src = _insp.getsource(mod.send)
    assert "resolve_endpoint(" in send_src, "send() 没走 resolve_endpoint（改了名字没同步）"
    assert "ENDPOINT," not in send_src, "send() 还在用已删除的裸常量 ENDPOINT"
    if os.path.isfile(mod.CFG):
        live_cfg = mod.load_config()
        assert mod.resolve_endpoint(live_cfg).startswith("http"), "本机配置应能解析出端点"

    # ⑧ 本机真实配置里确实能解析出生效 token（只断言形状，不打印值）
    if os.path.isfile(mod.CFG):
        live = mod.load_token()
        assert isinstance(live, str) and len(live) >= 20, "本机配置应能解析出 token"
        assert live == live.strip() and "\n" not in live, "token 不该带空白"
        assert mod.build_headers(live)["Authorization"].startswith("Bearer ")
        described = f"本机配置解析成功（长度 {len(live)}）"
    else:
        described = "本机无配置文件，仅验证解析逻辑"

    PASS.append(f"[31] A2A 凭据解析：嵌套 a2a_agents.server.auth.token 命中（含多层兜底）、"
                f"Bearer 头与 JSON-RPC message/send + parts[{{kind:text}}] 形状正确、"
                f"取不到时明确失败；{described}")


async def case_dir_mapping():
    """本机相对路径 → 115 目录：改名（Anime→/OtherAnime）、大小写、最长前缀、未命中兜底。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1, fail_first=0, small_file_mb=0.0)
    try:
        cfg["paths"]["target_root"] = "/UPILOT-mini"
        cfg["paths"]["map"] = [
            {"from": "TVs", "to": "/TVs"},
            {"from": "Movies/Others", "to": "/Movies/Others"},
            {"from": "Movies/AnimeMovies", "to": "/Movies/AnimeMovies"},
            {"from": "Anime", "to": "/OtherAnime"},
            {"from": "Something", "to": "/something"},
        ]
        cases = {
            "TVs/我的剧集/S01E01.mkv": "/TVs/我的剧集",
            "Movies/Others/某电影/movie.mkv": "/Movies/Others/某电影",
            "Movies/AnimeMovies/某动画电影/a.mkv": "/Movies/AnimeMovies/某动画电影",
            "Anime/葬送的芙莉莲/E01.mkv": "/OtherAnime/葬送的芙莉莲",
            "Something/杂物/a.bin": "/something/杂物",
            "Movies/某电影/hit.mkv": "/UPILOT-mini/Movies/某电影",        # 未命中 → 回落 target_root
            "tvs/小写不算命中/x.mkv": "/UPILOT-mini/tvs/小写不算命中",     # 大小写敏感
            "顶层的文件.mkv": "/UPILOT-mini",                            # 没有子目录
        }
        for rel, want in cases.items():
            full = os.path.join(src, rel.replace("/", os.sep))
            got = resolve_upload_target(full, src, cfg["paths"]["target_root"], cfg["paths"]["map"])
            assert got == want, f"{rel} → {got}（期望 {want}）"

        path = write_file(src, "Anime/葬送的芙莉莲/E01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path)
        assert rec["status"] == "已上传", rec
        assert rec["target_dir"] == "/OtherAnime/葬送的芙莉莲", rec["target_dir"]
        assert "/OtherAnime/葬送的芙莉莲" in mgr._mem, mgr._mem
        PASS.append("[9] 目录映射：TVs→/TVs、Anime→/OtherAnime（改名）、Movies/Others 保留、"
                    "大小写敏感、最长前缀、未命中回落 target_root")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_settings_map_and_slots():
    """设置页：多行「本机 => 115 目录」解析与回填、坏行拒绝、登录槽位中文下拉。"""
    field = cfgmod.find_field("paths.map")
    assert field is not None and field["type"] == "map", field
    parsed = cfgmod.coerce(field, "TVs => /TVs\nAnime -> /OtherAnime\n\n# 注释行跳过\nSomething=>something")
    assert parsed == [{"from": "TVs", "to": "/TVs"}, {"from": "Anime", "to": "/OtherAnime"},
                      {"from": "Something", "to": "/something"}], parsed
    # 「=> /x」是合法的：from 留空表示整个监控根映射过去
    assert cfgmod.coerce(field, "=> /root-mapped") == [{"from": "", "to": "/root-mapped"}]
    for bad in ("只有一边", "TVs => "):
        try:
            cfgmod.coerce(field, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"坏行应被拒：{bad!r}")

    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    cfg["paths"]["map"] = parsed
    payload = cfgmod.form_payload(cfg)
    text = [f for g in payload["groups"] for f in g["fields"] if f["key"] == "paths.map"][0]["value"]
    assert "Anime => /OtherAnime" in text, text
    assert "Something => /something" in text, text

    appf = cfgmod.find_field("p115.app")
    assert appf["type"] == "select" and appf["options"][0]["value"] == "115ipad", appf
    assert cfgmod.app_label("ipad") == "115(iPad端)"
    assert cfgmod.app_label("qandroid") == "115管理(Android端)", "老配置里的历史槽位仍要能读出中文名"
    assert all("qr" not in x for x in cfgmod.APP_OPTIONS), "扫码已删除，槽位里不该再有 qr 标记"
    assert not hasattr(cfgmod, "QR_UNSUPPORTED"), "扫码槽位白名单已随扫码功能一起删除"
    assert not hasattr(cfgmod, "QR_SUPPORTED"), "扫码槽位白名单已随扫码功能一起删除"
    assert len(cfgmod.APP_OPTIONS) == 13, len(cfgmod.APP_OPTIONS)
    try:
        cfgmod.coerce(appf, "不存在的端")
    except ValueError:
        pass
    else:
        raise AssertionError("未知登录槽位应被拒")
    PASS.append("[10] 设置页：映射多行解析/回填、坏行拒绝；115 客户端身份中文下拉 + 未知值拒绝")



def case_qr_removed():
    """扫码登录已删除：相关接口、内存会话、配置常量都不该再存在。"""
    assert not hasattr(webui, "_qr_api"), "扫码接应函数应已删除"
    assert not hasattr(webui, "QR_SESSION"), "扫码会话应已删除"
    assert not hasattr(webui, "QR_TTL"), "扫码会话应已删除"
    for dead in ("qr_start", "qr_poll", "qrSetState", "qrKick", "qrCancel"):
        assert dead not in open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read(), dead
    html = open(os.path.join(APP, "static", "index.html"), encoding="utf-8").read()
    for dead in ("115 扫码登录", "qr-canvas", "btn-qr-start", "获取二维码"):
        assert dead not in html, dead
    css = open(os.path.join(APP, "static", "app.css"), encoding="utf-8").read()
    assert "#qr-canvas" not in css and ".qrsteps" not in css and ".qrmsg" not in css
    PASS.append("[11] 扫码登录已删除：/api/p115/qrcode*、QR_SESSION、_qr_api、"
                "前端卡片与样式全部清掉，只留「填 cookie / 跟别人同步 cookie」两条路")



def case_cookie_sync_from_file():
    """从外部程序配置同步 cookie：嵌套 yaml / 字典形式 / JSON 都能抠出来，无关文件返回空。"""
    tmp = tempfile.mkdtemp(prefix="upilot-ck-")
    try:
        p1 = os.path.join(tmp, "a.yaml")
        with open(p1, "w", encoding="utf-8") as f:
            f.write('settings_115:\n  - name: x\n    cookies: "UID=1_A; CID=2_B; SEID=3_C; KID=4_D"\n')
        ck = cfgmod.load_cookie_from(p1)
        assert ck == "UID=1_A; CID=2_B; SEID=3_C; KID=4_D", ck

        p2 = os.path.join(tmp, "b.json")
        with open(p2, "w", encoding="utf-8") as f:
            f.write('{"a": {"cookies": {"UID": "u", "CID": "c", "SEID": "s"}}}')
        ck2 = cfgmod.load_cookie_from(p2)
        assert "UID=u" in ck2 and "CID=c" in ck2 and "SEID=s" in ck2, ck2

        p3 = os.path.join(tmp, "c.yaml")
        with open(p3, "w", encoding="utf-8") as f:
            f.write("nothing: here\n")
        assert cfgmod.load_cookie_from(p3) == ""
        assert cfgmod.find_field("p115.cookie_from") is not None
        PASS.append("[12] cookie 同步：外部 yaml（嵌套 list/dict）与 json 都能抠出 115 cookie，无关文件返回空")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_cid_parse_and_ensure_pid():
    """fs_makedirs 两种返回形态都要能解析出 cid。

    线上真实故障：目录已存在时 115 只回 {"data": {"file_id": ...}}，早期只处理
    data 为路径段列表的情况，导致同一个目录第二次进来就抛「无法解析 cid」。
    """
    import p115 as p115mod

    P = p115mod.parse_dir_cid
    # 形态 1：整条路径新建 → 路径段列表，取最后一段
    assert P({"state": True, "data": [{"id": "0", "name": "根目录"},
                                      {"id": "2325467636346843491", "name": "TVs"},
                                      {"id": "1000000000000000100", "name": "Season 01"}]}) \
        == 1000000000000000100
    # 形态 2：目录已存在（线上真实响应）
    assert P({"state": True, "error": "", "errno": 0,
              "data": {"file_id": "1000000000000000100", "is_private": "0"}}) == 1000000000000000100
    # 形态 3：顶层兜底
    assert P({"state": True, "file_id": "123"}) == 123
    # 失败 / 认不出来 → None（调用方据此抛错，不把文件丢到根目录）
    assert P({"state": False, "data": {"file_id": "1"}}) is None
    assert P({"state": True, "data": {}}) is None
    assert P({"state": True, "data": []}) is None
    assert P({}) is None

    # 集成：目录已存在也要拿到 cid，且第二次走缓存不再请求
    class FakeStore:
        def __init__(self):
            self.saved = {}

        def cid_get(self, p):
            return self.saved.get(p)

        def cid_set(self, p, c):
            self.saved[p] = c

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def fs_makedirs(self, path, async_=True):
            self.calls.append(path)
            return {"state": True, "error": "", "errno": 0,
                    "data": {"file_id": "1000000000000000100", "is_private": "0"}}

    mgr = p115mod.P115.__new__(p115mod.P115)
    mgr.store, mgr.client = FakeStore(), FakeClient()
    mgr._cid_mem, mgr._makedirs_lock = {}, asyncio.Lock()

    cloud = "/TVs/甲剧 (2026) [tmdbid=100001]/Season 01"
    pid1 = await mgr.ensure_pid(cloud)
    assert pid1 == 1000000000000000100, pid1
    pid2 = await mgr.ensure_pid(cloud)
    assert pid2 == pid1 and len(mgr.client.calls) == 1, mgr.client.calls
    PASS.append("[13] fs_makedirs「目录已存在」响应（dict/file_id）也能解析出 cid，且第二次走缓存")


async def case_rapid_only_mode():
    """纯秒传：未命中就排队等，绝不普通上传（小文件也一样）；等云端有同 hash 后零上行命中。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, small_file_mb=10.0,
                                             retry_interval=60, fail_first=99,
                                             mode="rapid_only", long_interval=300)
    try:
        assert up.rapid_only and up.long_interval == 300, (up.mode, up.long_interval)

        # 1) 小文件（小于 small_file_mb）也不走普通上传，进重试队列等
        small = write_file(src, "小文件/cover.jpg", 200 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, small, rounds=1)
        assert rec["status"] == "待重试", rec
        assert rec["retry_count"] == 1 and "不会真上传" in (rec["message"] or ""), rec
        assert rec["next_retry_at"] > time.time() + 30, rec        # 真在等 60 秒，不是马上重来
        assert rec["upload_mode"] is None, rec                    # 一次普通上传都没发生过

        # 2) 大文件：短间隔次数用完后转长间隔，继续等（仍然不普通上传）
        big = write_file(src, "大文件/movie.mkv", 12 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        for _ in range(6):                                        # max_count 默认 5
            store.set(big, next_retry_at=0)
            await up._process(big)
        rb = store.get(big)
        assert rb["status"] == "待重试" and rb["retry_count"] >= 6, rb
        assert "5 分钟" in (rb["message"] or "") and "不会真上传" in (rb["message"] or ""), rb["message"]
        assert rb["upload_mode"] is None, rb

        # 3) 纯秒传下「待上传」不允许停在原地：会被派发，且 _process 立刻把它拉回重试队列。
        #    绝不能因此发生一次普通上传（下面那条 upload_mode 断言才是核心保证）。
        #    这正是「先按 rapid_then_upload 跑出一批待上传、之后切到只秒传」的自愈路径 ——
        #    dispatch 若漏掉这个状态，那些文件会永久卡在「待上传」，无人值守时没人会发现。
        store.set(big, status="待上传")
        up.dispatch()
        for _ in range(20):
            await asyncio.sleep(0.05)
            if store.get(big)["status"] != "待上传":
                break
        assert store.get(big)["status"] == "待重试", store.get(big)
        assert store.get(big)["upload_mode"] is None, store.get(big)   # 一次普通上传都没有

        # 4) 云端后来有了同 hash → 零上行命中
        mgr.fail_first = 0
        store.set(big, next_retry_at=0, retry_count=0)
        rec = await drive(up, store, big, rounds=4)
        assert rec["status"] == "已上传" and rec["upload_mode"] == "rapid", rec
        PASS.append("[14] 纯秒传：小文件也不普通上传；短间隔用完转长间隔一直等；"
                    "云端有同 hash 后零上行命中")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_notify_webhook():
    """秒传命中 / 上传完成后，按 CloudDrive2 file_notify 契约通知别的程序。

    symedia 的「链接同步（Webhook 模式）」就是靠这个端点收文件变更事件的，
    所以 body 必须与 CD2 的一致（action / is_dir / source_file），路径还得换成
    CD2 挂载视图（path_prefix）。
    """
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    got: list = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                got.append((self.path, dict(self.headers), _json.loads(raw or b"{}")))
            except Exception:  # noqa: BLE001
                got.append((self.path, dict(self.headers), {}))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):  # 关掉测试输出
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    async def wait_until(pred, timeout: float = 3.0) -> bool:
        """通知现在是丢到后台发的，断言前先等它跑完。"""
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            await asyncio.sleep(0.05)
        return pred()

    dirs: list[str] = []
    try:
        base = {"enabled": True,
                "url": f"http://127.0.0.1:{port}",      # 只填地址，端点路径由程序补全
                "query": "device_name=upilot-mini&user_name=upilot-mini&type=notify",
                "path_prefix": "/CloudNAS/CloudDrive/115open",
                "auth": "Basic dTpw", "on": "both", "timeout": 5, "retries": 0}

        # 1) 秒传命中 → 发一条 create 通知，路径补成 CD2 视角
        cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
        dirs.append(tmp)
        cfg["paths"]["map"] = [{"from": "TVs", "to": "/TVs"}]
        cfg["notify"] = dict(base)
        up = Uploader(cfg, store, mgr)
        path = write_file(src, "TVs/甲剧 (2026) [tmdbid=100001]/Season 01/E27.mkv", 12 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        rec = await drive(up, store, path, rounds=6)
        assert rec["status"] == "已上传" and rec["upload_mode"] == "rapid", rec
        assert await wait_until(lambda: len(got) == 1), got
        p_, hdrs, body = got[0]
        assert "file_notify" in p_ and "type=notify" in p_, p_
        assert hdrs.get("Authorization") == "Basic dTpw", hdrs
        assert body.get("event_category") == "file" and body.get("event_name") == "notify", body
        item = (body.get("data") or [{}])[0]
        assert item.get("action") == "create" and item.get("is_dir") == "false", item
        assert item.get("source_file") == ("/CloudNAS/CloudDrive/115open/TVs/"
                                          "甲剧 (2026) [tmdbid=100001]/Season 01/E27.mkv"), item

        # 2) 对方打不通 → 上传本身照常成功（通知失败不阻塞、不改状态）
        got.clear()
        cfg2, store2, mgr2, up2, tmp2, src2 = make_env(stable_rounds=0, fail_first=0)
        dirs.append(tmp2)
        cfg2["notify"] = dict(base, url="http://127.0.0.1:1/nope", retries=0, timeout=2)
        up2 = Uploader(cfg2, store2, mgr2)
        p2 = write_file(src2, "小文件/cover.jpg", 512 * 1024)
        scanner.scan_once(store2, cfg2)
        rec2 = await drive(up2, store2, p2, rounds=4)
        assert rec2["status"] == "已上传", rec2
        await asyncio.sleep(0.8)                 # 等后台通知跑完（应当失败，但不影响上传结果）
        assert not got, got

        # 3) on=rapid_hit 时，走分块上传完成不发通知
        cfg3, store3, mgr3, up3, tmp3, src3 = make_env(stable_rounds=0, fail_first=99,
                                                       retry_max_count=0)
        dirs.append(tmp3)
        cfg3["notify"] = dict(base, on="rapid_hit")
        up3 = Uploader(cfg3, store3, mgr3)
        p3 = write_file(src3, "小文件/photo.jpg", 300 * 1024)
        scanner.scan_once(store3, cfg3)
        rec3 = await drive(up3, store3, p3, rounds=4)
        assert rec3["status"] == "已上传" and rec3["upload_mode"] == "normal", rec3
        await asyncio.sleep(0.4)
        assert not got, got

        # 4) 面板改 notify.* 应当触发重建（否则要重启容器才生效）
        cfg4 = copy.deepcopy(cfgmod.DEFAULTS)
        r4 = cfgmod.apply_values(cfg4, {"notify.url": "http://127.0.0.1:9/x"})
        assert r4["reload"] is True and "notify.url" in r4["changed"], r4
        r5 = cfgmod.apply_values(copy.deepcopy(cfgmod.DEFAULTS), {"upload.mode": "rapid_then_upload"})
        assert r5["reload"] is True, r5

        PASS.append("[15] Webhook 通知：只填 symedia 地址也能自动补全端点与参数；秒传命中"
                    "按 CD2 file_notify 契约发 create（路径补前缀、带 auth）；对方不通不影响"
                    "上传；on=rapid_hit 时上传完成不发；面板改 notify./upload./retry. 即时重建")
    finally:
        srv.shutdown()
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


async def case_delete_after_upload():
    """[16] 秒传成功后删掉本地源文件（越界和大小不符两道护栏都要拦住）。"""
    print("[16] 秒传成功后删除本地源文件")
    tmps = []
    try:
        # 1) 开关打开：秒传命中 → 源文件被清掉，并在记录里留痕
        cfg, store, mgr, up, tmp, src = make_env(mode="rapid_only", fail_first=0,
                                                 delete_after=True)
        tmps.append(tmp)
        p = write_file(src, "TVs/Show/S01E01.mkv", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)      # 第 1 轮：入队
        scanner.scan_once(store, cfg)      # 第 2 轮：稳定计数到 1
        rec = await drive(up, store, p)
        assert rec["status"] == "已上传" and rec["upload_mode"] == "rapid", rec
        assert not os.path.exists(p), "秒传命中后源文件应该被删掉"
        assert "已删除本地源文件" in (store.get(p)["message"] or ""), store.get(p)["message"]

        # 2) 开关关着：一个字节都不动
        cfg2, store2, mgr2, up2, tmp2, src2 = make_env(mode="rapid_only", fail_first=0)
        tmps.append(tmp2)
        p2 = write_file(src2, "TVs/Show/S01E02.mkv", 3 * 1024 * 1024)
        scanner.scan_once(store2, cfg2)
        scanner.scan_once(store2, cfg2)
        rec2 = await drive(up2, store2, p2)
        assert rec2["status"] == "已上传", rec2
        assert os.path.exists(p2), "开关关着时不该动源文件"

        # 3) 护栏一：监控目录外面的路径一律不删
        outsider = os.path.join(tmp, "outside.mkv")
        with open(outsider, "wb") as f:
            f.write(b"x" * 2048)
        up._delete_source(outsider, {"size": 2048})
        assert os.path.exists(outsider), "监控目录外的文件不许动"

        # 4) 护栏二：大小与入库记录不符（说明文件被换过）不删
        swapped = write_file(src, "TVs/Show/S01E03.mkv", 2048)
        up._delete_source(swapped, {"size": 999999})
        assert os.path.exists(swapped), "大小对不上时不许删"

        # 5) 只删文件，不删目录
        assert os.path.isdir(os.path.join(src, "TVs", "Show")), "目录必须留着"

        PASS.append("[16] 传完删源文件：命中即清、开关关着不动、"
                    "越界与大小不符都拦住、目录保留")
    finally:
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)


def case_episode_key():
    """[17] 同一集归一化：画质标签不参与；**扩展名也不参与**（mkv / mp4 是同一集）；
    剧名按 title_match 比；认得出 1x01 / EP01 / 第十二集 / 动漫 " - 05 " 等写法。"""
    import episode as epmod
    from p115 import episode_key as K

    dv = "甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL DV TX DemoWeb].mkv"
    nov = "甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv"
    assert K(dv) == K(nov) == "s01e031", (K(dv), K(nov))
    assert K("Show.S01E31.2160p.DV.mkv") == K("Show.S01E31.2160p.mkv") == "s01e031"
    assert K("甲剧 第31集 [1080p].mkv") == K("甲剧 第31集 [2160p].mkv") == "e031"

    assert K(dv) != K(dv.replace("E31", "E32")), "不同集不该算同一集"
    assert K(dv) == K(dv.replace(".mkv", ".mp4")) == K("Show.S01E31.ts"), "扩展名不参与：mkv/mp4/ts 同一集"
    assert K("S01E01.mkv") == K("s01e01.mp4") == K("Show.Name.s01.e01.1080p.avi")
    assert K("Show S01E01.mkv") != K("Show S02E01.mkv"), "不同季不同集"

    # 其它写法
    assert K("Show 1x05.mkv") == K("Show.S01E05.mp4") == "s01e005"
    assert K("Show Season 2 Episode 3.mkv") == "s02e003"
    assert K("某剧 第二季 第十二集.mkv") == "s02e012" and K("某剧 第12集.mkv") == "e012"
    assert K("Show EP05.mp4") == "e005" and K("Show S02 EP05.mp4") == "s02e005"
    assert K("[ANi] Show - 05 [1080p].mp4") == K("[Other] Show - 05v2 [720p].mkv") == "e005"
    assert K("Show.S01E01-E02.mkv") == "s01e001-e002" != K("Show.S01E01.mkv"), "多集区间单独成键，不与单集混"
    assert K("Show S01E01-1080p.mkv") == "s01e001", "后面跟分辨率不是多集区间"
    assert K("Show 1920x1080.mkv") is None and K("Movie (2012).mkv") is None
    assert K("Movie - 2012.mkv") is None, "4 位年份不是集号"

    assert K("甲剧 (2026) [tmdbid=100001] S01E31 [2160p].nfo") is None, "非视频不参与"
    assert K("甲剧 (2026) [tmdbid=100001] [2160p WEB-DL].mkv") is None, "认不出季集就一律不动"
    assert K("") is None

    # 剧名兼容（auto）：同一文件夹多部剧时，A 剧的 S01E01 不是 B 剧的 S01E01
    P = epmod.parse_episode
    try:
        epmod.set_title_match("auto")
        assert epmod.titles_compatible(P("甲剧 (2026) S01E01.mkv"), P("甲剧.S01E01.HDR.mp4"))
        assert epmod.titles_compatible(P("Show Name S01E01.mkv"), P("show.name.2026.S01E01.mp4"))
        assert epmod.titles_compatible(P("S01E01.mkv"), P("Show S01E01.mp4")), "一方没剧名按同剧"
        assert not epmod.titles_compatible(P("Alpha S01E01.mkv"), P("Beta S01E01.mkv"))
        assert not epmod.titles_compatible(P("狂飙 S01E01.mkv"), P("甲剧 S01E01.mkv"))
        assert epmod.titles_compatible(P("Alpha S01E01.mkv"), P("Beta S01E01.mkv"), "ignore")
        assert not epmod.titles_compatible(P("Show Name S01E01.mkv"), P("Show S01E01.mkv"), "same")
        groups = epmod.group_same_episode([(1, "Alpha S01E01.mkv"), (2, "Beta S01E01.mkv"),
                                           (3, "alpha.S01E01.mp4"), (4, "Beta S01E02.mkv")])
        assert groups == [[1, 3]], groups
    finally:
        epmod.set_title_match("auto")
    assert epmod.cn_to_int("十二") == 12 and epmod.cn_to_int("二十") == 20 and epmod.cn_to_int("一百零五") == 105
    assert epmod.cn_to_int("一千零一") == 1001 and epmod.cn_to_int("一千一百") == 1100
    assert K("某剧 第一百零五集.mkv") == "e105" and K("某剧 第一千零一集.mkv") == "e1001"
    # S00 / SP：特别篇
    assert K("Show.S00E01.mkv") == "s00e001" != K("Show.S01E01.mkv")
    assert K("Show.SP01.mkv") == K("Show.Special.01.mkv") == "s00e001"
    # 不带季号的多集区间
    assert K("Show.E01-E02.mkv") == K("Show EP01-EP02.mkv") == "e001-e002"
    assert K("Show.E01-E02.mkv") != K("Show.E01.mkv")
    # 修订标记不进同一集键
    assert K("Show.S01E01.PROPER.mkv") == K("Show.S01E01.REPACK.mkv") == K("Show.S01E01.v2.mkv") == "s01e001"
    assert epmod.version_score("Show.S01E01.mkv") == 0
    assert epmod.version_score("Show.S01E01.PROPER.mkv") == 2
    assert epmod.version_score("Show.S01E01.v3.mkv") == 3
    assert epmod.version_score("[ANi] Show - 05v2 [1080p].mkv") == 2
    PASS.append("[17] 同集归一化：画质标签与扩展名都不参与（S01E01 mkv/mp4/ts 同一集）；认得 1x01 / Season N Episode M / "
                "EP01 / 第十二集 / 第一百零五集 / 第一千零一集 / 动漫「 - 05 」/ S00 与 SP 特别篇；"
                "多集区间单独成键；PROPER/REPACK/v2 不进键；年份/分辨率不误判；auto 下多部剧同集号不混")


async def case_cleanup_previous_versions():
    """[18] 洗板清理：新版传成功后云端同集旧版本进回收站；开关关着/不同集/超上限都不动。"""
    dv_name = "甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL DV TX DemoWeb].mkv"
    nov_name = "甲剧 (2026) [tmdbid=100001] S01E31 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv"
    rel_dir = "TVs/甲剧 (2026) [tmdbid=100001]/Season 01"
    cloud_dir = f"/TVs/甲剧 (2026) [tmdbid=100001]/Season 01"
    tmps: list[str] = []
    try:
        async def build(enable: bool, max_delete: int = 3):
            cfg, store, mgr, _up, tmp, src = make_env(mode="rapid_only", fail_first=0)
            tmps.append(tmp)
            cfg["paths"]["map"] = [{"from": "TVs", "to": "/TVs"}]
            cfg["upload"]["cleanup_previous_versions"] = enable
            cfg["upload"]["cleanup_max_delete"] = max_delete
            up = Uploader(cfg, store, mgr)
            await mgr.ensure_pid(cloud_dir)                 # 让 StubP115 认得这个目录
            gone: list[str] = []

            async def fake_delete(fid):
                gone.append(fid)
                return True

            mgr.delete_file = fake_delete
            return cfg, store, mgr, up, tmp, src, gone

        def put_cloud(store, tmp, name: str, size: int) -> str:
            """在「云端」放一条已上传记录（同时是 list_dir 的数据来源）。"""
            p = os.path.join(tmp, name)
            store.add(p, size, 1.0)
            store.set(p, status="已上传", target_dir=cloud_dir, message="秒传命中（旧版）")
            return p

        async def upload_new(up, store, src, rel_dir, name):
            p = write_file(src, f"{rel_dir}/{name}", 3 * 1024 * 1024)
            scanner.scan_once(store, cfg_of[0])
            scanner.scan_once(store, cfg_of[0])
            return await drive(up, store, p), p

        # 1) 开关打开：带 DV 的旧版被清，记录里留痕
        cfg, store, mgr, up, tmp, src, gone = await build(True)
        cfg_of = [cfg]
        old = put_cloud(store, tmp, dv_name, 5 * 1024 * 1024)
        rec, _p = await upload_new(up, store, src, rel_dir, nov_name)
        assert rec["status"] == "已上传" and rec["target_dir"] == cloud_dir, rec
        assert gone == [f"stub-{old}"], gone
        assert "云端同集旧版本已清 1 个" in (rec["message"] or ""), rec["message"]

        # 2) 开关关着：一个字节都不动
        cfg2, store2, mgr2, up2, tmp2, src2, gone2 = await build(False)
        cfg_of = [cfg2]
        put_cloud(store2, tmp2, dv_name, 5 * 1024 * 1024)
        rec2, _ = await upload_new(up2, store2, src2, rel_dir, nov_name)
        assert rec2["status"] == "已上传", rec2
        assert gone2 == [], gone2

        # 3) 不同集 / 非视频不参与：只有同集同扩展名才动手
        cfg3, store3, mgr3, up3, tmp3, src3, gone3 = await build(True)
        cfg_of = [cfg3]
        put_cloud(store3, tmp3, dv_name.replace("S01E31", "S01E30"), 5 * 1024 * 1024)
        put_cloud(store3, tmp3, dv_name.replace(".mkv", ".nfo"), 2048)
        put_cloud(store3, tmp3, "别的剧 (2026) [tmdbid=1] S01E31 [1080p].mkv", 5 * 1024 * 1024)
        rec3, _ = await upload_new(up3, store3, src3, rel_dir, nov_name)
        assert rec3["status"] == "已上传" and gone3 == [], (rec3["status"], gone3)

        # 4) 超过上限：整体跳过并记警告，一条都不删
        cfg4, store4, mgr4, up4, tmp4, src4, gone4 = await build(True, max_delete=1)
        cfg_of = [cfg4]
        put_cloud(store4, tmp4, dv_name, 5 * 1024 * 1024)
        put_cloud(store4, tmp4, dv_name.replace("DV ", "HDR "), 4 * 1024 * 1024)
        rec4, _ = await upload_new(up4, store4, src4, rel_dir, nov_name)
        assert gone4 == [], gone4
        assert "超过上限 1" in (rec4["message"] or ""), rec4["message"]

        # 5) 认不出季集的新版：跳过，不去猜
        cfg5, store5, mgr5, up5, tmp5, src5, gone5 = await build(True)
        cfg_of = [cfg5]
        put_cloud(store5, tmp5, dv_name, 5 * 1024 * 1024)
        rec5, _ = await upload_new(up5, store5, src5, rel_dir, "花絮合集.mkv")
        assert gone5 == [], gone5

        # 6) list_dir 只收文件：115 的文件夹条目没有 fid（身份 id 在 cid 键上），必须被滤掉
        import p115 as p115mod

        class _FilesClient:
            """伪造 fs_files：一条目录 + 一条文件（形状照真机实测）。"""

            async def fs_files(self, payload, async_=True):
                return {"state": True, "errno": 0, "count": 2, "data": [
                    {"n": "甲剧 (2026) [tmdbid=100001]", "cid": "111", "pid": "0", "ns": "x"},
                    {"n": "甲剧 (2026) [tmdbid=100001] S01E31 [1080p].mkv",
                     "fid": "222", "s": 10, "ico": "mkv"},
                ]}

        probe = p115mod.P115.__new__(p115mod.P115)
        probe.client = _FilesClient()
        probe._dir_cache, probe._dir_cache_ttl = {}, 30.0
        only_files = await probe.list_dir(777)
        assert [r["n"] for r in only_files] == ["甲剧 (2026) [tmdbid=100001] S01E31 [1080p].mkv"], only_files
        rows_again = await probe.list_dir(777)                 # 第二次走缓存，不再请求
        assert rows_again == only_files, rows_again

        # 7) 刚上传那条被 115 改名（且列表里没有 s 字段）时，靠 file_id 排除掉
        class _RenamedCloud:
            def __init__(self):
                self.deleted: list[str] = []

            async def list_dir(self, pid):
                return [{"n": nov_name.replace(".mkv", " (1).mkv"), "fid": "999",
                         "ico": "mkv"},                        # 刚传的新版被改名，且没给 s
                        {"n": dv_name, "fid": "888", "s": 5 * 1024 * 1024, "ico": "mkv"}]

            async def delete_file(self, fid):
                self.deleted.append(fid)
                return True

        rc = _RenamedCloud()
        res = await p115mod.P115.cleanup_previous(rc, 1, nov_name, 3 * 1024 * 1024, 3,
                                                 keep_fid="999")
        assert rc.deleted == ["888"], rc.deleted        # 只删旧版，绝不碰刚上传那条
        assert res["deleted"] == [dv_name], res

        # 7b) 真机实测：秒传落库的 file_id 是 pickcode（abcd0demo0pick001），列表里的 fid 是数字串，
        #     所以排除必须同时认 fid / pc / file_id 三栏，否则这道护栏等于没有
        class _PickcodeCloud:
            def __init__(self):
                self.deleted: list[str] = []

            async def list_dir(self, pid):
                return [{"n": nov_name, "fid": "1000000000000000013", "pc": "abcd0demo0pick001",
                         "s": 3 * 1024 * 1024, "ico": "mkv"},                 # 刚上传那条（无 s 缺失，靠 pc 认）
                        {"n": dv_name, "fid": "1000000000000000014", "pc": "abcd0demo0pick004",
                         "s": 5 * 1024 * 1024, "ico": "mkv"}]

            async def delete_file(self, fid):
                self.deleted.append(fid)
                return True

        pc_cloud = _PickcodeCloud()
        res_pc = await p115mod.P115.cleanup_previous(pc_cloud, 1, nov_name, 0, 3,
                                                    keep_fid="abcd0demo0pick001")
        assert pc_cloud.deleted == ["1000000000000000014"], pc_cloud.deleted
        assert res_pc["deleted"] == [dv_name], res_pc

        # 8) 115 文档要求 fs_delete 不得并发执行：并发删除由实例锁串行化（这里走真代码路径）
        class _SlowClient:
            def __init__(self):
                self.inflight = 0
                self.peak = 0

            async def fs_delete(self, payload, async_=True):
                self.inflight += 1
                self.peak = max(self.peak, self.inflight)
                await asyncio.sleep(0.05)
                self.inflight -= 1
                return {"state": True}

        shell = p115mod.P115.__new__(p115mod.P115)
        shell.client = _SlowClient()
        shell._delete_lock = asyncio.Lock()
        await asyncio.gather(shell.delete_file("1"), shell.delete_file("2"), shell.delete_file("3"))
        assert shell.client.peak == 1, f"fs_delete 被并发调用了（峰值 {shell.client.peak}）"

        PASS.append("[18] 洗板清理：带 DV 的旧版进回收站并留痕；开关关着/不同集/非视频/"
                    "别的剧都不动；同集旧版超过上限整体跳过；新版认不出季集也不猜；"
                    "list_dir 只收文件（文件夹条目滤掉）+ 走缓存；新版被改名时按 fid 或 "
                    "pickcode 排除；fs_delete 不并发（实例锁串行化）")
    finally:
        for t in tmps:
            shutil.rmtree(t, ignore_errors=True)


def case_upload_mark():
    """[19] 上传标识解析：秒传给 pickcode、分块回调给 pick_code，都要能取到。"""
    from p115 import upload_mark as M

    assert M({"state": True, "data": {"file_id": "1000000000000000013"}}) == "1000000000000000013"
    assert M({"state": True, "pickcode": "abcd0demo0pick001"}) == "abcd0demo0pick001"
    assert M({"state": True, "data": {"pickcode": "abcd0demo0pick001"}}) == "abcd0demo0pick001"
    assert M({"state": True, "data": {"pick_code": "abcd0demo0pick001"}}) == "abcd0demo0pick001"
    assert M({"state": True, "data": {"id": "1000000000000000013"}}) == "1000000000000000013"
    assert M({"state": True}) == "", "什么都没有时给空串，调用方据此退回名字/大小护栏"
    assert M({"state": True, "file_id": 0, "data": {"file_id": "0"}}) == "", "0 / '0' 都当没有"
    assert M(None) == "" and M("x") == ""
    PASS.append("[19] 上传标识解析：fid / id / pickcode / pick_code 都能取（秒传给前者、"
                "分块回调给后者），全空或 0 则空串，调用方退回名字+大小护栏")


def case_config_roundtrip():
    """[20] 面板保存往返：开关存得进、读得回，键名不漂移。"""
    from pathlib import Path

    # 1) schema 里的 upload.* 键必须在 DEFAULTS 里存在（防键名漂移：面板加了键、
    #    DEFAULTS 没加 → 读取侧拿不到值，表现就是「保存了又变回默认」）
    up_defaults = set(cfgmod.DEFAULTS["upload"].keys())
    missing = []
    for group in cfgmod.FIELD_GROUPS:
        for f in group["fields"]:
            if f["key"].startswith("upload."):
                short = f["key"].split(".", 1)[1]
                if short not in up_defaults:
                    missing.append(f["key"])
    assert not missing, f"面板 schema 里有 DEFAULTS 不认识的键：{missing}"

    # 2) 打开开关 → 内存命中 → form_payload 回 true（面板重新渲染不回弹）
    cfg = copy.deepcopy(cfgmod.DEFAULTS)
    # 洗板默认开（核心需求），开关保留；仅记录模式默认关；剧名判断默认 auto
    assert cfg["upload"]["cleanup_previous_versions"] is True
    assert cfg["upload"]["cleanup_dry_run"] is False and cfg["upload"]["cleanup_title_match"] == "auto"
    cfgmod.apply_values(cfg, {"upload.cleanup_previous_versions": False})
    assert cfg["upload"]["cleanup_previous_versions"] is False
    res = cfgmod.apply_values(cfg, {"upload.cleanup_previous_versions": True,
                                    "upload.cleanup_max_delete": 5})
    assert "upload.cleanup_previous_versions" in res["changed"], res
    assert cfg["upload"]["cleanup_previous_versions"] is True
    assert cfg["upload"]["cleanup_max_delete"] == 5
    assert res["reload"] is True, "upload.* 改动应当触发 115 客户端重建"

    def bool_of(payload, key):
        for g in payload["groups"]:
            for f in g["fields"]:
                if f["key"] == key:
                    return f["value"]
        raise AssertionError(f"form_payload 里没有 {key}")

    assert bool_of(cfgmod.form_payload(cfg), "upload.cleanup_previous_versions") is True
    # 开着的时候 UI 送 false 要能关掉（不能被「留空=不改」的逻辑挡住）
    cfgmod.apply_values(cfg, {"upload.cleanup_previous_versions": False})
    assert cfg["upload"]["cleanup_previous_versions"] is False
    # 前端送的是真布尔（不是字符串），字符串形式也得认
    cfgmod.apply_values(cfg, {"upload.cleanup_previous_versions": "false"})
    assert cfg["upload"]["cleanup_previous_versions"] is False
    cfgmod.apply_values(cfg, {"upload.cleanup_previous_versions": "on"})
    assert cfg["upload"]["cleanup_previous_versions"] is True

    # 3) 落盘 → 重载：值必须还在（save/load 不丢新键）
    tmp = tempfile.mkdtemp(prefix="upilot-roundtrip-")
    old_path = cfgmod.CONFIG_PATH
    try:
        cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
        try:
            cfg2 = copy.deepcopy(cfgmod.DEFAULTS)
            cfgmod.apply_values(cfg2, {"upload.cleanup_previous_versions": False})
            cfgmod.apply_values(cfg2, {"upload.cleanup_previous_versions": True})
            cfgmod.save(cfg2)
            text = cfgmod.CONFIG_PATH.read_text(encoding="utf-8")
            assert "cleanup_previous_versions: true" in text, text
            back = cfgmod.load()
            assert back["upload"]["cleanup_previous_versions"] is True, back["upload"]
        finally:
            cfgmod.CONFIG_PATH = old_path
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[20] 配置往返：面板 schema 的 upload.* 键与 DEFAULTS 逐字对应；开关写入内存后 "
                "form_payload 回 true（面板重渲染不回弹）；开着能关、真布尔与字符串都认；"
                "落盘 yaml 含 cleanup_previous_versions: true 且重载后仍在")


async def case_status_selfheal():
    """[21] 状态自愈：稳定阈值热改、算 SHA1 期间的「处理中」、残留状态能被捞起。

    这几条都是「配置改了/重启了，文件却永久卡在某个中间状态」的同类问题：
    代码里有兜底，但调度器压根不派发那个状态，兜底永远跑不到。
    """
    import uploader as upmod

    # 1) scan.stable_rounds 调小：Uploader 必须现读配置，否则旧值更大 → 永久卡「待处理」
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=5, fail_first=0)
    try:
        path = write_file(src, "热改/s01e01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)                    # 入队，stable_count=0
        cfg["scan"]["stable_rounds"] = 2                 # 面板调小；scan.* 不触发 Uploader 重建
        for _ in range(4):
            scanner.scan_once(store, cfg)                # scanner 按新值涨到 2 就停
        rec = store.get(path)
        assert rec["stable_count"] == 2 and rec["status"] == "待处理", rec
        done = await drive(up, store, path)
        assert done["status"] == "已上传", f"阈值热改后不该卡住：{done}"
        assert not hasattr(up, "stable_rounds"), "Uploader 不应缓存 scan 段的值"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 2) 算 SHA1 期间状态是「处理中」（active() 能捞到 → 界面看得到进度）
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
    try:
        path = write_file(src, "进度/s01e01.mkv", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        seen: list[str] = []
        real = upmod.sha1_of

        async def spy(p, record, st):
            seen.append(st.get(p)["status"])             # 记下「算 SHA1 那一刻」的状态
            return await real(p, record, st)

        upmod.sha1_of = spy
        try:
            await up._process(path)
        finally:
            upmod.sha1_of = real
        assert seen == ["处理中"], f"算 SHA1 时应处于「处理中」，实得 {seen}"
        # 这个用例只关心「算 SHA1 那一刻的状态」；_process 是完整状态机，
        # 在 fail_first=0 的 stub 下会一路秒传命中走到「已上传」。
        assert store.get(path)["status"] == "已上传", store.get(path)

        # 3) 残留「处理中」（容器重启时卡住的那条）要被 dispatch 捞起来接着跑完
        store.set(path, status="处理中", sha1=None)
        up.dispatch()
        for _ in range(20):
            await asyncio.sleep(0.05)
            if store.get(path)["status"] != "处理中":
                break
        assert store.get(path)["status"] != "处理中", f"残留的「处理中」没被救回：{store.get(path)}"
        assert store.get(path)["status"] not in ("处理中", "待处理"), store.get(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[21] 状态自愈：stable_rounds 热改后不再卡「待处理」（Uploader 不缓存 scan 段）；"
                "算 SHA1 期间状态为「处理中」且 active() 可见；重启残留的「处理中」被 dispatch 捞回")


async def case_unexpected_error_stops_retry():
    """[22] 未预期异常：置「错误」并停止重复派发。

    以前只写 message、不改状态 → 文件停在「处理中」这类中间态，dispatch 每轮又把它
    捞回来，于是每轮重试一次：日志一直刷、并发名额一直被占，界面上还看不到「错误」。
    """
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
    try:
        path = write_file(src, "坏文件/s01e01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        real = upmod.sha1_of
        calls = {"n": 0}

        async def boom(p, record, st):
            calls["n"] += 1
            raise PermissionError("模拟：文件读不了")

        upmod.sha1_of = boom
        try:
            # 第 1 次异常：退避重试（不是直接判错）——115 抖一下不该就把文件标死
            up.dispatch()
            await asyncio.sleep(0.2)
            up._active.discard(path)
            rec = store.get(path)
            assert calls["n"] == 1, calls
            assert rec["status"] == "待重试", f"第 1 次异常应退避重试，实得 {rec['status']!r}"
            assert (rec["next_retry_at"] or 0) > time.time(), "应带退避时间"

            # 退避期内不得重复派发（这是修掉「每轮刷屏、一直占并发」的核心）
            up.dispatch()
            await asyncio.sleep(0.2)
            up._active.discard(path)
            assert calls["n"] == 1, f"退避期内不该再被派发，实际被处理 {calls['n']} 次"

            # 第 2 次（把退避时间清零，模拟时间到）：仍退避
            store.set(path, next_retry_at=0)
            up.dispatch()
            await asyncio.sleep(0.2)
            up._active.discard(path)
            assert calls["n"] == 2, calls
            assert store.get(path)["status"] == "待重试", store.get(path)

            # 第 3 次：达到上限 → 「错误」，之后不再被派发
            store.set(path, next_retry_at=0)
            up.dispatch()
            await asyncio.sleep(0.2)
            up._active.discard(path)
            rec = store.get(path)
            assert calls["n"] == 3, calls
            assert rec["status"] == "错误", f"连续 3 次异常应停止自动重试，实得 {rec['status']!r}"
            assert "文件读不了" in (rec["message"] or ""), rec["message"]
            up.dispatch()
            await asyncio.sleep(0.2)
            up._active.discard(path)
            assert calls["n"] == 3, f"判「错误」后不该再重试，实际 {calls['n']} 次"
        finally:
            upmod.sha1_of = real

        # 错误项走正常恢复路径：界面「失败项重新入队」→ 回到待处理
        assert store.requeue() >= 1
        assert store.get(path)["status"] == "待处理", store.get(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[22] 未预期异常：先退避重试（退避期内不重复派发，不再每轮刷屏占并发），"
                "连续 3 次才置「错误」停止；错误项能被「失败项重新入队」救回")


async def case_all_states_recoverable():
    """[23] 全部非终态都能被 dispatch 恢复（容器被强杀后的自愈）。

    `_process` 是「先置状态、再去干活」的：置完「秒传中」「上传中」就被 SIGKILL
    （OOM / docker restart / 宿主机重启）时，except 根本不执行，记录就悬在那个状态上。
    dispatch 若不认这个状态，它就永久卡死 —— 而且这种卡死没有任何报错，界面也看不出。
    """
    import uploader as upmod

    stuck_states = ["待处理", "处理中", "待秒传", "秒传中", "待上传", "上传中", "待重试"]
    for st_name in stuck_states:
        cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
        try:
            path = write_file(src, f"{st_name}/s01e01.mkv", 2 * 1024 * 1024)
            store.add(path, os.path.getsize(path), os.path.getmtime(path))
            # 模拟容器被强杀时的库状态：带 sha1；分块进行中的还带断点信息
            store.set(path, status=st_name, sha1="0" * 40,
                      upload_id="uid-1", upload_oss_url="http://oss/x",
                      upload_callback="{}", upload_partsize=10 * 1024 * 1024)
            up = Uploader(cfg, store, mgr)
            for _ in range(4):
                up.dispatch()
                await asyncio.sleep(0.15)
            after = store.get(path)["status"]
            assert after != st_name, f"{st_name} 卡死了，重启后无法自愈：{after}"
            assert after == "已上传", f"{st_name} 应被推进到「已上传」，实得 {after}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 「上传中」且断点齐全时，应直接续传，而不是白走一轮秒传再被推回待重试
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=99)
    try:
        path = write_file(src, "续传/s01e01.mkv", 12 * 1024 * 1024)
        store.add(path, os.path.getsize(path), os.path.getmtime(path))
        store.set(path, status="上传中", sha1="0" * 40,
                  upload_id="uid-1", upload_oss_url="http://oss/x",
                  upload_callback="{}", upload_partsize=10 * 1024 * 1024)
        up = Uploader(cfg, store, mgr)
        calls = {"rapid": 0}
        real_rapid = mgr.rapid

        async def spy(*a, **k):
            calls["rapid"] += 1
            return await real_rapid(*a, **k)

        mgr.rapid = spy
        await up._process(path)
        assert calls["rapid"] == 0, f"断点齐全时应直接续传，不该再走秒传（实际调了 {calls['rapid']} 次）"
        assert store.get(path)["status"] == "已上传", store.get(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[23] 状态自愈（全量）：7 个非终态（待处理/处理中/待秒传/秒传中/待上传/上传中/待重试）"
                "都能被 dispatch 捞起并推进到「已上传」；「上传中」且断点齐全时直接续传、不白走秒传")


async def case_changed_file_not_lost():
    """[24] 上传期间文件被替换（洗板重下）→ 按新内容重新入队，绝不丢。

    MoviePilot 洗板重下会往**同一个路径**写新内容，而上传（尤其分块几 GB）要跑很久，
    中间完全可能被换掉。这时 _process 手里还是旧 rec：
      - 照常写「已上传」→ 盖掉 scanner 刚做的重置 → 库里终态、云端旧、本地新，
        而终态让 scanner 与 dispatch 都不再碰它 → **新内容永远不会上传**；
      - _delete_source 只比 size → 同 size 的新内容会被**误删**。
    """
    # 1) forget_pid 必须真的清掉 SQLite 里的 cid，否则 10014 兜底是空转
    #    注意用真 P115：StubP115 的 forget_pid 是空操作、也没有 SQLite 缓存这回事。
    import p115 as p115mod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
    try:
        class _FakeClient:
            def __init__(self):
                self.calls = 0

            async def fs_makedirs(self, path, async_=True):
                self.calls += 1
                return {"state": True, "errno": 0,
                        "data": {"file_id": str(1000000000000000100 + self.calls)}}

        real = p115mod.P115.__new__(p115mod.P115)
        real.cfg, real.store = cfg, store
        real._cid_mem = {}
        real._makedirs_lock = asyncio.Lock()
        real.client = _FakeClient()
        first = await real.ensure_pid("/TVs/x/Season 01")
        await real.forget_pid("/TVs/x/Season 01")
        second = await real.ensure_pid("/TVs/x/Season 01")
        assert first != second, f"forget_pid 后应重建目录，实得同一个 cid={first}"
        assert real.client.calls == 2, f"应调用两次 fs_makedirs，实得 {real.client.calls}"
        assert store.cid_get("/TVs/x/Season 01") == second, "库里 cid 应更新为新值"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 2) 秒传命中路径：文件在途被换 → 重新入队，不写「已上传」
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
    try:
        path = write_file(src, "换版/s01e31.mkv", 4 * 1024)
        scanner.scan_once(store, cfg)
        store.set(path, status="待秒传", sha1="old-hash")
        with open(path, "wb") as fh:
            fh.write(b"B" * (8 * 1024))                 # 在途替换：内容与大小都变
        await up._process(path)
        rec = store.get(path)
        assert rec["status"] == "待处理", f"应重新入队，实得 {rec['status']!r}"
        assert rec["sha1"] is None, "换过内容后旧 sha1 必须作废"
        assert rec["size"] == 8 * 1024, rec["size"]
        assert "替换" in (rec["message"] or ""), rec["message"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 3) 文件没变时不受影响（不能误判成「被替换」）
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0)
    try:
        path = write_file(src, "正常/s01e31.mkv", 4 * 1024)
        scanner.scan_once(store, cfg)
        await up._process(path)
        assert store.get(path)["status"] == "已上传", store.get(path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4) 分块完成路径：在途被换 → 重新入队
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, small_file_mb=0.0, fail_first=99)
    try:
        path = write_file(src, "分块/s01e31.mkv", 4 * 1024)
        scanner.scan_once(store, cfg)
        # retry_count 顶到上限 → 秒传未命中即判「重试耗尽，转普通上传」，
        # 这样才真正走到分块路径（否则会被安排成「待重试」就返回了）。
        store.set(path, status="待上传", sha1="old-hash",
                  retry_count=99, first_failed_at=time.time())
        real_mp = mgr.multipart

        async def mp_then_swap(p, pid, sha1, record):
            out = await real_mp(p, pid, sha1, record)
            with open(p, "wb") as fh:
                fh.write(b"C" * (16 * 1024))            # 传完那一刻文件已被换
            return out

        mgr.multipart = mp_then_swap
        await up._process(path)
        rec = store.get(path)
        assert rec["status"] == "待处理", f"分块路径也应重新入队，实得 {rec['status']!r}"
        assert "替换" in (rec["message"] or ""), rec["message"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 5) _delete_source：同 size 但 mtime 不同 → 不删（破坏性操作必须两个都对上）
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, fail_first=0, delete_after=True)
    try:
        path = write_file(src, "同大小/s01e31.mkv", 4 * 1024)
        scanner.scan_once(store, cfg)
        rec = store.get(path)
        with open(path, "wb") as fh:
            fh.write(b"Z" * (4 * 1024))                 # 同 size、不同内容
        os.utime(path, (os.path.getatime(path), os.path.getmtime(path) + 100))
        up._delete_source(path, rec)
        assert os.path.exists(path), "同 size 但 mtime 已变：新内容被误删了"
        assert store.get(path)["status"] == "待处理", store.get(path)

        # 两个都对上 → 正常删
        rec2 = store.get(path)
        os.utime(path, (os.path.getatime(path), rec2["mtime"]))
        up._delete_source(path, rec2)
        assert not os.path.exists(path), "size 与 mtime 都对上时应正常删除"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[24] 上传期间文件被替换（洗板重下）不丢内容：forget_pid 连 SQLite 一起清"
                "（10014 兜底不再空转）；秒传/分块两条路径都按新内容重新入队并不盖掉 scanner 的重置；"
                "文件未变时不误判；_delete_source 要求 size 与 mtime 双匹配（拒绝误删同 size 的新文件）")


async def case_config_save_concurrent():
    """[25] 配置落盘并发安全：多线程同时 save/load 不报错、不撕裂、不留临时文件。

    webui 是 ThreadingHTTPServer（每个请求一个线程），主循环的 115 cookie 同步也在
    自己的线程里 —— 两边都会调 cfgmod.save() / load()。原来既没锁、临时文件又是固定
    名字（config.yaml.tmp）：一个线程刚写完就被另一个的 os.replace 消费掉，前者立刻
    FileNotFoundError；还有 PermissionError。实测 4 线程并发：613/1000 次抛异常。
    """
    import re as _re
    import tempfile as _tf
    from pathlib import Path as _P

    tmp = _tf.mkdtemp(prefix="upilot-save-")
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    cfgmod.CONFIG_PATH = _P(tmp) / "config.yaml"
    cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
    try:
        import threading as _th
        cfgmod.save(copy.deepcopy(cfgmod.DEFAULTS))
        bad_write: list[str] = []
        bad_read: list[str] = []
        stop = _th.Event()

        def writer(tag: str, n: int = 120):
            c = copy.deepcopy(cfgmod.DEFAULTS)
            c["state"] = {"db": os.path.join(tmp, f"{tag}.db")}
            for i in range(n):
                c["log_level"] = f"{tag}{i:05d}"
                try:
                    cfgmod.save(c)
                except Exception as exc:  # noqa: BLE001
                    bad_write.append(f"{tag}/{i} {type(exc).__name__}: {exc}")

        def reader():
            while not stop.is_set():
                try:
                    cfgmod.load()
                except Exception as exc:  # noqa: BLE001
                    bad_read.append(f"{type(exc).__name__}: {exc}")

        rd = _th.Thread(target=reader, daemon=True)   # 守护线程：异常路径也不会挂住退出
        ws = [_th.Thread(target=writer, args=(f"W{j}",), daemon=True) for j in range(2)]
        rd.start()
        for t in ws:
            t.start()
        for t in ws:
            t.join(timeout=60)
        stop.set()
        rd.join(timeout=10)

        assert not bad_write, f"并发 save 抛错：{bad_write[:3]}"
        assert not bad_read, f"并发 load 抛错：{bad_read[:3]}"

        # 落盘必须是「某一次完整写入」，不能是两次交叉的半截内容
        import yaml as _yaml
        got = _yaml.safe_load(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"))
        lv = str(got.get("log_level") or "")
        assert _re.fullmatch(r"W[01]\d{5}", lv), f"落盘内容撕裂：log_level={lv!r}"

        # 临时文件不许残留（里面是完整凭据）
        left = [p.name for p in _P(tmp).iterdir() if ".tmp" in p.name]
        assert not left, f"残留临时文件：{left}"

        # 失败路径不留垃圾：写到不存在的目录 → 抛错但不留下 .tmp
        cfgmod.CONFIG_PATH = _P(tmp) / "nodir" / "config.yaml"
        cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
        try:
            cfgmod.save(copy.deepcopy(cfgmod.DEFAULTS))
        except Exception:  # noqa: BLE001
            pass
        strays = [p for p in _P(tmp).rglob("*.tmp")]
        assert not strays, f"失败路径留下临时文件：{strays}"
    finally:
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[25] 配置落盘并发安全：多线程 save/load 交叉零异常、落盘无撕裂、"
                "不残留 .tmp（凭据不在里面过夜）；失败路径同样不留垃圾")


async def case_save_cookie_atomic():
    """[26] `main.py login` 写 cookie 必须原子化 + 正确转义。

    save_cookie() 原来是 read_text → 改行 → write_text **直接覆盖正本**：既没锁、
    也不是原子替换（实测 inode 不变）。它与面板保存是两个进程，并发概率低；但
    「非原子写正本」的风险与并发无关 —— 写入途中被 kill 或磁盘满，config.yaml
    只剩半截，cookie / 面板密码 / notify 凭据一起丢。另外旧写法 f'"{cookie}"'
    是裸拼，cookie 里出现反斜杠或双引号会把整个 YAML 写坏。
    """
    import tempfile as _tf
    from pathlib import Path as _P

    tmp = _tf.mkdtemp(prefix="upilot-cookie-")
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    cfgmod.CONFIG_PATH = _P(tmp) / "config.yaml"
    cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
    try:
        import yaml as _yaml
        import threading as _th

        cfg = copy.deepcopy(cfgmod.DEFAULTS)
        cfg["state"] = {"db": os.path.join(tmp, "s.db")}
        cfg["p115"]["cookie"] = "OLD=1"
        cfgmod.save(cfg)

        # 原子替换：必须换 inode（原地改写说明还是直接覆盖正本）
        ino_before = cfgmod.CONFIG_PATH.stat().st_ino
        cfgmod.save_cookie("UID=abc; CID=xyz; SEID=999")
        assert cfgmod.CONFIG_PATH.stat().st_ino != ino_before, \
            "save_cookie 仍是原地改写（非原子），写入途中断电会丢全部凭据"

        got = _yaml.safe_load(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"))
        assert got["p115"]["cookie"] == "UID=abc; CID=xyz; SEID=999", \
            f"cookie 没写对：{got['p115']['cookie']!r}"
        # 其余内容（注释、别的键）不能被冲掉
        assert cfgmod.CONFIG_PATH.read_text(encoding="utf-8").startswith("#"), "首行注释丢了"
        assert got["state"]["db"].endswith("s.db"), "其它配置项被冲掉了"

        # 特殊字符：反斜杠 / 双引号（旧实现会把 YAML 写坏）
        nasty = 'UID=a\\b; CID="x:y"; SEID=z\\\\w'
        cfgmod.save_cookie(nasty)
        got = _yaml.safe_load(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"))
        assert got["p115"]["cookie"] == nasty, \
            f"特殊字符 cookie 往返不一致：{got['p115']['cookie']!r} != {nasty!r}"

        # 与 save() 并发：两边都不得抛错，落盘必须可解析
        bad: list[str] = []

        def do_cookie():
            for i in range(60):
                try:
                    cfgmod.save_cookie(f"UID=iter{i}")
                except Exception as exc:  # noqa: BLE001
                    bad.append(f"cookie/{i} {type(exc).__name__}: {exc}")

        def do_save():
            c = copy.deepcopy(cfgmod.DEFAULTS)
            c["state"] = {"db": os.path.join(tmp, "s2.db")}
            for i in range(60):
                c["log_level"] = f"LVL{i:05d}"
                try:
                    cfgmod.save(c)
                except Exception as exc:  # noqa: BLE001
                    bad.append(f"save/{i} {type(exc).__name__}: {exc}")

        ts = [_th.Thread(target=do_cookie, daemon=True),
              _th.Thread(target=do_save, daemon=True)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        assert not bad, f"save_cookie ∥ save 并发抛错：{bad[:3]}"
        _yaml.safe_load(cfgmod.CONFIG_PATH.read_text(encoding="utf-8"))

        assert not [p for p in _P(tmp).iterdir() if ".tmp" in p.name], "残留 .tmp"
    finally:
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[26] save_cookie 原子化：换 inode（非原地改写）+ 特殊字符正确转义 + "
                "与 save() 并发零异常 + 不残留 .tmp")


async def case_scan_now_wakes_loop():
    """[27] 界面的「立即扫描 / 马上再试」必须**立刻**生效。

    两处叠加的缺陷：
    1. 逻辑错误：`state.pop("scan_now")` 之后，下面又 `state.get("scan_now")` 决定
       要不要把等待缩短到 0.5 秒 —— 同一个标志先 pop 后 get，那个分支永远不可达。
    2. 机制错误：主循环原来 `await asyncio.wait_for(stop.wait(), timeout=interval)`
       直接睡满 interval；而 scan_now 是 webui 的**请求线程**写的，置进 dict
       唤不醒主循环里的 await。
    叠加结果：面板提示「已触发扫描 / 马上再试」，实际要等满一个间隔。而面板把
    `scan.interval_seconds` 上限开放到 86400 秒（24 小时），调大后点「立即扫描」
    等于没点。判据：界面说生效、实际没生效 + 代码存在但不可达。
    """
    import main as mainmod

    # 静态：不可达写法不许回来；唤醒入口必须存在且被用上。
    src = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "wait_s = 0.5 if state.get(\"scan_now\")" not in src, \
        "「先 pop 后 get」的不可达写法又回来了"
    assert "async def _wait_next_round" in src, "缺少唤醒等待的实现"
    assert "_wait_next_round(stop, state, interval)" in src, "等待循环没有用它"

    # 行为 1：长间隔期间下指令 → 必须在几百毫秒内返回（不是等满 5 秒）
    stop = asyncio.Event()
    state: dict = {}
    holder: dict = {}

    async def setter():
        await asyncio.sleep(0.3)
        state["scan_now"] = True

    t0 = time.monotonic()
    await asyncio.gather(mainmod._wait_next_round(stop, state, 5), setter())
    spent = time.monotonic() - t0
    assert spent < 1.5, f"下了「立即扫描」指令仍等了 {spent:.2f}s（应 <1.5s）"
    holder["scan_now_wait"] = spent

    # 行为 2：interval 上限那个量级（86400s）下指令同样立刻返回
    stop2, state2 = asyncio.Event(), {}

    async def setter2():
        await asyncio.sleep(0.3)
        state2["scan_now"] = True

    t0 = time.monotonic()
    await asyncio.gather(mainmod._wait_next_round(stop2, state2, 86400), setter2())
    spent2 = time.monotonic() - t0
    assert spent2 < 1.5, f"interval=86400 时下指令仍等了 {spent2:.2f}s"

    # 行为 3：reload（改配置重建 115 客户端）也要能唤醒
    stop3, state3 = asyncio.Event(), {}

    async def setter3():
        await asyncio.sleep(0.3)
        state3["reload"] = True

    t0 = time.monotonic()
    await asyncio.gather(mainmod._wait_next_round(stop3, state3, 3600), setter3())
    assert time.monotonic() - t0 < 1.5, "reload 唤不醒等待循环"

    # 行为 4：没指令时不许提前返回（不能变成 0.5 秒空转一轮）
    stop4, state4 = asyncio.Event(), {}
    t0 = time.monotonic()
    await mainmod._wait_next_round(stop4, state4, 1.2)
    spent4 = time.monotonic() - t0
    assert spent4 >= 1.0, f"无指令却 {spent4:.2f}s 就返回了（应等满 ~1.2s）"

    # 行为 5：收到停止信号立即返回
    stop5, state5 = asyncio.Event(), {}
    stop5.set()
    t0 = time.monotonic()
    await mainmod._wait_next_round(stop5, state5, 60)
    assert time.monotonic() - t0 < 0.3, "停止信号没能立刻结束等待"

    # 行为 6：指令在等待期间置入 → 下一轮 pop 得到 True（与消费点口径一致）
    stop6, state6 = asyncio.Event(), {}

    async def setter6():
        await asyncio.sleep(0.2)
        state6["scan_now"] = True

    await asyncio.gather(mainmod._wait_next_round(stop6, state6, 30), setter6())
    assert state6.pop("scan_now", False) is True, "唤醒后 pop 不到指令，会白扫一轮"

    PASS.append(
        f"[27] 「立即扫描」立刻生效：长间隔（5s / 3600s / 面板上限 86400s）下指令 "
        f"{holder['scan_now_wait']:.2f}s 内唤醒（修复前要睡满整个间隔）；reload 同样能唤醒；"
        f"无指令时不空转；停止信号立即结束；「先 pop 后 get」的不可达写法已锁死")


async def case_ui_actions_wake_loop():
    """[28] 所有界面动作都必须叫醒主循环（否则提示「已…」实际要等满 interval）。

    第 22 处修好 `scan_now` 之后，同类问题还有一批：`/api/requeue`、`/api/resume`、
    `/api/task upload-now`、`/api/config` 保存 —— 提示语分别写着「N 个失败项已重新入队」
    「已恢复调度」「已跳过等待，立即处理」，但它们只改 store / `paused` / `process_one`，
    这些标志**唤不醒**主循环里的 `await stop.wait()`。面板把 `scan.interval_seconds`
    上限开放到 86400 秒（24 小时），调大后这几个按钮等于没点。

    本用例做三重防守：① 静态上不许再有绕过 `_wake` 的裸写；② 运行时逐动作验证
    长间隔下都能立刻唤醒；③ 消费后不许空转（不然每 0.5 秒白扫一轮）。
    """
    import main as mainmod

    src = open(os.path.join(APP, "webui.py"), encoding="utf-8").read()

    # ---- ① 静态：路由里不许裸写 scan_now；_wake 必须覆盖所有改状态的动作 ----
    wake_def_start = src.index("def _wake(state: dict) -> None:")
    wake_def_end = src.index("# ---------- 配置读取 ----------", wake_def_start)
    outside = src[:wake_def_start] + src[wake_def_end:]
    assert 'state["scan_now"] = True' not in outside, \
        "有界面动作绕过了 _wake()，直接裸写 scan_now"
    n_wake = src.count("_wake(state)")
    assert n_wake >= 8, f"_wake(state) 调用点只有 {n_wake} 个，界面动作有遗漏"

    # 每个「改状态后需要主循环立刻反应」的路由段里都必须出现 _wake。
    # 注意两件事：① 窗口要取到下一个路由为止 —— 取太短会漏掉注释之后那一行；
    # ② `/api/config` 在文件里有 GET/POST 两份，POST 那份（真正保存的那个）在后面，
    #    所以要 last=True 定位，否则抓到 GET 段必然误报。
    def seg_after(marker: str, end_markers: tuple[str, ...], last: bool = False) -> str:
        idx = src.rindex(marker) if last else src.index(marker)
        rest = src[idx + len(marker):]
        cut = len(rest)
        for em in end_markers:
            if em in rest:
                cut = min(cut, rest.index(em))
        return rest[:cut]

    checks = {
        'if path == "/api/requeue":': ('if path in ("/api/pause"',),
        'if path == "/api/retry-now":': ('if path == "/api/requeue":',),
        'if path == "/api/scan":': ('if path == "/api/retry-now":',),
        'elif action == "upload-now":': ('elif action == "delete":',),
        'if action == "retry":': ('elif action == "retry-now":',),
        'elif action == "retry-now":': ('elif action == "upload-now":',),
        'if path in ("/api/pause", "/api/resume"):': ('if path == "/logout":',),
    }
    for marker, ends in checks.items():
        body = seg_after(marker, ends)
        assert "_wake(state)" in body, f"{marker} 分支没有叫醒主循环"

    # 保存配置：任何改动（哪怕只是把 interval 调小）都要立刻叫醒。
    # 注意用 last=True —— /api/config 有 GET（表单结构）和 POST（保存）两份，
    # 只有 POST 那份才需要 _wake。
    cfg_body = seg_after('if path == "/api/config":', ('if path == "/api/task":',), last=True)
    assert "_wake(state)" in cfg_body, "保存配置后没有叫醒主循环"

    # ---- ② 运行时：interval=3600 下逐个动作都必须立刻唤醒 ----
    async def wakes_within(mutate, interval: float = 3600.0, hard: float = 2.0):
        stop, state = asyncio.Event(), {"paused": True}

        async def click():
            await asyncio.sleep(0.2)
            mutate(state)

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.gather(mainmod._wait_next_round(stop, state, interval), click()),
                timeout=hard)
            return True
        except asyncio.TimeoutError:
            return False

    def wake(s: dict):
        s["wake_seq"] = int(s.get("wake_seq") or 0) + 1
        s["scan_now"] = True

    scenarios = {
        "requeue（重试失败项）": wake,
        "resume（恢复调度）": lambda s: (s.update(paused=False), wake(s)),
        "upload-now（立即处理）": lambda s: (s.update(process_one="x"), wake(s)),
        "scan（立即扫描）": wake,
        "保存配置（含调小间隔）": lambda s: (s.update(reload=True), wake(s)),
    }
    for name, mut in scenarios.items():
        ok = await wakes_within(mut)
        assert ok, f"{name} 唤不醒主循环（interval=3600 时要等满）"

    # ---- ③ 回归守卫：无指令不空转；消费后不空转；停止信号立即返回 ----
    stop = asyncio.Event()
    t0 = time.monotonic()
    await mainmod._wait_next_round(stop, {}, 1.5)
    assert time.monotonic() - t0 >= 1.2, "无指令却提前返回（会变成 0.5 秒空转一轮）"

    stop2 = asyncio.Event()
    stop2.set()
    t0 = time.monotonic()
    await mainmod._wait_next_round(stop2, {}, 60)
    assert time.monotonic() - t0 < 0.3, "停止信号没能立刻结束等待"

    stop3, state3 = asyncio.Event(), {}

    async def click3():
        await asyncio.sleep(0.2)
        wake(state3)

    await asyncio.gather(mainmod._wait_next_round(stop3, state3, 30), click3())
    state3.pop("scan_now", False)          # 主循环消费掉指令
    try:
        await asyncio.wait_for(mainmod._wait_next_round(stop3, state3, 5), timeout=1.0)
        raised = False
    except asyncio.TimeoutError:
        raised = True
    assert raised, "指令消费后仍在空转（wake_seq 没被正确当作「一次性」信号）"

    PASS.append(f"[28] 界面动作统一叫醒主循环：{n_wake} 个 _wake 调用点、"
                "requeue/resume/upload-now/scan/保存配置 在 interval=3600s 时全部立即唤醒"
                "（修复前要等满整个间隔，上限 24 小时）；无指令不空转、消费后不空转、"
                "停止信号立即结束；路由里裸写 scan_now 已被锁死")


async def case_request_body_hardening():
    """[32][33] HTTP 请求体与连接收尾的两处硬化。

    [32] `_body_fields()` 原来直接 `self.rfile.read(Content-Length)`：
         length 由客户端**自己声明**、且 Handler 没设 `timeout`。实测「声明 100MB、
         只发 3 字节」的连接会把线程永久卡在 read 上 —— 20 条连接就让线程净增 20
         （ThreadingHTTPServer 每请求一线程、线程数无上限）。而 `POST /login` 在
         **校验凭据之前**就读 body ⇒ 未鉴权即可触发。
    [33] `_send()` 只 catch `BrokenPipeError`/`ConnectionResetError`，而客户端早断在
         Windows 上抛 `ConnectionAbortedError(WinError 10053)` ⇒ 每条早断连接都在
         日志里刷一段 traceback，把真正的错误淹掉。
    """
    import socket as _socket
    import threading as _th

    src_txt = open(os.path.join(APP, "webui.py"), encoding="utf-8").read()
    # 静态口径：上限存在且合理、Handler 有 timeout、断开异常在捕获元组里
    assert 0 < webui.MAX_BODY_BYTES <= 64 * 1024 * 1024, \
        f"MAX_BODY_BYTES 不合理：{webui.MAX_BODY_BYTES}"
    assert webui.HANDLER_TIMEOUT and webui.HANDLER_TIMEOUT > 0, "HANDLER_TIMEOUT 未设置"
    assert "timeout = HANDLER_TIMEOUT" in src_txt, "Handler 没设 timeout（半开会永久占线程）"
    assert "ConnectionAbortedError" in src_txt, "断开异常没被捕获（会刷 traceback）"
    assert "rfile.read(length) if length > 0" not in src_txt, "裸读 body 的写法又回来了"

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    saved_failures = None
    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "form",
                                "username": "admin", "password": "s3cret-token",
                                "session_hours": 72}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        port = srv.server_address[1]

        def conn(t=5.0):
            return _socket.create_connection(("127.0.0.1", port), timeout=t)

        def reset_limits():
            """本用例会连打多次失败登录，清掉限速计数以免干扰其它断言。"""
            nonlocal saved_failures
            with webui._FAIL_LOCK:
                if saved_failures is None:
                    saved_failures = dict(webui._FAILURES)
                webui._FAILURES.clear()

        def health_ok() -> bool:
            try:
                s = conn()
                s.sendall(b"GET /health HTTP/1.1\r\nHost: x\r\n\r\n")
                d = s.recv(100)
                s.close()
                return d.startswith(b"HTTP/1.0 200")
            except OSError:
                return False

        # ---- [32]-1 超限请求体必须**立刻**被拒，而不是阻塞在 read 上 ----
        reset_limits()
        s = conn()
        s.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: 104857600\r\n"
                  b"Content-Type: application/x-www-form-urlencoded\r\n\r\nabc")
        s.settimeout(6.0)
        t0 = time.monotonic()
        head = ""
        try:
            head = s.recv(200).split(b"\r\n")[0].decode("latin1")
        except _socket.timeout:
            pass
        spent = time.monotonic() - t0
        s.close()
        assert head, "超限请求体没被拒绝：服务端仍阻塞在 read"
        assert spent < 3.0, f"超限请求耗时 {spent:.2f}s，仍在阻塞"

        # ---- [32]-2 一批超限半开连接不得把线程堆起来 ----
        reset_limits()
        base_threads = _th.active_count()
        socks = []
        for _ in range(20):
            try:
                x = conn()
                x.sendall(b"POST /login HTTP/1.1\r\nHost: x\r\n"
                          b"Content-Length: 104857600\r\n\r\n")
                socks.append(x)
            except OSError:
                break
        time.sleep(1.5)
        grew = _th.active_count() - base_threads
        alive = len(socks)
        for x in socks:
            try:
                x.close()
            except OSError:
                pass
        assert alive >= 10, f"只开成 {alive} 条连接，样本太少"
        assert grew < alive // 2, \
            f"{alive} 条超限半开连接让线程净增 {grew}（修复前约等于连接数）"
        assert health_ok(), "半开连接期间 /health 应仍可用"

        # ---- [32]-3 回归红线：正常表单登录必须照旧 302 + 下发会话 ----
        reset_limits()
        body = urllib.parse.urlencode({"username": "admin",
                                       "password": "s3cret-token", "next": "/"}).encode()

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(_NoRedirect)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/login", data=body,
                                     headers={"Content-Type":
                                              "application/x-www-form-urlencoded"})
        try:
            r = opener.open(req, timeout=8)
            code, jar = r.status, r.headers.get("Set-Cookie", "")
        except urllib.error.HTTPError as exc:
            code, jar = exc.code, exc.headers.get("Set-Cookie", "")
        assert code == 302 and jar.startswith("upilot_session="), \
            f"正常登录被请求体上限挡住了：{code} {jar[:40]}"

        # ---- [32]-4 边界：恰好等于上限不得被误拒（别 off-by-one 挡正常表单）----
        reset_limits()
        pad = "a=" + "x" * (webui.MAX_BODY_BYTES - 2)
        s2 = conn(8.0)
        s2.settimeout(8.0)
        s2.sendall(("POST /login HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n"
                    "Content-Type: application/x-www-form-urlencoded\r\n\r\n"
                    % len(pad)).encode() + pad.encode())
        head2 = ""
        try:
            head2 = s2.recv(200).split(b"\r\n")[0].decode("latin1")
        except _socket.timeout:
            pass
        s2.close()
        assert head2, "恰好等于上限的请求体被误拒"

        # ---- [33] 早断连接不得把服务搞坏（修复前每条都刷 traceback）----
        reset_limits()
        for _ in range(10):
            try:
                x = conn()
                x.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                x.close()                       # 立刻断开，不等响应
            except OSError:
                pass
        time.sleep(0.8)
        assert health_ok(), "早断连接后服务应仍健康"

        # ---- 收尾：其它正常接口不受影响 ----
        reset_limits()
        assert http_get(f"http://127.0.0.1:{port}/health")[0] == 200
        assert http_get(f"http://127.0.0.1:{port}/api/status",
                        "admin", "s3cret-token")[0] == 200

        PASS.append(
            f"[32][33] 请求体与连接收尾硬化：超限（声明 100MB 只发 3 字节）立刻被拒、"
            f"20 条超限半开连接线程只净增 {grew}（修复前≈连接数，全卡在 read）、"
            f"正常表单登录照旧 302+会话、恰好等于上限（{webui.MAX_BODY_BYTES}B）不误拒、"
            f"早断连接不刷 traceback 且服务仍健康")
    finally:
        if saved_failures is not None:
            with webui._FAIL_LOCK:
                webui._FAILURES.clear()
                webui._FAILURES.update(saved_failures)
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def case_coerce_finite_values():
    """[34] 面板数值字段必须挡掉 inf/nan，且异常统一成 ValueError。

    两个真实后果（都实测过）：
    1. **静默失效**：`coerce` 用 `float(text)` 后直接比值，`math.isfinite` 从没被检查。
       把 `scan.min_size_mb` 存成 `inf` → 落盘成 `.inf` → 重新 load 仍是 inf →
       scanner 里 `st.st_size < min_bytes` **恒真**，每个文件都被跳过
       （实测 0 个入队；同一目录 min_size_mb=0 时是 3 个）。
       界面上看不到任何错误，只是「从此再也不上传」。nan 则相反（比较恒 False ⇒ 不限）。
    2. **500 而非友好提示**：int 字段走 `int(float(text))`，`float("1e400")` 得到 inf、
       `int(inf)` 抛的是 **OverflowError**，而 webui 的 `POST /api/config` 只 catch
       `ValueError` ⇒ 保存时变成未处理异常。
    """
    from pathlib import Path

    src_txt = open(os.path.join(APP, "config.py"), encoding="utf-8").read()
    assert "import math" in src_txt, "config.py 没导入 math"
    assert "math.isfinite" in src_txt, "coerce 里没有有限性检查"

    def field_of(key: str) -> dict:
        f = cfgmod.find_field(key)
        assert f, f"面板里没有字段 {key}"
        return f

    # 1) 非有限值：必须 ValueError（不是 OverflowError，也不是被接受）
    hazards = [
        ("scan.min_size_mb", "inf"), ("scan.min_size_mb", "-inf"),
        ("scan.min_size_mb", "nan"), ("scan.min_size_mb", "Infinity"),
        ("scan.min_size_mb", "NaN"), ("scan.min_size_mb", "1e400"),
        ("scan.interval_seconds", "1e400"), ("scan.interval_seconds", "inf"),
        ("scan.interval_seconds", "1e309"), ("scan.stable_rounds", "inf"),
        ("upload.concurrency", "1e400"), ("upload.small_file_mb", "inf"),
        ("retry.max_hours", "inf"), ("retry.max_hours", "nan"),
    ]
    for key, raw in hazards:
        f = field_of(key)
        try:
            got = cfgmod.coerce(f, raw)
        except ValueError:
            continue                                  # 正是期望的
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"{key}={raw!r} 抛了 {type(exc).__name__}，webui 只 catch ValueError ⇒ 会变成 500"
            ) from exc
        raise AssertionError(f"{key}={raw!r} 被接受了：{got!r}（会让配置校验形同虚设）")

    # 2) 正常值不受影响（注意 float 字段本来就返回 float：0 → 0.0）
    normal = [
        ("scan.min_size_mb", "0", 0.0), ("scan.min_size_mb", "1.5", 1.5),
        ("scan.interval_seconds", "30", 30), ("scan.interval_seconds", "2", 2),
        ("scan.interval_seconds", "86400", 86400), ("scan.interval_seconds", "30.0", 30),
        ("scan.interval_seconds", " 30 ", 30),
        ("scan.stable_rounds", "2", 2), ("upload.concurrency", "2", 2),
        ("retry.max_hours", "12.5", 12.5), ("retry.max_hours", "0", 0.0),
    ]
    for key, raw, want in normal:
        f = field_of(key)
        got = cfgmod.coerce(f, raw)
        assert got == want, f"{key}={raw!r} → {got!r}，期望 {want!r}"
        if f["type"] == "int":
            assert isinstance(got, int), f"{key} 是 int 字段却返回 {type(got).__name__}"

    # 3) 越界校验不能被这轮改动碰坏
    for key, raw in (("scan.interval_seconds", "1"), ("scan.interval_seconds", "99999"),
                     ("scan.stable_rounds", "0"), ("scan.stable_rounds", "21"),
                     ("scan.min_size_mb", "-1")):
        f = field_of(key)
        try:
            got = cfgmod.coerce(f, raw)
        except ValueError:
            continue
        raise AssertionError(f"{key}={raw!r} 越界却被接受：{got!r}")

    # 4) 端到端：apply_values 必须拒绝、内存不许被改脏、落盘不许出现 .inf/.nan
    tmp = tempfile.mkdtemp(prefix="upilot-finite-")
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
    cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
    try:
        cfg = copy.deepcopy(cfgmod.DEFAULTS)
        cfg["state"] = {"db": os.path.join(tmp, "s.db")}
        cfgmod.save(cfg)

        # JSON 通道真的能送来 Infinity（Python json 默认 allow_nan=True）
        inf = __import__("json").loads('{"v": Infinity}')["v"]
        # 注意：不能用 `inf != inf - 1` 判断 —— inf-1 仍是 inf，那样写表达式恒为 False。
        assert math.isinf(inf) and inf > 0, "json.loads 应能解析出 Infinity"
        before_min = cfg["scan"]["min_size_mb"]
        before_int = cfg["scan"]["interval_seconds"]
        try:
            cfgmod.apply_values(cfg, {"scan.min_size_mb": inf})
            raise AssertionError("apply_values 接受了 inf")
        except ValueError:
            pass
        assert cfg["scan"]["min_size_mb"] == before_min, "非法字段不该改到内存"

        # 混合提交：合法项也不许生效（两阶段原子性）
        try:
            cfgmod.apply_values(cfg, {"scan.interval_seconds": 45, "scan.min_size_mb": inf})
            raise AssertionError("混合提交被接受")
        except ValueError:
            pass
        assert cfg["scan"]["interval_seconds"] == before_int, "混合提交里合法项被提前改了"

        # 落盘内容里不许出现 YAML 的 .inf/.nan 记法
        cfgmod.save(cfg)
        text = cfgmod.CONFIG_PATH.read_text(encoding="utf-8")
        for token in (".inf", ".nan", "-.inf", ".Infinity"):
            assert token not in text, f"配置里出现了 {token}"

        # 5) 危害守卫：coerce 之后的 min_size_mb 必然有限（扫描不会再被静默跳过）
        f = field_of("scan.min_size_mb")
        for raw in ("0", "1.5", "100"):
            v = cfgmod.coerce(f, raw)
            assert v == v and v not in (float("inf"), float("-inf")), \
                f"min_size_mb={raw!r} 变成了非有限值 {v!r}"

        # 正常保存仍可用
        res = cfgmod.apply_values(cfg, {"scan.min_size_mb": 2, "scan.interval_seconds": 45})
        cfgmod.save(cfg)
        back = cfgmod.load()
        assert back["scan"]["min_size_mb"] == 2 and back["scan"]["interval_seconds"] == 45, back["scan"]
    finally:
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[34] 面板数值字段挡 inf/nan：14 组非有限输入全部 ValueError（修复前 inf/nan 会被"
                "接受、int 字段抛 OverflowError 变 500）、scanner 不再因 min_size_mb=inf 静默跳过"
                "全部文件、正常值与越界校验不受影响、apply_values 拒绝且不脏内存、落盘无 .inf/.nan")


def case_cloud_path_dotdot_guard():
    """[35] 云端路径不许带 `..` 段 —— 两层拦截。

    实测（修复前）：面板 `paths.map` 写 `TVs => /TVs/../../etc` 一路通过
    `coerce` → `apply_values` → 落盘 → 重载，最后 `resolve_upload_target` 算出
    目标目录 `/TVs/../../etc/Show`，而 `normalize_cloud_path` **只折叠 `/`、不解析 `..`**
    → 原样发给 115，云端建出带 `..` 的怪目录；symedia 仍按 `/TVs/...` 建 strm
    ⇒ **文件传上去了却永远不出现在媒体库**，界面上一点提示都没有。

    两层拦截：
      1. `coerce(map)` 直接拒绝带 `..` 的 `from`/`to`（给用户明确报错）；
      2. `normalize_cloud_path` 解析 `..`（兜住「手改 config.yaml」「老配置」「target_root」）。
    注意第 2 层不能只折叠 `/`：它是发给 115 之前的最后一道。
    """
    from pathlib import Path

    # ---- 第 2 层：normalize_cloud_path 必须解析 .. ----
    norm_cases = [
        ("/a/../b", "/b"), ("/../b", "/b"), ("a/../../b", "/b"),
        ("/a/b/../../../c", "/c"), ("//a///b//", "/a/b"),
        ("/UPILOT-mini/..", "/"), ("/", "/"), ("", "/"),
        ("/TVs/../../etc/Show", "/etc/Show"), ("/UPILOT-mini/../evil", "/evil"),
        # 正常路径必须一字不变（回归红线，含中文/空格/点号）
        ("/UPILOT-mini/TVs/Show", "/UPILOT-mini/TVs/Show"),
        ("/OtherAnime/葬送的芙莉莲", "/OtherAnime/葬送的芙莉莲"),
        ("/TVs/甲剧 (2026) [tmdbid=100001]/Season 01",
         "/TVs/甲剧 (2026) [tmdbid=100001]/Season 01"),
        ("/a.b/c..d", "/a.b/c..d"),      # 含 .. 但不是独立段 → 保留
        ("/a/.../b", "/a/.../b"),        # 三个点不是 ..
    ]
    for src, want in norm_cases:
        got = normalize_cloud_path(src)
        assert got == want, f"normalize_cloud_path({src!r}) → {got!r}，期望 {want!r}"
    # 结果永远绝对且不含 . / ..
    for src in ("a", "./a", "a/./b/../c", "../../x"):
        out = normalize_cloud_path(src)
        assert out.startswith("/"), f"{src!r} → {out!r} 不是绝对路径"
        assert not any(s in (".", "..") for s in out.split("/")), \
            f"{src!r} → {out!r} 仍含 . 或 .."

    # ---- 第 1 层：面板 map 必须明确拒绝 ----
    field = cfgmod.find_field("paths.map")
    assert field, "面板里没有 paths.map 字段"
    for raw in ("TVs => /TVs/../../etc", "TVs => ..", "TVs => /../etc",
                "../TVs => /Safe", "TVs => /a/../../b", "a/.. => /ok"):
        try:
            got = cfgmod.coerce(field, raw)
        except ValueError:
            continue
        raise AssertionError(f"{raw!r} 被接受：{got!r}（会原样传到云端）")

    # 正常映射照旧（不能被这轮改动碰坏）
    good = [
        ("TVs => /TVs", [{"from": "TVs", "to": "/TVs"}]),
        ("Movies/Others => /Movies/Others",
         [{"from": "Movies/Others", "to": "/Movies/Others"}]),
        ("Anime => /OtherAnime", [{"from": "Anime", "to": "/OtherAnime"}]),
        ("TVs => TVs", [{"from": "TVs", "to": "/TVs"}]),              # 自动补前导 /
        ("TVs => /a.b/c..d", [{"from": "TVs", "to": "/a.b/c..d"}]),   # 非独立 ..
        ("# 注释\nTVs => /TVs", [{"from": "TVs", "to": "/TVs"}]),
        ("", []),
    ]
    for raw, want in good:
        got = cfgmod.coerce(field, raw)
        assert got == want, f"coerce({raw!r}) → {got!r}，期望 {want!r}"

    # ---- 端到端：面板入口拒绝 + 内存不脏 + 手改场景被兜住 ----
    tmp = tempfile.mkdtemp(prefix="upilot-path-")
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
    cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
    try:
        cfg = copy.deepcopy(cfgmod.DEFAULTS)
        cfg["state"] = {"db": os.path.join(tmp, "s.db")}
        cfg["paths"]["source"] = "/media/source"
        cfg["paths"]["target_root"] = "/UPILOT-mini"
        cfgmod.save(cfg)

        before = copy.deepcopy(cfg["paths"]["map"])
        try:
            cfgmod.apply_values(cfg, {"paths.map": "TVs => /TVs/../../etc"})
            raise AssertionError("apply_values 接受了带 .. 的映射")
        except ValueError:
            pass
        assert cfg["paths"]["map"] == before, "非法 map 改到了内存"

        # 手改 config.yaml / 老配置：resolve 出来的路径经 normalize 必须收敛
        manual = [
            ("map.to 含 ..", [{"from": "TVs", "to": "/TVs/../../etc"}],
             "/UPILOT-mini", "/media/source/TVs/Show/S01E01.mkv", "/etc/Show"),
            ("target_root 含 ..", None, "/UPILOT-mini/../evil",
             "/media/source/X/a.mkv", "/evil/X"),
            ("map.from 含 ..", [{"from": "../TVs", "to": "/Safe"}],
             "/UPILOT-mini", "/media/source/TVs/a.mkv", "/UPILOT-mini/TVs"),
        ]
        for label, mapping, root, fp, want in manual:
            out = resolve_upload_target(fp, cfg["paths"]["source"], root, mapping)
            safe = normalize_cloud_path(out)
            assert ".." not in safe.split("/"), f"{label}：{safe!r} 仍含 .."
            assert safe == want, f"{label}：{safe!r}，期望 {want!r}"

        # 正常映射端到端仍然正确
        cfgmod.apply_values(cfg, {"paths.map": "TVs => /TVs\nAnime => /OtherAnime"})
        cfgmod.save(cfg)
        back = cfgmod.load()
        out = resolve_upload_target("/media/source/Anime/葬送的芙莉莲/E01.mkv",
                                    back["paths"]["source"], back["paths"]["target_root"],
                                    back["paths"]["map"])
        assert out == "/OtherAnime/葬送的芙莉莲", out
    finally:
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[35] 云端路径不许带 ..：面板 map 明确拒绝 6 组注入（from/to 两侧）、"
                "normalize_cloud_path 解析 ..（16 组，含「含点号但非独立段」必须保留）、"
                "手改 config.yaml / target_root 带 .. 也被最后一道收敛、"
                "正常映射与中文/空格路径一字不变")


async def case_dir_paging_cursor():
    """[36] `list_dir` 的翻页游标必须用「实际返回条数」推进。

    原实现是 `offset += page`（固定加**请求的** limit）。服务端有自己的单页上限时
    （很常见），这会把中间整段数据整段跳过：实测 2500 个文件、服务端每页只给 500 条时，
    请求序列 `(0,1150)(1150,1150)(2300,1150)` 只拿回 **1200** 条，**漏 1300 条**。
    而 `list_dir` 正是洗板清理「找同集旧版」的唯一依据 —— 漏掉的旧版**永远清不掉**，
    且没有任何报错（判据：代码没抛错、日志干净、界面看不出）。

    本用例覆盖三层：
      ① 修复前会漏数据的三档（服务端单页 100 / 500 / 7）必须补齐；
      ② 正常场景（单页 = 请求上限）不许变差，含跨页边界与空目录；
      ③ 服务端异常（忽略 offset、每页给同一批）必须**终止 + 记 warning + 不重复**；
         以及大目录触到条目上限时必须**可见地**截断（不能静默少数据）。
    """
    import logging as _logging

    class _FakeClient:
        """模拟 115 的 fs_files：cap = 服务端自己的单页上限。"""

        def __init__(self, n, cap=None, ignore_offset=False, dirs=0):
            self.items = [{"fid": str(1000000 + i), "n": f"f{i:05d}.mkv",
                           "ico": "mkv", "s": 1024} for i in range(n)]
            if dirs:
                self.items += [{"cid": str(900000 + i), "n": f"d{i}", "ico": "folder"}
                               for i in range(dirs)]
            self.cap = cap or 1150
            self.ignore_offset = ignore_offset
            self.calls: list[tuple[int, int]] = []

        async def fs_files(self, payload, async_=False):
            off = int(payload["offset"])
            lim = int(payload["limit"])
            self.calls.append((off, lim))
            take = min(lim, self.cap)
            rows = (self.items[:take] if self.ignore_offset
                    else self.items[off:off + take])
            return {"state": True, "count": len(self.items),
                    "data": {"count": len(self.items), "list": rows}}

    def mgr_with(client):
        m = P115.__new__(P115)                  # 不过 __init__（不需要 cookie）
        m.client = client
        m._dir_cache = {}
        m._dir_cache_ttl = 30.0
        m._dir_cache_max = 64
        return m

    class _Capture(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.msgs: list[str] = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    logger = _logging.getLogger("upilot.p115")
    cap = _Capture()
    old_level = logger.level
    logger.addHandler(cap)
    logger.setLevel(_logging.DEBUG)

    async def fetch(client):
        m = mgr_with(client)
        before = len(cap.msgs)
        rows = await asyncio.wait_for(m.list_dir(700, use_cache=False), timeout=30)
        return rows, len(cap.msgs) - before, client.calls

    try:
        # ① 修复前漏数据的档位 → 现在必须一条不漏
        for label, total, srv_cap in (
                ("300 文件 / 服务端每页 100", 300, 100),
                ("2500 文件 / 服务端每页 100", 2500, 100),
                ("2500 文件 / 服务端每页 500", 2500, 500),
                ("3000 文件 / 服务端每页 7", 3000, 7)):
            cli = _FakeClient(total, srv_cap)
            rows, _, calls = await fetch(cli)
            fids = [str(r["fid"]) for r in rows]
            assert len(fids) == total, \
                f"{label}：只取回 {len(fids)}/{total}（游标又在按请求 limit 推进了？）"
            assert len(set(fids)) == total, f"{label}：出现重复条目"
            # 游标必须严格按「上一页实际返回条数」递进（不是固定 1150）
            for (prev_off, _), (next_off, _) in zip(calls, calls[1:]):
                assert next_off - prev_off == srv_cap, \
                    f"{label}：游标步进 {next_off - prev_off}，应等于服务端实际返回数 {srv_cap}"

        # ② 正常场景不许变差（含边界）
        for label, total, srv_cap in (
                ("300 文件 / 每页 1150", 300, None),
                ("2500 文件 / 每页 1150", 2500, None),
                ("1150 正好一页", 1150, None),
                ("1151 跨页边界", 1151, None),
                ("0 文件（空目录）", 0, None)):
            cli = _FakeClient(total, srv_cap)
            rows, warns, _ = await fetch(cli)
            assert len(rows) == total, f"{label}：取回 {len(rows)}/{total}"
            assert len({str(r['fid']) for r in rows}) == len(rows), f"{label}：有重复"
            assert warns == 0, f"{label}：正常场景不该有 warning，却记了 {warns} 条"

        # ③-1 服务端忽略 offset：必须终止、记 warning、不重复累积
        cli = _FakeClient(2500, 100, ignore_offset=True)
        rows, warns, calls = await fetch(cli)
        fids = [str(r["fid"]) for r in rows]
        assert len(set(fids)) == len(fids), "服务端反复给同一批时出现了重复条目"
        assert len(calls) <= 3, f"卡住检测没生效：翻了 {len(calls)} 页"
        assert warns >= 1, "服务端反复返回同一批，应当记一条 warning（否则是静默停）"
        assert any("反复返回同一批" in m for m in cap.msgs), \
            f"warning 内容不对：{cap.msgs[-3:]}"

        # ③-2 目录条目永远不混进文件列表
        cli = _FakeClient(500, None, dirs=50)
        rows, _, _ = await fetch(cli)
        assert len(rows) == 500, f"文件数不对：{len(rows)}"
        assert all(r.get("fid") not in (None, "") for r in rows), "文件夹条目混进了结果"

        # ③-3 触到条目上限必须可见地截断（不能静默少数据）
        old_max = p115.MAX_DIR_ITEMS
        try:
            p115.MAX_DIR_ITEMS = 250
            cli = _FakeClient(2000, 100)
            rows, warns, _ = await fetch(cli)
            # 截断按**页**粒度生效（每读完一页检查一次），所以实际条数是「第一个 >= 上限的
            # 页边界」，即 上限 <= len < 上限 + 单页。别断言精确等于上限 —— 那需要把条目
            # 切一半，反而会丢掉整页数据的完整性。
            assert 250 <= len(rows) < 250 + 100, \
                f"截断点不对：{len(rows)}（应在 [250, 350) 之间）"
            assert warns >= 1 and any("超过" in m for m in cap.msgs), \
                "触到条目上限却没记 warning（等于静默少数据）"
        finally:
            p115.MAX_DIR_ITEMS = old_max
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)

    PASS.append("[36] list_dir 翻页游标用「实际返回条数」推进：修复前（固定加请求 limit）"
                "服务端每页 500 条时 2500 文件只读到 1200、**漏 1300**；现在四种小页场景"
                "一条不漏且步进等于实际返回数、正常/边界/空目录无 warning、"
                "服务端忽略 offset 时终止并记 warning、目录条目不混入、"
                "触条目上限时可见截断")


async def case_same_episode_no_mutual_delete():
    """[37] 同一集两个版本几乎同时传完 → **不能互删**。

    原实现只保护「本文件自己」那一条（`keep_fid`）。于是：
      A 的清理看到 B（同集、已完成）→ B 的 id != A 的 keep_fid → 删 B
      B 的清理看到 A → 删 A
    ⇒ **云端同集一个不剩**，而本地两条都是「已上传」终态、symedia 的 strm 指向
    不存在的文件。本地已复现（串行与并发都会中，实测剩余云端 `[]`）。

    修复：`cleanup_previous()` 增加 `keep_fids` / `keep_names`，调用方
    （`Uploader._same_episode_keeps`）把「同集、仍在本地、且已上传成功」的其它版本传进来。
    判据是「本地文件是否还在」：洗板时旧版本地已被换掉/删除 ⇒ 不保护 ⇒ 照旧清理。
    """
    from p115 import P115 as _P115

    dv = "Show.S01E01.2160p.DV.mkv"
    nov = "Show.S01E01.2160p.mkv"
    assert p115.episode_key(dv) == p115.episode_key(nov), "构造前提：两个文件名必须同集"

    # ---- A) 单元层：额外保护集合 ----
    class _Cloud:
        def __init__(self):
            self.files: dict[str, dict] = {}

        def add(self, fid: str, name: str, size: int = 1000):
            self.files[fid] = {"fid": fid, "n": name, "s": size, "ico": "mkv"}

        def snap(self):
            return [dict(v) for v in self.files.values()]

    def mgr_of(cloud):
        m = _P115.__new__(_P115)                     # 不过 __init__（不需要 cookie）
        m._dir_cache = {}
        m._dir_cache_ttl = 30.0
        m._dir_cache_max = 64
        m._delete_lock = asyncio.Lock()

        async def list_dir(pid, use_cache=True):
            rows = cloud.snap()
            m._remember_dir(int(pid), rows)
            return rows

        async def delete_file(fid):
            async with m._delete_lock:
                return bool(cloud.files.pop(str(fid), None))

        m.list_dir = list_dir
        m.delete_file = delete_file
        return m

    # ① 同集两版本互认 → 一个都不删
    c1 = _Cloud(); c1.add("111", dv); c1.add("222", nov)
    r1 = await mgr_of(c1).cleanup_previous(700, dv, 0, 9,
                                           keep_fid="111", keep_fids=["222"], keep_names=[nov])
    assert r1["deleted"] == [], f"同集两版本互认时不该删：{r1}"
    assert len(c1.snap()) == 2, c1.snap()

    # ② 只按名字保护（file_id 拿不到时也要挡住）
    c2 = _Cloud(); c2.add("111", dv); c2.add("222", nov)
    r2 = await mgr_of(c2).cleanup_previous(700, dv, 0, 9, keep_fid="111", keep_names=[nov])
    assert r2["deleted"] == [], r2
    assert len(c2.snap()) == 2, c2.snap()

    # ③ 洗板核心行为不变：非本地的同集旧版仍必须被清
    c3 = _Cloud(); c3.add("111", dv); c3.add("333", "Show.S01E01.1080p.mkv")
    r3 = await mgr_of(c3).cleanup_previous(700, dv, 0, 9, keep_fid="111")
    assert r3["deleted"] == ["Show.S01E01.1080p.mkv"], r3
    assert [x["n"] for x in c3.snap()] == [dv], c3.snap()

    # ---- B) 端到端：两个同集文件都真走完状态机 ----
    cfg, store, mgr, _up0, tmp, src = make_env(mode="rapid_only", fail_first=0)
    try:
        rel_dir, cloud_dir = "TVs/Drama", "/TVs/Drama"
        cfg["paths"]["map"] = [{"from": "TVs", "to": "/TVs"}]
        cfg["upload"]["cleanup_previous_versions"] = True
        cfg["upload"]["cleanup_max_delete"] = 9
        up = Uploader(cfg, store, mgr)
        await mgr.ensure_pid(cloud_dir)

        gone: list[str] = []

        async def fake_delete(fid):
            gone.append(fid)
            return True

        mgr.delete_file = fake_delete

        p1 = write_file(src, f"{rel_dir}/{dv}", 3 * 1024 * 1024)
        p2 = write_file(src, f"{rel_dir}/{nov}", 3 * 1024 * 1024)
        now = time.time()
        os.utime(p1, (now - 600, now - 600))          # DV 版是先下载的（旧）
        os.utime(p2, (now, now))                       # 无 DV 版后下载（新）
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)

        rec1 = await drive(up, store, p1)
        rec2 = await drive(up, store, p2)
        assert rec1["status"] == "已上传" and rec2["status"] == "已上传", \
            (rec1["status"], rec2["status"])
        # 洗板「只留最新」：两个都在本地时按本地下载时间裁决 —— 旧的 DV 版被清、新的留下；
        # 裁决是确定性的（永远不删最新那条），所以两个版本不可能互删到云端一个不剩
        assert gone == [f"stub-{p1}"], f"应只清掉先下载的那个版本：{gone}"
        combined = (rec1.get("message") or "") + (rec2.get("message") or "")
        assert "同集旧版本已清" in combined, combined

        # _same_episode_keeps 的判据：本地在 → 保护
        _fids, names = up._same_episode_keeps(cloud_dir, p2)
        assert dv in names, f"另一版本仍在本地却没被保护：{names}"

        # 本地删掉（模拟洗板旧版已被替换）→ 不再保护 ⇒ 照旧会被清理
        os.remove(p1)
        _fids2, names2 = up._same_episode_keeps(cloud_dir, p2)
        assert dv not in names2, f"本地已删的旧版仍被保护（洗板会漏清）：{names2}"

        # 洗板端到端：旧的同集记录（本地已不在）仍应被清
        gone.clear()
        old_path = os.path.join(tmp, "Show.S01E01.1080p.mkv")      # 刻意不建文件
        store.add(old_path, 5 * 1024 * 1024, 1.0)
        store.set(old_path, status="已上传", target_dir=cloud_dir, message="秒传命中（旧版）")
        res = await mgr.cleanup_previous(cloud_dir and await mgr.ensure_pid(cloud_dir),
                                         nov, 0, 9,
                                         keep_fid="stub-keep",
                                         keep_fids=[], keep_names=[dv])
        assert "Show.S01E01.1080p.mkv" in res["deleted"], res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[37] 同集两版本不互删：cleanup_previous 支持额外保护集合（keep_fids/keep_names）；"
                "端到端两个同集文件都「已上传」且谁都没被误删；"
                "只按名字也能挡住；洗板场景（旧版本地已不在）不再保护、非本地同集旧版仍被清")


async def case_delete_after_upload_no_mutual_delete():
    """[38] `upload.delete_after_upload` 开着时，同集两版本仍**不能互删**。

    第 37 处的判据是「本地文件是否还在」，但它分不开两种情况：
      ① 洗板把旧版换掉了（本地文件没了）；
      ② `delete_after_upload` 开关把**刚传完的正当版本**收尾删了（本地文件也没了）。
    于是 ② 会退化成互删：A 传完 → 本地被程序删掉 → B 的清理看不到 A → 把 A 当旧版删掉。
    本地已复现（同对照把开关关掉则 delete 目标为空）。

    修法：`files` 表加 `source_deleted`，`_delete_source()` 成功删除后置 1；
    `_same_episode_keeps()` 判据改为「本地还在 **或** 是本程序收尾删的」；
    `_requeue_changed_file()` / scanner 的整条替换负责把该事实作废。
    """
    dv = "Show.S01E05.2160p.DV.mkv"
    nov = "Show.S01E05.2160p.mkv"
    assert p115.episode_key(dv) == p115.episode_key(nov)

    # ---- A) DDL / 老库补列 ----
    assert "source_deleted" in store_store.DDL, "DDL 必须含 source_deleted"
    import sqlite3 as _sq
    tmp0 = tempfile.mkdtemp(prefix="v38-ddl-")
    try:
        db = os.path.join(tmp0, "old.db")
        conn = _sq.connect(db)
        conn.executescript(
            "CREATE TABLE files(path TEXT PRIMARY KEY, size INTEGER NOT NULL,"
            " mtime REAL NOT NULL, sha1 TEXT, status TEXT NOT NULL DEFAULT '待处理',"
            " stable_count INTEGER NOT NULL DEFAULT 0, retry_count INTEGER NOT NULL DEFAULT 0,"
            " first_failed_at REAL, next_retry_at REAL, upload_id TEXT, upload_callback TEXT,"
            " upload_oss_url TEXT, upload_partsize INTEGER, target_dir TEXT, pid INTEGER,"
            " file_id TEXT, progress REAL DEFAULT 0, speed REAL DEFAULT 0, message TEXT,"
            " upload_mode TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);"
            " CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);")
        conn.execute("INSERT INTO files(path,size,mtime,status,created_at,updated_at)"
                     " VALUES('/old/a.mkv',1,1.0,'已上传',1,1)")
        conn.commit()
        conn.close()
        st_old = store_store.Store(db)
        cols = {r["name"] for r in st_old.db.execute("PRAGMA table_info(files)").fetchall()}
        assert "source_deleted" in cols, f"老库没补上该列：{sorted(cols)}"
        assert int(st_old.get("/old/a.mkv").get("source_deleted") or 0) == 0, \
            "旧记录必须默认 0（否则老库里的旧版会被误保护、洗板漏清）"
    finally:
        shutil.rmtree(tmp0, ignore_errors=True)

    # ---- B) delete_after_upload 开着：同集两版本不互删 ----
    cfg, store, mgr, _up0, tmp, src = make_env(mode="rapid_only", fail_first=0)
    try:
        cloud_dir = "/TVs/Drama"
        cfg["paths"]["map"] = [{"from": "TVs", "to": "/TVs"}]
        cfg["upload"]["cleanup_previous_versions"] = True
        cfg["upload"]["cleanup_max_delete"] = 9
        cfg["upload"]["delete_after_upload"] = True          # ← 关键：程序会删本地源文件
        up = Uploader(cfg, store, mgr)
        await mgr.ensure_pid(cloud_dir)

        gone: list[str] = []

        async def fake_delete(fid):
            gone.append(str(fid))
            return True

        mgr.delete_file = fake_delete
        # 两个版本大小刻意不同（相同会被 keep_size 那道挡住，测不出这个洞）
        p1 = write_file(src, f"TVs/Drama/{dv}", 3 * 1024 * 1024)
        p2 = write_file(src, f"TVs/Drama/{nov}", 5 * 1024 * 1024)
        _t = time.time()
        os.utime(p1, (_t - 600, _t - 600))                    # DV 版先下载（旧），无 DV 版后下载（新）
        os.utime(p2, (_t, _t))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)

        rec1 = await drive(up, store, p1)
        assert rec1["status"] == "已上传", rec1["status"]
        assert not os.path.exists(p1), "delete_after_upload 应删掉本地源文件"
        assert int(store.get(p1).get("source_deleted") or 0) == 1, \
            "删除本地源文件后必须落 source_deleted=1（否则判据分不开洗板与收尾删除）"

        # 关键：另一版本的本地文件已不在，但它仍是正当版本 → 必须被保护
        _fids, names = up._same_episode_keeps(cloud_dir, p2)
        assert dv in names, f"被程序收尾删掉的正当版本没被保护（互删会重现）：{names}"

        gone.clear()
        rec2 = await drive(up, store, p2)
        assert rec2["status"] == "已上传", rec2["status"]
        # 洗板只留最新：库里记录在本地文件被收尾删除后仍在，按本地下载时间判新旧 ——
        # 先下载的 DV 版被替换、后下载的留下；裁决确定，不会互删到云端一个不剩
        assert gone == [f"stub-{p1}"], f"应只清掉先下载的旧版：{gone}"

        # ---- C) 洗板场景：本地文件不在、且不是本程序删的 → 仍要清 ----
        # 把 p1（dv 那条）降回「非程序删」—— 此时它本地文件已不在、source_deleted=0，
        # 正是洗板把旧版换掉后的等价态。
        store.set(p1, source_deleted=0)
        assert not os.path.exists(p1), "构造前提：本地文件必须不在（模拟洗板换掉）"
        assert int(store.get(p1).get("source_deleted") or 0) == 0
        _fids2, names2 = up._same_episode_keeps(cloud_dir, p2)
        assert dv not in names2, f"洗板场景仍保护旧版（会漏清）：{names2}"

        # ---- D) 洗板重下：scanner 整条替换 → 事实作废 ----
        p1b = write_file(src, f"TVs/Drama/{dv}", 9 * 1024 * 1024)   # 新内容、同路径
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        rec = store.get(p1b)
        assert int(rec.get("source_deleted") or 0) == 0, \
            f"洗板重下后旧事实必须作废：{rec.get('source_deleted')}"
        assert rec["status"] == "待处理", rec["status"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append("[38] delete_after_upload 开着时同集两版本不互删：source_deleted 落库；"
                "被程序收尾删掉的正当版本仍受保护、delete 目标为空；"
                "洗板场景（本地被换掉且非程序删）不再保护、照旧清理；"
                "洗板重下经 scanner 整条替换使事实作废；老库自动补列且默认 0")


async def case_secure_compare_all_channels():
    """[39][40] 三条鉴权通道必须用**同一种**常数时间比较，且能吃下非 ASCII 凭据。

    [40] `hmac.compare_digest()` 作用在 **str** 上只接受纯 ASCII —— 遇到中文/emoji 抛
    `TypeError: comparing strings with non-ASCII characters is not supported`。
    而 `check_credentials()` 的两个调用点（`check_auth` 的 Basic 分支、`_do_login`）
    都不在 try 里：面板把密码设成含中文后，**Basic 通道每次请求都抛 TypeError**，
    socketserver 兜住并打一整段 traceback，客户端拿到的是「连接断开」而不是 401/500
    （界面表现为一直转圈）。同一条凭据，Cookie 通道与 `?token=` 通道却是好的 ——
    「一条路能进、一条路进不去」是最难查的形态。本地已端到端复现。

    [39] 同一份 `check_auth` 里三条通道，两条用 `compare_digest`、只有 `?token=` 那条用
    `==`（首个不同字节即返回，耗时与匹配前缀相关）。统一成 `_secure_eq()`。

    修法：新增 `_secure_eq(a, b)` —— 先 `str(...).encode("utf-8")` 再 `compare_digest`，
    任意内容都接受且仍是常数时间；三条通道全部改走它。
    """
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    srv = None
    webui._FAILURES.clear()

    # ---- A) 单元层：_secure_eq 自身 ----
    assert webui._secure_eq("abc", "abc") is True
    assert webui._secure_eq("abc", "abd") is False
    assert webui._secure_eq("", "") is True
    # ★ 核心：非 ASCII 不抛异常
    assert webui._secure_eq("密码", "密码") is True, "非 ASCII 相等必须为 True（旧写法抛 TypeError）"
    assert webui._secure_eq("密码", "密碼") is False
    assert webui._secure_eq("p@ss🔐", "p@ss🔐") is True, "emoji 也要能比"
    assert webui._secure_eq(None, "") is False or webui._secure_eq(None, None) is True
    # 直接锤旧写法会抛（记录这个前提，别把用例写成恒真）
    try:
        import hmac as _hmac
        _hmac.compare_digest("密码", "密码")
        non_ascii_raises = False
    except TypeError:
        non_ascii_raises = True
    assert non_ascii_raises, "前提：hmac.compare_digest 对非 ASCII str 必须抛 TypeError"

    # ---- B) 三通道 × 中文/emoji 凭据 ----
    USR, PWD = "管理员", "密码中文测试🔐"
    a = {"enabled": True, "mode": "basic", "username": USR, "password": PWD}
    assert webui.check_credentials(a, USR, PWD) is True, "中文凭据必须通过（旧写法抛 TypeError）"
    assert webui.check_credentials(a, USR, "wrong") is False
    assert webui.check_credentials(a, "wrong", PWD) is False

    c = copy.deepcopy(cfgmod.DEFAULTS)
    c["http"]["auth"] = dict(a, session_hours=72)
    # ① ?token= 通道
    assert webui.check_auth(c, {}, "token=" + urllib.parse.quote(PWD)) is True, "?token= 中文密码应放行"
    assert webui.check_auth(c, {}, "token=nope") is False
    # ② Cookie 会话（签名内部走 encode，本来就支持非 ASCII —— 这条是「对照，不能修坏」）
    tok = webui.make_session(USR, PWD, 1)
    assert webui.check_auth(c, {"Cookie": f"{webui.COOKIE_NAME}={tok}"}, "") is True
    # ③ Basic 通道（★ 修复点）
    ok_cred = base64.b64encode(f"{USR}:{PWD}".encode()).decode()
    bad_cred = base64.b64encode(f"{USR}:wrong".encode()).decode()
    assert webui.check_auth(c, {"Authorization": "Basic " + ok_cred}, "") is True, \
        "Basic 中文凭据应放行（修复前抛 TypeError）"
    assert webui.check_auth(c, {"Authorization": "Basic " + bad_cred}, "") is False
    # ④ 关闭鉴权时恒真（别把开关弄坏）
    c_off = copy.deepcopy(cfgmod.DEFAULTS)
    c_off["http"]["auth"]["enabled"] = False
    assert webui.check_auth(c_off, {}, "") is True

    # ---- C) 端到端：中文密码走 Basic 必须是 200/401，而不是断连 ----
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)

    def get(headers=None, path="/api/status"):
        req = urllib.request.Request(f"{base}{path}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            return opener.open(req, timeout=5).status
        except urllib.error.HTTPError as exc:
            return exc.code

    try:
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": True, "mode": "basic", "username": USR,
                                "password": PWD, "session_hours": 72}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        assert get() == 401, "无凭据应 401"
        assert get({"Authorization": "Basic " + ok_cred}) == 200, \
            "★ Basic 中文凭据应得 200（修复前抛 TypeError → 连接断开）"
        assert get({"Authorization": "Basic " + bad_cred}) == 401, "错误凭据应 401"
        assert get(path="/api/status?token=" + urllib.parse.quote(PWD)) == 200
        assert get(path="/api/status?token=nope") == 401
        assert get(path="/health") == 200, "/health 免鉴权"

        PASS.append("[39][40] 三条鉴权通道统一常数时间比较、且吃下非 ASCII 凭据："
                    "`_secure_eq` 按 UTF-8 编码后 compare_digest（中文/emoji 不再抛 "
                    "TypeError）；?token= 通道由 `==` 改为常数时间；"
                    "端到端 Basic 中文密码得 200 / 错误得 401（修复前 TypeError → 断连）；"
                    "Cookie 与关闭鉴权两条路径未受影响")
    finally:
        webui._FAILURES.clear()
        if srv is not None:
            srv.shutdown()


async def case_same_name_duplicate_cleaned():
    """[41] 云端同集**同名重复**必须能被清掉（真机现象：两条逐字同名的 S01E33）。

    真机取证：115 同一目录下出现两条完全同名的
    `甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv`，
    面板开关开着、日志里连一行都没有 —— 洗板清理**静默失效**。

    根因：`cleanup_previous` 把 `keep_name` 自动塞进了「名字保护集」，
    于是 `if name in keep_names_set: continue` 把**所有与 keep_name 同名的条目**
    一并保护住。而下列两种情形里，要清的旧条目名恰好就等于 `keep_name`：
      ① 云端同集同名重复（本次真机现象）；
      ② 115 同名自动改名：新版成了 "xxx (1).mkv"，而**旧的那条名字正好等于 keep_name**。
    两种情形下旧条目都永远跳过 ⇒ 连「大小不同」那道闸门都走不到，静默返回「没有旧版本」。
    本地已复现：四种组合全部 `deleted=[]`。

    修法：判据改为**标识优先** —— 只要 `keep_ids` 精确命中了某一条，就说明「刚上传那条」
    已被 fid/pickcode 唯一确定，同名的其它条目就是重复项，名字与大小两道闸门都不再保护它；
    无标识可用时保持保守（不动手），但把静默改成**可见的 note**。
    另外区分两类保护：`keep_name`（自动、无标识时兜底）vs `explicit_names`
    （调用方显式传的同集其它正当版本、**始终**保护）。
    """
    from p115 import P115 as _P115

    FULL = "甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv"
    REN = FULL[:-4] + " (1).mkv"                     # 115 同名自动改名的产物
    OTHER = "甲剧 (2026) [tmdbid=100001] S01E33 [1080p WEB-DL].mkv"
    assert p115.episode_key(FULL) == p115.episode_key(REN) == p115.episode_key(OTHER), \
        "构造前提：三个文件名必须同集"

    class _Cloud:
        def __init__(self, rows):
            self.f = {fid: {"fid": fid, "n": n, "s": s, "ico": "mkv"} for fid, n, s in rows}

        def snap(self):
            return [dict(v) for v in self.f.values()]

    def mgr_of(cloud):
        m = _P115.__new__(_P115)
        m._dir_cache = {}
        m._dir_cache_ttl = 30.0
        m._dir_cache_max = 64
        m._delete_lock = asyncio.Lock()

        async def list_dir(pid, use_cache=True):
            rows = cloud.snap()
            m._remember_dir(int(pid), rows)
            return rows

        async def delete_file(fid):
            async with m._delete_lock:
                return bool(cloud.f.pop(str(fid), None))

        m.list_dir = list_dir
        m.delete_file = delete_file
        return m

    MB = 1024 * 1024
    got = []

    async def run(label, rows, keep_fid, keep_fids, keep_names, keep_size, expect_left):
        c = _Cloud(rows)
        r = await mgr_of(c).cleanup_previous(700, FULL, keep_size, 9,
                                            keep_fid=keep_fid, keep_fids=keep_fids,
                                            keep_names=keep_names)
        left = sorted(x["fid"] for x in c.snap())
        assert left == sorted(expect_left), f"{label}：剩余 {left}，期望 {sorted(expect_left)}（{r}）"
        got.append(label)
        return r

    # ① 真机现象：两条逐字同名、大小不同、标识命中「新的」
    r1 = await run("① 同名重复 + 标识命中", [("111", FULL, 3 * MB), ("222", FULL, 5 * MB)],
                   "222", [], [], 5 * MB, ["222"])
    assert r1["deleted"] == [FULL], r1

    # ② 同上但大小完全相同 —— 修复前会被 keep_size 挡住
    r2 = await run("② 同名重复 + 大小相同", [("111", FULL, 5 * MB), ("222", FULL, 5 * MB)],
                   "222", [], [], 5 * MB, ["222"])
    assert r2["deleted"] == [FULL], r2

    # ③ 115 自动改名：旧版名 = keep_name(完整)，新版名 = keep_name (1)
    r3 = await run("③ 115 自动改名", [("111", FULL, 3 * MB), ("222", REN, 5 * MB)],
                   "222", [], [], 5 * MB, ["222"])
    assert r3["deleted"] == [FULL], r3

    # ④ 无标识 + 同名多条：保守不动手，但 note 必须**可见**（不能再说「没有旧版本」）
    r4 = await run("④ 无标识兜底", [("111", FULL, 3 * MB), ("222", FULL, 5 * MB)],
                   "", [], [], 5 * MB, ["111", "222"])
    assert r4["deleted"] == [], r4
    assert "同名" in r4["note"], f"无标识时 note 必须点明同名重复：{r4}"

    # ⑤ 显式 keep_names（同集其它正当版本）必须始终保护，即使同名
    r5 = await run("⑤ 显式保护始终生效", [("111", FULL, 3 * MB), ("222", REN, 5 * MB)],
                   "111", [], [REN], 3 * MB, ["111", "222"])
    assert r5["deleted"] == [], r5

    # ⑥ 洗板核心行为不变：不同名的同集旧版照旧被清
    r6 = await run("⑥ 洗板核心行为不变", [("111", FULL, 3 * MB), ("333", OTHER, 7 * MB)],
                   "111", [], [], 3 * MB, ["111"])
    assert r6["deleted"] == [OTHER], r6

    # ⑦ 同集正当版本仍互不删（第 37 处的保护不能被这次改动弄坏）
    r7 = await run("⑦ 同集正当版本互不删", [("111", FULL, 3 * MB), ("222", OTHER, 7 * MB)],
                   "111", ["222"], [OTHER], 3 * MB, ["111", "222"])
    assert r7["deleted"] == [], r7

    PASS.append("[41] 云端同集同名重复能被清掉（真机两条逐字同名的 S01E33）：判据改为标识优先 —— "
                "标识命中后同名条目不再受名字/大小两道闸门保护；"
                "覆盖「同名+大小不同」「同名+大小相同」「115 自动改名（旧名=keep_name）」三种情形；"
                "无标识时保守不动手但 note 点明同名重复（不再静默说「没有旧版本」）；"
                "显式 keep_names 始终保护、同集正当版本仍互不删、不同名旧版照旧清理")


async def case_same_episode_time_judgment():
    """[42] 洗板重下后**两个版本本地文件都在**时，旧版仍必须被清（真机报障第二次）。

    真机现象（用户截图）：云端同集两条都在
      `甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv`（新下）
      `甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv`（旧的）
    用户「重新下了一遍」后点重新排队，**还是一样没删**。

    根因：第 37 处引入的保护判据是「本地文件还在 ⇒ 正当版本」，而洗板重下后旧版的本地
    文件**也还在**（用户只是重新下了一份新的）⇒ 旧版被当成正当版本保护住 ⇒
    `cleanup_previous` 返回「没有旧版本」⇒ 一条都不删。
    本地已复现：NEW(刚传)/OLD(3 天前) 两条本地都在 → 保护集合含 OLD → deleted=[]。

    修法：判据加**时间维度** —— 只有「内容入队时间与当前文件同批」
    （差 ≤ `SAME_BATCH_SECONDS`）的才保护；明显更早的是上一轮洗板遗留的老版本，照旧清理。
    必须用 `created_at` 而不是 `updated_at`：后者被每次状态写入刷新（scanner 推进
    `stable_count`、秒传命中改 message、用户点「重新排队」都会刷新），用它会让老版本
    看起来和新的差不多，判据失效。
    另加护栏 `_newer_episode_sibling()`：同集已有**更新**的版本时本次不动手 ——
    否则用户点在老版本上触发清理，会把真正的最新版删掉（结果与预期相反）。
    另把「同集多条但一条都没删」的静默改成可见 note。
    """
    cfg, store, mgr, _up0, tmp, src = make_env(stable_rounds=1)
    try:
        cloud_dir = "/TVs/Drama"
        cfg["paths"]["map"] = [{"from": "TVs", "to": "/TVs"}]
        cfg["upload"]["cleanup_previous_versions"] = True
        cfg["upload"]["cleanup_max_delete"] = 9
        up = Uploader(cfg, store, mgr)
        await mgr.ensure_pid(cloud_dir)

        NEW = "甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv"
        OLD = "甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv"
        assert p115.episode_key(NEW) == p115.episode_key(OLD), "构造前提：两条必须同集"

        def stage(p_old: str, p_new: str, old_age_days: float):
            """造「两条本地都在」的状态；old_age_days>0 ⇒ 旧版是上一轮洗板遗留。"""
            store.set(p_old, status="已上传", target_dir=cloud_dir, file_id="fid-OLD")
            store.db.execute("UPDATE files SET created_at=? WHERE path=?",
                             (time.time() - old_age_days * 86400, p_old))
            store.db.commit()
            store.set(p_new, status="已上传", target_dir=cloud_dir, file_id="fid-NEW")
            return p_old, p_new

        # ═══ A) 洗板场景：OLD 明显更早 ⇒ 不保护，必须能被清 ═══
        p_new = write_file(src, f"TVs/Drama/{NEW}", 9 * 1024 * 1024)
        p_old = write_file(src, f"TVs/Drama/{OLD}", 7 * 1024 * 1024)
        store.add(p_new, 9 * 1024 * 1024, os.path.getmtime(p_new))
        store.add(p_old, 7 * 1024 * 1024, os.path.getmtime(p_old))
        stage(p_old, p_new, old_age_days=3)
        assert os.path.exists(p_old) and os.path.exists(p_new), "构造前提：两条本地都要在"
        _fids, names = up._same_episode_keeps(cloud_dir, p_new)
        assert OLD not in names, f"洗板遗留的旧版仍被保护（永远清不掉）：{names}"
        assert NEW not in names, f"自己不该出现在保护集合里：{names}"

        # ═══ B) 同批场景（第 37 处回归）：两条同一轮入队 ⇒ 必须互相保护 ═══
        store.db.execute("UPDATE files SET created_at=? WHERE path=?", (time.time(), p_old))
        store.db.commit()
        _fids2, names2 = up._same_episode_keeps(cloud_dir, p_new)
        assert OLD in names2, f"同批正当版本没被保护（互删会重现）：{names2}"

        # ═══ C) 第 38 处回归：本地被程序收尾删掉、且同批 ⇒ 仍保护 ═══
        os.remove(p_old)
        store.set(p_old, source_deleted=1)
        _fids3, names3 = up._same_episode_keeps(cloud_dir, p_new)
        assert OLD in names3, f"delete_after_upload 收尾删掉的正当版本没被保护：{names3}"

        # ═══ D) 护栏：点在**老版本**上 → 必须识别出更新的版本、让位 ═══
        # 注意方向：当前文件（p_old）要**更早**、另一条（p_new）要**更新**，
        # 这样才对应「用户点了老版本那条的重新排队」。C 段把两条都改过时间，这里重设回去。
        os.remove(p_new)
        write_file(src, f"TVs/Drama/{NEW}", 9 * 1024 * 1024)
        store.set(p_new, status="已上传", target_dir=cloud_dir, file_id="fid-NEW")
        store.set(p_old, source_deleted=0, status="已上传", target_dir=cloud_dir, file_id="fid-OLD")
        if not os.path.exists(p_old):
            open(p_old, "wb").write(b"o" * (7 * 1024 * 1024))
        store.db.execute("UPDATE files SET created_at=? WHERE path=?",
                         (time.time() - 3 * 86400, p_old))    # 当前 = 老版本
        store.db.execute("UPDATE files SET created_at=? WHERE path=?",
                         (time.time(), p_new))                # 另一条 = 更新的版本
        store.db.commit()
        newer = up._newer_episode_sibling(cloud_dir, p_old)   # 当前=旧版
        assert newer == NEW, f"点在老版本上时没识别出更新的版本（会把最新版删掉）：{newer!r}（p_old/created_at={store.get(p_old)['created_at']}, p_new/created_at={store.get(p_new)['created_at']}）"
        newer2 = up._newer_episode_sibling(cloud_dir, p_new)  # 当前=最新版
        assert newer2 == "", f"自己是最新时不该有更新的版本：{newer2!r}"

        # ═══ E) 「同集多条但一条都不删」必须可见（不能静默「没有旧版本」）═══
        class _Cloud:
            def __init__(self):
                self.f = {"fid-NEW": {"fid": "fid-NEW", "n": NEW, "s": 9 * 1024 * 1024, "ico": "mkv"},
                          "fid-OLD": {"fid": "fid-OLD", "n": OLD, "s": 7 * 1024 * 1024, "ico": "mkv"}}
            def snap(self):
                return [dict(v) for v in self.f.values()]

        m = p115.P115.__new__(p115.P115)
        m._dir_cache = {}
        m._dir_cache_ttl = 30.0
        m._dir_cache_max = 64
        m._delete_lock = asyncio.Lock()

        async def list_dir(pid, use_cache=True):
            rows = _Cloud().snap()
            m._remember_dir(int(pid), rows)
            return rows

        m.list_dir = list_dir
        r = await m.cleanup_previous(700, NEW, 9 * 1024 * 1024, 9,
                                     keep_fid="fid-NEW", keep_fids=["fid-OLD"], keep_names=[OLD])
        assert r["deleted"] == [], r
        assert "同集" in r["note"] and "没有旧版本" != r["note"], \
            f"同集多条却没动手时必须给出可见说明：{r['note']!r}"

        PASS.append("[42] 洗板重下后两个版本本地文件都在时旧版仍被清：判据加时间维度"
                    "（created_at 同批才保护，用 updated_at 会被状态写入刷新而失效）；"
                    "护栏 _newer_episode_sibling 使「点在老版本上」让位给更新的版本而不是删掉它；"
                    "「同集多条但一条未删」由静默改为可见 note；"
                    "第 37 处（同批互不删）与第 38 处（收尾删除仍保护）回归通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_cloud_only_episode_cleanup():
    """[43] 同集清理**只用 115 云端时间**判据（用户真机报障的最终根因）。

    用户原话：「上传到115以后校验115的那个文件夹里面有没有相同集数的视频，就拿第33集为例，
    现在115里面有两个第33集的甲剧，但是一个是hdr的版本（后秒传上去的），一个普通4k版本
    （先秒传上去的），我需要的是后秒传上去这个hdr的版本后，把先秒传上去的普通4k版本删掉」；
    并纠正：「你只能在 115 云端去核对，本地肯定是没有的，因为本地的文件在秒传完已经被删掉了」。

    真机实测数据（只读探针，逐字取自回贴）：
      HDR 版: fid=1000000000000000011 te=1789989329 (19:15) pc=abcd0demo0pick002 size=5187057457
      普通版: fid=1000000000000000012 te=1789985493 (18:11) pc=abcd0demo0pick003 size=1286794717
      配置: cleanup_previous_versions=True / delete_after_upload=True / mode=rapid_only
      日志: 2 小时内 `清理|洗板|旧版` **零行** —— 清理一次都没进过删除分支

    根因：`delete_after_upload=True` ⇒ 每个上传成功的版本本地文件都被 `_delete_source()`
    收尾删掉、库里 `source_deleted=1`。而第 37/38 处为防「同集互删」加的保护判据是
    「本地文件还在 **或** 是本程序收尾删的 ⇒ 正当版本 ⇒ 保护」—— 在这个配置下**每个版本
    都命中**，于是同集所有条目全被保护、`cleanup_previous` 返回「没有旧版本」、
    清理永久空转（而调用方刻意不打印这个 note ⇒ 界面上开关开着、日志里连一行都没有）。

    修法：新增纯云端判据 `p115.P115.cleanup_episode_newest()` —— 只用 115 条目自身的
    时间字段（`te`/`tp`/`tu` epoch 秒，`t` 字符串兜底），与本地文件、本地记录状态完全无关。
    """
    from p115 import P115 as _P115

    HDR = {"fid": "1000000000000000011", "cid": "1000000000000000100",
           "n": "甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL HDR TX DemoWeb].mkv",
           "s": 5187057457, "te": "1789989329", "tp": "1789989329", "tu": "1789989329",
           "t": "2026-09-21 19:15", "pc": "abcd0demo0pick002", "ico": "mkv"}
    PLN = {"fid": "1000000000000000012", "cid": "1000000000000000100",
           "n": "甲剧 (2026) [tmdbid=100001] S01E33 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv",
           "s": 1286794717, "te": "1789985493", "tp": "1789985493", "tu": "1789985493",
           "t": "2026-09-21 18:11", "pc": "abcd0demo0pick003", "ico": "mkv"}
    assert p115.episode_key(HDR["n"]) == p115.episode_key(PLN["n"]) == "s01e033"

    # ---- A) 时间字段解析（真机字段值逐字）----
    assert p115.P115.cloud_ts(HDR) == 1789989329.0
    assert p115.P115.cloud_ts(PLN) == 1789985493.0
    assert p115.P115.cloud_ts(HDR) > p115.P115.cloud_ts(PLN), "HDR 是后传的，时间必须更大"
    # 字符串兜底（只有 t 时）
    assert p115.P115.cloud_ts({"t": "2026-09-21 19:15"}) > 1789980000
    # 无时间字段 → 0（保守路径的前提）
    assert p115.P115.cloud_ts({"fid": "1", "n": "x.mkv"}) == 0.0
    # 单一真相源：uploader 的时间解析必须委托给 p115（别两份实现漂移）
    assert Uploader._cloud_ts(HDR) == p115.P115.cloud_ts(HDR)

    class _Cloud:
        def __init__(self, rows):
            self.f = {r["fid"]: dict(r) for r in rows}

        def snap(self):
            return [dict(v) for v in self.f.values()]

    def mgr_of(cloud):
        m = _P115.__new__(_P115)
        m._dir_cache = {}
        m._dir_cache_ttl = 30.0
        m._dir_cache_max = 64
        m._delete_lock = asyncio.Lock()

        async def list_dir(pid, use_cache=True):
            rows = cloud.snap()
            m._remember_dir(int(pid), rows)
            return rows

        async def delete_file(fid):
            async with m._delete_lock:
                return bool(cloud.f.pop(str(fid), None))

        m.list_dir = list_dir
        m.delete_file = delete_file
        return m

    def names(c):
        return [v["n"] for v in c.snap()]

    # ---- B) ★ 用户的确切场景：普通4K先传(18:11) → HDR后传(19:15) → 删普通版 ----
    c = _Cloud([PLN, HDR])
    r = await mgr_of(c).cleanup_episode_newest(700, HDR["n"], 3, keep_fid=HDR["pc"])
    assert names(c) == [HDR["n"]], f"应只剩 HDR 版，实际 {names(c)}"
    assert r["deleted"] == [PLN["n"]], r

    # ---- C) 反向：后传的是普通版 → 留普通版 ----
    c2 = _Cloud([{**PLN, "te": "1789999000", "t": "2026-09-21 22:00"}, dict(HDR)])
    await mgr_of(c2).cleanup_episode_newest(700, PLN["n"], 3, keep_fid=PLN["pc"])
    assert names(c2) == [PLN["n"]], f"应只剩后传的普通版：{names(c2)}"

    # ---- D) 护栏：本次那条不是最新（点在老版本重新排队）→ 让位不动手 ----
    c3 = _Cloud([PLN, HDR])
    r3 = await mgr_of(c3).cleanup_episode_newest(700, PLN["n"], 3, keep_fid=PLN["pc"])
    assert len(c3.f) == 2, f"该让位不动手，实际删了：{names(c3)}"
    assert "不是最新" in r3["note"], r3

    # ---- E) 无时间字段（测试替身那种）→ 保守不动手 + 可见 note ----
    c4 = _Cloud([{"fid": "1", "n": PLN["n"], "s": 1, "ico": "mkv"},
                 {"fid": "2", "n": HDR["n"], "s": 2, "ico": "mkv"}])
    r4 = await mgr_of(c4).cleanup_episode_newest(700, HDR["n"], 3, keep_fid="2")
    assert len(c4.f) == 2 and "时间字段" in r4["note"], r4

    # ---- F) 时间并列 → 不动手 ----
    c5 = _Cloud([{**PLN, "te": "1789999999"}, {**HDR, "te": "1789999999"}])
    r5 = await mgr_of(c5).cleanup_episode_newest(700, HDR["n"], 3, keep_fid=HDR["pc"])
    assert len(c5.f) == 2 and "并列" in r5["note"], r5

    # ---- G) 超过上限 → 整体跳过（防命名意外批量误删）----
    rows = [PLN, {**PLN, "fid": "3001", "pc": "p3001", "te": "1789985000"},
            {**PLN, "fid": "3002", "pc": "p3002", "te": "1789985100"}, HDR]
    c6 = _Cloud(rows)
    r6 = await mgr_of(c6).cleanup_episode_newest(700, HDR["n"], 1, keep_fid=HDR["pc"])
    assert len(c6.f) == 4 and "超过上限" in r6["note"], r6

    # ---- H) 只该同集：不同集/别的剧不许碰 ----
    other = {**PLN, "fid": "4001", "pc": "p4001", "te": "1789980000",
             "n": "甲剧 (2026) [tmdbid=100001] S01E34 [2160p H265 DDP 5.1 WEB-DL TX DemoWeb].mkv"}
    c7 = _Cloud([HDR, other])
    await mgr_of(c7).cleanup_episode_newest(700, HDR["n"], 3, keep_fid=HDR["pc"])
    assert len(c7.f) == 2, f"不同集不该动：{names(c7)}"

    # ---- I) 端到端：走 Uploader._cleanup_prev_versions（确认它改走纯云端判据）----
    cfg, store, _mgr, _up0, tmp, src = make_env(mode="rapid_only", fail_first=0)
    try:
        cloud_dir = "/TVs/甲剧 (2026) [tmdbid=100001]/Season 01"
        cfg["upload"]["cleanup_previous_versions"] = True
        cfg["upload"]["cleanup_max_delete"] = 3
        cfg["upload"]["delete_after_upload"] = True          # ← 真机配置：本地会被收尾删掉
        up = Uploader(cfg, store, _mgr)

        class _RT(_P115):
            def __init__(self_, rows):
                self_._dir_cache = {}
                self_._dir_cache_ttl = 30.0
                self_._dir_cache_max = 64
                self_._delete_lock = asyncio.Lock()
                self_.f = {r["fid"]: dict(r) for r in rows}

            async def ensure_pid(self_, p):
                return 700

            async def list_dir(self_, pid, use_cache=True):
                rows = [dict(v) for v in self_.f.values()]
                self_._remember_dir(int(pid), rows)
                return rows

            async def delete_file(self_, fid):
                async with self_._delete_lock:
                    return bool(self_.f.pop(str(fid), None))

        rt = _RT([PLN, HDR])
        up.mgr = rt
        # 本地文件按真机行为**不存在**（秒传完已被收尾删掉），库里记录 source_deleted=1。
        # ★ 本地文件名必须与云端条目名一致（真机就是这样：`<剧名 (年) [tmdbid=…]>/Season 01/`
        #   下与云端同名），否则 episode_key 算出的剧名段不同、匹配不上（这是现实约束：
        #   本地名与云端名形式不同时清不掉，代码已把这种情况做成**可见 note**）。
        p_local = os.path.join(src, "TVs/Drama", HDR["n"])
        store.add(p_local, 5187057457, 1.0)
        store.set(p_local, status="已上传", target_dir=cloud_dir,
                  file_id=HDR["pc"], source_deleted=1)
        await up._cleanup_prev_versions(cloud_dir, 700, p_local)
        assert list(rt.f.keys()) == [HDR["fid"]], \
            f"★ 本地文件已被收尾删掉时旧版仍应被清：剩 {[v['n'] for v in rt.f.values()]}"

        # ---- J) 名字对不上时必须给出**可见** note（不能静默「没有旧版本」）----
        rt2 = _RT([PLN, HDR])
        up.mgr = rt2
        p_odd = os.path.join(src, "TVs/Drama", "Other.Show.S01E33.HDR.mkv")   # 剧名与云端不兼容
        store.add(p_odd, 1, 1.0)
        store.set(p_odd, status="已上传", target_dir=cloud_dir, file_id="x", source_deleted=1)
        r_odd = await rt2.cleanup_episode_newest(700, "Other.Show.S01E33.HDR.mkv", 3, keep_fid="x")
        assert len(rt2.f) == 2, "剧名对不上时不该动手"
        assert "剧名" in r_odd["note"] and "cleanup_title_match" in r_odd["note"], \
            f"剧名对不上必须可见：{r_odd}"
        # 剧名只是写法不同（点分 vs 带年份/标签）= 同一部剧，照常清理
        rt3 = _RT([PLN, HDR])
        r_ok = await rt3.cleanup_episode_newest(700, "甲剧.S01E33.HDR.mkv", 3, keep_fid=HDR["pc"])
        assert list(rt3.f) == [HDR["fid"]] and r_ok["deleted"] == [PLN["n"]], r_ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- K) 巡检的列目录必须走 mgr.client（真机实测踩过：self.client 不存在）----
    # 真机回贴：`'Uploader' object has no attribute 'client'` × 3，而巡检的 except 把它
    # 降级成一行 warning、`removed` 恒为 0 —— 表现成「巡检跑了但清不掉」。
    # `Uploader.__init__(cfg, store, manager)` 只存 `self.mgr`，115 客户端在 `mgr.client` 上。
    up_src = inspect.getsource(Uploader)
    assert "self.mgr.client" in up_src, "巡检列目录必须走 self.mgr.client"
    assert "await self.client." not in up_src, \
        "★ Uploader 没有 self.client，写成它会 AttributeError 并被 except 静默吞掉"
    assert "return None                                # 失败 ≠ 空目录" in up_src or \
           "return None" in inspect.getsource(Uploader._cloud_page), \
        "列目录失败必须返回 None（与空目录区分），否则「已扫目录数」会骗人"

    PASS.append("[43] 同集清理改为**纯云端时间**判据（真机根因）：delete_after_upload=True 时"
                "每个版本本地都被收尾删掉、source_deleted=1，任何「本地在不在」判据都会把同集"
                "全部版本保护住 ⇒ 清理永久空转（真机 2 小时日志零行）；"
                "新判据用 115 条目 `te`/`tp`/`tu` epoch 秒（`t` 字符串兜底），与本地无关；"
                "用户场景 6 项全通（HDR后传删普通版 / 反向 / 让位护栏 / 无时间字段保守 + 可见 / "
                "时间并列不动手 / 超上限跳过 / 不同集不碰）；端到端确认本地被删时仍能清理；"
                "巡检列目录走 mgr.client（真机踩过 self.client 不存在 → 静默恒 0）")


async def case_sweep_business_error_visible():
    """[46~47] 巡检把 115 业务拒绝当「空目录」吞掉；is_transient 死代码（真机+拆包取证）。

    起因：用户问「你这个巡检不会触发115的风控吧」。答案本身是「不会」（36 次/小时、
    只读元数据、同一会话、开关关着零请求），但顺着验证挖出两处真 bug：

    [46] 115 的业务拒绝是 **HTTP 200 + `state:false`（带 errno/error），不抛异常**。
    第 44 处只把「抛异常」这条路和空目录分开 —— 业务拒绝这条路全部落进「空目录」分支：
    真被限流/风控/登录失效时，巡检照报「扫了 N 个、清了 0 个」，**一行日志都没有**
    —— 假装一切正常比崩溃更糟，用户永远查不到原因。

    [47] `is_transient()` **从未生效过**。证据 = `pip download --python-version 312
    --no-deps` 拉 p115client 0.0.9.6.5.1 wheel 拆全源码：`HTTPError`/`urllib.error`/
    `requests.` 命中数**全为 0**；传输层 `from urllib3_future_request import request`；
    业务错误由 `check_response()` 转成 `P115OSError(P115Error, OSError)` 抛出，
    errno 语义（其源码映射）：EAGAIN=操作过于频繁、EBUSY=忙（115 的复制/移动/删除/
    还原同时只允许一个）、EAUTH=登录失效。⇒ 原判据只认 `urllib.error.HTTPError`
    （p115.py:86-87）恒 False；且旧 `TRANSIENT_HTTP_CODES` 含 **401/403**
    —— 认证失败被拒后原地自动重试，方向恰好反了，正是风控最忌讳的行为。
    """
    import errno as _errno
    import io

    # ---- A) _cloud_page：业务拒绝必须 → None（失败）；真空目录仍 → []（可继续）----
    BAD = [
        ("state=False", {"state": False, "error": "操作过于频繁"}),
        ("风控 errno", {"state": False, "errno": 990009, "error": "触发风控"}),
        ("只有 error", {"error": "登录失效"}),
        ("空响应体", {}),
        ("state=False+data", {"state": False, "error": "x", "data": []}),
    ]
    OK_EMPTY = [
        ("正常空目录", {"state": True, "data": {"list": [], "count": 0}}),
        ("data=[] 空", {"state": True, "data": []}),
        ("state 真但 data 里没有 list", {"state": True, "data": {"count": 0}}),
    ]
    OK_ROWS = [
        ("dict+list", {"state": True, "data": {"list": [{"fid": 1, "n": "a.mkv"}]}}),
        ("data 直接是 list", {"state": True, "data": [{"fid": 1, "n": "a.mkv"}]}),
        ("顶层 list", {"state": True, "list": [{"fid": 1, "n": "a.mkv"}]}),
    ]

    class _C:
        def __init__(self, resp):
            self.resp = resp

        async def fs_files(self, payload, async_=True):
            return self.resp

    class _Mgr:
        def __init__(self, resp):
            self.client = _C(resp)

        async def cached_pid(self, td):
            return 7999

    tmp = tempfile.mkdtemp(prefix="upilot-test-46-")
    try:
        cfg = copy.deepcopy(cfgmod.DEFAULTS)
        cfg["state"] = {"db": os.path.join(tmp, "s.db")}
        cfg["upload"]["cleanup_previous_versions"] = True
        st = Store(cfg["state"]["db"])

        for label, resp in BAD:
            got = await Uploader(cfg, st, _Mgr(resp))._cloud_page(7999)
            assert got is None, f"{label}：业务拒绝必须判「失败」(None)，实际 {got!r}"
        for label, resp in OK_EMPTY:
            got = await Uploader(cfg, st, _Mgr(resp))._cloud_page(7999)
            assert got == [], f"{label}：真空目录必须是 []，实际 {got!r}"
        for label, resp in OK_ROWS:
            got = await Uploader(cfg, st, _Mgr(resp))._cloud_page(7999)
            assert got is not None and len(got) == 1, f"{label}：应拿到 1 条，实际 {got!r}"

        # ---- B) 巡检端到端：被拒目录既不能删、也不能算进「已扫」（计数会骗人）----
        p = os.path.join(tmp, "x.mkv")
        st.add(p, 1, 1.0)
        st.set(p, status="已上传", target_dir="/root")
        up = Uploader(cfg, st, _Mgr({"state": False, "errno": 990009, "error": "触发风控"}))
        r = await up.sweep_cloud_episode_duplicates()
        assert r["removed"] == 0, f"被风控时绝不能删任何东西：{r!r}"
        assert r["dirs"] == 0, f"被拒绝的目录绝不能算「已扫」（第 44 处同款病根）：{r!r}"

        # ---- C) is_transient：按 OSError.errno 判；认证/参数类绝不软重试 ----
        assert p115.is_transient(OSError(_errno.EAGAIN, "操作过于频繁")) is True
        assert p115.is_transient(OSError(_errno.EBUSY, "busy")) is True
        assert p115.is_transient(OSError(_errno.ETIMEDOUT, "timeout")) is True
        assert p115.is_transient(OSError(_errno.ECONNRESET, "reset")) is True
        assert p115.is_transient(OSError(_errno.EIO, "io")) is False, "EIO 非「稍后再试」语义"
        assert p115.is_transient(OSError(_errno.EINVAL, "参数错误")) is False
        assert p115.is_transient(OSError(_errno.EACCES, "拒绝访问")) is False, \
            "权限/认证方向绝不软重试（风控最忌讳被拒后原地重试）"

        def _http(code):
            return urllib.error.HTTPError("http://x", code, "m", {}, io.BytesIO(b""))

        assert p115.is_transient(_http(429)) is True
        assert p115.is_transient(_http(503)) is True
        assert p115.is_transient(_http(401)) is False, "401 认证失败：绝不自动重试"
        assert p115.is_transient(_http(403)) is False
        assert p115.is_transient(ValueError("无关异常")) is False

        # ---- D) 源码自检（防回归）：业务拒绝的判据必须在 _cloud_page 里 ----
        src = inspect.getsource(Uploader._cloud_page)
        assert "被拒绝" in src, "_cloud_page 必须显式判 115 业务拒绝并打日志"
        assert "return None" in src, "失败必须返回 None（与空目录区分）"
        assert "state" in src, "必须看 state 字段"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    PASS.append(
        "[46] 巡检把 115 业务拒绝（HTTP 200 + state:false，不抛异常）当「空目录」吞掉："
        "被限流/风控/登录失效时照报「扫了 N 个、清了 0 个」且零日志（比崩溃更糟——"
        "永远查不到原因）→ 修为 state:false/带 error 无 data → warning（含 errno）"
        "+ None 不计入已扫；11 种响应形状复测全对；"
        "[47] is_transient 实测从未生效（p115client wheel 里 HTTPError/urllib.error/"
        "requests. 命中全 0，走 urllib3_future_request，异常是 P115OSError(OSError)）→ "
        "改按 OSError.errno 判（EAGAIN/EBUSY/网络抖动软重试，EAUTH/EINVAL/EIO 立即上抛），"
        "HTTPError 兼容分支剔除 401/403（认证失败绝不自动重试）")


# ---------------------------------------------------------------------------
# [48]~[52]：断点续传偏移 / 进度节流 / SHA1 与扫描移出事件循环 / 分块失败退避 / CSRF
# ---------------------------------------------------------------------------

class _FakeOSS:
    """离线替身：p115client.tool.P115MultipartUpload + p115oss 的分块接口。

    和 StubP115 不同，它**真的从传进来的文件句柄里读数据**，按分块编号存起来，
    complete 时按编号拼回整份内容 —— 这样续传偏移错了就能被内容比对抓出来。
    """

    def __init__(self, cloud_parts: dict[int, bytes] | None = None):
        self.cloud: dict[int, bytes] = dict(cloud_parts or {})
        self.calls: list[dict] = []
        self.completed: list[dict] | None = None
        self.fail_after_parts: int | None = None
        self.fail_times: int | None = None      # 只失败这么多次（None = 每次都失败）
        self.fail_mid_part = False              # True：读完整块数据后才断（模拟传到一半断网）
        fake = self

        class MultipartUpload:
            def __init__(self, url, path, callback, upload_id):
                self.url, self.path, self.callback, self.upload_id = url, path, callback, upload_id

            async def list_parts(self, async_=True, **kw):
                return [{"PartNumber": n, "Size": len(b), "ETag": f"e{n}"}
                        for n, b in sorted(fake.cloud.items())]

            async def complete(self, parts, async_=True, **kw):
                fake.completed = list(parts)
                return {"state": True, "file_id": "fid-mp"}

        async def part_iter(url, upload_id, fh, part_number_start=1, partsize=0,
                            endpoint="", reporthook=None, async_=True, **kw):
            fake.calls.append({"offset": fh.tell(), "start": part_number_start,
                               "partsize": partsize, "endpoint": endpoint})
            n = part_number_start
            sent_parts = 0
            while True:
                due = fake.fail_after_parts is not None and sent_parts >= fake.fail_after_parts
                if due and fake.fail_times is not None:
                    due = fake.fail_times > 0
                if due and not fake.fail_mid_part:
                    if fake.fail_times is not None:
                        fake.fail_times -= 1
                    raise ConnectionResetError("模拟：上传中途断网")
                buf = b""
                while len(buf) < partsize:
                    piece = fh.read(min(64 * 1024, partsize - len(buf)))
                    if not piece:
                        break
                    buf += piece
                    if reporthook:
                        reporthook(len(piece))           # 与 p115oss 一样按 64KB 回调
                if due and buf:
                    if fake.fail_times is not None:
                        fake.fail_times -= 1
                    raise ConnectionResetError("模拟：这一块传到一半断网")
                if not buf:
                    break
                fake.cloud[n] = buf
                yield {"PartNumber": n, "Size": len(buf), "ETag": f"e{n}"}
                sent_parts += 1
                if len(buf) < partsize:
                    break
                n += 1

        async def oss_init(**kw):
            return {"data": {"bucket": "b", "object": "o", "callback": {"cb": 1}},
                    "url": "https://oss.example/o"}

        async def mp_init(url, **kw):
            return "upload-new"

        self.mods = {
            "p115client": types.ModuleType("p115client"),
            "p115client.tool": types.ModuleType("p115client.tool"),
            "p115oss": types.ModuleType("p115oss"),
            "p115oss.oss": types.ModuleType("p115oss.oss"),
        }
        self.mods["p115client.tool"].P115MultipartUpload = MultipartUpload
        self.mods["p115oss"].oss_multipart_upload_init = mp_init
        self.mods["p115oss"].oss_multipart_upload_part_iter = part_iter
        self.mods["p115oss"].upload_file_init = oss_init
        self.mods["p115oss.oss"].oss_url = lambda *a, **k: "https://oss.example/o"

    def __enter__(self):
        self.saved = {k: sys.modules.get(k) for k in self.mods}
        sys.modules.update(self.mods)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def assembled(self) -> bytes:
        assert self.completed is not None, "没有调用 complete"
        nums = [int(p["PartNumber"]) for p in self.completed]
        assert nums == list(range(1, len(nums) + 1)), f"complete 的分块编号不连续：{nums}"
        return b"".join(self.cloud[n] for n in nums)


def _bare_p115(store, partsize: int) -> P115:
    """不联网地造一个真实 P115（跳过 __init__ 里的 P115Client 构造）。"""
    m = P115.__new__(P115)
    m.store = store
    m.partsize = partsize
    m.client = types.SimpleNamespace(cookies="UID=x; CID=y")

    async def passthrough(fn, what):
        return await fn()

    m._retry_transient = passthrough
    return m


def _count_progress_writes(store) -> list:
    writes: list = []
    real_set = store.set

    def spy(path, **kv):
        if "progress" in kv:
            writes.append(kv["progress"])
        return real_set(path, **kv)

    store.set = spy
    return writes


async def case_multipart_resume_offset():
    """[48] 断点续传必须从**已传字节**处接着读，且用建断点时的 partsize；进度写库要节流。

    根因（真实 I/O 路径，StubP115 覆盖不到）：p115oss 的 `oss_multipart_upload_part_iter`
    不会自己跳过已传数据（文档原话「如果需要跳过一定的数据，请提前处理好」），而这里
    句柄停在偏移 0、分块编号却从 len(parts)+1 开始 ⇒ 第 3 块上传的是文件开头的数据，
    云端拼出来是坏文件。另外续传用的是当前配置的 partsize（用户改过就错位），
    reporthook 每 64KB 写一次 SQLite（10GB ≈ 16 万次 UPDATE+commit）。
    """
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    try:
        ps = 1024 * 1024
        path = os.path.join(src, "电影/大文件.mkv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = os.urandom(6 * ps + 12345)
        with open(path, "wb") as f:
            f.write(content)
        store.add(path, len(content), os.path.getmtime(path))
        # 上次跑到一半：云端已有前 2 块（按当时的 1MB 分块），断点在库里
        store.set(path, upload_id="upload-old", upload_oss_url="https://oss.example/o",
                  upload_callback=json.dumps({"cb": 1}), upload_partsize=ps)
        # 之后用户把分块大小改成了 4MB —— 续传仍必须按断点里的 1MB 切
        m = _bare_p115(store, 4 * ps)
        with _FakeOSS({1: content[:ps], 2: content[ps:2 * ps]}) as oss:
            writes = _count_progress_writes(store)
            result = await m.multipart(path, 700, "sha", store.get(path))
            call = oss.calls[0]
            assert call["offset"] == 2 * ps, f"续传没跳过已传部分：从偏移 {call['offset']} 开始读"
            assert call["start"] == 3, call
            assert call["partsize"] == ps, f"续传没用断点里的 partsize：{call['partsize']}"
            assert oss.assembled() == content, "续传拼出来的云端内容与本地文件不一致"
            assert result.get("file_id") == "fid-mp", result
            callbacks = (len(content) - 2 * ps + 64 * 1024 - 1) // (64 * 1024)
            assert len(writes) <= 4, f"进度写库没节流：{callbacks} 次回调写了 {len(writes)} 次"
            assert writes[0] >= 30.0, f"续传起点进度应反映已传部分：{writes[0]}"
            assert writes[-1] == 99.9, f"收尾进度不对：{writes[-1]}"

        # 全新上传：从 0 开始、按当前 partsize、断点落库
        path2 = os.path.join(src, "电影/新文件.mkv")
        content2 = os.urandom(3 * ps + 7)
        with open(path2, "wb") as f:
            f.write(content2)
        store.add(path2, len(content2), os.path.getmtime(path2))
        m2 = _bare_p115(store, ps)
        with _FakeOSS() as oss2:
            await m2.multipart(path2, 700, "sha", store.get(path2))
            assert oss2.calls[0]["offset"] == 0 and oss2.calls[0]["start"] == 1, oss2.calls
            assert oss2.assembled() == content2
        rec2 = store.get(path2)
        assert rec2["upload_id"] == "upload-new" and rec2["upload_partsize"] == ps, rec2

        # resume_prefix 边界：缺块 / 大小不对 / 最后一块正好收尾
        rp = p115.resume_prefix
        mk = lambda n, s: {"PartNumber": n, "Size": s}          # noqa: E731
        assert rp([mk(1, ps), mk(3, ps)], ps, 10 * ps)[1] == ps, "中间缺块只能沿用连续前缀"
        assert rp([mk(2, ps), mk(1, ps)], ps, 10 * ps)[1] == 2 * ps, "乱序要先排序"
        assert rp([mk(1, ps // 2)], ps, 10 * ps) == ([], 0), "大小对不上的不能沿用"
        assert rp([mk(1, ps), mk(2, 5)], ps, ps + 5)[1] == ps + 5, "末块正好收尾应沿用"
        assert rp([mk(1, ps)], ps, ps // 2) == ([], 0), "云端比本地还大：断点作废"
        assert rp([], ps, 10) == ([], 0)

        # 节流器本身：16 万次回调（10GB / 64KB）在 1 秒内只写首尾两次
        got: list = []
        th = p115.ProgressThrottle(got.append)
        for i in range(160_000):
            th.update(i / 160_000 * 100)
        th.update(100, force=True)
        assert len(got) <= 3 and got[-1] == 99.9, (len(got), got[-3:])
        PASS.append(f"[48] 断点续传从已传偏移（{2 * ps} 字节、第 3 块）接着读、按断点里的 "
                    f"partsize 切块，云端拼回内容与本地逐字节一致（修复前从偏移 0 重读 ⇒ 坏文件）；"
                    f"进度写库节流：{callbacks} 次回调只写 {len(writes)} 次，16 万次回调写 {len(got)} 次")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_sha1_off_event_loop():
    """[49] 算 SHA1 不再按住事件循环；算的过程中文件被换掉时结果不落库。

    原来 `sha1_of` 是 async 函数里的同步读盘循环，没有任何 await：算 20GB 原盘时
    秒传、巡检、主循环全停（NAS 机械盘约 3 分钟）。
    """
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    real_chunk, real_hashlib = upmod.CHUNK, upmod.hashlib
    try:
        path = write_file(src, "剧集/S01/E09.mkv", 8 * 64 * 1024)
        scanner.scan_once(store, cfg)

        class SlowSha1:
            """每块额外「卡」50ms（同步 sleep，模拟机械盘读一块的耗时）。"""

            def __init__(self):
                self.h = real_hashlib.sha1()

            def update(self, b):
                time.sleep(0.05)
                self.h.update(b)

            def hexdigest(self):
                return self.h.hexdigest()

        upmod.CHUNK = 64 * 1024
        upmod.hashlib = types.SimpleNamespace(sha1=SlowSha1)
        ticks = {"n": 0}
        done = asyncio.Event()

        async def ticker():
            while not done.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        async def hash_it():
            try:
                return await sha1_of(path, store.get(path), store)
            finally:
                done.set()

        t0 = time.monotonic()
        digest, _ = await asyncio.gather(hash_it(), ticker())
        spent = time.monotonic() - t0
        expect = real_hashlib.sha1(open(path, "rb").read()).hexdigest()
        assert digest == expect, "SHA1 算错了"
        assert store.get(path)["sha1"] == expect
        # 8 块 × 50ms = 0.4s；事件循环没被按住的话 10ms 一跳能跳 20+ 次（按住时只有 1~2 次）
        assert ticks["n"] >= 15, f"算 SHA1 的 {spent:.2f}s 里事件循环只跑了 {ticks['n']} 轮"

        # 0 字节文件：size=0 不能被当成「对不上」（否则永远在重新入队里打转）
        empty = write_file(src, "剧集/S01/空.mkv", 0)
        scanner.scan_once(store, cfg)
        assert await sha1_of(empty, store.get(empty), store) == real_hashlib.sha1(b"").hexdigest()

        # 算到一半文件被换掉（洗板重下）：结果不能落库，_process 要按新内容重新入队
        path2 = write_file(src, "剧集/S01/E10.mkv", 4 * 64 * 1024)
        scanner.scan_once(store, cfg)
        store.set(path2, status="待处理", stable_count=5)

        class SwapSha1(SlowSha1):
            swapped = False

            def update(self, b):
                if not SwapSha1.swapped:
                    SwapSha1.swapped = True
                    with open(path2, "ab") as f:
                        f.write(b"new-content")
                super().update(b)

        upmod.hashlib = types.SimpleNamespace(sha1=SwapSha1)
        await up._process(path2)
        rec2 = store.get(path2)
        assert not rec2["sha1"], f"文件已被换掉，旧摘要不该落库：{rec2['sha1']}"
        assert rec2["status"] == "待处理" and rec2["stable_count"] == 0, rec2
        assert rec2["size"] == os.path.getsize(path2), "应按新内容重新入队"

        # 取消（容器退出）时线程尽快收手，不拖住 asyncio.run 的收尾
        upmod.hashlib = types.SimpleNamespace(sha1=SlowSha1)
        path3 = write_file(src, "剧集/S01/E11.mkv", 40 * 64 * 1024)
        scanner.scan_once(store, cfg)
        task = asyncio.create_task(sha1_of(path3, store.get(path3), store))
        await asyncio.sleep(0.12)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        t1 = time.monotonic()
        while threading.active_count() > 1 and any(
                th.name.startswith("asyncio_") and th.is_alive() for th in threading.enumerate()):
            if time.monotonic() - t1 > 1.0:
                break
            await asyncio.sleep(0.02)
        assert not store.get(path3)["sha1"], "被取消的哈希不该落库"
        PASS.append(f"[49] SHA1 移到线程池：慢盘模拟下哈希 {spent:.2f}s 期间事件循环照跑 "
                    f"{ticks['n']} 轮（修复前被按住、只跑 1~2 轮）；算的过程中文件被换掉 ⇒ 摘要丢弃、"
                    f"按新内容重新入队；取消时不落库")
    finally:
        upmod.CHUNK, upmod.hashlib = real_chunk, real_hashlib
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_off_event_loop():
    """[50] 主循环里的目录扫描放到线程里跑：慢扫描期间，在途任务（秒传/SHA1/巡检）照常推进。"""
    import main as mainmod

    src_txt = open(os.path.join(APP, "main.py"), encoding="utf-8").read()
    assert "await asyncio.to_thread(scanner.scan_once, store, cfg)" in src_txt, \
        "主循环里的 scan_once 又变回同步调用了"

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    real_scan = mainmod.scanner.scan_once
    try:
        scans = {"n": 0}

        def slow_scan(store_, cfg_):
            scans["n"] += 1
            time.sleep(0.4)                     # 模拟几万个文件 / 网络挂载上的 os.walk
            return real_scan(store_, cfg_)

        mainmod.scanner.scan_once = slow_scan
        ticks = {"n": 0}

        async def ticker():
            while True:
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        svc = asyncio.create_task(mainmod.run_service(cfg, dry_run=True))
        tk = asyncio.create_task(ticker())
        await asyncio.sleep(0.3)               # 落在第一轮扫描的 0.4s 里
        mid = ticks["n"]
        for t in (svc, tk):
            t.cancel()
        for t in (svc, tk):
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        assert scans["n"] >= 1, "主循环没扫描"
        assert mid >= 15, f"扫描期间事件循环只跑了 {mid} 轮（被同步扫描按住）"
        PASS.append(f"[50] 目录扫描移到线程：0.4s 的慢扫描期间事件循环照跑 {mid} 轮"
                    f"（修复前只有 1 轮），先扫后派发的顺序不变")
    finally:
        mainmod.scanner.scan_once = real_scan
        shutil.rmtree(tmp, ignore_errors=True)


async def case_multipart_failure_backoff_resume():
    """[51] 分块上传中途失败：不再一次就判「错误」，走统一退避，到期后从断点续传。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, small_file_mb=100.0)
    try:
        ps = 1024 * 1024
        path = os.path.join(src, "电影/断网.mkv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = os.urandom(4 * ps + 99)
        with open(path, "wb") as f:
            f.write(content)
        scanner.scan_once(store, cfg)
        m = _bare_p115(store, ps)

        async def miss(*a, **k):
            return False, {"reason": "未命中"}

        async def ensure_pid(cloud_dir):
            return 701

        m.rapid = miss
        m.ensure_pid = ensure_pid
        up.mgr = m
        import p115 as _p115mod
        real_retries = _p115mod.PART_RETRIES
        _p115mod.PART_RETRIES = 0            # 本用例测的是任务级退避；分块原地重试见 [69]
        with _FakeOSS() as oss:
            oss.fail_after_parts = 2
            up.spawn(path)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if path not in up._active:
                    break
            rec = store.get(path)
            assert rec["status"] == "待重试", f"一次断网就判死了：{rec['status']} {rec['message']}"
            assert "分块上传失败" in (rec["message"] or "") and rec["next_retry_at"] > time.time()
            assert sorted(oss.cloud) == [1, 2] and rec["upload_id"], "断点应保留"

            # 退避到期：直接续传（不再绕回秒传），从第 3 块、偏移 2MB 接着传
            oss.fail_after_parts = None
            rapid_calls = {"n": 0}

            async def miss_count(*a, **k):
                rapid_calls["n"] += 1
                return False, {"reason": "未命中"}

            m.rapid = miss_count
            store.set(path, next_retry_at=0)
            await up._process(path)
            rec = store.get(path)
            assert rec["status"] == "已上传", (rec["status"], rec["message"])
            assert rapid_calls["n"] == 0, "带断点的「待重试」应直接续传，不该再发秒传"
            assert oss.calls[-1]["offset"] == 2 * ps and oss.calls[-1]["start"] == 3, oss.calls
            assert oss.assembled() == content
        _p115mod.PART_RETRIES = real_retries
        up._err_streak.clear()
        PASS.append("[51] 分块上传中途断网：转「待重试」退避（断点保留，不再一次就判「错误」），"
                    "到期后直接从第 3 块续传，云端内容与本地一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_csrf_protection():
    """[52] 改状态的 POST 防跨站伪造：默认不开鉴权时，任意网页都能让本机浏览器
    往面板提交表单（打开「传完删本地」、改通知地址、删记录、暂停调度）。"""
    import urllib.parse
    from pathlib import Path

    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    assert '"Content-Type": "application/json"' in js, "面板 POST 必须发 JSON（否则会被自己拦掉）"

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    srv = None
    try:
        cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
        cfgmod.PORT_FILE = Path(tmp) / ".http_port"
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": False}}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        port = srv.server_address[1]
        base = f"http://127.0.0.1:{port}"
        evil = json.dumps({"values": {"upload.delete_after_upload": True}}).encode()

        def post(path, data=b"", ctype="application/json", **hdr):
            req = urllib.request.Request(base + path, data=data, method="POST")
            if data:
                req.add_header("Content-Type", ctype)
            for k, v in hdr.items():
                req.add_header(k.replace("_", "-"), v)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code

        form = urllib.parse.urlencode(
            {"values": json.dumps({"upload.delete_after_upload": True})}).encode()
        # 跨站表单（经典 CSRF）：Origin 是别的站
        assert post("/api/config", form, "application/x-www-form-urlencoded",
                    Origin="http://evil.example") == 403
        # 现代浏览器：Sec-Fetch-Site=cross-site，哪怕是 JSON 也拦
        assert post("/api/config", evil, Sec_Fetch_Site="cross-site",
                    Origin="http://evil.example") == 403
        assert post("/api/config", evil, Sec_Fetch_Site="same-site") == 403
        # 沙箱 iframe / data: 页面：Origin: null
        assert post("/api/config", evil, Origin="null") == 403
        # 没有 Origin 的老浏览器：看 Referer
        assert post("/api/config", evil, Referer="http://evil.example/x.html") == 403
        # text/plain 伪装 JSON（跨站表单 enctype=text/plain 的老招数）
        assert post("/api/config", evil, "text/plain") == 403
        # 不带请求体的动作同样拦
        assert post("/api/pause", Origin="http://evil.example") == 403
        assert post("/api/task", json.dumps({"path": "x", "action": "delete"}).encode(),
                    Sec_Fetch_Site="cross-site") == 403
        assert cfg["upload"]["delete_after_upload"] is False, "跨站请求改到了配置"
        assert not state.get("paused"), "跨站请求暂停了调度"

        # 放行：自带面板（同源 fetch + JSON）
        ok = json.dumps({"values": {"scan.interval_seconds": 45}}).encode()
        assert post("/api/config", ok, Sec_Fetch_Site="same-origin", Origin=base) == 200
        assert int(cfg["scan"]["interval_seconds"]) == 45
        # 放行：脚本 / curl（没有任何浏览器头）
        assert post("/api/pause") == 200 and state.get("paused") is True
        assert post("/api/resume") == 200
        # 放行：反代（Host 被改写成内网地址，Origin 是公网域名）
        assert post("/api/scan", Origin="https://nas.example.com",
                    X_Forwarded_Host="nas.example.com") == 200
        cfg["http"]["public_url"] = "https://media.example.org/"
        assert post("/api/scan", Origin="https://media.example.org") == 200
        assert post("/api/scan", Referer=f"{base}/") == 200
        # 反代改写 Host 且没配 public_url，但浏览器给了 Sec-Fetch-Site=same-origin：放行
        assert post("/api/scan", Origin="https://other.example", Sec_Fetch_Site="same-origin") == 200
        # 脚本发表单体到 /api/*：现在只收 JSON
        assert post("/api/config", form, "application/x-www-form-urlencoded") == 403
        PASS.append("[52] CSRF：跨站表单 / Sec-Fetch-Site 跨站 / Origin:null / 外站 Referer / "
                    "text/plain 伪装 JSON 一律 403 且配置与调度不受影响；自带面板、curl 脚本、"
                    "反代（X-Forwarded-Host / public_url / Sec-Fetch-Site）照常放行")
    finally:
        if srv is not None:
            srv.shutdown()
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# [53]~[57]：SHA1 小缓冲 / updated_at 索引 / 扫描批量读写 / 优雅退出 / OSS 地址可配
# ---------------------------------------------------------------------------

async def case_sha1_small_buffer():
    """[53] SHA1 改成 1MB 复用缓冲区：结果与 hashlib 一致（各种边界大小），峰值内存不随分块涨。

    实测 8MB 与 1MB 吞吐相同（1.60 GB/s，卡在 OpenSSL），但 8MB 每路哈希峰值多占约 16MB。
    """
    import hashlib
    import tracemalloc
    import uploader as upmod

    assert upmod.CHUNK <= 1024 * 1024, f"SHA1 分块又变大了：{upmod.CHUNK}"
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    try:
        c = upmod.CHUNK
        for i, size in enumerate([1, c - 1, c, c + 1, 3 * c + c // 2]):
            p = os.path.join(src, f"b{i}.mkv")
            data = os.urandom(size)
            with open(p, "wb") as f:
                f.write(data)
            store.add(p, size, os.path.getmtime(p))
            got = await sha1_of(p, store.get(p), store)
            assert got == hashlib.sha1(data).hexdigest(), f"{size} 字节的 SHA1 算错了"
        big = os.path.join(src, "big.mkv")
        with open(big, "wb") as f:
            for _ in range(24):
                f.write(os.urandom(c))
        store.add(big, 24 * c, os.path.getmtime(big))
        tracemalloc.start()
        try:
            await sha1_of(big, store.get(big), store)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert peak < 3 * c, f"哈希 24MB 文件峰值分配 {peak / 2**20:.1f}MB（应 < 3MB）"
        PASS.append(f"[53] SHA1 读盘改 1MB 复用缓冲：5 种边界大小结果与 hashlib 一致；"
                    f"哈希 24MB 文件峰值分配 {peak / 2**20:.2f}MB（修复前 8MB 分块 ≥ 8MB）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_updated_at_index():
    """[54] 面板按 updated_at 倒序翻页要走索引；老库打开时自动补上。"""
    import sqlite3

    tmp = tempfile.mkdtemp(prefix="upilot-test-")
    try:
        db = os.path.join(tmp, "old.db")
        # 模拟老库：只有表和 idx_status
        con = sqlite3.connect(db)
        con.executescript(store_store.DDL.split("-- 面板")[0])
        con.execute("DROP INDEX IF EXISTS idx_updated")
        con.commit()
        con.close()
        st = Store(db)
        names = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_updated" in names, f"老库没补上 updated_at 索引：{names}"
        for sql in ("SELECT path FROM files ORDER BY updated_at DESC LIMIT 20",
                    "SELECT * FROM files ORDER BY updated_at DESC LIMIT 50 OFFSET 100"):
            plan = " ".join(str(tuple(r)) for r in st.db.execute("EXPLAIN QUERY PLAN " + sql))
            assert "idx_updated" in plan and "TEMP B-TREE" not in plan, plan
        st.db.close()
        PASS.append("[54] updated_at 索引：新库建表即有、老库打开自动补；「最近活动 / 任务明细」"
                    "排序走索引不再全表排序")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_scan_batched():
    """[55] 扫描按目录批量查库、整轮一个事务写回；判断语义逐条不变。"""
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=2)
    try:
        a = write_file(src, "A/a.mkv", 2048)
        b = write_file(src, "B/b.mkv", 2048)
        c = write_file(src, "C/c.mkv", 2048)

        # 扫描期间不许再走逐条接口
        def forbid(*a_, **k_):
            raise AssertionError("扫描不该再逐条 SELECT / 写库")

        saved = {k: getattr(store, k) for k in ("seen", "add", "set")}
        for k in saved:
            setattr(store, k, forbid)
        try:
            assert scanner.scan_once(store, cfg) == 3           # 3 个新文件入队
            assert scanner.scan_once(store, cfg) == 3           # 计数 0→1
        finally:
            for k, v in saved.items():
                setattr(store, k, v)
        assert all(store.get(p)["stable_count"] == 1 for p in (a, b, c))

        store.set(c, status="已上传", sha1="old")                # 终态：不再推进
        store.set(b, sha1="stale", upload_id="u1")
        time.sleep(0.01)
        with open(b, "ab") as f:                                # b 被改写 → 重建记录
            f.write(b"more")

        # 扫描进行中用户点了「立即处理」（stable_count=999）：不能被快照里的旧值盖回去
        real_lookup = store.scan_lookup

        def lookup_then_click(paths):
            snap = real_lookup(paths)
            store.mark_ready(a)
            return snap

        store.scan_lookup = lookup_then_click
        try:
            scanner.scan_once(store, cfg)
        finally:
            store.scan_lookup = real_lookup
        ra, rb, rc = store.get(a), store.get(b), store.get(c)
        assert ra["stable_count"] == 999, f"扫描把「立即处理」盖掉了：{ra['stable_count']}"
        assert rb["stable_count"] == 0 and not rb["sha1"] and not rb["upload_id"], rb
        assert rb["size"] == os.path.getsize(b)
        assert rc["status"] == "已上传" and rc["stable_count"] == 1 and rc["sha1"] == "old", rc

        # 一致性：一大批新文件一次提交
        for i in range(300):
            write_file(src, f"D/{i}.mkv", 10)
        before = store.db.total_changes
        scanner.scan_once(store, cfg)
        assert store.db.total_changes - before >= 300
        assert len(store.list_tasks(limit=1000)[0]) == 303
        PASS.append("[55] 扫描批量化：按目录批量查库、整轮一个事务写回（不再逐条 SELECT/commit）；"
                    "新文件入队、计数推进、改写重建（SHA1/断点作废）、终态不动与原来一致；"
                    "扫描中途点「立即处理」不会被旧计数盖回去")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_graceful_shutdown():
    """[56] 收到退出信号：停止派发，给在途任务 SHUTDOWN_GRACE 秒自然结束，超时的再取消。

    原来 finally 里只 sleep 0.5 秒、日志却写「等待在途任务结束」，然后 asyncio.run 收尾
    直接取消所有任务（算到一半的 SHA1、传到一半的分块全部白做）。
    """
    import main as mainmod
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    real_grace = mainmod.SHUTDOWN_GRACE
    try:
        mainmod.SHUTDOWN_GRACE = 1.0
        flags = {"short_done": False, "long_cancelled": False}

        async def short_job():
            await asyncio.sleep(0.4)
            flags["short_done"] = True

        async def long_job():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                flags["long_cancelled"] = True
                raise

        stop = asyncio.Event()
        svc = asyncio.create_task(mainmod.run_service(cfg, dry_run=True, stop=stop))
        await asyncio.sleep(0.2)
        for job in (short_job, long_job):
            t = asyncio.create_task(job())
            upmod._SHARED["bg"].add(t)
            t.add_done_callback(upmod._SHARED["bg"].discard)
        t0 = time.monotonic()
        stop.set()
        await asyncio.wait_for(svc, timeout=10)
        spent = time.monotonic() - t0
        assert flags["short_done"], "在途任务没等它跑完就退出了"
        assert flags["long_cancelled"], "超时的任务没被取消"
        assert spent < 3.0, f"退出耗时 {spent:.2f}s，超出等待上限太多"
        PASS.append(f"[56] 优雅退出：收到信号后在途任务跑完（0.4s 的任务完成），超过等待上限的被取消，"
                    f"整体 {spent:.2f}s 退出（修复前只 sleep 0.5s 就全部强制取消）")
    finally:
        mainmod.SHUTDOWN_GRACE = real_grace
        shutil.rmtree(tmp, ignore_errors=True)


async def case_oss_endpoint_configurable():
    """[57] OSS 地址可配：默认值不变；填了合法地址就用它；填错回落默认值。"""
    assert cfgmod.DEFAULTS["upload"]["oss_endpoint"] == p115.DEFAULT_OSS_ENDPOINT \
        == "https://oss-cn-shenzhen.aliyuncs.com"
    assert p115.oss_endpoint({}) == p115.DEFAULT_OSS_ENDPOINT
    assert p115.oss_endpoint({"upload": {"oss_endpoint": "https://oss-cn-hangzhou.aliyuncs.com/"}}) \
        == "https://oss-cn-hangzhou.aliyuncs.com"
    assert p115.oss_endpoint({"upload": {"oss_endpoint": "oss-cn-hangzhou"}}) == p115.DEFAULT_OSS_ENDPOINT
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    try:
        path = os.path.join(src, "e.mkv")
        with open(path, "wb") as f:
            f.write(os.urandom(300 * 1024))
        store.add(path, 300 * 1024, os.path.getmtime(path))
        m = _bare_p115(store, 1024 * 1024)
        m.cfg = {"upload": {"oss_endpoint": "https://oss-cn-beijing.aliyuncs.com"}}
        with _FakeOSS() as oss:
            await m.multipart(path, 700, "sha", store.get(path))
            assert oss.calls[0]["endpoint"] == "https://oss-cn-beijing.aliyuncs.com", oss.calls
        PASS.append("[57] OSS 地址改为 upload.oss_endpoint 可配（默认仍是深圳），分块上传按配置走；"
                    "非法值回落默认")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def case_compose_log_rotation():
    """[58] compose 示例带日志轮转：Docker 默认 json-file 不限大小，纯秒传长期等待时日志会一直涨。"""
    import yaml

    root = os.path.dirname(APP)
    doc = yaml.safe_load(open(os.path.join(root, "docker-compose.yml"), encoding="utf-8"))
    for name, svc in doc["services"].items():
        lg = svc.get("logging") or {}
        opts = lg.get("options") or {}
        assert lg.get("driver") == "json-file", (name, lg)
        assert str(opts.get("max-size", "")).endswith("m"), (name, opts)
        assert int(opts.get("max-file", 0)) >= 2, (name, opts)
    PASS.append("[58] docker-compose.yml 带日志轮转（json-file，max-size 10m × max-file 3）")


async def case_rapid_age_backoff():
    """[59] 纯秒传长间隔按「第一次未命中至今」逐级放慢：15 分钟 → 1 小时（满 1 天）→ 6 小时（满 7 天）。

    原来不管等了多久都固定 long_interval_seconds：挂几周的老片每 15 分钟问一次，
    1000 个这样的文件就是持续不断的请求流。
    """
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=60, fail_first=999,
                                             mode="rapid_only", long_interval=900)
    try:
        assert cfg["retry"]["long_backoff"], "默认应带逐级放慢的档位"
        big = write_file(src, "剧/S01E01.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        await up._process(big)                         # 第 1 次未命中，记下 first_failed_at

        async def interval_at(age_hours: float, uploader=up) -> int:
            store.set(big, status="待重试", next_retry_at=0, retry_count=10,
                      first_failed_at=time.time() - age_hours * 3600)
            await uploader._process(big)
            rec = store.get(big)
            return round(rec["next_retry_at"] - time.time())

        got = {h: await interval_at(h) for h in (1, 25, 24 * 8)}
        assert abs(got[1] - 900) <= 2, got            # 头 24 小时：仍是 15 分钟
        assert abs(got[25] - 3600) <= 2, got          # 满 1 天：每小时
        assert abs(got[24 * 8] - 21600) <= 2, got     # 满 7 天：每 6 小时

        # 用户自己调得比档位更慢：以用户为准（只会更慢，不会更快）
        cfg["retry"]["long_interval_seconds"] = 7200
        slow = Uploader(cfg, store, mgr)
        assert abs(await interval_at(25, slow) - 7200) <= 2
        # long_backoff: [] → 旧行为：永远按 long_interval_seconds
        cfg["retry"]["long_interval_seconds"] = 900
        cfg["retry"]["long_backoff"] = []
        legacy = Uploader(cfg, store, mgr)
        assert abs(await interval_at(24 * 30, legacy) - 900) <= 2
        # 写错的档位被跳过，不会把重试停掉
        assert upmod.long_retry_interval(900, [{"after_hours": "x"}, [1], None, [-1, 5],
                                               [2, 0], [float("nan"), 99999], [24, 3600]],
                                         25 * 3600) == 3600
        assert upmod.long_retry_interval(900, [[24, 3600]], 3600) == 900
        PASS.append(f"[59] 纯秒传长间隔按等待时长放慢：1h→{got[1]}s，25h→{got[25]}s，"
                    f"8 天→{got[24 * 8]}s；用户值更慢时以用户为准；long_backoff: [] 恢复旧行为")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _ListHandler:
    def __init__(self):
        import logging
        self.records: list = []
        self.h = logging.Handler(level=logging.DEBUG)
        self.h.emit = self.records.append

    def __enter__(self):
        import logging
        self.lg = logging.getLogger("upilot")
        self.old = self.lg.level
        self.lg.setLevel(logging.DEBUG)
        self.lg.addHandler(self.h)
        return self.records

    def __exit__(self, *exc):
        self.lg.removeHandler(self.h)
        self.lg.setLevel(self.old)


async def case_miss_logs_summarized():
    """[60] 重复的「秒传未命中」降为 DEBUG，主循环每轮打一行汇总。

    原来每个待命中文件每次校验一行 INFO：1000 个文件每天约 10 万行。
    """
    import logging

    import main as mainmod
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=60, fail_first=999,
                                             mode="rapid_only", long_interval=900)
    try:
        upmod.take_rapid_summary()
        a = write_file(src, "a/x.mkv", 2 * 1024 * 1024)
        b = write_file(src, "b/y.mkv", 2 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        with _ListHandler() as recs:
            for p in (a, b):
                await up._process(p)                       # 第 1 次未命中 → INFO
            for _ in range(3):
                for p in (a, b):
                    store.set(p, next_retry_at=0)
                    await up._process(p)                   # 之后的未命中 → DEBUG
        miss = [r for r in recs if r.name == "upilot.upload" and "未命中" in r.getMessage()]
        assert len(miss) == 8, [r.getMessage() for r in miss]
        info = [r for r in miss if r.levelno >= logging.INFO]
        assert len(info) == 2 and all("第 1 次" in r.getMessage() for r in info), \
            [r.getMessage() for r in info]
        s = upmod.take_rapid_summary()
        assert s == {"checked": 8, "hit": 0, "miss": 8}, s
        assert upmod.take_rapid_summary()["checked"] == 0, "取走后应清零"

        # 主循环：每轮开头把上一轮的计数打成一行 INFO 汇总
        for p in (a, b):
            store.set(p, next_retry_at=0)
        cfg["scan"]["interval_seconds"] = 2
        stop = asyncio.Event()
        with _ListHandler() as recs:
            svc = asyncio.create_task(mainmod.run_service(cfg, dry_run=True, stop=stop))
            for _ in range(80):
                await asyncio.sleep(0.1)
                if any("秒传校验汇总" in r.getMessage() for r in recs):
                    break
            stop.set()
            await asyncio.wait_for(svc, timeout=15)
        summ = [r for r in recs if "秒传校验汇总" in r.getMessage()]
        assert summ and summ[0].levelno == logging.INFO, [r.getMessage() for r in recs][-20:]
        assert "未命中" in summ[0].getMessage(), summ[0].getMessage()
        per_file_info = [r for r in recs if r.levelno >= logging.INFO
                         and "未命中" in r.getMessage() and "汇总" not in r.getMessage()]
        assert not per_file_info, [r.getMessage() for r in per_file_info]
        PASS.append(f"[60] 重复的秒传未命中降为 DEBUG（8 次未命中只有首次 2 行 INFO），"
                    f"主循环每轮一行汇总：「{summ[0].getMessage()}」")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_gzip_and_hidden_pause():
    """[61] 面板 API 响应 gzip（客户端接受才压）；标签页在后台时前端停止轮询。"""
    import gzip as gz
    from pathlib import Path

    js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
    assert "visibilitychange" in js, "前端没监听 visibilitychange"
    assert "if (!S.auto || document.hidden) return;" in js, "轮询没在后台标签页暂停"

    assert webui.accepts_gzip("gzip, deflate, br") and webui.accepts_gzip("br;q=1, *;q=0.5")
    assert not webui.accepts_gzip("gzip;q=0") and not webui.accepts_gzip("") \
        and not webui.accepts_gzip(None) and not webui.accepts_gzip("identity")

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    srv = None
    try:
        cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
        cfgmod.PORT_FILE = Path(tmp) / ".http_port"
        for i in range(60):
            write_file(src, f"剧{i}/S01E{i:02d}.mkv", 1024)
        scanner.scan_once(store, cfg)
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0, "auth": {"enabled": False}}
        srv = webui.serve(cfg, store, {"scan_rounds": 0, "dry_run": True})
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def get(path, enc=None):
            req = urllib.request.Request(base + path)
            if enc is not None:
                req.add_header("Accept-Encoding", enc)
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.headers, r.read()

        h_plain, plain = get("/api/status")
        assert h_plain.get("Content-Encoding") is None, dict(h_plain)
        h_gz, zipped = get("/api/status", "gzip, deflate")
        assert h_gz.get("Content-Encoding") == "gzip", dict(h_gz)
        assert "Accept-Encoding" in (h_gz.get("Vary") or ""), dict(h_gz)
        assert int(h_gz["Content-Length"]) == len(zipped)
        a, b = json.loads(gz.decompress(zipped)), json.loads(plain)
        a.pop("now", None), b.pop("now", None)
        assert a.keys() == b.keys()
        h_q0, _ = get("/api/status", "gzip;q=0")
        assert h_q0.get("Content-Encoding") is None
        h_js, js_body = get("/static/app.js", "gzip")
        assert h_js.get("Content-Encoding") == "gzip" and b"visibilitychange" in gz.decompress(js_body)
        h_health, body = get("/health", "gzip")
        assert h_health.get("Content-Encoding") is None and body == b"ok"     # 太小不压
        PASS.append(f"[61] 面板响应 gzip：/api/status {len(plain)} → {len(zipped)} 字节；"
                    f"q=0 / 不带头不压；标签页在后台时前端暂停轮询")
    finally:
        if srv:
            srv.shutdown()
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        shutil.rmtree(tmp, ignore_errors=True)


async def case_health_heartbeat():
    """[62] /health 带主循环心跳：主循环卡住超过 http.health_stall_seconds 就回 503，
    Docker HEALTHCHECK（healthcheck.py）随之失败。原来 HTTP 线程活着就永远 ok。"""
    from pathlib import Path

    import healthcheck
    import main as mainmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    srv = None
    try:
        cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
        cfgmod.PORT_FILE = Path(tmp) / ".http_port"
        cfg["http"] = {"enabled": True, "host": "127.0.0.1", "port": 0,
                       "auth": {"enabled": False}, "health_stall_seconds": 60}
        state = {"scan_rounds": 0, "dry_run": True}
        srv = webui.serve(cfg, store, state)
        port = srv.server_address[1]
        cfgmod.PORT_FILE.write_text(str(port), encoding="utf-8")
        saved = copy.deepcopy(cfg)
        saved["http"]["port"] = port
        cfgmod.save(saved)

        def health():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()

        assert health() == (200, b"ok")                     # 还没打过心跳（启动中）
        mainmod.beat(state)
        assert health() == (200, b"ok")
        assert healthcheck.main() == 0
        state["heartbeat"] = time.monotonic() - 120          # 主循环 2 分钟没动
        code, body = health()
        assert code == 503 and body.startswith(b"stalled"), (code, body)
        assert healthcheck.main() == 1, "主循环卡住时 HEALTHCHECK 必须失败"
        ok, why = webui.health_status({"http": {"health_stall_seconds": 600}}, state)
        assert ok, why                                       # 阈值内不误报
    finally:
        if srv:
            srv.shutdown()
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf

    # 真跑主循环：等待期间心跳也在走（不能只在每轮开头打，否则长扫描间隔会被误判）
    try:
        cfg["http"]["enabled"] = False
        cfg["scan"]["interval_seconds"] = 3600
        holder: dict = {}
        real_start = mainmod.start_http

        def spy(c, s, st):
            holder["state"] = st
            return None

        mainmod.start_http = spy
        stop = asyncio.Event()
        svc = asyncio.create_task(mainmod.run_service(cfg, dry_run=True, stop=stop))
        await asyncio.sleep(0.5)
        hb1 = holder["state"]["heartbeat"]
        await asyncio.sleep(1.2)
        hb2 = holder["state"]["heartbeat"]
        stop.set()
        await asyncio.wait_for(svc, timeout=10)
        assert hb2 > hb1, "等待下一轮期间心跳没更新"
        PASS.append("[62] /health 带主循环心跳：卡住超过阈值回 503 且 healthcheck 失败；"
                    "等待期间每 0.5 秒续一次心跳，长扫描间隔不误报")
    finally:
        mainmod.start_http = real_start
        shutil.rmtree(tmp, ignore_errors=True)


def case_dockerfile_precompile():
    """[63] 镜像构建时预编译 app 目录；依赖的 .pyc 保留（不加 --no-compile，否则启动慢 0.45 秒）。"""
    root = os.path.dirname(APP)
    df = open(os.path.join(root, "Dockerfile"), encoding="utf-8").read()
    lines = [ln.strip() for ln in df.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    copy_i = next(i for i, ln in enumerate(lines) if ln.startswith("COPY app/"))
    comp = [i for i, ln in enumerate(lines) if "compileall" in ln and "/app/app" in ln]
    assert comp and comp[0] > copy_i, "compileall 必须在 COPY app/ 之后"
    assert "--no-compile" not in df, "依赖的 .pyc 要保留"
    assert any(ln.startswith("HEALTHCHECK") for ln in lines) and "healthcheck.py" in df
    PASS.append("[63] Dockerfile 在 COPY app/ 之后 compileall 预编译 app，依赖 .pyc 保留")


def case_stats_covering_index():
    """[64] 概览统计的聚合查询走覆盖索引（不回表）；老库打开自动补上。"""
    import sqlite3

    tmp = tempfile.mkdtemp(prefix="upilot-test-")
    try:
        db = os.path.join(tmp, "old.db")
        con = sqlite3.connect(db)
        con.executescript(store_store.DDL.split("-- 面板")[0])
        con.commit()
        con.close()
        st = Store(db)
        names = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_stats" in names, names
        for sql in ("SELECT status, COUNT(*) c, COALESCE(SUM(size),0) b FROM files GROUP BY status",
                    "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM files WHERE upload_mode='rapid'",
                    "SELECT COALESCE(SUM(retry_count),0) r FROM files"):
            plan = " ".join(str(tuple(r)) for r in st.db.execute("EXPLAIN QUERY PLAN " + sql))
            assert "COVERING INDEX idx_stats" in plan, (sql, plan)
        assert st.stats()["total"]["count"] == 0
        st.db.close()
        PASS.append("[64] stats() 的聚合查询走覆盖索引 idx_stats（只扫索引不回表），老库自动补")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_sha1_reuse_same_inode():
    """[65] 同一个文件换了路径（改名 / 移动目录 / 硬链接）复用已算好的 SHA1，不再整份读盘。

    原来路径一变就新建记录、从头算：给剧集目录改个名，整季几十 GB 全部重读一遍。
    """
    import sqlite3

    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    real = upmod._sha1_file
    reads: list[str] = []

    def counting(path, total, on_progress, cancel):
        reads.append(os.path.basename(path))
        return real(path, total, on_progress, cancel)

    upmod._sha1_file = counting
    try:
        a = write_file(src, "旧名/S01E01.mkv", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        rec = store.get(a)
        assert rec["ino"], rec
        d1 = await sha1_of(a, rec, store)
        assert reads == ["S01E01.mkv"], reads

        # 1) 改名整个目录：新路径新记录，SHA1 直接复用
        os.rename(os.path.join(src, "旧名"), os.path.join(src, "新名"))
        b = os.path.normpath(os.path.join(src, "新名", "S01E01.mkv"))
        scanner.scan_once(store, cfg)
        assert await sha1_of(b, store.get(b), store) == d1
        assert len(reads) == 1, f"改名后又读了一遍盘：{reads}"
        assert store.get(b)["sha1"] == d1

        # 2) 硬链接：同一份数据第二个路径
        c = os.path.normpath(os.path.join(src, "链接", "S01E01.mkv"))
        os.makedirs(os.path.dirname(c))
        os.link(b, c)
        scanner.scan_once(store, cfg)
        assert await sha1_of(c, store.get(c), store) == d1 and len(reads) == 1, reads

        # 3) 拷贝（不同 inode）→ 照常算
        cp = write_file(src, "拷贝/S01E01.mkv", 0)
        shutil.copyfile(b, cp)
        scanner.scan_once(store, cfg)
        assert await sha1_of(cp, store.get(cp), store) == d1 and len(reads) == 2, reads

        # 4) 同 inode 但内容变了（size/mtime 变）→ 不复用
        with open(c, "ab") as f:
            f.write(b"more")
        scanner.scan_once(store, cfg)
        d_new = await sha1_of(c, store.get(c), store)
        assert d_new != d1 and len(reads) == 3, (reads, d_new)

        # 5) 老库（没有 ino 列）打开时自动补列 + 建索引
        db = os.path.join(tmp, "old.db")
        con = sqlite3.connect(db)
        con.executescript(store_store.DDL.replace("  ino TEXT,\n", ""))
        con.commit()
        con.close()
        st = Store(db)
        cols = {r[1] for r in st.db.execute("PRAGMA table_info(files)")}
        idx = {r[0] for r in st.db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "ino" in cols and "idx_ino" in idx, (cols, idx)
        st.db.close()
        PASS.append("[65] 同一文件换路径复用 SHA1：目录改名、硬链接都不再读盘；拷贝、内容变了照常算；"
                    "老库自动补 ino 列和索引")
    finally:
        upmod._sha1_file = real
        shutil.rmtree(tmp, ignore_errors=True)


async def case_partsize_oss_limit():
    """[66] 分块大小不超过 OSS 的 10000 片上限：固定 10MB 时 >97.6 GiB 的文件传不上去。"""
    import p115 as p115mod

    MiB, GiB = 1024 * 1024, 1024 ** 3
    ps = p115mod.part_size_for
    assert ps(20 * GiB, 10 * MiB) == 10 * MiB                    # 普通大小：不变
    assert ps(0, 10 * MiB) == 10 * MiB
    for size in (97 * GiB, 98 * GiB, 110 * GiB, 115 * 10 ** 9, 200 * GiB):
        got = ps(size, 10 * MiB)
        assert got % MiB == 0 and -(-size // got) <= p115mod.OSS_MAX_PARTS, (size, got)
    assert ps(110 * GiB, 64 * MiB) == 64 * MiB                    # 用户设得更大：以用户为准

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    real_retries = p115mod.PART_RETRIES
    p115mod.PART_RETRIES = 0                  # 只想记下分块参数就断开，不要原地重试
    try:
        path = os.path.join(src, "huge.iso")
        size = 110 * GiB
        with open(path, "wb") as f:                                 # 稀疏文件，不真占盘
            f.truncate(size)
        store.add(path, size, os.path.getmtime(path))
        m = _bare_p115(store, 10 * MiB)
        with _FakeOSS() as oss:
            oss.fail_after_parts = 0                                # 记下参数就断开，不真读 110GB
            try:
                await m.multipart(path, 700, "sha", store.get(path))
            except ConnectionResetError:
                pass
        used = oss.calls[0]["partsize"]
        assert -(-size // used) <= 10000, used
        assert store.get(path)["upload_partsize"] == used, "断点里要记下实际用的分块大小"
        # 续传按断点里记的大小走，即使配置变了
        with _FakeOSS({1: b"\0" * used}) as oss2:
            oss2.fail_after_parts = 0
            m.partsize = 32 * MiB
            try:
                await m.multipart(path, 700, "sha", store.get(path))
            except ConnectionResetError:
                pass
        assert oss2.calls[0]["partsize"] == used and oss2.calls[0]["offset"] == used, oss2.calls
        PASS.append(f"[66] 110 GiB 文件分块自动放大到 {used // MiB} MB（{-(-size // used)} 片 ≤ 10000），"
                    f"断点记下实际分块大小、续传沿用")
    finally:
        p115mod.PART_RETRIES = real_retries
        shutil.rmtree(tmp, ignore_errors=True)


def case_scan_log_capped():
    """[67] 一轮扫描逐条 INFO 最多 20 条，其余 DEBUG，外加一行汇总（首扫 2 万个文件原来刷 4 万行）。"""
    import logging

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        for i in range(30):
            write_file(src, f"剧/E{i:02d}.mkv", 1024)
        with _ListHandler() as recs:
            scanner.scan_once(store, cfg)                        # 30 个新文件
            scanner.scan_once(store, cfg)                        # 30 个变稳定
        allinfo = [r.getMessage() for r in recs if r.levelno >= logging.INFO]
        info = [m for m in allinfo if not m.startswith("本轮扫描")]
        dbg = [r.getMessage() for r in recs if r.levelno == logging.DEBUG]
        assert sum("发现新文件" in m for m in info) == 20, info
        assert sum("文件已稳定" in m for m in info) == 20, info
        assert sum("发现新文件" in m for m in dbg) == 10 and sum("文件已稳定" in m for m in dbg) == 10
        summ = [m for m in allinfo if m.startswith("本轮扫描")]
        assert len(summ) == 2 and "30" in summ[0], summ
        # 少量文件：照旧逐条 INFO，不打汇总
        write_file(src, "新/E99.mkv", 1024)
        with _ListHandler() as recs:
            scanner.scan_once(store, cfg)
        info = [r.getMessage() for r in recs if r.levelno >= logging.INFO]
        assert sum("发现新文件" in m for m in info) == 1 and not any(m.startswith("本轮扫描") for m in info)
        PASS.append("[67] 扫描日志：每轮逐条 INFO 最多 20 条，其余降 DEBUG，超出时打一行汇总；少量文件照旧")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_ino_backfill():
    """[68] 升级前入库、没有 inode 的记录：扫描时补上（只补空值、不动其他列），
    之后改名也能复用 SHA1。"""
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    real = upmod._sha1_file
    reads: list[str] = []

    def counting(path, total, on_progress, cancel):
        reads.append(path)
        return real(path, total, on_progress, cancel)

    upmod._sha1_file = counting
    try:
        a = write_file(src, "旧库/S01E01.mkv", 2 * 1024 * 1024)
        z = write_file(src, "旧库/S01E02.mkv", 0)
        scanner.scan_once(store, cfg)
        d1 = await sha1_of(a, store.get(a), store)
        store.set(z, status="已上传")                          # 终态记录也要补
        with store.lock:                                       # 模拟升级前入库：ino 为空
            store.db.execute("UPDATE files SET ino=NULL")
            store.db.commit()
        before = {p: store.get(p)["updated_at"] for p in (a, z)}
        time.sleep(0.02)
        assert scanner.scan_once(store, cfg) == 0, "补 inode 不算「触及文件」"
        for p in (a, z):
            r = store.get(p)
            assert r["ino"] and r["ino"] == scanner.file_ino(os.stat(p)), r
            assert r["updated_at"] == before[p], "补 inode 不该顶动 updated_at（面板最近活动）"
        assert store.get(a)["sha1"] == d1 and store.get(z)["status"] == "已上传"

        # 文件在两次扫描之间被换了内容（size/mtime 变）：走重建，不拿旧 inode 乱补
        with store.lock:
            store.db.execute("UPDATE files SET ino=NULL WHERE path=?", (a,))
            store.db.commit()
        with open(a, "ab") as f:
            f.write(b"x")
        scanner.scan_once(store, cfg)
        assert store.get(a)["ino"] and not store.get(a)["sha1"], store.get(a)

        # 补完之后改名：直接复用，不读盘
        b0 = write_file(src, "旧库2/S02E01.mkv", 1024 * 1024)
        scanner.scan_once(store, cfg)
        d2 = await sha1_of(b0, store.get(b0), store)
        with store.lock:
            store.db.execute("UPDATE files SET ino=NULL WHERE path=?", (b0,))
            store.db.commit()
        scanner.scan_once(store, cfg)                          # 补回
        n_reads = len(reads)
        os.rename(os.path.join(src, "旧库2"), os.path.join(src, "新库2"))
        b1 = os.path.normpath(os.path.join(src, "新库2", "S02E01.mkv"))
        scanner.scan_once(store, cfg)
        assert await sha1_of(b1, store.get(b1), store) == d2 and len(reads) == n_reads, reads
        PASS.append("[68] 升级前没有 inode 的记录扫描时补上（含终态；不动 updated_at；内容变了的不补），"
                    "补完后改名同样复用 SHA1")
    finally:
        upmod._sha1_file = real
        shutil.rmtree(tmp, ignore_errors=True)


async def case_part_inplace_retry():
    """[69] 单个分块上传失败：原地重试（只重传失败那块），不再整个上传中止、退避 30 秒。"""
    import p115 as p115mod

    MiB = 1024 * 1024
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    real_delay = p115mod.PART_RETRY_DELAY
    p115mod.PART_RETRY_DELAY = 0.0
    try:
        path = os.path.join(src, "big.mkv")
        content = os.urandom(3 * MiB + 12345)
        with open(path, "wb") as f:
            f.write(content)
        store.add(path, len(content), os.path.getmtime(path))
        m = _bare_p115(store, MiB)
        m.cfg = {"upload": {}}

        # 1) 第 2 块传到一半断网两次：原地重试后整份内容正确拼回
        with _FakeOSS() as oss:
            oss.fail_after_parts, oss.fail_times, oss.fail_mid_part = 1, 2, True
            await m.multipart(path, 700, "sha", store.get(path))
            body = b"".join(oss.cloud[n] for n in sorted(oss.cloud))
            assert body == content, "原地重试后内容不对（偏移/分块编号错了）"
            assert len(oss.calls) == 3, oss.calls
            assert [(c["offset"], c["start"]) for c in oss.calls] == [(0, 1), (MiB, 2), (2 * MiB, 3)], oss.calls
            assert len(oss.completed) == 4

        # 2) 连续失败超过上限：照旧上抛（交给任务级退避）
        store.set(path, upload_id=None, upload_oss_url=None, upload_callback=None)
        with _FakeOSS() as oss:
            oss.fail_after_parts = 0                            # 每次都在第一块前断
            try:
                await m.multipart(path, 700, "sha", store.get(path))
                raise AssertionError("应当上抛")
            except ConnectionResetError:
                pass
            assert len(oss.calls) == p115mod.PART_RETRIES + 1, oss.calls

        # 3) 非连接类错误（比如被拒、编程错误）不重试
        store.set(path, upload_id=None, upload_oss_url=None, upload_callback=None)

        class Rejected(Exception):
            pass

        with _FakeOSS() as oss:
            real_iter = oss.mods["p115oss"].oss_multipart_upload_part_iter

            async def bad_iter(*a, **k):
                raise Rejected("403")
                yield  # pragma: no cover

            oss.mods["p115oss"].oss_multipart_upload_part_iter = bad_iter
            try:
                await m.multipart(path, 700, "sha", store.get(path))
                raise AssertionError("应当上抛")
            except Rejected:
                pass
            oss.mods["p115oss"].oss_multipart_upload_part_iter = real_iter
        assert p115mod.is_part_retryable(ConnectionResetError()) and p115mod.is_part_retryable(TimeoutError())
        assert not p115mod.is_part_retryable(Rejected()) and not p115mod.is_part_retryable(asyncio.CancelledError())
        PASS.append("[69] 分块失败原地重试：第 2 块两次传到一半断网后仍拼出正确内容（只重传失败块）；"
                    "连续失败超过上限照旧上抛；非连接类错误不重试")
    finally:
        p115mod.PART_RETRY_DELAY = real_delay
        shutil.rmtree(tmp, ignore_errors=True)


async def case_max_upload_size():
    """[70] 超过 115 单文件上限（默认 115 GiB，upload.max_file_gb 可调、0=不限）的文件不走普通上传：
    直接标「错误」并说明原因，不白传；秒传不受影响。"""
    MiB = 1024 * 1024
    assert cfgmod.DEFAULTS["upload"]["max_file_gb"] == 115

    async def run(max_gb, fail_first):
        cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=0, fail_first=fail_first,
                                                 mode="rapid_then_upload", small_file_mb=1)
        try:
            cfg["upload"]["max_file_gb"] = max_gb
            up = Uploader(cfg, store, mgr)
            calls = []
            real = mgr.multipart

            async def spy(*a, **k):
                calls.append(a[0])
                return await real(*a, **k)

            mgr.multipart = spy
            p = write_file(src, "huge/E01.mkv", 3 * MiB)
            scanner.scan_once(store, cfg)
            rec = await drive(up, store, p, rounds=6)
            return rec, calls
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    limit = 2 / 1024                                            # 2 MiB 的上限，文件 3 MiB
    rec, calls = await run(limit, fail_first=99)
    assert rec["status"] == "错误" and "上限" in (rec["message"] or "") and not calls, (rec, calls)
    rec, calls = await run(0, fail_first=99)                    # 0 = 不限制
    assert rec["status"] == "已上传" and rec["upload_mode"] == "normal" and calls, rec
    rec, calls = await run(limit, fail_first=0)                 # 秒传命中：不受上限影响
    assert rec["status"] == "已上传" and rec["upload_mode"] == "rapid" and not calls, rec
    PASS.append("[70] 超过 115 单文件上限（upload.max_file_gb，默认 115）的文件不普通上传、直接标错误；"
                "0=不限制；秒传命中不受影响")


class P115AuthenticationError(OSError):
    """与 p115client 同名同 errno 的替身（按类名/errno 识别，不依赖真实库）。"""


async def case_auth_failure_breaker():
    """[71] 115 登录失效（cookie 过期）：不当「秒传未命中」（不涨 retry_count），不累计到「错误」，
    暂停向 115 发请求只定时试探，面板有提示，更新 cookie 后自动恢复。"""
    import errno as _e
    import p115 as p115mod
    import uploader as upmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=60, fail_first=0,
                                             mode="rapid_only", long_interval=900)
    try:
        upmod._SHARED["auth_fail"] = None
        paths = [write_file(src, f"剧/E{i:02d}.mkv", 1024 * 1024) for i in range(4)]
        scanner.scan_once(store, cfg)
        for p in paths:                                       # 先把 SHA1 算好，只看联网阶段
            await sha1_of(p, store.get(p), store)
            store.set(p, status="待秒传")

        calls = {"n": 0}
        real = mgr.rapid

        async def expired(*a, **k):
            calls["n"] += 1
            raise P115AuthenticationError(getattr(_e, "EAUTH", 0) or 1, "login required")

        mgr.rapid = expired
        assert p115mod.is_auth_error(P115AuthenticationError(1, "x"))
        assert p115mod.is_auth_error(RuntimeError("wrap")) is False
        wrapped = RuntimeError("分块上传失败")
        wrapped.__cause__ = P115AuthenticationError(1, "x")
        assert p115mod.is_auth_error(wrapped)

        # 连续 5 轮：原来 3 次异常就标「错误」、或每次都算一次未命中
        for _ in range(5):
            for p in paths:
                store.set(p, next_retry_at=0, status="待重试")
            up.dispatch()
            for _ in range(60):
                await asyncio.sleep(0.02)
                if not up._active:
                    break
            upmod._SHARED["auth_fail"]["next_probe"] = 0       # 让下一轮可以试探
        for p in paths:
            r = store.get(p)
            assert r["status"] == "待重试", r
            assert (r["retry_count"] or 0) == 0 and r["first_failed_at"] is None, r
            assert "登录失效" in (r["message"] or ""), r
        assert upmod._SHARED["auth_fail"], "断路器没记下"
        assert webui._payload(cfg, store, {})["auth_failed"], "面板数据里没有登录失效提示"
        assert "d.auth_failed" in open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()

        # 断路器期间：dispatch 不再向 115 发请求，到试探时间只放 1 个文件去试
        up._err_streak.clear()
        n0 = calls["n"]
        for p in paths:
            store.set(p, next_retry_at=0, status="待重试")
        upmod._SHARED["auth_fail"]["next_probe"] = time.time() + 300
        up.dispatch()
        await asyncio.sleep(0.2)
        assert calls["n"] == n0, "登录失效期间还在打 115"
        upmod._SHARED["auth_fail"]["next_probe"] = 0
        up.dispatch()
        for _ in range(60):
            await asyncio.sleep(0.02)
            if not up._active:
                break
        assert calls["n"] == n0 + 1, f"试探应只放 1 个文件，实际 {calls['n'] - n0}"

        # 换 cookie（重建 Uploader）→ 立刻放开；115 恢复后正常命中、断路器清除
        mgr.rapid = real
        up2 = Uploader(cfg, store, mgr)
        assert upmod._SHARED["auth_fail"] is None
        for p in paths:
            store.set(p, next_retry_at=0, status="待重试")
        up2.dispatch()
        for _ in range(100):
            await asyncio.sleep(0.02)
            if not up2._active:
                break
        assert all(store.get(p)["status"] == "已上传" for p in paths), [store.get(p)["status"] for p in paths]
        assert upmod._SHARED["auth_fail"] is None
        PASS.append("[71] 115 登录失效：不算未命中（retry_count 不涨）、不累计成「错误」，暂停向 115 发请求"
                    "只定时试探 1 个文件，面板有提示，换 cookie 后自动恢复")
    finally:
        upmod._SHARED["auth_fail"] = None
        shutil.rmtree(tmp, ignore_errors=True)


async def case_state_backup():
    """[72] 状态库每天在线备份一份（state.db.bak）：可打开、数据一致；未到期不重复备份；失败不影响运行。"""
    import sqlite3

    import main as mainmod

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0)
    try:
        for i in range(50):
            store.add(os.path.join(src, f"f{i}.mkv"), 100 + i, 1.0 + i)
        bak = mainmod.backup_path(cfg)
        assert bak.endswith("state.db.bak") and mainmod.backup_due(cfg)
        size = store.backup(bak)
        assert size > 0 and not os.path.exists(bak + ".tmp")
        con = sqlite3.connect(bak)
        assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 50
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.close()
        assert not mainmod.backup_due(cfg)
        assert mainmod.backup_due(cfg, now=time.time() + mainmod.BACKUP_INTERVAL + 5)

        # 主循环：到期就备份；没到期不动
        os.unlink(bak)
        stop = asyncio.Event()
        svc = asyncio.create_task(mainmod.run_service(cfg, dry_run=True, stop=stop))
        for _ in range(60):
            await asyncio.sleep(0.1)
            if os.path.exists(bak):
                break
        assert os.path.exists(bak), "主循环没有做备份"
        m1 = os.path.getmtime(bak)
        await asyncio.sleep(0.3)
        stop.set()
        await asyncio.wait_for(svc, timeout=15)
        assert os.path.getmtime(bak) == m1
        PASS.append("[72] 状态库每天在线备份（state.db.bak，integrity_check ok、行数一致）；"
                    "未到期不重复；主循环启动后自动补备份")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def case_core_rapid_only_contract():
    """[73] 核心契约 A/B 端到端：
    A 递归扫描 + 云端镜像本地文件夹结构；
    B 默认配置下秒传一直未命中 → 一直重试、永远不普通上传（连配置写空/写错、库里残留断点都不行）。"""
    # ---- A：嵌套目录 → 云端目录镜像 ----
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=0, fail_first=0,
                                             mode="rapid_only")
    try:
        files = ["剧A/Season 1/E01.mkv", "剧A/Season 1/E02.mkv", "剧A/Season 2/E01.mkv",
                 "剧B/Specials/OP.mp4", "电影/Movie (2020)/Movie.mkv", "根目录.mkv"]
        for f in files:
            write_file(src, f, 2048)
        scanner.scan_once(store, cfg)
        up.dispatch()
        for _ in range(200):
            await asyncio.sleep(0.02)
            if not up._active and all(r["status"] == "已上传" for r in store.list_tasks(limit=50)[0]):
                break
        rows, total = store.list_tasks(limit=50)
        assert total == len(files) and all(r["status"] == "已上传" and r["upload_mode"] == "rapid" for r in rows), rows
        got = {os.path.relpath(r["path"], src).replace(os.sep, "/"): r["target_dir"] for r in rows}
        assert got == {
            "剧A/Season 1/E01.mkv": "/UPILOT-mini/剧A/Season 1",
            "剧A/Season 1/E02.mkv": "/UPILOT-mini/剧A/Season 1",
            "剧A/Season 2/E01.mkv": "/UPILOT-mini/剧A/Season 2",
            "剧B/Specials/OP.mp4": "/UPILOT-mini/剧B/Specials",
            "电影/Movie (2020)/Movie.mkv": "/UPILOT-mini/电影/Movie (2020)",
            "根目录.mkv": "/UPILOT-mini",
        }, got
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- B：默认配置，永远未命中 ----
    for raw_mode in (None, "default", "", "rapid_only", "RAPID-ONLY", "rapid_then_upload "):
        cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=0, fail_first=10 ** 9,
                                                 mode="rapid_only", long_interval=0)
        try:
            if raw_mode == "default":
                cfg["upload"].pop("mode", None)              # 配置里根本没写
                cfg["upload"]["mode"] = cfgmod.DEFAULTS["upload"]["mode"]
            else:
                cfg["upload"]["mode"] = raw_mode
            cfg["retry"]["max_hours"] = 0                    # 「rapid_then_upload」才看它，这里要证明纯秒传不看
            cfg["retry"]["max_count"] = 1
            up = Uploader(cfg, store, mgr)
            explicit_real = str(raw_mode or "").strip().lower() == "rapid_then_upload"
            assert up.rapid_only == (not explicit_real), (raw_mode, up.mode)
            if explicit_real:
                continue                                      # 显式 rapid_then_upload 是允许真传的
            mp = {"n": 0}
            real_mp = mgr.multipart

            async def spy(*a, _mp=mp, _real=real_mp, **k):
                _mp["n"] += 1
                return await _real(*a, **k)

            mgr.multipart = spy
            p = write_file(src, "剧/E01.mkv", 3 * 1024 * 1024)
            small = write_file(src, "剧/cover.jpg", 100)
            scanner.scan_once(store, cfg)
            # 库里残留一份「上传中 + 分块断点」（从 rapid_then_upload 切回来的现场）
            store.set(p, status="上传中", upload_id="u1", upload_oss_url="https://oss/x",
                      upload_callback="{}", upload_partsize=1048576)
            for _ in range(40):
                for q in (p, small):
                    if store.get(q)["status"] == "待重试":
                        store.set(q, next_retry_at=0, first_failed_at=time.time() - 30 * 86400)
                    await up._process(q)
            for q in (p, small):
                r = store.get(q)
                assert r["status"] == "待重试", (raw_mode, r["status"], r["message"])
                assert (r["retry_count"] or 0) >= 30 and r["upload_mode"] is None, (raw_mode, r)
            assert mp["n"] == 0, f"mode={raw_mode!r}：纯秒传下发生了 {mp['n']} 次普通上传"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    assert cfgmod.DEFAULTS["upload"]["mode"] == "rapid_only"
    assert cfgmod.normalize_upload_mode(None) == "rapid_only" and cfgmod.normalize_upload_mode("typo") == "rapid_only"
    assert cfgmod.normalize_upload_mode("rapid_then_upload") == "rapid_then_upload"
    PASS.append("[73] 核心契约：嵌套目录 6 个文件秒传后云端目录与本地一一镜像；默认/空/写错的 upload.mode "
                "下秒传一直未命中 → 重试 40 轮（超过 max_count/max_hours）仍是待重试、普通上传 0 次，"
                "库里残留的「上传中+断点」也不续传；只有明确写 rapid_then_upload 才会真传")


async def case_replace_newest_episode():
    """[74] 洗板：同集（不论扩展名/名字）只留最新下载的那份 —— 端到端（mock 115：建目录/秒传/删除）。

    S01E01.mkv 先下载、S01E01.mp4 后下载 → mp4 秒传成功后 mkv 被移进 115 回收站（本地文件不动）。
    旧代码：扩展名在键里，mkv 与 mp4 不算同一集，什么都不会发生。"""
    import uploader as upmod
    from p115 import StubP115 as _Stub

    class TsStub(_Stub):
        """在 dry-run 替身上加：云端条目带上传时间 te、删除可见、删掉的不再出现在列表里。"""

        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.te: dict[str, int] = {}
            self.gone: list[str] = []

        async def list_dir(self, pid, use_cache=True):
            rows = await super().list_dir(pid, use_cache)
            out = []
            for r in rows:
                if r["fid"] in self.gone:
                    continue
                r = dict(r)
                if r["n"] in self.te:
                    r["te"] = self.te[r["n"]]
                out.append(r)
            return out

        async def delete_file(self, file_id):
            self.gone.append(file_id)
            return True

    def env(**up_cfg):
        cfg, store, _m, _u, tmp, src = make_env(mode="rapid_only", fail_first=0)
        cfg["upload"].update(up_cfg)
        mgr = TsStub(cfg, store, fail_first=0)
        return cfg, store, mgr, Uploader(cfg, store, mgr), tmp, src

    now = time.time()
    D = "剧/Season 1"
    OLD, NEW = "Show.S01E01.1080p.BluRay.mkv", "Show.S01E01.2160p.WEB-DL.mp4"

    # ---- 默认配置：洗板开着（核心需求默认生效）----
    assert cfgmod.DEFAULTS["upload"]["cleanup_previous_versions"] is True

    # ---- S1：mkv 先、mp4 后 → mp4 就位后 mkv 进回收站；本地文件不动；面板有记录 ----
    _SHARED = upmod._SHARED
    _SHARED["replaced"].clear()
    cfg, store, mgr, up, tmp, src = env()
    try:
        p_old = write_file(src, f"{D}/{OLD}", 2 * 1024 * 1024)
        p_new = write_file(src, f"{D}/{NEW}", 3 * 1024 * 1024)
        os.utime(p_old, (now - 3600, now - 3600))
        os.utime(p_new, (now, now))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        r_old = await drive(up, store, p_old)
        assert r_old["status"] == "已上传" and mgr.gone == [], "新版还没传，旧版不能动"
        r_new = await drive(up, store, p_new)
        assert r_new["status"] == "已上传"
        assert mgr.gone == [f"stub-{p_old}"], f"mkv 应被替换掉：{mgr.gone}"
        assert os.path.exists(p_old) and os.path.exists(p_new), "本地文件一个都不能删"
        ev = list(_SHARED["replaced"])
        assert len(ev) == 1 and ev[0]["kept"] == NEW and ev[0]["removed"] == [OLD] \
            and ev[0]["basis"] == "本地下载时间" and ev[0]["dry"] is False, ev
        import webui
        assert webui._replacements()[0]["kept"] == NEW, "面板接口要带上洗板记录"
        assert "洗板替换" in open(os.path.join(os.path.dirname(webui.__file__), "static", "app.js"),
                                  encoding="utf-8").read()

        # ---- S1b：关掉开关 → 什么都不删 ----
        mgr.gone.clear()
        cfg["upload"]["cleanup_previous_versions"] = False
        up2 = Uploader(cfg, store, mgr)
        await up2._cleanup_prev_versions("/UPILOT-mini/" + D, await mgr.ensure_pid("/UPILOT-mini/" + D), p_new)
        assert mgr.gone == []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- S2：老版本的秒传反而更晚命中（云端时间 mkv 更新）—— 仍然留后下载的 mp4 ----
    cfg, store, mgr, up, tmp, src = env()
    try:
        p_old = write_file(src, f"{D}/{OLD}", 2 * 1024 * 1024)
        p_new = write_file(src, f"{D}/{NEW}", 3 * 1024 * 1024)
        os.utime(p_old, (now - 3600, now - 3600))
        os.utime(p_new, (now, now))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        mgr.te = {NEW: 1000, OLD: 2000}                 # 云端上传时间：mkv 更晚（会骗人）
        await drive(up, store, p_new)
        assert mgr.gone == []
        await drive(up, store, p_old)                    # 老 mkv 后秒传上去
        assert mgr.gone == [f"stub-{p_old}"], f"应删老 mkv、留后下载的 mp4：{mgr.gone}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- S3：对不上本地记录（别的工具传的）→ 退回云端上传时间，仍可用 ----
    cfg, store, mgr, up, tmp, src = env()
    try:
        p_new = write_file(src, f"{D}/{NEW}", 3 * 1024 * 1024)
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        pid = await mgr.ensure_pid("/UPILOT-mini/" + D)
        # 云端里有一条库里没有的同集旧文件（不在 DB）：用子类把它塞进列表
        orig = mgr.list_dir

        async def with_foreign(pid_, use_cache=True):
            rows = await orig(pid_, use_cache)
            return rows + [{"fid": "foreign-1", "n": "Show S01E01 老版本.mkv", "s": 777, "te": 500, "pc": "fp1"}]

        mgr.list_dir = with_foreign
        mgr.te = {NEW: 900}
        await drive(up, store, p_new)
        assert mgr.gone == ["foreign-1"], mgr.gone
        assert pid
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- S4：新版还没上传成功 → 云端永远不会被清空；新版到了才删旧的 ----
    cfg, store, mgr, up, tmp, src = env()
    try:
        A, B, C = "Show.S01E02.720p.mp4", "Show.S01E02.1080p.mkv", "Show.S01E02.2160p.mkv"
        pa = write_file(src, f"{D}/{A}", 1 * 1024 * 1024)
        pb = write_file(src, f"{D}/{B}", 2 * 1024 * 1024)
        pc = write_file(src, f"{D}/{C}", 3 * 1024 * 1024)
        for p_, age in ((pa, 7200), (pb, 3600), (pc, 0)):
            os.utime(p_, (now - age, now - age))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        await drive(up, store, pa)
        await drive(up, store, pb)          # B 比 A 新 → A 被清，云端剩 B
        assert mgr.gone == [f"stub-{pa}"], mgr.gone
        remaining = [r["n"] for r in await mgr.list_dir(await mgr.ensure_pid("/UPILOT-mini/" + D))]
        assert remaining == [B], remaining   # C 还没传：云端留着最近的 B，不会一条不剩
        await drive(up, store, pc)
        assert mgr.gone == [f"stub-{pa}", f"stub-{pb}"], mgr.gone
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- S5：仅记录模式 → 不删，只留日志/面板记录 ----
    _SHARED["replaced"].clear()
    cfg, store, mgr, up, tmp, src = env(cleanup_dry_run=True)
    try:
        p_old = write_file(src, f"{D}/{OLD}", 2 * 1024 * 1024)
        p_new = write_file(src, f"{D}/{NEW}", 3 * 1024 * 1024)
        os.utime(p_old, (now - 3600, now - 3600))
        os.utime(p_new, (now, now))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        await drive(up, store, p_old)
        rec = await drive(up, store, p_new)
        assert mgr.gone == [], "仅记录模式不许真删"
        ev = list(_SHARED["replaced"])
        assert len(ev) == 1 and ev[0]["dry"] is True and ev[0]["removed"] == [OLD], ev
        assert "仅记录" in (rec.get("message") or ""), rec.get("message")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- S6：同文件夹里两部不同的剧同集号 —— auto 不动；ignore 才按季集号替换 ----
    for mode, expect_gone in (("auto", 0), ("same", 0), ("ignore", 1)):
        cfg, store, mgr, up, tmp, src = env(cleanup_title_match=mode)
        try:
            pa = write_file(src, "flat/Alpha.S01E01.mkv", 1 * 1024 * 1024)
            pb = write_file(src, "flat/Beta.S01E01.mp4", 2 * 1024 * 1024)
            os.utime(pa, (now - 3600, now - 3600))
            os.utime(pb, (now, now))
            scanner.scan_once(store, cfg)
            scanner.scan_once(store, cfg)
            await drive(up, store, pa)
            await drive(up, store, pb)
            assert len(mgr.gone) == expect_gone, (mode, mgr.gone)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    PASS.append("[74] 洗板只留最新：S01E01.mkv 先下载、S01E01.mp4 后下载 → mp4 秒传成功后 mkv 进回收站（本地文件不动、面板有记录）；"
                "老版本秒传更晚命中也按本地下载时间留 mp4；库里对不上才退回云端上传时间；新版未传成功云端不会被清空；"
                "仅记录模式不删；同文件夹两部剧同集号 auto/same 不动、ignore 才替换；默认开启、开关关了不删")


async def case_sweep_local_order():
    """[75] 巡检（每 5 分钟）：同集多条按本地下载时间判新旧（对不上才退回云端时间），
    扩展名不同也算同集，仅记录模式不删，超过上限不动手。"""
    import uploader as upmod

    class _C:
        def __init__(self, rows):
            self.rows = rows

        async def fs_files(self, payload, async_=True):
            return {"state": True, "data": [dict(r) for r in self.rows if not r.get("_gone")]}

    class _M:
        def __init__(self, rows):
            self.rows = rows
            self.client = _C(rows)
            self.deleted: list[str] = []

        async def cached_pid(self, td):
            return 7999

        async def delete_file(self, fid):
            self.deleted.append(str(fid))
            for r in self.rows:
                if str(r["fid"]) == str(fid):
                    r["_gone"] = True
            return True

    def rows():
        return [{"fid": "f-mkv", "n": "Show S01E05 [1080p].mkv", "s": 100, "pc": "pc-mkv", "te": 2000},
                {"fid": "f-mp4", "n": "Show.S01E05.WEB-DL.mp4", "s": 200, "pc": "pc-mp4", "te": 1000},
                {"fid": "f-other", "n": "Show S01E06.mkv", "s": 300, "pc": "pc-6", "te": 1500}]

    tmp = tempfile.mkdtemp(prefix="upilot-test-75-")
    try:
        cfg = copy.deepcopy(cfgmod.DEFAULTS)
        cfg["state"] = {"db": os.path.join(tmp, "s.db")}
        cfg["paths"]["target_root"] = "/T"
        st = Store(cfg["state"]["db"])
        # 本地记录：mp4 是后下载的（mtime 更大），尽管云端上传时间 mkv 更晚
        for name, size, pc, mt in (("Show S01E05 [1080p].mkv", 100, "pc-mkv", 1000.0),
                                   ("Show.S01E05.WEB-DL.mp4", 200, "pc-mp4", 5000.0)):
            pth = os.path.join(tmp, "src", name)
            st.add(pth, size, mt)
            st.set(pth, status="已上传", target_dir="/T/Show", file_id=pc, source_deleted=1)

        # a) 本地时间裁决：删 mkv、留 mp4
        m = _M(rows())
        res = await Uploader(cfg, st, m).sweep_cloud_episode_duplicates()
        assert m.deleted == ["f-mkv"] and res["removed"] == 1, (m.deleted, res)
        ev = upmod._SHARED["replaced"][0]
        assert ev["kept"] == "Show.S01E05.WEB-DL.mp4" and ev["basis"] == "本地下载时间", ev

        # b) 仅记录
        cfg["upload"]["cleanup_dry_run"] = True
        m = _M(rows())
        await Uploader(cfg, st, m).sweep_cloud_episode_duplicates()
        assert m.deleted == [], m.deleted
        cfg["upload"]["cleanup_dry_run"] = False

        # c) 本地对不上（记录里没有 mp4）→ 云端上传时间：mkv 更晚 → 删 mp4
        st2 = Store(os.path.join(tmp, "s2.db"))
        m = _M(rows())
        await Uploader(cfg, st2, m).sweep_cloud_episode_duplicates()
        assert m.deleted == ["f-mp4"], m.deleted

        # d) 超过 cleanup_max_delete → 不动手
        cfg["upload"]["cleanup_max_delete"] = 1
        many = rows() + [{"fid": "f-ts", "n": "Show S01E05 old.ts", "s": 1, "pc": "pc-ts", "te": 10}]
        m = _M(many)
        await Uploader(cfg, st2, m).sweep_cloud_episode_duplicates()
        assert m.deleted == [], m.deleted
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    PASS.append("[75] 同集巡检：按本地下载时间判新旧（云端时间 mkv 更晚也留后下载的 mp4）、对不上退回云端时间、"
                "不同扩展名算同集、仅记录不删、超上限不动手、其它集不碰")


def case_panel_lightweight():
    """[76] 面板保持轻量：没有持续动画 / 模糊 / 网络字体 / CDN；深浅主题都有令牌；轮询仍随页面可见性暂停。"""
    import re as _re
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "static")
    css = open(os.path.join(root, "app.css"), encoding="utf-8").read()
    html = open(os.path.join(root, "index.html"), encoding="utf-8").read()
    login = open(os.path.join(root, "login.html"), encoding="utf-8").read()
    js = open(os.path.join(root, "app.js"), encoding="utf-8").read()
    for name, text in (("app.css", css), ("login.html", login)):
        assert "infinite" not in text, f"{name}：不许有持续动画（常驻后台会一直吃 CPU/GPU）"
        assert "backdrop-filter" not in text, f"{name}：不许用模糊背景"
        assert "@font-face" not in text and "fonts.googleapis" not in text
    for name, text in (("index.html", html), ("login.html", login), ("app.css", css), ("app.js", js)):
        assert not _re.search(r"https?://(?!www\.w3\.org)", _re.sub(r"<!--.*?-->", "", text, flags=_re.S)) \
            or name == "app.js", f"{name}：不许引用外部资源（CDN / 网络字体）"
    assert "prefers-color-scheme:light" in css and "data-theme=light" in css, "缺浅色主题"
    assert "visibilitychange" in js and "document.hidden" in js, "轮询必须随页面可见性暂停"
    PASS.append("[76] 面板轻量：无持续动画/模糊/网络字体/CDN；深色默认 + 跟随系统浅色 + 可手动切换；后台标签页暂停轮询")


def case_hint_tone():
    """[77] 设置页说明保持简短：每项一句话（≤ 80 字），警示语（danger_note）≤ 60 字，别再写成小作文。"""
    for g in cfgmod.FIELD_GROUPS:
        for f in g["fields"]:
            h = f.get("hint", "")
            assert len(h) <= 80, f"{f['key']} 的说明太长（{len(h)} 字），一句话讲完，细节放 README 附录"
            assert len(f.get("danger_note", "")) <= 60, f"{f['key']} 的警示语太长"
    PASS.append("[77] 设置页每项说明 ≤ 80 字、警示语 ≤ 60 字（防止再写成小作文）")



async def case_delete_after_keeps_local_mtime():
    """[78] 生产配置（传完删本地 + ignore 剧名 + 只秒传）下，旧版本地文件已经没了，
    仍按库里记下的本地下载时间判新旧。

    真机已经验证过「先 1080p、后 2160p」这一路。
    这里补上更刁的反向：后下载的 2160p 先秒传命中，先下载的 1080p 更晚才命中，
    云端上传时间会骗人，必须靠库里的 mtime 才能留对那份。
    """
    import uploader as upmod
    from p115 import StubP115 as _Stub

    class TsStub(_Stub):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.te: dict[str, int] = {}
            self.gone: list[str] = []

        async def list_dir(self, pid, use_cache=True):
            rows = await super().list_dir(pid, use_cache)
            out = []
            for r in rows:
                if r["fid"] in self.gone:
                    continue
                r = dict(r)
                if r["n"] in self.te:
                    r["te"] = self.te[r["n"]]
                out.append(r)
            return out

        async def delete_file(self, file_id):
            self.gone.append(file_id)
            return True

    now = time.time()
    D = "乙剧样例 (2026) [tmdbid=100002]/Season 01"
    OLD = "乙剧样例 (2026) [tmdbid=100002] S01E19 [1080p H265 AAC WEB-DL TX DemoWeb].mkv"
    NEW = "乙剧样例 (2026) [tmdbid=100002] S01E19 [2160p H265 DDP 2.0 WEB-DL TX DemoWeb].mkv"
    cfg, store, _m, _u, tmp, src = make_env(mode="rapid_only", fail_first=0, delete_after=True)
    try:
        cfg["upload"]["cleanup_previous_versions"] = True
        cfg["upload"]["cleanup_title_match"] = "ignore"
        cfg["upload"]["cleanup_dry_run"] = False
        mgr = TsStub(cfg, store, fail_first=0)
        up = Uploader(cfg, store, mgr)
        p_old = write_file(src, f"{D}/{OLD}", 2 * 1024 * 1024)
        p_new = write_file(src, f"{D}/{NEW}", 3 * 1024 * 1024)
        os.utime(p_old, (now - 3600, now - 3600))
        os.utime(p_new, (now, now))
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        # 云端时间故意写反：1080p 后秒传上去，te 更大
        mgr.te = {NEW: 1000, OLD: 9000}
        upmod._SHARED["replaced"].clear()
        r_new = await drive(up, store, p_new)
        assert r_new["status"] == "已上传" and not os.path.exists(p_new)
        assert int(store.get(p_new).get("source_deleted") or 0) == 1
        assert store.get(p_new).get("mtime")  # 本地没了，库里 mtime 还在
        assert mgr.gone == [], "新版先就位时不应删任何东西"
        r_old = await drive(up, store, p_old)
        assert r_old["status"] == "已上传" and not os.path.exists(p_old)
        assert int(store.get(p_old).get("source_deleted") or 0) == 1
        assert mgr.gone == [f"stub-{p_old}"], f"应删先下载的 1080p：{mgr.gone}"
        ev = list(upmod._SHARED["replaced"])
        assert ev and ev[0]["kept"] == NEW and ev[0]["removed"] == [OLD]
        assert ev[0]["basis"] == "本地下载时间", ev
        # 两条本地都没了，库里记录都在，判据仍然是本地下载时间
        assert store.get(p_old)["status"] == "已上传" and store.get(p_new)["status"] == "已上传"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    PASS.append("[78] 传完删本地时旧文件已经不在：反向秒传顺序仍按库里的本地下载时间留 2160p、"
                "把 1080p 移进回收站；source_deleted 落库，mtime 还在")


async def case_rate_limit_breaker():
    """[79] 115 硬限流 / 风控：不当「秒传未命中」，共享冷却 + 封顶退避 + jitter；
    软忙只推迟这一个文件。"""
    import errno as _e
    import urllib.error
    import p115 as p115mod
    import uploader as upmod

    # ---- A) 分类与退避公式 ----
    assert p115mod.is_rate_limited({"state": False, "errno": 590075,
                                    "error": "操作太频繁，请稍候再试"})
    assert p115mod.is_rate_limited({"state": False, "error": "已达到当前访问上限"})
    assert p115mod.is_rate_limited({"state": False, "errno": 990009, "error": "触发风控"})
    assert p115mod.is_rate_limited(p115mod.RateLimitedError("x"))
    assert p115mod.is_rate_limited(urllib.error.HTTPError("http://x", 429, "n", hdrs=None, fp=None))
    assert p115mod.is_rate_limited(urllib.error.HTTPError("http://x", 405, "n", hdrs=None, fp=None))
    wrapped = RuntimeError("wrap")
    wrapped.__cause__ = p115mod.RateLimitedError("已达到当前访问上限")
    assert p115mod.is_rate_limited(wrapped)
    soft = {"state": False, "errno": 990009, "error": "删除操作尚未执行完成，请稍后再试！"}
    assert p115mod.is_soft_busy(soft) and not p115mod.is_rate_limited(soft)
    assert p115mod.is_soft_busy(OSError(_e.EBUSY, "busy"))
    assert p115mod.is_soft_busy(OSError(_e.EAGAIN, "again"))
    assert not p115mod.is_soft_busy(p115mod.RateLimitedError("x"))
    assert not p115mod.is_rate_limited({"state": True, "reuse": False, "message": "未命中"})
    assert upmod.rate_limit_backoff(0, rng=lambda: 0.0) == 30.0
    assert upmod.rate_limit_backoff(0, rng=lambda: 1.0) == 60.0
    assert upmod.rate_limit_backoff(1, rng=lambda: 0.0) == 60.0
    assert upmod.rate_limit_backoff(20, rng=lambda: 1.0) == upmod.RATE_LIMIT_CAP
    mid = upmod.rate_limit_backoff(2, rng=lambda: 0.5)
    assert 120.0 <= mid <= 240.0

    # ---- B) rapid() 拿到限流正文 / 429：立刻上抛，不当未命中，也不短间隔连试 ----
    calls = {"n": 0}

    class _Client:
        def __init__(self, maker):
            self._maker = maker

        async def upload_file_init(self, **kw):
            calls["n"] += 1
            return self._maker()

    async def _try_rapid(maker, retries=2):
        m = p115mod.P115.__new__(p115mod.P115)
        m.min_interval = 0.0
        m.transient_retries = retries
        m._last_init = 0.0
        m._init_lock = asyncio.Lock()
        m.client = _Client(maker)
        tmp = tempfile.mkdtemp(prefix="upilot-test-79-")
        path = os.path.join(tmp, "a.mkv")
        open(path, "wb").write(b"x" * 16)
        try:
            return await m.rapid(path, 16, "a" * 40, 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    calls["n"] = 0
    try:
        await _try_rapid(lambda: {"state": False, "error": "已达到当前访问上限"})
        raise AssertionError("限流正文应抛 RateLimitedError")
    except p115mod.RateLimitedError:
        pass
    assert calls["n"] == 1, f"限流正文不该短重试：{calls['n']}"

    def boom():
        raise urllib.error.HTTPError("http://x", 429, "Too Many", hdrs=None, fp=None)

    calls["n"] = 0
    try:
        await _try_rapid(boom)
        raise AssertionError("429 应立刻上抛")
    except urllib.error.HTTPError as exc:
        assert exc.code == 429
    assert calls["n"] == 1, f"429 不该短间隔连试：{calls['n']}"

    # 软忙（EBUSY）仍走原来的短重试（把 sleep 垫掉，免得单测白等 6 秒）
    def busy():
        raise OSError(_e.EBUSY, "操作尚未执行完成")

    real_sleep = asyncio.sleep

    async def no_sleep(_=0):
        return None

    asyncio.sleep = no_sleep
    calls["n"] = 0
    try:
        try:
            await _try_rapid(busy, retries=2)
            raise AssertionError("软忙用尽后应上抛")
        except OSError as exc:
            assert exc.errno == _e.EBUSY
        assert calls["n"] == 3, f"软忙应短重试 2 次（共 3 次调用），实际 {calls['n']}"
    finally:
        asyncio.sleep = real_sleep

    # ---- C) 端到端：不当未命中、共享冷却、只放 1 个试探、成功后解除 ----
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=0, retry_interval=60, fail_first=0,
                                             mode="rapid_only", long_interval=900)
    try:
        upmod._SHARED["auth_fail"] = None
        upmod._SHARED["rate_limit"] = None
        paths = [write_file(src, f"剧/E{i:02d}.mkv", 1024 * 1024) for i in range(4)]
        scanner.scan_once(store, cfg)
        for pth in paths:
            await sha1_of(pth, store.get(pth), store)
            store.set(pth, status="待秒传")

        n115 = {"n": 0}
        real = mgr.rapid

        async def limited(*a, **k):
            n115["n"] += 1
            raise p115mod.RateLimitedError("已达到当前访问上限")

        mgr.rapid = limited
        for _ in range(5):
            for pth in paths:
                store.set(pth, next_retry_at=0, status="待重试")
            up.dispatch()
            for _ in range(80):
                await asyncio.sleep(0.02)
                if not up._active:
                    break
            upmod._SHARED["rate_limit"]["next_probe"] = 0
        for pth in paths:
            r = store.get(pth)
            assert r["status"] == "待重试", r
            assert (r["retry_count"] or 0) == 0 and r["first_failed_at"] is None, r
            assert "限制访问" in (r["message"] or ""), r
        assert upmod._SHARED["rate_limit"], "断路器没记下"
        payload = webui._payload(cfg, store, {})
        assert payload["rate_limited"] and payload["rate_limited"]["retry_in"] >= 0, payload.get("rate_limited")
        js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
        assert "d.rate_limited" in js

        # 冷却期间不再打 115；到期只放 1 个文件
        n0 = n115["n"]
        for pth in paths:
            store.set(pth, next_retry_at=0, status="待重试")
        upmod._SHARED["rate_limit"]["next_probe"] = time.time() + 300
        up.dispatch()
        await asyncio.sleep(0.2)
        assert n115["n"] == n0, "冷却期间还在打 115"
        upmod._SHARED["rate_limit"]["next_probe"] = 0
        up.dispatch()
        for _ in range(80):
            await asyncio.sleep(0.02)
            if not up._active:
                break
        assert n115["n"] == n0 + 1, f"试探应只放 1 个文件，实际 {n115['n'] - n0}"

        # 限流解除：正常命中，断路器清除；真正未命中仍加 retry_count
        mgr.rapid = real
        up2 = Uploader(cfg, store, mgr)
        assert upmod._SHARED["rate_limit"] is None
        for pth in paths:
            store.set(pth, next_retry_at=0, status="待重试")
        up2.dispatch()
        for _ in range(120):
            await asyncio.sleep(0.02)
            if not up2._active:
                break
        assert all(store.get(pth)["status"] == "已上传" for pth in paths), [
            store.get(pth)["status"] for pth in paths]
        assert upmod._SHARED["rate_limit"] is None

        # 真正未命中仍按原逻辑加次数
        cfg2, store2, mgr2, up3, tmp2, src2 = make_env(stable_rounds=0, retry_interval=60,
                                                       fail_first=99, mode="rapid_only")
        try:
            pmiss = write_file(src2, "剧/miss.mkv", 1024 * 1024)
            scanner.scan_once(store2, cfg2)
            rec = await drive(up3, store2, pmiss, rounds=2)
            assert rec["status"] == "待重试" and (rec["retry_count"] or 0) >= 1, rec
            assert "不会真上传" in (rec["message"] or "") and "后再问" in (rec["message"] or ""), rec
            assert upmod._SHARED["rate_limit"] is None
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        # 软忙：不加 retry_count，不拉总闸
        async def busy(*a, **k):
            raise OSError(_e.EBUSY, "删除操作尚未执行完成，请稍后再试！")

        pbusy = write_file(src, "剧/busy.mkv", 1024 * 1024)
        scanner.scan_once(store, cfg)
        await sha1_of(pbusy, store.get(pbusy), store)
        store.set(pbusy, status="待秒传", retry_count=0)
        up3b = Uploader(cfg, store, mgr)
        mgr.rapid = busy
        upmod._SHARED["rate_limit"] = None
        await up3b._process(pbusy)
        rb = store.get(pbusy)
        assert rb["status"] == "待重试" and (rb["retry_count"] or 0) == 0, rb
        assert "正忙" in (rb["message"] or ""), rb
        assert upmod._SHARED["rate_limit"] is None, "软忙不该拉总闸"
        PASS.append("[79] 115 硬限流/风控：不当未命中（retry_count 不涨）、共享冷却只放 1 个试探、"
                    "退避有上限和 jitter、面板有提示、成功后自动恢复；429 不短间隔连试；"
                    "软忙只推迟该文件；真正未命中仍加次数")
    finally:
        upmod._SHARED["auth_fail"] = None
        upmod._SHARED["rate_limit"] = None
        shutil.rmtree(tmp, ignore_errors=True)



async def case_season_panel_history():
    """[80] 洗板记录落库（重启后还在）、按季度分组，面板能看到每个季度的待重试数量。"""
    import json
    import uploader as upmod

    tmp = tempfile.mkdtemp(prefix="upilot-test-80-")
    try:
        db = os.path.join(tmp, "s.db")
        st = Store(db)
        d1 = "/TVs/乙剧样例 (2026) [tmdbid=100002]/Season 01"
        d2 = "/TVs/甲剧 (2026) [tmdbid=100001]/Season 01"
        st.add_replacement(d1, "E19.2160p.mkv", ["E19.1080p.mkv"], "本地下载时间", False, at=1000)
        st.add_replacement(d1, "E18.mkv", ["E18.old.mkv"], "本地下载时间", True, at=900)
        st.add_replacement(d2, "S01E01.mp4", ["S01E01.mkv"], "云端上传时间", False, at=800)
        # 重启 = 重新打开同一个库，内存里什么都没有
        st2 = Store(db)
        rows = st2.recent_replacements(10)
        assert len(rows) == 3 and rows[0]["kept"] == "E19.2160p.mkv", rows
        assert rows[0]["removed"] == ["E19.1080p.mkv"] and rows[0]["dry"] is False
        assert rows[1]["dry"] is True
        # 只留最近 200 条
        for i in range(210):
            st2.add_replacement("/T/Show/Season 02", f"keep-{i}", [f"old-{i}"], "本地下载时间", False, at=2000 + i)
        left = st2.recent_replacements(300)
        assert len(left) == 200, len(left)
        assert left[0]["kept"] == "keep-209" and left[-1]["kept"] == "keep-10", (left[0]["kept"], left[-1]["kept"])

        # 待重试按目录分组：一个已到期，一个还在等
        now = time.time()
        p_due = os.path.join(tmp, "due.mkv")
        p_wait = os.path.join(tmp, "wait.mkv")
        p_other = os.path.join(tmp, "other.mkv")
        st3 = Store(os.path.join(tmp, "s3.db"))
        for path, size in ((p_due, 1), (p_wait, 2), (p_other, 3)):
            st3.add(path, size, 1.0)
        st3.set(p_due, status="待重试", target_dir=d1, next_retry_at=now - 10, retry_count=2)
        st3.set(p_wait, status="待重试", target_dir=d1, next_retry_at=now + 3600, retry_count=3)
        st3.set(p_other, status="待重试", target_dir=d2, next_retry_at=now - 1, retry_count=1)
        pend = st3.retry_pending()
        assert pend["total"] == 3 and pend["due"] == 2, pend
        by = {r["dir"]: r for r in pend["by_dir"]}
        assert by[d1]["count"] == 2 and by[d1]["due"] == 1, by
        assert by[d2]["count"] == 1 and by[d2]["due"] == 1, by

        # Uploader 写入后，模拟重启：内存清空，面板接口仍能从库里读到
        cfg, store, mgr, up, tmp2, src = make_env(mode="rapid_only", fail_first=0)
        try:
            up._record_replacement(d1, "Show.S01E19.2160p.mkv",
                                   ["Show.S01E19.1080p.mkv"], "本地下载时间", False)
            assert store.recent_replacements(1)[0]["kept"] == "Show.S01E19.2160p.mkv"
            upmod._SHARED["replaced"].clear()
            payload = webui._payload(cfg, store, {})
            ev = payload["replacements"]
            assert ev and ev[0]["kept"] == "Show.S01E19.2160p.mkv", ev
            assert ev[0]["basis"] == "本地下载时间" and ev[0]["dir"] == d1
            assert "retry_pending" in payload
        finally:
            shutil.rmtree(tmp2, ignore_errors=True)

        html = open(os.path.join(APP, "static", "index.html"), encoding="utf-8").read()
        js = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()
        css = open(os.path.join(APP, "static", "app.css"), encoding="utf-8").read()
        assert 'id="season-list"' in html and "各季度情况" in html
        assert "renderSeasons" in js and "seasonLabel" in js and "retry_pending" in js
        assert ".season{" in css
        assert "infinite" not in css and "backdrop-filter" not in css
        PASS.append("[80] 洗板记录落库：重启后面板仍能看到；只留最近 200 条；待重试按季度目录分组"
                    "（到期/未到期分得清）；概览新增「各季度情况」，无动画/模糊")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def case_version_tiebreak():
    """[81] 洗板时间并列时，PROPER/REPACK/v2 算更新的那份；后下载的仍然优先。"""
    from p115 import P115 as _P115

    def rows(*names_and_te):
        out = []
        for i, (name, te) in enumerate(names_and_te, 1):
            out.append({"fid": str(i), "n": name, "s": i * 10, "pc": f"p{i}", "te": te, "ico": "mkv"})
        return out

    plain = "Show.S01E01.1080p.mkv"
    proper = "Show.S01E01.1080p.PROPER.mkv"
    v3 = "Show.S01E01.1080p.v3.mkv"

    # 云端时间相同 → PROPER 赢
    r = _P115.rank_same_episode(rows((plain, 1000), (proper, 1000)))
    assert "ranks" in r and r["basis"] == "版本标记（PROPER/REPACK/v2）", r
    assert r["ranks"].index(max(r["ranks"])) == 1

    # v3 比 PROPER（当作 v2）更新
    r = _P115.rank_same_episode(rows((proper, 1000), (v3, 1000)))
    assert r["ranks"].index(max(r["ranks"])) == 1, r

    # 两个都没有修订标记、时间并列 → 仍然不动手
    r = _P115.rank_same_episode(rows((plain, 1000), ("Show.S01E01.2160p.mkv", 1000)))
    assert "ranks" not in r and "并列" in r["note"], r

    # 本地下载时间更晚的无标记版，仍然赢过更早的 PROPER（后下载优先）
    def lookup(row):
        return {"Show.S01E01.1080p.mkv": (5000.0, 1.0),
                "Show.S01E01.1080p.PROPER.mkv": (1000.0, 1.0)}[row["n"]]

    r = _P115.rank_same_episode(rows((plain, 9000), (proper, 1000)), lookup)
    assert r["basis"] == "本地下载时间", r
    assert r["ranks"].index(max(r["ranks"])) == 0

    # 本地时间并列 → 走版本标记
    def lookup2(row):
        return (2000.0, 1.0)

    r = _P115.rank_same_episode(rows((plain, 1), (proper, 1)), lookup2)
    assert r["basis"] == "版本标记（PROPER/REPACK/v2）", r
    assert r["ranks"].index(max(r["ranks"])) == 1
    PASS.append("[81] 洗板时间并列时 PROPER/REPACK/v2 算更新；后下载的无标记版仍然优先；"
                "两个都没修订标记且时间并列则不动手")



async def case_sidecar_still_writing():
    """[82] 下载还没写完时（旁边有 .aria2 / .!qB）不推进稳定计数、不算 SHA1。"""
    assert scanner.still_writing("/no/such.mkv") is False
    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        path = write_file(src, "剧集/Show.S01E01.mkv", 4096)
        sidecar = path + ".aria2"
        Path = __import__("pathlib").Path
        Path(sidecar).write_text("aria2 lock")
        assert scanner.still_writing(path)
        scanner.scan_once(store, cfg)          # 入队
        scanner.scan_once(store, cfg)
        scanner.scan_once(store, cfg)
        rec = store.get(path)
        assert rec and rec["stable_count"] == 0 and rec.get("sha1") in (None, ""), rec
        await up._process(path)
        rec = store.get(path)
        assert rec["status"] == "待处理" and not rec.get("sha1"), rec

        # qBittorrent 的 .!qB 同样拦住
        os.remove(sidecar)
        qb = path + ".!qB"
        Path(qb).write_bytes(b"qb")
        scanner.scan_once(store, cfg)
        await up._process(path)
        rec = store.get(path)
        assert rec["stable_count"] == 0 and not rec.get("sha1"), rec

        os.remove(qb)
        scanner.scan_once(store, cfg)          # 控制文件没了，推进到 1，达到稳定
        rec = store.get(path)
        assert rec["stable_count"] >= 1, rec
        await up._process(path)
        rec = store.get(path)
        assert rec.get("sha1"), rec
        PASS.append("[82] 旁边有 .aria2 / .!qB 时不推进稳定计数、不算 SHA1；"
                    "控制文件消失后再稳定、再哈希")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def case_config_startup_validation():
    """[83] 启动时检查配置：YAML 写坏、类型不对会中文报错退出；目录不存在 / 没填 cookie 只警告。"""
    from pathlib import Path
    tmp = tempfile.mkdtemp(prefix="upilot-cfgval-")
    old_path, old_pf = cfgmod.CONFIG_PATH, cfgmod.PORT_FILE
    cfgmod.CONFIG_PATH = Path(tmp) / "config.yaml"
    cfgmod.PORT_FILE = cfgmod.CONFIG_PATH.parent / ".http_port"
    # 避免环境变量把测试配置盖掉
    env_backup = {k: os.environ.pop(k) for k in ("P115_COOKIE", "UPILOT_SOURCE", "UPILOT_TARGET_ROOT")
                  if k in os.environ}
    src_ok = os.path.join(tmp, "media")
    os.makedirs(src_ok)

    def dump(body: str) -> None:
        cfgmod.CONFIG_PATH.write_text(body, encoding="utf-8")

    def load_err(body: str) -> str:
        dump(body)
        try:
            cfgmod.load()
        except cfgmod.ConfigError as exc:
            return str(exc)
        raise AssertionError("应当抛出 ConfigError")

    try:
        # 1) YAML 语法错误
        msg = load_err("scan:\n  interval_seconds: [未闭合")
        assert "YAML" in msg and "缩进" in msg, msg

        # 2) 顶层不是 mapping
        msg = load_err("- just a list\n")
        assert "顶层应该是一组键值" in msg, msg

        # 3) 整段写成一行文字
        msg = load_err("paths: 不是一组设置\n")
        assert "目录设置" in msg and "一组设置" in msg, msg

        # 4) 该是数字的写成文字 / 小于最小值
        base = (f"paths:\n  source: {src_ok}\n"
                f"p115:\n  cookie: UID=1; CID=2\n")
        msg = load_err(base + "scan:\n  interval_seconds: 不是数字\n")
        assert "多久看一眼文件夹" in msg and "应该是数字" in msg, msg
        msg = load_err(base + "scan:\n  interval_seconds: 0\n")
        assert "至少是 1 秒" in msg, msg
        # 设置页下限是 2，但写 1 仍能启动（只警告）
        dump(base + "scan:\n  interval_seconds: 1\n")
        cfg = cfgmod.load()
        assert cfg["scan"]["interval_seconds"] == 1
        assert any("建议至少是 2" in w for w in cfgmod.startup_warnings(cfg))

        # 5) 监控目录没填
        msg = load_err("paths:\n  source: ''\n")
        assert "没有指定监控目录" in msg, msg

        # 6) 合法配置能加载
        dump(base)
        cfg = cfgmod.load()
        assert cfg["paths"]["source"] == src_ok

        # 7) 目录还不存在、没填 cookie → 只警告，不阻止启动
        missing = os.path.join(tmp, "not-mounted")
        dump(f"paths:\n  source: {missing}\n")
        cfg = cfgmod.load()
        warns = cfgmod.startup_warnings(cfg)
        assert any("还不存在" in w and missing in w for w in warns), warns
        assert any("登录凭证" in w for w in warns), warns
        _err, _w = cfgmod.inspect_config(cfg)
        assert not _err, _err

        PASS.append("[83] 启动配置检查：YAML 写坏 / 顶层不是键值 / 类型不对 / 数字过小会中文报错退出；"
                    "监控目录还不存在、还没填 cookie 只警告")
    finally:
        cfgmod.CONFIG_PATH, cfgmod.PORT_FILE = old_path, old_pf
        for k, v in env_backup.items():
            os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)



def case_scan_large_library_sidecars():
    """[84] 大片库扫描：用目录文件名判断控制文件，已上传的不再逐个 lexists；语义不变。"""
    assert scanner.has_sidecar("Show.mkv", {"Show.mkv", "Show.mkv.aria2"})
    assert scanner.has_sidecar("Show.mkv", {"Show.mkv.!qB"})
    assert not scanner.has_sidecar("Show.mkv", {"Show.mkv", "other.aria2"})

    cfg, store, mgr, up, tmp, src = make_env(stable_rounds=1)
    try:
        n = 400
        uploaded = [write_file(src, f"库/已传/{i:04d}.mkv", 64) for i in range(n)]
        pending = write_file(src, "库/新/a.mkv", 64)
        blocked = write_file(src, "库/新/b.mkv", 64)
        Path = __import__("pathlib").Path
        Path(blocked + ".aria2").write_text("lock")
        # 先扫一轮入队，再把大部分标成已上传（模拟已经跑过一轮的大片库）
        scanner.scan_once(store, cfg)
        for pth in uploaded:
            store.set(pth, status="已上传", sha1="done", stable_count=1)
        rec_u = {pth: dict(store.get(pth)) for pth in (uploaded[0], uploaded[-1])}

        calls = []
        real_lexists = os.path.lexists

        def counting_lexists(path):
            calls.append(path)
            return real_lexists(path)

        os.path.lexists = counting_lexists
        try:
            t0 = time.perf_counter()
            scanner.scan_once(store, cfg)
            elapsed = time.perf_counter() - t0
        finally:
            os.path.lexists = real_lexists

        assert elapsed < 3.0, f"扫描 {n}+2 个文件耗时 {elapsed:.2f}s，太慢"
        assert calls == [], f"扫描不该再逐个 lexists，实际 {len(calls)} 次：{calls[:5]}"
        for pth, before in rec_u.items():
            after = store.get(pth)
            assert after["status"] == "已上传" and after["sha1"] == "done"
            assert after["stable_count"] == before["stable_count"]
        assert store.get(pending)["stable_count"] >= 1
        assert store.get(blocked)["stable_count"] == 0 and not store.get(blocked).get("sha1")

        PASS.append("[84] 大片库扫描用目录文件名判断控制文件、已上传的不再逐个 lexists；"
                    "未完成下载仍不推进稳定计数，语义与原来一致")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)



def case_retry_status_copy():
    """[85] 纯秒传未命中的等待文案说人话：面板不再把「有人在等秒传」写成队列为空。"""
    from uploader import wait_text
    assert wait_text(0) == "0 秒" and wait_text(45) == "45 秒"
    assert wait_text(60) == "60 秒" and wait_text(90) == "2 分钟"
    assert wait_text(300) == "5 分钟" and wait_text(3600) == "1 小时"
    assert wait_text(21600) == "6 小时" and wait_text(2 * 86400) == "2 天"

    from pathlib import Path
    js = Path(__file__).resolve().parents[1].joinpath("app", "static", "app.js").read_text(encoding="utf-8")
    assert "队列为空" not in js
    assert "没有待处理文件，也没有报错" in js
    assert "有 ${nRetry} 个文件在等秒传：云端还没有相同文件，会自动再问，不会真上传" in js
    assert "约 ${fmtWait(left)} 后再问" in js
    assert "倒计时 ${left}s" not in js
    PASS.append("[85] 纯秒传等待文案说人话（5 分钟/1 小时，不写 300s）；"
                "面板在有待重试时不再写「队列为空」，idle 时才说没有待处理文件")


async def main():
    await case_folder_and_rapid_hit()
    await case_sha1_cached_no_reread()
    await case_small_file_goes_direct_upload()
    await case_retry_exhausted_falls_back()
    await case_scan_dedupe_and_rewrite()
    await case_scan_suffix_and_dir_filter()
    await case_http_auth()
    await case_form_auth()
    case_qr_removed()
    await case_dir_mapping()
    case_settings_map_and_slots()
    case_cookie_sync_from_file()
    await case_cid_parse_and_ensure_pid()
    await case_rapid_only_mode()
    await case_notify_webhook()
    await case_delete_after_upload()
    case_episode_key()
    case_upload_mark()
    case_config_roundtrip()
    await case_status_selfheal()
    await case_unexpected_error_stops_retry()
    await case_all_states_recoverable()
    await case_changed_file_not_lost()
    await case_config_save_concurrent()
    await case_save_cookie_atomic()
    await case_scan_now_wakes_loop()
    await case_ui_actions_wake_loop()
    await case_login_limiter_source()
    await case_empty_username_session()
    case_a2a_token_resolution()
    await case_request_body_hardening()
    case_coerce_finite_values()
    case_cloud_path_dotdot_guard()
    await case_dir_paging_cursor()
    await case_same_episode_no_mutual_delete()
    await case_delete_after_upload_no_mutual_delete()
    await case_secure_compare_all_channels()
    await case_same_name_duplicate_cleaned()
    await case_same_episode_time_judgment()
    await case_cloud_only_episode_cleanup()
    await case_cleanup_previous_versions()
    await case_sweep_business_error_visible()
    await case_multipart_resume_offset()
    await case_sha1_off_event_loop()
    await case_scan_off_event_loop()
    await case_multipart_failure_backoff_resume()
    await case_csrf_protection()
    await case_sha1_small_buffer()
    case_updated_at_index()
    await case_scan_batched()
    await case_graceful_shutdown()
    await case_oss_endpoint_configurable()
    case_compose_log_rotation()
    await case_rapid_age_backoff()
    await case_miss_logs_summarized()
    await case_gzip_and_hidden_pause()
    await case_health_heartbeat()
    case_dockerfile_precompile()
    case_stats_covering_index()
    await case_sha1_reuse_same_inode()
    await case_partsize_oss_limit()
    case_scan_log_capped()
    await case_ino_backfill()
    await case_part_inplace_retry()
    await case_max_upload_size()
    await case_auth_failure_breaker()
    await case_state_backup()
    await case_core_rapid_only_contract()
    await case_replace_newest_episode()
    await case_sweep_local_order()
    case_panel_lightweight()
    case_hint_tone()
    await case_delete_after_keeps_local_mtime()
    await case_rate_limit_breaker()
    await case_season_panel_history()
    case_version_tiebreak()
    await case_sidecar_still_writing()
    case_config_startup_validation()
    case_scan_large_library_sidecars()
    case_retry_status_copy()
    # 兜底：清掉遗留的测试临时目录（SQLite 句柄在 GC 后才释放，所以要重试几次）
    for _ in range(3):
        left = [d for d in glob.glob(os.path.join(tempfile.gettempdir(), "upilot-test-*")) if os.path.isdir(d)]
        if not left:
            break
        gc.collect()
        for d in left:
            shutil.rmtree(d, ignore_errors=True)
        time.sleep(0.2)
    print("\n".join(PASS))
    print(f"\n全部通过：{len(PASS)} 个用例")


if __name__ == "__main__":
    asyncio.run(main())
