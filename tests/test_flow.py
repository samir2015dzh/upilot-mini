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
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
sys.path.insert(0, APP)

import config as cfgmod          # noqa: E402
import scanner                   # noqa: E402
import webui                     # noqa: E402
from p115 import StubP115, resolve_upload_target   # noqa: E402
from store import Store          # noqa: E402
from uploader import Uploader, sha1_of   # noqa: E402

PASS: list[str] = []


def make_env(stable_rounds: int = 1, small_file_mb: float = 10.0, retry_max_count: int = 5,
             retry_interval: int = 0, fail_first: int = 2, mode: str = "rapid_then_upload",
             long_interval: int = 0):
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
        assert rec["retry_count"] == 1 and "纯秒传" in (rec["message"] or ""), rec
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
        assert "300s" in (rb["message"] or ""), rb["message"]     # 已切到长间隔
        assert rb["upload_mode"] is None, rb

        # 3) 调度器不派发「待上传」；一旦被处理也拉回重试队列
        store.set(big, status="待上传")
        up.dispatch()
        await asyncio.sleep(0.05)
        assert store.get(big)["status"] == "待上传", store.get(big)   # 纯秒传下不碰它
        await up._process(big)
        assert store.get(big)["status"] == "待重试", store.get(big)

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
